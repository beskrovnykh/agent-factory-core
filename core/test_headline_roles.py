"""Шапка есть у КАЖДОЙ роли, которой фабрика пишет в задачу (DEV-108).

Первые заходы этой задачи выдали шапку трём ролям, потом четырём — и каждый
раз казалось, что это все. Роль, до которой шапка не доехала, ничем себя не
выдаёт: её комментарии так же зелены в наборе и так же нечитаемы человеку.
Дежурный простоял без шапки целый круг ровно поэтому.

Поэтому список ролей здесь не свой, а живой — `personas.PERSONAS`, та самая
карта, по которой раннер выбирает, чьим именем подписать комментарий. Новая
личность без шапки роняет `test_every_persona_has_a_headline_probe` ещё до
того, как напишет первое слово человеку.

Проверка у каждой роли СВОЯ, и это не небрежность: сдают они разное. Работа,
проверка и дежурный получают схему шапки внутрь контракта — значит смотрим
собранный контракт. Ревью и приём пишут текст сами — значит смотрим текст и
то, что с ним делает код.
"""
import asyncio
from pathlib import Path

import headline
import personas
import runner
from agent_factory import queueing
from prompt import (build_incident_prompt, build_merge_prompt, build_prompt,
                    build_verify_prompt)

TASK = {"issue_id": "i1", "identifier": "DEV-1", "title": "Заголовок",
        "url": "https://linear.app/x/issue/DEV-1"}
VERIFY_TASK = {**TASK, "kind": "verify", "branch": "agent/DEV-1"}
INCIDENT_TASK = {**TASK, "kind": "incident", "symptom": "прод не отвечает",
                 "source": "алерт"}


def _schema() -> str:
    """Схема шапки ровно в том виде, в каком её вкладывают в контракты."""
    return headline.schema_block()


# ── по личности на проверку ──────────────────────────────────────────────────

def _engineer() -> None:
    """Работа: схема лежит в контракте целиком, а не пересказом.

    Контрактов у инженера два — обычный и разрешение конфликта (DEV-109), и
    сдачу раннер линтует у обоих одинаково. Контракт без схемы это сессия,
    которую вернут за то, чего у неё не просили.
    """
    text = build_prompt(TASK, Path("/tmp/w"), Path("/repos"), "Андрей")
    assert _schema() in text
    merge = build_merge_prompt({**TASK, "kind": "merge", "ref": "agent/DEV-1"},
                               Path("/tmp/w"), Path("/repos"))
    assert _schema() in merge


def _qa() -> None:
    """Проверка: та же схема, одна на все исходы (`headline.verdict_block`)."""
    text = build_verify_prompt(VERIFY_TASK, Path("/tmp/w"), Path("/repos"))
    assert _schema() in text


def _duty() -> None:
    """Дежурный: схема в шаге 8, и раньше подробностей, а не после них."""
    text = build_incident_prompt(INCIDENT_TASK, reviewer="Андрей")
    assert _schema() in text
    # Порядок — половина смысла: шапка стоит РАНЬШЕ подробностей, а не после.
    # Якорь однострочный нарочно: контракт переносится по ширине, и проверка,
    # завязанная на место переноса, ломалась бы от переформатирования.
    assert text.index(_schema()) < text.index("числа до/после")


def _reviewer() -> None:
    """Ревью: вернуть сессию некуда, поэтому пропуск называется вслух."""
    with_head = ("Прочитал свой дифф целиком: четыре находки, все починены "
                 "вторым коммитом. Разбор по строке на находку — ниже черты. "
                 "Спорного не осталось, разбирать тебе нечего.\n\n---\n\n"
                 "1. Лишний импорт — убрал.\n")
    assert runner._review_headline(with_head, "DEV-1") == with_head
    without = "1. Лишний импорт — убрал.\n2. Опечатка в докстроке — поправил.\n"
    marked = runner._review_headline(without, "DEV-1")
    assert marked != without and marked.endswith(without)
    assert marked.startswith(">")


def _intake() -> None:
    """Приём: комментарий он пишет сам, поэтому линтуется сам текст."""

    class Tracker:
        def __init__(self):
            self.bodies: list[str] = []

        def assign(self, issue_id, user_id):
            pass

        def comment(self, issue_id, body):
            self.bodies.append(body)

    tr = Tracker()
    asyncio.run(queueing.hand_over(tr, "i1", "DEV-1", 3, "rev-1"))
    (body,) = tr.bodies
    assert headline.lint_text(body, "комментарий приёма") == []


PROBES = {"engineer": _engineer, "qa": _qa, "duty": _duty,
          "reviewer": _reviewer, "intake": _intake}


def test_every_persona_has_a_headline_probe():
    """Личность без проверки шапки — то же, что личность без шапки: никто не
    заметит. Список берётся у `personas`, а не переписывается сюда."""
    assert set(PROBES) == set(personas.PERSONAS), (
        f"проверены {sorted(PROBES)}, а личностей {sorted(personas.PERSONAS)}")



SUMMARY = ("Падал вход в приложение — у сертификата кончился срок. Выпустил "
           "новый, вход работает: `curl -sI https://api/health` отвечает 200. "
           "От тебя ничего не нужно, на причину завёл отдельную задачу.\n\n"
           "---\n\nПодробно: 14:02 алерт, 14:09 перевыпуск, ошибок 0.\n")

STEP_SIX = "Перезапускаю `doc-engine`: в журнале `EADDRINUSE`, порт держит старый процесс."

STARTED = "Взялся, дежурю по падению входа. Вернусь с диагнозом."


def _c(at: str, body: str) -> dict:
    return {"id": at, "createdAt": at, "body": body, "author": "Duty"}


def _since(raw: str):
    from datetime import datetime
    return datetime.fromisoformat(raw)


def test_a_duty_summary_with_a_headline_passes():
    comments = [_c("2026-09-16T10:00:00.412Z", STARTED),
                _c("2026-09-16T10:05:00.907Z", STEP_SIX),
                _c("2026-09-16T10:09:00.031Z", SUMMARY)]
    assert runner.incident_headline_misses(comments, _since("2026-09-16T09:59:00+00:00")) == []


def test_a_duty_summary_without_a_headline_is_named():
    """Сводка открывается разбором — это ровно то, из-за чего задача заведена."""
    bare = "## Что случилось\n\n14:02 алерт, 14:09 перевыпуск, ошибок 0.\n"
    comments = [_c("2026-09-16T10:00:00.412Z", STARTED),
                _c("2026-09-16T10:09:00.031Z", bare)]
    misses = runner.incident_headline_misses(comments, _since("2026-09-16T09:59:00+00:00"))
    assert misses and all("сводке дежурного" in m for m in misses)


def test_the_running_commentary_of_step_six_is_not_asked_for_a_headline():
    """Шаг 6 велит писать по комментарию на каждое действие, и шапки у них
    нет по замыслу. Требовать её с каждого — ругать сессию за послушание."""
    comments = [_c("2026-09-16T10:00:00.412Z", STARTED),
                _c("2026-09-16T10:04:00.113Z", STEP_SIX),
                _c("2026-09-16T10:06:00.220Z", SUMMARY),
                _c("2026-09-16T10:07:00.550Z", STEP_SIX)]
    assert runner.incident_headline_misses(comments, _since("2026-09-16T09:59:00+00:00")) == []


def test_comments_older_than_the_session_are_not_its_summary():
    """Чужая шапка из прошлого захода не засчитывается этому."""
    comments = [_c("2026-09-16T08:00:00.777Z", SUMMARY),
                _c("2026-09-16T10:09:00.031Z", "## Что случилось\n\nошибок 0.\n")]
    assert runner.incident_headline_misses(comments, _since("2026-09-16T09:59:00+00:00")) != []


def test_a_silent_duty_session_is_not_scolded_for_the_headline():
    """Ни одного комментария — это молчание, и его называет `ensure_incident_handoff`
    своими словами. Второй промах на то же действие только запутал бы."""
    assert runner.incident_headline_misses([], _since("2026-09-16T09:59:00+00:00")) == []


def test_the_note_lands_in_the_task_when_the_headline_is_missing(monkeypatch):
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_tracker", _FakeTracker(
        [_c("2026-09-16T10:09:00.031Z", "## Что случилось\n\nошибок 0.\n")]))
    monkeypatch.setattr(runner, "linear_comment",
                        lambda issue_id, body, persona="": posted.append((issue_id, body)))
    assert runner.note_incident_headline("i1", "DEV-1", _since("2026-09-16T09:59:00+00:00"))
    ((issue_id, body),) = posted
    assert issue_id == "i1" and "У сводки дежурного нет шапки" in body


def test_nothing_is_said_when_the_headline_is_there(monkeypatch):
    posted = []
    monkeypatch.setattr(runner, "_tracker",
                        _FakeTracker([_c("2026-09-16T10:09:00.031Z", SUMMARY)]))
    monkeypatch.setattr(runner, "linear_comment",
                        lambda *a, **k: posted.append(a))
    assert not runner.note_incident_headline("i1", "DEV-1", _since("2026-09-16T09:59:00+00:00"))
    assert posted == []


def test_a_tracker_that_will_not_answer_does_not_take_the_incident_down(monkeypatch):
    """Подача не роняет сдачу: инцидент уже отработан."""
    class Broken:
        def list_comments(self, issue_id):
            raise RuntimeError("Linear молчит")

    monkeypatch.setattr(runner, "_tracker", Broken())
    assert not runner.note_incident_headline("i1", "DEV-1", _since("2026-09-16T09:59:00+00:00"))


class _FakeTracker:
    def __init__(self, comments):
        self._comments = comments

    def list_comments(self, issue_id, limit=50):
        return list(self._comments)


def test_the_check_runs_on_the_duty_path_even_when_the_task_was_handed_over(monkeypatch):
    """Механизм, который никто не зовёт, — ровно то, чем дежурный и стоял без
    шапки. Поэтому проверяется не функция, а дежурный ПУТЬ раннера, и на
    сессии, которая всё передала правильно: спрячь вызов за развилкой о
    передаче — и шапка проверялась бы только у молчаливых."""
    posted: list[str] = []
    monkeypatch.setattr(runner, "_handoff_state",
                        lambda issue_id: {"assignee": {"id": "rev-1"}})
    monkeypatch.setattr(runner, "_tracker", _FakeTracker(
        [_c("2026-09-16T10:09:00.031Z", "## Что случилось\n\nошибок 0.\n")]))
    monkeypatch.setattr(runner, "linear_comment",
                        lambda issue_id, body, persona="": posted.append(body))
    runner.ensure_incident_handoff("i1", "DEV-1", Path("/tmp/none.log"),
                                   {"id": "rev-1"}, _since("2026-09-16T09:59:00+00:00"))
    assert posted and "У сводки дежурного нет шапки" in posted[0]


def test_the_note_about_a_missing_headline_has_one_itself(monkeypatch):
    """Замечание раннера — тоже машинный комментарий человеку. Написать его
    без шапки значило бы ровно то, за что оно ругает: обе двери, через
    которые раннер говорит о пропущенной шапке, проходят её же линт."""
    posted: list[str] = []
    monkeypatch.setattr(runner, "_tracker",
                        _FakeTracker([_c("2026-09-16T10:09:00.031Z",
                                         "## Что случилось\n\nошибок 0.\n")]))
    monkeypatch.setattr(runner, "linear_comment",
                        lambda issue_id, body, persona="": posted.append(body))
    runner.note_incident_headline("i1", "DEV-1", _since("2026-09-16T09:59:00+00:00"))
    (note,) = posted
    assert headline.lint_text(note, "замечание о шапке") == []

    marked = runner._review_headline("1. Лишний импорт — убрал.\n", "DEV-1")
    assert headline.lint_text(marked.split("\n\n", 1)[0], "замечание о шапке") == []


def test_the_words_of_the_miss_come_from_the_summary_not_from_the_first_trace():
    """Linear отдаёт комментарии НОВЫМИ ВПЕРЁД (`list_comments`, `first:N`), а
    промах называется по сводке — последней по времени, а не первой в списке.
    Без сортировки сессия получила бы слова про комментарий шага 6, которому
    шапка и не положена, и чинила бы не то.
    """
    blank = "**Что сделано** — перевыпустил сертификат\n**Итог** — прошло\n"
    newest_first = [_c("2026-09-16T10:09:00.031Z", blank),
                    _c("2026-09-16T10:04:00.113Z", STEP_SIX)]
    misses = runner.incident_headline_misses(newest_first, _since("2026-09-16T09:59:00+00:00"))
    assert any("бланком" in m for m in misses), misses
