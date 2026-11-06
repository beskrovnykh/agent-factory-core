"""Кнопки под карточкой заведённой задачи: «Отдать агенту» и «Удалить».

Отдельно от разбора результатов (`results.py`), который карточку рисует:
рисование и клики по ней меняются по разным поводам. Отдельно от трекера
(`tracking.py`) — здесь только то, что делает нажатие.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from .deploy import remember_task
from .queueing import work_task
from .tracking import Tracker

log = logging.getLogger("intake")


class Cards:
    """Клики по кнопкам под карточкой заведённой задачи."""

    def __init__(self, store, transport, tracker: Tracker, queue_key: str,
                 agent_user_id: str, enqueue_on_assign: bool = True,
                 incident_label: str = ""):
        """`enqueue_on_assign` — ставить ли задачу в очередь самим нажатием.

        У инсталляции без вебхука от трекера иначе никто её не поставит. У той,
        где вебхук есть, наоборот: назначение поймает он, и постановка здесь
        будет ВТОРОЙ. 11.09 так и вышло — по TASK-285 и TASK-288 стартовало по
        две сессии, две ветки, два «взялся».
        """
        self.store = store
        self.transport = transport
        self.tracker = tracker
        self.queue_key = queue_key
        self.agent_user_id = agent_user_id
        self.enqueue_on_assign = enqueue_on_assign
        self.incident_label = incident_label

    async def handle(self, action: str, issue_id: str, chat_id, message_id: str,
                     clicker: str) -> str:
        # Замок на задачу: спиннер на кнопке гасится до похода в сеть, и два
        # быстрых тапа без сериализации дают две рабочие сессии на одну задачу.
        if not await self.store.claim(f"intake:card:{issue_id}:{action}", 60):
            return "already-running"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if action == "delete":
            try:
                ident = await asyncio.to_thread(self.tracker.delete, issue_id)
            except Exception as e:
                log.warning("удаление задачи %s не прошло: %s", issue_id, e)
                await self.transport.send(
                    chat_id, f"⚠️ Не смог удалить задачу: {e}. "
                             f"Она на месте — удали в трекере.")
                return "delete-failed"
            await self.transport.edit(
                chat_id, message_id,
                f"🗑 Удалена {ident or 'задача'} — {clicker}, {ts}.\n"
                f"Восстановить можно из Trash.", None)
            return "deleted"

        if not self.agent_user_id:
            await self.transport.send(
                chat_id, "⚠️ AGENT_LINEAR_USER_ID не настроен — назначь агента "
                         "в трекере руками.")
            return "agent-not-configured"
        try:
            issue = await asyncio.to_thread(self.tracker.assign, issue_id,
                                            self.agent_user_id)
        except Exception as e:
            log.warning("назначение %s на агента не прошло: %s", issue_id, e)
            await self.transport.send(
                chat_id, f"⚠️ Не смог отдать задачу агенту: {e}. "
                         f"Назначь его в трекере — сессия стартует от этого.")
            return "assign-failed"
        if self.enqueue_on_assign and not issue.get("already"):
            task = work_task(issue, clicker, self.incident_label)
            await self.store.enqueue(self.queue_key,
                                     json.dumps(task, ensure_ascii=False))
            # Тот же след, что оставляет вебхук: по нему проверка выката
            # найдёт задачу, когда из события хостинга известна одна ветка.
            await remember_task(self.store, task)
        ident = issue.get("identifier") or ""
        head = (f"🤖 {ident} уже у агента" if issue.get("already")
                else f"🤖 {ident} отдана агенту")
        text = f"{head} — {clicker}, {ts}\n" + (issue.get("url") or "")
        # Кнопки снимаем обе: задача уже у агента, и «Удалить» из-под неё
        # удалила бы работу, которая уже идёт.
        if not await self.transport.edit(chat_id, message_id, text, None):
            await self.transport.send(chat_id, text)
        return "assigned"
