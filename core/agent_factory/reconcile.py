"""Сверки по факту: подобрать то, о чём события не было.

Здесь две, и обе про одно — состояние на доске разошлось с фактом, и
подобрать расхождение нечем. `Reconciler` спрашивает git, не влита ли работа
задачи, которая всё ещё ждёт человека. `Stalled` спрашивает рабочий каталог
и очередь, работает ли кто-нибудь над задачей, которая стоит «в работе».

──────────────────────────────────────────────────────────────────────────
Сверка мержей: подобрать то, о чём события не было.

Фабрика узнаёт о мерже из живого события хостинга (`deploy.Verification.
on_merge`) или из кнопки «Смержить» на странице подписи (`accept_merged`).
Оба пути — про НАСТОЯЩЕЕ. События, которого не было, не подберёт никто:
доставку не настроили, служба лежала, репозиторий подписали на
`pull_request` позже, чем смержили первые ветки. Задача остаётся в стопке
человека навсегда, и разбирает он её руками.

Так висела DEV-81: PR `beskrovnykh/agent-factory#16` (`agent/DEV-81-silent`)
влит 14.09.2026, репозиторий подписали позже, и на доске задача осталась в
«In Review». Раннер это ВИДЕЛ — «режим state, коммит a83699e1», то есть
коммит задачи найден в main, — и сделать не мог ничего: он читает состояние,
а не меняет его.

Отсюда третий путь, по факту, а не по событию: раз в час (и на старте)
пройти стопки человека и спросить у git, не лежит ли работа задачи уже в
основной ветке. Лежит — та же дорога в `Done`, что у события, и та же
запись в задаче.

Кто отвечает на вопрос «влито ли». Спрашиваем ТОЛЬКО git (`gitref.Repos.
locate`) — тот самый поиск, которым ворота выбирают режим:

  • он знает про НЕВЛИТУЮ работу. У DEV-66 в main лежит PR #3, а рядом
    открыт #22 с веткой `agent/DEV-66-v2` — работа задачи НЕ в main, и
    закрывать её по старому мержу значит выбросить доработку. `locate`
    ставит невлитую ветку выше влитой (там это правило и живёт), и сверка
    получает `ancestor=False` — то есть «не трогай»;
  • он одинаков для любого хостинга и не стоит запросов к API;
  • «не знаю» он говорит отдельно от «нет» (`Unavailable`), и на «не знаю»
    сверка не делает ничего: молчащий git не должен читаться как «не влито»
    — но и как «влито» тоже.

Хостинг (`hosting.Hosting.merged_request`) сверку НЕ решает — он только
дописывает в запись то, чего в git нет: номер PR, ссылку и кто смержил.
Нет хостинга или он старой сборки — задача всё равно закрывается, запись
просто короче, и это сказано в журнале, а не проглочено.

Дедуп тут не нужен отдельной меткой, и это не экономия, а свойство: перевод
в `Done` убирает задачу из стопки человека, то есть со следующего круга
сверка её уже не видит; а если увидит (лежачий трекер, гонка) — `_close_by_
merge` ответит «уже закрыта» и второго комментария не будет. Ровно та же
защита, что у кнопки мержа.

## Второй вопрос обхода: а сольётся ли то, что осталось (DEV-109)

Стопка «ждёт мержа» обещает, что нажатие сработает. 15.09.2026 оно не
сработало трижды подряд: за день в `main` уехали чужие правки тех же файлов,
и DEV-38, DEV-66, DEV-13 стали `CONFLICTING`. Задача, сливаемая в момент
проверки, протухает от чужого мержа, и до кнопки этого не видит никто.

Поэтому тот же обход, который ищет пропущенные мержи, задаёт второй вопрос
ровно тем задачам, чья работа в `main` НЕ лежит: вливается ли их ветка
(`mergeable.ask` → хостинг). Не вливается — задача уходит из стопки человека
к агенту, с причиной в комментарии, и в очередь ложится сессия разрешения
конфликта (`queueing.merge_task`, род `merge`). Круг работы это не считает:
род `merge` не входит в `runs.WORK_KINDS` — задача решена, конфликт ей
достался от чужого мержа, и отнимать за него круг не за что.

Обход будит и сам мерж (`Verification.after_merge` → `wake`): протухают
задачи не по часам, а в тот момент, когда в `main` что-то уехало. Между
сигналом и вопросом — пауза (`settle_sec`): GitHub пересчитывает
`mergeable` лениво и первые секунды отвечает «не знаю» (см. `mergeable`).

Кого обход к агенту НЕ отдаёт: задачу, у которой открытого PR нет (мержить
нечего), задачу, про которую хостинг промолчал, и задачу, уже назначенную на
агента, — захват `agent:started:<id>` тот же, что у вебхука и опроса, и
второй сессии он не даёт.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime

from . import mergeable, runs, sessions as sess
from .deploy import remember_task
from .gitref import Located, Unavailable
from .queueing import merge_task

log = logging.getLogger("intake")

# Как часто сверять, по умолчанию. Ноль — сверки нет вовсе.
#
# Час, а не минута: пропущенный мерж — это редкая авария доставки, а не
# рабочий ход, и лишний круг стоит по `ls-remote` на каждый клон площадки.
# Главный круг — первый, на старте: им и подбирается всё, что накопилось,
# пока событий не было.
DEFAULT_INTERVAL_SEC = 3600


def sec_from(raw, default: int, var: str) -> int:
    """Числовая ручка из окружения → секунды.

    Мусор — это умолчание со строкой в журнале, а не падение: переменные
    читаются на импорте, и `int("1ч")` уронил бы весь приём при старте.
    Правило то же, что у `archive.days_from`.
    """
    if raw in (None, ""):
        return default
    try:
        every = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("%s=%r — не число, беру %dс", var, raw, default)
        return default
    return max(0, every)


def every_from(raw) -> int:
    """Ручка `AGENT_RECONCILE_SEC` → секунды между кругами сверки мержей."""
    return sec_from(raw, DEFAULT_INTERVAL_SEC, "AGENT_RECONCILE_SEC")


INTERVAL_SEC = every_from(os.environ.get("AGENT_RECONCILE_SEC"))

# Сколько ждать после сигнала о мерже, прежде чем спрашивать сливаемость.
# Мерж в `main` сбрасывает `mergeable` у всех открытых PR, и пересчёт у
# GitHub занимает секунды: спросив мгновенно, получишь «не знаю» по всей
# стопке. Переспрос в `mergeable.ask` это добирает, пауза — чтобы не тратить
# попытки впустую.
DEFAULT_SETTLE_SEC = 20

# На сколько занимается ключ `agent:started:<id>` при постановке сессии
# разрешения конфликта. Тот же ключ, что у вебхука и опроса назначений:
# назначение агента, которое мы делаем следом, вебхук увидит занятым и
# второй сессии не заведёт. Час — с запасом на сессию; дольше держать нельзя,
# иначе следующий законный заход по задаче не поднимется.
STARTED_WINDOW_SEC = 3600


class Reconciler:
    """Обход стопок человека: работа в main → задача в `Done`; работа не
    вливается → задача к агенту (DEV-109)."""

    def __init__(self, tracker, verification, team_id: str,
                 states: tuple[str, ...] = (), repos=None, hosting=None,
                 interval_sec: int = INTERVAL_SEC, store=None, queue_key: str = "",
                 agent_id: str = "", stale_state: str = "",
                 settle_sec: float = DEFAULT_SETTLE_SEC):
        """`verification` — `deploy.Verification`: её `accept_merged` и есть
        дорога в `Done`, общая с кнопкой мержа. `repos` — `gitref.Repos`
        площадки; без него сверять нечем. `states` — имена состояний, в
        которых задача ждёт человека (те же две стопки, что считает тяга:
        на подписи и не прошедшие проверку).

        Для второго вопроса — «а сольётся ли?» — нужны ещё четверо, и без
        любого из них обход только сверяет мержи, сказав об этом в журнал:
        `hosting` (кто отвечает про сливаемость), `store` с `queue_key` (куда
        класть сессию разрешения конфликта), `agent_id` (на кого её
        назначить) и `stale_state` — состояние, куда уходит протухшая задача
        (`AGENT_UNVERIFIED_STATE_NAME`, то же, куда её кладёт маршрут по
        вердикту «не прошла, чинится»). Отдавать агенту, не уводя из стопки,
        нельзя: стопка ровно тем и врёт, что задача в ней лежит.
        """
        self.tracker = tracker
        self.verification = verification
        self.team_id = str(team_id or "")
        self.states = tuple(s for s in states if str(s).strip())
        self.repos = repos
        self.hosting = hosting
        self.interval_sec = int(interval_sec)
        self.store = store
        self.queue_key = str(queue_key or "")
        self.agent_id = str(agent_id or "")
        self.stale_state = str(stale_state or "").strip()
        self.settle_sec = float(settle_sec)
        # Сигнал «в main что-то уехало, пересмотри стопку сейчас». Событие, а
        # не отдельная задача на каждый мерж: два мержа подряд разбираются
        # одним кругом, и лишних походов в хостинг не будет.
        self._woken = asyncio.Event()

    def able(self) -> str:
        """Чего не хватает, чтобы сверять, — словами. Пусто — можно."""
        if self.repos is None:
            return "нет клонов площадки (AGENT_REPOS_ROOT)"
        if self.tracker is None or not callable(
                getattr(self.tracker, "issues_awaiting", None)):
            return "трекер не умеет перечислять задачи в стопках"
        if self.verification is None:
            return "проверка выката не собрана — переводить некому"
        if not self.team_id:
            return "нет AGENT_TEAM_ID — чужие команды сверять нельзя"
        if not self.states:
            return "не заданы состояния стопок человека"
        return ""

    def able_to_restack(self) -> str:
        """Чего не хватает, чтобы уводить протухшие задачи, — словами.
        Пусто — можно. Сверку мержей это не выключает: вопросы разные, и
        неумение ответить на второй не повод не задавать первый."""
        if self.hosting is None or not callable(
                getattr(self.hosting, "open_request", None)):
            return "хостинг не называет открытый PR — сливаемость не спросить"
        if self.store is None or not self.queue_key:
            return "некуда ставить сессию разрешения конфликта (нет очереди)"
        if not self.agent_id:
            return "не задан агент в трекере — кому отдавать конфликт"
        if not self.stale_state:
            return ("не задано состояние для протухших задач "
                    "(AGENT_UNVERIFIED_STATE_NAME)")
        return ""

    async def tick(self) -> int:
        """Один круг. Возвращает, сколько задач подобрано пропущенным мержем.

        Уведённые из стопки в этот счёт не идут: подбор мержа и пересмотр
        сливаемости — разные исходы, и складывать их в одно число значило бы
        читать в журнале «подобрано 3» там, где закрыта одна, а две ушли
        чинить конфликт.
        """
        why = self.able()
        if why:
            log.info("сверка мержей не идёт: %s", why)
            return 0
        try:
            issues = await asyncio.to_thread(
                self.tracker.issues_awaiting, self.team_id, self.states) or []
        except Exception as e:
            log.warning("сверка: стопки человека не спросить: %s", e)
            return 0
        cannot = self.able_to_restack()
        if cannot:
            log.info("пересмотр сливаемости не идёт: %s", cannot)
        done, stale = 0, 0
        for issue in issues:
            ident = str(issue.get("identifier") or "").strip()
            if not ident:
                continue
            state = str((issue.get("state") or {}).get("name") or "")
            located = await self._where(ident)
            if isinstance(located, Located) and located.ancestor:
                if await self.pick_up(ident, state, located):
                    done += 1
                continue
            if cannot or not isinstance(located, Located):
                # Ветки нет вовсе или git промолчал — сливаемость спрашивать
                # не о чем: мержить нечего, и PR по такой задаче не открыт.
                continue
            if await self.restack(issue, located):
                stale += 1
        if done:
            log.info("сверка: подобрано пропущенных мержей — %d из %d задач "
                     "в стопках человека", done, len(issues))
        if stale:
            log.warning("пересмотр: уведено из стопок человека как несливаемое "
                        "— %d из %d задач", stale, len(issues))
        return done

    async def _where(self, ident: str):
        """Где лежит работа задачи: `Located`, None (нигде) или None на
        «не знаем» — с разными строками в журнале. Один поиск на задачу: его
        ответ нужен обоим вопросам круга."""
        try:
            return await asyncio.to_thread(self.repos.locate, ident)
        except Unavailable as e:
            # «Не знаем» — не «не влито». Молчащий git не повод ни закрывать,
            # ни считать задачу разобранной: следующий круг спросит снова.
            log.info("сверка %s: где работа — не знаем (%s), не трогаю", ident, e)
            return None
        except Exception as e:
            log.warning("сверка %s: поиск работы не отработал: %s", ident, e)
            return None

    async def pick_up(self, ident: str, state: str = "", located=None) -> bool:
        """Одна задача: работа в main — закрыть, иначе не трогать."""
        if located is None:
            located = await self._where(ident)
        if not isinstance(located, Located) or not located.ancestor:
            return False
        # `ref` — куда влито, `source_ref` — откуда. Что из двух нашлось,
        # зависит от того, жива ли ветка: удалённая при мерже находится
        # мерж-коммитом в main (знаем «куда»), живая — самой веткой (знаем
        # «откуда»). Положить имя в оба поля значило бы напечатать в записи
        # «main → main»: пустое поле честнее, оно печатается как «?».
        on_main = located.ref == await asyncio.to_thread(
            self.repos.main_of, located.repo)
        facts = {"identifier": ident,
                 "ref": located.ref if on_main else "",
                 "source_ref": "" if on_main else located.ref,
                 "project_path": os.path.basename(located.repo.rstrip("/")),
                 "merge_commit_sha": located.sha}
        facts.update(await self._request(ident))
        log.info("сверка %s: работа лежит в основной ветке (коммит %s), а задача "
                 "в «%s» — события о мерже не было, подбираю", ident,
                 located.sha[:8] or "?", state or "?")
        outcome = await self.verification.accept_merged(ident, facts)
        return outcome == "done"

    async def _request(self, ident: str) -> dict:
        """Чем хостинг дополнит запись: номер PR, ссылка, кто смержил. Не
        ответил или не умеет — пусто, и это видно в журнале: запись без
        ссылки не должна выглядеть так, будто ссылки не было."""
        ask = getattr(self.hosting, "merged_request", None)
        if not callable(ask):
            log.info("сверка %s: хостинг не называет смерженный PR — запись "
                     "будет без ссылки", ident)
            return {}
        try:
            got = await asyncio.to_thread(ask, ident)
        except Exception as e:
            log.warning("сверка %s: хостинг не ответил про смерженный PR (%s) — "
                        "запись будет без ссылки", ident, e)
            return {}
        return {k: v for k, v in (got or {}).items() if v}

    async def restack(self, issue: dict, located) -> bool:
        """Одна задача, чья работа НЕ в main: вливается ли ветка. Не
        вливается — увести из стопки человека и отдать агенту.

        True — увели. «Вливается» и «не знаем» — False и ничего: уводить
        задачу из-за молчащего хостинга значит заводить сессию на ровном
        месте (и снимать с человека то, что он готов был смержить).
        """
        ident = str(issue.get("identifier") or "").strip()
        issue_id = str(issue.get("id") or "")
        answer, request = await mergeable.ask(self.hosting, ident)
        if answer is not False:
            return False
        why = mergeable.why_not(ident, request)
        started = f"agent:started:{issue_id}"
        try:
            # Тот же ключ, что у вебхука и опроса. Берём ПЕРВЫМ делом: следом
            # мы назначим агента, вебхук увидит это событие — и должен
            # упереться в занятый ключ, иначе на ту же задачу поднимутся две
            # сессии, причём вторая обычной работой, с потраченным кругом.
            if not await self.store.claim(started, STARTED_WINDOW_SEC):
                log.info("пересмотр %s: %s, но сессия по задаче уже идёт или "
                         "ставится — не трогаю", ident, why)
                return False
        except Exception as e:
            log.warning("пересмотр %s: захват не отработал (%s) — не ставлю",
                        ident, e)
            return False
        task = merge_task(issue, why, request, located.ref)
        try:
            await self.store.enqueue(self.queue_key,
                                     json.dumps(task, ensure_ascii=False))
        except Exception as e:
            log.error("пересмотр %s: сессия разрешения конфликта не поставлена: "
                      "%s — задачу из стопки не увожу", ident, e)
            try:
                await self.store.release(started)
            except Exception:
                pass
            return False
        await remember_task(self.store, task)
        log.warning("пересмотр %s: %s — увожу из стопки «%s» и отдаю агенту "
                    "(род merge, круг работы не считается)", ident,
                    why, str((issue.get("state") or {}).get("name") or "?"))
        await self._tell(issue, why, request)
        return True

    async def _tell(self, issue: dict, why: str, request: dict) -> None:
        """Сказать трекеру то же, что журналу: задача уходит из стопки к
        агенту, и почему. Осечка любого из трёх шагов — строка в журнал, а не
        отказ: сессия уже в очереди, и она переведёт задачу сама."""
        ident = str(issue.get("identifier") or "")
        issue_id = str(issue.get("id") or "")
        was = str((issue.get("state") or {}).get("name") or "?")
        link = f" ([PR #{request['number']}]({request['url']}))" if (
            request.get("number") and request.get("url")) else ""
        body = (f"🔀 **Из стопки «ждёт мержа» — на разрешение конфликта.** "
                f"{why}{link}. Кнопка мержа на этой задаче сейчас ответила бы "
                f"отказом, поэтому она уходит из `{was}` в `{self.stale_state}` "
                f"на агента: влить основную ветку, разобрать конфликты, "
                f"дождаться зелёного пайплайна и сдать заново. Работа уже "
                f"сделана и проверена — круг по потолку "
                f"`AGENT_MAX_WORK_ROUNDS` этот заход не считает.")
        # Комментарий первым: перевод и назначение видны на доске сами, а
        # причина — только здесь. Упади трекер посередине — человек увидит
        # объяснение, а не молча переехавшую задачу.
        await self._try(ident, "комментарий", "comment", issue_id, body)
        await self._try(ident, "перевод", "set_state", issue_id, self.stale_state)
        await self._try(ident, "назначение", "assign", issue_id, self.agent_id)

    async def _try(self, ident: str, what: str, method: str, *args) -> None:
        call = getattr(self.tracker, method, None)
        if not callable(call):
            log.warning("пересмотр %s: трекер не умеет «%s» (%s) — сделай руками",
                        ident, what, method)
            return
        try:
            await asyncio.to_thread(call, *args)
        except Exception as e:
            log.warning("пересмотр %s: %s не прошёл: %s", ident, what, e)

    async def wake(self, why: str = "") -> None:
        """«В main что-то уехало — пересмотри стопку». Зовёт мерж (событие
        хостинга и кнопка на странице подписи, `Verification.after_merge`).

        Ставит сигнал и возвращается немедленно: мерж не должен ждать обхода
        стопки, а обход не должен запускаться по разу на каждый мерж — два
        мержа подряд разберутся одним кругом.
        """
        log.info("пересмотр стопки: разбудили%s", f" — {why}" if why else "")
        self._woken.set()

    async def _nap(self) -> None:
        """Пауза до следующего круга: либо срок, либо сигнал о мерже. По
        сигналу ждём ещё `settle_sec` — хостингу нужно время пересчитать
        сливаемость после мержа, и вопрос, заданный раньше, вернёт «не знаю»
        по всей стопке."""
        try:
            await asyncio.wait_for(self._woken.wait(), timeout=self.interval_sec)
        except asyncio.TimeoutError:
            return
        self._woken.clear()
        if self.settle_sec > 0:
            await asyncio.sleep(self.settle_sec)

    async def run(self) -> None:
        """Круг на старте, дальше раз в `interval_sec` или по сигналу мержа."""
        if not self.interval_sec:
            return
        why = self.able()
        if why:
            log.info("сверка мержей выключена: %s", why)
            return
        cannot = self.able_to_restack()
        log.info("сверка мержей: смотрю стопки (%s) раз в %dс и после каждого "
                 "мержа, первый круг сейчас; сливаемость %s",
                 ", ".join(self.states), self.interval_sec,
                 f"не пересматриваю ({cannot})" if cannot else "пересматриваю тут же")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Один кривой круг не должен выключать сверку навсегда:
                # именно молчание и есть та беда, которую она чинит.
                log.exception("круг сверки мержей не удался")
            await self._nap()


# ── сверка «в работе»: статус есть, а сессии нет ───────────────────────────
#
# `In Progress` означает «агент работает». 16.09.2026 в нём висели три
# задачи, и не работал никто: у DEV-44 сессия кончилась за четыре часа до
# того с `исход=FAILED`, а у DEV-50 и DEV-37 рабочего каталога не было
# вовсе — статус поставили, работа не начиналась. Два разных провала под
# одним статусом, и отличить их на доске нечем: она в обоих случаях говорит
# «в работе».
#
# Почему упавшая не вернулась сама. Раннер её ВИДЕЛ и отработал: написал в
# задачу причину и передал принимающему (`linear_assign_to_reviewer`). Но
# исполнитель — не статус: задача осталась в «In Progress», теперь ещё и на
# человеке. А комментарий с причиной в тот раз не доехал совсем — ключ
# личности отдал 401 («комментарий НЕ записан — текст потерян», 20:33:08), и
# от всей аварии на доске не осталось ни следа. Статус раннер теперь двигает
# сам (`runner.hand_failed_to_human`), но на него одного полагаться нельзя:
# он и есть тот, кто упал.
#
# Отсюда вторая сверка, симметричная сверке мержей: раз в час спросить не
# доску, а ФАКТ — рабочий каталог раннера и очередь. Никто не работает и
# никто не собирается — увести из «в работе», назвав причину.

# Сколько задача должна молчать, прежде чем считать её зависшей.
#
# Час, а не минута: между постановкой в очередь и стартом сессии проходят
# десятки минут (очередь глубиной 4-5 задач — норма), и сверка, срабатывающая
# раньше, отбирала бы задачи у тех, кто ещё только ждёт слот. Очередь мы
# спрашиваем отдельно, но окно между «задачу забрали из очереди» и «каталог
# сессии появился» ничем, кроме срока, не закрыть.
DEFAULT_QUIET_SEC = 3600
QUIET_SEC = sec_from(os.environ.get("AGENT_STALLED_QUIET_SEC"),
                     DEFAULT_QUIET_SEC, "AGENT_STALLED_QUIET_SEC")

# Исходы, при которых задача остаётся «в работе» НАМЕРЕННО: сессия кончилась,
# но заход по ней ещё будет — раннер ждёт зелёных проверок (`WAITING_CI`) или
# сброса лимита Claude (`WAITING_LIMIT`), и в обоих случаях «в работе» — это и
# есть правда. Трогать такую задачу значит отобрать её у собственного
# ожидания: продолжение вернётся в ветку, а задача будет уже у человека.
WAITING_OUTCOMES = ("WAITING_CI", "WAITING_LIMIT")

# Маршруты. Пусто — не трогаем.
TO_HUMAN = "human"
TO_TODO = "todo"


def _stamp_of(issue: dict) -> float:
    """Когда задачу трогали последний раз, секундами. Не разобрали — `now`.

    Метка приблизительная: `updatedAt` двигает любая правка, а не только
    смена статуса, — то есть срок молчания она скорее ЗАНИЖАЕТ, и сверка
    лишний круг подождёт. Ошибка в эту сторону и нужна: цена промаха здесь —
    задача, отобранная у живой работы.

    Не разобрали метку — возвращаем «только что»: неизвестные часы читаются
    как свежие, и задача остаётся на месте. Ноль тут значил бы «молчит с
    начала времён», то есть «трогай немедленно».
    """
    raw = str(issue.get("updatedAt") or "").strip()
    if not raw:
        return time.time()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        log.warning("сверка «в работе» %s: не разобрать updatedAt=%r — считаю "
                    "задачу свежей", issue.get("identifier") or "?", raw)
        return time.time()


def owner_of(issue: dict, agent_user_id: str) -> str:
    """Кто держит задачу: `""` — никто, `"agent"` — агент, `"human"` — человек.

    Всё, что не опознано как агент, считается человеком — включая задачу, у
    которой исполнитель есть, а id его нам не отдали. Направление ошибки
    выбрано: сверка промолчит там, где могла бы сработать, но не заберёт
    задачу у того, кто, может быть, ею занят.
    """
    who = issue.get("assignee") or {}
    if not who:
        return ""
    if agent_user_id and str(who.get("id") or "") == str(agent_user_id):
        return "agent"
    return "human"


def route_of(issue: dict, sessions: list[dict], *, queued: bool, now: float,
             quiet_sec: int, agent_user_id: str = "") -> tuple[str, str]:
    """Куда задачу из «в работе» — и почему. Чистая функция, без трекера.

    Порядок веток — порядок доверия факту. Сначала то, что означает «работа
    идёт или вот-вот пойдёт» (очередь, живая сессия, объявленное ожидание), и
    только потом сроки. `sessions` — то, что отдал `sessions.walk`: заходы
    ВСЕХ задач, фильтр по имени здесь.
    """
    ident = str(issue.get("identifier") or "")
    if queued:
        return "", "стоит в очереди"
    mine = [s for s in sessions if str(s.get("task") or "") == ident]
    if any(s.get("идёт") for s in mine):
        return "", "сессия идёт"
    quiet = now - _stamp_of(issue)
    if mine:
        last = max(mine, key=lambda s: float(s.get("touched") or 0))
        outcome = str(last.get("исход") or "")
        if outcome in WAITING_OUTCOMES:
            return "", f"заход кончился как {outcome} — ожидание за раннером"
        # Из двух часов берём МЕНЬШИЙ срок молчания: заход мог кончиться
        # вчера, а задачу могли переназначить пять минут назад — и тогда
        # свежий заход по ней ещё не успел появиться.
        quiet = min(quiet, now - float(last.get("touched") or 0))
        if quiet < quiet_sec:
            return "", f"молчит всего {runs.human_dur(quiet)}"
        what = (f"исход `{outcome}`" if outcome else
                "исхода нет — заход оборвался, не оставив итоговой записи")
        return TO_HUMAN, (f"последний заход `{last.get('name')}` кончился "
                          f"{runs.human_dur(now - float(last.get('touched') or 0))} "
                          f"назад, {what}")
    owner = owner_of(issue, agent_user_id)
    if owner == "human":
        # Каталога нет, а задача на человеке — может, он ею и занят сам.
        # Доказательства, что это заход агента, у нас нет никакого: рабочего
        # каталога, по которому это было бы видно, не существует.
        return "", "каталога нет, но задача на человеке"
    if quiet < quiet_sec:
        return "", f"молчит всего {runs.human_dur(quiet)}"
    return TO_TODO, (f"сессии не было ни одной — рабочего каталога нет, в "
                     f"очереди задачи нет, молчит {runs.human_dur(quiet)}")


def _body(route: str, why: str, state: str, to: str, *, handed: bool = False,
          owner: str = "") -> str:
    """Запись в задачу: что сверка увидела, что сделала и что дальше.

    Последняя строка — единственное, что человеку придётся делать руками, и
    она обязана быть правдой про сделанное, а не про задуманное. Поэтому
    `handed` (дался ли исполнитель) и `owner` (на ком задача была) приходят
    сюда фактами: обещать передачу, которой не вышло, хуже, чем не обещать.
    """
    head = (f"🤖 Сверка «в работе»: задача стоит в «{state}», а сессии по ней "
            f"нет — {why}.\n\n")
    if route == TO_HUMAN:
        return head + (
            f"Сессия не вернула задачу сама: так бывает, когда она упала "
            f"раньше, чем успела отчитаться, или когда трекер не принял её "
            f"запись. Перевожу в «{to}» — статус «в работе» без работающего "
            f"врёт про состояние доски."
            + (" Задача на принимающем.\n\n" if handed else
               " Исполнителя сменить не удалось — задача в стопке без "
               "него.\n\n")
            + "Что дальше: посмотреть лог захода на хосте и решить — ещё "
              "один круг агенту или разбор руками.")
    return head + (
        f"Статус говорит «агент работает», работать некому: сессии по задаче "
        f"не поднималось ни одной. Возвращаю в «{to}» — оттуда её можно "
        f"начать по-настоящему."
        + ("\n\nИсполнителя не трогаю: задача осталась на агенте, а "
           "назначение — это и есть команда «начни работу». Чтобы круг "
           "начался, назначь заново."
           if owner == "agent" else
           " Назначить агента или дать взять тягой."))


class Stalled:
    """Обход состояния «в работе»: статус есть, а сессии нет.

    Что спрашиваем и у кого:

      • ОЧЕРЕДЬ (`queued`) — единственный ответ на «сессии ещё нет, но она
        будет». Без него сверка отбирала бы задачи у тех, кто ждёт слот,
        поэтому без него она и не работает вовсе (`able`);
      • РАБОЧИЙ КАТАЛОГ (`sessions.walk`) — был ли заход и чем кончился. Это
        тот же источник, по которому считается журнал запусков: файлы
        раннера, а не статусы в трекере;
      • ТРЕКЕР — только список задач в состоянии «в работе» и метка
        последней правки.

    Дедуп — свойство, а не метка: перевод уводит задачу из «в работе», то
    есть со следующего круга сверка её уже не видит. Порядок действий тот
    же: сначала перевод, потом запись. Упади трекер посередине — задача
    уедет без объяснения (оно останется в журнале), и это лучше, чем
    комментарий каждый час по задаче, которую перевести не удалось.
    """

    def __init__(self, tracker, work_root: str, team_id: str, *,
                 states: tuple[str, ...] = (), review_state: str = "",
                 todo_state: str = "", agent_user_id: str = "",
                 reviewer_id=None, queued=None, quiet_sec: int = QUIET_SEC,
                 interval_sec: int = INTERVAL_SEC):
        """`queued` — чем спросить очередь: функция без аргументов, отдающая
        имена задач, которые фабрика ещё собирается взять (можно
        асинхронная). `reviewer_id` — кто принимает: строка или функция, как
        у вебхука (делегирование может смениться под руками)."""
        self.tracker = tracker
        self.work_root = str(work_root or "")
        self.team_id = str(team_id or "")
        self.states = tuple(s for s in states if str(s).strip())
        self.review_state = str(review_state or "").strip()
        self.todo_state = str(todo_state or "").strip()
        self.agent_user_id = str(agent_user_id or "")
        self.reviewer_id = reviewer_id
        self.queued = queued
        self.quiet_sec = int(quiet_sec)
        self.interval_sec = int(interval_sec)

    def able(self) -> str:
        """Чего не хватает, чтобы сверять, — словами. Пусто — можно."""
        if self.tracker is None or not callable(
                getattr(self.tracker, "issues_awaiting", None)):
            return "трекер не умеет перечислять задачи в состоянии"
        if not self.team_id:
            return "нет AGENT_TEAM_ID — чужие команды сверять нельзя"
        if not self.states:
            return "не заданы состояния работы (SIGNING_WORKING_STATES)"
        if not self.work_root:
            return "нет AGENT_WORK_ROOT — заходы считать негде"
        if not callable(self.queued):
            return ("нечем спросить очередь — ждущую слот задачу не отличить "
                    "от брошенной")
        if not (self.review_state and self.todo_state):
            return "не заданы состояния, куда уводить задачу"
        return ""

    async def _in_queue(self) -> set[str]:
        got = self.queued()
        if asyncio.iscoroutine(got) or asyncio.isfuture(got):
            got = await got
        return {str(x) for x in (got or ())}

    async def _reviewer(self) -> str:
        who = self.reviewer_id
        if callable(who):
            who = who()
            if asyncio.iscoroutine(who) or asyncio.isfuture(who):
                who = await who
        return str(who or "")

    async def tick(self) -> int:
        """Один круг. Возвращает, сколько задач уведено из «в работе»."""
        why = self.able()
        if why:
            log.info("сверка «в работе» не идёт: %s", why)
            return 0
        try:
            issues = await asyncio.to_thread(
                self.tracker.issues_awaiting, self.team_id, self.states) or []
        except Exception as e:
            log.warning("сверка «в работе»: доску не спросить: %s", e)
            return 0
        if not issues:
            return 0
        try:
            queued = await self._in_queue()
        except Exception as e:
            log.warning("сверка «в работе»: очередь не спросить (%s) — круг "
                        "пропускаю целиком", e)
            return 0
        try:
            os.listdir(self.work_root)
        except OSError as e:
            # Нечитаемый каталог отдаёт пустой список заходов, а пустой список
            # означает «сессии не было ни разу» — то есть все задачи разом
            # уехали бы в Todo. Круг без каталога не имеет смысла вовсе.
            log.warning("сверка «в работе»: рабочий каталог %s не прочитан "
                        "(%s) — круг пропускаю целиком", self.work_root, e)
            return 0
        sessions = await asyncio.to_thread(sess.walk, self.work_root)
        now = time.time()
        moved = 0
        for issue in issues:
            ident = str(issue.get("identifier") or "").strip()
            if not ident:
                continue
            route, why = route_of(issue, sessions, queued=ident in queued,
                                  now=now, quiet_sec=self.quiet_sec,
                                  agent_user_id=self.agent_user_id)
            if not route:
                log.debug("сверка «в работе» %s: не трогаю (%s)", ident, why)
                continue
            if await self.pick_up(issue, route, why):
                moved += 1
        if moved:
            log.info("сверка «в работе»: уведено задач без сессии — %d из %d "
                     "в работе", moved, len(issues))
        return moved

    async def pick_up(self, issue: dict, route: str, why: str) -> bool:
        """Одна задача: перевод, потом запись. True — переехала."""
        ident = str(issue.get("identifier") or "")
        issue_id = str(issue.get("id") or "")
        state = str((issue.get("state") or {}).get("name") or "")
        to = self.review_state if route == TO_HUMAN else self.todo_state
        log.info("сверка «в работе» %s: %s → «%s» (%s)", ident, state or "?",
                 to, why)
        try:
            await asyncio.to_thread(self.tracker.set_state, issue_id, to)
        except Exception as e:
            log.warning("сверка «в работе» %s: статус не сменить (%s) — и "
                        "записи не делаю: без перевода она повторится на "
                        "следующем круге", ident, e)
            return False
        handed = (await self._hand_over(ident, issue_id)
                  if route == TO_HUMAN else False)
        try:
            await asyncio.to_thread(
                self.tracker.comment, issue_id,
                _body(route, why, state or "в работе", to, handed=handed,
                      owner=owner_of(issue, self.agent_user_id)))
        except Exception as e:
            # Задача уже переехала, и второго круга по ней не будет: причина
            # останется только здесь. Поэтому не «не записали», а сама
            # причина в строке журнала.
            log.error("сверка «в работе» %s: запись не легла в задачу (%s) — "
                      "причина перевода только тут: %s", ident, e, why)
        return True

    async def _hand_over(self, ident: str, issue_id: str) -> bool:
        """Принимающий на упавшей задаче. True — дался.

        Не вышло — не беда: статуса достаточно, задача уже не выглядит идущей
        и видна в стопке. Но в записи это будет сказано, а не проглочено.
        """
        if not hasattr(self.tracker, "assign"):
            return False
        try:
            who = await self._reviewer()
        except Exception as e:
            log.warning("сверка «в работе» %s: кто принимает — не спросить: %s",
                        ident, e)
            return False
        if not who:
            log.warning("сверка «в работе» %s: принимающий не задан — задача "
                        "уходит в стопку без исполнителя", ident)
            return False
        try:
            await asyncio.to_thread(self.tracker.assign, issue_id, who)
        except Exception as e:
            log.warning("сверка «в работе» %s: исполнителя не сменить: %s",
                        ident, e)
            return False
        return True

    async def run(self) -> None:
        """Круг на старте, дальше раз в `interval_sec`."""
        if not self.interval_sec:
            return
        why = self.able()
        if why:
            log.info("сверка «в работе» выключена: %s", why)
            return
        log.info("сверка «в работе»: смотрю «%s» раз в %dс, зависшей считаю "
                 "задачу, которая молчит дольше %s, первый круг сейчас",
                 ", ".join(self.states), self.interval_sec,
                 runs.human_dur(self.quiet_sec))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("круг сверки «в работе» не удался")
            await asyncio.sleep(self.interval_sec)
