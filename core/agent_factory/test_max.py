"""Тесты транспорта MAX. Каждое отличие от телеграмного стоило живой отладки."""
import asyncio

from agent_factory.intake import Button, Intake, control_key
from agent_factory.max_messenger import (
    MaxTransport, chat_id_of, item_from_message, update_fingerprint, user_id_of)


# ── разбор ───────────────────────────────────────────────────────────────

def test_plain_message():
    it = item_from_message({"body": {"text": " почини экспорт "}})
    assert it and it.type == "text" and it.source == "max"
    assert it.text == "почини экспорт"


def test_forwarded_message_content_lives_in_the_link():
    """У пересланного тело ПУСТОЕ. Калька телеграмного разбора возвращала
    ноль вложений молча, и человек видел «файлов не было»."""
    it = item_from_message({
        "body": {"text": "", "attachments": []},
        "link": {"type": "forward",
                 "sender": {"first_name": "Иван", "last_name": "И"},
                 "message": {"text": "смотри что упало",
                             "attachments": [{"type": "file", "filename": "лог.txt",
                                              "size": 120,
                                              "payload": {"url": "https://x/1"}}]}}})
    assert it.type == "document" and it.file_name == "лог.txt"
    assert it.frm == "Иван И", "происхождение — половина постановки"
    assert it.text == "смотри что упало"


def test_image_gets_a_name_even_though_max_has_none():
    it = item_from_message({"body": {"text": "", "attachments": [
        {"type": "image", "size": 900, "payload": {"url": "https://x/2",
                                                   "photo_id": 77}}]}})
    assert it.type == "photo" and it.file_name.endswith(".webp")
    assert it.file_url == "https://x/2"


def test_attachment_without_a_url_is_not_taken():
    """Скачивать нечего: кусок без ссылки — это обещание фактуры, которой нет."""
    assert item_from_message({"body": {"text": "", "attachments": [
        {"type": "file", "filename": "x"}]}}) is None


def test_empty_message_is_not_collected():
    assert item_from_message({"body": {"text": "   "}}) is None


# ── адресация события ────────────────────────────────────────────────────

def test_a_click_carries_no_message_so_chat_comes_from_the_update():
    """Проверено на живом: у callback ровно четыре ключа, сообщения внутри
    НЕТ. Искали chat_id там — и молча выходили с пустой пачкой чужого чата."""
    up = {"update_type": "message_callback", "chat_id": 42,
          "callback": {"callback_id": "cb1", "payload": "intake:collect",
                       "user": {"user_id": 7}}}
    assert chat_id_of(up) == "42" and user_id_of(up) == "7"


def test_a_message_carries_its_own_chat_and_sender():
    up = {"update_type": "message_created",
          "message": {"recipient": {"chat_id": 9}, "sender": {"user_id": 7}}}
    assert chat_id_of(up) == "9" and user_id_of(up) == "7"


def test_fingerprint_separates_retries_from_new_events():
    """Доставка ретраится до десяти раз: без отпечатка одно нажатие завело бы
    несколько сессий — сожжённые слоты и дубли задач."""
    a = {"update_type": "message_callback", "timestamp": 1,
         "callback": {"callback_id": "cb1"}}
    assert update_fingerprint(a) == update_fingerprint(dict(a))
    b = {**a, "callback": {"callback_id": "cb2"}}
    assert update_fingerprint(a) != update_fingerprint(b)


# ── клавиатура ───────────────────────────────────────────────────────────

def test_keyboard_is_an_attachment_not_a_field():
    kb = MaxTransport._keyboard([Button("Собрать задачу", "intake:collect")])
    assert kb["type"] == "inline_keyboard"
    assert kb["payload"]["buttons"][0][0]["payload"] == "intake:collect"


def test_no_buttons_means_no_attachment():
    assert MaxTransport._keyboard(None) is None


def test_edit_refuses_an_empty_message_id():
    """Пустой mid уходил в запрос и возвращался «Missing required parameter»:
    правка молча не применялась, счётчик застревал."""
    t = MaxTransport("токен")
    sent = []
    t._call = lambda *a, **k: sent.append(a) or {}
    assert asyncio.run(t.edit(1, "", "текст")) is False
    assert sent == []


# ── счётчик на транспорте без удаления ───────────────────────────────────

class Store:
    def __init__(self):
        self.lists, self.vals = {}, {}

    async def append(self, key, value):
        self.lists.setdefault(key, []).append(value); return len(self.lists[key])

    async def read_all(self, key): return list(self.lists.get(key) or [])
    async def drop(self, key): self.lists.pop(key, None); self.vals.pop(key, None)
    async def get(self, key): return self.vals.get(key)
    async def put(self, key, value, ttl): self.vals[key] = value
    async def expire(self, key, ttl): pass
    async def enqueue(self, queue, element): await self.append(queue, element)


class NoDeleteTransport:
    """Как MAX: править умеет, удалять — нет."""

    def __init__(self):
        self.sent, self.edited = [], []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append(text); return f"mid.{len(self.sent)}"

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((message_id, text)); return True

    async def answer(self, callback_id, notice=""): pass
    async def delete(self, chat_id, message_id): return False


def test_counter_is_edited_where_deleting_is_impossible():
    """Слать новое, не убрав старое, — это ровно тот случай, из-за которого
    человек получал три счётчика с разными числами."""
    st, tr = Store(), NoDeleteTransport()
    i = Intake(source="max", store=st, transport=tr, queue_key="q")

    async def go():
        from agent_factory.intake import Item
        await i.add(1, Item(type="text", source="max", text="раз"))
        await i.add(1, Item(type="text", source="max", text="два"))
        await i.add(1, Item(type="text", source="max", text="три"))

    asyncio.run(go())
    assert len(tr.sent) == 1, "счётчик создаётся один раз"
    assert len(tr.edited) == 2
    assert st.vals[control_key("max", 1)] == "mid.1"


def test_mid_is_found_wherever_max_put_it():
    """Пустой mid уходил в правку и возвращался «Missing required parameter»:
    счётчик застревал на прежнем числе."""
    from agent_factory.max_messenger import message_id_of
    assert message_id_of({"message": {"body": {"mid": "m1"}}}) == "m1"
    assert message_id_of({"message": {"mid": "m2"}}) == "m2"
    assert message_id_of({"callback": {"message": {"body": {"mid": "m3"}}}}) == "m3"
    assert message_id_of({"callback": {}}) == ""


def test_the_clicker_is_found_even_without_a_user_object():
    """08.09: владелец жал кнопку восемь раз, проверка получала None и
    отвечала «Недоступно»."""
    assert user_id_of({"message": {"sender": {"user_id": 5}}}) == "5"
    assert user_id_of({"callback": {"user": {"user_id": 7}},
                       "message": {"sender": {"user_id": 5}}}) == "7"
