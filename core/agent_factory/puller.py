"""Бэклог горит сам: агент берёт задачи, а не ждёт, пока их дадут.

До этого работа начиналась одним способом — человек назначал задачу (кнопкой
или руками), вебхук ловил назначение, раннер поднимал сессию. Значит скорость
всей фабрики упиралась в то, как часто человек заходит в трекер. Задач при
этом становится больше, чем поводов туда заходить: продуктов несколько, а
человек один.

Тяга переворачивает это: пока есть свободный слот и дневной лимит не выбран,
агент сам берёт следующую задачу по политике. Политика — не «что интереснее»,
а порядок, записанный один раз:

  инцидент (метка дежурного) → срочное → высокое → обычное → низкое,
  внутри одного уровня — что дольше лежит.

Техдолг из этого списка не исчезает, он просто заполняет простой: пока есть
что важнее, до него не доходит, а в тихий час доходит.

Два потолка, оба обязательны и оба — параметры инсталляции:

  • СКОЛЬКО ОДНОВРЕМЕННО. Слот занят не только счётом: сессия правит репозиторий
    и открывает MR, а человек потом это подписывает. Десять параллельных веток
    означают десять разборов за раз — узкое место не в машине.
  • СКОЛЬКО В СУТКИ. Защита от ночи, когда бэклог кончился, а тяга нашла в нём
    сотню черновиков: за такую ночь можно сжечь дневной бюджет и завалить
    человека подписями.

Часть ПРОДУКТА: политика и потолки одинаковы у любой инсталляции, различаются
только числа и id команды.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("intake")

# Сколько задач агент ведёт одновременно.
MAX_IN_FLIGHT = 2
# Сколько берёт за сутки. Ноль — без дневного потолка.
MAX_PER_DAY = 8
# Сколько задач терпим У ЧЕЛОВЕКА. Ноль — не смотрим вовсе.
#
# Дневная квота ограничивает производство, а узкое место — приёмка: агенты
# сдают быстрее, чем человек подписывает, и очередь на подпись растёт, пока
# квота не кончится. Лимит на незакрытые упирает их ровно туда, где затор:
# не разобрал — новое не берут. И расширяется он не настройкой, а вторым
# приёмщиком.
MAX_OPEN = 0
# Что занимает внимание человека: две стопки — прошедшее проверку (он
# мержит) и не прошедшее (он разбирает). Сумма двух, а не одна первая: с
# 15.09.2026 (DEV-96) провал в любом режиме уезжает во вторую стопку, и
# считать только первую значило бы копить поломки без потолка. Исполнитель
# не важен — задача на агенте место у человека занимает так же: он её
# разберёт, когда агент сдастся. Имена состояний — умолчания Linear и
# первого клиента; инсталляция передаёт свои.
DEFAULT_OPEN_STATES = ("In Review", "Not Verified")
# Что не считать НИКОГДА, даже если инсталляция вписала это в список:
# состояние «проверка идёт» рассасывается само, и потолок, который его
# считает, блокирует сам себя — проверки стоят в очереди, тяга ждёт проверок.
NEVER_COUNT = ("Verifying",)
# Порция и её окно: не больше стольких задач за столько секунд. Дневной
# потолок один не спасает от залпа — восемь задач можно взять и за минуту,
# освободись слоты подряд. Человеку же нужно, чтобы работа приходила ровно:
# две за два часа, потом ещё две.
PER_WINDOW = 0
WINDOW_SEC = 7200
# Как часто смотреть, не освободился ли слот.
INTERVAL_SEC = 60
# Молодые задачи не трогаем: человек может дописывать постановку прямо сейчас,
# и взять её на середине фразы — худшее, что тяга может сделать. Инцидента это
# не касается: он и заводится потому, что ждать нельзя.
MIN_AGE_SEC = 600
# Ключ дневного счётчика.
TAKEN_KEY = "agent:pull:taken"
# Ключ настроек. Потолки правятся со страницы очереди и должны применяться
# сразу: перезапуск приёма ради «возьми сегодня не восемь, а четыре» — это
# оборванная сессия и потерянный час работы.
LIMITS_KEY = "agent:pull:limits"
# Что вообще можно править снаружи и в каких границах. Границы обязательны:
# страница открыта в интернете, и опечатка в поле не должна превращаться в
# сотню задач за ночь.
TUNABLE = {"max_in_flight": (1, 8), "max_per_day": (0, 50),
           "max_open": (0, 50), "per_window": (0, 20),
           "window_sec": (300, 86400)}
DAY_SEC = 24 * 3600

# Приоритеты трекера: 1 срочно, 2 высокий, 3 обычный, 4 низкий, 0 — не задан.
# Ноль означает «не решили», а не «срочно», поэтому ставим его после низкого.
NO_PRIORITY = 5


def label_names(issue: dict) -> list[str]:
    nodes = ((issue.get("labels") or {}).get("nodes")
             if isinstance(issue.get("labels"), dict) else issue.get("labels"))
    out = []
    for n in nodes or []:
        out.append(n.get("name", "") if isinstance(n, dict) else str(n))
    return [s for s in out if s]


def is_incident(issue: dict, incident_label: str) -> bool:
    if not incident_label:
        return False
    want = incident_label.strip().casefold()
    return any(n.strip().casefold() == want for n in label_names(issue))


def age_sec(issue: dict, now: float) -> float:
    created = issue.get("created_at") or issue.get("createdAt") or ""
    if isinstance(created, (int, float)):
        return max(0.0, now - float(created))
    try:
        from datetime import datetime
        ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return max(0.0, now - ts.timestamp())
    except Exception:
        # Даты нет или она непонятная: считаем задачу старой. Пропустить
        # задачу молча хуже, чем взять её на минуту раньше.
        return float("inf")


def rank(issue: dict, incident_label: str) -> tuple:
    """Чем меньше — тем раньше берём."""
    priority = issue.get("priority")
    priority = NO_PRIORITY if not priority else int(priority)
    created = str(issue.get("created_at") or issue.get("createdAt") or "")
    return (0 if is_incident(issue, incident_label) else 1, priority, created)


def next_up(issues: list[dict], incident_label: str = "", now: float = 0.0,
            min_age_sec: int = MIN_AGE_SEC,
            skip_labels: tuple[str, ...] = ()) -> list[dict]:
    """Что можно взять, в порядке взятия."""
    skip = {s.strip().casefold() for s in skip_labels if s.strip()}
    ready = []
    for i in issues:
        if not i.get("id"):
            continue
        names = {n.strip().casefold() for n in label_names(i)}
        if skip & names:
            continue
        if not is_incident(i, incident_label) and age_sec(i, now) < min_age_sec:
            continue
        ready.append(i)
    return sorted(ready, key=lambda i: rank(i, incident_label))


def taken_today(marks: list, now: float) -> list:
    return [float(m) for m in marks if now - float(m) < DAY_SEC]


def batch_since(marks: list, now: float, window_sec: int) -> tuple[float, int]:
    """Начало текущей пачки и сколько в неё уже взято. Пусто — окно свободно.

    Пачка ФИКСИРОВАННАЯ: её открывает первая задача, и живёт пачка ровно
    `window_sec`. Было скользящее окно — и обратный отсчёт прыгал вверх: он
    считался от старейшей метки в окне, а стоило взять новую задачу, как
    старейшей становилась более поздняя. Человек видел «12 минут», потом «26»
    и справедливо считал это обманом.
    """
    marks = sorted(float(m) for m in marks)
    if not marks:
        return 0.0, 0
    start = marks[0]
    for m in marks:
        if m >= start + window_sec:      # прежняя пачка кончилась, эта — новая
            start = m
    if now >= start + window_sec:        # и текущая уже истекла
        return 0.0, 0
    return start, len([m for m in marks if m >= start])


class Puller:
    def __init__(self, tracker, store, agent_user_id: str, team_id: str,
                 incident_label: str = "", max_in_flight: int = MAX_IN_FLIGHT,
                 max_per_day: int = MAX_PER_DAY, max_open: int = MAX_OPEN,
                 open_states: tuple[str, ...] = DEFAULT_OPEN_STATES,
                 never_count: tuple[str, ...] = NEVER_COUNT,
                 per_window: int = PER_WINDOW, window_sec: int = WINDOW_SEC,
                 interval_sec: int = INTERVAL_SEC,
                 min_age_sec: int = MIN_AGE_SEC,
                 skip_labels: tuple[str, ...] = (), key: str = TAKEN_KEY,
                 limits_key: str = LIMITS_KEY):
        self.tracker = tracker
        self.store = store
        self.agent_user_id = agent_user_id
        self.team_id = team_id
        self.incident_label = incident_label
        self.max_in_flight = int(max_in_flight)
        self.max_per_day = int(max_per_day)
        self.max_open = int(max_open)
        # Какие состояния считать «у человека». Имена, а не типы: у Linear
        # работа, приёмка и проверка — один тип `started`, а копятся только
        # две стопки человека. Состояние проверки вычитается всегда, как бы
        # ни был настроен список: иначе потолок блокирует сам себя.
        skip = {s.strip().casefold() for s in never_count}
        self.open_states = tuple(s for s in open_states
                                 if s.strip() and s.strip().casefold() not in skip)
        # Ноль в порции выключает ровный темп: берём, как только есть слот.
        self.per_window = int(per_window)
        self.window_sec = int(window_sec)
        self.interval_sec = int(interval_sec)
        self.min_age_sec = int(min_age_sec)
        self.skip_labels = tuple(skip_labels)
        self.key = key
        self.limits_key = limits_key

    async def _marks(self, now: float) -> list:
        try:
            raw = await self.store.get(self.key)
        except Exception as e:
            # Счётчик не прочитан — считаем, что дневной лимит выбран. Иначе
            # молчащий redis превращается в «бери сколько хочешь».
            log.warning("дневной счётчик тяги не прочитан (%s) — не беру", e)
            return [now] * self.max_per_day
        try:
            return taken_today(json.loads(raw) if raw else [], now)
        except Exception:
            log.error("дневной счётчик тяги не разбирается — начинаю пустым")
            return []

    async def _mark(self, marks: list, now: float) -> None:
        try:
            await self.store.put(self.key, json.dumps(marks + [now]), DAY_SEC)
        except Exception as e:
            log.error("взятие не записано в счётчик (%s) — дневной потолок "
                      "перестанет считаться", e)

    async def open_now(self) -> int | None:
        """Сколько задач лежит у человека — на подписи и на разборе, кто бы
        ни был исполнителем. None — трекер не ответил; в этом случае не
        берём: «не знаю» не должно читаться как «свободно»."""
        counter = getattr(self.tracker, "open_count", None)
        if not (counter and self.team_id and self.open_states):
            return 0
        try:
            return int(await asyncio.to_thread(counter, self.team_id,
                                               self.open_states))
        except Exception as e:
            log.warning("не спросить, сколько не закрыто (%s) — не беру", e)
            return None

    def in_flight(self) -> int:
        """Сколько задач агент ведёт прямо сейчас. Не начатые тоже считаются:
        они уже назначены, и слот под них занят."""
        got = self.tracker.assigned_issues(
            self.agent_user_id, ("backlog", "unstarted", "started"))
        return len(got or [])

    def limits(self) -> dict:
        """Действующие потолки — с учётом того, что поправили на странице."""
        return {name: getattr(self, name) for name in TUNABLE}

    async def tune(self, values: dict) -> dict:
        """Поменять потолки на лету. Возвращает то, что реально применилось.

        Чужое и выходящее за границы отбрасывается молча для вызывающего, но
        громко в журнал: настройка, которая делает вид, что применилась, хуже
        отказа."""
        applied = {}
        for name, (low, high) in TUNABLE.items():
            if name not in values:
                continue
            try:
                got = int(values[name])
            except Exception:
                log.warning("настройка тяги %s=%r — не число", name, values[name])
                continue
            if not (low <= got <= high):
                log.warning("настройка тяги %s=%d вне границ %d..%d — не беру",
                            name, got, low, high)
                continue
            applied[name] = got
        if not applied:
            return self.limits()
        current = self.limits()
        current.update(applied)
        try:
            await self.store.put(self.limits_key, json.dumps(current), 0)
        except Exception as e:
            log.error("настройки тяги не сохранены (%s) — вернутся к прежним "
                      "после перезапуска", e)
        for name, value in applied.items():
            setattr(self, name, value)
        log.info("тяга настроена: %s", ", ".join(f"{k}={v}" for k, v in
                                                 sorted(applied.items())))
        return self.limits()

    async def _restore(self) -> None:
        """Поднять сохранённые потолки при старте: иначе перезапуск приёма
        молча возвращал бы то, что человек уже отменил."""
        try:
            raw = await self.store.get(self.limits_key)
            saved = json.loads(raw) if raw else {}
        except Exception as e:
            log.warning("настройки тяги не прочитаны (%s) — беру из окружения", e)
            return
        for name, (low, high) in TUNABLE.items():
            got = saved.get(name)
            if isinstance(got, int) and low <= got <= high:
                setattr(self, name, got)

    async def next_batch_in(self, now: float | None = None) -> float:
        """Секунд до следующей порции. Ноль — можно брать сейчас.

        Считается от начала текущей пачки, поэтому число только убывает.
        """
        if not self.per_window:
            return 0.0
        now = time.time() if now is None else now
        start, taken = batch_since(await self._marks(now), now, self.window_sec)
        if not start or taken < self.per_window:
            return 0.0
        return max(0.0, start + self.window_sec - now)

    async def once(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not (self.agent_user_id and self.team_id):
            return {"взято": 0, "почему": "нет агента или команды"}
        try:
            busy = await asyncio.to_thread(self.in_flight)
        except Exception as e:
            log.warning("не спросить, что на агенте (%s) — не беру", e)
            return {"взято": 0, "почему": "трекер молчит"}
        free = self.max_in_flight - busy
        if free <= 0:
            return {"взято": 0, "почему": f"занято {busy} из {self.max_in_flight}"}
        if self.max_open:
            open_now = await self.open_now()
            if open_now is None:
                return {"взято": 0, "почему": "не спросить, сколько не закрыто"}
            if open_now >= self.max_open:
                # Не «ошибка», а штатный упор: дальше ход за человеком.
                return {"взято": 0,
                        "почему": f"у человека {open_now} из {self.max_open} "
                                  f"(на подписи и разборе) — жду разбора"}
            left_open = self.max_open - open_now
        else:
            left_open = None
        marks = await self._marks(now)
        left = self.max_per_day - len(marks) if self.max_per_day else 10 ** 6
        if left <= 0:
            return {"взято": 0, "почему": f"за сутки уже {len(marks)}"}
        if left_open is not None:
            left = min(left, left_open)
        if self.per_window:
            start, taken = batch_since(marks, now, self.window_sec)
            left_now = self.per_window - taken
            if start and left_now <= 0:
                ждать = int((start + self.window_sec - now) // 60)
                return {"взято": 0,
                        "почему": f"порция выбрана ({taken} за "
                                  f"{self.window_sec // 3600}ч), следующая "
                                  f"через {ждать} мин"}
            left = min(left, max(left_now, self.per_window if not start else 0))
        try:
            free_issues = await asyncio.to_thread(
                self.tracker.unassigned_issues, self.team_id)
        except Exception as e:
            log.warning("бэклог не прочитан (%s) — не беру", e)
            return {"взято": 0, "почему": "трекер молчит"}
        queue = next_up(free_issues or [], self.incident_label, now,
                        self.min_age_sec, self.skip_labels)
        took = 0
        for issue in queue[:min(free, left)]:
            try:
                await asyncio.to_thread(self.tracker.assign, issue["id"],
                                        self.agent_user_id)
            except Exception as e:
                log.warning("тяга: %s не назначилась (%s)",
                            issue.get("identifier"), e)
                continue
            marks = marks + [now]
            await self._mark(marks[:-1], now)
            took += 1
            # Суточный счётчик печатаем ТОЛЬКО когда потолок задан. При
            # нуле («без дневного потолка») строка «за сутки 5 из 0» читается
            # как исчерпанная квота при работающей тяге — то есть врёт ровно
            # наоборот, и по ней уже искали несуществующую поломку.
            day = (f", за сутки {len(marks)} из {self.max_per_day}"
                   if self.max_per_day else f", за сутки {len(marks)} "
                   "(суточный потолок не задан)")
            log.info("тяга: взята %s (%s, приоритет %s) — занято %d из %d%s",
                     issue.get("identifier"),
                     "инцидент" if is_incident(issue, self.incident_label)
                     else "работа", issue.get("priority") or "не задан",
                     busy + took, self.max_in_flight, day)
        return {"взято": took, "почему": "" if took else "нечего брать"}

    def limits_line(self) -> str:
        """Чем тяга ограничена — одной строкой, для журнала.

        Отдельным методом, чтобы это проверялось тестом. Настройка, которую не
        видно при запуске, неотличима от настройки, которая не доехала: за один
        вечер трижды выяснялось, что значение лежит в окружении и ничего не
        делает.
        """
        limits = [f"не больше {self.max_in_flight} одновременно"]
        if self.per_window:
            limits.append(f"по {self.per_window} за {self.window_sec // 3600}ч")
        if self.max_open:
            limits.append(f"не больше {self.max_open} у человека "
                          f"({' + '.join(self.open_states) or 'состояния не заданы'})")
        if self.max_per_day:
            limits.append(f"{self.max_per_day} в сутки")
        if self.skip_labels:
            limits.append("мимо ярлыков: " + ", ".join(self.skip_labels))
        return ", ".join(limits)

    async def run(self) -> None:
        await self._restore()
        log.info("тяга запущена: %s, смотрю раз в %dс",
                 self.limits_line(), self.interval_sec)
        while True:
            try:
                await self.once()
            except Exception as e:
                log.exception("тяга споткнулась: %s", e)
            await asyncio.sleep(self.interval_sec)
