"""Канарейка маршрута: фабрика раз в сутки прогоняет себя по собственному пути.

Модульные тесты проверяют компоненты, проверяющие сессии — предмет задачи.
Сам маршрут — заведение → работа → проверка → подпись → мерж → закрытие — не
проверяет никто, и три его поломки за 14–15.09.2026 нашлись случайно:

* датчик логов сообщал находку кодом возврата, а его съедала труба: 34 вызова
  за три месяца, ни один не сработал;
* досдача ждала не тот статус и утаскивала к человеку правильно сданные задачи;
* перевода в `Done` по мержу не существовало вовсе, и событие о мерже
  физически не доходило.

Каждая выглядела работающей, ни одна не уронила тест. Общее у всех трёх одно:
сломан был не расчёт, а дорога, и увидеть это можно было только проехав по ней
целиком.

Отсюда канарейка. **Предмет её задачи тривиален намеренно** — одна строка в
файле-пустышке: измеряется не работа, а то, доехала ли задача и за сколько
заходов. Канарейка, падающая из-за сложности собственной задачи, не измеряет
ничего, кроме себя.

Что здесь ПРОДУКТ: сам разбор маршрута — стадии, сроки, счёт кругов, тревога.
Он одинаков у любой инсталляции. Что приносит инсталляция: репозиторий-пустышка
и файл в нём, час прогона, куда писать тревогу. Боевых репозиториев канарейка
не касается вовсе — ни читать, ни мержить в них она не умеет (`_mergeable`).

## Три решения, которые стоит знать

**Стадию узнаём по ИМЕНИ состояния, а тип — только запасной путь.** У Linear
`In Progress`, `Verifying`, `In Review` и `Not Verified` — один и тот же тип
`started`, поэтому по типу маршрут не разложить. Но имя может и смениться:
тогда закрытые типы (`completed`/`canceled`) всё же дадут «доехала»/«снята».
Незнакомое имя — это НЕ «ничего не произошло», а отдельный исход: разбор
говорит «не знаю такой стадии» вслух. Молчаливое «не узнал» превратило бы
канарейку в зелёный огонёк на любой поломке.

**Круги считаются входами в работу, а не заходами сессии.** Маршрут,
доезжающий за три захода вместо одного, сломан не меньше вставшего — просто
иначе, и увидеть это можно только снаружи: раннер о втором заходе знает, а
о том, что он второй ПО ЭТОЙ дороге, — нет.

**Человека на мерже канарейка заменяет сама, и только в своём репозитории.**
Иначе «не доехала за четыре часа» измеряло бы сон владельца, а не маршрут.
Граница узкая и проверяемая: мержим PR, только если его репозиторий — тот
самый пустой, ветка названа нашей задачей, а цель — основная ветка. Зоны
автомержа (`automerge.py`) это не трогает и не расширяет: там решается, что
агент вливает в БОЕВЫЕ репозитории, здесь — что канарейка вливает в свой
собственный. Хостинга инсталляция не дала — канарейка честно ждёт человека и
на этой стадии не краснеет по сроку.

## Чего канарейка не умеет

Она живёт в процессе приёма. **Лёг приём — канарейка молчит вместе с ним**, и
пропуск суток в этом случае скажет не она. Внешний сторож за самим процессом —
отдельная работа; здесь честнее назвать границу, чем сделать вид, что её нет.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import NamedTuple

from .deploy import MAIN_BRANCHES
from .hosting import branch_names

log = logging.getLogger("intake")

# Идущий прогон. Один на инсталляцию: две канарейки в воздухе мерили бы друг
# друга — вторая заняла бы слот, из-за которого первая встала бы на «заведена».
RUN_KEY = "agent:canary:run"
# Последний прогон и его исход. По нему же считается пропуск суток.
LAST_KEY = "agent:canary:last"
# Право начать прогон в этот день. Захват, а не проверка времени: приём
# перезапускают, и два процесса в минуту пересменки завели бы две задачи.
START_KEY = "agent:canary:start"
# О пропуске суток говорим раз в сутки, а не каждый тик.
MISSED_KEY = "agent:canary:missed"
# С какого момента смотрим. Нужен ровно одному исходу — канарейка не взлетала
# НИ РАЗУ: сравнивать не с чем, последнего прогона нет, и без этой отметки
# молчание выглядело бы как «всё хорошо» ровно в том случае, ради которого
# канарейку и заводили.
SINCE_KEY = "agent:canary:since"

# Как часто смотреть. Стадии живут десятками минут, чаще незачем.
TICK_SEC = 120
# Час старта по UTC. Инсталляция ставит свой: прогон не должен отнимать слот у
# настоящей работы, поэтому ему отводят заведомо тихое время.
HOUR_UTC = 2
# Сколько заходов в работу считается нормой. Больше — маршрут доезжает, но
# сломан.
ROUNDS_NORM = 1
# Сколько терпим без единого прогона, прежде чем сказать об этом. Сутки плюс
# запас на перезапуск приёма в неудачный час.
MISS_AFTER_SEC = 36 * 3600
# Через сколько бросаем присмотр за вставшим прогоном. Не «забываем»: исход
# записан и сказан, но следующая канарейка должна вылететь по расписанию, а не
# ждать, пока разберут предыдущую.
GIVE_UP_SEC = 6 * 3600

# Стадии маршрута. Значения — то, что читает человек в тревоге, поэтому
# по-русски и в одно слово: «встала на стадии «проверка»» должно быть понятно
# без сверки с кодом.
CREATED = "заведена"
WORK = "работа"
VERIFY = "проверка"
SIGN = "подпись"
DONE = "доехала"
FAILED = "провалена"
DROPPED = "снята"
UNKNOWN = ""

# Порядок нормального пути. Стадии-исходы (`FAILED`, `DROPPED`) сюда не входят:
# они означают конец прогона, а не шаг вперёд.
ROUTE = (CREATED, WORK, VERIFY, SIGN, DONE)

# Сколько стадия имеет право длиться. Сроки с запасом: канарейка ловит
# вставший маршрут, а не медленный. Мерж («подпись») — только там, где мержит
# сама канарейка; ждущему человека сроку нет (`_stuck`).
DEADLINES = {CREATED: 1800, WORK: 5400, VERIFY: 3600, SIGN: 3600}


class Stages(NamedTuple):
    """Имена состояний доски. Умолчания — Linear и обеих инсталляций;
    площадка со своими именами передаёт свои, теми же переменными, что читает
    раннер (`AGENT_PROGRESS_STATE_NAME` и соседние)."""

    todo: str = "Todo"
    progress: str = "In Progress"
    verifying: str = "Verifying"
    review: str = "In Review"
    unverified: str = "Not Verified"
    done: str = "Done"
    canceled: str = "Canceled"


def stage_of(state_name: str, state_type: str = "",
             stages: Stages | None = None) -> str:
    """Стадия маршрута по состоянию задачи. Пусто — состояние не на маршруте.

    Имя главнее типа: у Linear четыре состояния маршрута имеют один тип
    `started`, и по типу их не различить. Тип — запасной путь для закрытых:
    переименованный `Done` всё равно означает «доехала», и терять на этом
    успешный прогон было бы обидно.
    """
    s = stages or Stages()
    name = (state_name or "").strip()
    by_name = {s.todo: CREATED, s.progress: WORK, s.verifying: VERIFY,
               s.review: SIGN, s.unverified: FAILED, s.done: DONE,
               s.canceled: DROPPED}
    if name in by_name:
        return by_name[name]
    # Backlog — то же «заведена»: задача создаётся в нём, и до первого события
    # раннера она там и лежит. Имя у него одно на все площадки Linear.
    if name.casefold() == "backlog":
        return CREATED
    kind = (state_type or "").strip().casefold()
    if kind == "completed":
        return DONE
    # `duplicate` — такой же ЗАКРЫВАЮЩИЙ тип Linear, как `canceled`, и на
    # живой доске он есть (статус «Duplicate» команды Development). Для
    # прогона это конец: задача закрыта не доехав. Не «не знаю такой стадии»
    # — иначе канарейка ждала бы движения от закрытой задачи до самого срока.
    if kind in ("canceled", "duplicate"):
        return DROPPED
    return UNKNOWN


def rounds_of(trail: list) -> int:
    """Сколько раз задача входила в работу. Ноль — работа ещё не начиналась.

    Считается по следу, а не счётчиком: счётчик пришлось бы наращивать в том
    же месте, где решается «стадия сменилась», и промах там был бы не виден
    ничем — след же печатается в тревоге целиком.
    """
    return sum(1 for step in trail or [] if (step or {}).get("стадия") == WORK)


def elapsed_human(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        return f"{sec // 60} мин"
    return f"{sec // 3600} ч {(sec % 3600) // 60:02d} мин"


def trail_text(trail: list, now: float) -> str:
    """След прогона одной строкой: где сколько простоял.

    Нужен именно в тревоге: «встала на проверке» без предыстории не отличает
    маршрут, который полз всю дорогу, от маршрута, который доехал за минуты и
    упёрся в одну стадию.
    """
    out = []
    for i, step in enumerate(trail or []):
        nxt = (trail[i + 1] or {}).get("когда") if i + 1 < len(trail) else now
        out.append(f"{step.get('стадия')} {elapsed_human(float(nxt or now) - float(step.get('когда') or now))}")
    return " → ".join(out)


def body(marker: str, repo: str, path: str) -> str:
    """Текст задачи-пустышки. Предмет обязан быть тривиальным И
    детерминированным: сессия должна получить один-единственный возможный
    дифф, иначе прогон измеряет качество работы, а не дорогу."""
    return (
        "Это **канарейка маршрута**, а не работа: фабрика раз в сутки "
        "проезжает собственный путь, чтобы поломка дороги была видна "
        "проверкой, а не случайно.\n\n"
        "Предмет намеренно тривиальный. Сделать ровно это и ничего больше.\n\n"
        "## Что сделать\n\n"
        f"В репозитории `{repo}` добавь в КОНЕЦ файла `{path}` одну строку:\n\n"
        f"```\n{marker}\n```\n\n"
        "Других правок в ветке быть не должно. Разбираться, анализировать и "
        "улучшать здесь нечего: ценность прогона в том, доедет ли задача до "
        "конца и за сколько заходов.\n\n"
        "## Критерий приёмки\n\n"
        f"В ветке задачи файл `{path}` заканчивается строкой `{marker}`, "
        "и дифф ветки состоит ровно из этой одной добавленной строки.\n"
    )


class Canary:
    """Прогон фабрики по собственному маршруту, раз в сутки.

    `tracker` — завести задачу и смотреть, где она; `store` — состояние
    прогона; `transport`+`notify_chat` — та же дорога, которой приём говорит
    остальные тревоги; `hosting` — необязательный, только чтобы смержить
    собственный PR в собственном пустом репозитории.
    """

    def __init__(self, tracker, store, transport, team_id: str,
                 agent_user_id: str, notify_chat: str = "",
                 repo: str = "", path: str = "canary/heartbeat.md",
                 project_id: str = "", hosting=None,
                 stages: Stages | None = None, hour_utc: int = HOUR_UTC,
                 rounds_norm: int = ROUNDS_NORM, deadlines: dict | None = None,
                 miss_after_sec: int = MISS_AFTER_SEC,
                 give_up_sec: int = GIVE_UP_SEC, tick_sec: int = TICK_SEC):
        self.tracker = tracker
        self.store = store
        self.transport = transport
        self.team_id = str(team_id or "")
        self.agent_user_id = str(agent_user_id or "")
        self.notify_chat = str(notify_chat or "")
        self.repo = str(repo or "").strip().strip("/")
        self.path = str(path or "").strip().lstrip("/")
        self.project_id = str(project_id or "")
        self.hosting = hosting
        self.stages = stages or Stages()
        self.hour_utc = int(hour_utc)
        self.rounds_norm = int(rounds_norm)
        self.deadlines = dict(DEADLINES if deadlines is None else deadlines)
        self.miss_after_sec = int(miss_after_sec)
        self.give_up_sec = int(give_up_sec)
        self.tick_sec = int(tick_sec)

    @property
    def able(self) -> bool:
        """Есть ли чем летать. Нет репозитория-пустышки — канарейки нет вовсе:
        заводить задачу, которую некуда сделать, значит каждый день отдавать
        слот работы под заведомо вставший прогон."""
        return bool(self.tracker and self.team_id and self.agent_user_id
                    and self.repo and self.path)

    def line(self) -> str:
        """Чем канарейка занята — для строки состояния при старте приёма.
        Выключенная молчанием неотличима от летающей и ничего не нашедшей."""
        if not self.able:
            missing = [n for n, v in (("трекера", self.tracker),
                                      ("AGENT_TEAM_ID", self.team_id),
                                      ("агента", self.agent_user_id),
                                      ("AGENT_CANARY_REPO", self.repo),
                                      ("AGENT_CANARY_FILE", self.path)) if not v]
            return "канарейка выключена (нет " + ", ".join(missing) + ")"
        how = ("мержит сама" if self.hosting else "мерж за человеком")
        return (f"канарейка маршрута: {self.hour_utc:02d}:00 UTC, "
                f"{self.repo}/{self.path}, {how}")

    # ── состояние ───────────────────────────────────────────────────────

    async def _load(self, key: str) -> dict:
        try:
            raw = await self.store.get(key)
        except Exception as e:
            log.warning("канарейка: %s не прочитан (%s)", key, e)
            return {}
        try:
            got = json.loads(raw) if raw else {}
        except Exception:
            log.error("канарейка: %s не разбирается — считаю пустым", key)
            return {}
        return got if isinstance(got, dict) else {}

    async def _save(self, key: str, value: dict, ttl: int = 30 * 24 * 3600) -> None:
        try:
            await self.store.put(key, json.dumps(value, ensure_ascii=False), ttl)
        except Exception as e:
            # Потеря записи — это потеря следа: прогон продолжит идти, но
            # стадии и круги посчитаются заново, с нуля.
            log.error("канарейка: %s не сохранён (%s)", key, e)

    async def _say(self, text: str) -> None:
        """Тревога уходит той же дорогой, что остальные: чат приёма."""
        if not (self.transport and self.notify_chat):
            log.error("канарейка: сказать некуда (нет notify_chat) — %s", text)
            return
        try:
            await self.transport.send(self.notify_chat, text)
        except Exception as e:
            log.error("канарейка: тревога не ушла (%s) — %s", e, text)

    # ── обход ───────────────────────────────────────────────────────────

    async def once(self, now: float | None = None) -> dict:
        """Один обход. Возвращает, что произошло, — по этому держатся тесты."""
        now = time.time() if now is None else now
        if not self.able:
            return {"выключена": True}
        run = await self._load(RUN_KEY)
        if run.get("issue"):
            return await self._watch(run, now)
        return await self._idle(now)

    async def _idle(self, now: float) -> dict:
        """Прогона нет: пора ли начать и не пропущены ли сутки."""
        last = await self._load(LAST_KEY)
        if self._due(now, last):
            return await self._start(now)
        missed = await self._miss(now, last, await self._since(now))
        return {"пропуск": missed} if missed else {}

    async def _since(self, now: float) -> float:
        """С какого момента считать молчание, пока прогонов не было вовсе."""
        got = await self._load(SINCE_KEY)
        when = float(got.get("когда") or 0)
        if not when:
            await self._save(SINCE_KEY, {"когда": now})
            return now
        return when

    def _due(self, now: float, last: dict) -> bool:
        """Час настал и сегодня ещё не летали."""
        if time.gmtime(now).tm_hour != self.hour_utc:
            return False
        started = float(last.get("начата") or 0)
        return not started or self._day(started) != self._day(now)

    @staticmethod
    def _day(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    async def _miss(self, now: float, last: dict, since: float = 0.0) -> str:
        """Пропуск суток. Молчащее расписание — это ровно тот исход, ради
        которого канарейку и заводят: сама она в этом случае не скажет
        ничего, поэтому смотрим на давность последнего прогона на КАЖДОМ
        обходе, а не только в час старта."""
        started = float(last.get("начата") or 0)
        # Ни одного прогона — считаем от того, когда начали смотреть. Иначе
        # канарейка, не взлетевшая ни разу (не тот час, не тот ключ, не то
        # право), молчала бы вечно, и молчание читалось бы как «всё цело».
        mark = started or since
        if not mark or (now - mark) < self.miss_after_sec:
            return ""
        try:
            first = await self.store.claim(f"{MISSED_KEY}:{self._day(now)}", 2 * 24 * 3600)
        except Exception as e:
            log.warning("канарейка: право на тревогу о пропуске не спросить (%s)", e)
            first = True
        if not first:
            return ""
        was = (f"последний прогон {time.strftime('%d.%m %H:%M', time.gmtime(started))} UTC "
               f"({last.get('issue') or '?'}, {last.get('исход') or '?'})"
               if started else "прогонов не было ни одного")
        text = (f"🔴 Канарейка не летала {elapsed_human(now - mark)}: {was}.\n"
                f"Расписание не работает — сам маршрут при этом может быть цел.")
        await self._say(text)
        return text

    async def _start(self, now: float) -> dict:
        """Завести задачу-пустышку и отдать её агенту. Дальше едет маршрут."""
        day = self._day(now)
        try:
            mine = await self.store.claim(f"{START_KEY}:{day}", 2 * 24 * 3600)
        except Exception as e:
            log.warning("канарейка: право на прогон не спросить (%s) — не лечу", e)
            return {}
        if not mine:
            return {}
        marker = f"канарейка {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} UTC"
        title = f"Канарейка маршрута {day}"
        try:
            issue = await asyncio.to_thread(
                self.tracker.create_issue, self.team_id, title,
                body(marker, self.repo, self.path), "", self.project_id)
            await asyncio.to_thread(self.tracker.assign, issue["id"], self.agent_user_id)
        except Exception as e:
            # Не завелась — это тоже поломка маршрута, и притом первой же его
            # стадии. Молчать о ней нельзя: снаружи она выглядит как пропуск
            # суток, а причина у неё совсем другая.
            log.exception("канарейка: прогон не начался")
            await self._say(f"🔴 Канарейка {day} не завелась: {type(e).__name__}: {e}\n"
                            f"Маршрут не проверен — встало на самом заведении.")
            await self._save(LAST_KEY, {"начата": now, "issue": "",
                                        "исход": "не завелась", "кругов": 0})
            return {"ошибка": str(e)}
        run = {"issue": issue.get("identifier") or "", "id": issue.get("id") or "",
               "url": issue.get("url") or "", "начата": now, "марка": marker,
               "стадия": CREATED, "с": now,
               "след": [{"стадия": CREATED, "когда": now}], "сказано": []}
        await self._save(RUN_KEY, run)
        log.info("канарейка %s пошла по маршруту (%s)", run["issue"], run["url"])
        return {"старт": run["issue"]}

    async def _watch(self, run: dict, now: float) -> dict:
        """Прогон идёт: где он сейчас, не встал ли, не пора ли мержить."""
        ident = run.get("issue") or ""
        try:
            issue = await asyncio.to_thread(self.tracker.issue_by_identifier, ident)
        except Exception as e:
            # Трекер не ответил — это не «задачи нет». Ждём следующего обхода:
            # закрыть прогон по молчанию сети значит соврать про маршрут.
            log.warning("канарейка %s: трекер молчит (%s)", ident, e)
            # Молчание сети — не вердикт, ждём следующего обхода. Но ждём не
            # вечно: не проверь мы тут общий срок, прогон при насовсем упавшем
            # трекере занимал бы место до перезапуска приёма, и пропуск суток
            # не сказал бы ничего — до `_idle` обход просто не доходит.
            await self._expired(run, now, "трекер молчит")
            return {}
        if issue is None:
            await self._finish(run, now, "задача исчезла",
                               f"🔴 Канарейка {ident}: задачи нет в трекере — "
                               f"прогон оборван, маршрут не проверен.")
            return {"итог": "задача исчезла"}
        state = (issue.get("state") or {})
        stage = stage_of(str(state.get("name") or ""), str(state.get("type") or ""),
                         self.stages)
        moved = stage and stage != run.get("стадия")
        if moved:
            run["стадия"], run["с"] = stage, now
            run.setdefault("след", []).append({"стадия": stage, "когда": now})
            run["сказано"] = []
            log.info("канарейка %s: стадия «%s»", ident, stage)
        if stage == DONE:
            return await self._arrived(run, now)
        if stage in (FAILED, DROPPED):
            return await self._derailed(run, now, stage, state)
        if stage == UNKNOWN:
            return await self._offroute(run, now, state)
        merged = ""
        if stage == SIGN:
            merged = await self._merge(run, now)
        await self._save(RUN_KEY, run)
        stuck = await self._stuck(run, now)
        return {"стадия": stage, "тревога": stuck, "мерж": merged}

    async def _arrived(self, run: dict, now: float) -> dict:
        """Доехала. Вердикт — два числа: сколько заняло и сколько кругов."""
        rounds = rounds_of(run.get("след"))
        took = elapsed_human(now - float(run.get("начата") or now))
        trail = trail_text(run.get("след"), now)
        if rounds < 1:
            # Задача закрылась, не побывав в работе ни разу: сессии не было, а
            # `Done` есть. Это самый тихий вид поломки маршрута — снаружи он
            # выглядит успехом, и зелёный отчёт тут был бы враньём.
            text = (f"🔴 Канарейка {run.get('issue')}: закрылась за {took}, "
                    f"НЕ побывав в работе ни разу.\n{trail}\n"
                    f"Маршрут срезан — сессия по задаче не поднималась.")
            await self._finish(run, now, "доехала мимо работы", text)
            return {"итог": "доехала мимо работы", "кругов": rounds}
        if VERIFY not in {(step or {}).get("стадия") for step in run.get("след") or []}:
            # Работа была, `Done` есть, а проверки на следе нет ни одной. Это
            # тот же тихий вид поломки, что и «мимо работы», и дорога сюда
            # короткая: сверка «в работе» (`reconcile.Stalled`) уводит задачу
            # без сессии прямо в стопку человека, то есть на «подпись», минуя
            # проверку. Дальше канарейка мержит свой PR сама — и прогон, где
            # проверки не было вовсе, выглядел бы зелёным.
            text = (f"🔴 Канарейка {run.get('issue')}: доехала до «{DONE}» за {took}, "
                    f"но проверки на маршруте не было ни разу.\n{trail}\n"
                    f"Маршрут срезан мимо «{VERIFY}» — так уводит задачу сверка "
                    f"«в работе», когда сессия по ней упала.")
            await self._finish(run, now, "доехала мимо проверки", text)
            return {"итог": "доехала мимо проверки", "кругов": rounds}
        if rounds > self.rounds_norm:
            # Доезжающий за три захода маршрут сломан не меньше вставшего,
            # просто иначе — и зелёным его называть нельзя.
            text = (f"⚠️ Канарейка {run.get('issue')}: доехала до «{DONE}» за {took}, "
                    f"но кругов {rounds} при норме {self.rounds_norm}.\n"
                    f"{trail}\nМаршрут доезжает не с первого захода — это поломка.")
            outcome = "доехала не с первого захода"
        else:
            text = (f"🐤 Канарейка {run.get('issue')}: доехала до «{DONE}» за {took}, "
                    f"кругов {rounds}.\n{trail}")
            outcome = "доехала"
        await self._finish(run, now, outcome, text)
        return {"итог": outcome, "кругов": rounds}

    async def _derailed(self, run: dict, now: float, stage: str, state: dict) -> dict:
        """Проверка завалила пустышку или задачу сняли. И то и другое —
        маршрут довёз, но не туда, куда обязан был."""
        was = trail_text(run.get("след"), now)
        text = (f"🔴 Канарейка {run.get('issue')}: «{stage}» "
                f"(статус {state.get('name') or '?'}).\n{was}\n"
                f"Предмет тривиальный, значит дело в дороге, а не в работе.")
        await self._finish(run, now, stage, text)
        return {"итог": stage}

    async def _offroute(self, run: dict, now: float, state: dict) -> dict:
        """Состояние не на маршруте. Говорим один раз и продолжаем смотреть:
        задачу могли увести руками, и тогда прогон уже ничего не измеряет."""
        if "вне маршрута" in (run.get("сказано") or []):
            await self._expired(run, now, "вне маршрута")
            return {"стадия": UNKNOWN}
        run.setdefault("сказано", []).append("вне маршрута")
        await self._save(RUN_KEY, run)
        await self._say(f"🔴 Канарейка {run.get('issue')}: статус "
                        f"«{state.get('name') or '?'}» не на маршруте — "
                        f"разбор не знает такой стадии, дальше считать нечего.")
        return {"стадия": UNKNOWN, "тревога": "вне маршрута"}

    async def _stuck(self, run: dict, now: float) -> str:
        """Стадия стоит дольше срока. Говорим один раз на стадию, а после
        общего срока бросаем присмотр: следующая канарейка обязана вылететь по
        расписанию, а не ждать, пока разберут предыдущую."""
        stage = run.get("стадия") or ""
        waited = now - float(run.get("с") or now)
        total = now - float(run.get("начата") or now)
        limit = self.deadlines.get(stage)
        # Мержит человек — срока тут нет ВООБЩЕ, ни стадийного, ни общего:
        # иначе через шесть часов канарейка объявила бы вставшим маршрут,
        # который на самом деле доехал и ждёт, когда владелец откроет телефон.
        # Прогон при этом всё равно закрывается — место должно освободиться к
        # завтрашнему вылету, — но исходом «ждёт человека», а не красным.
        human = stage == SIGN and not self._mergeable(run)
        if human:
            limit = None
            if total >= self.give_up_sec:
                await self._finish(
                    run, now, "ждёт человека",
                    f"🐤 Канарейка {run.get('issue')}: маршрут довёз до «{SIGN}» "
                    f"за {elapsed_human(total)}, дальше мерж за человеком.\n"
                    f"{trail_text(run.get('след'), now)}")
                return "ждёт человека"
            return ""
        if limit and waited >= limit and stage not in (run.get("сказано") or []):
            run.setdefault("сказано", []).append(stage)
            await self._save(RUN_KEY, run)
            text = (f"🔴 Канарейка {run.get('issue')} встала на стадии «{stage}»: "
                    f"{elapsed_human(waited)} без движения при сроке "
                    f"{elapsed_human(limit)}.\n{trail_text(run.get('след'), now)}\n"
                    f"Дальше маршрут не идёт.")
            await self._say(text)
            if total >= self.give_up_sec:
                await self._finish(run, now, f"встала на «{stage}»", "")
            return text
        if total >= self.give_up_sec:
            await self._finish(
                run, now, f"встала на «{stage}»",
                f"🔴 Канарейка {run.get('issue')}: бросаю присмотр через "
                f"{elapsed_human(total)}, последняя стадия «{stage}».\n"
                f"{trail_text(run.get('след'), now)}")
            return "брошена"
        return ""

    async def _expired(self, run: dict, now: float, why: str) -> bool:
        """Общий срок вышел — бросаем присмотр, чем бы прогон ни был занят.

        Зовётся из всех веток обхода, которые выходят рано: место должно
        освободиться к завтрашнему вылету, а занятый прогон вдобавок глушит
        тревогу о пропуске суток — она живёт в `_idle`, куда обход с живым
        прогоном не доходит вовсе.
        """
        if (now - float(run.get("начата") or now)) < self.give_up_sec:
            return False
        await self._finish(
            run, now, why,
            f"🔴 Канарейка {run.get('issue')}: бросаю присмотр через "
            f"{elapsed_human(now - float(run.get('начата') or now))} — {why}.\n"
            f"{trail_text(run.get('след'), now)}")
        return True

    async def _finish(self, run: dict, now: float, outcome: str, text: str) -> None:
        await self._save(LAST_KEY, {"начата": float(run.get("начата") or now),
                                    "кончена": now, "issue": run.get("issue") or "",
                                    "исход": outcome,
                                    "кругов": rounds_of(run.get("след")),
                                    "след": run.get("след") or []})
        await self._save(RUN_KEY, {}, ttl=60)
        if text:
            await self._say(text)
        log.info("канарейка %s: %s", run.get("issue"), outcome)

    # ── мерж собственного PR ────────────────────────────────────────────

    def _mergeable(self, run: dict) -> bool:
        return bool(self.hosting and self.repo)

    def _mine(self, pr: dict, ident: str) -> str:
        """Наш ли это PR. Отказ называет причину — иначе непонятно, промах
        это канарейки или чужой PR в том же репозитории.

        Границы три, и все обязательные: репозиторий — только пустышка из
        конфигурации, ветка — только названная нашей задачей, цель — только
        основная ветка. В боевой репозиторий канарейка не ходит вовсе, и
        расширить эту границу настройкой нельзя: `self.repo` и есть вся
        настройка, а имя ветки и цель проверяются кодом.
        """
        repo = str(pr.get("repo") or "").strip().strip("/")
        if repo != self.repo:
            return f"PR в {repo or '?'}, а канарейке принадлежит только {self.repo}"
        if not branch_names(str(pr.get("source_ref") or ""), ident):
            return f"ветка {pr.get('source_ref') or '?'} не названа задачей {ident}"
        if str(pr.get("target_ref") or "") not in MAIN_BRANCHES:
            return f"цель {pr.get('target_ref') or '?'} — не основная ветка"
        return ""

    async def _merge(self, run: dict, now: float) -> str:
        """Дошли до подписи — вливаем свой PR сами.

        Человека здесь заменяем намеренно: иначе срок стадии мерил бы не
        маршрут, а то, когда владелец откроет телефон. Мерж — тот же вызов
        хостинга, которым мержит кнопка на странице подписи, поэтому проверен
        оказывается и он.
        """
        if not self._mergeable(run) or "мерж" in (run.get("сказано") or []):
            return ""
        ident = run.get("issue") or ""
        try:
            pr = await asyncio.to_thread(self.hosting.open_request, ident)
        except Exception as e:
            log.warning("канарейка %s: PR не спросить (%s)", ident, e)
            return ""
        if not pr:
            return ""
        why = self._mine(pr, ident)
        if why:
            # Это не «подождём»: чужой PR по нашей задаче означает, что
            # маршрут привёл не туда, и мержить вслепую нельзя.
            run.setdefault("сказано", []).append("мерж")
            await self._say(f"🔴 Канарейка {ident}: мержить нечего — {why}.")
            return why
        try:
            done = await asyncio.to_thread(self.hosting.merge, pr)
        except Exception as e:
            run.setdefault("сказано", []).append("мерж")
            await self._say(f"🔴 Канарейка {ident}: мерж не прошёл — "
                            f"{type(e).__name__}: {e}.\nДальше маршрут не идёт.")
            return f"мерж не прошёл: {e}"
        run.setdefault("сказано", []).append("мерж")
        log.info("канарейка %s: %s", ident, done)
        return str(done)

    async def run(self) -> None:
        log.info("%s", self.line())
        if not self.able:
            return
        while True:
            try:
                await self.once()
            except Exception as e:
                log.exception("канарейка споткнулась: %s", e)
            await asyncio.sleep(self.tick_sec)
