"""Порядок вызовов проверки по трассе (DEV-87).

Контракт велит записать эталон ДО того, как читать заявку автора. Слову
сессии тут верить нельзя — верить можно порядку инструментов в
`transcript.jsonl`. Эти тесты держат саму машинку: что она считает записью
эталона, что — чтением заявки, и на каких трассах молчит зря.
"""
import json
from pathlib import Path

import verify_order as vo

W = "/home/ubuntu/agent-work/DEV-1-verify-20260915-101010"


def tool(name, **inp):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use",
                                                "name": name, "input": inp}]}},
                      ensure_ascii=False)


def session(tmp_path, *events, name="DEV-1-verify-20260915-101010") -> Path:
    d = tmp_path / name
    d.mkdir()
    lines = ['{"type":"system","subtype":"init","session_id":"s1"}', *events,
             '{"type":"result","total_cost_usd":0.1}']
    (d / "transcript.jsonl").write_text("\n".join(lines) + "\n")
    return d


GET_ISSUE = tool("mcp__linear__get_issue", id="DEV-1")
LIST_COMMENTS = tool("mcp__linear__list_comments", issueId="DEV-1")
WRITE_REFERENCE = tool("Write", file_path=f"{W}/expected.md",
                       content="Источник критерия: задача\n")
SAVE_COMMENT = tool("mcp__linear__save_comment", issueId="DEV-1", body="OK")
WRITE_VERDICT = tool("Bash", command=f"cat > {W}/verdict.txt <<'EOF'\nOK всё\nEOF")


# ── эталон раньше заявки ─────────────────────────────────────────────────────

def test_reference_written_before_the_claim_is_clean(tmp_path):
    d = session(tmp_path, GET_ISSUE, WRITE_REFERENCE, LIST_COMMENTS,
                SAVE_COMMENT, WRITE_VERDICT)
    r = vo.check(d)
    assert r["нарушения"] == []
    assert r["эталон"] == 1 and r["заявка"] == 2


def test_claim_read_before_the_reference_is_a_violation(tmp_path):
    """Ровно тот порядок, что был у старого контракта: get_issue,
    list_comments, и только потом выписанный критерий."""
    d = session(tmp_path, GET_ISSUE, LIST_COMMENTS, WRITE_REFERENCE,
                SAVE_COMMENT, WRITE_VERDICT)
    r = vo.check(d)
    assert r["нарушения"] == ["заявка (#1) раньше эталона (#2)"]


def test_a_session_without_a_reference_file_is_a_violation(tmp_path):
    """Нет файла — нет эталона, чем бы сессия ни отчиталась."""
    d = session(tmp_path, GET_ISSUE, LIST_COMMENTS, SAVE_COMMENT, WRITE_VERDICT)
    assert "эталон не записан" in vo.check(d)["нарушения"]


def test_pr_view_counts_as_reading_the_claim(tmp_path):
    for n, cmd in enumerate(("gh pr view 42 --repo org/repo",
                             "glab mr view 42 --repo org/repo")):
        d = session(tmp_path, GET_ISSUE, tool("Bash", command=cmd), WRITE_REFERENCE,
                    name=f"DEV-{n}-verify-20260915-101010")
        assert vo.check(d)["нарушения"] == ["заявка (#1) раньше эталона (#2)"], cmd


def test_shell_redirect_into_the_reference_file_counts_as_writing_it(tmp_path):
    for n, cmd in enumerate((
            f"cat > {W}/expected.md <<'EOF'\nИсточник критерия: задача\nEOF",
            f"printf '%s\\n' 'Источник критерия: нет' >> \"{W}/expected.md\"",
            f"tee {W}/expected.md <<'EOF'\nx\nEOF")):
        d = session(tmp_path, GET_ISSUE, tool("Bash", command=cmd), LIST_COMMENTS,
                    name=f"DEV-{n}-verify-20260915-101010")
        assert vo.check(d)["нарушения"] == [], cmd


def test_reading_the_reference_file_is_not_writing_it(tmp_path):
    d = session(tmp_path, GET_ISSUE, tool("Bash", command=f"cat {W}/expected.md"),
                LIST_COMMENTS)
    assert vo.check(d)["эталон"] is None


def test_a_file_mentioned_inside_a_heredoc_body_is_not_written(tmp_path):
    """Первая редакция линтера дала два ложных нарушения на живых трассах:
    сессия дописывала заметку памяти, в ТЕЛЕ которой упоминался verdict.txt,
    а команда содержала `>>`. Смотреть надо на цель перенаправления."""
    note = ("cd ~/.claude/memory && cat >> verify-note.md <<'EOF'\n"
            "Раннер читает verdict.txt после save_comment; expected.md — до.\nEOF")
    d = session(tmp_path, GET_ISSUE, tool("Bash", command=note), LIST_COMMENTS,
                SAVE_COMMENT, WRITE_VERDICT)
    r = vo.check(d)
    assert r["эталон"] is None
    assert r["вердикт"] == 4


def test_a_session_that_never_reads_the_claim_is_clean_if_the_reference_exists(tmp_path):
    """Заявку можно и не читать (задачи в трекере не оказалось) — нарушения
    порядка тут нет, лишь бы эталон был."""
    d = session(tmp_path, GET_ISSUE, WRITE_REFERENCE, WRITE_VERDICT)
    assert vo.check(d)["нарушения"] == []


# ── вердикт после комментария: та же машинка, старое правило ────────────────

def test_verdict_after_the_comment_is_clean(tmp_path):
    d = session(tmp_path, WRITE_REFERENCE, SAVE_COMMENT, WRITE_VERDICT)
    assert vo.check(d)["нарушения"] == []


def test_verdict_before_the_comment_is_a_violation(tmp_path):
    d = session(tmp_path, WRITE_REFERENCE, WRITE_VERDICT, SAVE_COMMENT)
    assert vo.check(d)["нарушения"] == ["вердикт (#1) раньше комментария (#2)"]


def test_pr_comment_counts_as_the_comment(tmp_path):
    d = session(tmp_path, WRITE_REFERENCE,
                tool("Bash", command="gh pr comment 42 --body 'OK'"), WRITE_VERDICT)
    assert vo.check(d)["комментарий"] == 1


# ── разбор трассы ────────────────────────────────────────────────────────────

def test_indices_count_tool_calls_not_lines(tmp_path):
    """Между вызовами лежат служебные записи и ответы — номер строки о порядке
    действий не говорит ничего."""
    d = session(tmp_path, '{"type":"user","message":{"content":[]}}', GET_ISSUE,
                '{"type":"system","subtype":"hook_started"}', WRITE_REFERENCE)
    assert vo.check(d)["эталон"] == 1


def test_a_torn_last_line_does_not_break_the_parse(tmp_path):
    d = session(tmp_path, GET_ISSUE, WRITE_REFERENCE)
    with (d / "transcript.jsonl").open("a") as f:
        f.write('{"type":"assistant","message":{"con')
    assert vo.check(d)["эталон"] == 1


def test_root_scan_takes_only_verify_sessions(tmp_path):
    session(tmp_path, GET_ISSUE, name="DEV-1-verify-20260915-101010")
    session(tmp_path, GET_ISSUE, name="DEV-2-20260915-101010")
    (tmp_path / "DEV-3-verify-20260915-101010").mkdir()   # без трассы
    found = vo.find_sessions([tmp_path])
    assert [d.name for d in found] == ["DEV-1-verify-20260915-101010"]


def test_since_filters_by_the_directory_stamp(tmp_path):
    """До выката файла нет ни у одной сессии, и без среза по дате линтер
    честно кричал бы про все старые трассы."""
    session(tmp_path, GET_ISSUE, name="DEV-1-verify-20260913-101010")
    session(tmp_path, GET_ISSUE, name="DEV-2-verify-20260915-101010")
    assert [d.name for d in vo.find_sessions([tmp_path], since="20260914")] == \
        ["DEV-2-verify-20260915-101010"]
    assert [d.name for d in vo.find_sessions([tmp_path], since="2026-09-15")] == \
        ["DEV-2-verify-20260915-101010"]


def test_cli_exit_codes(tmp_path, capsys):
    good = session(tmp_path, WRITE_REFERENCE, LIST_COMMENTS,
                   name="DEV-1-verify-20260915-101010")
    bad = session(tmp_path, LIST_COMMENTS, WRITE_REFERENCE,
                  name="DEV-2-verify-20260915-101011")
    assert vo.main([str(good)]) == 0
    assert vo.main([str(bad)]) == 10
    assert "с нарушениями: 1" in capsys.readouterr().out
    assert vo.main([str(tmp_path / "нет-такого")]) == 2
