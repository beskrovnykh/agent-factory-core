"""Контракт рода `merge` между раннером и приёмом (DEV-109).

Сессию разрешения конфликта ставит приём (`queueing.merge_task`), поднимает
раннер (`runner.run_session`), а не считает кругом журнал запусков
(`runs.WORK_KINDS`). Импортировать друг друга раннер и приём не могут —
держится всё на совпадении слова, и этот файл его закрепляет, как
`test_verify_bridge.py` закрепляет мост вердиктов.

Почему `merge` не в `WORK_KINDS`: задача решена и проверена, ветка перестала
вливаться от ЧУЖОГО мержа. Отнять за это круг по `AGENT_MAX_WORK_ROUNDS`
значит наказать задачу за чужую работу — и на третьем конфликте отдать её
человеку как исчерпавшую попытки.
"""
from datetime import datetime, timezone

import runner
from agent_factory import queueing, runs, sessions


def test_both_sides_call_it_the_same_word():
    assert queueing.MERGE_KIND == runner.MERGE_KIND == "merge"


def test_the_conflict_round_is_not_counted_as_work():
    assert queueing.MERGE_KIND not in runs.WORK_KINDS
    assert queueing.MERGE_KIND not in runs.INTAKE_KINDS
    # Проверкой он тоже не считается: у `last_verify` свой отбор по `verify`.
    assert queueing.MERGE_KIND != "verify"


def test_the_stack_line_names_it_a_conflict_not_a_second_take():
    # В карточке учёта заход должен читаться как «конфликт», а не как
    # «доработка»: иначе человек увидит переделку там, где её не было.
    assert runs.STAGE_WORDS[queueing.MERGE_KIND] == "конфликт"


def session(root, name, kind):
    """Каталог сессии, как его оставляет раннер: транскрипт и статистика."""
    d = root / name
    d.mkdir()
    (d / "transcript.jsonl").write_text(
        '{"type":"result","total_cost_usd":0.5,"num_turns":3,"duration_ms":1000}\n')
    (d / "stats.json").write_text('{"род": "%s", "исход": "READY_FOR_HUMAN"}' % kind)
    return d


def test_the_work_directory_of_a_conflict_session_belongs_to_its_task(tmp_path):
    # Раннер заводит каталог `DEV-38-merge-<штамп>`; журнал обязан отнести
    # его к DEV-38, иначе круги и проверки задачи считаются мимо неё.
    session(tmp_path, "DEV-38-merge-20260916-004500", "merge")
    (got,) = sessions.walk(str(tmp_path))
    assert got["task"] == "DEV-38" and got["род"] == "merge"


def test_a_conflict_directory_without_stats_is_still_not_a_work_round(tmp_path):
    # Раннер прежней версии статистики не писал; род тогда угадывается по
    # имени каталога, и «merge» обязан угадаться, иначе заход посчитается
    # рабочим и отнимет круг.
    d = tmp_path / "DEV-38-merge-20260916-004500"
    d.mkdir()
    (d / "transcript.jsonl").write_text('{"type":"result"}\n')
    (got,) = sessions.walk(str(tmp_path))
    assert got["род"] == "merge"


def test_a_conflict_round_does_not_spend_a_work_round(tmp_path):
    # Тот же счёт, что читает потолок кругов (`deploy.preflight` → `rounds`).
    session(tmp_path, "DEV-38-20260915-120000", "work")
    session(tmp_path, "DEV-38-merge-20260916-004500", "merge")
    assert runs.work_rounds(str(tmp_path), "DEV-38") == 1


def test_a_conflict_task_carries_what_the_contract_needs():
    issue = {"id": "iss-1", "identifier": "DEV-38", "title": "Тяга",
             "url": "https://linear.app/x/DEV-38"}
    task = queueing.merge_task(issue, "PR #23 не вливается",
                               {"number": "23", "url": "https://gh/pr/23",
                                "target_ref": "main"}, "agent/DEV-38")
    assert task["kind"] == "merge" and task["identifier"] == "DEV-38"
    assert task["ref"] == "agent/DEV-38" and task["mr_iid"] == "23"
    assert task["conflict"] == "PR #23 не вливается"


def test_an_incident_label_does_not_hijack_a_conflict_round():
    # У протухшей задачи мог остаться лейбл дежурного с прошлой жизни;
    # уехать она должна разрешением конфликта, а не инцидентом.
    issue = {"id": "iss-1", "identifier": "DEV-38", "title": "Тяга", "url": "u",
             "labels": {"nodes": [{"name": "дежурный"}]}}
    assert queueing.merge_task(issue, "не вливается")["kind"] == "merge"


# ── ожидание проверок: заход остаётся конфликтным и в нём (DEV-109) ─────────
#
# Контракт разрешения конфликта сам велит оставить `ci-wait.json` (шаг 7), то
# есть эта дорога для него не редкая, а обычная. А послабления рода `merge`
# стояли только на дороге «сессия дошла до конца»: через ожидание задача
# получала и спрос за `plan.md`, которого её контракт не просит, и отказ
# «ревью не состоялось» — обе неправды в задаче у человека.

HANDOFF = ("Влил основную ветку и развёл конфликты, работу не переделывал. "
           "Посмотреть можно в PR: ветка снова вливается. От тебя нужен мерж."
           "\n\n---\n\n"
           "## Цель\nразрешить конфликт\n## Формат выхода\nветка\n"
           "## Чем наблюдать\ngh pr view\n## Границы\nничего не отложено\n")

SNAPSHOT = {"state": {"name": "In Progress", "type": "started"},
            "assignee": {"id": "agent"},
            "comments": {"nodes": [{"createdAt": "2099-01-01T00:00:00Z",
                                    "body": "отчёт"}]}}


def waiting(monkeypatch, tmp_path, kind: str) -> dict:
    """Кладёт заход рода `kind` в ожидание проверок. Возвращает запись."""
    stored = {}
    monkeypatch.setattr(runner, "_ci_store",
                        lambda conn, entry, at, now: stored.update(entry))
    monkeypatch.setattr(runner, "_ci_conn", lambda: None)
    monkeypatch.setattr(runner, "linear_comment", lambda *a, **k: None)
    monkeypatch.setattr(runner, "notify", lambda *a, **k: None)
    monkeypatch.setattr(runner, "current_reviewer",
                        lambda: {"id": "rev", "name": "Андрей", "telegram_id": "",
                                 "until": "", "delegated": False})
    workdir = tmp_path / f"DEV-38-{kind}-20260916-004500"
    workdir.mkdir()
    (workdir / "handoff.md").write_text(HANDOFF)
    task = {"identifier": "DEV-38", "issue_id": "iss-1", "kind": kind,
            "url": "https://linear.app/x/DEV-38"}
    req = {"forge": "github", "repo": "o/r", "branch": "agent/DEV-38",
           "sha": "c0ffee"}
    assert runner._ci_enter(task, workdir, workdir / "session.log", "", req,
                            cut_off=False, issue=dict(SNAPSHOT),
                            since=datetime(2026, 9, 16, tzinfo=timezone.utc))
    return stored


