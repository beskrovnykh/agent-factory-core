"""Тесты канала — того, что у обеих инсталляций одно и то же."""
import asyncio
import json

from agent_factory.channel import Channel
from agent_factory.intake import Item, batch_key, control_key
from agent_factory.progress import progress_key


class Store:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.vals: dict[str, str] = {}
        self.claimed: set[str] = set()

    async def append(self, key, value):
        self.lists.setdefault(key, []).append(value); return len(self.lists[key])

    async def read_all(self, key): return list(self.lists.get(key) or [])
    async def get(self, key): return self.vals.get(key)
    async def put(self, key, value, ttl): self.vals[key] = value
    async def expire(self, key, ttl): pass
    async def enqueue(self, queue, element): await self.append(queue, element)
    async def position(self, queue, element):
        return (self.lists.get(queue) or []).index(element) if element in (
            self.lists.get(queue) or []) else None

    async def drop(self, key):
        self.lists.pop(key, None); self.vals.pop(key, None)
        self.claimed.discard(key)

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key); return True


class Transport:
    def __init__(self):
        self.sent, self.edited, self.deleted = [], [], []
        self._n = 0

    async def send(self, chat_id, text, buttons=None):
        self._n += 1; self.sent.append(text); return f"mid.{self._n}"

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((message_id, text, buttons)); return True

    async def delete(self, chat_id, message_id):
        self.deleted.append(message_id); return True

    async def answer(self, callback_id, notice=""): pass


class Tracker:
    def __init__(self): self.assigned, self.deleted = [], []
    def create_issue(self, *a, **k): return {"id": "u", "identifier": "X-1", "url": "l"}
    def assign(self, issue_id, user_id):
        self.assigned.append(issue_id)
        return {"id": issue_id, "identifier": "X-1", "url": "l", "already": False}
    def delete(self, issue_id): self.deleted.append(issue_id); return "X-1"


def mk(source="telegram"):
    st, tr, tk = Store(), Transport(), Tracker()
    ch = Channel(source, st, tr, tk, "agent:queue:x", allowed={"1"},
                 agent_user_id="агент", progress_opts={"tick_sec": 0})
    return st, tr, tk, ch


async def collected(ch, st, tr):
    await ch.on_item(1, Item(type="text", source=ch.source, text="фактура"))
    return await ch.on_click("intake:collect", 1, "mid.1")


def test_collect_queues_the_batch_and_keeps_the_counter():
    st, tr, tk, ch = mk()
    assert asyncio.run(collected(ch, st, tr)) == "queued"
    assert len(st.lists["agent:queue:x"]) == 1
    assert st.lists.get(batch_key("telegram", 1)) is None, "пачка ушла в заход"
    # Счётчик стал шапкой сборки в том же сообщении, а не новым пузырём.
    assert tr.edited[-1][0] == "mid.1" and "Собираю" in tr.edited[-1][1]


def test_collect_on_an_empty_batch_burns_no_slot():
    st, tr, tk, ch = mk()
    assert asyncio.run(ch.on_click("intake:collect", 1, "m")) == "empty"
    assert st.lists.get("agent:queue:x") is None


def test_reset_clears_the_batch():
    st, tr, tk, ch = mk()

    async def go():
        await ch.on_item(1, Item(type="text", source="telegram", text="a"))
        return await ch.on_click("intake:reset", 1, "mid.1")

    assert asyncio.run(go()) == "reset"
    assert st.lists.get(batch_key("telegram", 1)) is None


def test_retry_reuses_the_batch_it_already_has():
    """Пересылать переписку второй раз человек не должен, иначе кнопка не
    стоит своего места."""
    st, tr, tk, ch = mk()

    async def go():
        await collected(ch, st, tr)
        # сборка упала
        draft_id = next(k.split(":")[-1] for k in st.vals
                        if k.startswith("intake:telegram:progress:"))
        await ch.progress.failed(draft_id, "сессия упала")
        st.lists["agent:queue:x"].clear()
        return draft_id, await ch.on_click(f"intake:retry:{draft_id}", 1, "mid.1")

    draft_id, out = asyncio.run(go())
    assert out == "queued"
    task = json.loads(st.lists["agent:queue:x"][0])
    assert task["draft_id"] == draft_id, "тот же черновик, та же фактура"
    assert task["items"][0]["text"] == "фактура"


def test_retry_of_a_running_collect_does_not_double_the_session():
    st, tr, tk, ch = mk()

    async def go():
        await collected(ch, st, tr)
        draft_id = next(k.split(":")[-1] for k in st.vals
                        if k.startswith("intake:telegram:progress:"))
        return await ch.on_click(f"intake:retry:{draft_id}", 1, "mid.1")

    assert asyncio.run(go()) == "already-running"
    assert len(st.lists["agent:queue:x"]) == 1


def test_retry_without_a_remembered_batch_says_so():
    st, tr, tk, ch = mk()
    st.vals[progress_key("telegram", "d1")] = json.dumps(
        {"state": "failed", "items": [], "message_id": "mid.9"})
    assert asyncio.run(ch.on_click("intake:retry:d1", 1, "mid.9")) == "nothing-to-retry"
    assert "перешли заново" in tr.edited[-1][1]


def test_card_buttons_go_through_the_same_door():
    """Кнопки карточки — тот же канал: у клиента с вебхуками не должно быть
    своей копии этой развилки."""
    st, tr, tk, ch = mk()
    assert asyncio.run(ch.on_click("intake:agent:u1", 1, "m", clicker="Андрей")) == "assigned"
    assert tk.assigned == ["u1"]
    assert asyncio.run(ch.on_click("intake:delete:u2", 1, "m", clicker="Андрей")) == "deleted"
    assert tk.deleted == ["u2"]


def test_an_unknown_button_is_not_silently_swallowed():
    st, tr, tk, ch = mk()
    assert asyncio.run(ch.on_click("intake:чтотоновое:1", 1, "m")) == "unknown"


def test_a_refused_piece_is_explained_not_dropped():
    st, tr, tk, ch = mk()
    big = Item(type="document", source="telegram", file_name="дамп",
               file_size=999 * 1024 * 1024)
    assert asyncio.run(ch.on_item(1, big)) == 0
    assert tr.sent and "дамп" in tr.sent[-1]


def test_max_channel_uses_max_keys():
    """Два мессенджера в одной инсталляции не должны видеть пачки друг друга."""
    st, tr, tk, ch = mk(source="max")
    asyncio.run(ch.on_item(7, Item(type="text", source="max", text="a")))
    assert batch_key("max", 7) in st.lists
    assert control_key("max", 7) in st.vals


def test_installation_knobs_reach_the_parts_that_use_them():
    """Ручки клиента (потолок пачки, темп тика, сторожевые сроки) молча
    выброшенными быть не могут: настройка перестанет работать без следа."""
    st, tr, tk = Store(), Transport(), Tracker()
    ch = Channel("telegram", st, tr, tk, "q",
                 intake_opts={"max_items": 3, "max_file_bytes": 111},
                 progress_opts={"tick_sec": 0, "expected_sec": 42,
                                "timeout_sec": 7, "grace_sec": 1, "max_sec": 9})
    assert (ch.intake.max_items, ch.intake.max_file_bytes) == (3, 111)
    assert (ch.progress.tick_sec, ch.progress.expected_sec) == (0, 42)
    assert (ch.progress.timeout_sec, ch.progress.grace_sec, ch.progress.max_sec) == (7, 1, 9)


def test_a_button_from_a_forwarded_message_does_not_touch_this_chat():
    """Кнопка адресует пачку СВОЕГО чата: пересланное сообщение с живой
    кнопкой иначе собрало бы чужую пачку."""
    st, tr, tk, ch = mk()

    async def go():
        await ch.on_item(1, Item(type="text", source="telegram", text="моё"))
        return await ch.on_click("intake:collect:999", 1, "mid.1")

    assert asyncio.run(go()) == "foreign-chat"
    assert st.lists.get("agent:queue:x") is None
    assert st.lists.get(batch_key("telegram", 1)), "пачка на месте"


def test_a_button_without_an_address_still_works():
    """Кнопки старого образца висят в ленте у людей — ломать их нельзя."""
    st, tr, tk, ch = mk()

    async def go():
        await ch.on_item(1, Item(type="text", source="telegram", text="моё"))
        return await ch.on_click("intake:collect", 1, "mid.1")

    assert asyncio.run(go()) == "queued"


def test_the_button_carries_the_chat_it_belongs_to():
    st, tr, tk, ch = mk()
    asyncio.run(ch.on_item(77, Item(type="text", source="telegram", text="a")))
    from agent_factory.intake import collect_keyboard
    assert [b.payload for b in collect_keyboard(77)] == [
        "intake:collect:77", "intake:reset:77"]


def test_the_count_grows_with_the_batch():
    """Число, которое видит человек на счётчике, — то же, что отдаёт канал."""
    st, tr, tk, ch = mk()

    async def go():
        a = await ch.on_item(1, Item(type="text", source="telegram", text="раз"))
        b = await ch.on_item(1, Item(type="text", source="telegram", text="два"))
        return a, b

    assert asyncio.run(go()) == (1, 2)


def test_the_button_does_not_queue_twice_where_a_webhook_already_does():
    """У инсталляции с вебхуком трекера назначение ловит он. Если кнопка ещё
    и ставит сама — это вторая сессия на ту же задачу. 11.09 по TASK-285 и
    TASK-288 так и стартовало по две: две ветки, два «взялся», полсессии на
    то, чтобы обнаружить соседа."""
    st, tr, tk = Store(), Transport(), Tracker()
    ch = Channel("telegram", st, tr, tk, "agent:queue:x", agent_user_id="агент",
                 enqueue_on_assign=False, progress_opts={"tick_sec": 0})
    assert asyncio.run(ch.on_click("intake:agent:u1", 1, "m", clicker="Андрей")) == "assigned"
    assert tk.assigned == ["u1"], "назначение всё равно делается"
    assert st.lists.get("agent:queue:x") is None, "а в очередь кладёт вебхук"


def test_the_button_still_queues_where_there_is_no_webhook():
    """У второй инсталляции публичного HTTPS нет, вебхуку взяться неоткуда —
    без постановки прямо из кнопки задача не поехала бы никуда."""
    st, tr, tk, ch = mk()
    asyncio.run(ch.on_click("intake:agent:u1", 1, "m", clicker="Андрей"))
    assert len(st.lists["agent:queue:x"]) == 1
