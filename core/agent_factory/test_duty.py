"""Инцидент без исполнителя не имеет права лежать молча.

12.09.2026 TASK-302 («уровни потеряли признак isMax») завели в 12:35 с меткой
дежурного, карточку с кнопкой отправили — и она пролежала 50 минут. Ни в
трекере, ни в чате за это время не было ни одного признака, что инцидент никем
не взят. Половина тестов здесь про то, чтобы это молчание стало невозможным,
вторая — про то, чтобы напоминания не превратились в спам.
"""
import asyncio
import json

from agent_factory.duty import Duty, PENDING_KEY, due, nag_text


def run(coro):
    return asyncio.run(coro)


class Store:
    def __init__(self, boom=False):
        self.kv, self.boom = {}, boom

    async def get(self, key):
        if self.boom:
            raise RuntimeError("redis молчит")
        return self.kv.get(key)

    async def put(self, key, value, ttl):
        if self.boom:
            raise RuntimeError("redis молчит")
        self.kv[key] = value


class Transport:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, chat_id, text, buttons=None):
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons})
        return "mid-1" if self.ok else ""


class Tracker:
    def __init__(self, assignee="", boom=False):
        self.assignee, self.boom, self.asked = assignee, boom, []

    def issue_assignee_id(self, issue_id):
        self.asked.append(issue_id)
        if self.boom:
            raise RuntimeError("трекер молчит")
        return self.assignee


ISSUE = {"id": "i-302", "identifier": "TASK-302",
         "url": "https://linear.app/x/issue/TASK-302"}


def mk(assignee="", to_chat="", ok=True, **kw):
    st, tp, tr = Store(), Transport(ok), Tracker(assignee)
    keys = lambda issue_id: [("Отдать агенту", f"issue:assign:{issue_id}")]
    return st, tp, tr, Duty(st, tp, tr, keyboard=keys, to_chat=to_chat,
                            after=300, every=600, times=6, **kw)


# ── когда пора напоминать ────────────────────────────────────────────────

def test_a_freshly_created_incident_is_not_nagged_at_once():
    # Взятый за пять минут инцидент не должен получать окриков вовсе.
    assert due({"since": 1000, "last": 1000, "sent": 0}, 1000 + 299) is False


def test_after_the_first_gap_it_speaks():
    assert due({"since": 1000, "last": 1000, "sent": 0}, 1000 + 300) is True


def test_the_second_reminder_waits_the_longer_gap():
    e = {"since": 1000, "last": 1300, "sent": 1}
    assert due(e, 1300 + 599) is False
    assert due(e, 1300 + 600) is True


def test_the_gap_counts_from_the_last_reminder_not_from_creation():
    """Иначе после перезапуска приёма все пропущенные шаги схлопнулись бы в
    очередь сообщений подряд — и человек получил бы шесть окриков разом."""
    e = {"since": 1000, "last": 9000, "sent": 3}
    assert due(e, 9000 + 599) is False


def test_it_falls_silent_after_the_cap():
    assert due({"since": 1, "last": 1, "sent": 6}, 10 ** 9) is False


# ── что именно говорит ───────────────────────────────────────────────────

def test_the_text_says_how_long_it_has_been_lying():
    text = nag_text({"identifier": "TASK-302", "title": "уровни потеряли isMax",
                     "since": 0, "sent": 2}, 3000)
    assert "TASK-302" in text and "50 мин" in text
    assert "уровни потеряли isMax" in text


def test_the_last_reminder_says_it_is_the_last():
    e = {"identifier": "TASK-302", "since": 0, "sent": 5}
    assert "Больше не напомню" in nag_text(e, 100, times=6)
    assert "Больше не напомню" not in nag_text(dict(e, sent=4), 100, times=6)


# ── обход ────────────────────────────────────────────────────────────────

def test_an_incident_without_an_assignee_gets_a_reminder_with_the_button():
    st, tp, tr, duty = mk()
    run(duty.remember(ISSUE, "chat-1", "уровни потеряли isMax"))
    assert run(duty.once(now=_since(st) + 310)) == 1
    assert tp.sent[0]["chat"] == "chat-1"
    # Кнопка обязательна: напоминание должно чиниться одним нажатием, а не
    # походом в трекер.
    assert tp.sent[0]["buttons"] == [("Отдать агенту", "issue:assign:i-302")]


def test_a_taken_incident_is_dropped_without_a_word():
    st, tp, tr, duty = mk(assignee="кто-то")
    run(duty.remember(ISSUE, "chat-1"))
    assert run(duty.once(now=_since(st) + 3000)) == 0
    assert tp.sent == []
    assert json.loads(st.kv[PENDING_KEY]) == []


def test_a_silent_tracker_does_not_silence_the_reminder():
    # Лишнее напоминание переживём, пропущенный инцидент — нет.
    st, tp, tr, duty = mk()
    tr.boom = True
    run(duty.remember(ISSUE, "chat-1"))
    assert run(duty.once(now=_since(st) + 310)) == 1


def test_the_common_chat_wins_over_the_card_chat():
    # У первого клиента пачка приходит в MAX, а общий чат — телеграм-группа.
    st, tp, tr, duty = mk(to_chat="-100500")
    run(duty.remember(ISSUE, "chat-1"))
    run(duty.once(now=_since(st) + 310))
    assert tp.sent[0]["chat"] == "-100500"


def test_a_reminder_that_did_not_go_is_not_counted_as_sent():
    """Иначе шесть неудачных отправок молча исчерпали бы лимит, и инцидент
    снова остался бы без единого слова."""
    st, tp, tr, duty = mk(ok=False)
    run(duty.remember(ISSUE, "chat-1"))
    run(duty.once(now=_since(st) + 310))
    assert json.loads(st.kv[PENDING_KEY])[0]["sent"] == 0


def test_the_cap_removes_the_incident_from_the_watch_list():
    st, tp, tr, duty = mk()
    run(duty.remember(ISSUE, "chat-1"))
    now = _since(st)
    for step in range(6):
        now += 3000
        run(duty.once(now=now))
    assert len(tp.sent) == 6
    assert json.loads(st.kv[PENDING_KEY]) == []


def test_two_incidents_are_watched_apart():
    st, tp, tr, duty = mk()
    run(duty.remember(ISSUE, "chat-1"))
    run(duty.remember({"id": "i-303", "identifier": "TASK-303"}, "chat-2"))
    assert run(duty.once(now=_since(st) + 310)) == 2
    assert {s["chat"] for s in tp.sent} == {"chat-1", "chat-2"}


def test_remembering_the_same_incident_twice_does_not_double_it():
    st, tp, tr, duty = mk()
    run(duty.remember(ISSUE, "chat-1"))
    run(duty.remember(ISSUE, "chat-1"))
    assert len(json.loads(st.kv[PENDING_KEY])) == 1


def test_an_incident_taken_by_the_button_is_forgotten():
    st, tp, tr, duty = mk()
    run(duty.remember(ISSUE, "chat-1"))
    later = _since(st) + 3000
    run(duty.forget("i-302"))
    assert run(duty.once(now=later)) == 0


def test_an_issue_without_an_id_is_not_watched_silently(caplog):
    import logging
    st, tp, tr, duty = mk()
    with caplog.at_level(logging.ERROR, logger="intake"):
        run(duty.remember({"identifier": "TASK-0"}, "chat-1"))
    assert "не взять" in caplog.text
    assert st.kv.get(PENDING_KEY) in (None, "[]")


def test_a_dead_store_says_so_instead_of_pretending(caplog):
    import logging
    st, tp, tr, duty = mk()
    st.boom = True
    with caplog.at_level(logging.ERROR, logger="intake"):
        run(duty.remember(ISSUE, "chat-1"))
    assert "напоминаний" in caplog.text


def _since(store) -> float:
    return json.loads(store.kv[PENDING_KEY])[0]["since"]
