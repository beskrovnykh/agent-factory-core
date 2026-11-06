"""Инцидент, на который никто не нажал, напоминает о себе сам.

Задача приёма создаётся БЕЗ исполнителя — это гейт качества: пока на ней
никого нет, ничего не выполняется, и человек успевает её поправить. Для
обычной работы это правильно. Для инцидента — нет: он и заводится потому, что
ждать нельзя, а лежит ровно так же, как всё остальное.

12.09.2026 так и вышло: TASK-302 («уровни потеряли признак isMax») завели в
12:35, метку дежурного поставили, карточку с кнопкой отправили — и она
пролежала 50 минут, пока человек не вернулся в чат и не нажал. Ни в трекере, ни
в чате за это время не было НИ ОДНОГО признака, что инцидент не взят.

Поэтому: карточка инцидента без исполнителя долбит в тот же чат, где висит, —
с той же кнопкой, чтобы взять его можно было одним нажатием. Замолкает, когда
исполнитель появился (неважно, кнопкой или руками в трекере) или когда
напоминаний вышло столько, что их всё равно не читают — тогда последнее
сообщение говорит, что больше не напомнит.

Часть ПРОДУКТА: у обеих инсталляций инцидент лежит одинаково.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("intake")

# Ключ списка ждущих инцидентов. Свой на инсталляцию — как очередь.
PENDING_KEY = "agent:duty:pending"

# Первое напоминание — через это время после заведения. Инцидент, взятый за
# пять минут, не должен получать окриков вовсе.
NAG_AFTER_SEC = 300
# Дальше — с этим шагом.
NAG_EVERY_SEC = 600
# Сколько раз напомнить, прежде чем замолчать.
NAG_TIMES = 6
# Как часто просыпаться и смотреть.
TICK_SEC = 60


def waited(entry: dict, now: float) -> int:
    """Сколько инцидент лежит. `or` здесь нельзя: `since` — отметка времени, и
    нулевую она даёт только в тестах, но именно на ней `0 or now` превращал
    «лежит 50 минут» в «лежит 0»."""
    since = entry.get("since")
    return max(0, int(now - float(now if since is None else since)))


def due(entry: dict, now: float, after: int = NAG_AFTER_SEC,
        every: int = NAG_EVERY_SEC, times: int = NAG_TIMES) -> bool:
    """Пора ли напоминать. Чистая функция — на ней и держатся тесты.

    Первое напоминание через `after`, дальше каждые `every`, но не больше
    `times` раз. Считаем от ПОСЛЕДНЕГО напоминания, а не от заведения: иначе
    после перезапуска приёма все пропущенные шаги схлопнулись бы в очередь
    сообщений подряд.
    """
    sent = int(entry.get("sent") or 0)
    if sent >= times:
        return False
    last = entry.get("last")
    last = float(entry.get("since") or 0 if last is None else last)
    gap = after if sent == 0 else every
    return (now - last) >= gap


def nag_text(entry: dict, now: float, times: int = NAG_TIMES) -> str:
    mins = waited(entry, now) // 60
    ident = entry.get("identifier") or "инцидент"
    title = (entry.get("title") or "").strip()
    last = int(entry.get("sent") or 0) + 1 >= times
    head = (f"🔴 {ident} — дежурный, без исполнителя уже {mins} мин."
            if mins else f"🔴 {ident} — дежурный, исполнителя нет.")
    tail = ("\nБольше не напомню — дальше сам."
            if last else "\nЖми «Отдать агенту», чтобы начать.")
    return f"{head}\n{title}{tail}" if title else head + tail


class Duty:
    """Напоминание про инцидент без исполнителя.

    `keyboard` — та же клавиатура, что под карточкой: напоминание должно
    чиниться одним нажатием, а не походом в трекер.
    """

    def __init__(self, store, transport, tracker, keyboard=None,
                 to_chat: str = "", key: str = PENDING_KEY,
                 after: int = NAG_AFTER_SEC, every: int = NAG_EVERY_SEC,
                 times: int = NAG_TIMES, tick_sec: int = TICK_SEC):
        self.store = store
        self.transport = transport
        self.tracker = tracker
        self.keyboard = keyboard
        # Куда напоминать. Пусто — в чат, где висит карточка. Задано — в общий
        # чат: инцидент касается всех, а карточка лежит у того одного, кто его
        # прислал, и молчать в общий чат значит держать команду в неведении.
        # Транспорт при этом свой: у инсталляции общий чат может быть в другом
        # мессенджере, чем пачка (у первого клиента пачка приходит в MAX, а
        # общий чат — телеграм-группа).
        self.to_chat = str(to_chat or "")
        self.key = key
        self.after, self.every, self.times = after, every, times
        self.tick_sec = tick_sec

    async def remember(self, issue: dict, chat_id, title: str = "") -> None:
        """Взять инцидент под присмотр. Зовётся сразу после карточки."""
        entry = {"issue_id": issue.get("id") or "",
                 "identifier": issue.get("identifier") or "",
                 "url": issue.get("url") or "", "title": title,
                 "chat_id": chat_id, "since": time.time(), "sent": 0,
                 "last": time.time()}
        if not entry["issue_id"]:
            log.error("инцидент без id задачи под присмотр не взять")
            return
        await self._save([e for e in await self._load()
                          if e.get("issue_id") != entry["issue_id"]] + [entry])
        log.info("инцидент %s под присмотром: без исполнителя напомню через %dс",
                 entry["identifier"], self.after)

    async def forget(self, issue_id: str) -> None:
        kept = [e for e in await self._load() if e.get("issue_id") != issue_id]
        await self._save(kept)

    async def _load(self) -> list[dict]:
        try:
            raw = await self.store.get(self.key)
        except Exception as e:
            log.warning("список инцидентов не прочитан: %s", e)
            return []
        try:
            got = json.loads(raw) if raw else []
        except Exception:
            log.error("список инцидентов не разбирается — начинаю пустым")
            return []
        return got if isinstance(got, list) else []

    async def _save(self, entries: list[dict]) -> None:
        try:
            await self.store.put(self.key, json.dumps(entries), 30 * 24 * 3600)
        except Exception as e:
            # Потеря списка — это молчание про инцидент, ровно то, что чиним.
            log.error("список инцидентов не сохранён (%s) — напоминаний не "
                      "будет", e)

    def taken(self, entry: dict) -> bool:
        """Исполнитель появился? Не знаем — считаем, что нет: лишнее
        напоминание переживём, пропущенный инцидент — нет."""
        try:
            return bool(self.tracker.issue_assignee_id(entry.get("issue_id") or ""))
        except Exception as e:
            log.warning("исполнителя %s не спросить (%s) — напоминаю дальше",
                        entry.get("identifier"), e)
            return False

    async def once(self, now: float | None = None) -> int:
        """Один обход. Возвращает, сколько напоминаний отправлено."""
        now = time.time() if now is None else now
        entries, kept, sent = await self._load(), [], 0
        for e in entries:
            if await asyncio.to_thread(self.taken, e):
                log.info("инцидент %s взят — снимаю с присмотра",
                         e.get("identifier"))
                continue
            if due(e, now, self.after, self.every, self.times):
                text = nag_text(e, now, self.times)
                btns = self.keyboard(e["issue_id"]) if self.keyboard else None
                where = self.to_chat or e["chat_id"]
                if await self.transport.send(where, text, btns):
                    e["sent"], e["last"], sent = int(e.get("sent") or 0) + 1, now, sent + 1
                else:
                    log.warning("напоминание про %s не ушло в чат %s",
                                e.get("identifier"), e.get("chat_id"))
            if int(e.get("sent") or 0) < self.times:
                kept.append(e)
            else:
                log.info("инцидент %s: напоминаний вышло %d — замолкаю",
                         e.get("identifier"), self.times)
        # Сохраняем по ЯВНОМУ признаку изменения, а не по сравнению списков:
        # `kept` держит те же объекты, что и `entries`, поэтому после пометки
        # «напомнил» списки оставались равны — и счётчик напоминаний не
        # доезжал до хранилища. Инцидент получал бы окрик на каждом обходе,
        # вечно, и лимит не наступал никогда.
        if sent or len(kept) != len(entries):
            await self._save(kept)
        return sent

    async def run(self) -> None:
        log.info("присмотр за инцидентами: напоминаю %s, первое через %dс, "
                 "дальше каждые %dс, всего %d",
                 f"в чат {self.to_chat}" if self.to_chat else "в чат карточки",
                 self.after, self.every, self.times)
        while True:
            try:
                await self.once()
            except Exception as e:
                log.exception("присмотр за инцидентами споткнулся: %s", e)
            await asyncio.sleep(self.tick_sec)
