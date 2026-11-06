"""Тикающий счётчик сборки вместо минуты тишины.

Сборка черновика занимает около минуты (замер: ~18 с накладные на старт
headless-сессии с MCP, ~57 с сама работа). Всё это время сообщение оставалось
прежним, и отличить работу от повисшего бота было нечем: человек уходил
переключать окна и терял контекст. Проблема не в скорости, а в тишине,
поэтому ускорения тут нет — есть видимая жизнь процесса.

Тикает приём, а не сессия: он владеет сообщением в мессенджере и знает момент
постановки в очередь. Стадий («читаю вложения» → «ищу похожие») здесь
намеренно нет: сессия про свой прогресс наружу не сообщает, и чтобы их
показать, пришлось бы тянуть отдельный канал прогресса через раннер. Это
дороже пользы — тревогу снимает сам факт, что счётчик идёт.

Одно состояние достаётся бесплатно и показывается отдельно: пока задача ждёт
свободного слота, её элемент лежит в очереди агента. Виден — «жду слот»,
пропал — «собираю». Проводов ради этого тянуть не нужно, это наблюдение.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .intake import Button, summary

log = logging.getLogger("intake")

# 10 с — компромисс с лимитами мессенджера: правки одного сообщения он режет
# флуд-контролем на темпе около секунды, а частить незачем. На обычной сборке
# это 7 правок; на самом долгом ожидании — несколько сотен, но растянутых на
# полтора часа, то есть тот же темп. Глазом счётчик, меняющийся раз в 10 с,
# читается как живой. 0 выключает сам тик: сообщение остаётся на «принято»,
# сторожа нет, а результат сессии по-прежнему доводит его до исхода.
TICK_SEC = 10.0
# Обещанное ожидание — замер, а не круглое число из головы. «Обычно около
# минуты» снимает половину тревоги само по себе, до всякого счётчика.
EXPECTED_SEC = 75
# Тот же таймаут, что у раннера: по нему раннер снимет сессию и сам пришлёт
# отказ, который человек и увидит. Сторожу он нужен на случай, когда отказ не
# придёт вовсе — раннер убит, redis лёг, результат потерян. Отсюда и запас
# сверху: вложения качаются ДО сессии и вне её таймаута, плюс дорога
# результата.
TIMEOUT_SEC = 10 * 60
WATCHDOG_GRACE_SEC = 300
# Потолок на всё ожидание, включая очередь: перед приёмом может стоять часовая
# рабочая сессия, и это нормально. Но тикать бесконечно нельзя — счётчик,
# идущий десятый час, врёт про состояние ровно так же, как молчащее
# сообщение, а мёртвый раннер оставил бы элемент в очереди навсегда.
MAX_SEC = 90 * 60
TTL_SEC = 60 * 60 * 24 * 7
STOP_WAIT_SEC = 12


def progress_key(source: str, draft_id: str) -> str:
    """Запись о сборке. Отдельно от `Progress._tasks`, потому что переживает
    перезапуск процесса: результат приезжает в него же, но уже другой."""
    return f"intake:{source}:progress:{draft_id}"


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def progress_text(head: str, elapsed: float, queued: bool,
                  expected: int = EXPECTED_SEC) -> str:
    """Строка состояния под шапкой. Ожидание названо явно: человек, который
    знает, что это минута, не идёт переключать окна на сороковой секунде."""
    tail = ("жду свободный слот — впереди другие задачи агента" if queued
            else f"обычно около {clock(expected)}")
    return f"{head}\n⏳ {clock(elapsed)} · {tail}"


def retry_keyboard(draft_id: str, limit: bool = False) -> list[Button]:
    """Кнопка повтора. На исчерпанном лимите она никуда не девается — фактура
    за ней та же и после сброса нажать надо будет именно её, — но называется
    честно: до сброса повтор упадёт так же."""
    label = "🔁 Повторить после сброса" if limit else "🔁 Повторить"
    return [Button(label, f"intake:retry:{draft_id}")]


def collect_head(items: list[dict]) -> str:
    return f"🧠 Собираю задачу из {len(items)} шт. ({summary(items)})…"


def record_of(raw) -> dict | None:
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def elapsed_of(rec: dict) -> float:
    """Сколько идёт сборка по записи. Битую метку времени считаем нулём, а не
    падаем: счётчик — украшение, ронять из-за него доставку карточки нельзя."""
    try:
        started = float(rec.get("started") or 0)
    except (TypeError, ValueError):
        return 0.0
    return time.time() - started if started else 0.0


class Progress:
    def __init__(self, source: str, store, transport, queue_key: str,
                 tick_sec: float = TICK_SEC, expected_sec: int = EXPECTED_SEC,
                 timeout_sec: int = TIMEOUT_SEC,
                 grace_sec: int = WATCHDOG_GRACE_SEC, max_sec: int = MAX_SEC):
        self.source = source
        self.store = store
        self.transport = transport
        self.queue_key = queue_key
        self.tick_sec = tick_sec
        self.expected_sec = expected_sec
        self.timeout_sec = timeout_sec
        self.grace_sec = grace_sec
        self.max_sec = max_sec
        self._tasks: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    def key(self, draft_id: str) -> str:
        return progress_key(self.source, draft_id)

    async def read(self, draft_id: str) -> dict | None:
        return record_of(await self.store.get(self.key(draft_id)))

    async def _queued(self, element: str) -> bool | None:
        """Лежит ли наша задача ещё в очереди агента.

        Поиск по точному элементу, а не вычитывание списка: элемент уникален
        (в нём draft_id и время постановки), а тянуть очередь целиком раз в
        десять секунд незачем.

        None — спросить не вышло. Тогда состояние не меняем: соврать «жду
        слот» на живой сборке хуже, чем показать прежнюю строчку.
        """
        try:
            return await self.store.position(self.queue_key, element) is not None
        except Exception as e:
            log.debug("прогресс: позицию в очереди не спросить: %s", e)
            return None

    async def stop(self, draft_id: str) -> None:
        """Погасить счётчик и дождаться, пока он замолчит.

        Просьбой, а не отменой: правка сообщения уходит в поток, и задача,
        отменённая посреди неё, не отменяет уже улетевший запрос. Такой тик
        приземлился бы ПОСЛЕ финальной надписи и вернул бы на экран замерший
        счётчик, стерев вместе с ним кнопку «Повторить», — то есть ровно то
        состояние, ради которого всё это и делается.

        Себя не ждём: сторожевой срок срабатывает внутри самого тикера, и
        ожидание собственной задачи — это RuntimeError.
        """
        entry = self._tasks.get(draft_id)
        if entry is None:
            return
        task, flag = entry
        flag.set()
        if task is asyncio.current_task():
            return
        self._tasks.pop(draft_id, None)
        try:
            await asyncio.wait_for(task, timeout=STOP_WAIT_SEC)
        except asyncio.CancelledError:
            # Задачу отменили извне (шатдаун) — достаточно, что она молчит.
            pass
        except Exception as e:
            log.warning("прогресс %s: остановка — %s", draft_id, e)

    async def start(self, chat_id, message_id: str, draft_id: str, head: str,
                    items: list[dict], element: str) -> None:
        """Запомнить сообщение со счётчиком и начать его тикать.

        В записи лежит всё, чем можно повторить этот же заход: пачка и шапка.
        Иначе «Повторить» после упавшей сборки требовал бы пересылать
        переписку заново — и кнопка не стоила бы своего места.
        """
        if not message_id:
            return
        await self.store.put(self.key(draft_id), json.dumps({
            "chat_id": chat_id, "message_id": message_id, "head": head,
            "items": items, "started": time.time(), "state": "running",
        }, ensure_ascii=False), TTL_SEC)
        await self.stop(draft_id)
        if self.tick_sec > 0:
            flag = asyncio.Event()
            self._tasks[draft_id] = (asyncio.create_task(
                self._tick(chat_id, message_id, draft_id, head, element, flag)),
                flag)

    async def _sleep_or_stopped(self, flag: asyncio.Event, seconds: float) -> bool:
        """Поспать до следующего тика или проснуться раньше, если попросили
        остановиться. True — попросили."""
        try:
            await asyncio.wait_for(flag.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _tick(self, chat_id, message_id: str, draft_id: str, head: str,
                    element: str, flag: asyncio.Event) -> None:
        """Правит сообщение раз в tick_sec, пока сборка не кончится.

        Гасится снаружи: результат приезжает в разбор, он и зовёт stop(). Флаг
        проверяется и до правки, и после сна — тик, приземлившийся уже после
        финальной надписи, вернул бы на экран замерший счётчик. Сам по себе
        цикл кончается только сторожевым сроком — на случай, когда результат
        не приедет никогда.
        """
        started = time.monotonic()
        deadline = None  # ставится, когда задачу забрал раннер
        # Трёхзначное: None — ещё не спрашивали или хранилище не ответило.
        # Считать «не в очереди» по неудавшемуся запросу нельзя: тогда одна
        # осечка заводит сторожевой срок сессии на задаче, которая всё ещё
        # ждёт слота, — и человек получает «сессия не ответила» с кнопкой
        # «Повторить», нажатие которой даст вторую сессию на ту же пачку.
        queued = None
        try:
            while not flag.is_set():
                if await self._sleep_or_stopped(flag, self.tick_sec):
                    return
                elapsed = time.monotonic() - started
                seen = await self._queued(element)
                if seen is not None:
                    queued = seen
                if queued is False and deadline is None:
                    # Сессия началась — с этого момента у неё свой таймаут, по
                    # которому раннер сам пришлёт отказ. Сторож ждёт заведомо
                    # дольше: он нужен только там, где отказ не придёт вовсе.
                    deadline = elapsed + self.timeout_sec + self.grace_sec
                if elapsed >= self.max_sec or (deadline and elapsed >= deadline):
                    await self.failed(
                        draft_id,
                        "сессия не ответила" if deadline
                        else "не дождался свободного слота", elapsed)
                    return
                if flag.is_set():
                    return
                await self.transport.edit(
                    chat_id, message_id,
                    progress_text(head, elapsed, queued is True, self.expected_sec),
                    None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Счётчик — украшение: его поломка не должна ронять доставку
            # черновика.
            log.warning("прогресс %s: %s", draft_id, e)
        finally:
            # Только свою запись: повтор переиспользует draft_id, и уходящий
            # предшественник не должен вычёркивать сменщика.
            entry = self._tasks.get(draft_id)
            if entry is not None and entry[0] is asyncio.current_task():
                self._tasks.pop(draft_id, None)

    async def done(self, draft_id: str, caption: str) -> None:
        """Сборка кончилась — счётчик остановить и превратить в подпись над
        тем, что придёт следующим сообщением (карточкой или отказом)."""
        await self.stop(draft_id)
        rec = await self.read(draft_id)
        await self.store.drop(self.key(draft_id))
        if not rec:
            return
        await self.transport.edit(
            rec.get("chat_id"), rec.get("message_id"),
            f"🧠 {caption} за {clock(elapsed_of(rec))} ↓", None)

    async def failed(self, draft_id: str, reason: str,
                     elapsed: float | None = None, limit: bool = False,
                     limit_reset: str = "") -> bool:
        """Сборка не удалась — сказать это на месте счётчика и дать «Повторить».

        False, если сообщения со счётчиком нет (индикатор выключен, запись
        протухла, процесс перезапустился до постановки) — тогда вызывающий
        отвечает обычным сообщением.

        `limit` — исчерпанный лимит модели. Повтор до сброса упадёт так же,
        поэтому предлагать его молча нельзя: пишем, что раньше сброса смысла
        нет, и когда он будет.
        """
        await self.stop(draft_id)
        rec = await self.read(draft_id)
        if not rec:
            return False
        rec["state"] = "failed"
        await self.store.put(self.key(draft_id),
                             json.dumps(rec, ensure_ascii=False), TTL_SEC)
        took = elapsed if elapsed is not None else elapsed_of(rec)
        kept = bool(rec.get("items"))
        if limit:
            when = f" Сброс в {limit_reset}." if limit_reset else ""
            tail = ("Повтор раньше сброса упадёт так же." + when
                    + (" Фактура не потеряна — соберу из неё же, когда нажмёшь."
                       if kept else " Перешли фактуру заново."))
        else:
            tail = ("Фактура не потеряна — «Повторить» соберёт из неё же."
                    if kept else "Перешли фактуру заново.")
        return await self.transport.edit(
            rec.get("chat_id"), rec.get("message_id"),
            f"⚠️ Задача не собралась за {clock(took)}: {reason}.\n{tail}",
            retry_keyboard(draft_id, limit) if kept else None)
