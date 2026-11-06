"""Пошла ли работа в дело.

Токены и минуты считает счётчик. Не считает он главного — была ли от работы
польза, и без этого статистика отвечает «сколько потратили», молча про «зачем».
Половина тестов здесь про то, чтобы цифра не начала врать: промах постановки
нельзя записывать агенту в брак.
"""
import asyncio
import json

from agent_factory.verdicts import KEY, MARKS, Verdicts, counted


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


def run(c):
    return asyncio.run(c)


def test_the_scale_has_four_outcomes():
    """Три из четырёх машина не знает в принципе — их ставит человек."""
    assert [k for k, *_ in MARKS] == ["ок", "ошибки", "постановка", "мимо"]


def test_a_mark_is_stored_and_read_back():
    st = Store()
    v = Verdicts(st)
    assert run(v.put("DEV-20-2026", "ок")) is True
    assert run(v.all())["DEV-20-2026"]["оценка"] == "ок"


def test_an_empty_mark_removes_it():
    st = Store()
    v = Verdicts(st)
    run(v.put("DEV-20-2026", "ок"))
    run(v.put("DEV-20-2026", ""))
    assert run(v.all()) == {}


def test_a_mark_outside_the_scale_is_refused():
    st = Store()
    v = Verdicts(st)
    assert run(v.put("DEV-20-2026", "супер")) is False
    assert run(v.all()) == {}


def test_a_silent_store_does_not_pretend_it_saved():
    st = Store(boom=True)
    assert run(Verdicts(st).put("DEV-20-2026", "ок")) is False


def test_broken_storage_shows_empty_not_crashes():
    st = Store()
    st.kv[KEY] = "не json"
    assert run(Verdicts(st).all()) == {}


# ── арифметика, ради которой всё и затевалось ────────────────────────────

def test_our_own_misses_do_not_count_against_the_agent():
    """Иначе «качество агента» падает от того, что мы криво формулируем, и
    цифра перестаёт о чём-либо говорить."""
    got = counted({"a": {"оценка": "ок"}, "b": {"оценка": "ок"},
                   "c": {"оценка": "постановка"}, "d": {"оценка": "мимо"}})
    assert got["толк"] == 100          # у агента два из двух
    assert got["впустую"] == 50        # половина работы не стоила начала


def test_errors_do_count_against_the_agent():
    got = counted({"a": {"оценка": "ок"}, "b": {"оценка": "ошибки"}})
    assert got["толк"] == 50 and got["впустую"] == 0


def test_unjudged_sessions_are_neither_success_nor_failure():
    """Неоценённая работа — это «ещё не смотрели». Записывать её в провал так
    же нечестно, как в успех."""
    got = counted({"a": {"оценка": "ок"}, "b": {}, "c": {"оценка": None}})
    assert got["оценено"] == 1 and got["толк"] == 100


def test_nothing_judged_is_not_a_division_by_zero():
    got = counted({})
    assert got["толк"] == 0 and got["впустую"] == 0 and got["оценено"] == 0


def test_everything_ours_leaves_the_agent_unscored():
    got = counted({"a": {"оценка": "постановка"}, "b": {"оценка": "мимо"}})
    assert got["толк"] == 0 and got["впустую"] == 100
