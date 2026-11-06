"""Закрытая задача уезжает в архив: потолок трекера — не срок.

Бесплатный Linear считает 250 задач, и считает он только НЕархивные. При
семи задачах в сутки этот потолок приходит за месяц, а приходит он не
предупреждением, а отказом заводить новые задачи — то есть встаёт приём
постановок. Авто-архивация трекера не спасает: её минимум — месяц.

Архив это не удаление. `issue(id:)` отдаёт архивную задачу целиком, вместе
со статусом и всеми комментариями (проверено роундтрипом 12.09.2026 на
DEV-62), поэтому учёт, который живёт в комментариях, переживает архивацию.
Убирается задача только из списков и обычных видов.

Кто зовёт: лента (`issue_feed.IssueFeed`) — на том же событии смены статуса,
на котором она правит сообщение в чате. Второго обхода «поищем закрытые» при
нуле нет намеренно: два источника правды про «задача закрылась» разойдутся,
и разойдутся молча.

`AGENT_ARCHIVE_AFTER_DAYS` — задержка в днях, умолчание 0 (убирать сразу).
Ноль годится там, где в трекере один человек и агент; там, где доской
пользуются пятеро живых людей, задачи, исчезающие из видов сразу после
«Готово», меняют привычную картину, и площадка ставит себе отсрочку. Тогда
одним событием закрытия не обойтись — нужен обход, и он здесь же. При нуле
обход не заводится вовсе.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("intake")

DAY_SEC = 24 * 3600

# Отложенный архив меряется днями — чаще ходить незачем.
INTERVAL_SEC = DAY_SEC


def days_from(raw) -> int:
    """Ручка `AGENT_ARCHIVE_AFTER_DAYS` → целое число дней.

    Мусор и отрицательное — это ноль, то есть «сразу», и об этом пишем в
    журнал: тихо подставленное умолчание выглядит как работающая настройка.
    """
    if raw in (None, ""):
        return 0
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("AGENT_ARCHIVE_AFTER_DAYS=%r — не число, архивирую сразу", raw)
        return 0
    if days < 0:
        log.warning("AGENT_ARCHIVE_AFTER_DAYS=%s — меньше нуля, архивирую сразу", raw)
        return 0
    return days


def closed_at(issue: dict) -> float:
    """Когда задачу закрыли, секундами эпохи. 0 — не понять.

    У `completed` заполнен `completedAt`, у `canceled` — `canceledAt`, и одно
    другое не подменяет. `updatedAt` в запасе: он сдвигается от любой правки,
    но лучше поздно, чем никогда, — а «никогда» здесь означает задачу,
    которая не уедет в архив вообще.
    """
    from datetime import datetime
    for field in ("completedAt", "canceledAt", "updatedAt"):
        raw = (issue or {}).get(field)
        if not raw:
            continue
        try:
            # `Z` вместо смещения: до 3.11 `fromisoformat` его не понимает, а
            # площадка живёт и на 3.10.
            return datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            log.warning("архив: %s=%r не разобрать", field, raw)
    return 0.0


def iso(ts: float) -> str:
    """Секунды эпохи → время в UTC, как его понимает трекер."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class Archiver:
    """Убирает закрытые задачи из активных. Живёт рядом с лентой."""

    def __init__(self, store, tracker, queue_key: str = "",
                 agent_user_id: str = "", after_days: int = 0,
                 team_id: str = "", interval_sec: int = INTERVAL_SEC):
        """`after_days` 0 — убирать при закрытии, положительное — обходом.

        `queue_key` и `agent_user_id` нужны одному: понять, не идёт ли по
        задаче сессия прямо сейчас. Без них проверка беднее, но не ломается.
        """
        self.store = store
        self.tracker = tracker
        self.queue_key = str(queue_key or "")
        self.agent_user_id = str(agent_user_id or "")
        self.after_days = int(after_days or 0)
        self.team_id = str(team_id or "")
        self.interval_sec = interval_sec

    @property
    def able(self) -> bool:
        """Умеет ли трекер этой площадки убирать задачи в архив."""
        return callable(getattr(self.tracker, "archive_issue", None))

    # ── по событию закрытия ──────────────────────────────────────────────

    def mine(self, issue: dict) -> bool:
        """Наша ли это задача. Чужую убирать нельзя ни на каком пути.

        Рабочее пространство трекера общее, а вебхук подписывают на весь
        воркспейс: без этой проверки закрытие в соседнем отделе уводило бы
        ЕГО задачи из активных. Лента по той же причине шире: писать про
        чужую задачу — шум, убрать её — чужая пропавшая работа.

        Пустой `AGENT_TEAM_ID` поэтому значит «не убираю», а не «убираю
        везде». То же правило у обхода — иначе два пути одной уборки жили бы
        по разным.
        """
        if not self.team_id:
            return False
        team = str(issue.get("teamId") or (issue.get("team") or {}).get("id") or "")
        return team == self.team_id

    async def on_closed(self, issue: dict) -> bool:
        """Задача перешла в закрытый статус. True — убрали в архив.

        Отложенный архив здесь ничего не делает: его дело — обход. Иначе
        ручка задержки не значила бы ничего.
        """
        if self.after_days:
            return False
        return await self.archive(issue)

    async def archive(self, issue: dict) -> bool:
        """Убрать задачу, если она наша и по ней никто не работает.

        Аргумент — задача целиком, а не россыпь полей: одно и то же нужно
        обоим путям (id, идентификатор для журнала, команда и исполнитель), а
        событие ленты и узел обхода дают его одинаково.
        """
        issue_id = str(issue.get("id") or "")
        name = str(issue.get("identifier") or "") or issue_id
        if not issue_id or not self.mine(issue):
            return False
        archive = getattr(self.tracker, "archive_issue", None)
        if not archive:
            # Площадка без архива — рабочее состояние, а не поломка: лента от
            # этого не должна падать, а задача просто остаётся в списках.
            return False
        if await self.session_live(issue_id, self.assignee_of(issue)):
            log.info("архив: %s пропускаю — по ней идёт сессия", name)
            return False
        try:
            ok = bool(await asyncio.to_thread(archive, issue_id))
        except Exception as e:
            # Архив — уборка, а не обязательство: не вышло сейчас, выйдет в
            # следующий раз или руками. Ронять ленту из-за неё нельзя.
            log.warning("архив: %s не убрана (%s)", name, e)
            return False
        if ok:
            log.info("архив: %s убрана из активных", name)
        return ok

    @staticmethod
    def assignee_of(issue: dict) -> str:
        """Исполнитель: в событии ленты он плоским полем, в ответе трекера —
        вложенным объектом."""
        return str(issue.get("assigneeId")
                   or (issue.get("assignee") or {}).get("id") or "")

    async def session_live(self, issue_id: str, assignee_id: str = "") -> bool:
        """Идёт ли по задаче работа агента прямо сейчас.

        Три признака, и все три дешёвые:

        • задача всё ещё на агенте — сессия её не отдала, значит не закончила.
          Самый надёжный из трёх: закрытие агент делает сам, и «закрыл и
          вернул» приезжает двумя событиями, между которыми он ещё работает;
        • захват `agent:started:<issue>` — сессию только что поставили;
        • задача лежит в очереди — сессия ещё не начиналась.

        Захват именно ЧИТАЕМ, а не захватываем: `claim` на свободном ключе
        занял бы его на своё окно, и настоящее назначение в эти минуты
        отбросилось бы как дубль — то есть работа бы не началась.

        Не спросили хранилище — считаем, что сессия идёт. Пропущенная уборка
        стоит одной задачи в списке; архив под работающей сессией стоит того,
        что сессия дописывает в задачу, которой уже нет в видах.
        """
        if (self.agent_user_id and assignee_id
                and str(assignee_id) == self.agent_user_id):
            return True
        try:
            if await self.store.get(f"agent:started:{issue_id}"):
                return True
        except Exception as e:
            log.warning("архив: не спросить захват по %s (%s) — не трогаю",
                        issue_id, e)
            return True
        if not self.queue_key:
            return False
        try:
            for raw in await self.store.read_all(self.queue_key):
                try:
                    if str(json.loads(raw).get("issue_id") or "") == str(issue_id):
                        return True
                except Exception:
                    continue
        except Exception as e:
            log.warning("архив: не прочитать очередь (%s) — не трогаю %s",
                        e, issue_id)
            return True
        return False

    # ── обход, если задержка задана ──────────────────────────────────────

    async def tick(self, now: float | None = None) -> int:
        """Один круг отложенного архива. Возвращает, сколько убрано.

        Возраст отбирает САМ ТРЕКЕР: свежих закрытых задач может оказаться
        больше страницы, и тогда просеивание на нашей стороне не дошло бы до
        старых никогда — обход крутился бы вхолостую каждые сутки, ничего об
        этом не говоря. У Linear это `or:[{completedAt:{lt}},{canceledAt:{lt}}]`,
        проверено на живой доске 12.09.2026.

        Спрашиваем закрытые и неархивные — то есть ровно тех, кого ещё не
        убрали. Отметок хранить не надо: сам трекер и есть память о сделанном.
        """
        ask = getattr(self.tracker, "closed_issues", None)
        if not (self.after_days and self.able and ask and self.team_id):
            return 0
        edge = (time.time() if now is None else now) - self.after_days * DAY_SEC
        try:
            issues = await asyncio.to_thread(ask, self.team_id, iso(edge))
        except Exception as e:
            log.warning("архив: закрытые задачи не спросить: %s", e)
            return 0
        done = 0
        for issue in issues:
            when = closed_at(issue)
            # Второй рубеж, свой: у трекера другой площадки отбор по дате
            # может значить не то же самое, а ноль здесь — «времени закрытия
            # не видно», и это не «давно».
            if not when or when > edge:
                continue
            if await self.archive(issue):
                done += 1
        return done

    async def run(self) -> None:
        """Обход раз в сутки. Заводится, только если задержка задана."""
        if not self.after_days:
            return
        if not self.team_id:
            log.error("архив: задержка %d дн. задана, но нет AGENT_TEAM_ID — "
                      "обход не завожу: чужие команды убирать нельзя",
                      self.after_days)
            return
        if not self.able:
            log.info("архив: трекер площадки не умеет убирать задачи — обхода нет")
            return
        log.info("архив: убираю закрытые старше %d дн., раз в %dс",
                 self.after_days, self.interval_sec)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Один кривой круг не должен останавливать уборку.
                log.exception("круг архива не удался")
            await asyncio.sleep(self.interval_sec)
