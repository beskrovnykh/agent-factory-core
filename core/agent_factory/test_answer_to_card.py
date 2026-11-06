"""Ответ на карточку в мессенджере доезжает до задачи (DEV-74).

Приёмщик умеет задать вопрос: поле `question` едет разделом в задачу и строкой
`❓` в карточку. Момент правильный — человек ещё в переписке. Но ответить он не
мог: транспорт не видел, что сообщение является ответом, а приёмщик, отправив
карточку, забывал её. Ответ уходил в пачку и становился ВТОРОЙ задачей.

Тесты держат обе половины связи и, главное, оба способа её не потерять: связь
неизвестна — кусок ведёт себя как обычный; трекер не принял — человеку сказали.
Молчание тут худший исход: человек считает, что ответил.
"""
import asyncio
import json

from agent_factory import intake as intake_mod
from agent_factory.channel import Channel
from agent_factory.intake import ANSWER_TTL_SEC, Item, answer_key, batch_key
from agent_factory.max_messenger import item_from_message as max_item
from agent_factory.results import Results
from agent_factory.telegram import item_from_message as tg_item


class Store:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.vals: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.claimed: set[str] = set()

    async def append(self, key, value):
        self.lists.setdefault(key, []).append(value); return len(self.lists[key])

    async def read_all(self, key): return list(self.lists.get(key) or [])
    async def get(self, key): return self.vals.get(key)

    async def put(self, key, value, ttl):
        self.vals[key] = value; self.ttls[key] = ttl

    async def expire(self, key, ttl): pass
    async def enqueue(self, queue, element): await self.append(queue, element)
    async def position(self, queue, element): return None

    async def drop(self, key):
        self.lists.pop(key, None); self.vals.pop(key, None)

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key); return True


class Transport:
    def __init__(self):
        self.sent: list[str] = []
        self._n = 0

    async def send(self, chat_id, text, buttons=None):
        self._n += 1; self.sent.append(text); return f"mid.{self._n}"

    async def edit(self, chat_id, message_id, text, buttons=None): return True
    async def delete(self, chat_id, message_id): return True
    async def answer(self, callback_id, notice=""): pass


class Tracker:
    def __init__(self, fails=False):
        self.comments: list[tuple[str, str]] = []
        self.fails = fails

    def create_issue(self, *a, **k): return {"id": "u", "identifier": "X-1", "url": "l"}
    def assign(self, issue_id, user_id): return {"id": issue_id, "identifier": "X-1"}
    def delete(self, issue_id): return "X-1"

    def comment(self, issue_id, body):
        if self.fails:
            raise RuntimeError("трекер недоступен")
        self.comments.append((issue_id, body))


def mk(source="telegram", fails=False):
    st, tr, tk = Store(), Transport(), Tracker(fails)
    ch = Channel(source, st, tr, tk, "agent:queue:x", allowed={"1"},
                 agent_user_id="агент", progress_opts={"tick_sec": 0})
    return st, tr, tk, ch


def remember(st, source, chat_id, card_id, ident="DEV-70", issue_id="uuid-70"):
    st.vals[answer_key(source, chat_id, card_id)] = json.dumps(
        {"id": issue_id, "identifier": ident}, ensure_ascii=False)


# ── транспорты: кусок знает, что он ответ ────────────────────────────────────

def test_telegram_reply_is_recognised():
    it = tg_item({"message_id": 42, "text": "порядок по убыванию даты",
                  "reply_to_message": {"message_id": 7}})
    assert it.reply_to == "7"


def test_telegram_plain_message_is_not_a_reply():
    assert tg_item({"message_id": 42, "text": "просто фактура"}).reply_to == ""


def test_telegram_forward_is_new_material_not_a_reply():
    """У пересылки `reply_to_message` нет — и это правильно: переслали чужое
    сообщение, а не ответили на карточку."""
    it = tg_item({"message_id": 42, "text": "смотри что пишут",
                  "forward_from": {"first_name": "Иван"}})
    assert it.reply_to == ""
    assert it.frm == "Иван"


def test_max_reply_comes_through_link():
    it = max_item({"body": {"mid": "m2", "text": "да, именно так"},
                   "link": {"type": "reply", "message": {"mid": "m1"}}})
    assert it.reply_to == "m1"


def test_max_forward_is_not_a_reply_though_it_uses_the_same_link():
    """У MAX пересылка и ответ приезжают одним `link`, разводит их только
    `type`. Перепутать нельзя: у пересылки в `link.message` лежит НОВАЯ
    фактура, у ответа — то, на что отвечают."""
    it = max_item({"body": {"mid": "m2", "text": ""},
                   "link": {"type": "forward", "message": {"text": "фактура"},
                            "sender": {"first_name": "Иван"}}})
    assert it.reply_to == ""
    assert it.text == "фактура" and it.frm == "Иван"


def test_max_reply_to_travels_with_attachments_too():
    """Развилка сегодня берёт только текст, но транспорт обязан сообщать
    правду о каждом куске: иначе следующий, кто решит обрабатывать вложения,
    получит пустое поле и не поймёт, почему."""
    link = {"type": "reply", "message": {"mid": "m1"}}
    photo = max_item({"link": link, "body": {"mid": "m2", "text": "", "attachments": [
        {"type": "image", "payload": {"url": "http://x/p.webp", "photo_id": 5}}]}})
    doc = max_item({"link": link, "body": {"mid": "m3", "text": "", "attachments": [
        {"type": "file", "payload": {"url": "http://x/f.pdf"}, "filename": "f.pdf"}]}})
    assert photo.type == "photo" and photo.reply_to == "m1"
    assert doc.type == "document" and doc.reply_to == "m1"


# ── ключ связи ───────────────────────────────────────────────────────────────

def test_answer_key_separates_messengers_and_chats():
    """Нумерация сообщений у мессенджеров своя, и номер из MAX однажды совпадёт
    с номером из Telegram. Без источника в ключе ответ уехал бы в чужую задачу.
    """
    assert answer_key("telegram", 1, "5") != answer_key("max", 1, "5")
    assert answer_key("telegram", 1, "5") != answer_key("telegram", 2, "5")


# ── приёмщик запоминает свою карточку ────────────────────────────────────────

def test_results_remembers_the_card_it_sent():
    st, tr, tk = Store(), Transport(), Tracker()
    res = Results(intake_mod.Intake(source="telegram", store=st, transport=tr,
                                    queue_key="q", allowed={"1"}),
                  st, tr, tk)
    issue = {"id": "uuid-70", "identifier": "DEV-70", "url": "http://x"}
    asyncio.run(res._remember_card(7, "mid.1", issue))
    saved = json.loads(st.vals[answer_key("telegram", 7, "mid.1")])
    assert saved == {"id": "uuid-70", "identifier": "DEV-70"}
    # Связь не вечная: карточек копятся сотни, отвечают почти всегда сразу.
    assert st.ttls[answer_key("telegram", 7, "mid.1")] == ANSWER_TTL_SEC


def test_results_survives_a_card_sent_without_an_id():
    """Задача заведена и карточка ушла — это главное. Потерянная связь стоит
    одного ответа, ушедшего в пачку; упавший приём стоит дороже."""
    st, tr, tk = Store(), Transport(), Tracker()
    res = Results(intake_mod.Intake(source="telegram", store=st, transport=tr,
                                    queue_key="q", allowed={"1"}),
                  st, tr, tk)
    asyncio.run(res._remember_card(7, "", {"id": "u", "identifier": "DEV-70"}))
    assert st.vals == {}


# ── развилка в канале ────────────────────────────────────────────────────────

def test_reply_to_a_known_card_becomes_a_comment_not_a_batch_item():
    st, tr, tk, ch = mk()
    remember(st, "telegram", 1, "mid.9")
    left = asyncio.run(ch.on_item(1, Item(type="text", source="telegram",
                                          text="да, по убыванию", reply_to="mid.9")))
    assert left == 0
    assert tk.comments == [("uuid-70", "да, по убыванию")]
    assert not st.lists.get(batch_key("telegram", 1))
    assert any("DEV-70" in s for s in tr.sent)


def test_an_unknown_or_expired_card_does_not_swallow_the_answer():
    """Связь истекла — это не повод терять текст. Пусть идёт в пачку: там
    человек хотя бы увидит его на счётчике."""
    st, tr, tk, ch = mk()
    left = asyncio.run(ch.on_item(1, Item(type="text", source="telegram",
                                          text="ответ в пустоту", reply_to="mid.404")))
    assert left == 1
    assert tk.comments == []
    assert st.lists.get(batch_key("telegram", 1))


def test_a_plain_message_behaves_exactly_as_before():
    st, tr, tk, ch = mk()
    remember(st, "telegram", 1, "mid.9")
    left = asyncio.run(ch.on_item(1, Item(type="text", source="telegram",
                                          text="новая фактура")))
    assert left == 1
    assert tk.comments == []


def test_an_attachment_in_reply_still_goes_to_the_batch():
    """Вложение в трекер надо загружать — это своя работа, и делать её молча
    внутри развилки нельзя."""
    st, tr, tk, ch = mk()
    remember(st, "telegram", 1, "mid.9")
    left = asyncio.run(ch.on_item(1, Item(type="photo", source="telegram",
                                          text="вот так", reply_to="mid.9",
                                          file_id="f1", file_name="s.jpg")))
    assert left == 1
    assert tk.comments == []


def test_a_failed_comment_is_reported_and_not_dropped_into_the_batch():
    """Молчание здесь худший исход: человек считает, что ответил."""
    st, tr, tk, ch = mk(fails=True)
    remember(st, "telegram", 1, "mid.9")
    left = asyncio.run(ch.on_item(1, Item(type="text", source="telegram",
                                          text="важный ответ", reply_to="mid.9")))
    assert left == 0
    assert any("Не смог записать ответ в DEV-70" in s for s in tr.sent)


def test_without_a_tracker_replies_are_ordinary_messages():
    """Инсталляция может не дать трекера вовсе — тогда развилки просто нет."""
    st, tr = Store(), Transport()
    ch = Channel("telegram", st, tr, None, "agent:queue:x", allowed={"1"},
                 agent_user_id="агент", progress_opts={"tick_sec": 0})
    remember(st, "telegram", 1, "mid.9")
    left = asyncio.run(ch.on_item(1, Item(type="text", source="telegram",
                                          text="ответ", reply_to="mid.9")))
    assert left == 1
