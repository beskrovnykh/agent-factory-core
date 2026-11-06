"""Расшифровка машинных слов (DEV-108) и ворота на новый код.

Главный тест здесь — не про `decorate`, а про полноту реестра: он читает
НАСТОЯЩИЕ источники машинных слов (контракты ролей, исходы сессии, состояния
задачи) и падает, если хоть одно из них доезжает до человека без человеческой
фразы. Дополнение владельца просило именно ворот, а не договорённости:
«появился новый код — расшифровка появляется вместе с ним, иначе он не
доезжает до человека вообще».
"""
import re
from pathlib import Path

import gatelog
import glossary
import prompt
import runner

SRC = Path(__file__).resolve().parent

# Классы вердиктов так, как они написаны в контрактах: в обратных кавычках.
_IN_CONTRACT = re.compile(
    r"`((?:OK|FIXED|PARTIAL|FAIL)(?:/[A-Za-z]+)?(?: НЕЧЕГО)?)`")


# Контракты лежат по ролям в `roles/` (DEV-13), а `prompt.py` остался фасадом.
# Читаются ВСЕ файлы роли, а не один: иначе класс, названный только в контракте
# проверки, ускользал бы от ворот — и ровно это тут однажды и случилось, когда
# контракты переехали, а тест продолжал смотреть в опустевший `prompt.py`.
def _contract_sources() -> list[Path]:
    return [SRC / "prompt.py", SRC / "headline.py"] + sorted(
        (SRC / "roles").glob("*.py"))


def _codes_in_contracts() -> set[str]:
    out: set[str] = set()
    for src in _contract_sources():
        out |= set(_IN_CONTRACT.findall(src.read_text(encoding="utf-8")))
    return out


# ── ворота ───────────────────────────────────────────────────────────────────

def test_every_verdict_class_written_in_the_contracts_has_a_human_line():
    """Ни один класс вердикта не доезжает до человека голым.

    Источник — сам текст контрактов, а не список, переписанный сюда руками:
    переписанный список разошёлся бы с контрактом ровно тогда, когда в
    контракте появился бы новый класс, то есть ровно в тот момент, ради
    которого тест и написан.
    """
    assert glossary.unexplained(_codes_in_contracts()) == []


def test_a_new_class_without_a_line_does_not_pass():
    """Сами ворота: выдуманный класс, которого нет в реестре, роняет проверку.

    Без этого предыдущий тест проходил бы и на пустом реестре, если бы
    контракты вдруг перестали называть классы (`_IN_CONTRACT` нашёл бы ноль
    совпадений, а `unexplained([])` пусто).
    """
    assert glossary.unexplained(_codes_in_contracts() | {"FAIL/выдумка"}) \
        == ["FAIL/выдумка"]
    # И источник не пуст — иначе ворота стерегли бы пустоту.
    assert len(_codes_in_contracts()) >= 6


def test_every_session_outcome_has_a_human_line():
    """Исходы сессии (`runner.OUTCOMES`) человек видит в задаче и в сводке."""
    assert glossary.unexplained(runner.OUTCOMES) == []


def test_every_task_state_and_label_has_a_human_line():
    """Состояния задачи и метка непроверенного — тоже машинные слова."""
    states = {prompt.DEFAULT_PROGRESS_STATE, prompt.DEFAULT_REVIEW_STATE,
              prompt.DEFAULT_UNVERIFIED_STATE, prompt.DEFAULT_VERIFYING_STATE,
              prompt.DEFAULT_DONE_STATE, prompt.DEFAULT_UNVERIFIABLE_LABEL}
    assert glossary.unexplained(states) == []


def test_every_tool_with_its_own_return_codes_has_a_human_line():
    """Инструмент, у которого ненулевой код значит «находка», а не поломку."""
    assert glossary.unexplained(gatelog.CODES) == []


# ── разметка ─────────────────────────────────────────────────────────────────

def test_the_code_stays_and_the_human_line_stands_next_to_it():
    """Код НЕ выкидывается: его читает следующая сессия, фраза — человек."""
    out = glossary.decorate("Вердикт `FAIL/implementation`.")
    assert "`FAIL/implementation`" in out
    assert "настоящий дефект в коде" in out


def test_only_the_first_occurrence_is_decorated():
    """Десять одинаковых скобок в длинном разборе — помеха, а не помощь."""
    out = glossary.decorate("`OK` " * 5)
    assert out.count("проверка прошла") == 1


def test_an_already_explained_code_is_left_alone():
    """Сессия написала фразу сама — второй копии не будет.

    Без этого комментарий, собранный из другого комментария (лента из
    вердиктов), копил бы скобки на каждом круге.
    """
    text = "`FAIL/implementation` — настоящий дефект в коде, чинит агент"
    assert glossary.decorate(text) == text


def test_a_command_inside_a_code_block_is_not_touched():
    """Скобка посреди команды ломает саму команду."""
    text = "```\ngrep OK verdict.txt && echo `PARTIAL`\n```"
    assert glossary.decorate(text) == text
    assert glossary.codes_in(text) == []


def test_a_verdict_word_inside_prose_is_not_decorated():
    """`FAIL` без кавычек внутри фразы — слово, а не вердикт."""
    assert glossary.decorate("ложный FAIL стоит одного круга") \
        == "ложный FAIL стоит одного круга"


def test_a_long_code_wins_over_a_short_one():
    """`FAIL/implementation` обязан получить свою фразу, а не фразу `FAIL`."""
    out = glossary.decorate("`FAIL/unobservable`")
    assert "наблюдать было негде" in out
    assert "проверка не подтвердила работу" not in out


def test_a_bare_outcome_word_is_decorated():
    """`READY_FOR_HUMAN` в прозе не встречается — узнаётся и без кавычек."""
    assert "решение за человеком" in glossary.decorate("исход READY_FOR_HUMAN")


# ── линт ─────────────────────────────────────────────────────────────────────

def test_a_bare_code_in_a_text_is_a_miss_naming_the_line():
    misses = glossary.lint_text("итог: `FAIL/specification`", "handoff.md")
    assert len(misses) == 1
    assert "FAIL/specification" in misses[0]
    assert "задача слишком крупная" in misses[0]


def test_a_code_with_its_line_nearby_is_not_a_miss():
    assert glossary.lint_text(
        "итог: `FAIL/specification` — задача слишком крупная или поставлена "
        "неточно — резать или уточнять, чинить нечего", "handoff.md") == []


# ── кто что читает ───────────────────────────────────────────────────────────

def test_the_readers_table_names_extractors_that_still_exist():
    """Таблица «что чем читается» протухает молча — поэтому её сверяют.

    Каждое имя во третьем столбце обязано разрешаться в живой вызов: иначе
    таблица обещает человеку, что поле кем-то читается, когда читателя уже
    нет, и «почистить» это поле будет выглядеть безопасным.
    """
    import importlib
    for what, who, how in glossary.READERS:
        assert who in ("человек", "машина", "оба"), what
        if not how:
            continue
        mod, _, name = how.rpartition(".")
        assert callable(getattr(importlib.import_module(mod), name)), how


def test_the_table_rendered_for_the_contract_names_both_readers():
    block = glossary.readers_block()
    assert "человек" in block and "машина" in block and "оба" in block


# ── адрес машинной части ─────────────────────────────────────────────────────

def test_the_machine_part_is_named_by_address_not_pasted(tmp_path):
    """Машинное не удалено и не вставлено телом — названо, где лежит."""
    (tmp_path / "expected.md").write_text("СОДЕРЖИМОЕ-ФАЙЛА")
    (tmp_path / "gates.json").write_text("[]")
    out = glossary.machine_part(tmp_path)
    assert "expected.md" in out and "gates.json" in out
    assert str(tmp_path) in out
    # Содержимого файлов в блоке нет — это адрес, а не копия.
    assert "СОДЕРЖИМОЕ-ФАЙЛА" not in out


def test_an_artifact_that_does_not_exist_is_not_promised(tmp_path):
    (tmp_path / "handoff.md").write_text("x")
    out = glossary.machine_part(tmp_path)
    assert "handoff.md" in out and "gates.json" not in out


def test_an_empty_workdir_gets_no_block(tmp_path):
    assert glossary.machine_part(tmp_path) == ""
    assert glossary.machine_part(None) == ""


# ── дверь, через которую раннер кладёт текст человеку ────────────────────────

def test_every_comment_the_runner_posts_goes_out_explained(monkeypatch):
    """Правило держится дверью, а не памятью того, кто пишет следующий текст.

    `linear_comment` — единственное место, через которое раннер кладёт
    комментарий в задачу: досдача за молчаливую сессию, отказ «ревью не
    состоялось», сам текст ревью. Разметка стоит там, а не в каждом из них
    по отдельности, — иначе новый комментарий уехал бы голым, и заметить это
    было бы нечем.
    """
    sent = []

    class Fake:
        def post_comment(self, issue_id, body):
            sent.append(body)
            return True

    monkeypatch.setattr(runner, "tracker_for", lambda persona: Fake())
    runner.linear_comment("issue-1", "Проверка вернула `FAIL/implementation`.")
    assert "`FAIL/implementation`" in sent[0]         # код на месте
    assert "настоящий дефект в коде" in sent[0]       # и фраза рядом
