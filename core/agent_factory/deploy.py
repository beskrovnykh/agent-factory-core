"""Выкат: от события хостинга до вердикта в чате.

Работа агента доехала до прода — надо убедиться, что в проде правда то, что
заявлено. Зелёная джоба этого не доказывает: 26.08.2026 у первого клиента
на хосте месяцами лежал `prompt.py` старее репозитория при неизменно зелёных
деплоях. Поэтому на выкат ставится короткая сессия-проверяльщик
(`kind=verify`, контракт в `core/prompt.py`), а её вердикт дописывается в
сообщение задачи в чате.

Часть ПРОДУКТА — всё, что одинаково у любой инсталляции:

  • заначка задачи по идентификатору (`agent:task:<TASK-N>`). Выкат случается
    через часы-дни после постановки, уже по событию хостинга, где ни id
    задачи, ни постановщика нет — из него известно только имя ветки. Заначку
    пишут все три пути постановки (вебхук, кнопка, опрос), читает она;
  • дедуп по выкаченному коммиту: сколько бы пайплайнов на нём ни собралось
    (ручной прогон, расписание, ретрай доставки), проверка одна;
  • сама постановка проверяльщика в очередь;
  • разбор вердикта из моста раннера и показ его в сообщении задачи;
  • перевод задачи в `Done` по мержу PR в основную ветку (`on_merge`):
    мерж — принятие человеком, дальше решать нечего.

За инсталляцией — протокол `Deployment` с одним вопросом: «это событие —
выкат работы агента, и что именно выехало?». Только это и различается: у
первого клиента событие пайплайна GitLab с мердж-коммитом, у второй
инсталляции — GitHub Actions. Ровно как `Source` у обновления ядра и
`Handoff` у сдачи: продукт не знает про хостинг и знать не должен.

Почему один метод, а не приёмник с разбором внутри: у инсталляции уже есть
свой приёмник событий хостинга — с подписью, дедупом доставок и своими
уведомлениями про джобы, — и ему решать, какое из событий выкат. Продукту
нужен только ответ. Инсталляции без реализации протокола (второй сейчас) —
продукт молчит: ничего не ставит, ничего не пишет, но вердикты из моста
разбирает, если кто-то поставил проверку руками.

Заначки нет — проверка всё равно ставится. Так было у первого клиента до
переезда сюда, и 0.6.13 это молча сузил: «нет заначки — не ставим». Заначка
живёт 30 дней, а задача умеет пролежать в ревью дольше; ветку `agent/<TASK-N>`
умеет собрать и человек. Непроверенный выкат хуже, чем вердикт без адреса.
Адрес при этом восстанавливается, если инсталляция умеет (`resolve_issue`):
без issue_id раннер шлёт вердикт отдельным сообщением, а с ним — строкой в
сообщение задачи.
"""
from __future__ import annotations

import asyncio
import inspect
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from . import runs
from . import mergeable
from .gitref import Located

log = logging.getLogger("intake")

# Мост вердиктов: раннер кладёт исход проверки в этот список, приём
# дописывает его в сообщение задачи. Та же переменная, что у раннера: одно
# имя, одно значение, два потребителя (закреплено в core/test_verify_bridge.py).
VERIFY_RESULTS_KEY = os.environ.get("AGENT_VERIFY_RESULTS_KEY", "agent:verify:results")

# Сколько живёт заначка. С запасом на «полежала в ревью, потом смержили»:
# выкат через месяц после постановки — уже не выкат этой работы.
STASH_TTL_SEC = 30 * 24 * 3600

# Сколько помним, что этот коммит уже проверяли. Пайплайны на одном коммите
# случаются и через неделю — повторный деплой соседнего сервиса.
VERIFIED_TTL_SEC = 30 * 24 * 3600

# Сколько держится захват «проверка этого коммита ПОСТАВЛЕНА и ещё не
# отработала» — общий для всех входов постановки (DEV-110).
#
# Своя метка есть у каждого входа, но они разные: `<ident>:<sha>` у выката и
# `<ident>:state:<updatedAt>` у статуса. Один коммит, пришедший обоими путями,
# проходил оба дедупа: 15.09.2026 DEV-106 встал в очередь дважды (выкат
# 23:23:06, статус 23:23:58, коммит один), обе сессии поднялись в два слота с
# разницей в 8 мс и перезаписали друг другу записи прогона.
#
# Повтор коммита ПОСЛЕ вердикта остаётся разрешённым — это решение владельца
# 15.09.2026 по 56 записанным сессиям, и его этот захват не трогает: вердикт
# снимает захват сам (`on_verdict`). Отсекается только повтор, у которого
# ответа ещё нет: две сессии на одном коммите в одно время отвечают на один
# вопрос по одним и тем же данным, и второй ответ взяться неоткуда.
#
# Срок — с запасом на «полежала в очереди»: у DEV-106 между постановкой и
# стартом прошло 26 минут, дальше час сессии. Он же страховка от залипания:
# вердикт может не записаться вовсе (раннер прежней версии, пустой
# verdict.txt), и тогда захват обязан отпустить сам — запереть проверку
# коммита насовсем хуже, чем поднять лишнюю сессию через два часа.
VERIFY_PENDING_TTL_SEC = 2 * 3600

# Значок, с которым раннер сам зовёт человека лично (runner.report_verdict):
# тревожный вердикт уходит от него отдельным сообщением, и второй раз тому же
# человеку слать нечего. Закреплено контрактным тестом.
LOUD_ICON = "⚠️"

# Значок отсева воротами: сессии не было, решение принял код. Не ⚠️ — тревоги
# нет, и не ✅ — ничего не проверено.
SKIP_ICON = "⏭"


# ── две точки проверки, два выключателя ──────────────────────────────────
#
# Проверка ставится в два разных момента жизни задачи: ДО мержа — ветка на
# dev-стенде (контракт `GOAL_BRANCH`), и ПОСЛЕ мержа — коммит уже в `main`,
# выкат на прод (`GOAL_DEPLOY`). Владельцу нужен только первый: после мержа он
# проверяет сам и платить за это токенами не хочет (DEV-97, схема согласована
# 15.09.2026: `docs/factory-flow.bpmn`). Но выкидывать второй насовсем
# неправильно — на другой инсталляции или позже он понадобится.
#
# Поэтому выключатели, а не удаление: `AGENT_VERIFY_BEFORE_MERGE` и
# `AGENT_VERIFY_AFTER_MERGE`, значения `1`/`0`, каждый сам по себе. Не задан —
# включена, как было до выключателей. Выключенная точка НЕ СТАВИТ сессию в
# очередь вовсе — не «ставит и снимает», а именно не ставит: смысл в экономии.
# Код второй точки при этом цел и под тестами; включается переменной, без
# выката. Обе выключены — фабрика работает без проверки вообще, и раннер об
# этом говорит на старте отдельной громкой строкой.
#
# Точка — по РЕЖИМУ, который выбрали ворота, а не по тому, кто позвал: перевод
# по статусу на уже влитом коммите — это проверка после мержа, а событие
# хостинга о зелёном прогоне на ветке — проверка до мержа. Иначе выключатель
# «после мержа» молчал бы на статусе и жёг токены ровно там, где обещал
# экономить.
#
# Раннер читает те же две переменные сам (`runner.py`, стартовая строка):
# импортировать друг друга они не могут, совпадение имён и разбора значения
# закреплено в `core/test_verify_bridge.py`.
VERIFY_BEFORE_MERGE_VAR = "AGENT_VERIFY_BEFORE_MERGE"
VERIFY_AFTER_MERGE_VAR = "AGENT_VERIFY_AFTER_MERGE"

POINT_BEFORE = "до мержа"
POINT_AFTER = "после мержа"

# Что считается «выключено». Всё остальное — в том числе пусто и не задано —
# включено: выключатель обязан быть осознанным словом, а не опечаткой.
SWITCH_OFF = ("0", "false", "no", "off")


def switch_on(raw) -> bool:
    """`1`/`0` из окружения → включена ли точка. Не задано — включена."""
    return str(raw or "").strip().casefold() not in SWITCH_OFF


def verify_points(env=None) -> dict[str, bool]:
    """Раскладка точек проверки: имя точки → включена ли. Читается один раз,
    на старте процесса — как и всё остальное окружение приёма."""
    env = os.environ if env is None else env
    return {POINT_BEFORE: switch_on(env.get(VERIFY_BEFORE_MERGE_VAR)),
            POINT_AFTER: switch_on(env.get(VERIFY_AFTER_MERGE_VAR))}


def point_of(mode: str) -> str:
    """Какая точка у режима контракта: `branch` — до мержа, `state`/`build`/
    `deploy` — после (см. `Ok.mode`)."""
    return POINT_BEFORE if mode == "branch" else POINT_AFTER


def switch_of(point: str) -> str:
    """Какой переменной включается точка — чтобы строка в журнале называла
    её, а не заставляла искать по коду."""
    return VERIFY_BEFORE_MERGE_VAR if point == POINT_BEFORE else VERIFY_AFTER_MERGE_VAR


# ── ворота перед постановкой ─────────────────────────────────────────────
#
# Проверка ставилась по переходу состояния, не спрашивая, есть ли что
# наблюдать. За трое суток 12–14.09.2026 на client-b три сессии ушли на задачи,
# которых в трекере ещё не было ($6,76), одна — на ветку, которой нет ($3,23),
# и по нескольку раз проверялся один и тот же коммит (DEV-43, DEV-59, DEV-16,
# DEV-18: те же 903 теста дважды за три часа). Всё это код может выяснить до
# постановки за секунды и без денег.
#
# Ворота ВЫБИРАЮТ КОНТРАКТ, а не отменяют сессию. Отмена — только когда
# наблюдать физически нечего: нет задачи, нет ветки и коммита, коммит уже
# проверяли. «Выката не было» и «PR открыт» — не причина: семь таких сессий
# 13–14.09 дали настоящую проверку поведения в ветке (1216 тестов, 903, 340 с
# шестью пойманными мутациями). Отсутствие критерия приёмки тоже не отменяет
# — оно пишется в запись запуска, и сессия знает, откуда критерий брать.
#
# Режим (ветка / после мержа) выбирается по предковости коммита относительно
# main, а не по тому, кто позвал: перевод по статусу после мержа — это
# проверка выката, зелёный прогон на ветке — проверка ветки, что бы ни
# говорил `trigger` события.

# Значения `criteria_source` в записи запуска. Слова те же, что раннер ждёт в
# первой строке `expected.md` сессии (`runner.CRITERION_SOURCES`, DEV-87): в
# `stats.json` рядом лягут два ответа на один вопрос — сессии и кода, — и
# сравнимы они только на одном словаре.
CRITERIA_TASK = "задача"        # раздел «Критерий приёмки» есть
CRITERIA_FALLBACK = "запасной"  # критерия нет, есть «Что сделать / решить / проверить»
CRITERIA_NONE = "нет"           # ни того, ни другого
CRITERIA_UNKNOWN = "неизвестно" # трекер не ответил или не подключён

# Факт, которого ворота не смогли добыть: git или трекер не ответили. Не
# None — у None уже есть смысл «нет».
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Ok:
    """Сессию ставим. `mode` уезжает в `trigger` контракта: `branch` — работа
    в ветке, до мержа; `state`/`build`/`deploy` — после мержа (различаются
    врезками, см. `prompt.build_verify_prompt`)."""
    mode: str
    ref: str = ""
    commit_sha: str = ""
    criteria: str = CRITERIA_UNKNOWN
    rounds: int = 0
    issue: dict = field(default_factory=dict)
    # Что ворота не сверили — в запись запуска: «git не ответил» отличимо от
    # «ветка есть».
    notes: tuple = ()
    # Метка коммита, поставленная воротами: снять, если постановка сорвётся.
    claim: str = ""
    # Прежний вердикт по этому коммиту, если он уже проверялся. ФАКТ для
    # проверяющего, не повод отменить постановку: повторные проверки находят
    # пропущенное (решение владельца 15.09.2026, см. `decide`).
    prior: str = ""
    # Вливается ли ветка в основную (DEV-109): True / False / None — не
    # спрашивали или хостинг не сказал. Постановку не отменяет НИКОГДА:
    # несливаемую ветку проверять можно и нужно, нельзя только класть её в
    # стопку «ждёт мержа» — это решает маршрут вердикта у раннера.
    mergeable: object = None


@dataclass(frozen=True)
class Skip:
    """Сессию не ставим. `seen` — отсев по «уже проверяли»: метка коммита
    поставлена не нами, и снимать её нельзя. `claim` — метка, которую ворота
    успели поставить сами до отказа: её вызывающий снимает, иначе коммит
    30 суток считался бы проверенным без проверки."""
    reason: str
    prior: str = ""
    seen: bool = False
    claim: str = ""


# Класс расхождения, за которым следующая проверка не ставится (DEV-89):
# наблюдать было негде, и до перемены точки негде и останется. Слово то же,
# что пишет раннер (`runner.CLASS_UNOBSERVABLE`), — закреплено
# `test_verify_bridge.py`.
CLASS_UNOBSERVABLE = "unobservable"


def criteria_source(description: str) -> str:
    """Откуда сессии брать критерий: из раздела «Критерий приёмки», из «Что
    сделать» или неоткуда. Тот же выбор делает контракт (шаг 1), но сессия
    выясняет это за деньги, а здесь — бесплатно, и в записи запуска видно,
    сколько задач ставилось без критерия."""
    text = (description or "").casefold()
    if re.search(r"критери[йия]\s+при[её]мки", text):
        return CRITERIA_TASK
    if re.search(r"что\s+(сделать|решить|проверить|починить)|ожидаемое\s+поведение", text):
        return CRITERIA_FALLBACK
    return CRITERIA_NONE


def when(ts: float) -> str:
    """Время для человека в причине отсева и в записи запуска. UTC: журнал
    приёма и метки каталогов раннера тоже в UTC, и разнобой заставлял бы
    сверять два часовых пояса при разборе одной строки."""
    if not ts:
        return "?"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%d.%m %H:%M UTC")


def mode_for(entry: str, hint: str, ancestor) -> str:
    """Режим контракта по предковости, а не по слову в событии.

    `entry` — откуда позвали: `state` (перевод по статусу) или `event`
    (событие хостинга). `hint` — режим, который предложил вызывающий;
    действует, только когда предковость неизвестна. `ancestor` — влит ли
    коммит в main: False → ветка, True → после мержа, None → не знаем.
    """
    if ancestor is False:
        return "branch"
    if ancestor is True:
        if entry == "state":
            return "state"
        return "build" if hint == "build" else "deploy"
    return hint or ("branch" if entry == "state" else "deploy")


def decide(*, entry: str, hint: str, ident: str, ref: str, exists, located,
           seen, criteria: str = CRITERIA_UNKNOWN, rounds: int = 0,
           issue: dict | None = None, notes: tuple = (), claim: str = "",
           sha: str = "", prior_class: str = "", prior_mode: str = "",
           prior_line: str = "", prior_at: float = 0.0,
           cleared_at: float = 0.0, cleared_by: str = "", cleared_why: str = "",
           mergeable=None):
    """Чистое решение ворот по собранным фактам. Без сети и без стора —
    ровно то, что прогоняется по журналу записанных сессий.

    `exists` — есть ли задача (None: трекер не ответил). `located` —
    `Located`, None (нет нигде) или `UNKNOWN`. `seen` — None, если коммит
    не проверяли, иначе прежний вердикт (пусто — вердикт ещё не пришёл).
    Повтор коммита постановку НЕ отменяет: прежний вердикт едет фактом.

    `prior_class` / `prior_mode` / `prior_line` — чем кончилась последняя
    состоявшаяся проверка ЭТОЙ ЗАДАЧИ (любого коммита): класс расхождения,
    точка, строка вердикта (`runs.last_verify`, DEV-89). Кончилась
    `unobservable` в той же точке — следующую не ставим: наблюдать негде
    по-прежнему, а DEV-43 так набрал четыре `PARTIAL` за $21,45. Точка
    сравнивается нарочно: «выката не было» до мержа не значит, что и прод
    после мержа наблюдать негде (DEV-59, DEV-20, DEV-4, DEV-48 — до мержа
    наблюдать было нечего, после выката проверка дала `OK`).

    `mergeable` — вливается ли ветка в основную (DEV-109): True / False /
    None («не спрашивали» и «хостинг не сказал» — одно и то же). Решение
    ворот он не меняет ни при каком значении: несливаемую ветку проверять
    можно, и её проверка — единственный способ узнать, что работа сделана.
    Факт едет в задачу и дальше в раннер: в стопку «ждёт мержа» такую работу
    не пустит маршрут вердикта (`runner.verdict_route`).

    `cleared_at` / `cleared_by` / `cleared_why` — сигнал человека «препятствие
    снято» (`runs.clearance`, DEV-105). Блокировку он отпускает, только если
    ПОЗЖЕ той проверки, которая её держит (`prior_at`): следующая
    `unobservable` запирает задачу снова, и лежалый сигнал не отпирает её
    задним числом. Времени у блокирующей проверки нет (`prior_at` 0, записи
    прежних версий) — сигнал считается свежее: он явное распоряжение человека,
    и отказать ему по недостающей метке хуже, чем поднять одну сессию. Запись прошлой проверки при этом не трогается — она
    свидетельство состоявшегося; снятие едет отдельной строкой в запись
    запуска, чтобы в журнале было видно, кто и когда его дал.
    """
    if exists is False:
        return Skip(f"задачи {ident} нет в трекере")
    if located is None:
        if sha:
            # Событие выката назвало коммит, а его нет ни в одном клоне.
            return Skip(f"коммита {sha[:8]} задачи {ident} нет ни в одном "
                        f"клоне площадки — наблюдать нечего")
        return Skip(f"ни ветки {ref}, ни коммита с именем {ident} в main — "
                    f"наблюдать нечего")
    sha = located.sha if isinstance(located, Located) else ""
    # Повтор того же коммита НЕ отсекаем. Решение владельца 15.09.2026 после
    # прогона по 56 записанным сессиям: из девяти повторов в пяти второй
    # вердикт отличался от первого, а в двух (DEV-16, DEV-18) вторая проверка
    # нашла настоящие поломки, пропущенные первой — PARTIAL превратился в FAIL
    # с находками. Экономия $22,69 не стоит пропущенных находок; повторные
    # проверки помогают. Прежний вердикт остаётся ФАКТОМ для проверяющего
    # (`prior` едет в запись задачи), но постановку не отменяет.
    ancestor = located.ancestor if isinstance(located, Located) else None
    mode = mode_for(entry, hint, ancestor)
    notes = tuple(notes)
    if prior_class == CLASS_UNOBSERVABLE and point_of(prior_mode) == point_of(mode):
        if cleared_at > prior_at:
            # Сигнал человека свежее той проверки, которая держит блокировку:
            # препятствие снято, ставим. В запись запуска — кто и когда его
            # дал: иначе поднятая сессия ничем не отличается от сессии,
            # поднятой в обход сторожа.
            notes += (f"блокировка «{CLASS_UNOBSERVABLE}» снята "
                      f"{when(cleared_at)}{f' ({cleared_by})' if cleared_by else ''}"
                      f"{f': {cleared_why}' if cleared_why else ''}",)
            log.info("проверка %s (%s): блокировка «%s» снята сигналом %s%s — ставлю",
                     ident, point_of(mode), CLASS_UNOBSERVABLE, when(cleared_at),
                     f" ({cleared_by})" if cleared_by else "")
        else:
            stale = (f" Сигнал «препятствие снято» есть ({when(cleared_at)}), но он "
                     f"старше этой проверки — значит, относился к прежней, и "
                     f"блокировку не снимает." if cleared_at else "")
            return Skip(f"прошлая проверка {ident} ({point_of(mode)}) кончилась классом "
                        f"{CLASS_UNOBSERVABLE} — наблюдать было негде, и до перемены "
                        f"точки следующая не ставится. Препятствие снято — скажите "
                        f"«/{runs.UNBLOCK_COMMAND} {ident} <что изменилось>»."
                        + stale, prior=prior_line, claim=claim)
    return Ok(mode=mode,
              ref=located.ref if isinstance(located, Located) else ref,
              commit_sha=sha, criteria=criteria, rounds=rounds,
              prior=seen or "", mergeable=mergeable,
              issue=dict(issue or {}), notes=tuple(notes), claim=claim)


def write_skip_record(work_root: str, task: dict, skip: Skip) -> str:
    """Запись отсева в рабочем каталоге — в той же форме, что у сессии:
    `verdict.txt`, `stats.json`, итоговая запись `transcript.jsonl`. Журнал
    запусков (`sessions.walk`) читает ровно эти файлы, и отсев виден в нём
    заходом за $0 с исходом `CANCELLED` — а не исчезает бесследно, как
    исчезала бы строка в логе. Возвращает каталог записи."""
    ident = str(task.get("identifier") or "UNKNOWN")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(work_root, f"{ident}-verify-{stamp}")
    os.makedirs(path, exist_ok=True)
    line = f"SKIP {skip.reason}"
    with open(os.path.join(path, "verdict.txt"), "w") as f:
        f.write(line + "\n")
        if skip.prior:
            f.write(f"прежний вердикт: {skip.prior}\n")
        f.write("Решение приняли ворота перед постановкой (deploy.preflight); "
                "сессия не поднималась.\n")
    with open(os.path.join(path, "transcript.jsonl"), "w") as f:
        f.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                            "num_turns": 0, "duration_ms": 0, "total_cost_usd": 0.0,
                            "result": line}, ensure_ascii=False) + "\n")
    with open(os.path.join(path, "stats.json"), "w") as f:
        json.dump({"задача": ident, "род": "verify", "слот": 0, "секунд": 0,
                   "пик_мб": 0, "код": 0, "когда": datetime.now(timezone.utc).timestamp(),
                   "исход": "CANCELLED", "ворота": skip.reason,
                   "коммит": str(task.get("commit_sha") or "")},
                  f, ensure_ascii=False)
    return path


# ── заначка ──────────────────────────────────────────────────────────────

def task_key(identifier: str) -> str:
    return f"agent:task:{identifier}"


async def remember_task(store, task: dict) -> bool:
    """Отложить поставленную задачу для проверки выката. True — отложили.

    Зовётся ПОСЛЕ постановки, и отказ здесь постановку не отменяет: задача
    уже в очереди, и назвать её неудачей значило бы соврать про исход —
    человек переназначил бы задачу и получил вторую сессию на ту же ветку.
    Без заначки проверка не состоится, и это видно в журнале.
    """
    ident = str(task.get("identifier") or "")
    if not ident:
        return False
    try:
        await store.put(task_key(ident), json.dumps(task, ensure_ascii=False),
                        STASH_TTL_SEC)
        return True
    except Exception as e:
        log.warning("заначка %s не записана (%s) — выкат этой задачи "
                    "проверен не будет", ident, e)
        return False


async def stashed_task(store, identifier: str) -> dict:
    """Что помним о задаче по идентификатору. Пусто — не помним."""
    try:
        raw = await store.get(task_key(identifier))
    except Exception as e:
        log.warning("заначка %s не прочитана: %s", identifier, e)
        return {}
    if not raw:
        return {}
    try:
        rec = json.loads(raw)
    except Exception:
        return {}
    return rec if isinstance(rec, dict) else {}


# ── что за инсталляцией ──────────────────────────────────────────────────

class Deployment(Protocol):
    """Откуда инсталляция знает, что работа агента выкатилась.

    Один вопрос на одно событие хостинга. Ответ — словарь фактов о выкате,
    либо None: «это не выкат работы агента» (чужая ветка, красный пайплайн,
    откат, промежуточный статус). Что считать своим, решает инсталляция:
    у первого клиента это мердж-коммит `Merge branch 'agent/<TASK-N>'` в
    релизной ветке, и разбор его — со всеми ловушками вроде реверта, где то
    же имя лежит цитатой в теле, — остаётся у неё.

    Обязательное поле одно — `identifier` (TASK-N, DOK-N): по нему находится
    заначка. Остальное уезжает в задачу проверяльщику как есть и читается
    контрактом сессии (`prompt.build_verify_prompt`):

      commit_sha    — что именно выкатили; с ним сверяют прод. Не «вершина
                      ветки»: пока задача ехала, туда могли влить ещё что-то
      ref           — релизная ветка
      project_path  — репозиторий, как его зовёт хостинг
      mr_iid        — номер MR/PR
      pipeline_id, pipeline_url — где смотреть журнал выката
      deploy_jobs   — какие джобы раскатили; пусто — выкатывать было нечего
      deployed_at   — с какого момента в проде новый код (граница «до/после»
                      для сравнения журнала сервиса)

    Метод синхронный и без сети: это разбор уже полученного тела. Дедуп по
    коммиту — дело продукта, второй раз про один выкат отвечать можно.

    Второй вопрос — НЕОБЯЗАТЕЛЬНЫЙ: `merged(payload) -> dict | None`, «это
    мерж работы агента в основную ветку, и какой?». Ответ той же формы, что у
    `merged_work` ниже. Метода нет — продукт разбирает событие сам; есть —
    его ответ старше.
    """

    def landed(self, payload: dict) -> dict | None: ...


# ── мерж в основную ветку — принятие работы человеком ────────────────────
#
# Схема ветвления одна и не обсуждается: агентские ветки `agent/<TASK-N>`, из
# них MR в основную (`main` или `master` — у репозиториев по-разному). Мерж в
# основную — сознательное решение человека, и после него решать нечего:
# задача принята. Решение владельца 13.09.2026: `Done` = проверено и сошлось,
# ЛИБО принято человеком сознательно. До 15.09.2026 второй половины не было:
# `Done` ставила только проверяющая сессия своим вердиктом, а событий о мерже
# приёмник не разбирал вовсе — он слушал только прогоны (`workflow_run`).
# Итог того дня: одиннадцать смерженных задач висели на доске в «In Review»
# («ждёт решения о мерже» — уже смержено) и в «Not Verified». Статус врал.
#
# Что здесь ПРОДУКТ: разбор двух форм события «PR смержен» — GitHub
# `pull_request` с `action: closed` и `merged: true`, GitLab `merge_request` с
# `action: merge`, — имя задачи из имени ветки, дедуп по PR, перевод в `Done`
# с комментарием в задаче и строкой в журнале. Инсталляция МОЖЕТ ответить на
# вопрос «это мерж работы агента, и какой?» сама — методом `merged(payload)`
# у реализации `Deployment`; тогда её ответ старше разбора продукта. Нет
# метода — продукт разбирает сам: форм две, обе известные и стабильные, а
# держать это правило в каждой инсталляции значит однажды получить две
# разные его версии.
#
# Мерж СТАРШЕ ВЕРДИКТА. Проверка до мержа могла не успеть или кончиться
# `FAIL`; человек это видел и смержил — значит, принял. Раннер, доводя маршрут
# по вердикту (`runner.ensure_verdict_route`), закрытую задачу не увозит из
# `Done` — это закреплено там и тестом.

MAIN_BRANCHES = ("main", "master")

# Идентификатор задачи: `DEV-86`, `TASK-200`. Не `\b`: `DEV-8` не должно ловить
# `DEV-81`, а `agent/DEV-81-silent` — должно (то же правило у `gitref`).
IDENT_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6}-\d+)(?![0-9])")

# Сколько помним, что этот PR уже разобран: две доставки одного события
# (ретрай хостинга, ручной «Redeliver») — одна запись, один комментарий.
MERGED_TTL_SEC = 30 * 24 * 3600

# Куда закрывается принятая работа. Та же переменная, что у раннера
# (`runner.DONE_STATE_NAME`): одно имя, одно значение, два потребителя —
# закреплено в `core/test_verify_bridge.py`.
DONE_STATE_VAR = "AGENT_DONE_STATE_NAME"
DEFAULT_DONE_STATE = "Done"

# Куда уезжает отменённая человеком задача («Отменить» в чате, DEV-123).
# Своё имя, а не `Done`: «принято» и «не нужно» — два разных ответа на
# работу, и считаются они по доске раздельно.
CANCEL_STATE_VAR = "AGENT_CANCELED_STATE_NAME"
DEFAULT_CANCEL_STATE = "Canceled"

# Типы состояний, в которых задача уже закрыта, — типы трекера, не имена:
# имена у каждой доски свои (те же, что у `issue_feed.CLOSED_TYPES`; импорт
# оттуда был бы циклом через `webhook`).
CLOSED_STATE_TYPES = ("completed", "canceled")


def is_merge_event(payload: dict) -> bool:
    """Событие про PR/MR, а не про прогон. GitHub кладёт `pull_request` на
    верхний уровень — у `workflow_run` его там нет, только список внутри
    прогона; GitLab называет род события в `object_kind`."""
    return (isinstance(payload.get("pull_request"), dict)
            or payload.get("object_kind") == "merge_request")


def closed_unmerged(payload: dict) -> bool:
    """PR закрыт БЕЗ мержа: у GitHub `closed` с `merged: false`, у GitLab
    `action: close`. Отдельный вопрос, потому что это единственный не-мерж,
    о котором стоит сказать в журнале: остальные действия (открыт, обновлён,
    помечен) — шум."""
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        return payload.get("action") == "closed" and not pr.get("merged")
    if payload.get("object_kind") == "merge_request":
        return (payload.get("object_attributes") or {}).get("action") == "close"
    return False


def ident_in(*texts: str) -> str:
    """Первый идентификатор задачи в текстах по порядку. Порядок — это
    надёжность: имя ветки, потом заголовок, потом тело."""
    for text in texts:
        m = IDENT_RE.search(text or "")
        if m:
            return m.group(1)
    return ""


def merged_work(payload: dict, main_branches=MAIN_BRANCHES) -> dict | None:
    """Факты о мерже из события хостинга, либо None — «это не мерж в основную
    ветку»: не PR-событие, PR открыт или обновлён, закрыт без мержа, влит не
    в основную.

    Ответ: `identifier` (пусто — ветка, заголовок и тело задачу не называют;
    вызывающий об этом говорит), `mr_iid`, `ref` (куда), `source_ref`
    (откуда), `project_path`, `merge_commit_sha`, `url`, `merged_by`,
    `merged_at`.

    Имя задачи — из имени ветки-источника: агентская ветка называется
    `agent/<TASK-N>[-суффикс]`, и это надёжнее заголовка. Заголовок PR
    человек правит как хочет, мерж-коммит GitHub называют как угодно, а
    сквош теряет имя ветки вовсе — 12.09.2026 так потерялась DEV-62. В ветке
    имени нет — заголовок, потом тело: PR человека на задачу агента.
    """
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        if payload.get("action") != "closed" or not pr.get("merged"):
            return None
        base = str((pr.get("base") or {}).get("ref") or "")
        head = str((pr.get("head") or {}).get("ref") or "")
        facts = {
            "mr_iid": str(pr.get("number") or payload.get("number") or ""),
            "ref": base, "source_ref": head,
            "project_path": str((payload.get("repository") or {}).get("full_name") or ""),
            "merge_commit_sha": str(pr.get("merge_commit_sha") or ""),
            "url": str(pr.get("html_url") or ""),
            "merged_by": str((pr.get("merged_by") or {}).get("login") or ""),
            "merged_at": str(pr.get("merged_at") or ""),
        }
        texts = (head, str(pr.get("title") or ""), str(pr.get("body") or ""))
    elif payload.get("object_kind") == "merge_request":
        attrs = payload.get("object_attributes") or {}
        # Ровно `merge`, а не `state: merged`: правка метки на уже влитом MR
        # приезжает как `update` с тем же `state`, и это не второй мерж.
        if attrs.get("action") != "merge":
            return None
        base = str(attrs.get("target_branch") or "")
        head = str(attrs.get("source_branch") or "")
        facts = {
            "mr_iid": str(attrs.get("iid") or ""),
            "ref": base, "source_ref": head,
            "project_path": str((payload.get("project") or {}).get("path_with_namespace") or ""),
            "merge_commit_sha": str(attrs.get("merge_commit_sha") or ""),
            "url": str(attrs.get("url") or ""),
            "merged_by": str((payload.get("user") or {}).get("username") or ""),
            "merged_at": str(attrs.get("merged_at") or attrs.get("updated_at") or ""),
        }
        texts = (head, str(attrs.get("title") or ""), str(attrs.get("description") or ""))
    else:
        return None
    if main_branches and base not in main_branches:
        return None
    return {"identifier": ident_in(*texts), **facts}


def closed_state(state: dict, done_state: str = "") -> bool:
    """Задача уже закрыта: по ТИПУ состояния, а имя `Done` — запасной
    признак на случай, если событие или трекер тип не отдали."""
    state = state or {}
    if str(state.get("type") or "").strip().lower() in CLOSED_STATE_TYPES:
        return True
    name = str(state.get("name") or "").strip()
    return bool(done_state) and name.casefold() == done_state.casefold()


# ── проверка выката ──────────────────────────────────────────────────────

# Заголовок с подписью у хостинга репозиториев свой, и формат тоже: не голый
# hex, как у трекера, а с приставкой вида `sha256=`. Держим это параметром —
# инсталляции на разных хостингах отличаются ровно этим.
DEPLOY_SIGNATURE_HEADER = "x-hub-signature-256"
DEPLOY_SIGNATURE_PREFIX = "sha256="


def same_digest(got: str, expected: str) -> bool:
    """Сравнение подписи — постоянное по времени, иначе она подбирается
    побайтово. Общее для обоих приёмников, поэтому живёт здесь: `webhook`
    зависит от этого модуля, обратная зависимость была бы циклом.

    Сравниваем БАЙТЫ, а не строки: `hmac.compare_digest` на строках требует
    ASCII и на чём угодно другом кидает `TypeError`. Заголовки читаются как
    latin-1, так что ОДИН байт >0x7F в подписи — и вместо 401 приёмник отвечал
    500 со стектрейсом, предупреждение «подпись не сошлась» не писалось
    никогда, а хостинг ретраил доставку, которую надо было отвергнуть.
    Снаружи это был способ лить трейсы в журнал одним curl.
    """
    return hmac.compare_digest(got.encode("latin-1", "replace"),
                               expected.encode("ascii"))


class DeployReceiver:
    """Событие хостинга репозиториев → проверка выката.

    Отдельный приёмник, а не ветка в трекерном: подпись считается иначе, и
    смешивать две схемы в одном месте значит однажды проверить тело чужим
    секретом. Сидит на том же порту, на другом пути (`webhook.Server`).
    """

    def __init__(self, verification: "Verification", secret: str = "",
                 prefix: str = DEPLOY_SIGNATURE_PREFIX):
        self.verification = verification
        self.secret = secret
        self.prefix = prefix

    def signed(self, body: bytes, signature: str) -> bool:
        """Подпись тела. Сравнение постоянное по времени; приставку снимаем,
        но не требуем — у разных хостингов она своя или её нет вовсе."""
        if not (self.secret and signature):
            return False
        got = signature.strip()
        if self.prefix and got.startswith(self.prefix):
            got = got[len(self.prefix):]
        expected = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        return same_digest(got, expected)

    async def handle(self, body: bytes, signature: str) -> tuple[int, dict]:
        if not self.secret:
            return 503, {"error": "секрет вебхука выката не задан"}
        if not self.signed(body, signature):
            log.warning("выкат: подпись не сошлась, тело %d байт", len(body))
            return 401, {"error": "подпись не сошлась"}
        try:
            payload = json.loads(body)
        except Exception:
            return 400, {"error": "тело не разбирается как json"}
        if not isinstance(payload, dict):
            return 400, {"error": "тело не объект"}
        if is_merge_event(payload):
            # Тот же путь и та же подпись: хостинг шлёт все подписанные
            # события на один адрес, а различаются они формой тела. Не
            # успех — только осечка трекера (503, ретрай сделает перевод) и
            # упавший разбор инсталляции (500).
            out = await self.verification.on_merge(payload)
            code = {"tracker-failed": 503, "deployment-failed": 500}.get(out, 200)
            return code, {"ok": code == 200, "merge": out}
        out = await self.verification.on_event(payload)
        # 2xx — только когда событие разобрано и с ним сделано то, что надо:
        # «не наш выкат», «уже проверяли» и «точка выключена настройкой»
        # (`disabled`) тоже успех, просить хостинг повторить бессмысленно.
        # Два исхода — не успех:
        #   • `enqueue-failed` — наш выкат, поставить не смогли: 503, и ретрай
        #     доставки ровно то, чего мы хотим;
        #   • `deployment-failed` — разбор инсталляции упал: 500. Ретрай не
        #     поможет (ошибка детерминированная), но отвечать `ok: true` на
        #     СВОЮ поломку нельзя: выкат остаётся непроверенным, в чате тишина,
        #     единственный след — трейс в журнале, а доставка у хостинга
        #     зелёная. Ровно так месяцами молчал детектор долгих запросов.
        code = {"enqueue-failed": 503, "deployment-failed": 500}.get(out, 200)
        return code, {"ok": code == 200, "verify": out}


class Verification:
    def __init__(self, store, queue_key: str, deployment: Deployment | None = None,
                 agent_user_id: str = "", feed=None, transport=None,
                 notify_chat: str = "", resolve_issue=None, lookup=None,
                 repos=None, work_root: str = "", points: dict | None = None,
                 tracker=None, done_state: str = "", cancel_state: str = "",
                 hosting=None):
        """`deployment` — реализация протокола или None: инсталляция без неё
        ничего не ставит и молчит, но вердикты из моста разбирает.

        `tracker` — трекер инсталляции (`tracking.Tracker`): по нему мерж PR
        закрывает задачу (`on_merge`). Нужны `issue_by_identifier`,
        `set_state` и `comment`; без трекера или без `set_state` перевод не
        делается, и это громко в журнале. `done_state` — имя закрытого
        состояния (`AGENT_DONE_STATE_NAME`), пусто — `Done`; `cancel_state` —
        имя отменённого (`AGENT_CANCELED_STATE_NAME`), пусто — `Canceled`:
        туда уезжает задача по кнопке «Отменить» из чата.

        `points` — раскладка точек проверки (`verify_points`): какая из двух
        включена. Не передана — читается из окружения при создании. Точка
        выключена — сессия в очередь не ставится, и это единственное, что
        выключатель делает: ворота, дедуп и разбор вердиктов работают как
        прежде.

        `lookup(identifier) -> dict | None` — задача целиком (id, title, url,
        description) или None, если её нет; может быть асинхронной. Это
        первая проверка ворот и она же адрес вердикта. Осечка трекера —
        исключение, и оно НЕ значит «задачи нет»: сессия ставится.

        `repos` — `gitref.Repos` площадки: есть ли ветка и коммит, влит ли
        он в main. Нет — ворота не сверяют ветку, режим берётся как раньше.

        `work_root` — рабочий каталог раннера: по нему считаются круги задачи
        и туда же ложится запись отсева (verdict.txt, написанный кодом).

        `feed` — лента задачи (`issue_feed.IssueFeed`): куда дописывается
        вердикт. `transport` и `notify_chat` — запасной путь, когда сообщения
        задачи в чате нет: тогда вердикт уходит короткой строкой в общий чат,
        иначе он потерян.

        `resolve_issue(identifier) -> issue_id` — как инсталляция находит id
        задачи по идентификатору, когда заначки нет. Может быть асинхронной.
        Зовётся только в этом случае: с заначкой поход в трекер на каждый
        выкат — лишний сетевой запрос. Пусто или осечка — проверка ставится
        без адреса, и вердикт уйдёт отдельным сообщением, а не строкой в
        сообщение задачи (у первого клиента 02.09.2026, TASK-193: два полотна
        в личке при пустой отметке в чате)."""
        self.store = store
        self.queue_key = queue_key
        self.deployment = deployment
        self.agent_user_id = str(agent_user_id or "")
        self.feed = feed
        self.transport = transport
        self.notify_chat = str(notify_chat or "")
        self.resolve_issue = resolve_issue
        self.lookup = lookup
        self.repos = repos
        self.work_root = str(work_root or "")
        self.points = dict(points) if points is not None else verify_points()
        self.tracker = tracker
        self.done_state = str(done_state or "").strip() or DEFAULT_DONE_STATE
        self.cancel_state = str(cancel_state or "").strip() or DEFAULT_CANCEL_STATE
        # Хостинг (`hosting.Hosting`) — единственный, кто отвечает на вопрос
        # «вольётся ли ветка» тем же способом, каким это узнает кнопка мержа
        # (DEV-109). Нет его — ворота этот факт не сверяют и говорят об этом
        # в записи запуска, как про любого промолчавшего сотрудника.
        self.hosting = hosting
        # Кому сказать, что в основную ветку что-то уехало (DEV-109). Ставит
        # приложение: `reconcile.Reconciler.wake`. Мерж — единственный
        # момент, когда чужие ветки становятся несливаемыми, и пересмотр
        # стопки человека по часам об этом узнаёт на час позже, чем он
        # случился. Нет подписчика — ничего не меняется.
        self.after_merge = None

    async def _merged_happened(self, ident: str) -> None:
        """Сказать подписчику, что мерж состоялся. Осечка — в журнал: сам
        мерж уже разобран, и ронять его исход из-за пересмотра нельзя."""
        tell = self.after_merge
        if not callable(tell):
            return
        try:
            got = tell(f"смержена работа {ident}")
            if inspect.isawaitable(got):
                await got
        except Exception as e:
            log.warning("мерж %s: пересмотр стопки не разбудить: %s", ident, e)

    @staticmethod
    def verified_key(identifier: str, mark: str) -> str:
        return f"agent:verify:{identifier}:{mark}"

    @staticmethod
    def verdict_key(identifier: str, sha: str) -> str:
        """Прежний вердикт по коммиту — чтобы отсев «уже проверяли» называл
        его, а не просто факт."""
        return f"agent:verify:verdict:{identifier}:{sha}"

    @staticmethod
    def pending_key(identifier: str, sha: str) -> str:
        """Захват постановки проверки — ОДИН на все её входы.

        По коммиту, а не по задаче и не по событию: вход, пришедший вторым,
        про первый ничего не знает, и различить их может только то, что у них
        одинаково. Одинаков у них выкаченный (или лежащий в ветке) коммит —
        его оба входа выясняют у ворот перед постановкой.

        Не `verified_key`: та метка живёт 30 суток и означает «этот коммит мы
        видели», а здесь нужен другой факт — «проверка по нему ПОСТАВЛЕНА и
        ещё не ответила». Слив их в один ключ, повтор после вердикта
        (разрешённый нарочно) оказался бы запертым на месяц.
        """
        return f"agent:verify:pending:{identifier}:{sha}"

    async def _claim_pending(self, ident: str, sha: str, entry: str) -> str:
        """Занять постановку проверки этого коммита. Пусто — заняли мы и
        сессия ставится; иначе исход для журнала и ответа приёмнику.

        Коммит неизвестен (нет клонов площадки, git молчит) — не запираем
        ничего: отличить повтор от новой работы тогда нечем, а отсечь
        настоящую проверку из-за несверенного факта здесь так же дорого, как
        в самих воротах.

        Отказ обязан НАЗЫВАТЬ причину: двойную постановку 15.09.2026 увидели
        только по двум строкам старта в журнале раннера, и молчаливый
        `deduped` был бы неотличим от потерянного события.
        """
        if not sha:
            return ""
        try:
            if await self.store.claim(self.pending_key(ident, sha), VERIFY_PENDING_TTL_SEC):
                return ""
        except Exception as e:
            log.error("проверка %s (%s): общий захват не отработал (%s) — не ставлю",
                      ident, entry, e)
            return "enqueue-failed"
        log.info("проверка %s (%s): проверка коммита %s уже поставлена другим "
                 "входом и ещё не ответила — вторую сессию не завожу",
                 ident, entry, sha[:8])
        return "deduped"

    @staticmethod
    def merged_key(identifier: str, where: str) -> str:
        """Метка «этот PR уже разобран». По PR, а не по задаче: у задачи
        бывает второй PR (`-v2`), и его мерж — своё событие."""
        return f"agent:merged:{identifier}:{where}"

    async def on_state(self, payload: dict) -> str:
        """Задачу перевели в состояние проверки → проверяльщик в очереди.

        Главный вход с DEV-78: рабочая сессия заканчивает работу переводом
        СЮДА, а не к человеку. Значит переход в это состояние почти всегда
        означает «работа лежит в ветке, MR открыт, до людей не доехало» — то
        есть проверку ДО мержа.

        Поэтому `trigger` здесь `branch`, а не `state`. Разница не в слове:
        от неё зависит врезка контракта и, главное, ИСХОД. При `state`
        проверяющий получает старые исходы («Done постановщику / Not
        Verified»), и весь новый порядок оказывается мёртвым — проверено
        исполнением живого кода 13.09.2026, вердикт по DEV-78.

        Ветку берём по соглашению `agent/<номер>`: её так называет рабочая
        сессия. Ветки может не оказаться (человек перевёл руками задачу, где
        кода нет вовсе) — тогда проверяющий это обнаружит и скажет; это
        честный `PARTIAL`, а не молчаливая проверка не того.

        Проверяющий в обоих случаях пропускает уровни про доставку: сверять
        нечего, прод в эту минуту никто не трогал.
        """
        data = dict(payload.get("data") or {})
        ident = str(data.get("identifier") or "")
        if not ident:
            return "not-ours"
        if self._nothing_on("проверка", ident):
            return "disabled"
        # Метка — момент перехода, а не имя задачи. Две доставки одного события
        # несут одно время и дедуплицируются; а вот задача, вернувшаяся на
        # доработку и переведённая в проверку ВТОРОЙ раз, несёт новое — и
        # обязана проверяться заново. Ключ по одному идентификатору запер бы
        # её навсегда.
        stamp = str(data.get("updatedAt") or data.get("updated_at") or "")
        if not stamp:
            # Без времени метка была бы одна на все переходы этой задачи, и
            # ВТОРОЙ перевод в проверку молча получал бы «уже проверяли» —
            # на все 30 суток жизни метки. Дубль проверки стоит одной сессии,
            # месяц тишины стоит непроверенной работы.
            stamp = datetime.now(timezone.utc).isoformat()
            log.warning("проверка %s: в событии нет времени перехода — метка "
                        "по текущему времени, повторная доставка может дать "
                        "вторую сессию", ident)
        mark = "state:" + stamp
        key = self.verified_key(ident, mark)
        try:
            if not await self.store.claim(key, VERIFIED_TTL_SEC):
                return "deduped"
        except Exception as e:
            log.error("проверка %s: дедуп не отработал (%s) — не ставлю", ident, e)
            return "enqueue-failed"
        known = await stashed_task(self.store, ident)
        if self.agent_user_id and str(known.get("requested_by") or "") == self.agent_user_id:
            # Иначе подтверждённая задача уехала бы назад на агента, а
            # назначение на агента — это новая рабочая сессия.
            known = {k: v for k, v in known.items()
                     if k not in ("requested_by", "requested_by_name")}
        # Адрес и заголовок берём из события: заначки может не быть вовсе, а
        # без issue_id вердикт не доедет до сообщения задачи в чате.
        task = {**known, "kind": "verify", "trigger": "branch",
                "ref": known.get("ref") or f"agent/{ident}",
                "identifier": ident,
                "issue_id": str(data.get("id") or known.get("issue_id") or ""),
                "title": data.get("title") or known.get("title") or "",
                "url": data.get("url") or known.get("url") or "",
                "queued_at": datetime.now(timezone.utc).isoformat()}
        # Ворота — ДО постановки: есть ли задача, ветка, коммит, не проверяли
        # ли его уже, и в каком режиме проверять. Метка перехода остаётся в
        # любом случае: событие разобрано, повторная доставка не нужна.
        gate = await self.preflight(task, entry="state")
        if isinstance(gate, Skip):
            if gate.claim:
                # Метку коммита ворота поставили сами, а сессии не будет —
                # снимаем: иначе следующая постановка этого коммита увидела
                # бы «уже проверяли» без единой проверки.
                await self._release(gate.claim, ident)
            await self._record_skip(task, gate)
            return "skipped"
        if self._switched_off("проверка", ident, gate):
            # Метки снимаем обе: точку включат переменной, и этот же переход
            # (повторная доставка) или этот же коммит обязаны проверяться —
            # иначе выключатель односторонний.
            await self._release(key, ident)
            if gate.claim:
                await self._release(gate.claim, ident)
            return "disabled"
        task = self._apply(task, gate)
        # Общий захват — ПОСЛЕ ворот (только они знают коммит) и ДО очереди.
        # Метку перехода при отказе не снимаем: событие разобрано, сессия по
        # этому коммиту есть, просто завёл её другой вход.
        sha = str(task.get("commit_sha") or "")
        busy = await self._claim_pending(ident, sha, "статус")
        if busy == "enqueue-failed":
            # Стор молчит — трекер повторит доставку (503), и повтор обязан
            # пройти: метки снимаем по тому же правилу, что и на провале
            # очереди ниже. Иначе сессии нет, а вторая доставка получает
            # «deduped» — проверка заперта, и не видно этого ничем.
            await self._release(key, ident)
            if gate.claim:
                await self._release(gate.claim, ident)
        if busy:
            return busy
        try:
            await self.store.enqueue(self.queue_key,
                                     json.dumps(task, ensure_ascii=False))
        except Exception as e:
            # Метка ставится ДО постановки. Не снять её здесь значит запереть
            # проверку этой задачи насовсем: повторная доставка получила бы
            # «deduped», а сессии так и не было. Общий захват — тем же
            # рассуждением: он запер бы и второй вход, а сессии не случилось.
            await self._release(key, ident)
            await self._release(self.pending_key(ident, sha), ident)
            if gate.claim:
                await self._release(gate.claim, ident)
            log.error("проверка %s: проверяльщик не поставлен: %s", ident, e)
            return "enqueue-failed"
        log.info("проверка %s: статус → проверяльщик в очереди (режим %s, %s)",
                 ident, task.get("trigger"), (task.get("commit_sha") or "?")[:8])
        return "queued"

    async def on_unblocked(self, ident: str) -> str:
        """Человек сказал «препятствие снято» (`commands.Unblock`) → тот же
        вход, что и перевод задачи в проверку.

        Отдельного пути постановки здесь нет НАРОЧНО. Сигнал уже лежит в
        рабочем каталоге фактом, и ворота прочтут его сами; поставь мы сессию
        мимо них — и команда стала бы вторым способом заводить проверку, со
        своими правилами про задачу, ветку, коммит и выключенную точку. Ответ
        тот же, что у `on_state`: `queued`, `skipped`, `disabled`, …

        Адрес задачи берётся из заначки (`agent:task:<ident>`), как при выкате:
        события трекера тут нет, и `id` взять неоткуда.
        """
        return await self.on_state({"data": {
            "identifier": ident,
            "updatedAt": datetime.now(timezone.utc).isoformat()}})

    async def on_event(self, payload: dict) -> str:
        """Событие хостинга → проверяльщик в очереди. Возвращает исход для
        журнала и ответа приёмнику инсталляции."""
        if self.deployment is None:
            return "no-deployment"
        try:
            target = self.deployment.landed(payload)
        except Exception as e:
            # Разбор инсталляции упал — это её ошибка, а не «не наш выкат»,
            # и в журнале они должны различаться.
            log.exception("выкат: разбор события не отработал: %s", e)
            return "deployment-failed"
        ident = str((target or {}).get("identifier") or "")
        if not ident:
            return "not-ours"
        if self._nothing_on("выкат", ident):
            return "disabled"
        # Один выкаченный коммит — одна проверка, сколько бы пайплайнов на нём
        # ни собралось: ручной «Run pipeline», расписание, повторный деплой
        # соседнего сервиса. Каждый поднимал бы вторую сессию по уже
        # подтверждённой задаче: слот занят, задача переназначена повторно, в
        # чате дубль вердикта. Нет ни sha, ни id пайплайна — ключ по одной
        # задаче: лучше одна проверка на задачу, чем по одной на доставку.
        mark = str(target.get("commit_sha") or target.get("pipeline_id") or "")
        key = self.verified_key(ident, mark)
        try:
            if not await self.store.claim(key, VERIFIED_TTL_SEC):
                return "deduped"
        except Exception as e:
            log.error("выкат %s: дедуп не отработал (%s) — не ставлю", ident, e)
            return "enqueue-failed"
        known = await stashed_task(self.store, ident)
        if not known:
            # Заначки нет — истекла, задача заведена до неё или ветку собрал
            # человек. Проверка всё равно ставится: непроверенный выкат хуже,
            # чем вердикт без адреса. Сессия найдёт задачу по идентификатору,
            # а вердикт без issue_id уйдёт отдельным сообщением.
            log.info("выкат %s: заначки нет — проверяльщик без постановщика",
                     ident)
        # Постановщик-агент замкнул бы круг: подтверждённая задача уехала бы
        # назад на агента, а назначение на агента — это новая рабочая сессия.
        if self.agent_user_id and str(known.get("requested_by") or "") == self.agent_user_id:
            known = {k: v for k, v in known.items()
                     if k not in ("requested_by", "requested_by_name")}
        # Факты о выкате — ПОВЕРХ заначки, а kind — поверх всего: в заначке
        # лежит рабочая задача, и её `kind` (work, incident) протекать сюда не
        # должен.
        task = {**known, **target, "kind": "verify",
                "queued_at": datetime.now(timezone.utc).isoformat()}
        if not task.get("issue_id") and self.resolve_issue:
            # Адрес вердикта — id задачи. Без него отметка «выкат проверен»
            # не доедет до сообщения задачи. Осечка трекера проверку не
            # отменяет: лучше вердикт без адреса, чем никакого.
            try:
                issue_id = self.resolve_issue(ident)
                if inspect.isawaitable(issue_id):
                    issue_id = await issue_id
                if issue_id:
                    task["issue_id"] = str(issue_id)
            except Exception as e:
                log.warning("выкат %s: id задачи не найден (%s) — вердикт "
                            "уйдёт отдельным сообщением", ident, e)
        # Ворота — ДО постановки. Метку коммита мы уже поставили сами (`key`):
        # воротам про это сказано, чтобы они не приняли её за «уже проверяли».
        gate = await self.preflight(task, entry="event", claimed=key)
        if isinstance(gate, Skip):
            if not gate.seen:
                # Задачи или коммита нет — метку снимаем: появится задача и
                # её переведут в проверку, тот же коммит должен проверяться.
                await self._release(key, ident)
            if gate.claim and gate.claim != key:
                await self._release(gate.claim, ident)
            await self._record_skip(task, gate)
            return "skipped"
        if self._switched_off("выкат", ident, gate):
            # Метку коммита снимаем: точку включат переменной, повторный
            # пайплайн на том же коммите обязан поставить проверку, а не
            # получить «deduped» на 30 суток.
            await self._release(key, ident)
            if gate.claim and gate.claim != key:
                await self._release(gate.claim, ident)
            return "disabled"
        task = self._apply(task, gate)
        # Тот же общий захват, что и у входа по статусу: своя метка у каждого
        # входа своя, и без общей один коммит, пришедший обоими путями, даёт
        # две сессии (DEV-110).
        sha = str(task.get("commit_sha") or "")
        busy = await self._claim_pending(ident, sha, "выкат")
        if busy == "enqueue-failed":
            # Как и ниже на провале очереди: сессии не случилось, а метка
            # коммита живёт 30 суток — не сняв её, мы заперли бы проверку
            # этого коммита обоими входами.
            await self._release(key, ident)
            if gate.claim and gate.claim != key:
                await self._release(gate.claim, ident)
        if busy:
            return busy
        try:
            await self.store.enqueue(self.queue_key, json.dumps(task, ensure_ascii=False))
        except Exception as e:
            # Метка ставится ДО постановки — иначе две доставки подряд дали бы
            # две сессии. Но до очереди дело не дошло, и метка запёрла бы
            # проверку насовсем: хостинг не ретраит (ответ 200), а повторный
            # пайплайн получил бы «deduped». Снимаем — вместе с общим захватом,
            # иначе запертым окажется и вход по статусу.
            await self._release(key, ident)
            await self._release(self.pending_key(ident, sha), ident)
            if gate.claim and gate.claim != key:
                await self._release(gate.claim, ident)
            log.error("выкат %s: проверяльщик не поставлен: %s", ident, e)
            return "enqueue-failed"
        log.info("выкат %s (%s) → проверяльщик в очереди (режим %s)", ident,
                 mark[:8] or "?", task.get("trigger"))
        return "queued"

    # ── мерж ─────────────────────────────────────────────────────────────

    async def on_merge(self, payload: dict) -> str:
        """PR смержен в основную ветку → задача в `Done`. Исход — строка для
        журнала и ответа приёмнику.

        Порядок: разбор (инсталляция или продукт) → имя задачи → дедуп по PR
        → задача в трекере → уже закрыта? → перевод → комментарий. Каждый
        отказ называет себя в журнале: 15.09.2026 одиннадцать задач молчали
        на доске именно потому, что молчание было неотличимо от поломки.

        Дедуп — метка в сторе; но она не единственная защита: «уже закрыта»
        отсекает повтор и без стора, поэтому лежачий стор перевод не
        отменяет. Осечка ТРЕКЕРА — отменяет, с 503 приёмнику: метку снимаем,
        повторная доставка сделает перевод.
        """
        answer = getattr(self.deployment, "merged", None)
        if answer is not None:
            try:
                facts = answer(payload)
            except Exception as e:
                log.exception("мерж: разбор события инсталляцией не отработал: %s", e)
                return "deployment-failed"
        else:
            facts = merged_work(payload)
        if not facts:
            if closed_unmerged(payload):
                log.info("мерж: PR закрыт без мержа — задача не принята, ничего не делаю")
            return "not-ours"
        ident = str(facts.get("identifier") or "")
        where = f"{facts.get('project_path') or '?'}#{facts.get('mr_iid') or '?'}"
        if not ident:
            log.warning("мерж %s в %s: задачи в имени ветки «%s», заголовке и теле "
                        "не видно — в Done переводить нечего",
                        where, facts.get("ref") or "?", facts.get("source_ref") or "?")
            return "not-ours"
        if self.tracker is None or not callable(getattr(self.tracker, "set_state", None)):
            log.warning("мерж %s (%s): задача принята мержем, а трекер %s — статус "
                        "на доске не сменится, переведи руками", ident, where,
                        "не подключён" if self.tracker is None else "не умеет менять статус")
            return "no-tracker"
        key = self.merged_key(ident, where)
        try:
            if not await self.store.claim(key, MERGED_TTL_SEC):
                log.info("мерж %s (%s): уже разобран — повторная доставка", ident, where)
                return "deduped"
        except Exception as e:
            log.warning("мерж %s (%s): дедуп не отработал (%s) — иду дальше, повтор "
                        "отсечёт «уже закрыта»", ident, where, e)
            key = ""
        try:
            outcome = await asyncio.to_thread(self._close_by_merge, ident, where, facts)
        except Exception as e:
            if key:
                await self._release(key, ident)
            log.error("мерж %s (%s): статус не сменился: %s", ident, where, e)
            return "tracker-failed"
        # Стопка человека протухает ровно здесь: в основную ветку уехали
        # чужие правки, и чьи-то ветки перестали вливаться. Будим пересмотр
        # независимо от того, чем кончился перевод ЭТОЙ задачи: соседние от
        # него не зависят.
        await self._merged_happened(ident)
        return outcome

    async def accept_merged(self, ident: str, facts: dict, by: str = "") -> str:
        """Мерж уже случился НЕ через событие хостинга — по кнопке на
        странице подписи (`signing.SigningPage`). Та же дорога к `Done`, что
        у `on_merge`, без разбора события и без метки дедупа: событие о
        мерже, если хук его пришлёт, увидит «уже закрыта» и промолчит.

        `facts` — той же формы, что у `merged_work`; `by` — кто нажал, для
        журнала и комментария."""
        if not ident:
            return "not-ours"
        if self.tracker is None or not callable(getattr(self.tracker, "set_state", None)):
            log.warning("мерж по кнопке %s: трекер не подключён или не умеет менять "
                        "статус — задачу переведи руками", ident)
            return "no-tracker"
        where = f"{facts.get('project_path') or '?'}#{facts.get('mr_iid') or '?'}"
        facts = {**facts, "merged_by": facts.get("merged_by") or by}
        try:
            outcome = await asyncio.to_thread(self._close_by_merge, ident, where, facts)
        except Exception as e:
            log.error("мерж по кнопке %s (%s): статус не сменился: %s", ident, where, e)
            return "tracker-failed"
        if by:
            # Только нажатие: сюда же приходит сверка пропущенных мержей
            # (`reconcile.pick_up`, `by` пустое), а её мерж случился днями
            # раньше — будить пересмотр на нём значит крутить лишний круг
            # после каждой подобранной задачи.
            await self._merged_happened(ident)
        return outcome

    async def accept_canceled(self, ident: str, by: str = "") -> str:
        """Задача отменена человеком — кнопкой «Отменить» под сообщением
        задачи в чате (`signoff.SignOff`, DEV-123).

        Отдельно от `accept_done`, потому что это другой ответ на работу:
        `Done` — «принято», `Canceled` — «не нужно». Слив их в один перевод,
        мы потеряли бы единственное место, где видно, сколько работы человек
        отклонил: по доске эти две стопки считаются раздельно.

        PR здесь не трогается вовсе — ни открытый, ни смерженный: закрыть
        чужой PR отдельное решение, и принимать его за человека нельзя.
        Исходы — те же слова, что у соседей (`already-closed`, `no-issue`,
        `no-tracker`, `tracker-failed`), плюс свой `canceled`: их читает чат,
        и один исход должен значить одно и то же у всех кнопок.
        """
        if not ident:
            return "not-ours"
        if self.tracker is None or not callable(getattr(self.tracker, "set_state", None)):
            log.warning("отмена %s: трекер не подключён или не умеет менять статус "
                        "— задачу переведи руками", ident)
            return "no-tracker"
        try:
            return await asyncio.to_thread(self._close_canceled, ident, by)
        except Exception as e:
            log.error("отмена %s: статус не сменился: %s", ident, e)
            return "tracker-failed"

    def _close_canceled(self, ident: str, by: str = "") -> str:
        """Синхронная часть отмены — та же дорога, что у `_close_without_merge`,
        но в отменённое состояние и без слов о сделанной работе."""
        issue = self.tracker.issue_by_identifier(ident)
        if not issue:
            log.warning("отмена %s: задачи нет в трекере — переводить нечего", ident)
            return "no-issue"
        state = dict(issue.get("state") or {})
        if closed_state(state, self.done_state):
            log.info("отмена %s: задача уже в «%s» — ничего не делаю",
                     ident, state.get("name") or "?")
            return "already-closed"
        moved = self.tracker.set_state(str(issue.get("id") or ""),
                                       self.cancel_state) or {}
        was = str(moved.get("from") or state.get("name") or "?")
        log.info("отмена %s: задача переведена в «%s» из «%s»%s", ident,
                 self.cancel_state, was, f" ({by})" if by else "")
        who = f" — отменил {by}" if by else ""
        try:
            self.tracker.comment(
                str(issue.get("id") or ""),
                f"🤖 **Отменено человеком.** Кнопкой «Отменить» под сообщением задачи "
                f"в чате{who}. Задача переведена в `{self.cancel_state}` из `{was}`. "
                f"Открытый PR, если он есть, НЕ тронут — закрыть его отдельное решение.")
        except Exception as e:
            log.warning("отмена %s: статус сменён, а комментарий не оставлен: %s",
                        ident, e)
        return "canceled"

    async def accept_done(self, ident: str, by: str = "") -> str:
        """Задача закрыта человеком БЕЗ мержа — кнопкой «Закрыть как
        сделанную» на странице подписи (`signing.SigningPage`, DEV-120).

        Бывает у задач-решений и справок: работа сделана, а кода в ней нет —
        ни ветки, ни PR, и мержить нечего. До этого такая задача оставалась в
        стопке «ждёт мержа» навсегда: единственная кнопка на карточке звала
        хостинг, который заведомо ничего не находил.

        Исходы — те же слова, что у `accept_merged`: их читает страница.
        """
        if not ident:
            return "not-ours"
        if self.tracker is None or not callable(getattr(self.tracker, "set_state", None)):
            log.warning("закрытие без мержа %s: трекер не подключён или не умеет менять "
                        "статус — задачу переведи руками", ident)
            return "no-tracker"
        try:
            return await asyncio.to_thread(self._close_without_merge, ident, by)
        except Exception as e:
            log.error("закрытие без мержа %s: статус не сменился: %s", ident, e)
            return "tracker-failed"

    def _close_without_merge(self, ident: str, by: str = "") -> str:
        """Синхронная часть закрытия без мержа — та же дорога, что у
        `_close_by_merge`, но без фактов о PR: их нет."""
        issue = self.tracker.issue_by_identifier(ident)
        if not issue:
            log.warning("закрытие без мержа %s: задачи нет в трекере — переводить нечего",
                        ident)
            return "no-issue"
        state = dict(issue.get("state") or {})
        if closed_state(state, self.done_state):
            log.info("закрытие без мержа %s: задача уже в «%s» — ничего не делаю",
                     ident, state.get("name") or "?")
            return "already-closed"
        moved = self.tracker.set_state(str(issue.get("id") or ""), self.done_state) or {}
        was = str(moved.get("from") or state.get("name") or "?")
        log.info("закрытие без мержа %s: открытого PR нет%s → задача переведена в «%s» "
                 "из «%s»", ident, f" ({by})" if by else "", self.done_state, was)
        who = f" — закрыл {by}" if by else ""
        try:
            self.tracker.comment(
                str(issue.get("id") or ""),
                f"🤖 **Закрыто без мержа.** Кнопкой «Закрыть как сделанную» на странице "
                f"подписи{who}: открытого PR по задаче нет, мержить нечего. Задача "
                f"переведена в `{self.done_state}` из `{was}`.")
        except Exception as e:
            log.warning("закрытие без мержа %s: статус сменён, а комментарий не "
                        "оставлен: %s", ident, e)
        return "done"

    def _close_by_merge(self, ident: str, where: str, facts: dict) -> str:
        """Синхронная часть: три похода в трекер из потока."""
        issue = self.tracker.issue_by_identifier(ident)
        if not issue:
            log.warning("мерж %s (%s): задачи нет в трекере — переводить нечего",
                        ident, where)
            return "no-issue"
        state = dict(issue.get("state") or {})
        if closed_state(state, self.done_state):
            log.info("мерж %s (%s): задача уже в «%s» — ничего не делаю",
                     ident, where, state.get("name") or "?")
            return "already-closed"
        moved = self.tracker.set_state(str(issue.get("id") or ""), self.done_state) or {}
        was = str(moved.get("from") or state.get("name") or "?")
        log.info("мерж %s: PR %s (%s → %s) смержен%s → задача переведена в «%s» "
                 "из «%s» по мержу", ident, where, facts.get("source_ref") or "?",
                 facts.get("ref") or "?",
                 f" ({facts['merged_by']})" if facts.get("merged_by") else "",
                 self.done_state, was)
        link = (f"[PR #{facts.get('mr_iid') or '?'}]({facts['url']})" if facts.get("url")
                else f"PR #{facts.get('mr_iid') or '?'}")
        who = f" — смержил {facts['merged_by']}" if facts.get("merged_by") else ""
        try:
            self.tracker.comment(
                str(issue.get("id") or ""),
                f"🤖 **Закрыто по мержу.** {link} (`{facts.get('source_ref') or '?'}` → "
                f"`{facts.get('ref') or '?'}`){who}. Мерж в основную ветку и есть "
                f"принятие работы человеком: задача переведена в `{self.done_state}` "
                f"из `{was}` приёмом. Вердикт проверки, если ещё придёт, статус не "
                f"меняет — мерж старше вердикта.")
        except Exception as e:
            log.warning("мерж %s (%s): статус сменён, а комментарий не оставлен: %s",
                        ident, where, e)
        return "done"

    # ── выключатели ──────────────────────────────────────────────────────

    def _nothing_on(self, what: str, ident: str) -> bool:
        """Обе точки выключены — ставить некуда, и звать ворота незачем:
        ни трекер, ни git, ни метки в сторе. Строка в журнале обязательна:
        фабрика без проверки не должна быть тихой."""
        if any(self.points.values()):
            return False
        log.info("%s %s: проверка не ставится по настройке — выключены обе "
                 "точки (%s=0, %s=0), фабрика работает без проверки",
                 what, ident, VERIFY_BEFORE_MERGE_VAR, VERIFY_AFTER_MERGE_VAR)
        return True

    def _switched_off(self, what: str, ident: str, gate: Ok) -> bool:
        """Точка, к которой ворота отнесли эту сессию, выключена. Точка — по
        режиму, а не по входу (см. `point_of`). Причина — в журнал."""
        point = point_of(gate.mode)
        if self.points.get(point, True):
            return False
        log.info("%s %s: проверка не ставится по настройке — точка «%s» "
                 "выключена (%s=0), режим %s, коммит %s",
                 what, ident, point, switch_of(point), gate.mode,
                 gate.commit_sha[:8] or "?")
        return True

    # ── ворота ───────────────────────────────────────────────────────────

    async def preflight(self, task: dict, entry: str, claimed: str = "") -> Ok | Skip:
        """Собрать факты и решить. Сеть и стор — здесь, решение — в `decide`.

        Каждый сотрудник может промолчать (трекер лёг, git не отвечает), и
        молчание — не отказ: факт помечается неизвестным, сессия ставится, а
        что не сверено — в записи запуска. Отсечь настоящую проверку из-за
        упавшей сети хуже, чем поднять лишнюю.
        """
        ident = str(task.get("identifier") or "")
        ref = str(task.get("ref") or "")
        notes: list[str] = []
        exists, issue = None, {}
        if self.lookup:
            try:
                got = self.lookup(ident)
                if inspect.isawaitable(got):
                    got = await got
                exists, issue = bool(got), dict(got or {})
            except Exception as e:
                notes.append(f"трекер не ответил: {e}")
                log.warning("ворота %s: трекер не ответил (%s) — существование "
                            "задачи не сверено", ident, e)
        else:
            notes.append("трекер не подключён — существование задачи не сверено")
        located = UNKNOWN
        if exists is not False:
            if self.repos:
                try:
                    located = await asyncio.to_thread(
                        self.repos.locate, ident, ref,
                        str(task.get("commit_sha") or ""),
                        str(task.get("project_path") or ""))
                except Exception as e:
                    notes.append(f"git не ответил: {e}")
                    log.warning("ворота %s: git не ответил (%s) — ветка и коммит "
                                "не сверены", ident, e)
            else:
                notes.append("git не подключён — ветка и коммит не сверены")
        seen, claim = None, ""
        sha = located.sha if isinstance(located, Located) else str(task.get("commit_sha") or "")
        if exists is not False and located is not None and sha:
            key = self.verified_key(ident, sha)
            if key != claimed:
                try:
                    if await self.store.claim(key, VERIFIED_TTL_SEC):
                        claim = key
                    else:
                        seen = str(await self.store.get(self.verdict_key(ident, sha)) or "")
                except Exception as e:
                    notes.append(f"стор не ответил: {e}")
                    log.warning("ворота %s: стор не ответил (%s) — повтор коммита "
                                "не сверен", ident, e)
        # Сливается ли ветка (DEV-109) — только у невлитой работы: у влитой
        # мержить уже нечего, а «не знаем» не стоит похода в хостинг. Факт
        # едет в задачу и дальше в раннер: по нему маршрут вердикта решает,
        # пускать ли работу в стопку «ждёт мержа».
        mergeable_now = None
        if isinstance(located, Located) and located.ancestor is False:
            # Одна попытка: тут не яма после мержа (её разбирает пересмотр
            # стопки), а ворота внутри обработки события — висеть в них
            # с переспросами нельзя.
            mergeable_now, _ = await mergeable.ask(self.hosting, ident, tries=1)
            if mergeable_now is None:
                # «Нет хостинга» и «хостинг промолчал» — разные факты, и в
                # записи запуска они должны читаться по-разному: первое
                # чинится настройкой инсталляции, второе — сетью.
                notes.append("сливаемость ветки не сверена — хостинг "
                             + ("не подключён" if self.hosting is None
                                else "не ответил или ещё считает"))
            elif mergeable_now is False:
                log.warning("ворота %s: ветка %s не вливается в основную — работа "
                            "проверяется, но в стопку «ждёт мержа» не поедет",
                            ident, located.ref)
        criteria = criteria_source(issue.get("description") or "") if exists else CRITERIA_UNKNOWN
        rounds, last, cleared = 0, {}, {}
        if self.work_root:
            try:
                rounds = await asyncio.to_thread(runs.work_rounds, self.work_root, ident)
                last = await asyncio.to_thread(runs.last_verify, self.work_root, ident)
                # Сигнал «препятствие снято» (DEV-105). Читается ВСЕГДА, а не
                # только при блокировке: решает `decide`, здесь только факты.
                cleared = await asyncio.to_thread(runs.clearance, self.work_root, ident)
            except Exception as e:
                notes.append(f"круги не сосчитаны: {e}")
        return decide(entry=entry, hint=str(task.get("trigger") or ""), ident=ident,
                      ref=ref, exists=exists, located=located, seen=seen,
                      criteria=criteria, rounds=rounds, issue=issue,
                      notes=tuple(notes), claim=claim,
                      sha=str(task.get("commit_sha") or ""),
                      prior_class=str(last.get("класс") or ""),
                      prior_mode=str(last.get("режим") or ""),
                      prior_line=str(last.get("вердикт") or ""),
                      prior_at=float(last.get("когда") or 0.0),
                      cleared_at=float(cleared.get("когда") or 0.0),
                      cleared_by=str(cleared.get("кто") or ""),
                      cleared_why=str(cleared.get("почему") or ""),
                      mergeable=mergeable_now)

    @staticmethod
    def _apply(task: dict, gate: Ok) -> dict:
        """Решение ворот — в задачу: режим, точная ветка и коммит, источник
        критерия, круги, что не сверено. Адрес и заголовок — из трекера, если
        в событии их не было."""
        out = {**task, "trigger": gate.mode, "criteria_source": gate.criteria,
               "rounds": gate.rounds}
        if gate.prior:
            out["prior_verdict"] = gate.prior
        if gate.ref:
            out["ref"] = gate.ref
        if gate.commit_sha:
            out["commit_sha"] = gate.commit_sha
        if gate.notes:
            out["preflight_notes"] = list(gate.notes)
        if gate.mergeable is True or gate.mergeable is False:
            # Только знание, не «не знаем»: поле, которое есть всегда и
            # значит то «нет ответа», то «конфликт», прочитается однажды не
            # так — у раннера `None` и отсутствие поля означают одно.
            out["mergeable"] = bool(gate.mergeable)
        for src, dst in (("id", "issue_id"), ("title", "title"), ("url", "url")):
            if not out.get(dst) and gate.issue.get(src):
                out[dst] = str(gate.issue[src])
        return out

    async def _record_skip(self, task: dict, skip: Skip) -> None:
        """След отсева: строка в журнале, verdict.txt в рабочем каталоге и
        отметка в сообщении задачи. Сессии не было, но решение — было, и оно
        должно быть видно там же, где видны вердикты сессий."""
        ident = str(task.get("identifier") or "?")
        log.info("проверка %s не поставлена: %s", ident, skip.reason)
        if self.work_root:
            try:
                await asyncio.to_thread(write_skip_record, self.work_root, task, skip)
            except Exception as e:
                log.warning("запись отсева %s не сделана: %s", ident, e)
        line = f"проверка не поднята: {skip.reason}"
        try:
            if task.get("issue_id"):
                await self.on_verdict({"issue_id": task["issue_id"], "identifier": ident,
                                       "icon": SKIP_ICON, "verdict": line})
            elif self.transport and self.notify_chat:
                await self.transport.send(self.notify_chat, f"{SKIP_ICON} {ident}: {line}")
        except Exception as e:
            log.warning("отсев %s не показан в чате: %s", ident, e)

    async def _release(self, key: str, ident: str) -> None:
        try:
            await self.store.drop(key)
        except Exception:
            log.warning("выкат %s: метка проверки не снята — следующий пайплайн "
                        "на этом коммите будет отброшен как дубль", ident)

    # ── вердикт ──────────────────────────────────────────────────────────

    @staticmethod
    def mark_of(result: dict) -> str:
        return f"{result.get('icon') or '✅'} {result.get('verdict') or ''}".strip()

    async def on_verdict(self, result: dict) -> str:
        """Исход проверки → строка в сообщении задачи, а не новое сообщение.

        Раньше каждая проверка постила отдельное сообщение, и на одну задачу
        в чате копилось три: назначение, сдача, проверка.
        """
        issue_id = str(result.get("issue_id") or "")
        ident = str(result.get("identifier") or issue_id or "?")
        if not issue_id:
            # Раннер кладёт в мост только вердикты с issue_id; без него он
            # сам шлёт сообщение. Сюда такое попасть не должно.
            log.warning("вердикт без issue_id (%s) — править нечего", ident)
            return "no-issue"
        mark = self.mark_of(result)
        # Вердикт запоминаем по коммиту: следующий раз ворота назовут его
        # вместо голого «уже проверяли». Sha приносит раннер из задачи;
        # прежние версии раннера его не шлют — тогда просто без памяти.
        sha = str(result.get("commit_sha") or "")
        if sha:
            try:
                await self.store.put(self.verdict_key(ident, sha),
                                     str(result.get("line") or mark)[:300],
                                     VERIFIED_TTL_SEC)
            except Exception as e:
                log.warning("вердикт %s по %s не запомнен: %s", ident, sha[:8], e)
            # Ответ по коммиту есть — общий захват постановки больше ничего не
            # держит: повтор ПОСЛЕ вердикта разрешён нарочно (решение владельца
            # 15.09.2026), и ждать два часа до истечения срока он не должен.
            await self._release(self.pending_key(ident, sha), ident)
        edited = 0
        if self.feed:
            edited = await self.feed.repaint(issue_id, mark=mark)
        log.info("вердикт %s → правок сообщений: %d", ident, edited)
        if edited:
            return "shown"
        if result.get("icon") == LOUD_ICON:
            # Тревожный вердикт раннер уже отправил человеку лично, целиком
            # и со ссылкой. Второе сообщение о том же — шум, который прячет
            # первое.
            return "shouted-by-runner"
        # Сообщения задачи в чате нет — править нечего. Молчать нельзя:
        # потерянный вердикт — это размен шума на тишину, ровно то, ради чего
        # проверка и заведена. Коротко и в общий чат.
        if self.transport and self.notify_chat:
            await self.transport.send(
                self.notify_chat, f"{mark} — {ident} (сообщения задачи в чате нет)")
            return "told"
        log.warning("вердикт %s: «%s» — сообщения задачи нет и общий чат не "
                    "задан, показать негде", ident, mark)
        return "lost"

    async def run(self) -> None:
        log.info("разбор вердиктов запущен: ключ %s", VERIFY_RESULTS_KEY)
        while True:
            try:
                raw = await self.store.pop_blocking(VERIFY_RESULTS_KEY, 30)
                if not raw:
                    continue
                await self.on_verdict(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception:
                # Цикл обязан пережить и лежачий redis, и битую запись: иначе
                # первая же помеха молча выключает отметки о проверке навсегда.
                log.exception("вердикт не разобрался")
                await asyncio.sleep(5)
