"""Готовый черновик → задача в трекере → карточка с кнопками.

Вторая половина приёма. Первая (`intake.py` + транспорт) собирает пачку и
кладёт её в очередь агента; сессия-приёмщик пишет черновик и возвращает его
через список в redis. Здесь черновик превращается в задачу и в карточку, под
которой человек жмёт «Отдать агенту» или «Удалить».

Почему заводит задачу не раннер, а этот процесс: ключ трекера и кнопки под
карточкой — одно хозяйство, и создание должно быть там же, где разбираются
клики по нему (`cards.py`). Раннер про мессенджер не знает вовсе.

Почему «Отдать агенту» ставит задачу в очередь прямо, а не ждёт вебхука от
трекера: инсталляция не обязана быть достижимой снаружи. Вебхук
(`webhook.py`) — обычный путь для тех, у кого адрес есть, а не условие работы
продукта.

Соседи, выехавшие отсюда 12.09.2026 (DEV-68): адаптер трекера —
`tracking.py`, правила постановки в очередь — `queueing.py`, клики по
кнопкам — `cards.py`. Имена ниже реэкспортированы: инсталляция первого
клиента импортирует их отсюда, и её сборка не должна упасть в момент выката.
Новый код берёт их из родных модулей.
"""
from __future__ import annotations

import asyncio
import json
import logging

from . import intake
from .cards import Cards
from .queueing import (SYMPTOM_MAX, has_incident_label, label_names,
                       symptom_of, work_task)
from .tracking import LinearTracker, Tracker

# Переходные реэкспорты: имена не используются в этом файле, но обязаны из
# него импортироваться.
__all__ = ["Cards", "LinearTracker", "Results", "SYMPTOM_MAX", "Tracker",
           "dispatch", "drain", "has_incident_label", "label_names",
           "symptom_of", "work_task"]

log = logging.getLogger("intake")


# ── разбор результатов ───────────────────────────────────────────────────

class Results:
    """Черновики от раннера → задача в трекере и карточка в личке."""

    def __init__(self, intake_, store, transport, tracker: Tracker,
                 notify_chat: str = "", progress=None, on_created=None,
                 project_id: str = "", duty=None):
        """`project_id` — в какой проект трекера складывать заведённое. Пусто
        — задача создаётся без проекта, как было; у площадки без проектов это
        единственный возможный исход."""
        self.intake = intake_
        self.project_id = str(project_id or "")
        # Присмотр за инцидентом без исполнителя (`duty.Duty`). Нет — инцидент
        # лежит молча, как было до 12.09.2026.
        self.duty = duty
        self.store = store
        self.transport = transport
        self.tracker = tracker
        self.notify_chat = str(notify_chat)
        # Счётчик необязателен: без него исходы уходят обычными сообщениями,
        # как было до него. Но если он есть — исход обязан лечь на его место,
        # иначе рядом с ответом останется тикающая строчка.
        self.progress = progress
        # Инсталляция может захотеть запомнить что-то о заведённой задаче —
        # например, кто её принёс, чтобы закрытая задача вернулась к нему, а
        # не к владельцу ключа трекера. Продукту это знать незачем.
        self.on_created = on_created

    async def _remember_card(self, chat_id, card_id, issue: dict) -> None:
        """Связь «сообщение карточки → задача», на срок `ANSWER_TTL_SEC`.

        Сбой связи не должен ронять приём: задача заведена, карточка ушла, и
        это главное. Потерянная связь стоит одного ответа, ушедшего в пачку, —
        человек увидит лишнюю задачу и поймёт. Упавший приём стоит дороже.
        """
        if not card_id:
            log.warning("карточка %s отправлена без идентификатора — ответ на "
                        "неё в задачу не доедет", issue.get("identifier"))
            return
        try:
            await self.store.put(
                intake.answer_key(self.intake.source, chat_id, str(card_id)),
                json.dumps({"id": issue["id"],
                            "identifier": issue.get("identifier") or ""},
                           ensure_ascii=False),
                intake.ANSWER_TTL_SEC)
        except Exception as e:
            log.warning("связь «карточка → %s» не запомнена: %s",
                        issue.get("identifier") or issue["id"], e)

    async def handle(self, result: dict) -> str:
        chat_id = result.get("chat_id")
        draft_id = result.get("draft_id") or ""
        if not (chat_id and draft_id):
            log.warning("результат без адреса, пропускаю: %r", str(result)[:200])
            return "no-address"

        if not result.get("ok"):
            # Провал сессии показываем человеку: он ждёт черновик и без ответа
            # будет ждать бесконечно.
            why = result.get("error") or "причина неизвестна"
            await self._fail(chat_id, draft_id, why,
                             limit=bool(result.get("limit")),
                             limit_reset=result.get("limit_reset") or "")
            return "failed"

        draft = result.get("draft") or {}
        items = result.get("items") or []

        if intake.reject_reason(draft):
            # Планка «из этого задачи не выходит»: предмета нет, значит и
            # задачи нет. Фактуру возвращаем в пачку — человек допишет
            # недостающее и соберёт ещё раз, не пересылая переписку заново.
            # Порядок важен: счётчик, объяснение, и только потом пачка —
            # сообщение с кнопкой «Собрать задачу» должно остаться внизу
            # ленты, иначе до него придётся скроллить.
            await self._done(draft_id, "Разобрал")
            await self.transport.send(chat_id, intake.reject_text(draft))
            await self._restore(chat_id, items)
            log.info("постановка %s → отказ, фактура возвращена в пачку", draft_id)
            return "rejected"

        team_id = (draft.get("team_id") or "").strip()
        if not team_id:
            # Без команды задачу не создать. Это провал сборки, а не особый
            # исход — тем же путём, что и все прочие.
            await self._fail(chat_id, draft_id,
                             "сессия не назвала команду в трекере")
            return "no-team"

        try:
            issue = await asyncio.to_thread(
                self._create, team_id, draft, items)
        except Exception as e:
            log.warning("создание задачи не прошло: %s", e)
            await self._fail(chat_id, draft_id, f"трекер отказал ({e})")
            return "create-failed"

        # Разбор приёма — отдельным комментарием и ДО карточки: человек
        # открывает задачу по ссылке из карточки, и решение приёма должно
        # ждать его там, а не приехать следом.
        ref = issue.get("identifier") or issue["id"]
        if not hasattr(self.tracker, "comment"):
            # Отдельно от отказа трекера: `comment` добавлен в протокол позже
            # остальных методов, и у стороннего адаптера его может не быть
            # вовсе. Такой AttributeError в общем `except` читался бы как
            # «трекер отказал», и чинить пошли бы не туда.
            log.warning("адаптер трекера (%s) не умеет comment — разбор приёма "
                        "в %s не записан", type(self.tracker).__name__, ref)
        else:
            try:
                await asyncio.to_thread(self.tracker.comment, issue["id"],
                                        intake.intake_notes(draft))
            except Exception as e:
                # Задача заведена, и это главное. Разбор — то, по чему видно
                # ошибку приёма; без него задача просто выглядит как раньше.
                log.warning("разбор приёма не записан в %s: %s", ref, e)

        if self.on_created:
            try:
                await self.on_created(issue, chat_id, draft, items)
            except Exception as e:
                # Заметка на полях не стоит потерянной карточки: задача уже
                # создана, и человек обязан получить на неё ссылку.
                log.warning("хук on_created не отработал: %s", e)
        # Счётчик — в подпись «задача заведена за X», и только потом карточка:
        # она придёт следующим сообщением, прямо под ней.
        await self._done(draft_id, "Задача заведена")
        card_id = await self.transport.send(chat_id, intake.card_text(issue, draft),
                                            intake.issue_keyboard(issue["id"]))
        # Ответ на карточку должен доехать в ЭТУ задачу. Без связи он ложится
        # в пачку и становится второй задачей — а именно ответа мы и просим,
        # когда приёмщик кладёт в карточку `❓`.
        await self._remember_card(chat_id, card_id, issue)
        log.info("постановка %s → %s, чат %s", draft_id,
                 issue.get("identifier"), chat_id)
        # Инцидент берём под присмотр: карточка отправлена, но пока на задаче
        # нет исполнителя, работа не идёт — и об этом надо напоминать.
        if self.duty and str(draft.get("role") or "").strip().lower() == "incident":
            try:
                await self.duty.remember(issue, chat_id,
                                         (draft.get("title") or "").strip())
            except Exception as e:
                log.error("инцидент %s под присмотр не взят: %s — напоминаний "
                          "не будет", issue.get("identifier"), e)
        if self.notify_chat:
            await self.transport.send(
                self.notify_chat,
                f"📝 Заведена {issue.get('identifier')} — "
                f"{(draft.get('title') or '').strip()}\n{issue.get('url') or ''}")
        return "created"

    async def _done(self, draft_id: str, caption: str) -> None:
        if self.progress:
            await self.progress.done(draft_id, caption)

    def _create(self, team_id: str, draft: dict, items: list[dict]) -> dict:
        """Создание задачи. Проект передаём ТОЛЬКО когда он задан: трекер
        инсталляции может про проекты не знать вовсе (у первого клиента свой
        адаптер), и лишний аргумент сломал бы ему создание вообще.

        Проект берётся из НАСТРОЙКИ, а не из черновика. В контракте черновика
        (`core/prompt.py`) такого поля нет, писать его некому — а если завести,
        значение придёт от модели, то есть может быть выдумано, и трекер на
        неизвестный проект отказывает в создании ЦЕЛИКОМ: вместо задачи не в том
        проекте человек не получит задачи вовсе. Появится выбор проекта по
        предмету (маркетинг отдельно от разработки — за этим проекты и
        заводились) — поле надо описать в контракте И завести откат на
        настройку, когда трекер его не принял.
        """
        args = (team_id, draft.get("title") or "Без названия",
                intake.issue_description(draft, items),
                str(draft.get("role") or "").strip().lower())
        if self.project_id:
            return self.tracker.create_issue(*args, project_id=self.project_id)
        return self.tracker.create_issue(*args)

    async def _fail(self, chat_id, draft_id: str, reason: str,
                    limit: bool = False, limit_reset: str = "") -> None:
        """Сказать о провале на месте счётчика, а если счётчика нет — обычным
        сообщением. Молчание человек прочитает как «бот сломался»."""
        if self.progress and await self.progress.failed(
                draft_id, reason, limit=limit, limit_reset=limit_reset):
            return
        tail = ("Раньше сброса лимита повторять бесполезно."
                + (f" Сброс в {limit_reset}." if limit_reset else "")) if limit else \
               "Фактура не потеряна — перешли заново или повтори."
        await self.transport.send(
            chat_id, f"⚠️ Задача не собралась: {reason}.\n{tail}")

    async def _restore(self, chat_id, items: list[dict]) -> None:
        """Пачку — обратно, вместе с кнопкой под ней.

        Возврат делает сам приём: у него потолок пачки, слияние с досланным и
        замок на чат. Здесь только чистка `path` — это файл в рабочем каталоге
        сессии, которого уже нет.
        """
        clean = [{k: v for k, v in it.items() if k != "path"} for it in items]
        dropped = await self.intake.restore(chat_id, clean)
        if dropped:
            await self.transport.send(
                chat_id, f"⚠️ {dropped} кусков не влезли в пачку (потолок "
                         f"{self.intake.max_items}) — перешли их заново, если нужны.")

    async def run(self) -> None:
        """Разбор для инсталляции с одним мессенджером. С несколькими —
        `drain` на всех сразу: список результатов один, и два читателя на нём
        разобрали бы чужое."""
        await drain(self.store, {self.intake.source: self})


async def dispatch(result: dict, by_source: dict[str, "Results"]) -> str:
    """Отдать результат разбору того мессенджера, откуда пришла пачка.

    Источник лежит в самих кусках (`intake.source_of`): раннер возвращает
    их как есть. Один мессенджер берёт всё — куски, положенные до появления
    источника, иначе ответили бы в никуда. С несколькими незнакомый источник
    НЕ отдаётся первому попавшемуся: id чатов в Telegram и MAX — числа из
    разных пространств, и пересланная человеком переписка уехала бы в чужой
    чат другого мессенджера.
    """
    if len(by_source) == 1:
        return await next(iter(by_source.values())).handle(result)
    source = intake.source_of(result)
    results = by_source.get(source)
    if results is None:
        log.error("результат %s из «%s» — такого мессенджера нет (есть: %s), "
                  "не отвечаю", result.get("draft_id") or "?", source,
                  ", ".join(sorted(by_source)))
        return "no-channel"
    return await results.handle(result)


async def drain(store, by_source: dict[str, "Results"]) -> None:
    """Один читатель списка результатов на все мессенджеры инсталляции.

    Раннер кладёт черновики в один список, из какого бы мессенджера ни
    пришла пачка. Читатель поэтому тоже один: два `pop_blocking` на одном
    ключе делили бы результаты случайно, и половина ответов уходила бы не
    в тот мессенджер.
    """
    if not by_source:
        raise ValueError("разбору результатов не дали ни одного мессенджера")
    log.info("разбор результатов запущен: ключ %s, мессенджеры %s",
             intake.RESULTS_KEY, ", ".join(sorted(by_source)))
    while True:
        try:
            raw = await store.pop_blocking(intake.RESULTS_KEY, 30)
            if not raw:
                continue
            await dispatch(json.loads(raw), by_source)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Один кривой результат не должен останавливать разбор.
            log.exception("результат не разобрался")
            await asyncio.sleep(5)
