"""Схема сдачи — файл (DEV-95).

Четыре раздела с ровно теми словами, что в контракте; раннер читает файл,
а не рассказ сессии. Тесты держат разбор: что считается разделом, что —
промахом, и что сообщение о промахе называет раздел теми же словами,
которыми его требует контракт, — сессия чинит по одному сообщению.
"""
from pathlib import Path

import handoff
from prompt import HANDOFF_SCHEMA, REPAIR_TEMPLATE, TEMPLATE

GOOD = """\
## Цель
Экспорт отчётов не падает на пустом периоде.

## Формат выхода
PR !195 в backend, ветка agent/TASK-200. Ревью: 2 находки, обе починены.

## Чем наблюдать
`curl -s $API/export?from=2026-09-01&to=2026-09-01` → 200 и пустой CSV.

## Границы
Пайплайн на момент сдачи ещё шёл. Кэш не трогал.
"""


def test_a_file_by_the_schema_has_no_misses(tmp_path):
    (tmp_path / "handoff.md").write_text(GOOD)
    assert handoff.lint_file(tmp_path) == []
    text, misses = handoff.handoff_of(tmp_path)
    assert misses == [] and text.startswith("## Цель")


def test_no_file_is_one_miss_not_four(tmp_path):
    """Сессии нужно одно действие — записать файл, — а не список из
    четырёх «нет раздела»."""
    assert handoff.lint_file(tmp_path) == [handoff.MISS_NO_FILE]
    assert handoff.handoff_of(tmp_path) == ("", [handoff.MISS_NO_FILE])


def test_an_empty_file_is_named_as_empty(tmp_path):
    (tmp_path / "handoff.md").write_text("  \n\n")
    assert handoff.lint_file(tmp_path) == [handoff.MISS_EMPTY_FILE]


def test_a_missing_section_is_named_by_the_contract_word():
    misses = handoff.lint_text(GOOD.replace("## Чем наблюдать\n`curl", "## Как смотреть\n`curl"))
    assert misses == ["в `handoff.md` нет раздела «Чем наблюдать»"]


def test_an_empty_section_is_a_miss_too():
    """Заголовок без тела — это форма без содержания: «## Границы» и ничего
    под ним не говорит, что границ нет, — говорит, что про них не думали."""
    misses = handoff.lint_text(GOOD.replace("Пайплайн на момент сдачи ещё шёл. Кэш не трогал.\n", ""))
    assert misses == ["в `handoff.md` раздел «Границы» пуст"]


def test_heading_level_case_and_decoration_do_not_matter():
    """Модель пишет «### Цель:» и «## **Границы**» — это те же разделы.
    Строгость нужна словам, не разметке."""
    text = (GOOD.replace("## Цель", "### Цель:")
                .replace("## Границы", "## **Границы**")
                .replace("## Чем наблюдать", "# чем НАБЛЮДАТЬ"))
    assert handoff.lint_text(text) == []


def test_extra_sections_and_any_order_are_fine():
    text = ("## Код-ревью\nбез находок\n"
            "## Границы\nнет\n## Чем наблюдать\ncurl\n"
            "## Формат выхода\nPR\n## Цель\nx\n")
    assert handoff.lint_text(text) == []


def test_a_word_inside_a_paragraph_is_not_a_heading():
    """«Цель — …» в теле «Формата выхода» не должна закрыть раздел раньше
    времени и объявить сам «Формат выхода» пустым."""
    text = GOOD.replace("## Формат выхода\nPR !195", "## Формат выхода\nЦель: PR !195")
    assert handoff.lint_text(text) == []



