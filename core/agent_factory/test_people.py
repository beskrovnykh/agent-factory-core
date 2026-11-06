"""Карта «человек в трекере ↔ чат в мессенджере».

Одна карта на всё. Половина тестов — про то, что незнакомцу не пишем и
кнопок не даём: кнопки под сообщением задачи пишут в трекер под общим ключом
инсталляции, и пустивший чужого отдаёт ему чужие задачи.
"""
import asyncio

from agent_factory.people import People, creator_id


class Store:
    def __init__(self):
        self.kv = {}
        self.fails = False

    async def put(self, key, value, ttl):
        if self.fails:
            raise RuntimeError("redis молчит")
        self.kv[key] = value

    async def get(self, key):
        if self.fails:
            raise RuntimeError("redis молчит")
        return self.kv.get(key)


def mk(store=None):
    return People({"u-andrey": 1000000002, "u-pavel": "777"},
                  owner_user_id="u-andrey", store=store)


# ── перевод в обе стороны ────────────────────────────────────────────────

def test_a_person_resolves_to_their_chat():
    assert mk().chat_of("u-pavel") == "777"


def test_a_chat_resolves_back_to_the_person():
    # Обратной карты не заводим: второй источник правды о тех же людях
    # расходится с первым молча.
    assert mk().user_of("777") == "u-pavel"


def test_numbers_and_strings_are_the_same_id():
    # В трекере id строковые, в Telegram числовые. Без приведения сравнение
    # «1000000002 == "1000000002"» молча ложно, и владелец перестаёт быть собой.
    p = mk()
    assert p.chat_of("u-andrey") == "1000000002"
    assert p.user_of(1000000002) == "u-andrey"


def test_a_stranger_resolves_to_nothing():
    p = mk()
    assert p.chat_of("u-кто-то") == "" and p.user_of("999") == ""


# ── доступ ───────────────────────────────────────────────────────────────

def test_a_person_from_the_map_may_press_buttons():
    assert mk().knows("777")


def test_a_stranger_may_not_press_buttons():
    # Чат групповой: без проверки любой участник закрывает любую задачу.
    assert not mk().knows("999")


def test_the_owner_is_recognised_by_their_chat():
    assert mk().is_owner(1000000002)


def test_a_colleague_is_not_the_owner():
    assert not mk().is_owner("777")


def test_without_a_map_nobody_is_the_owner():
    # Безопаснее, чем «команды работают у всех».
    p = People({}, owner_user_id="u-andrey")
    assert not p.is_owner("1000000002")
    # И отдельно — нажатие без отправителя. Без явной проверки «чат владельца
    # известен» пустое сравнивалось бы с пустым, и владельцем становился бы
    # кто угодно, у кого в событии не оказалось id.
    assert not p.is_owner("") and not p.is_owner(None)


def test_without_an_owner_nobody_is_the_owner():
    assert not People({"u-andrey": "1"}).is_owner("1")


# ── разбор карты ─────────────────────────────────────────────────────────

def test_a_map_comes_from_json():
    assert People.from_json('{"u-1": 42}').chat_of("u-1") == "42"


def test_broken_json_does_not_stop_the_start():
    # Приём и сессии от карты не зависят; падать на старте из-за неё значит
    # обменять рабочую площадку на недоставленное сообщение.
    assert People.from_json("{не json").chat_by_user == {}


def test_json_that_is_not_an_object_is_refused():
    assert People.from_json("[1,2,3]").chat_by_user == {}


def test_an_empty_map_is_a_working_state():
    assert People.from_json("").chat_by_user == {}


# ── кто принёс задачу ────────────────────────────────────────────────────

def test_the_author_of_an_intake_issue_is_remembered():
    # Приёмная задача заводится под общим ключом инсталляции, поэтому автор у
    # всех таких задач один — владелец ключа. Без своей памяти возврат
    # закрытой задачи автору не работал бы ровно для приёма через личку.
    st = Store()
    p = mk(st)
    assert asyncio.run(p.remember_author("i1", "777")) == "u-pavel"
    assert asyncio.run(p.author_of("i1")) == "u-pavel"


def test_an_unknown_bringer_is_not_remembered():
    st = Store()
    p = mk(st)
    assert asyncio.run(p.remember_author("i1", "999")) == ""
    # Ничего не записали: пустой автор в хранилище выглядит как «помним», и
    # настоящего автора потом уже не спросят у трекера.
    assert st.kv == {}
    assert asyncio.run(p.author_of("i1")) == ""


def test_a_store_that_fails_does_not_break_the_intake():
    # Забытый автор — потеря удобства. Упавший приём — потеря задачи.
    st = Store()
    st.fails = True
    p = mk(st)
    assert asyncio.run(p.remember_author("i1", "777")) == ""
    assert asyncio.run(p.author_of("i1")) == ""


def test_without_a_store_the_author_is_simply_unknown():
    p = mk()
    assert asyncio.run(p.remember_author("i1", "777")) == ""
    assert asyncio.run(p.author_of("i1")) == ""


# ── автор из тела события ────────────────────────────────────────────────

def test_the_creator_comes_from_the_event():
    assert creator_id({"creatorId": "u-1"}) == "u-1"


def test_the_creator_may_come_as_an_object():
    assert creator_id({"creator": {"id": "u-2"}}) == "u-2"


def test_an_event_without_a_creator_says_so():
    assert creator_id({}) == ""
