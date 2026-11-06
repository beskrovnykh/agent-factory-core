"""Тесты счётчика сборки."""
import asyncio
import json

from agent_factory import progress as P
from agent_factory.progress import Progress, clock, progress_text, retry_keyboard


class FakeStore:
    def __init__(self, in_queue=True, position_raises=False):
        self.vals: dict[str, str] = {}
        self.in_queue = in_queue
        self.position_raises = position_raises
        self.queue: list[str] = []

    async def get(self, key): return self.vals.get(key)
    async def put(self, key, value, ttl): self.vals[key] = value
    async def drop(self, key): self.vals.pop(key, None)
    async def enqueue(self, queue, element): self.queue.append(element)

    async def position(self, queue, element):
        if self.position_raises:
            raise RuntimeError("redis лёг")
        return 0 if self.in_queue else None


class FakeTransport:
    def __init__(self, edit_ok=True):
        self.edited: list[tuple] = []
        self.edit_ok = edit_ok

    async def send(self, chat_id, text, buttons=None): return "m1"

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((chat_id, message_id, text, buttons))
        return self.edit_ok


def mk(**kw):
    st = FakeStore(in_queue=kw.pop("in_queue", True),
                   position_raises=kw.pop("position_raises", False))
    tr = FakeTransport()
    return st, tr, Progress("telegram", st, tr, "agent:queue:x", **kw)


ITEMS = [{"type": "text"}, {"type": "photo"}]


# ── тексты ───────────────────────────────────────────────────────────────

def test_clock_is_minutes_and_seconds():
    assert clock(0) == "0:00" and clock(75) == "1:15" and clock(-5) == "0:00"


def test_waiting_for_a_slot_is_named_differently_from_working():
    """«Жду слот» и «собираю» — разные состояния. Одинаковая строчка на них
    врёт: человек думает, что сессия идёт, когда она даже не началась."""
    a = progress_text("шапка", 30, queued=True)
    b = progress_text("шапка", 30, queued=False)
    assert a != b and "слот" in a and "обычно около" in b


def test_expected_wait_is_shown():
    """Знающий, что это минута, не идёт переключать окна на сороковой секунде."""
    assert "1:15" in progress_text("h", 5, False, expected=75)


def test_retry_button_is_honest_about_the_limit():
    """Повтор до сброса упадёт так же — предлагать его молча нельзя."""
    assert "после сброса" in retry_keyboard("d1", limit=True)[0].text
    assert "после сброса" not in retry_keyboard("d1", limit=False)[0].text
    assert retry_keyboard("d1")[0].payload == "intake:retry:d1"


# ── запись о сборке ──────────────────────────────────────────────────────

def test_start_remembers_the_batch_for_retry():
    """Без пачки в записи «Повторить» требовал бы переслать переписку заново,
    и кнопка не стоила бы своего места."""
    st, tr, p = mk(tick_sec=0)
    asyncio.run(p.start(1, "m5", "d1", "шапка", ITEMS, "элемент"))
    rec = json.loads(st.vals[p.key("d1")])
    assert rec["items"] == ITEMS and rec["message_id"] == "m5"
    assert rec["head"] == "шапка" and rec["state"] == "running"


def test_no_message_means_no_record():
    """Нечего тикать: сообщение не создалось, и запись о нём соврала бы."""
    st, tr, p = mk(tick_sec=0)
    asyncio.run(p.start(1, "", "d1", "шапка", ITEMS, "элемент"))
    assert st.vals == {}


def test_done_replaces_the_counter_and_drops_the_record():
    st, tr, p = mk(tick_sec=0)
    asyncio.run(p.start(1, "m5", "d1", "шапка", ITEMS, "e"))
    asyncio.run(p.done("d1", "Задача заведена"))
    assert "Задача заведена" in tr.edited[-1][2]
    assert tr.edited[-1][3] is None, "кнопок под подписью быть не должно"
    assert st.vals == {}, "запись отработала"


def test_failed_offers_retry_and_keeps_the_batch():
    st, tr, p = mk(tick_sec=0)
    asyncio.run(p.start(1, "m5", "d1", "шапка", ITEMS, "e"))
    assert asyncio.run(p.failed("d1", "сессия упала")) is True
    assert tr.edited[-1][3], "кнопка «Повторить» обязана быть"
    rec = json.loads(st.vals[p.key("d1")])
    assert rec["state"] == "failed" and rec["items"] == ITEMS


def test_failed_without_a_batch_does_not_offer_retry():
    """Повтор без фактуры — кнопка, которая заведомо ничего не соберёт."""
    st, tr, p = mk(tick_sec=0)
    asyncio.run(p.start(1, "m5", "d1", "шапка", [], "e"))
    asyncio.run(p.failed("d1", "сессия упала"))
    assert tr.edited[-1][3] is None
    assert "заново" in tr.edited[-1][2]


def test_failed_without_a_record_tells_the_caller_to_speak_up():
    """Счётчика нет (выключен, протух, процесс перезапустился) — вызывающий
    обязан ответить обычным сообщением, а не промолчать."""
    st, tr, p = mk(tick_sec=0)
    assert asyncio.run(p.failed("нет-такого", "повод")) is False
    assert tr.edited == []


def test_limit_says_when_the_reset_is():
    st, tr, p = mk(tick_sec=0)
    asyncio.run(p.start(1, "m5", "d1", "шапка", ITEMS, "e"))
    asyncio.run(p.failed("d1", "лимит", limit=True, limit_reset="04:20 UTC"))
    assert "04:20 UTC" in tr.edited[-1][2]


# ── тик ──────────────────────────────────────────────────────────────────

def test_ticker_edits_the_same_message():
    st, tr, p = mk(tick_sec=0.01, in_queue=True)
    asyncio.run(_run_ticks(p, st, tr))
    assert tr.edited, "счётчик должен править сообщение"
    assert all(e[1] == "m5" for e in tr.edited), "своё, а не новое"
    assert "слот" in tr.edited[0][2]


async def _run_ticks(p, st, tr, pause=0.06):
    await p.start(1, "m5", "d1", "шапка", ITEMS, "e")
    await asyncio.sleep(pause)
    await p.stop("d1")


def test_a_broken_position_lookup_does_not_start_the_watchdog():
    """Одна осечка не должна заводить сторожевой срок сессии на задаче, которая
    всё ещё ждёт слота: иначе человек получает «сессия не ответила» с кнопкой,
    нажатие которой даёт вторую сессию на ту же пачку."""
    st, tr, p = mk(tick_sec=0.01, position_raises=True, timeout_sec=0, grace_sec=0)
    asyncio.run(_run_ticks(p, st, tr))
    assert not any("не ответила" in e[2] for e in tr.edited)
    rec = json.loads(st.vals[p.key("d1")])
    assert rec["state"] == "running"


def test_watchdog_fires_when_the_session_never_answers():
    st, tr, p = mk(tick_sec=0.01, in_queue=False, timeout_sec=0, grace_sec=0)
    asyncio.run(_run_ticks(p, st, tr))
    assert any("не ответила" in e[2] for e in tr.edited)


def test_waiting_for_a_slot_forever_is_capped():
    """Счётчик, идущий десятый час, врёт про состояние ровно так же, как
    молчащее сообщение."""
    st, tr, p = mk(tick_sec=0.01, in_queue=True, max_sec=0)
    asyncio.run(_run_ticks(p, st, tr))
    assert any("не дождался свободного слота" in e[2] for e in tr.edited)


def test_stop_leaves_the_final_caption_standing():
    """Тик, приземлившийся после финальной надписи, вернул бы на экран
    замерший счётчик и стёр бы вместе с ним кнопку «Повторить»."""
    st, tr, p = mk(tick_sec=0.01, in_queue=True)

    async def go():
        await p.start(1, "m5", "d1", "шапка", ITEMS, "e")
        await asyncio.sleep(0.05)
        await p.done("d1", "Задача заведена")
        await asyncio.sleep(0.05)
        return tr.edited[-1][2]

    assert "Задача заведена" in asyncio.run(go())


def test_tick_zero_means_no_ticker_but_still_a_record():
    """Индикатор выключают конфигурацией; исход всё равно обязан доехать."""
    st, tr, p = mk(tick_sec=0)
    asyncio.run(_run_ticks(p, st, tr))
    assert tr.edited == []
    assert st.vals, "запись нужна и без тика — по ней отработает исход"


def test_a_tick_woken_before_the_stop_does_not_repaint():
    """Окно между «проснулся» и «правлю»: остановка приходит, пока тик ходит
    в хранилище за позицией. Правка после этого приземлилась бы уже поверх
    финальной надписи — вернув замерший счётчик и стерев «Повторить»."""
    st, tr, p = mk(tick_sec=0.01, in_queue=True)

    async def stop_midway(queue, element):
        # Ровно то, что делает done()/failed(): просит тик замолчать.
        entry = p._tasks.get("d1")
        if entry:
            entry[1].set()
        return 0

    st.position = stop_midway

    async def go():
        await p.start(1, "m5", "d1", "шапка", ITEMS, "e")
        await asyncio.sleep(0.08)

    asyncio.run(go())
    assert tr.edited == [], "после просьбы замолчать правок быть не должно"
