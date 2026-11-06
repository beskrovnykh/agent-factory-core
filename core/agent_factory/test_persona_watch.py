"""Сторож личностей: отвалившийся ключ слышен за час, а не за сутки.

13.09.2026 четыре личности из пяти отвалились разом — приложениям отозвали
авторизацию. Ключи в окружении лежали как живые, трекер отвечал 401,
комментарии не создавались. Фабрика сутки писала в пустоту, и со стороны это
неотличимо от «сессия ничего не сделала». Узнали случайно.
"""
import asyncio
import logging

import pytest

import personas
from agent_factory.telegram import (PERSONA_KEY_PREFIX, TelegramIntake, dead_personas,
                                    _persona_keys_from_env)


def test_a_revoked_key_is_reported_dead():
    """Отозванный токен выглядит ровно как живой: отличить можно только
    запросом. «Ключ непустой» здесь не проверка."""
    def whoami(key):
        if key == "мёртвый":
            raise RuntimeError("401")
        return "Имя"
    assert dead_personas({"qa": "живой", "duty": "мёртвый"}, whoami) == ["duty"]


def test_an_empty_answer_counts_as_dead():
    """Трекер может ответить 200 и пустотой — подписаться этим всё равно не
    выйдет."""
    assert dead_personas({"duty": "k"}, lambda key: None) == ["duty"]


def test_one_dead_persona_does_not_hide_the_others():
    """Список, а не исключение: иначе сторож слепнет на первой же мёртвой."""
    def whoami(key):
        if key.startswith("м"):
            raise RuntimeError("401")
        return "Имя"
    assert dead_personas({"a": "мёртвый", "b": "живой", "c": "мёртвый2"}, whoami) == ["a", "c"]


def test_a_broken_watch_is_not_a_dead_persona():
    """Опечатка в самом стороже — не «личность умерла».

    14.09.2026 сторож звал несуществующее поле, падал с `AttributeError` за
    четыре миллисекунды — раньше, чем успел бы уйти хоть один запрос, — и
    широкий `except` объявил мёртвыми ВСЕХ пятерых. В чат ушла тревога о
    поломке, которой не было. Ошибка кода обязана падать наверх, а не
    превращаться в ложный сигнал."""
    def сломан(key):
        raise AttributeError("нет такого поля")
    with pytest.raises(AttributeError):
        dead_personas({"qa": "k"}, сломан)


def test_a_refusal_from_the_tracker_is_still_a_dead_persona():
    """А вот отказ трекера — именно то, что сторож и ищет: ловим и помечаем."""
    def отказ(key):
        raise RuntimeError("401 Unauthorized")
    assert dead_personas({"qa": "k"}, отказ) == ["qa"]


def test_all_alive_is_an_empty_list():
    assert dead_personas({"qa": "k"}, lambda key: "Имя") == []


# ── ключи из окружения ───────────────────────────────────────────────────────

def test_keys_are_taken_by_the_prefix_not_by_a_list():
    """Список личностей живёт в другом пакете, и копия здесь разъехалась бы с
    ним ровно так же, как разъезжались все прочие копии."""
    got = _persona_keys_from_env({"LINEAR_API_KEY_QA": "q", "LINEAR_API_KEY_DUTY": "d",
                                  "PATH": "/bin"})
    assert got == {"qa": "q", "duty": "d"}


def test_the_installation_key_is_not_a_persona():
    """`LINEAR_API_KEY` — ключ инсталляции, он не личность. Попади он в список,
    сторож ругался бы на него при каждой проверке."""
    assert _persona_keys_from_env({"LINEAR_API_KEY": "общий"}) == {}


def test_an_empty_value_is_not_a_persona():
    """Пустое значение — «не настроено», а не «личность с пустым ключом»."""
    assert _persona_keys_from_env({"LINEAR_API_KEY_DUTY": "  "}) == {}


def test_both_packages_agree_on_the_real_personas():
    """Приставки мало: разъехаться может СМЫСЛ. На одном окружении обе функции
    обязаны дать один набор для настоящих личностей — иначе раннер подпишется
    ключом, за которым никто не следит, или наоборот."""
    env = {personas.key_var(p): f"ключ-{p}" for p in personas.PERSONAS}
    env["LINEAR_API_KEY"] = "общий"
    assert _persona_keys_from_env(env) == personas.keys_from_env(env)


def test_an_unknown_key_is_watched_deliberately():
    """Переменная с приставкой, которой нет в списке личностей, — либо личность,
    о которой раннер не знает, либо опечатка. Оба случая надо увидеть, а не
    проглотить: проглотить — это ровно та поломка, ради которой сторож заведён."""
    got = _persona_keys_from_env({"LINEAR_API_KEY_НЕИЗВЕСТНАЯ": "k"})
    assert "неизвестная" in got


def test_the_prefix_matches_the_one_the_runner_reads():
    """Контракт между пакетами: раннер подписывается этими ключами, приём за
    ними присматривает. Разъедутся имена — сторож будет смотреть в пустоту и
    молчать, что хуже отсутствия сторожа."""
    assert PERSONA_KEY_PREFIX == personas.KEY_PREFIX


# ── настройки видны при запуске ──────────────────────────────────────────────

def test_the_skipped_labels_are_named_at_startup():
    """Ярлык «память» существует ровно чтобы тяга такие задачи НЕ брала —
    у них цена ошибки другая. Молча — значит непроверяемо: настройка, которую
    не видно при запуске, неотличима от настройки, которая не доехала."""
    from agent_factory.puller import Puller

    p = Puller(tracker=None, store=None, agent_user_id="a", team_id="t",
               skip_labels=("память",))
    assert "мимо ярлыков: память" in p.limits_line()


def test_without_skipped_labels_the_line_says_nothing_about_them():
    from agent_factory.puller import Puller

    p = Puller(tracker=None, store=None, agent_user_id="a", team_id="t")
    assert "ярлык" not in p.limits_line()


# ── сторож говорит человеку, а не в пустоту ──────────────────────────────────

class Транспорт:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text))


class Приём:
    """Ровно те две части приёма, которые нужны сторожу."""
    notify_chat = "-100"

    def __init__(self):
        self.transport = Транспорт()

    _say_personas = TelegramIntake._say_personas


def test_a_dead_persona_is_reported_to_the_channel():
    """Первая версия сторожа писала только `log.error` — то есть ровно туда,
    куда в тот раз никто и не посмотрел. Сигнал был бы, а поведение системы
    осталось бы прежним: узнали бы снова случайно."""
    a = Приём()
    asyncio.run(a._say_personas("⚠️ Личности не отвечают: duty", logging.ERROR))
    assert a.transport.sent and a.transport.sent[0][0] == "-100"
    assert "duty" in a.transport.sent[0][1]


def test_a_silent_channel_does_not_break_the_watch():
    """Не сказать в канал — плохо, но упасть из-за этого хуже: сторож перестанет
    проверять вовсе."""
    a = Приём()

    async def падает(chat_id, text, buttons=None):
        raise RuntimeError("телеграм молчит")
    a.transport.send = падает
    asyncio.run(a._say_personas("⚠️ что-то", logging.ERROR))   # не бросает
