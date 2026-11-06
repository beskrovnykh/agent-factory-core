"""Капкан на разнос results.py (DEV-68).

`LinearTracker` уехал в свой модуль, а `label_names` — в `queueing.py`. Пока
оба лежали в одном файле, вызов резолвился сам собой; после разноса он
держится на импорте, и забытый импорт даёт `NameError` ровно в одном методе.

Наружу это не видно: единственный вызывающий — приёмник вебхука
(`webhook.Receiver.labels`) — ловит всё в `except Exception` и отвечает
пустым списком. То есть инцидент, пришедший вебхуком без лейблов в событии,
уехал бы обычной работой, и признаком была бы одна строка в журнале.

Настоящий `LinearTracker` в остальных тестах не вызывается: у фейков свой
`issue_label_names`, а протокол проверяется по сигнатурам. Поэтому проверка
здесь.
"""
import pytest

from agent_factory.tracking import LinearTracker


def tracker_answering(issue: dict) -> LinearTracker:
    t = LinearTracker("ключ")
    t._gql = lambda query, variables, key="": {"issue": issue}
    return t


def test_the_real_tracker_reads_the_labels_of_an_issue():
    t = tracker_answering({"labels": {"nodes": [{"name": "дежурный"},
                                                {"name": "repo:dok-engine"}]}})
    assert t.issue_label_names("ISSUE-1") == ["дежурный", "repo:dok-engine"]


def test_an_issue_without_labels_gives_an_empty_list():
    assert tracker_answering({"labels": {"nodes": []}}).issue_label_names("X") == []


def test_a_tracker_answer_without_the_issue_does_not_blow_up():
    t = LinearTracker("ключ")
    t._gql = lambda query, variables, key="": {}
    assert t.issue_label_names("X") == []


# ── карточка учёта: правка на месте, а не второй комментарий ────────────────

# Метка — первая строка нашей карточки. Ровно та, что отдаёт
# `runs.card_marker`; здесь она записана буквально, чтобы тест падал при
# молчаливой смене формата, а не подстраивался под него.
MARKER = "### 🧾 Учёт: DEV-18"
CARD = MARKER + '\n\n**$10.35** за задачу\n\n```json\n{"учёт": 1}\n```'

# Комментарий ЧЕЛОВЕКА про формат карточки: машиночитаемый блок процитирован
# целиком, а карточкой он не является. Такой комментарий в DEV-18 есть
# по-настоящему — согласованный HLD от 12.09.2026, и старая метка
# («`"учёт": 1` встречается в теле») выбрала бы именно его.
QUOTING_HUMAN = (
    '# HLD: учёт работы фабрики\n\nВнутри — блок:\n\n'
    '```json\n{"учёт": 1, "заходов": 2, "usd": 10.35}\n```\n\n'
    'А выглядит карточка так:\n\n' + MARKER + '\n\nи дальше числа.'
)

ME = "uuid-меня"


def recording_tracker(comments: list[dict], author_key: str = "",
                      me: str = ME):
    """Настоящий `LinearTracker` с подменённой единственной точкой выхода в
    сеть. Иначе метод остаётся непроверенным: у фейков в остальных тестах
    свой `upsert_comment`, и его тело может падать на первой строке."""
    t = LinearTracker("ключ-инсталляции", author_key=author_key)
    seen = []

    def gql(query, variables, key=""):
        seen.append((query, variables, key))
        if "query(" in query:
            return {"viewer": {"id": me},
                    "issue": {"id": "uuid-1", "comments": {"nodes": comments}}}
        if "commentUpdate" in query:
            return {"commentUpdate": {"success": True}}
        return {"commentCreate": {"success": True}}

    t._gql = gql
    return t, seen


def test_the_first_card_is_created():
    t, seen = recording_tracker([])
    assert t.upsert_comment("DEV-18", MARKER, CARD) == "создан"
    assert "commentCreate" in seen[-1][0]
    # id задачи берётся из ответа, а не из того, чем спрашивали: учёт знает
    # только идентификатор, а мутация требует id.
    assert seen[-1][1]["i"] == "uuid-1"


def test_an_existing_card_is_edited_not_duplicated():
    t, seen = recording_tracker([{"id": "c-1", "body": "чужой",
                                  "user": {"id": ME}},
                                 {"id": "c-2", "body": CARD,
                                  "user": {"id": ME}}])
    assert t.upsert_comment("DEV-18", MARKER, "новое тело") == "поправлен"
    assert "commentUpdate" in seen[-1][0]
    assert seen[-1][1] == {"i": "c-2", "b": "новое тело"}


def test_a_comment_that_only_quotes_the_card_is_not_the_card():
    """Главная находка проверки DEV-18: писатель принимал за свою карточку
    ЧУЖОЙ комментарий, где формат карточки приведён примером, — и заменил бы
    согласованный HLD телом учёта. Молча: `Ledger.write` отчитался бы
    «записано», а учёта в задаче не появилось бы."""
    t, seen = recording_tracker([{"id": "c-hld", "body": QUOTING_HUMAN,
                                  "user": {"id": ME}}])
    assert t.upsert_comment("DEV-18", MARKER, CARD) == "создан"
    assert "commentCreate" in seen[-1][0]
    assert all("commentUpdate" not in q for q, _v, _k in seen)


def test_a_card_of_another_task_is_left_alone():
    """Метка несёт номер задачи: без него карточка DEV-7 в обсуждении DEV-18
    выглядела бы своей."""
    t, seen = recording_tracker([{"id": "c-7",
                                  "body": "### 🧾 Учёт: DEV-7\n\nчисла",
                                  "user": {"id": ME}}])
    assert t.upsert_comment("DEV-18", MARKER, CARD) == "создан"


def test_a_card_planted_by_someone_else_is_not_ours():
    """Вторая проверка — автор. Карточку, написанную другим ключом (личностью
    или человеком), не правим: это чужой текст, даже если он выглядит нашим."""
    t, seen = recording_tracker([{"id": "c-1", "body": CARD,
                                  "user": {"id": "uuid-кого-то"}}])
    assert t.upsert_comment("DEV-18", MARKER, CARD) == "создан"


def test_a_card_without_an_author_is_still_ours():
    """У комментария от приложения `user` пустой. Если бы пустой автор читался
    как чужой, писатель заводил бы новую карточку каждый заход."""
    t, seen = recording_tracker([{"id": "c-1", "body": CARD, "user": None}])
    assert t.upsert_comment("DEV-18", MARKER, CARD) == "поправлен"


def test_the_card_goes_by_the_installation_key_not_the_persona():
    """У приложения-личности скоуп `comments:create` без правки: пойди
    карточка её ключом, она обновлялась бы только в первый раз, а дальше молча
    заводила бы вторую."""
    t, seen = recording_tracker([], author_key="ключ-личности")
    t.upsert_comment("DEV-18", MARKER, CARD)
    assert all(key == "" for _q, _v, key in seen)


def test_a_missing_issue_is_an_error_not_a_silent_skip():
    t = LinearTracker("ключ")
    t._gql = lambda query, variables, key="": {"issue": None}
    try:
        t.upsert_comment("DEV-404", MARKER, CARD)
    except RuntimeError as e:
        assert "DEV-404" in str(e)
    else:
        raise AssertionError("пропавшая задача обязана быть видна")


def test_a_refused_update_is_not_reported_as_written():
    t = LinearTracker("ключ")
    t._gql = lambda query, variables, key="": (
        {"viewer": {"id": ME},
         "issue": {"id": "uuid-1", "comments": {"nodes": [
             {"id": "c-1", "body": CARD, "user": {"id": ME}}]}}}
        if "query(" in query else {"commentUpdate": {"success": False}})
    try:
        t.upsert_comment("DEV-18", MARKER, CARD)
    except RuntimeError:
        pass
    else:
        raise AssertionError("отказ трекера не должен читаться как запись")


# ── задача по идентификатору (DEV-86): «нет» и «не ответил» различимы ────────

def test_a_missing_identifier_is_none():
    t = LinearTracker("ключ")

    def gone(query, variables, key=""):
        raise RuntimeError("[{'message': 'Entity not found: Issue', 'path': ['issue']}]")
    t._gql = gone
    assert t.issue_by_identifier("DEV-9999") is None


def test_a_silent_tracker_is_an_error_not_a_missing_task():
    t = LinearTracker("ключ")

    def down(query, variables, key=""):
        raise RuntimeError("HTTP Error 502: Bad Gateway")
    t._gql = down
    try:
        t.issue_by_identifier("DEV-86")
    except RuntimeError:
        pass
    else:
        raise AssertionError("упавший трекер нельзя читать как «задачи нет»")


def test_an_existing_identifier_gives_the_issue_with_its_description():
    t = tracker_answering({"id": "uuid-86", "identifier": "DEV-86", "title": "Ворота",
                           "url": "https://l/DEV-86", "description": "## Критерий приёмки"})
    got = t.issue_by_identifier("DEV-86")
    assert got["id"] == "uuid-86" and "Критерий" in got["description"]


# ── перевод состояния по имени (мерж → Done) ────────────────────────────────

def tracker_with_board(state="In Review", states=("In Progress", "In Review", "Done")):
    t = LinearTracker("ключ")
    calls = []

    def gql(query, variables, key=""):
        calls.append((query, variables))
        if query.startswith("mutation"):
            return {"issueUpdate": {"success": True}}
        return {"issue": {"id": "iss-1", "state": {"id": "st-cur", "name": state, "type": "started"},
                          "team": {"id": "team-1", "name": "Client-b",
                                   "states": {"nodes": [{"id": f"st-{i}", "name": n}
                                                        for i, n in enumerate(states)]}}}}
    t._gql = gql
    return t, calls


def test_the_real_tracker_resolves_the_state_by_name_and_moves():
    t, calls = tracker_with_board()
    assert t.set_state("iss-1", "Done") == {"from": "In Review", "to": "Done", "already": False}
    mutation = [c for c in calls if c[0].startswith("mutation")]
    assert len(mutation) == 1 and mutation[0][1] == {"id": "iss-1", "s": "st-2"}


def test_a_state_already_set_is_not_set_again():
    t, calls = tracker_with_board(state="Done")
    assert t.set_state("iss-1", "Done")["already"] is True
    assert not [c for c in calls if c[0].startswith("mutation")]


def test_a_missing_state_name_names_the_ones_that_exist():
    t, calls = tracker_with_board(states=("In Review",))
    with pytest.raises(RuntimeError) as e:
        t.set_state("iss-1", "Done")
    assert "'Done'" in str(e.value) and "'In Review'" in str(e.value) and "Client-b" in str(e.value)
    assert not [c for c in calls if c[0].startswith("mutation")]


def test_a_missing_issue_is_an_error_not_a_silent_no():
    t = LinearTracker("ключ")
    t._gql = lambda query, variables, key="": {}
    with pytest.raises(RuntimeError):
        t.set_state("iss-x", "Done")


def test_issue_by_identifier_carries_the_state_type():
    # По типу состояния мерж отличает закрытую задачу; имя — запасной признак.
    t = LinearTracker("ключ")
    seen = []
    t._gql = lambda query, variables, key="": seen.append(query) or {"issue": {"id": "i"}}
    t.issue_by_identifier("DEV-86")
    assert "state{name type}" in seen[0]
