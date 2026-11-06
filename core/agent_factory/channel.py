"""Канал приёма: один мессенджер целиком.

Собирает вместе пачку (`Intake`), счётчик сборки (`Progress`), карточки
заведённых задач (`Cards`) и разбор результатов (`Results`) — и отвечает на
два события: пришёл кусок, нажали кнопку.

Здесь, а не во фронте, потому что фронты разные: вторая инсталляция читает
длинными опросами, у первого клиента события приезжают вебхуком. Логика
нажатия от этого не зависит — а жила она во фронте, и клиенту досталась бы
второй копией. Ровно то расхождение, из-за которого продукт и заводился.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone

from . import intake as intake_mod
from . import runs
from . import signoff as signoff_mod
from .cards import Cards
from .commands import Commands, Stop, Unblock
from .duty import Duty
from .intake import Intake, build_task
from .people import People
from .progress import Progress, TTL_SEC, collect_head
from .results import Results
from .updater import Updater

log = logging.getLogger("intake")


class Channel:
    def __init__(self, source: str, store, transport, tracker, queue_key: str,
                 allowed: set[str] | None = None, agent_user_id: str = "",
                 notify_chat: str = "", on_created=None,
                 enqueue_on_assign: bool = True, updater: Updater | None = None,
                 incident_label: str = "",
                 intake_opts: dict | None = None,
                 progress_opts: dict | None = None,
                 people: People | None = None, reviewer=None,
                 project_id: str = "", duty_opts: dict | None = None,
                 journal=None, stopper=None, unblocker: Unblock | None = None,
                 signoff=None):
        """`intake_opts` и `progress_opts` — ручки инсталляции (потолки пачки
        и файла, темп тика, сторожевые сроки). Пусто — берутся умолчания
        продукта; молча игнорировать заданное нельзя, иначе настройка клиента
        перестаёт работать без единого следа.

        `journal` — журнал запусков (`runs.Journal`). Есть — канал вешает
        `/digest`; нет — команды просто не существует, и текст с ней уходит в
        пачку, как любой другой незнакомый.

        `people` и `reviewer` — кто владелец и кто принимает работу. Нужны
        командам в личке: без карты владельца нет, и команды не работают ни
        у кого; без принимающего `/reviewer` не встроена, и текст с ней идёт
        в пачку, как любой другой.

        `stopper` — команда `/stop` (`commands.Stop`). Не передан — канал
        строит её сам из окружения (те же переменные, что у раннера): в
        отличие от `/digest` ей нужен только стор, а он у канала есть, и
        инсталляция, не знающая о команде, всё равно получает кнопку
        остановки — ради неё команда и заведена."""
        self.source = source
        self.store = store
        self.transport = transport
        # Нужен, чтобы положить ответ на карточку комментарием в задачу.
        # Инсталляция может не дать трекера вовсе — тогда ответы ведут себя
        # как обычные сообщения, то есть как до этой развилки.
        self.tracker = tracker
        self.intake = Intake(source=source, store=store, transport=transport,
                             queue_key=queue_key, allowed=allowed or set(),
                             **(intake_opts or {}))
        self.progress = Progress(source, store, transport, queue_key,
                                 **(progress_opts or {}))
        self.cards = Cards(store, transport, tracker, queue_key, agent_user_id,
                           enqueue_on_assign, incident_label)
        # Присмотр за инцидентом без исполнителя. Клавиатура — та же, что под
        # карточкой: напоминание чинится одним нажатием, а не походом в трекер.
        # По умолчанию — в общий чат тем же транспортом. Инсталляция, у
        # которой общий чат в другом мессенджере, передаёт свой транспорт и
        # адрес через `duty_opts`.
        duty_args = dict(transport=transport, keyboard=intake_mod.issue_keyboard,
                         to_chat=notify_chat)
        duty_args.update(duty_opts or {})
        self.duty = Duty(store, tracker=tracker, **duty_args) if tracker else None
        self.results = Results(self.intake, store, transport, tracker,
                               notify_chat, self.progress, on_created,
                               project_id=project_id, duty=self.duty)
        # Обновление ядра — тоже кнопка в том же чате, и разводить её по
        # фронтам значит снова написать одно и то же дважды.
        self.updater = updater
        # Команды в личке — здесь же, по той же причине: развилка «это
        # команда, а не кусок постановки» во фронте была бы копией на каждый
        # мессенджер. `/reviewer` канал вешает сам: инсталляции не надо помнить
        # о ней, ей остаётся зарегистрировать свои.
        self.people = people
        self.reviewer = reviewer
        self.commands = Commands(people, transport)
        # Ленте от принимающего нужен только `user_id()`, команде — весь
        # продуктовый Reviewer. Инсталляция могла отдать первое без второго.
        handler = getattr(reviewer, "command", None)
        if handler:
            self.commands.register("reviewer", handler)
        # Сводка по журналу запусков — тоже владельческая команда: она
        # показывает, чего стоила работа фабрики, и это не общее чтение.
        self.journal = journal
        if journal is not None:
            self.commands.register("digest", journal.command)
        self.stopper = stopper if stopper is not None else Stop.from_env(store, transport)
        self.commands.register("stop", self.stopper.command)
        # `/unblock` — снять сторожа «наблюдать негде» (DEV-105). Умолчания у
        # неё нет: чтобы поднять проверку прямо по сигналу, нужен вход
        # постановки, а он у фронта. Не передан — команды нет вовсе, и текст с
        # ней уходит в пачку, как любой незнакомый.
        self.unblocker = unblocker
        if unblocker is not None:
            self.commands.register(runs.UNBLOCK_COMMAND, unblocker.command)
        # «Слить» и «Отменить» под сообщением задачи (`signoff.SignOff`,
        # DEV-123). Здесь, а не во фронте, по той же причине, что и
        # остальные нажатия: фронтов два, а правило одно. Не передан —
        # нажатия нет, и кнопок тоже: их вешает тот же объект.
        self.signoff = signoff

    # ── события ──────────────────────────────────────────────────────────

    async def on_item(self, chat_id, item) -> int:
        """Кусок в пачку. Возвращает, сколько в ней стало; 0 — не взяли.

        Счёт, а не «да/нет»: по нему видно, что пачка растёт, и это же число
        человек видит на счётчике.

        Команды — ДО пачки. Иначе `/reviewer Иван` ложится куском постановки,
        счётчик показывает «1 шт.», а приём никому не передан.
        """
        if await self.commands.dispatch(chat_id, item) is not None:
            return 0
        if await self.answer(chat_id, item):
            return 0
        added = await self.intake.add(chat_id, item)
        if added.ok:
            # Видно КАЖДЫЙ принятый кусок. Без этой строки «прислал — в ответ
            # пустота» неотличимо от «не дошло до бота вовсе», и разбираться
            # приходится по содержимому redis (12.09.2026 — дважды).
            log.info("приём %s: взят %s, в пачке %d", self.source, item.type,
                     added.count)
            return added.count
        log.info("приём %s: не взят %s — %s", self.source, item.type,
                 added.reason)
        # Молча не взять нельзя: человек считает, что фактура доехала, и
        # узнает обратное только по постановке без половины предмета.
        await self.transport.send(chat_id, added.reason)
        return 0

    async def answer(self, chat_id, item) -> bool:
        """Ответ на карточку заведённой задачи → комментарий в неё.

        Возвращает, взят ли кусок как ответ. False — он обычный, и дальше идёт
        в пачку, как шёл всегда.

        Развилка стоит здесь, а не во фронте каждого мессенджера, по той же
        причине, что и команды: иначе одно и то же было бы написано дважды, и
        второй мессенджер отставал бы от первого на правку.

        Ответом считается только ТЕКСТ. Картинка или файл в ответ на карточку
        идут в пачку, как раньше: вложение в трекер надо загружать, это своя
        работа, и делать её молча внутри развилки нельзя.
        """
        reply_to = str(getattr(item, "reply_to", "") or "")
        if not (reply_to and self.tracker and getattr(item, "type", "") == "text"):
            return False
        raw = await self.store.get(
            intake_mod.answer_key(self.source, chat_id, reply_to))
        if not raw:
            # Связь неизвестна или истекла. Молча съесть нельзя: человек
            # считает, что написал по делу. Пусть идёт в пачку — там он его
            # хотя бы увидит на счётчике.
            return False
        try:
            issue = json.loads(raw)
        except ValueError:
            log.warning("приём %s: связь карточки %s нечитаема — %r",
                        self.source, reply_to, raw[:80])
            return False
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            return False
        ident = issue.get("identifier") or issue.get("id") or "задачу"
        try:
            await asyncio.to_thread(self.tracker.comment, issue["id"], text)
        except Exception as e:
            # Ответ не потерян и не съеден: человек узнаёт, что он не доехал,
            # и может повторить. Молчание здесь было бы худшим исходом —
            # он бы считал, что ответил.
            log.error("приём %s: ответ в %s не записан: %s", self.source, ident, e)
            await self.transport.send(
                chat_id, f"Не смог записать ответ в {ident}. Напиши прямо в задаче.")
            return True
        log.info("приём %s: ответ на карточку → комментарий в %s", self.source, ident)
        await self.transport.send(chat_id, f"Записал в {ident}.")
        return True

    async def on_click(self, data: str, chat_id, message_id: str,
                       callback_id: str = "", clicker: str = "",
                       clicker_id=None) -> str:
        """Нажатие любой кнопки приёма. Возвращает исход — для журнала и тестов.

        Гасить спиннер обязан вызывающий ДО этого вызова: поход в трекер это
        секунда, и всё это время кнопка выглядела бы незажатой.
        """
        if data.startswith(("intake:reset", "intake:collect")):
            action, _, whose = data.partition(":")[2].partition(":")
            # Кнопка адресует пачку своего чата. Чужой id означает пересланное
            # сообщение с живой кнопкой — такой клик не должен трогать чужую
            # пачку. Пустой — кнопка старого образца, до появления адреса.
            if whose and str(whose) != str(chat_id):
                return "foreign-chat"
            if action == "reset":
                await self.intake.reset(chat_id)
                return "reset"
            return await self._collect(chat_id)
        if data.startswith("intake:retry:"):
            return await self._retry(chat_id, data.split(":", 2)[2])
        if data.startswith("update:apply:"):
            if not self.updater:
                log.warning("нажали обновление, но обновлять нечем")
                return "no-updater"
            return await self.updater.apply(
                data.split(":", 2)[2], chat_id, message_id, clicker_id)
        if signoff_mod.handles(data):
            if self.signoff is None:
                # Кнопка живёт в сообщении месяц, а подпись из чата могли и
                # выключить. Молчать нельзя: нажатие выглядит сработавшим.
                log.warning("приём: нажали подпись %r, а подписывать нечем", data)
                await self.transport.send(
                    chat_id, "Подпись работы из чата не подключена — "
                             "мержи на странице очереди.")
                return "no-signoff"
            return await self.signoff.on_click(data, chat_id, message_id,
                                               clicker, clicker_id)
        if data.startswith("intake:agent:") or data.startswith("intake:delete:"):
            action, issue_id = data.split(":")[1], data.split(":", 2)[2]
            return await self.cards.handle(action, issue_id, chat_id, message_id,
                                           clicker or "?")
        log.warning("приём: не узнал нажатие %r", data)
        return "unknown"

    # ── сборка ───────────────────────────────────────────────────────────

    async def _collect(self, chat_id) -> str:
        draft_id = secrets.token_hex(6)
        got = await self.intake.collect(
            chat_id, draft_id, datetime.now(timezone.utc).isoformat())
        if not got:
            return "empty"
        await self.progress.start(chat_id, got.control_mid, draft_id,
                                  collect_head(got.items), got.items, got.element)
        log.info("приём %s → в очередь, кусков %d", draft_id, len(got.items))
        return "queued"

    async def _retry(self, chat_id, draft_id: str) -> str:
        """Повтор упавшей сборки. Фактура лежит в записи о прогрессе —
        пересылать переписку второй раз человек не должен, иначе кнопка не
        стоит своего места.

        Состояние переводим ДО постановки: два быстрых тапа не должны дать
        две сессии на одну пачку.
        """
        rec = await self.progress.read(draft_id)
        items = (rec or {}).get("items") or []
        mid = str((rec or {}).get("message_id") or "")
        if not items:
            await self.transport.edit(
                chat_id, mid, "⚠️ Фактуру уже не помню — перешли заново.", None)
            return "nothing-to-retry"
        if rec.get("state") != "failed":
            return "already-running"
        rec["state"] = "running"
        await self.store.put(self.progress.key(draft_id),
                             json.dumps(rec, ensure_ascii=False), TTL_SEC)
        task = build_task(self.source, chat_id, items, draft_id,
                          datetime.now(timezone.utc).isoformat())
        element = json.dumps(task, ensure_ascii=False)
        await self.store.enqueue(self.intake.queue_key, element)
        await self.progress.start(chat_id, mid, draft_id,
                                  rec.get("head") or collect_head(items),
                                  items, element)
        log.info("повтор %s → в очередь, кусков %d", draft_id, len(items))
        return "queued"
