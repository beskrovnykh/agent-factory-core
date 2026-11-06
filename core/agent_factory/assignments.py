"""Назначил задачу агенту — он взялся. Запасной путь, вместо вебхука.

Обычно новость о назначении приносит вебхук трекера (`webhook.py`). Опрос
нужен там, где его нет: до площадки не достучаться снаружи, или доставка не
доехала. Разница только в том, кто принёс новость, — что с ней делать,
решает один и тот же `work_task`.

Здесь новость добывается опросом: раз в интервал спрашиваем трекер, что
висит на агенте и ещё не начато. Состояние задачи и есть отметка: сессия
переводит её в работу, и следующий круг её уже не видит. Ключ захвата тот же,
что у вебхука, — вместе они не дают второй сессии.
"""
from __future__ import annotations

import asyncio
import json
import logging

from .deploy import remember_task
from .queueing import hand_over, work_task

log = logging.getLogger("intake")

# Какие состояния значат «ещё не начато». Типы, а не имена: имена у каждой
# команды свои, типы у Linear общие.
NOT_STARTED = ("backlog", "unstarted")
INTERVAL_SEC = 60


class Assignments:
    def __init__(self, tracker, store, queue_key: str, agent_user_id: str,
                 interval_sec: int = INTERVAL_SEC,
                 states: tuple[str, ...] = NOT_STARTED,
                 incident_label: str = "", rounds=None, reviewer_id: str = ""):
        self.tracker = tracker
        self.store = store
        self.queue_key = queue_key
        self.agent_user_id = agent_user_id
        self.interval_sec = interval_sec
        self.states = states
        # Развилка «дежурный или работа» живёт в work_task — одна на все пути
        # постановки. Здесь только приносим имя лейбла этой инсталляции.
        self.incident_label = incident_label
        # Потолок кругов — тот же, что у вебхука (`runs.rounds_exhausted`).
        # Опрос не знает, кто назначил, поэтому исключения для принимающего
        # здесь нет: этот путь запасной, и человек им не пользуется.
        self.rounds = rounds
        self.reviewer_id = str(reviewer_id or "")

    async def _already_queued(self, issue_id: str) -> bool:
        """Лежит ли задача в очереди прямо сейчас.

        Проверяем состав очереди, а не отметку с временем жизни: перед задачей
        может стоять часовая сессия, и всё это время она честно ждёт слота в
        состоянии «не начато». Отметка на пару минут пропустила бы её второй
        раз, отметка на сутки заблокировала бы законное переназначение.
        """
        try:
            for raw in await self.store.read_all(self.queue_key):
                if json.loads(raw).get("issue_id") == issue_id:
                    return True
        except Exception as e:
            log.warning("очередь не прочитать (%s) — считаю, что задачи в ней нет", e)
        return False

    async def tick(self) -> int:
        """Один круг опроса. Возвращает, сколько задач поставлено в очередь."""
        if not self.agent_user_id:
            return 0
        try:
            issues = await asyncio.to_thread(
                self.tracker.assigned_issues, self.agent_user_id, self.states)
        except Exception as e:
            log.warning("назначения не спросить: %s", e)
            return 0
        queued = 0
        for issue in issues:
            issue_id = issue.get("id") or ""
            if not issue_id or await self._already_queued(issue_id):
                continue
            # Короткий захват — только против наложения кругов: сам круг может
            # идти дольше интервала, если трекер отвечает медленно.
            if not await self.store.claim(f"agent:started:{issue_id}", 120):
                continue
            spent = 0
            if self.rounds:
                try:
                    spent = int(await asyncio.to_thread(
                        self.rounds, issue.get("identifier") or "") or 0)
                except Exception as e:
                    log.warning("опрос (%s): круги не сосчитаны (%s) — ставлю "
                                "без потолка", issue.get("identifier") or issue_id, e)
            if spent:
                await hand_over(self.tracker, issue_id,
                                issue.get("identifier") or issue_id, spent,
                                self.reviewer_id)
                continue
            task = work_task(issue, "назначено в трекере", self.incident_label)
            await self.store.enqueue(self.queue_key,
                                     json.dumps(task, ensure_ascii=False))
            # Как и у вебхука: проверке выката нужен след задачи по имени ветки.
            await remember_task(self.store, task)
            log.info("назначение %s → в очередь", issue.get("identifier") or issue_id)
            queued += 1
        return queued

    async def run(self) -> None:
        if not self.agent_user_id:
            log.info("опрос назначений выключен: не задан исполнитель-агент")
            return
        log.info("слежу за назначениями: раз в %dс", self.interval_sec)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Один кривой круг не должен останавливать слежение.
                log.exception("круг опроса назначений не удался")
            await asyncio.sleep(self.interval_sec)
