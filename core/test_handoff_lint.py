"""Линтер границ передачи (DEV-90).

Сессия обязана и отчитаться в задаче, и перевести её; сделать одно без
другого — тот промах, который по коду возврата не виден. Тесты держат
машинку: что она считает комментарием, что — переводом, и что на записанных
трассах client-b она находит ровно те промахи, ради которых заведена.
"""
import json
from pathlib import Path

import pytest

import handoff_lint as hl
from test_verify_order import session, tool

FIXTURE = Path(__file__).parent / "testdata" / "handoff-client-b-2026-09-15.json"

SAVE_COMMENT = tool("mcp__linear__save_comment", issueId="DEV-1", body="Проверка: OK")
MOVE_TO_REVIEW = tool("mcp__linear__save_issue", id="DEV-1", state="In Review",
                      assignee="andrey")
RENAME_ONLY = tool("mcp__linear__save_issue", id="DEV-1", title="новое имя")
SHELL_COMMENT = tool("Bash", command=(
    'python3 - <<EOF\nq="mutation($i:String!,$b:String!){commentCreate('
    'input:{issueId:$i,body:$b}){success}}"\nEOF'))
SHELL_MOVE = tool("Bash", command=(
    'curl -sS https://api.linear.app/graphql -d \'{"query":"mutation{issueUpdate('
    'id:\\"x\\",input:{stateId:\\"st-1\\"}){success}}"}\''))
WRITE_VERDICT = tool("Bash", command="cat > verdict.txt <<'EOF'\nOK\nEOF")


def verify(tmp_path, *events, files=("verdict.txt",)):
    d = session(tmp_path, *events)
    for f in files:
        (d / f).write_text("x")
    return d


# ── комментарий и перевод ────────────────────────────────────────────────────

def test_comment_and_transition_together_are_clean(tmp_path):
    r = hl.check(verify(tmp_path, SAVE_COMMENT, MOVE_TO_REVIEW))
    assert r["нарушения"] == []
    assert r["комментарий"] and r["перевод"] and r["состояние"] == "In Review"


def test_a_comment_without_a_transition_is_the_miss(tmp_path):
    """Ровно трасса DEV-46 (213025): get_issue, list_comments, save_comment —
    и ничего про состояние. Сессия вышла с кодом 0."""
    r = hl.check(verify(tmp_path, SAVE_COMMENT))
    assert r["нарушения"] == [hl.MISS_NO_TRANSITION]


def test_editing_the_title_is_not_a_transition(tmp_path):
    r = hl.check(verify(tmp_path, SAVE_COMMENT, RENAME_ONLY))
    assert r["нарушения"] == [hl.MISS_NO_TRANSITION]


def test_a_session_that_never_wrote_to_the_task_is_flagged_too(tmp_path):
    """DEV-80: 34 вызова, verdict.txt на месте, в задаче ни слова."""
    r = hl.check(verify(tmp_path, WRITE_VERDICT))
    assert r["нарушения"] == [hl.MISS_NO_COMMENT]


def test_an_empty_trace_is_not_a_handoff_miss(tmp_path):
    """Сессия не поднялась — нечего было передавать; это другой класс сбоя."""
    d = verify(tmp_path, files=())
    assert hl.check(d)["нарушения"] == ["нет verdict.txt"]


# ── в трекер ходят и из shell ────────────────────────────────────────────────

def test_a_shell_comment_counts_as_a_comment(tmp_path):
    """DEV-78 (233741): ключ личности мёртв, комментарий ушёл python-скриптом
    с GraphQL-мутацией. По одним MCP-именам это «без комментария», а на деле
    — «комментарий без перевода»."""
    r = hl.check(verify(tmp_path, SHELL_COMMENT))
    assert r["комментарий"] is True
    assert r["нарушения"] == [hl.MISS_NO_TRANSITION]


def test_a_shell_transition_counts_as_a_transition(tmp_path):
    r = hl.check(verify(tmp_path, SHELL_COMMENT, SHELL_MOVE))
    assert r["нарушения"] == []


def test_an_issue_update_without_state_or_assignee_is_not_a_transition(tmp_path):
    only_title = tool("Bash", command='… mutation{issueUpdate(id:"x",input:{title:"t"})} …')
    r = hl.check(verify(tmp_path, SHELL_COMMENT, only_title))
    assert r["нарушения"] == [hl.MISS_NO_TRANSITION]


# ── файлы по роли ────────────────────────────────────────────────────────────

def test_a_work_session_with_code_must_leave_a_review(tmp_path):
    d = session(tmp_path, SAVE_COMMENT,
                tool("mcp__linear__save_issue", id="DEV-1", state="Verifying"),
                name="DEV-1-20260915-101010")
    assert hl.check(d)["нарушения"] == ["нет review.md"]
    (d / "review.md").write_text("Находок нет.")
    assert hl.check(d)["нарушения"] == []


@pytest.mark.parametrize("state", ["Done", "Canceled"])
def test_a_closed_work_session_needs_no_review(tmp_path, state):
    """Исходы 2 и 3 контракта: справка или отказ — кода нет, ревьюить нечего."""
    d = session(tmp_path, SAVE_COMMENT,
                tool("mcp__linear__save_issue", id="DEV-1", state=state, assignee="pavel"),
                name="DEV-1-20260915-101010")
    assert hl.check(d)["нарушения"] == []


def test_closed_state_names_are_the_installations(tmp_path):
    d = session(tmp_path, SAVE_COMMENT,
                tool("mcp__linear__save_issue", id="DEV-1", state="Закрыто"),
                name="DEV-1-20260915-101010")
    assert hl.check(d)["нарушения"] == ["нет review.md"]
    assert hl.check(d, closed=("Закрыто",))["нарушения"] == []


def test_an_intake_session_must_leave_a_draft(tmp_path):
    d = session(tmp_path, tool("mcp__linear__list_issues", query="x"),
                name="INTAKE-abc-intake-20260915-101010")
    assert hl.check(d)["нарушения"] == ["нет draft.json"]


def test_kind_is_read_from_the_directory_name_like_the_runner_writes_it():
    assert hl.kind_of("DEV-46-verify-20260913-213025") == "verify"
    assert hl.kind_of("INTAKE-7bc73da54fc1-intake-20260912-013622") == "intake"
    assert hl.kind_of("DEV-1-incident-20260915-101010") == "incident"
    assert hl.kind_of("DEV-46-20260912-155908") == "work"


# ── прогон по записанным трассам client-b ─────────────────────────────────────
#
# 98 каталогов на 15.09.2026: 66 проверок, 29 рабочих, 3 приёма. Трассы
# сведены к вызовам, на которые смотрит линтер (`testdata/…`), без команд и
# ключей. Постановка DEV-90 называла семь промахов «комментарий без
# перевода» — линтер обязан находить ровно их. Нашёл ноль — не работает.

KNOWN_MISSES = [
    "DEV-46-verify-20260913-193043",
    "DEV-46-verify-20260913-213025",
    "DEV-47-verify-20260913-193049",
    "DEV-77-verify-20260913-192059",
    "DEV-78-verify-20260913-233741",
    "DEV-83-verify-20260914-010142",
    "DEV-84-verify-20260914-011923",
]


def replay(records, **kw):
    """Трассы в фикстуре сведены к значимым вызовам; сколько их было всего —
    отдельным числом, иначе работавшая молча сессия неотличима от не
    поднявшейся."""
    return {r["каталог"]: hl.judge(hl.kind_of(r["каталог"]), r["вызовы"], r["файлы"],
                                   total=r["вызовов всего"], **kw)
            for r in records}


def test_the_recorded_traces_yield_the_seven_known_misses():
    records = json.loads(FIXTURE.read_text())
    assert len(records) == 98
    got = replay(records)
    misses = [k for k, r in got.items() if hl.MISS_NO_TRANSITION in r["нарушения"]]
    assert misses == KNOWN_MISSES
    # Постановка говорила «три из них с кодом 0»; по stats.json — пять, у
    # двух (DEV-78, DEV-84) stats.json нет вовсе.
    rc = {r["каталог"]: r["код"] for r in records}
    assert [k for k in KNOWN_MISSES if rc[k] == 0] == [
        "DEV-46-verify-20260913-193043", "DEV-46-verify-20260913-213025",
        "DEV-47-verify-20260913-193049", "DEV-77-verify-20260913-192059",
        "DEV-83-verify-20260914-010142"]


def test_dev_78_is_found_only_because_shell_writes_count():
    """Калибровка: без разбора shell-мутаций седьмой промах терялся бы."""
    records = json.loads(FIXTURE.read_text())
    (rec,) = [r for r in records if r["каталог"] == "DEV-78-verify-20260913-233741"]
    assert not any(c["name"].startswith("mcp__linear__save") for c in rec["вызовы"])
    assert hl.judge("verify", rec["вызовы"], rec["файлы"])["нарушения"] == [hl.MISS_NO_TRANSITION]


def test_the_recorded_traces_tell_the_rest_of_the_boundary_story():
    records = json.loads(FIXTURE.read_text())
    got = replay(records)
    # Вердикт (или ничего) без единого слова в задаче — две проверки.
    no_comment = sorted(k for k, r in got.items() if hl.MISS_NO_COMMENT in r["нарушения"])
    assert no_comment == ["DEV-76-verify-20260913-182803", "DEV-80-verify-20260913-230650"]
    # `review.md` появился 14.09 вместе со скиллом: до него ни одна рабочая
    # сессия его не оставляла. Из 16 каталогов без файла четыре — закрытые
    # исходы без кода (DEV-13, DEV-32, DEV-55, DEV-65); 12 нарушений, из них
    # DEV-58 — пустой каталог без трассы, который сам линтер пропускает
    # (на живом прогоне их 11).
    no_review = [k for k, r in got.items() if "нет review.md" in r["нарушения"]]
    assert len(no_review) == 12
    assert all("-verify-" not in k and "-intake-" not in k for k in no_review)
    assert all(k < "DEV-99" for k in no_review)  # все до появления файла
    # Без verdict.txt — только две оборвавшиеся проверки.
    no_verdict = sorted(k for k, r in got.items() if "нет verdict.txt" in r["нарушения"])
    assert no_verdict == ["DEV-76-verify-20260913-182803", "DEV-84-verify-20260914-011923"]
    # Приёмы: все три оставили draft.json.
    assert all(r["нарушения"] == [] for k, r in got.items() if "-intake-" in k)


# ── командная строка ─────────────────────────────────────────────────────────

def test_cli_reports_and_exits_ten_on_a_miss(tmp_path, capsys):
    verify(tmp_path, SAVE_COMMENT)
    assert hl.main([str(tmp_path)]) == 10
    out = capsys.readouterr().out
    assert "НАРУШЕНИЕ" in out and hl.MISS_NO_TRANSITION in out
    assert "с нарушениями: 1" in out


def test_cli_exits_zero_when_clean_and_two_without_traces(tmp_path, capsys):
    verify(tmp_path, SAVE_COMMENT, MOVE_TO_REVIEW)
    assert hl.main([str(tmp_path)]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert hl.main([str(empty)]) == 2


def test_since_filters_by_the_directory_stamp(tmp_path):
    session(tmp_path, SAVE_COMMENT, name="DEV-1-verify-20260913-101010")
    session(tmp_path, SAVE_COMMENT, name="DEV-2-verify-20260915-101010")
    found = hl.find_sessions([tmp_path], since="20260915")
    assert [d.name for d in found] == ["DEV-2-verify-20260915-101010"]
