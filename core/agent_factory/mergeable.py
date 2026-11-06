"""Вливается ли ветка задачи в основную — факт, а не надежда.

Стопка «ждёт мержа» обещает человеку одно: нажми — сольётся. 15.09.2026 в
ней лежали три задачи (DEV-38, DEV-66, DEV-13), и ни одна не слилась:
`mergeable=CONFLICTING`, `mergeStateStatus=DIRTY` — за день в `main` уехали
чужие правки тех же файлов. Ветка была сливаемой, когда её проверяли, и
перестала быть потом; узнал об этом человек, нажав кнопку.

Отвечает на вопрос ХОСТИНГ, а не git, и это выбор, а не удобство:

  • кнопка мержа спрашивает ровно его. Считать сливаемость самим значит
    держать второй ответ на тот же вопрос — и однажды сказать «сольётся»
    там, где `gh pr merge` откажет;
  • `git merge-tree --write-tree`, который отвечает кодом возврата, есть
    только с git 2.38, а на хосте раннера client-b стоит 2.34.1 (проверено
    16.09.2026). Разбирать вывод старого `merge-tree` на маркеры конфликта
    — гадание по тексту, который git волен переформатировать;
  • `Hosting.open_request` уже отдаёт это поле, и другого похода в сеть не
    нужно.

Три ответа, и путать их нельзя: ДА, НЕТ и НЕ ЗНАЕМ. «Не знаем» — хостинга
нет, он промолчал, PR по задаче не найден или сам хостинг ещё считает.
Уводить по нему задачу из стопки нельзя: молчание сети не делает работу
конфликтной.

**Почему «не знаем» бывает нормой сразу после мержа.** GitHub считает
`mergeable` лениво: мерж в `main` сбрасывает поле у всех открытых PR в
`UNKNOWN`, и пересчёт занимает секунды. Пересмотр, позванный по событию
мержа, спросит ровно в эту яму — поэтому здесь переспрос с паузой, а не
один вопрос. Без него самый нужный круг (сразу после мержа) отвечал бы «не
знаю» по всей стопке и не делал ничего.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os

log = logging.getLogger("intake")

# Сколько раз переспросить, пока хостинг считает, и сколько ждать между
# вопросами. Три попытки по пять секунд — это 10 секунд ожидания в худшем
# случае, и они окупаются: без них круг сразу после мержа слеп.
DEFAULT_TRIES = 3
DEFAULT_PAUSE_SEC = 5.0


def _int_from(raw, default: int) -> int:
    """Ручка окружения → число. Мусор — умолчание со строкой в журнале, а не
    падение при импорте: правило то же, что у `reconcile.every_from`."""
    if raw in (None, ""):
        return default
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        log.warning("значение %r — не число, беру %d", raw, default)
        return default


TRIES = _int_from(os.environ.get("AGENT_MERGEABLE_TRIES"), DEFAULT_TRIES)


def answer_of(request: dict | None) -> bool | None:
    """Ответ хостинга про открытый PR → вливается / нет / не знаем.

    `None` и на «PR не найден»: открытого PR по задаче нет — значит и мержа
    человек не ждёт, и объявлять такую задачу конфликтной не за что.

    Ответ признаётся, только если он РОВНО `True` или `False`. Проверка
    строгая (`is`), а не `in (True, False)`: у последней `0` и `1` проходят
    как булевы, и чужая реализация протокола, отдающая числа, вернула бы
    `0` — то есть «конфликт», который дальше сравнивается `is False` и
    молча читается как «вливается». Незнакомая форма ответа — «не знаем».
    """
    if not request:
        return None
    got = request.get("mergeable")
    return got if got is True or got is False else None


def why_not(ident: str, request: dict | None) -> str:
    """Причина словами — в комментарий задачи и в журнал. Номер PR и ветку
    называем: человек по ним найдёт, что именно не слилось."""
    number = str((request or {}).get("number") or "")
    source = str((request or {}).get("source_ref") or f"agent/{ident}")
    target = str((request or {}).get("target_ref") or "основную ветку")
    where = f"PR #{number}" if number else "открытый PR"
    return (f"{where} (`{source}` → `{target}`) не вливается: хостинг отвечает "
            f"«конфликт»")


async def ask(hosting, ident: str, *, tries: int = 0, pause: float = DEFAULT_PAUSE_SEC,
              sleep=None) -> tuple[bool | None, dict]:
    """Спросить хостинг: вливается ли ветка задачи. → (ответ, сам PR).

    `tries` — сколько раз спрашивать, пока ответ «не знаю» (0 — взять
    настройку). Переспрашиваем ТОЛЬКО неизвестность: «вливается» и
    «конфликт» — окончательные ответы, и второй вопрос по ним лишний.

    Осечка хостинга — это «не знаем», а не «конфликт»: увести задачу из
    стопки из-за протухшего токена хуже, чем не увести вовсе.
    """
    open_request = getattr(hosting, "open_request", None)
    if not callable(open_request):
        log.info("сливаемость %s: хостинг не подключён — не знаем", ident)
        return None, {}
    naps = sleep or asyncio.sleep
    attempts = max(1, tries or TRIES)
    request: dict = {}
    for attempt in range(1, attempts + 1):
        try:
            # В потоке: эталонная реализация ходит в сеть через `gh`, и
            # держать этим цикл приёма нельзя (то же правило, что у сверки).
            got = await asyncio.to_thread(open_request, ident)
            if inspect.isawaitable(got):
                got = await got
            request = dict(got or {})
        except Exception as e:
            log.warning("сливаемость %s: хостинг не ответил (%s) — не знаем", ident, e)
            return None, {}
        answer = answer_of(request)
        if answer is not None:
            return answer, request
        if not request:
            # Открытого PR нет вовсе — это окончательный ответ, а не «ещё
            # считаю»: переспрашивать нечего, и на стопке из шести задач без
            # PR переспрос стоил бы минуты пустого ожидания.
            log.info("сливаемость %s: открытого PR нет — мержить нечего", ident)
            return None, {}
        if attempt < attempts:
            log.info("сливаемость %s: хостинг ещё считает (попытка %d из %d) — "
                     "переспрошу через %.0fс", ident, attempt, attempts, pause)
            await naps(pause)
    log.info("сливаемость %s: хостинг так и не сказал за %d попыток — не знаем",
             ident, attempts)
    return None, request
