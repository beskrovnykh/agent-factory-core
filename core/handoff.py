"""Схема сдачи работы (DEV-95): что сессия обязана оставить на границе.

До этого сдача была прозой комментария: раннер проверял только, что
комментарий есть и задача переведена, а чего в комментарии нет — не видел
никто. Проверяющему при этом велено писать `expected.md` ДО чтения заявки
(DEV-87), то есть требование стояло на выходе, а на границе от сдающего не
требовалось ничего.

Схема — файл `handoff.md` в рабочем каталоге с четырьмя разделами:

    ## Цель            что заказано, как понято
    ## Формат выхода   что именно сдаётся и где лежит
    ## Чем наблюдать   место, команда, ожидаемое значение
    ## Границы         чего не сделано и что не проверено

Тот же текст сессия кладёт комментарием в задачу; раннер сверяет файл
здесь, а комментарий и состояние — по снимку трекера (`runner.handoff_misses`).
Не сошлось — сессии возвращают список недостающего и продолжают её же
(`--resume`), а не дописывают за неё; потолок неудачных сдач —
`runner.MAX_HANDOFF_ATTEMPTS`, дальше задача уходит человеку.

Здесь — только файл: чистые функции без трекера и без окружения, чтобы их
гонять и по записанным каталогам.
"""
from __future__ import annotations

import re
from pathlib import Path

HANDOFF_FILE = "handoff.md"
# Слова разделов — ровно те, что в контракте (`prompt.TEMPLATE`, шаг 11) и в
# сообщении о промахе: сессия должна суметь исправить по одному сообщению.
SECTIONS = ("Цель", "Формат выхода", "Чем наблюдать", "Границы")

MISS_NO_FILE = f"нет файла `{HANDOFF_FILE}`"
MISS_EMPTY_FILE = f"`{HANDOFF_FILE}` пуст"

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$")
_DECOR = re.compile(r"[*_`:.]+")


def key_of(title: str) -> str:
    """Имя раздела для сравнения: без разметки, двоеточий и регистра.

    Публичное, потому что читателей схемы стало двое: сдача и план шлюза
    (`plan.py`). Два разбора заголовков разъехались бы в первый же раз,
    когда один из них научится терпеть лишний символ, а другой нет."""
    return " ".join(_DECOR.sub(" ", title).split()).casefold()


def sections_of(text: str) -> dict[str, str]:
    """Разделы файла по заголовкам любого уровня: ключ — нормализованное имя,
    значение — тело до следующего заголовка. Разделы вне схемы не мешают —
    сессия вправе добавить «## Код-ревью» или что угодно ещё."""
    out: dict[str, str] = {}
    current = ""
    body: list[str] = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            if current:
                out[current] = "\n".join(body).strip()
            current, body = key_of(m.group(1)), []
            continue
        body.append(line)
    if current:
        out[current] = "\n".join(body).strip()
    return out


def lint_text(text: str) -> list[str]:
    """Чего не хватает в тексте сдачи. Пусто — по схеме."""
    if not text.strip():
        return [MISS_EMPTY_FILE]
    found = sections_of(text)
    misses: list[str] = []
    for name in SECTIONS:
        body = found.get(key_of(name))
        if body is None:
            misses.append(f"в `{HANDOFF_FILE}` нет раздела «{name}»")
        elif not body:
            misses.append(f"в `{HANDOFF_FILE}` раздел «{name}» пуст")
    return misses


def lint_file(workdir: Path) -> list[str]:
    """То же по файлу в рабочем каталоге. Нет файла — один промах, а не
    четыре: сессии нужно одно действие, а не список."""
    try:
        text = (workdir / HANDOFF_FILE).read_text(errors="replace")
    except OSError:
        return [MISS_NO_FILE]
    return lint_text(text)


def handoff_of(workdir: Path) -> tuple[str, list[str]]:
    """(текст сдачи, промахи). Текст возвращается и при промахах — человеку
    на потолке показывают то, что сессия всё-таки написала."""
    try:
        text = (workdir / HANDOFF_FILE).read_text(errors="replace")
    except OSError:
        return "", [MISS_NO_FILE]
    return text.strip(), lint_text(text)


def schema_block() -> str:
    """Схема словами — одна на контракт и на сообщение о промахе, чтобы
    сессия видела в возврате ровно то, что читала в постановке."""
    return "\n".join(f"## {name}" for name in SECTIONS)
