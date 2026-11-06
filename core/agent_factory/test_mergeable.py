"""Вливается ли ветка: три ответа, и «не знаем» не равно «не вливается».

Случай задачи (DEV-109) слово в слово: 15.09.2026 в стопке «ждёт мержа»
лежали DEV-38, DEV-66 и DEV-13, у всех `mergeable=CONFLICTING`, и кнопка
ответила отказом. Здесь проверяется, что этот ответ хостинга доходит до кода
неискажённым, а всё остальное («ещё считаю», «не достучался», «PR нет»)
читается как незнание, а не как конфликт.
"""
import asyncio
import logging

from agent_factory import mergeable


def run(coro):
    return asyncio.run(coro)


class Gh:
    """Хостинг: что он отвечает про открытый PR по задаче."""

    def __init__(self, answers, boom=False):
        # Список ответов по порядку вопросов: так видно переспрос.
        self.answers = list(answers)
        self.boom = boom
        self.asked: list[str] = []

    def open_request(self, ident):
        self.asked.append(ident)
        if self.boom:
            raise RuntimeError("gh: HTTP 502")
        return self.answers[min(len(self.asked), len(self.answers)) - 1]


def pr(mergeable_flag, number="23"):
    return {"number": number, "repo": "beskrovnykh/agent-factory",
            "url": f"https://github.com/beskrovnykh/agent-factory/pull/{number}",
            "source_ref": "agent/DEV-38", "target_ref": "main",
            "title": "…", "mergeable": mergeable_flag}


async def nosleep(_sec):
    return None


def test_a_conflicting_pull_request_answers_no():
    gh = Gh([pr(False)])
    assert run(mergeable.ask(gh, "DEV-38", sleep=nosleep))[0] is False
    assert gh.asked == ["DEV-38"]


def test_a_mergeable_pull_request_answers_yes_and_is_not_asked_twice():
    gh = Gh([pr(True)])
    answer, request = run(mergeable.ask(gh, "DEV-38", sleep=nosleep))
    assert answer is True and request["number"] == "23"
    # Окончательный ответ переспрашивать незачем: лишний вопрос — лишний
    # поход в сеть на каждую задачу стопки.
    assert gh.asked == ["DEV-38"]


def test_while_the_hosting_is_still_counting_we_ask_again():
    # GitHub считает `mergeable` лениво: мерж в main сбрасывает поле у всех
    # открытых PR. Спросив один раз сразу после мержа, получишь «не знаю» по
    # всей стопке — и самый нужный круг не сделает ничего.
    gh = Gh([pr(None), pr(None), pr(False)])
    assert run(mergeable.ask(gh, "DEV-66", sleep=nosleep))[0] is False
    assert gh.asked == ["DEV-66"] * 3


def test_a_hosting_that_never_decides_is_unknown_not_conflict(caplog):
    gh = Gh([pr(None)])
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(mergeable.ask(gh, "DEV-13", sleep=nosleep))[0] is None
    assert "не сказал" in " | ".join(r.getMessage() for r in caplog.records)


def test_a_broken_hosting_is_unknown_not_conflict():
    # Протухший токен и упавшая сеть выглядят одинаково; увести по ним
    # задачу из стопки значит завести сессию на ровном месте.
    assert run(mergeable.ask(Gh([pr(False)], boom=True), "DEV-38"))[0] is None


def test_no_open_request_is_unknown_and_is_not_asked_again():
    # Открытого PR нет — мержа никто и не ждёт; конфликтной такую задачу
    # объявлять не за что. И переспрашивать нечего: «PR нет» — окончательный
    # ответ, а не «хостинг ещё считает».
    gh = Gh([None])
    assert run(mergeable.ask(gh, "DEV-81", sleep=nosleep))[0] is None
    assert gh.asked == ["DEV-81"]


def test_without_a_hosting_the_answer_is_unknown():
    assert run(mergeable.ask(None, "DEV-38"))[0] is None
    assert run(mergeable.ask(object(), "DEV-38"))[0] is None


def test_the_reason_names_the_pull_request_and_the_branch():
    why = mergeable.why_not("DEV-38", pr(False, number="22"))
    assert "PR #22" in why and "agent/DEV-38" in why and "main" in why
    # Без PR причина всё равно называет ветку по соглашению: человек должен
    # понимать, о чём речь, даже когда хостинг ответил скупо.
    assert "agent/DEV-38" in mergeable.why_not("DEV-38", None)


def test_a_broken_tries_knob_is_the_default_not_a_crash(caplog):
    with caplog.at_level(logging.WARNING, logger="intake"):
        assert mergeable._int_from("три", mergeable.DEFAULT_TRIES) == mergeable.DEFAULT_TRIES
    assert mergeable._int_from("", 3) == 3 and mergeable._int_from("1", 3) == 1


def test_a_non_boolean_answer_is_unknown_not_conflict():
    # Протокол `Hosting` реализует и инсталляция: её ответ может прийти
    # числом. `0 in (True, False)` — истина, и такой ответ прочитался бы как
    # «конфликт», а сравнение `is False` дальше — как «вливается»: задача
    # молча осталась бы в стопке. Незнакомая форма — «не знаем».
    assert mergeable.answer_of({"mergeable": 0}) is None
    assert mergeable.answer_of({"mergeable": 1}) is None
    assert mergeable.answer_of({"mergeable": "CONFLICTING"}) is None
    assert mergeable.answer_of({"mergeable": False}) is False
    assert mergeable.answer_of({"mergeable": True}) is True
