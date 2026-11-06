"""Команды в личке бота: `/reviewer` встроена, свои регистрирует инсталляция.

Зачем отдельный роутер. Приём кладёт в пачку ЛЮБОЙ текст из лички, и без
развилки «это команда» команда `/reviewer` уезжала бы в пачку куском
постановки. У первого клиента развилка стояла во фронте — там, где он
разбирает вебхук мессенджера. Фронтов у него два (Telegram и MAX), у второй
инсталляции свой, на длинных опросах; развилка во фронте — это три копии
одного правила, и ровно такие копии были причиной заводить продукт.

Почему реестр по имени, а не протокол вроде `Handoff`. Протокол хорош, когда
у инсталляции ОДИН ответ на ОДИН вопрос продукта. Команд же у инсталляции
столько, сколько ей нужно ручек (клиенту — `/automerge`, и завтра будет
другая), и каждая новая не должна требовать правки продукта. Реестр это
даёт: `channel.commands.register("automerge", handler)` — и всё.

Почему доступ проверяет роутер, а не каждая команда. Команды в личке —
управление инсталляцией: кто принимает работу, что мержится без спроса. По
умолчанию они владельческие, и правило «карта не заполнена → владельца нет →
не работает ни у кого» обязано быть одно на всех, иначе оно разойдётся ровно
в той команде, которую написали последней. Открыть команду шире — осознанный
флаг при регистрации, а не забытая проверка.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from . import runs, sessions
from .deploy import CLASS_UNOBSERVABLE
from .people import People

log = logging.getLogger("intake")

# Имя команды и хвост. «@бот» после имени Telegram дописывает сам в группах и
# при выборе из подсказки — его отбрасываем, иначе `/reviewer@our_bot` было
# бы незнакомой командой.
COMMAND = re.compile(r"^/([A-Za-z][A-Za-z0-9_]*)(?:@\w+)?(?:\s+(.*))?$", re.S)

NOT_OWNER = "⚠️ Эту команду может выполнять только владелец."

# ── /stop: снять идущую сессию (DEV-91) ─────────────────────────────────────
#
# До этой команды ушедшую не туда сессию было не остановить ничем, кроме
# рестарта юнита: она доезжала до конца или до потолка ходов и стоила $8–17.
# Механизм снятия у раннера уже есть (группа процессов, тот же, что на
# таймауте) — команде остаётся положить сигнал туда, откуда раннер его
# читает: ключ в локальном redis, том же, где очередь и делегирование.
# Умолчания — те же, что у раннера (AGENT_STOP_KEY_PREFIX); раннер это
# отдельный процесс, импортировать его нельзя, совпадение закреплено
# контрактным тестом в core/test_runner.py.
STOP_KEY_PREFIX_VAR = "AGENT_STOP_KEY_PREFIX"
DEFAULT_STOP_KEY_PREFIX = "agent:stop"
# Сколько ключ живёт, если сессия его не прочла: раннер спрашивает раз в
# секунды, час — это «команда дана, сессии нет», и такой ключ раннер к
# следующей сессии не применит (снимает на старте), но и вечно лежать ему
# незачем.
STOP_TTL_SEC = 3600
# Идентификатор задачи и хвост: общая форма для команд, адресованных одной
# задаче (`/stop`, `/unblock`).
IDENT_TAIL = re.compile(r"^([A-Za-z][A-Za-z0-9]*-\d+)(?:\s+(.*))?$", re.S)
STOP_HELP = ("Как: `/stop DEV-91 ушла не туда`. Идентификатор задачи и причина "
             "— причина уходит в комментарий задачи и в учёт.")


def stop_key(prefix: str, ident: str) -> str:
    return f"{prefix}:{ident}"


def stop_record(reason: str, by: str, at: float | None = None) -> str:
    """Что раннер прочтёт из ключа: причина словами, кто, когда."""
    return json.dumps({"reason": reason, "by": by,
                       "at": time.time() if at is None else at},
                      ensure_ascii=False)


class Stop:
    """`/stop <задача> <причина>` — сигнал раннеру снять сессию по задаче.

    Что это владелец, проверяет роутер команд ДО вызова. Причина обязательна:
    останов без причины оставляет в задаче комментарий «остановлена», по
    которому через неделю не понять, чем сессия провинилась, — а понять надо,
    это и есть материал для правки контракта.

    `work_root` — рабочий каталог раннера: по нему команда видит, идёт ли
    сейчас сессия по задаче, и не кладёт сигнал в пустоту. Нет каталога (у
    приёма в чужом контейнере его может не быть) — кладём вслепую и говорим
    об этом.
    """

    def __init__(self, store, transport, key_prefix: str = DEFAULT_STOP_KEY_PREFIX,
                 work_root: str = "", ttl_sec: int = STOP_TTL_SEC):
        self.store = store
        self.transport = transport
        self.key_prefix = (key_prefix or "").strip() or DEFAULT_STOP_KEY_PREFIX
        self.work_root = work_root or ""
        self.ttl_sec = ttl_sec

    @classmethod
    def from_env(cls, store, transport, environ=None) -> "Stop":
        """Умолчания из окружения инсталляции — теми же переменными, что
        читает раннер: одно имя, одно значение, два потребителя."""
        env = os.environ if environ is None else environ
        return cls(store, transport,
                   key_prefix=env.get(STOP_KEY_PREFIX_VAR, ""),
                   work_root=env.get("AGENT_WORK_ROOT", ""))

    async def _say(self, chat_id, text: str) -> None:
        if self.transport is None:
            log.warning("ответить некуда: у /stop нет транспорта")
            return
        await self.transport.send(chat_id, text)

    async def running(self, ident: str) -> bool | None:
        """Идёт ли сейчас сессия по задаче. None — каталога нет, не знаем."""
        if not self.work_root:
            return None
        try:
            got = await asyncio.to_thread(sessions.walk, self.work_root)
        except Exception as e:
            log.warning("/stop: рабочий каталог не прочитан: %s", e)
            return None
        return any(s.get("идёт") and str(s.get("task") or "") == ident for s in got)

    async def command(self, chat_id, args: str) -> str:
        m = IDENT_TAIL.match((args or "").strip())
        if not m:
            await self._say(chat_id, "⚠️ Не понял, какую задачу остановить. " + STOP_HELP)
            return "no-ident"
        ident = m.group(1).upper()
        reason = " ".join((m.group(2) or "").split())
        if not reason:
            await self._say(chat_id, f"⚠️ Назови причину: `/stop {ident} <почему>`. "
                                     "Она уходит в комментарий задачи.")
            return "no-reason"
        running = await self.running(ident)
        if running is False:
            await self._say(chat_id, f"Сессии по {ident} сейчас не вижу — "
                                     "останавливать нечего. Сигнал не положен.")
            return "not-running"
        try:
            await self.store.put(stop_key(self.key_prefix, ident),
                                 stop_record(reason, str(chat_id)), self.ttl_sec)
        except Exception as e:
            await self._say(chat_id, f"⚠️ Не смог положить сигнал остановки: {e}.")
            return "store"
        blind = " (рабочего каталога не вижу, кладу вслепую)" if running is None else ""
        await self._say(chat_id, (
            f"⏹ {ident}: сигнал остановки положен{blind}. Причина: {reason}.\n"
            f"Раннер снимет сессию на ближайшем опросе; задача вернётся "
            f"принимающему с комментарием."))
        log.info("/stop %s от %s: %s", ident, chat_id, reason)
        return "ok"


# ── /unblock: препятствие снято (DEV-105) ───────────────────────────────────
#
# За `unobservable` следующая проверка в той же точке не ставится (DEV-89), и
# снять это было нечем: точка меняется мержем, а проверка после мержа у
# client-b выключена. DEV-66 так и заперся — препятствием были мёртвые токены
# личностей, их починили в тот же вечер, а сказать об этом фабрике было
# нечем; перевод в `Verifying` не поднимал ничего и выглядел как поломка.
#
# Команда НЕ ставит проверку в обход сторожа: она кладёт факт «препятствие
# снято» рядом с записями проверок (`runs.clear_block`) и зовёт тот же вход,
# что и перевод статуса. Решают по-прежнему ворота — они и решат, что теперь
# сигнал свежее блокирующей проверки. Так у постановки остаётся одна дорога,
# а у сигнала — своя запись с автором и временем.
UNBLOCK_HELP = (f"Как: `/{runs.UNBLOCK_COMMAND} DEV-66 токены личностей починены`. "
                "Идентификатор задачи и что именно изменилось — причина ложится "
                "в запись рядом с проверками, по ней через месяц видно, на каком "
                "основании сторожа сняли.")


class Unblock:
    """`/unblock <задача> <что изменилось>` — снять блокировку «наблюдать негде».

    Что это владелец, проверяет роутер команд ДО вызова. Причина обязательна
    по той же причине, что у `/stop`: «снято» без причины через неделю не
    объяснит, почему сторожа сняли.

    `work_root` — рабочий каталог раннера: там лежат и записи проверок, и
    запись снятия. Нет каталога — команда отказывает, а не кладёт сигнал
    вслепую: блокировку держат следы в этом каталоге, и снимать её больше
    негде.

    `raise_check(ident) -> str` — тот же вход постановки проверки, что у
    перевода статуса (`deploy.Verification.on_unblocked`). Не задан — сигнал
    записан, и проверку поднимет следующий перевод задачи в проверку.
    """

    def __init__(self, work_root: str, transport, raise_check=None):
        self.work_root = work_root or ""
        self.transport = transport
        self.raise_check = raise_check

    async def _say(self, chat_id, text: str) -> None:
        if self.transport is None:
            log.warning("ответить некуда: у /%s нет транспорта", runs.UNBLOCK_COMMAND)
            return
        await self.transport.send(chat_id, text)

    async def command(self, chat_id, args: str) -> str:
        m = IDENT_TAIL.match((args or "").strip())
        if not m:
            await self._say(chat_id, "⚠️ Не понял, с какой задачи снимать. " + UNBLOCK_HELP)
            return "no-ident"
        ident = m.group(1).upper()
        reason = " ".join((m.group(2) or "").split())
        if not reason:
            await self._say(chat_id, f"⚠️ Назови, что изменилось: "
                                     f"`/{runs.UNBLOCK_COMMAND} {ident} <что снято>`. "
                                     f"Это уходит в запись о снятии.")
            return "no-reason"
        if not self.work_root:
            await self._say(chat_id, "⚠️ Рабочего каталога раннера не вижу — "
                                     "блокировка живёт в нём, и записать снятие некуда.")
            return "no-root"
        # Снимаем то, что есть, а не «на всякий случай». Блокировку держит
        # последняя состоявшаяся проверка; кончилась она не `unobservable` —
        # сторожа нет, и запись о снятии была бы бумагой ни о чём.
        last = await asyncio.to_thread(runs.last_verify, self.work_root, ident)
        cls = str(last.get("класс") or "")
        if cls != CLASS_UNOBSERVABLE:
            was = (f"последняя проверка кончилась классом «{cls}»" if cls
                   else "состоявшихся проверок по ней не вижу")
            await self._say(chat_id, f"По {ident} блокировки «наблюдать негде» нет: "
                                     f"{was}. Ничего не записал.")
            return "no-block"
        try:
            rec = await asyncio.to_thread(runs.clear_block, self.work_root, ident,
                                          str(chat_id), reason)
        except Exception as e:
            await self._say(chat_id, f"⚠️ Не смог записать снятие блокировки: {e}.")
            return "write-failed"
        log.info("/%s %s от %s: %s", runs.UNBLOCK_COMMAND, ident, chat_id, reason)
        if self.raise_check is None:
            await self._say(chat_id, (
                f"🔓 {ident}: блокировка снята ({reason}). Переведи задачу в "
                f"состояние проверки — теперь перевод поднимет проверяльщика."))
            return "cleared"
        try:
            got = await self.raise_check(ident)
        except Exception as e:
            await self._say(chat_id, (
                f"🔓 {ident}: снятие записано ({reason}), а поставить проверку "
                f"сейчас не вышло: {e}. Переведи задачу в состояние проверки."))
            return "cleared"
        await self._say(chat_id, f"🔓 {ident}: {UNBLOCK_OUTCOMES.get(got, UNBLOCK_UNKNOWN.format(got=got))}")
        return got


# Что сказать человеку про исход постановки. Слова — его, а не наши коды:
# «queued» в личке не объясняет ничего.
UNBLOCK_OUTCOMES = {
    "queued": "блокировка снята, проверяльщик в очереди.",
    "skipped": "снятие записано, но ворота проверку не поставили — причина в "
               "сообщении задачи (⏭) и в журнале приёма.",
    "disabled": "снятие записано, но эта точка проверки выключена настройкой — "
                "сессия не ставится.",
    "deduped": "снятие записано, но постановка отброшена как повтор — переведи "
               "задачу в состояние проверки.",
    "enqueue-failed": "снятие записано, а очередь проверку не приняла — смотри "
                      "журнал приёма.",
}
UNBLOCK_UNKNOWN = "снятие записано; постановка ответила «{got}»."


def parse(text: str) -> tuple[str, str] | None:
    """`/reviewer Иван на неделю` → («reviewer», «Иван на неделю»)."""
    m = COMMAND.match((text or "").strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


class Commands:
    def __init__(self, people: People | None, transport):
        # Без карты владельца нет — и владельческие команды не работают ни у
        # кого. Это безопаснее, чем «работают у всех».
        self.people = people or People()
        self.transport = transport
        self._handlers: dict[str, tuple[object, bool]] = {}

    def register(self, name: str, handler, owner_only: bool = True) -> None:
        """`handler(chat_id, args) -> str` — исход для журнала. Отвечает
        человеку он сам: у команды бывает не один ответ и не в один чат."""
        self._handlers[name.lower().lstrip("/")] = (handler, owner_only)

    def knows(self, name: str) -> bool:
        return name.lower().lstrip("/") in self._handlers

    async def dispatch(self, chat_id, item) -> str | None:
        """Выполнить, если кусок — команда. None — не команда, кусок идёт в
        пачку.

        Команда — это СВОЙ набранный текст. Пересланное командой не считается:
        это чужой текст, и «/reviewer» в нём — цитата, а не распоряжение.
        Незнакомая команда — тоже в пачку: молча съесть текст, который не
        поняли, хуже, чем показать его в счётчике, и человек сразу видит, что
        как команду его не прочли.
        """
        if getattr(item, "type", "") != "text" or getattr(item, "frm", ""):
            return None
        parsed = parse(getattr(item, "text", ""))
        if not parsed:
            return None
        name, args = parsed
        entry = self._handlers.get(name)
        if not entry:
            return None
        handler, owner_only = entry
        if owner_only and not self.people.is_owner(chat_id):
            log.warning("команда /%s: не владелец, chat_id=%s", name, chat_id)
            await self.transport.send(chat_id, NOT_OWNER)
            return "not-owner"
        outcome = await handler(chat_id, args)
        log.info("команда /%s → %s", name, outcome)
        return outcome or "done"
