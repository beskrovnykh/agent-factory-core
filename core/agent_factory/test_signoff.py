"""Подпись работы из чата: «Слить» и «Отменить» под сообщением задачи.

Каждый тест здесь — строка критерия приёмки DEV-123 или грабля, названная в
постановке: кнопка стоит только там, где применима; повторное нажатие не
врёт «смержено сейчас»; осечка трекера видна в чате, а не только в журнале;
«часики» гасятся до походов в сеть.
"""
import asyncio

from agent_factory.deploy import Verification
from agent_factory.issue_feed import IssueFeed
from agent_factory.people import People
from agent_factory.signoff import (CANCEL, MERGE, SignOff, keyboard,
                                   who_pressed)

AGENT = "u-agent"
ANDREY = "u-andrey"
CHAT = "1000000002"
Q = "agent:queue:x"


def run(coro):
    return asyncio.run(coro)


# ── подставные двери наружу ──────────────────────────────────────────────

class Markup:
    def safe(self, t): return (t or "").replace("*", "")
    def bold(self, t): return f"*{t}*"
    def link(self, t, u): return f"[{t}]({u})"
    def mention(self, chat, name): return f"@{name}<{chat}>"


class Transport:
    def __init__(self):
        self.markup = Markup()
        self.sent = []
        self.edits = []
        self.next_mid = 100

    async def send(self, chat_id, text, buttons=None, rich=False):
        self.next_mid += 1
        self.sent.append({"chat": str(chat_id), "text": text,
                          "buttons": [b.text for b in (buttons or [])]})
        return str(self.next_mid)

    async def edit(self, chat_id, message_id, text, buttons=None, rich=False):
        self.edits.append({"chat": str(chat_id), "mid": str(message_id),
                           "text": text,
                           "buttons": [b.text for b in (buttons or [])]})
        return True

    def said(self) -> str:
        return "\n".join(m["text"] for m in self.sent)


class Hosting:
    """Хостинг площадки: открытый PR, мерж, смерженный. Считает походы."""

    def __init__(self, open_pr=None, merged=None, boom="", merge_boom=""):
        self.open_pr = open_pr
        self.merged = merged
        self.boom = boom
        self.merge_boom = merge_boom
        self.asked = 0
        self.merges = []

    def open_request(self, ident):
        self.asked += 1
        if self.boom:
            raise RuntimeError(self.boom)
        return self.open_pr

    def merge(self, req):
        self.merges.append(req)
        if self.merge_boom:
            raise RuntimeError(self.merge_boom)
        # Смержили — открытого больше нет, зато есть смерженный. Ровно то
        # состояние, которое видит второе нажатие.
        self.open_pr = None
        self.merged = {"mr_iid": req["number"], "project_path": req["repo"],
                       "ref": req["target_ref"], "source_ref": req["source_ref"],
                       "url": req.get("url") or "", "merged_by": "кнопка",
                       "merged_at": "2026-09-16T21:00:00Z"}
        return f"смержен PR #{req['number']} в {req['repo']}, ветка удалена"

    def merged_request(self, ident):
        return self.merged


class Board:
    """Трекер: задача по идентификатору, перевод состояния, комментарий."""

    def __init__(self, issues=None, boom=False, move_boom=False):
        self.issues = dict(issues or {})
        self.moved = []
        self.comments = []
        self.boom = boom
        # Отдельно от `boom`: трекер, который ОТДАЁТ задачу, но не даёт
        # сменить статус. Иначе осечка перевода неотличима от «задачу не
        # найти», и тест про неё проходит вхолостую по второй причине.
        self.move_boom = move_boom

    def issue_by_identifier(self, ident):
        if self.boom:
            raise RuntimeError("Linear 502")
        return self.issues.get(ident)

    def set_state(self, issue_id, name):
        if self.boom or self.move_boom:
            raise RuntimeError("Linear 502")
        issue = next(i for i in self.issues.values() if i["id"] == issue_id)
        was = issue["state"]["name"]
        issue["state"] = {"name": name, "type": "completed"}
        self.moved.append((issue_id, name))
        return {"from": was, "to": name}

    def comment(self, issue_id, body):
        self.comments.append((issue_id, body))


class Store:
    def __init__(self):
        self.kv = {}
        self.lists = {}
        self.claimed = set()

    async def put(self, key, value, ttl): self.kv[key] = value
    async def get(self, key): return self.kv.get(key)
    async def drop(self, key): self.kv.pop(key, None); self.claimed.discard(key)
    async def append(self, key, value): self.lists.setdefault(key, []).append(value)
    async def read_all(self, key): return list(self.lists.get(key) or [])
    async def expire(self, key, ttl): pass

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


PR = {"number": "70", "repo": "org/agent-factory",
      "url": "https://gh/org/agent-factory/pull/70",
      "source_ref": "agent/DEV-123", "target_ref": "main",
      "title": "Кнопки в боте", "mergeable": True}


def in_review(ident="DEV-123", issue_id="iss-123"):
    return {ident: {"id": issue_id, "identifier": ident, "title": "Кнопки",
                    "url": f"https://linear.app/x/{ident}",
                    "state": {"name": "In Review", "type": "started"}}}


def mk(open_pr=PR, merged=None, boom="", merge_boom="", tracker=True,
       may_act=None, issues=None, board_boom=False):
    """Подпись из чата с настоящей `Verification` за спиной: перевод задачи
    считается по-настоящему, подставлены только хостинг, трекер и чат."""
    st, tp = Store(), Transport()
    host = Hosting(open_pr=open_pr, merged=merged, boom=boom, merge_boom=merge_boom)
    board = Board(in_review() if issues is None else issues,
                  boom=board_boom) if tracker else None
    v = Verification(st, Q, None, tracker=board)
    return st, tp, host, board, SignOff(hosting=host, verification=v, transport=tp,
                                        store=st, may_act=may_act)


def handoff(ident="DEV-123", issue="iss-123"):
    """Событие сдачи: работа ушла ОТ агента к человеку."""
    return {"type": "Issue", "action": "update", "webhookTimestamp": "s1",
            "updatedFrom": {"assigneeId": AGENT},
            "data": {"id": issue, "identifier": ident, "title": "Кнопки",
                     "url": f"https://linear.app/x/{ident}", "assigneeId": ANDREY,
                     "assignee": {"id": ANDREY, "name": "Андрей"},
                     "state": {"name": "In Review", "type": "started"}}}


# ── 1. под сдачей обе кнопки ─────────────────────────────────────────────

def test_a_handoff_with_an_open_pull_request_gets_both_buttons():
    st, tp, host, board, so = mk()
    text, buttons = run(so.on_handoff(handoff(), "📋 DEV-123 → Андрей"))
    assert [b.text for b in buttons] == ["✅ Слить", "🚫 Отменить"]
    assert [b.payload for b in buttons] == ["sign:merge:DEV-123",
                                            "sign:cancel:DEV-123"]
    # Человек видит, ЧТО сольётся: номер PR, репозиторий и куда.
    assert "PR #70" in text and "org/agent-factory" in text
    assert "agent/DEV-123 → main" in text


def test_the_whole_keyboard_travels_to_the_chat_message():
    """Кнопки вешает лента, а не подпись: пока они не доехали до
    `transport.send`, в чате их нет."""
    st, tp = Store(), Transport()
    host = Hosting(open_pr=PR)
    so = SignOff(hosting=host, verification=None, transport=tp, store=st)
    feed = IssueFeed(st, tp, Board(in_review()),
                     People({ANDREY: CHAT}, owner_user_id=ANDREY, store=st),
                     agent_user_id=AGENT, handoff=so)
    run(feed.show(handoff()))
    assert tp.sent[-1]["buttons"] == ["✅ Слить", "🚫 Отменить"]
    assert tp.sent[-1]["chat"] == CHAT


def test_the_pull_request_line_survives_underscores_in_names():
    """Разметка проверяется НАСТОЯЩЕЙ: имя репозитория и ветки приходят от
    хостинга, и `_` в них открывает курсив в legacy-Markdown. Непарный
    символ — 400 «can\'t parse entities», то есть сообщение о сдаче вместе с
    кнопками не доедет вовсе."""
    from agent_factory.telegram import TelegramMarkup

    st, tp = Store(), Transport()
    tp.markup = TelegramMarkup()
    host = Hosting(open_pr={**PR, "repo": "org/dok_engine",
                            "source_ref": "agent/DEV-123_fix"})
    so = SignOff(hosting=host, verification=None, transport=tp, store=st)
    text, buttons = run(so.on_handoff(handoff(), "📋 DEV-123"))
    line = text.splitlines()[-1]
    assert "_" not in line and "PR #70" in line and "dok" in line
    assert [b.text for b in buttons] == ["✅ Слить", "🚫 Отменить"]


def test_the_second_press_names_the_branch_it_calls_merged():
    """У задачи бывают две ветки (`agent/DEV-66` и `-v2`), и хостинг,
    не перечисливший открытые PR какого-то репозитория, отвечает пустым
    списком, а не осечкой. Тогда сюда попадёт старый мерж — и человек
    обязан видеть, ЧТО считается смерженным."""
    st, tp, host, board, so = mk(open_pr=None, merged={
        "mr_iid": "3", "project_path": "org/agent-factory", "ref": "main",
        "source_ref": "agent/DEV-123", "url": "", "merged_at": "2026-09-14T10:00:00Z"})
    run(so.on_click(MERGE + "DEV-123", CHAT, "101"))
    assert "ветка agent/DEV-123" in tp.said() and "2026-09-14" in tp.said()


# ── 2. нет PR — нет кнопки «Слить» ───────────────────────────────────────

def test_a_task_without_an_open_pull_request_has_no_merge_button():
    st, tp, host, board, so = mk(open_pr=None)
    text, buttons = run(so.on_handoff(handoff(), "📋 DEV-123 → Андрей"))
    # «Отменить» остаётся: она про задачу, а не про PR.
    assert [b.text for b in buttons] == ["🚫 Отменить"]
    assert "PR #" not in text


def test_a_silent_hosting_is_not_an_absent_pull_request():
    """Молчащий хостинг прячет действие, которое на самом деле есть.
    Поэтому кнопку показываем, а строку про PR не сочиняем — нажатие
    спросит заново и скажет прямо, что случилось."""
    st, tp, host, board, so = mk(boom="gh не ответил")
    text, buttons = run(so.on_handoff(handoff(), "📋 DEV-123"))
    assert [b.text for b in buttons] == ["✅ Слить", "🚫 Отменить"]
    assert text == "📋 DEV-123"


def test_without_a_hosting_there_is_nothing_to_merge_with():
    so = SignOff(hosting=None, verification=None, transport=Transport())
    _, buttons = run(so.on_handoff(handoff(), "📋 DEV-123"))
    assert [b.text for b in buttons] == ["🚫 Отменить"]


def test_an_event_without_an_identifier_gets_no_buttons():
    st, tp, host, board, so = mk()
    payload = handoff()
    payload["data"]["identifier"] = ""
    assert run(so.on_handoff(payload, "текст")) == ("текст", [])
    assert host.asked == 0


def test_the_keyboard_needs_an_identifier():
    assert keyboard("", True) == []
    assert [b.payload for b in keyboard("DEV-9", False)] == ["sign:cancel:DEV-9"]


# ── 3. «Слить» мержит и закрывает задачу ─────────────────────────────────

def test_the_merge_button_merges_that_pull_request_and_closes_the_task():
    st, tp, host, board, so = mk()
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101", "Андрей", CHAT)) == "merged"
    # В хостинг ушёл ровно тот PR, что показан на кнопке.
    assert [(r["repo"], r["number"]) for r in host.merges] == [("org/agent-factory", "70")]
    assert board.moved == [("iss-123", "Done")]
    said = tp.said()
    assert "смержен PR #70" in said and "Задача переведена в Done." in said
    body = board.comments[0][1]
    assert "Закрыто по мержу" in body and "`Done`" in body


def test_the_merge_comment_names_who_pressed():
    """Имя И идентификатор: по имени человек узнаёт себя, по идентификатору
    нажатие сходится с журналом приёма, где имён нет."""
    st, tp, host, board, so = mk()
    run(so.on_click(MERGE + "DEV-123", CHAT, "101", "Андрей", CHAT))
    body = board.comments[0][1]
    assert "Андрей" in body and CHAT in body
    assert who_pressed("Андрей", 1000000002) == "Андрей (1000000002)"
    assert who_pressed("", 42) == "42" and who_pressed("Андрей") == "Андрей"
    assert who_pressed() == ""


def test_a_button_of_an_unknown_task_goes_nowhere():
    st, tp, host, board, so = mk()
    assert run(so.on_click("sign:merge:не-задача", CHAT, "101")) == "bad-ident"
    assert host.asked == 0 and host.merges == []


def test_a_failed_merge_is_said_in_the_chat_and_the_task_stays_open():
    st, tp, host, board, so = mk(merge_boom="Pull request is not mergeable")
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merge-failed"
    assert "не прошёл" in tp.said() and "not mergeable" in tp.said()
    assert board.moved == []


def test_a_silent_hosting_on_click_does_not_merge_anything():
    st, tp, host, board, so = mk(boom="gh: таймаут")
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "hosting-failed"
    assert host.merges == [] and board.moved == []
    assert "не ответил" in tp.said() and "Нажми ещё раз" in tp.said()


def test_with_nothing_to_merge_the_answer_names_what_to_do_instead():
    st, tp, host, board, so = mk(open_pr=None, merged=None)
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "nothing-to-merge"
    assert host.merges == [] and board.moved == []
    assert "открытого PR нет" in tp.said() and "Отменить" in tp.said()


# ── 4. повторное нажатие не врёт ─────────────────────────────────────────

def test_a_second_press_says_it_was_already_merged():
    st, tp, host, board, so = mk()
    run(so.on_click(MERGE + "DEV-123", CHAT, "101", "Андрей", CHAT))
    tp.sent.clear()
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101", "Андрей", CHAT)) == "already-merged"
    # Второго мержа нет, и «смержено сейчас» не сказано.
    assert len(host.merges) == 1
    said = tp.said()
    assert "уже был смержен" in said and "второй раз не мержу" in said
    # Именно «не врёт»: слов первого нажатия («смержен … ветка удалена»)
    # во втором ответе нет.
    assert "ветка удалена" not in said
    assert "Задача уже была закрыта." in said


def test_a_second_press_finishes_a_merge_whose_tracker_step_failed():
    """Первое нажатие смержило, а перевод не прошёл — задача висит в ревью.
    Второе нажатие обязано довести дело до конца, а не просто отчитаться."""
    st, tp, host, board, so = mk(open_pr=None, merged={
        "mr_iid": "70", "project_path": "org/agent-factory", "ref": "main",
        "source_ref": "agent/DEV-123", "url": "https://gh/pr/70",
        "merged_at": "2026-09-16T20:00:00Z"})
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101", "Андрей", CHAT)) == "already-merged"
    assert host.merges == [] and board.moved == [("iss-123", "Done")]
    assert "уже был смержен" in tp.said() and "Задача переведена в Done." in tp.said()


def test_two_fast_presses_do_not_merge_twice():
    st, tp, host, board, so = mk()
    st.claimed.add("sign:busy:merge:DEV-123")      # первое нажатие ещё в `gh`
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "busy"
    assert host.merges == [] and board.moved == []
    assert "уже работаю" in tp.said()


def test_the_lock_is_released_after_a_failure():
    """Захват — защёлка на время похода, а не память о сделанном: после
    осечки повтор должен работать сразу, а не через минуту."""
    st, tp, host, board, so = mk(merge_boom="конфликт")
    run(so.on_click(MERGE + "DEV-123", CHAT, "101"))
    assert "sign:busy:merge:DEV-123" not in st.claimed
    host.merge_boom = ""
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merged"


# ── 5. осечка трекера видна в чате ───────────────────────────────────────

def test_a_tracker_failure_after_a_merge_is_visible_in_the_chat():
    st, tp, host, board, so = mk(board_boom=True)
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merged"
    said = tp.said()
    assert "смержен PR #70" in said          # мерж прошёл — это первая новость
    assert "Трекер не ответил" in said and "переведи задачу руками" in said


def test_without_a_tracker_the_chat_says_who_moves_the_task():
    st, tp = Store(), Transport()
    host = Hosting(open_pr=PR)
    so = SignOff(hosting=host, verification=Verification(st, Q, None),
                 transport=tp, store=st)
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merged"
    assert "переведи задачу руками" in tp.said()


def test_a_merge_with_no_verification_at_all_still_says_so():
    st, tp = Store(), Transport()
    host = Hosting(open_pr=PR)
    so = SignOff(hosting=host, verification=None, transport=tp, store=st)
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merged"
    assert "Перевести задачу нечем" in tp.said()


def test_a_task_the_tracker_does_not_know_is_not_a_crash():
    st, tp, host, board, so = mk(issues=in_review("DEV-999", "iss-999"))
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merged"
    assert "Задачи в трекере нет" in tp.said()


# ── 6. «Отменить» PR не трогает ──────────────────────────────────────────

def test_the_cancel_button_moves_the_task_and_leaves_the_pull_request_alone():
    st, tp, host, board, so = mk()
    assert run(so.on_click(CANCEL + "DEV-123", CHAT, "101", "Андрей", CHAT)) == "canceled"
    assert host.merges == [] and host.asked == 0
    assert board.moved == [("iss-123", "Canceled")]
    body = board.comments[0][1]
    assert "Отменено человеком" in body and "`Canceled`" in body
    assert "НЕ тронут" in body and CHAT in body
    said = tp.said()
    assert "Задача переведена в Canceled." in said and "отдельное решение" in said


def test_cancelling_a_closed_task_moves_nothing():
    st, tp, host, board, so = mk()
    run(so.on_click(CANCEL + "DEV-123", CHAT, "101"))
    assert run(so.on_click(CANCEL + "DEV-123", CHAT, "101")) == "canceled"
    assert len(board.moved) == 1 and len(board.comments) == 1
    assert "уже была закрыта" in tp.said()


def test_a_tracker_failure_on_cancel_is_visible_in_the_chat():
    st, tp, host, board, so = mk(board_boom=True)
    assert run(so.on_click(CANCEL + "DEV-123", CHAT, "101")) == "tracker-failed"
    assert "Трекер не ответил" in tp.said() and board.moved == []


def test_cancelling_without_a_verification_says_so():
    so = SignOff(hosting=None, verification=None, transport=Transport(), store=Store())
    assert run(so.on_click(CANCEL + "DEV-123", CHAT, "101")) == "no-tracker"


# ── кто вправе нажать ────────────────────────────────────────────────────

def test_only_those_who_sign_the_work_may_press():
    st, tp, host, board, so = mk(may_act=lambda uid: str(uid) == CHAT)
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101", "Сосед", "999")) == "not-allowed"
    assert host.asked == 0 and host.merges == [] and board.moved == []
    assert "не выполнено" in tp.said()
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101", "Андрей", CHAT)) == "merged"


# ── роутер канала ────────────────────────────────────────────────────────

def test_the_channel_routes_the_press_to_the_signoff():
    from agent_factory.channel import Channel

    seen = []

    class Sign:
        async def on_click(self, data, chat_id, message_id="", clicker="",
                           clicker_id=None):
            seen.append((data, str(chat_id), message_id, clicker, clicker_id))
            return "merged"

    st, tp = Store(), Transport()
    ch = Channel("telegram", st, tp, None, Q, signoff=Sign())
    got = run(ch.on_click(MERGE + "DEV-123", CHAT, "101", "cb", "Андрей", CHAT))
    assert got == "merged"
    assert seen == [(MERGE + "DEV-123", CHAT, "101", "Андрей", CHAT)]


def test_a_press_with_the_signoff_switched_off_is_not_silent():
    from agent_factory.channel import Channel

    st, tp = Store(), Transport()
    ch = Channel("telegram", st, tp, None, Q)
    assert run(ch.on_click(CANCEL + "DEV-123", CHAT, "101")) == "no-signoff"
    assert "не подключена" in tp.said()


# ── «часики» гасятся ДО походов в сеть ───────────────────────────────────

def test_the_spinner_is_answered_before_anything_goes_to_the_network():
    """Мерж — это `gh` по нескольким репозиториям, секунды. Всё это время
    незажатая кнопка приглашает нажать её снова."""
    from agent_factory.telegram import TelegramIntake

    order = []

    class Tp:
        markup = Markup()

        async def answer(self, cb_id, notice=""):
            order.append(("answer", notice))

        async def send(self, *a, **kw):
            order.append(("send", ""))
            return "1"

    class Ch:
        async def on_click(self, data, chat_id, message_id="", callback_id="",
                           clicker="", clicker_id=None):
            order.append(("click", data))
            return "merged"

    app = TelegramIntake(token="т", redis_url="redis://127.0.0.1:6379/0",
                         queue_key=Q, allowed={"1"})
    app.transport = Tp()
    app.channel = Ch()
    run(app._on_click({"id": "cb1", "data": MERGE + "DEV-123", "from": {"id": 1},
                       "message": {"message_id": 101, "chat": {"id": CHAT}}}))
    assert order == [("answer", "Мержу"), ("click", MERGE + "DEV-123")]

    order.clear()
    run(app._on_click({"id": "cb2", "data": CANCEL + "DEV-123", "from": {"id": 1},
                       "message": {"message_id": 101, "chat": {"id": CHAT}}}))
    assert order[0] == ("answer", "Отменяю")


# ── сработавшее нажатие снимает кнопки ───────────────────────────────────

def wired(open_pr=PR, merged=None, merge_boom="", feed_on=True):
    """То же, что `mk`, но с НАСТОЯЩЕЙ лентой и уже показанным сообщением
    задачи: кнопки доехали до чата, и дальше их снимает нажатие.

    Проверяется тем, что видит человек (клавиатура правки сообщения) И
    памятью ленты: правка статуса возвращает кнопки из неё, и снятая мимо
    памяти клавиатура вернулась бы через секунду.
    """
    st, tp = Store(), Transport()
    host = Hosting(open_pr=open_pr, merged=merged, merge_boom=merge_boom)
    board = Board(in_review())
    v = Verification(st, Q, None, tracker=board)
    so = SignOff(hosting=host, verification=v, transport=tp, store=st)
    feed = IssueFeed(st, tp, board,
                     People({ANDREY: CHAT}, owner_user_id=ANDREY, store=st),
                     agent_user_id=AGENT, handoff=so)
    if feed_on:
        so.feed, so.lookup = feed, board.issue_by_identifier
    run(feed.show(handoff()))
    assert tp.sent[-1]["buttons"]          # кнопки были — иначе снимать нечего
    return st, tp, host, board, feed, so


def buttons_after_a_status_change(feed, tp):
    """Что окажется под сообщением на следующей перерисовке. Именно она и
    возвращала бы снятые мимо памяти кнопки."""
    run(feed.repaint("iss-123", status="Done"))
    return tp.edits[-1]["buttons"]


def test_a_merge_that_worked_takes_the_buttons_off_the_message():
    """Мерж происходит один раз, а кнопка под сообщением жила дальше и
    обещала сделать его снова."""
    st, tp, host, board, feed, so = wired()
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merged"
    assert tp.edits[-1]["buttons"] == []
    assert buttons_after_a_status_change(feed, tp) == []


def test_a_second_press_on_an_already_merged_request_takes_them_off_too():
    """Первое нажатие смержило, а кнопки остались — например перевод статуса
    не прошёл и событие трекера их не сняло. Второе нажатие обязано и
    ответить правду, и убрать кнопку."""
    st, tp, host, board, feed, so = wired(open_pr=None, merged={
        "mr_iid": "3", "project_path": "org/agent-factory", "ref": "main",
        "source_ref": "agent/DEV-123", "url": "", "merged_at": "2026-09-14T10:00:00Z"})
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "already-merged"
    assert buttons_after_a_status_change(feed, tp) == []


def test_a_cancel_takes_the_buttons_off_as_well():
    st, tp, host, board, feed, so = wired()
    assert run(so.on_click(CANCEL + "DEV-123", CHAT, "101")) == "canceled"
    assert buttons_after_a_status_change(feed, tp) == []
    assert host.merges == []               # PR не тронут


def test_a_press_that_did_not_work_keeps_the_buttons():
    """Единственный способ повторить не отбирают вместе с кнопкой."""
    st, tp, host, board, feed, so = wired(merge_boom="gh: conflict")
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merge-failed"
    assert buttons_after_a_status_change(feed, tp) == ["✅ Слить", "🚫 Отменить"]


def test_nothing_to_merge_keeps_the_buttons_too():
    """Ни открытого PR, ни смерженного — работа могла и не дойти до ветки.
    Кнопка остаётся: «Отменить» тут единственный выход, и снять её значит
    запереть задачу. (Самой «Слить» под таким сообщением и не было: на показе
    открытого PR уже не нашлось.)"""
    st, tp, host, board, feed, so = wired(open_pr=None, merged=None)
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "nothing-to-merge"
    assert buttons_after_a_status_change(feed, tp) == ["🚫 Отменить"]


def test_without_a_feed_the_merge_still_happens():
    """Снять кнопки нечем — это строка в журнале, а не отказ на мерже:
    закрытие задачи снимет их событием трекера."""
    st, tp, host, board, feed, so = wired(feed_on=False)
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merged"
    assert len(host.merges) == 1
    assert board.moved == [("iss-123", "Done")]


def test_a_merge_whose_status_move_failed_keeps_the_buttons():
    """Мерж прошёл, а перевод задачи — нет. Кнопки тут снимать нельзя:
    второе нажатие именно этот перевод и доводит до конца
    (`test_a_second_press_finishes_a_merge_whose_tracker_step_failed`), и
    снятая кнопка отобрала бы единственный способ починить это из чата."""
    st, tp, host, board, feed, so = wired()
    # Отказ ТОЧЕЧНЫЙ: задачу трекер отдаёт, а статус сменить не даёт. Свались
    # он целиком — кнопки остались бы и без сторожа (снять их тоже нечем), и
    # тест прошёл бы вхолостую.
    board.move_boom = True
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "merged"
    assert host.merges and board.moved == []          # смержено, не переведено
    assert "Трекер не ответил" in tp.said()
    assert buttons_after_a_status_change(feed, tp) == ["✅ Слить", "🚫 Отменить"]


def test_a_second_press_that_repairs_the_status_move_takes_the_buttons_off():
    """А когда второе нажатие перевод ДОВЕЛО — кнопки уходят: чинить больше
    нечего."""
    st, tp, host, board, feed, so = wired(open_pr=None, merged={
        "mr_iid": "3", "project_path": "org/agent-factory", "ref": "main",
        "source_ref": "agent/DEV-123", "url": "", "merged_at": "2026-09-14T10:00:00Z"})
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "already-merged"
    assert board.moved == [("iss-123", "Done")]
    assert buttons_after_a_status_change(feed, tp) == []


def test_a_second_press_that_could_not_repair_the_move_keeps_the_buttons():
    """Второе нажатие по смерженному PR: перевод опять не прошёл. Кнопка
    остаётся — чинить ещё есть что, и чинится это ровно ею."""
    st, tp, host, board, feed, so = wired(open_pr=None, merged={
        "mr_iid": "3", "project_path": "org/agent-factory", "ref": "main",
        "source_ref": "agent/DEV-123", "url": "", "merged_at": "2026-09-14T10:00:00Z"})
    board.move_boom = True
    assert run(so.on_click(MERGE + "DEV-123", CHAT, "101")) == "already-merged"
    assert board.moved == [] and host.merges == []
    assert "уже был смержен" in tp.said() and "Трекер не ответил" in tp.said()
    assert buttons_after_a_status_change(feed, tp) == ["🚫 Отменить"]
