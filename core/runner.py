#!/usr/bin/env python3
"""agent-runner — автономные Claude Code сессии по задачам из Linear.

Задача, назначенная в Linear на пользователя-агента, попадает в очередь Redis
(её кладёт events-api, см. `_enqueue_agent_task` в events-api/app.py). Этот
демон разбирает очередь: на каждую задачу поднимает отдельную headless-сессию
`claude -p`, которая сама читает issue, делает работу, оформляет MR и пишет
результат комментарием в Linear.

Параллелизм ограничен числом слотов (AGENT_MAX_SLOTS): слот = поток, который
блокирующе ждёт задачу на очереди. Занято всё — задача просто лежит в очереди
до освобождения, ничего не теряется и не запускается сверх лимита. Лимит
маленький осознанно: хост 2 vCPU / 7 GB, сессии соседствуют с прод-стеком.

Раннер намеренно тонкий. Всё содержательное — работа по задаче, решение «эту
задачу делать не надо», статусы в Linear — делает сама сессия по контракту из
prompt.py. Раннер отвечает только за то, чтобы задача не пропала молча: если
сессия упала или не уложилась в таймаут, он пишет об этом в Linear и возвращает
задачу автору.
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import redis

import gatelog
import glossary
import handoff
import headline
import memory
import plan
import timeline
import transparent
from prompt import (DEFAULT_PROGRESS_STATE, DEFAULT_REVIEW_STATE, DEFAULT_VERIFYING_STATE,
                    DEFAULT_UNVERIFIED_STATE, DEFAULT_DONE_STATE,
                    DEFAULT_UNVERIFIABLE_LABEL, DEFAULT_HANDOFF_ATTEMPTS,
                    build_incident_prompt, build_intake_prompt, build_merge_prompt,
                    build_prompt, build_repair_prompt, build_verify_prompt)
from tracker import LinearAdapter

import ciwait
import codetrace
import personas

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380")
QUEUE_KEY = os.environ.get("AGENT_QUEUE_KEY", "agent:queue")
MAX_SLOTS = int(os.environ.get("AGENT_MAX_SLOTS", "2"))
# 60, а не 45 минут: к работе добавился шаг самопроверки (код-ревью по диффу
# плюс ожидание пайплайна), а измеренные сессии занимали 1–20 минут. Обрыв по
# таймауту стоит дорого именно на хвосте: он приходит уже после пуша, но до
# перевода задачи, и `release_worktrees` не зовётся — ветка остаётся занятой,
# и следующий заход уезжает в `-v2` со вторым MR на ту же задачу.
SESSION_TIMEOUT_SEC = int(os.environ.get("AGENT_SESSION_TIMEOUT_SEC", str(60 * 60)))
# 300, а не 200 — по той же причине, что и таймаут выше: `/code-review` идёт
# внутри этой же сессии и разворачивает под-агентов через Task/Agent, а это
# расходует её бюджет ходов — как и разбор находок с правками после. Урезанный
# по ходам обрыв — тот же отказ, что и по времени (ветка занята, следующий
# заход уезжает в `-v2` со вторым MR).
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "300"))
# Прерыватель (DEV-91): столько ПОДРЯД одинаковых вызовов Bash — сессия
# снимается, не доезжая до потолка ходов. Потолок ходов — синтаксический
# рубильник: петля из одной и той же команды доезжает до него за $8–17, и по
# дороге ничего не делает. 0 — прерыватель выключен.
#
# Почему 8, а не 5 из постановки: по 346 записанным сессиям раннера обеих
# инсталляций (15 278 вызовов Bash, 14–15.09.2026) самый длинный законный
# повтор — 5: TASK-234 пять раз подряд спросила `glab ci status`, дождалась
# пайплайна и сдала работу. Порог 5 снял бы её за пять вызовов до сдачи.
# Настоящая петля сама не останавливается и доезжает до 300 ходов, так что
# три лишних вызова до останова стоят меньше доллара, а запас над законным
# опросом CI — три вызова, а не ноль.
REPEAT_BASH_LIMIT = int(os.environ.get("AGENT_REPEAT_BASH_LIMIT", "8") or 0)
# Как часто во время сессии смотреть на прерыватель и на команду остановки.
STOP_POLL_SEC = float(os.environ.get("AGENT_STOP_POLL_SEC", "5") or 5)
# Ключ команды остановки: `<prefix>:<идентификатор задачи>` в ТОМ ЖЕ локальном
# redis, что и очередь. Пишет приём по `/stop` (agent_factory/commands.py),
# читает раннер раз в STOP_POLL_SEC. Одно имя переменной у обоих.
STOP_KEY_PREFIX = os.environ.get("AGENT_STOP_KEY_PREFIX", "").strip() or "agent:stop"
# Потолки под-агентов — переменные самого CLI (документированы, работают с
# v2.1.217; на client-b 2.1.259, на client-a 2.1.234). Через под-агентов уходит
# ~10% денег мимо учёта (DEV-68: $8,95 за один ход на двух под-агентах), и без
# потолков глубина 3 и 20 одновременных — умолчания CLI. Раннер ставит своё
# умолчание, окружение инсталляции его переопределяет (setdefault, не set).
SUBAGENT_ENV_DEFAULTS = {
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",   # под-агент своих не заводит
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "2",  # /code-review в два потока, не в двадцать
}
# Сессия-проверяльщик (kind=verify) кода не пишет: читает задачу, дёргает
# несколько проверок, пишет вердикт. Ей нужен свой, куда меньший бюджет —
# иначе проверка занимает слот наравне с разработкой, а слотов всего два и
# лимит памяти общий. Долгих мест два: ожидание перезапущенной деплой-джобы и
# сама сверка данных, если выкатилось не в events-api (у core это RU-хосты и
# MySQL). 30 минут, а не 20: по TASK-176 проверки core в 20 не уложились.
#
# Потолок при этом — страховка, а не главный ограничитель. Главный — сам срок,
# который сессия ЗНАЕТ: он уезжает в её промпт (`build_verify_prompt`), и её
# контракт велит за пять минут до него закончить вердиктом `PARTIAL`. Снятая
# по таймауту сессия не оставляет ничего; успевшая сказать «на поведение
# времени не хватило» оставляет честный ответ. Поэтому поднимать потолок
# дальше или разводить его по репозиториям не понадобилось.
VERIFY_TIMEOUT_SEC = int(os.environ.get("AGENT_VERIFY_TIMEOUT_SEC", str(30 * 60)))
VERIFY_MAX_TURNS = int(os.environ.get("AGENT_VERIFY_MAX_TURNS", "80"))
# Две точки проверки — два выключателя (DEV-97): до мержа (ветка на dev-стенде)
# и после мержа (выкат на прод). Ставит проверку приём (`agent_factory.deploy`),
# раннер лишь исполняет очередь — но файл окружения у них один, и на старте
# раннер говорит вслух, какая раскладка задана: иначе настройку не видно,
# пока не пропадут вердикты. Имена и разбор значения — те же, что у приёма
# (закреплено в test_verify_bridge.py); импортировать его раннер не может.
VERIFY_BEFORE_MERGE_VAR = "AGENT_VERIFY_BEFORE_MERGE"
VERIFY_AFTER_MERGE_VAR = "AGENT_VERIFY_AFTER_MERGE"
SWITCH_OFF = ("0", "false", "no", "off")
# Сессия-приёмщик (kind=intake) собирает постановку из пересланного в личку
# бота. Кода не пишет, репозитории не трогает — читает вложения, ищет в
# Linear похожие открытые задачи и кладёт draft.json, по которому events-api
# заводит issue. Бюджет ещё меньше, чем у проверки: человек ждёт ответ в
# мессенджере прямо сейчас, и десять минут молчания это уже отказ от фичи.
INTAKE_TIMEOUT_SEC = int(os.environ.get("AGENT_INTAKE_TIMEOUT_SEC", str(10 * 60)))
INTAKE_MAX_TURNS = int(os.environ.get("AGENT_INTAKE_MAX_TURNS", "60"))
# Дежурная сессия (kind=incident) не пишет код — читает логи/метрики,
# восстанавливает по известному рецепту или предлагает команду и
# останавливается (см. INCIDENT_TEMPLATE). По времени — как verify, не
# меньше (диагностика реального инцидента небыстрая); по ходам — между
# verify и work, без code-review и ожидания CI, которые и составляют хвост
# обычной рабочей сессии.
INCIDENT_TIMEOUT_SEC = int(os.environ.get("AGENT_INCIDENT_TIMEOUT_SEC", str(30 * 60)))
INCIDENT_MAX_TURNS = int(os.environ.get("AGENT_INCIDENT_MAX_TURNS", "120"))
# Сессия разрешения конфликта (kind=merge, DEV-109): работа сделана и
# проверена, а ветка перестала вливаться в основную от чужого мержа. Пишет
# код (мерж и разбор конфликтов), поэтому бюджет у неё рабочий; отличается
# контрактом (`build_merge_prompt`) и тем, что кругом по потолку
# `AGENT_MAX_WORK_ROUNDS` этот заход НЕ считается — род `merge` не входит в
# `runs.WORK_KINDS` у приёма. Слово одно на обе стороны: приём кладёт его в
# очередь (`queueing.MERGE_KIND`), импортировать друг друга они не могут, и
# совпадение закреплено контрактным тестом.
MERGE_KIND = "merge"
CLAUDE_BIN = os.environ.get("AGENT_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("AGENT_CLAUDE_MODEL", "claude-opus-5")
WORK_ROOT = Path(os.environ.get("AGENT_WORK_ROOT", "/home/ubuntu/agent-work"))
# Корень, где лежат рабочие копии репозиториев — сессия сама выбирает нужный.
REPOS_ROOT = Path(os.environ.get("AGENT_REPOS_ROOT", "/home/ubuntu/repos"))
# Список известных репозиториев и плейбук гашения симптома — специфика
# конкретного клиента, ядро (этот файл) её не хранит. Пусто → prompt.py
# подставит свою честную заглушку вместо выдуманной под кого-то команды.
AGENT_KNOWN_REPOS = os.environ.get("AGENT_KNOWN_REPOS", "")
# Канон окружений клиента (см. DEFAULT_ENV_CANON в prompt.py): без него intake
# угадывает окружение и попадает пальцем в небо. Пусто = честная заглушка
# «не угадывай», а не выдуманный за клиента контур.
AGENT_ENV_CANON = os.environ.get("AGENT_ENV_CANON", "").strip()
# Корень стора памяти: каталог, внутри которого у каждой роли свой подкаталог
# `agent-<role>`. Пусто → механизм выключен, память сессии остаётся там, где её
# создаёт claude (одноразовый scope по рабочему каталогу задачи), то есть
# прежнее поведение. Клиент-независимо: путь знает только конфиг инстанса.
AGENT_MEMORY_STORE = os.environ.get("AGENT_MEMORY_STORE", "").strip()
# Слой памяти О ЧЕЛОВЕКЕ — личный стор того, кто ставит и принимает работу.
# Лежит ВНЕ стора инсталляции сознательно: стор отдаётся клиенту вместе с
# установкой, а привычки владельца процесса переносятся за ним из проекта в
# проект (DEV-43). Пусто → слоя нет, сессия работает с двумя остальными.
AGENT_PERSON_MEMORY = os.environ.get("AGENT_PERSON_MEMORY", "").strip()
_SYMPTOM_PLAYBOOK_FILE = os.environ.get("AGENT_SYMPTOM_PLAYBOOK_FILE", "")
# Плейбук известных инцидентов (уровень 2 дежурного контракта) — отдельный от
# symptom_playbook: тот один пункт про гашение симптома в рабочей сессии, этот
# растущий список рецептов восстановления по классам инцидентов. Тот же
# принцип «пусто -> честная заглушка в prompt.py», не выдуманный рецепт.
_INCIDENT_PLAYBOOK_FILE = os.environ.get("AGENT_INCIDENT_PLAYBOOK_FILE", "")
LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY", "")
# Кто принимает работу агента: на него уходит задача, когда сессия закончила
# (с MR или с отказом). Значения ниже — ВЛАДЕЛЕЦ, он же значение по умолчанию.
# Фактического принимающего спрашивают у `current_reviewer()`: владелец может
# передать приём другому человеку из бота, на срок или бессрочно.
AGENT_OWNER_ID = os.environ.get("AGENT_REVIEWER_LINEAR_USER_ID", "")
AGENT_OWNER_NAME = os.environ.get("AGENT_REVIEWER_NAME", "")
# Личка владельца — чтобы тревожный вердикт проверки выката звал его лично, а
# не только строкой в общий чат. Пусто → шлём только в общий, как раньше.
AGENT_OWNER_TELEGRAM_ID = os.environ.get("AGENT_REVIEWER_TELEGRAM_ID", "")
# Ключ в ТОМ ЖЕ локальном redis, что и очередь: пишет его events-api по
# команде из лички бота, читаем мы. Ни нового секрета, ни нового порта —
# ровно как мост вердиктов (VERIFY_RESULTS_KEY).
REVIEWER_KEY = os.environ.get("AGENT_REVIEWER_KEY", "agent:reviewer")
# Имена статусов трекера — такая же настройка инсталляции, как очередь или
# лейбл дежурного: Linear даёт переименовать любой статус, и у второй
# инсталляции «In Review» не оказалось вовсе — ensure_handoff не находил его
# по имени и молча оставлял сданную работу в «In Progress». Искать по типу
# состояния, как делает приём, здесь нельзя: у «в работе» и «на приёмке» в
# Linear один тип `started`, различить их можно только по имени. Умолчания —
# те, что у первого клиента; пустое значение — тоже умолчание, а не статус
# с пустым именем.
PROGRESS_STATE_NAME = (os.environ.get("AGENT_PROGRESS_STATE_NAME", "").strip()
                       or DEFAULT_PROGRESS_STATE)
REVIEW_STATE_NAME = (os.environ.get("AGENT_REVIEW_STATE_NAME", "").strip()
                     or DEFAULT_REVIEW_STATE)
# Куда уезжает не прошедшая проверку задача. Имя своё у каждой доски, и
# промахнуться мимо него нельзя молча: раннер ищет статус по имени.
UNVERIFIED_STATE_NAME = (os.environ.get("AGENT_UNVERIFIED_STATE_NAME", "").strip()
                         or DEFAULT_UNVERIFIED_STATE)
# Куда рабочая сессия передаёт готовую работу. НЕ человеку: до него задача
# доходит уже проверенной, иначе он решает про мерж вслепую.
VERIFYING_STATE_NAME = (os.environ.get("AGENT_VERIFYING_STATE_NAME", "").strip()
                        or DEFAULT_VERIFYING_STATE)
# Куда закрывается принятая работа. Раннер ищет и его: маршрут по вердикту
# проверки он теперь сверяет сам (DEV-96), а «проверять было нечего» после
# мержа кончается закрытием.
# Имя переменной — то же, что у приёма (`agent_factory.deploy.DONE_STATE_VAR`):
# мерж PR закрывает задачу приёмом в это же состояние. Закреплено
# `core/test_verify_bridge.py`.
DONE_STATE_VAR = "AGENT_DONE_STATE_NAME"
DONE_STATE_NAME = (os.environ.get(DONE_STATE_VAR, "").strip()
                   or DEFAULT_DONE_STATE)
# Пользователь-агент в трекере — та же переменная, что у приёма: назначение
# на него и есть новый рабочий круг. Провал проверки, который ещё чинится,
# раннер назначает на него сам. Пусто — назначать некого, провал уходит
# принимающему.
AGENT_USER_ID = os.environ.get("AGENT_LINEAR_USER_ID", "").strip()
# Потолок рабочих заходов — тот же, что у ворот и вебхука (`runs.MAX_WORK_ROUNDS`):
# с него провал уходит человеку, а не агенту. Ноль — потолка нет.
MAX_WORK_ROUNDS = int(os.environ.get("AGENT_MAX_WORK_ROUNDS", "3") or 0)
# Потолок расхода на ОДНУ сессию в долларах. CLI считает его вместе с
# под-агентами и завершает запуск, когда сумма достигнута, — то есть это
# единственный ограничитель, который срабатывает раньше, чем деньги потрачены.
# Ходы и срок таким не являются: сессия может упереться в них, потратив втрое
# больше соседней. Ноль или пусто — без потолка (поведение до 15.09.2026).
MAX_BUDGET_USD = float(os.environ.get("AGENT_MAX_BUDGET_USD", "0") or 0)
# Потолок неудачных сдач подряд (DEV-95): столько раз рабочей сессии
# возвращают список недостающего и продолжают её же; на потолке задача
# уходит человеку, а не крутится. Решено в постановке: три. Ноль или мусор
# в окружении — умолчание контракта, не «без потолка»: без потолка — петля.
MAX_HANDOFF_ATTEMPTS = (int(os.environ.get("AGENT_MAX_HANDOFF_ATTEMPTS", "") or 0)
                        or DEFAULT_HANDOFF_ATTEMPTS)
# Метка на задаче, которую проверять было нечего: по ней считается доля
# непроверенного, ушедшего к людям.
UNVERIFIABLE_LABEL = (os.environ.get("AGENT_UNVERIFIABLE_LABEL", "").strip()
                      or DEFAULT_UNVERIFIABLE_LABEL)
# Как назвать агента, когда проверка возвращает задачу в работу. Имя в трекере
# знает инсталляция: у одного клиента это приложение с actor=app, у другого
# обычный пользователь.
AGENT_NAME_HINT = os.environ.get("AGENT_NAME_HINT", "").strip()
LINEAR_API_URL = "https://api.linear.app/graphql"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("AGENT_NOTIFY_CHAT_ID", "")

log = logging.getLogger("agent-runner")
_stop = threading.Event()

# Путь задан, а файла нет — та же грабля, что уронила прод 05.09.2026 с
# tracker.py: конфиг сослался на файл, который не доехал деплоем, и раннер об
# этом промолчал бы. Здесь молчать нельзя — предупреждаем сразу при старте, а
# не когда сессия соберёт промпт без плейбука и никто не поймёт почему.
if _SYMPTOM_PLAYBOOK_FILE and not Path(_SYMPTOM_PLAYBOOK_FILE).is_file():
    log.warning("AGENT_SYMPTOM_PLAYBOOK_FILE=%s задан, но файл не найден — "
                "шаг 6 контракта соберётся с заглушкой вместо плейбука клиента",
                _SYMPTOM_PLAYBOOK_FILE)
AGENT_SYMPTOM_PLAYBOOK = (
    Path(_SYMPTOM_PLAYBOOK_FILE).read_text()
    if _SYMPTOM_PLAYBOOK_FILE and Path(_SYMPTOM_PLAYBOOK_FILE).is_file() else ""
)

if not AGENT_ENV_CANON:
    log.warning("AGENT_ENV_CANON не задан — приёмщик и дежурный не смогут "
                "определять окружение и обязаны будут писать «не определено». "
                "Для клиента с несколькими площадками это стоит заполнить.")
if _INCIDENT_PLAYBOOK_FILE and not Path(_INCIDENT_PLAYBOOK_FILE).is_file():
    log.warning("AGENT_INCIDENT_PLAYBOOK_FILE=%s задан, но файл не найден — "
                "дежурная сессия соберётся с заглушкой вместо плейбука клиента",
                _INCIDENT_PLAYBOOK_FILE)
AGENT_INCIDENT_PLAYBOOK = (
    Path(_INCIDENT_PLAYBOOK_FILE).read_text()
    if _INCIDENT_PLAYBOOK_FILE and Path(_INCIDENT_PLAYBOOK_FILE).is_file() else ""
)

# Единственное место, где ядро знает, что трекер — конкретно Linear. Смена
# трекера — замена этой строчки на другую реализацию TrackerAdapter, без
# правок ниже (см. tracker.py).
_tracker = LinearAdapter(LINEAR_API_KEY, LINEAR_API_URL)

# Ключи личностей: под чьим именем появляется комментарий. Пусто — фабрика
# пишет общим ключом инсталляции, то есть ведёт себя как раньше (см.
# personas.py). Назначение и смена статуса личностям не поручаются: у
# приложения трекера с `actor=app` скоупы на это может не быть, а сдача
# задачи дороже подписи под ней.
PERSONA_KEYS = personas.keys_from_env(os.environ)
_persona_trackers: dict[str, LinearAdapter] = {}


def _report_personas() -> None:
    """Сказать при старте, кто подписывается своим именем, а кто владельцем.

    Ключи личностей читаются ОДИН раз, при импорте модуля (`PERSONA_KEYS`
    выше). Значит ключ, дописанный в окружение инсталляции позже, не
    действует до следующего перезапуска юнита, и признака этого не было
    никакого: `write_mcp_config`
    молча возвращал `None`, комментарии молча уходили общим ключом, и со
    стороны задачи это выглядело так, будто всю работу сделал владелец ключа —
    ровно то состояние, ради ухода от которого личности и заводились.

    Поэтому при старте печатаем ВСЕ три стороны: своих, занявших чужой голос
    и безымянных. Пустой список личностей — тоже строка в журнале:
    инсталляция без приложений в трекере это нормально, но знать об этом надо
    по журналу, а не по авторам комментариев в задачах.

    Занявшие чужой голос — это `personas.FALLBACK_PERSONA` (сейчас там
    `qa → reviewer`): `keys_from_env` кладёт такой личности ключ соседа, и в
    карте она лежит НАСТРОЕННОЙ. Считать её своей нельзя — строка «личности
    трекера настроены: qa» сказала бы ровно неправду о том, ради чего этот
    отчёт и пишется: приложения QA у инсталляции нет, и вердикты проверки
    подпишет Reviewer. Отличаем по ключу: он у них один и тот же.
    """
    borrowed = {p: instead for p, instead in personas.FALLBACK_PERSONA.items()
                if PERSONA_KEYS.get(p)
                and PERSONA_KEYS.get(p) == PERSONA_KEYS.get(instead)}
    named = sorted(set(PERSONA_KEYS) - set(borrowed))
    nameless = [p for p in personas.PERSONAS if p not in PERSONA_KEYS]
    if named:
        log.info("личности трекера настроены: %s", ", ".join(named))
    for persona in sorted(borrowed):
        log.info("личность %s без своего ключа (%s) — её комментарии подпишет "
                 "%s, а не она сама", persona, personas.key_var(persona),
                 borrowed[persona])
    if nameless:
        log.warning("личности без ключа: %s — их комментарии уйдут общим "
                    "ключом инсталляции, то есть от имени его владельца "
                    "(ключи читаются один раз при старте юнита)",
                    ", ".join(sorted(nameless)))


def tracker_for(persona: str) -> LinearAdapter:
    """Адаптер, пишущий от имени личности. Ключа нет — общий адаптер."""
    key = PERSONA_KEYS.get(persona or "")
    if not key:
        return _tracker
    if persona not in _persona_trackers:
        _persona_trackers[persona] = LinearAdapter(key, LINEAR_API_URL)
    return _persona_trackers[persona]


# ── внешние сервисы (обёртки поверх TrackerAdapter) ─────────────────────────

def linear_comment(issue_id: str, body: str, persona: str = "") -> None:
    """Комментарий в задачу. `persona` — чьим именем он подписан.

    Пусто или личность не настроена — общий ключ инсталляции, как было до
    появления личностей.

    Здесь же — разметка машинных слов расшифровкой (DEV-108). Место выбрано
    не по удобству: это ЕДИНСТВЕННАЯ дверь, через которую раннер кладёт текст
    человеку, и правило «любой код доезжает с расшифровкой» должно держаться
    дверью, а не памятью того, кто дописывает следующий комментарий. Код при
    этом остаётся на месте — расшифровка встаёт РЯДОМ: комментарии в Linear
    читают и следующие сессии, и выкинутый код ослепил бы их.
    """
    tracker_for(persona).post_comment(issue_id, glossary.decorate(body))


def linear_assign_to_reviewer(issue_id: str, reviewer: dict | None = None) -> None:
    """Перевести задачу на человека, который её принимает прямо сейчас.

    Страховка на случай, когда сессия не довела дело до конца: задача не должна
    остаться висеть на агенте, иначе про неё все забудут — у агента нет входящих.
    `reviewer` передаётся вызывающим, если он уже спросил его для промпта: одна
    сессия должна целиком принадлежать одному человеку, даже если делегирование
    поменялось у неё под руками.
    """
    rev = reviewer or current_reviewer()
    if not rev["id"]:
        log.warning("принимающий не задан — issue %s остаётся на агенте", issue_id)
        return
    _tracker.assign(issue_id, rev["id"])


def _handoff_state(issue_id: str) -> dict:
    """Что сейчас в Linear по задаче: статус, на ком, когда последний комментарий."""
    return _tracker.fetch_issue_snapshot(issue_id)


def _send_telegram(chat: str, text: str) -> bool:
    """Одна отправка. True — доставлено. Исход нужен, чтобы notify мог
    откатиться с лички на общий чат, а не потерять сообщение молча."""
    if not (TELEGRAM_BOT_TOKEN and chat):
        return False
    payload = json.dumps({"chat_id": chat, "text": text[:4000]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("telegram notify failed (chat %s): %s", chat, e)
        return False


def notify(text: str, chat_id: str = "") -> None:
    """Короткая строка в Telegram: упавшая сессия, тревожный вердикт, осечка
    приёма. Всё это — ошибки.

    Ошибки уходят ВЛАДЕЛЬЦУ В ЛИЧКУ, а не в общий чат. Андрей 01.09.2026:
    «владелец процесса я, я отвечаю за результат, и эти сообщения нужны
    именно мне». Команде они не нужны: в общем чате остаётся состояние
    задачи, которое и так рисуется по вебхуку Linear.

    Личка не настроена или не приняла (диалог с ботом не начат, бот
    заблокирован) — падаем в общий чат, как раньше. Молчание хуже шума:
    непрочитанное падение сессии = потерянная задача.

    `chat_id` задан явно — шлём только туда, без отката: адресата выбрал
    вызывающий, и подменять его общим чатом нельзя.
    """
    if chat_id:
        _send_telegram(chat_id, text)
        return
    for chat in (AGENT_OWNER_TELEGRAM_ID, TELEGRAM_CHAT_ID):
        if chat and _send_telegram(chat, text):
            return


def notify_reviewer(text: str) -> None:
    """То же, но в личку тому, кто принимает работу сейчас.

    Нужно ровно для одного случая — тревожного вердикта проверки выката. Когда
    MR смержен автоматически, эта проверка остаётся последним рубежом, и
    строчка в общем чате для неё слишком тихая: она уезжает вверх за минуты.
    Личка не заменяет общий чат, а добавляется к нему.

    Не знаем лички (человека нет в карте, владелец не задал свою) — молчим:
    общий чат сообщение уже получил.
    """
    rev = current_reviewer()
    if rev["telegram_id"]:
        notify(text, chat_id=rev["telegram_id"])


# ── кто принимает работу ───────────────────────────────────────────────────
#
# Владелец задан переменными окружения юнита и меняется только правкой файла с
# рестартом. Этого мало: «я недоступен» — состояние на день, а не на релиз.
# Поэтому поверх окружения лежит запись в локальном redis, которую владелец
# ставит командой в личке бота (events-api, `/reviewer`). Окружение остаётся
# значением по умолчанию: пусто в redis, лежит redis, протух срок — работа
# едет владельцу. Отказ всегда в сторону владельца, потому что потерявшаяся
# задача хуже лишнего пинга.
#
# Срок проверяется НА ЧТЕНИИ, а не по таймеру: раннер и так спрашивает
# принимающего на каждой сессии, и «само вернулось к владельцу» не требует ни
# планировщика, ни живого процесса в момент истечения.

_redis_client = None
_redis_lock = threading.Lock()


def _redis_conn():
    """Один общий клиент на процесс: current_reviewer зовут по нескольку раз
    за сессию, и открывать пул на каждый вызов незачем."""
    global _redis_client
    with _redis_lock:
        if _redis_client is None:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        return _redis_client


def _owner() -> dict:
    return {"id": AGENT_OWNER_ID, "name": AGENT_OWNER_NAME,
            "telegram_id": AGENT_OWNER_TELEGRAM_ID, "until": "",
            "delegated": False}


def reviewer_from_record(raw: str, now: datetime) -> dict | None:
    """Запись из redis → принимающий, либо None (нет / битая / просрочена).

    Чистая функция, всё внешнее приходит аргументами — это контракт между
    events-api (пишет) и раннером (читает), и проверяться он должен без сети.
    Битую запись трактуем как её отсутствие: делегирование, которое нельзя
    разобрать, не повод оставить задачу ни на ком.
    """
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        log.warning("запись о принимающем не разбирается, беру владельца")
        return None
    if not isinstance(rec, dict) or not rec.get("linear_user_id"):
        return None
    until = rec.get("until") or ""
    if until:
        try:
            deadline = datetime.fromisoformat(until)
        except ValueError:
            log.warning("срок делегирования %r не разбирается, беру владельца", until)
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= now:
            return None
    return {"id": str(rec["linear_user_id"]), "name": rec.get("name") or "",
            "telegram_id": str(rec.get("telegram_id") or ""), "until": until,
            "delegated": True}


def current_reviewer() -> dict:
    """Кто принимает работу прямо сейчас: делегат или владелец."""
    try:
        raw = _redis_conn().get(REVIEWER_KEY)
    except Exception as e:
        # Лежачий redis не должен останавливать сессии: работаем на владельце.
        log.warning("не спросить делегирование в redis (%s), беру владельца", e)
        return _owner()
    rev = reviewer_from_record(raw, datetime.now(timezone.utc))
    if rev is None:
        if raw:
            # Срок вышел (или запись битая) — убираем, чтобы состояние в чате и
            # в раннере не расходились: `/reviewer` без аргументов читает тот
            # же ключ и должен показать владельца, а не просроченного делегата.
            try:
                _redis_conn().delete(REVIEWER_KEY)
            except Exception as e:
                log.warning("не убрать просроченное делегирование: %s", e)
        return _owner()
    return rev


# ── рабочие копии ──────────────────────────────────────────────────────────

# git в каталоге: None вместо исключения — уборка не должна ронять слот.
# Реализация одна, в `codetrace.git_out` (там же она и проверяется, на
# настоящих репозиториях); имя `_git` остаётся — им зовут уборку рабочих копий.
_git = codetrace.git_out


def release_worktrees(workdir: Path) -> None:
    """Снять git-worktree, оставшиеся от сессии.

    Пока дерево на месте, ветка занята: git не даёт подцепиться к ней второй
    раз (`already used by worktree`). Для переоткрытой задачи это значит, что
    доработка по ревью не может продолжиться в своей же ветке и уезжает в
    новую — со вторым MR на ту же работу. Коммиты и сама ветка удаление
    дерева переживают, уходит только рабочая копия; лог сессии лежит уровнем
    выше и не затрагивается.

    Зовётся только после успешной сессии. После падения дерево остаётся: там
    незакоммиченные изменения, по которым и разбирают, что пошло не так.
    """
    try:
        subdirs = sorted(p for p in workdir.iterdir() if p.is_dir())
    except OSError as e:
        log.warning("не смог прочитать %s: %s", workdir, e)
        return
    for sub in subdirs:
        # Рабочая копия помечена файлом .git (в обычном клоне это каталог).
        if not (sub / ".git").is_file():
            continue
        # Путь основного репозитория узнаём ДО удаления — после него каталога
        # уже нет, а prune надо звать именно в основном клоне.
        main_git_dir = _git(sub, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if _git(sub, "worktree", "remove", "--force", str(sub)) is None:
            log.warning("worktree %s не снялся — ветка останется занятой", sub)
            continue
        if main_git_dir:
            _git(Path(main_git_dir), "worktree", "prune")
        log.info("рабочая копия %s снята, ветка свободна", sub)


# ── память сессии ──────────────────────────────────────────────────────────

# Как часто наблюдатель пересчитывает вес дерева сессии. 15с — компромисс:
# короткий всплеск не пропустим, а /proc опрашивается редко.
MEM_POLL_SEC = float(os.environ.get("AGENT_MEM_POLL_SEC", "15"))
PAGE_SIZE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
# Счётчик OOM-убийств от ядра. Он на весь юнит (оба слота и раннер), но это
# единственный однозначный признак: rc сессии про OOM не говорит — killer
# снимает дочерний `claude.exe`, а процесс-обёртка возвращает обычный код.
CGROUP_MEMORY_EVENTS = Path(os.environ.get(
    "AGENT_CGROUP_MEMORY_EVENTS",
    "/sys/fs/cgroup/system.slice/agent-runner.service/memory.events"))


def _proc_tree_rss_kb(root_pid: int) -> int:
    """Суммарный RSS процесса и всех его потомков, КБ. 0 — если дерева уже нет.

    Меряем именно дерево, а не один pid: `claude` — тонкая обёртка, вся память
    живёт в дочернем `claude.exe` (30.08.2026 OOM-killer снял именно его,
    anon-rss 3.8G, при 45 МБ у родителя). Данные из /proc — без зависимостей,
    их и так нет у раннера.
    """
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # statm: поля в страницах, второе — resident. Дешевле, чем status.
            resident_pages = int((entry / "statm").read_text().split()[1])
            # ppid — четвёртое поле stat, но comm в скобках может содержать
            # пробелы, поэтому режем по последней «)».
            stat_tail = (entry / "stat").read_text().rpartition(")")[2].split()
            ppid = int(stat_tail[1])
        except (OSError, ValueError, IndexError):
            # Процесс умер между iterdir и чтением — обычное дело, пропускаем.
            continue
        pid = int(entry.name)
        rss[pid] = resident_pages * PAGE_SIZE_KB
        children.setdefault(ppid, []).append(pid)

    total, stack = 0, [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        stack.extend(children.get(pid, ()))
    return total


def _oom_kills() -> int | None:
    """Сколько раз ядро убивало процессы в cgroup раннера. None — не прочиталось.

    Считаем разницу до и после сессии: выросло — сессию (или соседнюю по слоту)
    сняли по памяти. Догадываться по коду возврата нельзя, см. CGROUP_MEMORY_EVENTS.
    """
    try:
        for line in CGROUP_MEMORY_EVENTS.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        return None
    return None


def _peak_note(peak_mb: int) -> str:
    """Как назвать пик, чтобы число не спорило с диагнозом.

    Замер идёт раз в MEM_POLL_SEC, поэтому короткий всплеск (в том числе тот
    самый, на котором сессию и сняли) может не попасть в выборку: у сессии,
    умершей за секунды, пик честно равен нулю. Молча печатать «пик 0 МБ»
    рядом с «снята по нехватке памяти» — вводить в заблуждение.
    """
    if peak_mb <= 0:
        return f"памяти замерить не успели (замер раз в {int(MEM_POLL_SEC)}с)"
    return f"пик памяти {peak_mb} МБ (максимум замеров раз в {int(MEM_POLL_SEC)}с)"


def _write_stats(workdir, *, slot: int, ident: str, kind: str, took: int,
                 peak_mb: int, rc: int, outcome: str = "",
                 criterion: str = "", gate_criterion: str = "",
                 gates: list | None = None, discrepancy: str | None = None,
                 mode: str = "", halt: str = "", gate_plan: dict | None = None) -> None:
    """Ресурсы и исход сессии — файлом рядом с логом, а не только строкой в
    журнале.

    Журнал читает человек с сервера, а очередь подписи открывают с телефона:
    ей нужен тот же факт машиночитаемым. Пишем после завершения — во время
    работы числа ещё не окончательные, а два источника правды про один пик
    разойдутся в первый же день. По той же причине файл пишется РОВНО один
    раз: исход считается до записи, а не дописывается следующей веткой.

    `когда` для сданной работы — это и есть время сдачи: сессия дошла до
    конца и отчиталась в задачу прямо перед этим.

    `критерий` — откуда проверка взяла эталон (`задача` / `запасной` / `нет`),
    из первой строки её `expected.md`; пусто — файла нет или строка не
    разобрана. Без этого поля разметку вердиктов нельзя резать по классам:
    самописный критерий проверен слабее, и это должно быть видно в учёте, а
    не угадываться по тексту комментария.

    `критерий по воротам` — тот же вопрос, но отвеченный КОДОМ до сессии
    (`deploy.preflight`, DEV-86) по телу задачи в трекере, теми же словами.
    Два ответа рядом: разошлись — сессия либо не нашла критерий, который
    есть, либо назвала «задачей» то, чего в задаче нет. Пусто — ворот не
    было (задача положена в очередь до них или другим путём).

    `ворота сессии` — детерминированные свидетельства, по которым считался
    исход (DEV-88): суд (`прошло` / `не прошло` / `ошибка обвязки` / `нет
    свидетельств`), причина словами и сами записи. Только у проверки: у
    работы, приёма и дежурного ворот нет, и поле не пишется — иначе «нет
    свидетельств» у рабочей сессии читалось бы как замечание к ней. Не
    путать с `ворота` у отсева (`deploy.write_skip_record`): там строка с
    причиной, здесь — разбор.

    `класс` — класс расхождения вердикта (`verdict_class`, DEV-89), `режим` —
    в какой точке проверяли (`trigger` задачи: `branch` до мержа, остальное
    после). Тоже только у проверки. По ним ворота постановки
    (`deploy.decide`) решают, ставить ли следующую: за `unobservable` в той
    же точке следующая не поднимается — наблюдать по-прежнему негде.

    `снята` — причина, по которой сессию сняли ДО конца прерыватель или
    команда `/stop` (DEV-91), словами. Только у снятых: у дошедшей до конца и
    у упавшей самой поля нет, и «нет поля» читается как «сама».

    `шлюз` — что ответил шлюз тяжёлой задачи (DEV-56): вердикт, названные
    признаки и счёт последствий из `plan.md`. Только у рабочей сессии.
    Пустой словарь не пишем: «шлюза не было» (раннер прежней версии, сессия
    без файла) и «шлюз ответил "лёгкая"» — разные факты, и слив их, журнал
    объявил бы пройденным то, чего не случилось.
    """
    try:
        (workdir / "stats.json").write_text(json.dumps({
            "задача": ident, "род": kind, "слот": slot, "секунд": took,
            "пик_мб": peak_mb, "код": rc, "когда": time.time(),
            "исход": outcome, "критерий": criterion,
            "критерий по воротам": gate_criterion,
            **({"ворота сессии": gatelog.summary(gates)} if gates is not None else {}),
            **({"класс": discrepancy, "режим": mode} if discrepancy is not None else {}),
            **({"снята": halt} if halt else {}),
            **({"шлюз": gate_plan} if gate_plan else {}),
        }, ensure_ascii=False))
        if not (workdir / "stats.json").is_file():
            # Запись есть или её нет — третьего не бывает, и «нет» обязано
            # прозвучать здесь же. 15.09.2026 (DEV-110) исход прогона DEV-106
            # не сохранился вовсе, и раннер об этом промолчал: человеку
            # пришлось читать verdict.txt руками, а очередь подписи осталась
            # без машиночитаемого исхода.
            raise OSError("файла нет после записи")
    except Exception as e:                      # каталог снят, диск полон
        log.error("[slot %d] %s → исход прогона не записан: stats.json в %s "
                  "— %s. Раннер не сможет сказать, чем кончился этот заход",
                  slot, ident, workdir, e)


def _kill_session(proc: subprocess.Popen, pgid: int | None = None) -> None:
    """Снять сессию вместе со всем, что она породила.

    `proc.kill()` бьёт только по процессу-обёртке, а память и работа — в
    дочернем `claude.exe` и в под-агентах, которых он разворачивает на
    `/code-review`. Осиротевший потомок остаётся в cgroup юнита и продолжает
    есть общий лимит, пока его никто не снимет: с OOMPolicy=continue раньше
    это делал systemd, снимая юнит целиком. Сессия запущена своей группой
    (start_new_session), поэтому бьём по группе.

    Группу передают снаружи, потому что звать нас приходится и после
    `proc.wait()`: pid к тому моменту уже пожат, и `getpgid` по нему не
    ответит. Запоминать её надо, пока обёртка жива.
    """
    # Прозрачная сессия (DEV-126) живёт в общей группе с демоном фоновых
    # сессий и с чужими сессиями: бить по группе значит снести чужую работу.
    # Её тушат своей командой — `kill` у обёртки зовёт `claude stop`.
    if isinstance(proc, transparent.Attached):
        proc.kill()
        return
    try:
        os.killpg(pgid if pgid is not None else os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Группы уже нет или она не наша — хотя бы обёртку.
        proc.kill()
    proc.wait()


# ── снятие до конца: прерыватель и команда остановки (DEV-91) ──────────────
#
# Потолок ходов слеп к тому, идёт ли ещё работа: петля из одной и той же
# команды доезжает до него целиком. Две вещи снимают сессию раньше, обе тем
# же `_kill_session`, что и таймаут, — второго механизма снятия здесь нет:
#   • прерыватель — REPEAT_BASH_LIMIT одинаковых вызовов Bash ПОДРЯД в одной
#     ветке (главная сессия и каждый под-агент считаются отдельно);
#   • команда `/stop <задача> <причина>` в личке бота — ключ в локальном redis,
#     раннер читает его между ломтями ожидания.

HALT_LOOP = "loop"
HALT_HUMAN = "human"


@dataclass(frozen=True)
class Halt:
    """Почему сессию сняли до конца. `kind` — HALT_LOOP или HALT_HUMAN,
    `reason` — словами, для журнала и комментария в задаче, `by` — кто
    остановил (у прерывателя пусто)."""
    kind: str
    reason: str
    by: str = ""


class RepeatBreaker:
    """Считает одинаковые вызовы Bash подряд по ветке транскрипта.

    «Подряд» — строго: любой другой вызов инструмента между ними (Read, Edit,
    другая команда) обнуляет счёт. Цикл «правка → тесты → правка → тесты»
    прерывателем не ловится намеренно: те же тесты пять раз подряд с правками
    между — это работа, а не петля. По записанным трассам оба определения
    («подряд среди всех вызовов» и «подряд среди Bash») дают одинаковый
    максимум законного повтора (5, опрос CI), так что строгое ничего не
    теряет и не рискует циклом правок.

    Ветка — `parent_tool_use_id` события: под-агент повторяет своё, главная
    сессия — своё, и повтор одного не складывается с повтором другого.

    Кормит его `_pump_stream` из своего потока; читает — цикл ожидания
    сессии. Одно присвоение `halt` на всю жизнь объекта, гонки нет.
    """

    def __init__(self, limit: int):
        self.limit = int(limit or 0)
        self.halt: Halt | None = None
        self._runs: dict[str | None, tuple[str, int]] = {}

    @staticmethod
    def _norm(command) -> str:
        return " ".join(str(command or "").split())

    def feed(self, lineage: str | None, name: str, inp: dict | None) -> None:
        if self.limit <= 0 or self.halt is not None:
            return
        if name != "Bash":
            self._runs.pop(lineage, None)
            return
        cmd = self._norm((inp or {}).get("command"))
        prev, n = self._runs.get(lineage, ("", 0))
        n = n + 1 if cmd == prev else 1
        self._runs[lineage] = (cmd, n)
        if n >= self.limit:
            brief = cmd[:160] + ("…" if len(cmd) > 160 else "")
            where = "под-агент" if lineage else "сессия"
            self.halt = Halt(HALT_LOOP, f"{n} раз подряд один и тот же вызов "
                                        f"Bash ({where}): `{brief}`")


def stop_key(ident: str) -> str:
    """Ключ остановки задачи. Формат — контракт с `commands.stop_key`."""
    return f"{STOP_KEY_PREFIX}:{ident}"


def halt_from_record(raw) -> Halt:
    """Запись `/stop` из redis → Halt. Битая запись — всё равно остановка:
    человек нажал, и «не разобрал причину» не повод ехать дальше."""
    try:
        rec = json.loads(raw) if raw else {}
        if not isinstance(rec, dict):
            rec = {}
    except (ValueError, TypeError):
        rec = {}
    reason = str(rec.get("reason") or "").strip() or "причина не названа"
    return Halt(HALT_HUMAN, reason, str(rec.get("by") or ""))


_stop_unreachable = set()


def _stop_requested(ident: str) -> Halt | None:
    """Есть ли команда остановки по задаче. Найденную снимаем сразу: следующая
    сессия по той же задаче не должна умереть от прошлого `/stop`.

    Лежачий redis — не остановка и не падение: сессия идёт, а в журнале один
    раз на задачу сказано, что команду остановки сейчас не услышать.
    """
    key = stop_key(ident)
    try:
        conn = _redis_conn()
        raw = conn.get(key)
        if not raw:
            return None
        conn.delete(key)
    except Exception as e:                       # redis лежит, URL не тот
        if ident not in _stop_unreachable:
            _stop_unreachable.add(ident)
            log.warning("%s: команду остановки не спросить в redis (%s) — "
                        "/stop по этой сессии не сработает", ident, e)
        return None
    return halt_from_record(raw)


def _clear_stale_stop(ident: str) -> None:
    """`/stop`, поданный ДО старта сессии, к ней не относится: останавливать
    было нечего, и человек это уже прочитал в ответе бота. Оставить ключ —
    значит снять новую сессию на первом же ломте, за старую."""
    try:
        if _redis_conn().delete(stop_key(ident)):
            log.warning("%s: в redis лежал /stop, поданный до старта, — снят, "
                        "к этой сессии не применяется", ident)
    except Exception:
        pass                                     # скажет _stop_requested


def _wait_session(proc: subprocess.Popen, pgid: int | None, ident: str,
                  timeout_sec: float, breaker: RepeatBreaker) -> tuple[int | None, Halt | None]:
    """Ждать сессию ломтями по STOP_POLL_SEC, между ломтями — прерыватель и
    команда остановки. Возвращает (код, почему сняли): код None — сняли мы,
    и `Halt` говорит кто; None и None — таймаут, как прежде.

    Срок считается ломтями, а не часами: `wait(timeout=…)` блокирует ровно
    на ломоть, и сумма ломтей — это и есть прошедшее время. Снятие — одно и
    то же `_kill_session` на любой причине: второго механизма нет.
    """
    left = float(timeout_sec)
    while True:
        try:
            return proc.wait(timeout=max(0.0, min(STOP_POLL_SEC, left))), None
        except subprocess.TimeoutExpired:
            left -= STOP_POLL_SEC
        halt = breaker.halt or _stop_requested(ident)
        if halt is None and left > 0:
            continue
        # Не оставляем зомби-сессию жрать CPU: слот нужен следующей задаче.
        _kill_session(proc, pgid)
        return None, halt


def _watch_memory(pid: int, done: threading.Event, peak: list[int]) -> None:
    """Пик веса дерева сессии, пока она жива. Ответ на «а это регрессия?».

    Без этого числа про потребление памяти можно только спорить: cgroup-лимит
    один на весь юнит и оба слота, и по нему не видно, какая задача сколько
    съела. Пик пишется в лог всегда, а не только при падении, — чтобы у
    «стало больше» была база для сравнения.
    """
    while not done.is_set():
        peak[0] = max(peak[0], _proc_tree_rss_kb(pid))
        done.wait(MEM_POLL_SEC)


# ── сессия ─────────────────────────────────────────────────────────────────

def _verdict_line(workdir: Path) -> str:
    """Одна строка вердикта проверяльщика из verdict.txt, либо пусто.

    Раннер не разбирает вывод сессии — он читает файл, который она обязана
    оставить (contract, шаг 9 VERIFY_TEMPLATE). Пустой или отсутствующий файл
    — тоже исход: сессия дошла до конца, но вердикта не оставила, и молчать
    об этом нельзя, иначе «проверка была» неотличимо от «проверки не было».
    """
    try:
        text = (workdir / "verdict.txt").read_text(errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


REFERENCE_FILE = "expected.md"
# Что проверка обязана назвать первой строкой эталона (contract, шаг 1а).
CRITERION_SOURCES = ("задача", "запасной", "нет")
# Строку сессии пишут по-разному — списком, жирным, через тире; разбор
# терпит разметку вокруг слова, но не другое слово.
_CRITERION_LINE = re.compile(
    r"^[\s#>*_-]*Источник критерия\s*[:—-]?\s*[*_`]*\s*[:—-]?\s*[*_`]*\s*"
    r"(" + "|".join(CRITERION_SOURCES) + r")\b",
    re.IGNORECASE | re.MULTILINE)
# Голова вердикта, которому не с чем было сверяться: `expected.md` не
# записан. Своё слово, а не пустая строка, — чтобы в чате и в журнале было
# видно, что проверка вердикт ДАЛА, но раннер его не принял, и почему.
NO_REFERENCE = "БЕЗ_ЭТАЛОНА"


def _criterion_source(workdir: Path) -> str | None:
    """Откуда эталон проверки, по `expected.md`: `задача` / `запасной` / `нет`.

    `None` — файла нет вовсе. Это другой ответ, чем `нет`: «нет» значит, что
    сессия искала критерий и не нашла, а отсутствие файла — что шаг 1а не
    делался, и заявка автора читалась без эталона. Пустая строка — файл
    есть, а первой строки по форме нет: эталон записан, учёт по классам
    не выйдет.
    """
    try:
        text = (workdir / REFERENCE_FILE).read_text(errors="replace")
    except OSError:
        return None
    m = _CRITERION_LINE.search(text)
    return m.group(1).lower() if m else ""


def _accepted_verdict(workdir: Path, verdict: str) -> str:
    """Вердикт проверки — только вместе с эталоном, записанным до заявки.

    Вердикт без `expected.md` рядом — это проверка, читавшая заявку автора
    без своего ожидания, ровно тот путь, которым DEV-46 дал два ложных `OK`
    подряд. Такой вердикт не выбрасывается (текст его нужен человеку), а
    помечается: голова `БЕЗ_ЭТАЛОНА` даёт ⚠️ в чате и не даёт `VERIFIED` в
    учёте — см. `_verdict_icon` и `_VERDICT_OUTCOME`.
    """
    if verdict and _criterion_source(workdir) is None:
        return f"{NO_REFERENCE} {verdict}"
    return verdict


VERIFY_RESULTS_KEY = os.environ.get("AGENT_VERIFY_RESULTS_KEY", "agent:verify:results")


# Что показать в чате на каждый исход. Строка короткая намеренно: она
# дописывается в сообщение задачи, которое читают все, и подробностям там не
# место — полный вердикт сессия и так кладёт комментарием в саму задачу.
# 31.08.2026 в чат уезжал вердикт целиком, абзацем на пол-экрана.
#
# Подпись зависит от ТОЧКИ проверки, а не только от вердикта (DEV-115). До
# этого «выкат проверен» стояло константой, и проверка ветки — та самая, что
# у client-b единственная включённая, — рапортовала в чат о выкате, которого
# не было: работа даже не смержена. Слово «выкат» — утверждение о том, что
# работа доехала до прода, и оно принадлежит одной точке из двух.
#
# Точку раннер берёт из `trigger` задачи — то самое слово, которое ворота
# приёма выбрали по предковости коммита (`deploy.mode_for`): `branch` —
# ветка до мержа, `state`/`build`/`deploy` — работа уже в основной ветке.
# Совпадение слов у двух процессов закреплено `test_verify_bridge.py`.
MODE_BRANCH = "branch"
MODES_AFTER_MERGE = ("state", "build", "deploy")

# Порядок в кортеже: ветка до мержа · выкат после мержа · точка неизвестна.
# Третья колонка не роскошь: проверку могли поставить руками, и тогда
# `trigger` пуст. Назвать при этом любую из двух точек значит соврать в ту
# же сторону, ради которой правка и делалась, — поэтому там нейтральное
# слово, а не «наверное, выкат».
_BRANCH, _DEPLOY, _UNKNOWN = 0, 1, 2
_NO_REFERENCE_LABEL = "вердикт не принят: проверка не записала эталон (expected.md)"
_VERDICT_LABEL = {
    "OK": ("ветка проверена",
           "выкат проверен",
           "проверка прошла"),
    "PARTIAL": ("ветка на месте, поведение проверить было нечем",
                "выкат доехал, поведение проверить было нечем",
                "поведение проверить было нечем"),
    "FIXED": ("ветка проверена со второго раза",
              "выкат доехал со второго раза",
              "проверка прошла со второго раза"),
    "FAIL": ("ветка НЕ подтверждена",
             "выкат НЕ подтверждён",
             "проверка НЕ подтвердила работу"),
    "БЕЗ_ЭТАЛОНА": (_NO_REFERENCE_LABEL,) * 3,
}


def _label_slot(mode: str) -> int:
    """Какая колонка подписи у этого режима контракта."""
    if mode == MODE_BRANCH:
        return _BRANCH
    return _DEPLOY if mode in MODES_AFTER_MERGE else _UNKNOWN


def _verdict_head(verdict: str) -> str:
    """Первое слово вердикта — по нему выбираются значок и подпись.

    Класс расхождения едет в том же слове после `/` (`FAIL/spec …`, DEV-89)
    и головой не считается: `FAIL/spec` — это `FAIL`. Пятого слова в голове
    нет намеренно — по ней ключуются значки, подписи и карта исходов.
    """
    return verdict.upper().split(" ", 1)[0].split("/", 1)[0].rstrip(":")


# ── класс расхождения: второе поле вердикта, не пятое слово (DEV-89) ───────
#
# Любой `FAIL` уезжал обратно в работу — и тот, где чинить нечего: DEV-78
# «три из четырёх пунктов критерия недостижимы» → агент → снова FAIL. А
# DEV-43 дал четыре `PARTIAL` подряд за $21,45 по одной причине: у репозитория
# нет стенда, наблюдать негде. Класс говорит, ЧТО разошлось, и по нему
# маршрут (`verdict_route`) и постановка следующей проверки
# (`deploy.decide`) решают, кому это чинить и чинить ли вообще.
CLASS_IMPLEMENTATION = "implementation"   # разошлась работа — чинит агент
CLASS_SPECIFICATION = "specification"     # не выдерживает постановка — человек
CLASS_UNOBSERVABLE = "unobservable"       # наблюдать было негде — человек
VERDICT_CLASSES = (CLASS_IMPLEMENTATION, CLASS_SPECIFICATION, CLASS_UNOBSERVABLE)
# Слово в вердикте → класс. Короткие формы — чтобы строка вердикта не
# разбухала; разбор терпит регистр и знак препинания после слова.
_CLASS_WORDS = {
    "impl": CLASS_IMPLEMENTATION, "implementation": CLASS_IMPLEMENTATION,
    "spec": CLASS_SPECIFICATION, "specification": CLASS_SPECIFICATION,
    "unobs": CLASS_UNOBSERVABLE, "unobservable": CLASS_UNOBSERVABLE,
}
# Класс, когда поля нет: прежнее поведение. `FAIL` без класса — дефект
# работы, как и до классов; `PARTIAL` без класса — наблюдать не вышло.
_CLASS_BY_HEAD = {"FAIL": CLASS_IMPLEMENTATION, "PARTIAL": CLASS_UNOBSERVABLE}


def _class_word(verdict: str) -> str:
    """Слово класса из первого токена (`FAIL/spec:` → `spec`), пусто — поля нет."""
    first = verdict.split(" ", 1)[0]
    if "/" not in first:
        return ""
    return first.split("/", 1)[1].strip(":;,.").lower()


def verdict_class(verdict: str) -> str:
    """Класс расхождения вердикта: явный из поля после `/`, иначе по голове.

    Прошедшим (`OK`, `FIXED`) класса нет — расходиться нечему; пустому,
    незнакомому и не принятому раннером (`БЕЗ_ЭТАЛОНА`) — тоже. Незнакомое
    слово в поле — как отсутствие поля: по голове, а не «наверное, это
    spec». Слово, а не разбор прозы: раннер читает признак, не догадывается.
    """
    if not verdict:
        return ""
    head = _verdict_head(verdict)
    explicit = _CLASS_WORDS.get(_class_word(verdict), "")
    if explicit and head in _CLASS_BY_HEAD:
        return explicit
    return _CLASS_BY_HEAD.get(head, "")


def _verdict_label(verdict: str, mode: str = "") -> str:
    """Короткая подпись для чата. Незнакомое начало — не «наверное, всё
    хорошо»: говорим прямо, что вердикт не разобран, и зовём в задачу.

    `mode` — режим контракта проверки (`trigger` задачи). Не задан — точку
    не называем вовсе: подпись говорит про проверку, а не про выкат.
    """
    row = _VERDICT_LABEL.get(_verdict_head(verdict))
    return row[_label_slot(mode)] if row else "вердикт проверки не разобран"


def _verdict_icon(verdict: str) -> str:
    """Значок исхода проверки выката по первому слову вердикта."""
    head = _verdict_head(verdict)
    if head == "OK":
        return "✅"
    if head == "PARTIAL":
        # Доставка подтверждена, поведение — нет (сервис не пишет в журнал,
        # живёт на другом хосте, безопасной пробы не придумалось). Под ✅ это
        # прятать нельзя: ровно из-за слитого «проверено» в чате читалось
        # «работает» там, где сверены одни байты. И не ⚠️: тревожиться не о
        # чем, выкат доехал.
        return "🟡"
    if head == "FIXED":
        # Отдельный значок, а не ✅: выкат доехал только со второго раза.
        # Зелёная джоба при этом была и в первый — значит в доставке что-то
        # шатается, и человеку стоит это увидеть, а не прочитать «всё хорошо».
        return "🔁"
    # FAIL и всё, что не разобрали. Незнакомая строка — это не «наверное, всё
    # хорошо»: под ✅ её никто не пойдёт читать, и недоехавший выкат останется
    # незамеченным ровно так же, как без проверки вообще.
    return "⚠️"


# ── маршрут по вердикту: состояние и исполнитель — по правилу трёх исходов ──
#
# Решение владельца 13.09.2026, подтверждено 15.09 (DEV-96): прошла → на
# приёмку, человек мержит; не прошла и чинится → «не проверено», исполнитель
# агент; не прошла и не чинится → «не проверено», исполнитель человек. Из «не
# проверено» на приёмку хода нет. Сессия и так получает эти правила
# контрактом (`prompt.OUTCOMES_FAILURE`), но раннер ей на слово не верит —
# ровно как `ensure_handoff` на рабочей сессии: до этого отказ раннера
# (`БЕЗ_ЭТАЛОНА`) жил только в журнале, а в трекере стояло «проверено».

# Куда едет «проверять было нечего» — решено счётом по журналу (DEV-99).
# Решение 13.09.2026 («нечего проверять» → туда же, куда прошедшая) принималось,
# когда проверка стояла ПОСЛЕ мержа: там `Done` не врёт, работа уже в основной
# ветке. С переносом проверки ДО мержа тот же маршрут означал бы приёмку, и
# 15.09 (DEV-96) так и было сделано — выбором исполнителя, без счёта.
#
# Счёт по 95 каталогам проверок (92 вердикта) на 16.09.2026:
#   • ни один вердикт не подошёл под сам признак: `НЕЧЕГО` вторым словом — 0,
#     `expected.md` с первой строкой `нет` — 0. Развилка ни разу не сработала;
#   • все 24 реальных `PARTIAL` — из времени, когда проверка шла после мержа,
#     и 18 из них по причине «выката не было / выкат неполный»: до мержа этой
#     причины не существует. 19 проверок в режиме `branch` дали 14 `OK` и
#     5 `FAIL` и ни одного `PARTIAL`;
#   • на доске все 24 и так уехали в «не проверено» (17 — сразу переходом
#     самой сессии), и человек их разобрал: 9 переводов «не проверено» → `Done`.
# То есть правило «до мержа — на приёмку» не совпадало с практикой ни разу.
#
# Мера из постановки — «меньше грузит утреннюю стопку» — двух вариантов не
# различает: потолок тяги считает СУММУ двух стопок (`puller.DEFAULT_OPEN_
# STATES`), а сверка мержей обходит обе (`reconcile`), так что задача с
# доехавшей до main работой закроется сама из любой. Различает их вторая мера:
# на приёмке задача неотличима по состоянию от проверенной, в «не проверено» —
# отличима, и мерж оттуда помечен как решение человека (`signing.risky`).
# Поэтому до мержа — «не проверено» к человеку, после мержа — как было.

# Второе слово вердикта `PARTIAL`, которое отличает «проверять было нечего»
# (стенд занят, выката не было, критерий не определён) от «было что, но не
# вышло». Слово, а не разбор прозы: раннер читает признак, а не догадывается.
PARTIAL_NOTHING = "НЕЧЕГО"


def _nothing_to_verify(verdict: str, criterion: str = "") -> bool:
    """`PARTIAL`, за которым проверять было нечего. Два детерминированных
    признака: слово `НЕЧЕГО` вторым в вердикте либо `нет` в первой строке
    эталона — критерий не определён, и сверять было не с чем."""
    if _verdict_head(verdict) != "PARTIAL":
        return False
    if criterion == "нет":
        return True
    words = verdict.split()
    return len(words) > 1 and words[1].strip(":;,.—-").upper() == PARTIAL_NOTHING


@dataclass(frozen=True)
class Route:
    """Куда едет задача после вердикта. Имена ролей, не id и не имена
    состояний: их подставляет `ensure_verdict_route` из настройки."""
    state: str          # review | done | unverified
    assignee: str       # reviewer | agent | requester
    label: str = ""     # метка, которую надо повесить (пусто — никакой)
    why: str = ""       # причина — в комментарий и журнал


def verdict_route(verdict: str, *, before_merge: bool, rounds: int | None,
                  criterion: str = "", in_unverified: bool = False,
                  max_rounds: int | None = None,
                  discrepancy: str | None = None,
                  gates_state: str = "", mergeable: bool | None = None) -> Route:
    """Правило трёх исходов — чистой функцией, одно на обе точки проверки.

    `rounds` — рабочих заходов по журналу раннера (ворота кладут в задачу,
    DEV-86); None — не сосчитаны, и тогда провал идёт человеку: отправить
    агента крутиться без счёта хуже лишнего разбора руками. `in_unverified` —
    задача УЖЕ лежит в «не проверено»: оттуда на приёмку хода нет ни при
    каком вердикте, решает человек. `discrepancy` — класс расхождения
    (DEV-89); None — взять из самого вердикта (`verdict_class`). На агента
    едет только `implementation`: `specification` он не починит, а при
    `unobservable` чинить нечего — оба у человека.

    `before_merge` меняет ровно два исхода, и оба — те, где решает сам мерж:
    прошедшая едет на приёмку (до) или закрывается (после), а «проверять было
    нечего» до мержа уходит человеку в «не проверено» с меткой, а не на
    приёмку (DEV-99 — счёт по журналу в комментарии над `PARTIAL_NOTHING`).

    `mergeable` — вливается ли ветка в основную (DEV-109); факт собирают
    ворота постановки и кладут в задачу, `None` — не спрашивали или хостинг
    не ответил. `False` до мержа не пускает работу в стопку «ждёт мержа» ни
    при каком вердикте: стопка обещает человеку, что нажатие сработает, а
    15.09.2026 оно не сработало трижды подряд. Уводит она в «не проверено» к
    человеку, а не к агенту: назначение на агента поднимает ОБЫЧНУЮ рабочую
    сессию и тратит круг, а конфликт кругом не считается — сессию разрешения
    конфликта ставит пересмотр стопки (`reconcile.Reconciler.restack`).
    Проверку это не отменяет: вердикт уже получен, и работа проверена.

    `gates_state` — суд над журналом ворот (`gatelog.judge`), пусто — ворот не
    смотрели. Он старше вердикта и здесь, ровно как в `outcome_of` (DEV-107):
    что не записано в «проверено», то и на приёмку не едет. Иначе задача с
    упавшими воротами лежит в стопке «мержить» — тот самый ложный `OK`, ради
    недопущения которого ворота и заведены. Исход не понижает только
    `прошло`; `не прошло`, `ошибка обвязки` и `нет свидетельств` — все три в
    «не проверено» к человеку: класса расхождения у прошедшего вердикта нет,
    а чинить это агента не пошлёшь — он не знает, что именно сломалось.
    """
    head = _verdict_head(verdict) if verdict else ""
    cls = verdict_class(verdict) if discrepancy is None else discrepancy
    limit = MAX_WORK_ROUNDS if max_rounds is None else int(max_rounds)
    passed = (Route("review", "reviewer", why="прошла проверку") if before_merge
              else Route("done", "requester", why="прошла проверку"))
    if gates_state and gates_state != gatelog.PASSED:
        passed = Route("unverified", "reviewer",
                       why=f"в «проверено» не записана: ворота — {gates_state}")
    elif before_merge and mergeable is False:
        # Причину ворот не затираем (оба исхода ведут в «не проверено», а
        # названа должна быть та, что старше): конфликт смотрим, только
        # когда ворота не понизили маршрут сами.
        passed = Route("unverified", "reviewer",
                       why="ветка не вливается в основную — в стопку «ждёт "
                           "мержа» не едет: кнопка мержа ответила бы отказом")
    if head in ("OK", "FIXED"):
        if in_unverified:
            return Route("unverified", "reviewer",
                         why="из «не проверено» на приёмку хода нет — решает человек")
        return passed
    if head == "PARTIAL":
        if cls == CLASS_SPECIFICATION:
            # Проверять было нечего, потому что постановка не выдерживает:
            # это не «прошла с меткой», это разбор руками.
            return Route("unverified", "reviewer",
                         why="постановка не выдерживает (specification) — "
                             "на приёмку не едет, решает человек")
        if _nothing_to_verify(verdict, criterion):
            if in_unverified:
                return Route("unverified", "reviewer", label=UNVERIFIABLE_LABEL,
                             why="проверять было нечего, но из «не проверено» "
                                 "на приёмку хода нет")
            if before_merge:
                # DEV-99: до мержа «нечего проверять» на приёмку НЕ едет.
                # Приёмка — стопка «мержить», и задача в ней неотличима по
                # состоянию от проверенной; метка это не чинит, потому что
                # состояние читают раньше меток. То же правило, что у ворот
                # десятью строками выше: что не записано в «проверено», то на
                # приёмку не едет, — а здесь в «проверено» не записано ничего.
                return Route("unverified", "reviewer", label=UNVERIFIABLE_LABEL,
                             why="проверять было нечего, а мержа ещё не было — "
                                 "на приёмку не едет, решает человек")
            return Route(passed.state, passed.assignee, label=UNVERIFIABLE_LABEL,
                         why=("проверять было нечего" if passed.state != "unverified"
                              else f"проверять было нечего, а {passed.why}"))
        return Route("unverified", "reviewer",
                     why="было что проверить, но не вышло — чинить нечего")
    if head == "FAIL":
        if cls == CLASS_SPECIFICATION:
            return Route("unverified", "reviewer",
                         why="не прошла: постановка не выдерживает (specification) "
                             "— агент это не починит, решает человек")
        if cls == CLASS_UNOBSERVABLE:
            return Route("unverified", "reviewer",
                         why="не прошла, но наблюдать было нечего (unobservable) "
                             "— чинить нечего, решает человек")
        if rounds is None:
            return Route("unverified", "reviewer",
                         why="не прошла, а круги не сосчитаны — без счёта агента не кручу")
        if limit > 0 and rounds >= limit:
            return Route("unverified", "reviewer",
                         why=f"не прошла, круги исчерпаны ({rounds} из {limit})")
        return Route("unverified", "agent",
                     why=f"не прошла, чинится (заходов {rounds}"
                         + (f" из {limit})" if limit > 0 else ", потолка нет)"))
    # Пустой, незнакомый, `БЕЗ_ЭТАЛОНА`: вердикта нет или раннер его не
    # принял. Это не «проверено» — и не провал работы: чинить нечего,
    # разбирается человек.
    return Route("unverified", "reviewer",
                 why=("проверка вердикта не оставила" if not verdict
                      else f"вердикт не принят раннером ({head})"))


def ensure_verdict_route(issue_id: str, ident: str, task: dict, verdict: str,
                         criterion: str, reviewer: dict, persona: str = "",
                         gates: list[gatelog.Gate] | None = None) -> None:
    """Довести маршрут по вердикту до трекера, если сессия увезла задачу не туда.

    Тот же механизм, что `ensure_handoff`: раннер сверяет факт с правилом и
    правит расхождение, оставляя помеченный комментарий. Сессия, которая всё
    сделала сама, не трогается. Осечка трекера — в журнал: маршрут — не повод
    ронять раннер, а вердикт человеку уже ушёл (`report_verdict`).
    """
    issue = _handoff_state(issue_id)
    if not issue:
        log.warning("%s: не смог прочитать задачу в Linear - маршрут по вердикту не сверил", ident)
        return
    state = (issue.get("state") or {}).get("name") or ""
    assignee = ((issue.get("assignee") or {}).get("id")) or ""
    rounds = task.get("rounds")
    rounds = int(rounds) if rounds not in (None, "") else None
    # Ворота судятся тем же разбором, что и исход запуска (`outcome_of`):
    # состояние в трекере и цифра в журнале обязаны говорить одно и то же.
    # `None` — ворот не читали (не проверка), и маршрут решает вердикт один.
    # Сливаемость ворота кладут в задачу только когда её знают: `None` и
    # отсутствие поля здесь означают одно — «не спрашивали».
    mergeable = task.get("mergeable")
    route = verdict_route(verdict, before_merge=str(task.get("trigger") or "") == "branch",
                          rounds=rounds, criterion=criterion,
                          in_unverified=(state == UNVERIFIED_STATE_NAME),
                          gates_state=gatelog.judge(gates)[0] if gates is not None else "",
                          mergeable=(mergeable if mergeable is True or mergeable is False
                                     else None))
    if route.state != "done" and (_closed(issue) or state == DONE_STATE_NAME):
        # Мерж старше вердикта. Задачу закрыл человек — мержем в основную
        # ветку (приём переводит её в Done сам, `deploy.on_merge`) или руками,
        # — а проверка, поднятая до того, доехала позже. Увезти закрытую
        # задачу в «не проверено» значило бы спорить с решением человека:
        # вердикт он уже получил (`report_verdict`), решать ему.
        log.info("%s: задача уже закрыта (%r), а маршрут по вердикту — %r (%s); "
                 "мерж старше вердикта, задачу не трогаю", ident, state,
                 route.state, route.why)
        return
    want_state = {"review": REVIEW_STATE_NAME, "done": DONE_STATE_NAME,
                  "unverified": UNVERIFIED_STATE_NAME}[route.state]
    if route.assignee == "agent":
        want_who, who_name = AGENT_USER_ID, (AGENT_NAME_HINT or "агента")
        if not want_who:
            log.warning("%s: агент в трекере не задан (AGENT_LINEAR_USER_ID) — "
                        "провал уходит принимающему", ident)
            want_who, who_name = reviewer.get("id") or "", reviewer.get("name") or "принимающего"
    elif route.assignee == "requester":
        want_who = str(task.get("requested_by") or "") or (reviewer.get("id") or "")
        who_name = (task.get("requested_by_name") if task.get("requested_by")
                    else reviewer.get("name")) or "постановщика"
    else:
        want_who, who_name = reviewer.get("id") or "", reviewer.get("name") or "принимающего"

    team = issue.get("team") or {}
    states = (team.get("states") or {}).get("nodes") or []
    state_id = next((st["id"] for st in states if st.get("name") == want_state), "")
    fields = {}
    if not state_id:
        log.error("%s: в команде %s нет статуса %r — состояние по вердикту не "
                  "сменится, есть только: %s", ident,
                  team.get("name") or team.get("id") or "?", want_state,
                  ", ".join(repr(st.get("name") or "") for st in states) or "ни одного")
    elif state != want_state:
        fields["stateId"] = state_id
    if want_who and assignee != want_who:
        fields["assigneeId"] = want_who
    if fields:
        log.warning("%s: сессия увезла задачу не туда (статус=%r, на=%s), маршрут "
                    "по вердикту — %r на %s (%s) - поправляю", ident, state,
                    assignee or "никого", want_state, who_name, route.why)
        _tracker.update_issue(issue_id, fields)
        linear_comment(
            issue_id,
            f"🤖 **Маршрут по вердикту поправлен раннером.** {route.why}: задача "
            f"должна лежать в `{want_state}` на {who_name}, а сессия оставила "
            f"`{state or '?'}`" + (" без исполнителя" if not assignee else "")
            + ". Правило одно на обе точки проверки: прошла → приёмка, не "
              "прошла → «не проверено» (чинится — на агенте, нет — у человека).",
            persona,
        )
    if route.label:
        have = {(lb.get("name") or "") for lb in ((issue.get("labels") or {}).get("nodes") or [])}
        if route.label not in have:
            if not _tracker.add_label(issue_id, team.get("id") or "", route.label):
                log.error("%s: метка %r не поставлена — доля непроверенного "
                          "недосчитается", ident, route.label)


# Машиночитаемый исход запуска: закрытый набор из постановки DEV-18. Ради
# него всё и затевалось — по коду возврата и тексту в журнале считать нельзя,
# а статусы трекера меряют не работу, а дисциплину того, кто их двигает.
#
# Раннер знает НЕ ВСЕ исходы набора, и это не дыра, а граница: REJECTED,
# CANCELLED и PAUSED — про решения людей, WAITING_CI — про пайплайн хостинга.
# Перечислены здесь целиком, чтобы читатель журнала видел полный словарь и не
# гадал, бывает ли что-то ещё; кто их пишет — появится, когда появится.
OUTCOMES = ("VERIFIED", "READY_FOR_HUMAN", "FAILED", "REJECTED",
            "CANCELLED", "PAUSED", "WAITING_CI", "WAITING_LIMIT")

# Первое слово вердикта проверяльщика → исход. Только OK и FIXED дают
# VERIFIED: у PARTIAL поведение проверить было нечем, и записать его в
# «проверено» значит повторить ровно ту ошибку, из-за которой у вердиктов
# разные значки (см. _verdict_icon). Незнакомое и пустое — тоже не провал:
# проверка не дала ответа, отвечает человек.
_VERDICT_OUTCOME = {"OK": "VERIFIED", "FIXED": "VERIFIED", "FAIL": "FAILED"}
# Головы вердикта, которые претендуют на «проверено» — и только они проходят
# суд ворот: FAIL и PARTIAL в VERIFIED не едут и без него.
_PASSING_HEADS = ("OK", "FIXED")


def _preflight_gates(task: dict) -> list[gatelog.Gate]:
    """Ворота перед постановкой (deploy.preflight, DEV-86) — записью журнала.

    Сессия поднялась — значит ворота её пропустили, и провала тут не бывает
    (`Skip` сессию не поднимает). Бывает третье: факт не сверен — трекер
    лёг, git не ответил, стор молчал. Ворота при этом сессию ставят («молчание
    — не отказ»), но в «проверено» несверённый факт не едет: это ошибка
    обвязки, а не «прошло». Ворот не было вовсе (`criteria_source` пуст —
    задача положена до них или другим путём) — записи нет.
    """
    if not task.get("criteria_source"):
        return []
    notes = [str(n) for n in (task.get("preflight_notes") or []) if str(n)]
    if notes:
        return [gatelog.Gate(name="ворота постановки", status=gatelog.ERROR,
                             note="; ".join(notes), source="постановка")]
    return [gatelog.Gate(name="ворота постановки", status=gatelog.PASSED,
                         note="задача, ветка и коммит сверены", source="постановка")]


def _gates_of(workdir: Path, task: dict) -> list[gatelog.Gate]:
    """Все свидетельства одного запуска проверки, из двух источников: ворота
    постановки из задачи и `gates.json` из рабочего каталога — что сессия
    запускала и с каким кодом. Туда `logdiff.py` пишет свой код сам (10 —
    находка, «не прошло»; 2 — «ошибка обвязки»), остальное сессия оборачивает
    `gatelog.py`. Битый файл — одна запись «ошибка обвязки», не пустота."""
    return _preflight_gates(task) + gatelog.read(workdir / gatelog.GATES_FILE)


def outcome_of(*, rc: int | None, kind: str, verdict: str,
               waiting_limit: bool, gates: list[gatelog.Gate],
               halted: str = "", waiting_ci: bool = False) -> str:
    """Чем кончился ОДИН запуск. Чистая функция: никакого чтения файлов.

    `halted` — кто снял сессию до конца (`Halt.kind`): `human` — команда
    `/stop`, и это решение человека, то есть CANCELLED; `loop` — прерыватель
    раннера, и это FAILED, как любая не дошедшая сессия. Вердикт старше и
    здесь: снятая на выходе проверка с записанным вердиктом — состоявшаяся.

    `waiting_ci` — сессия оставила заявку на ожидание проверок, и раннер её
    принял (DEV-16): исход этого запуска — WAITING_CI, то же понятие, что
    WAITING_LIMIT, только ждём не сброса квоты, а чужую систему. Лимит
    старше: не продолженная после лимита сессия ничего ждать не может.

    Порядок веток — порядок доверия, и ворота в нём старше вердикта (DEV-88):
    вердикт проверяльщика — слово модели, ворота — код возврата команды.
    `OK` поверх упавших ворот в «проверено» не записывается никогда; `OK`
    без единого свидетельства в журнале ворот — тоже: пустая сессия иначе
    проходила бы как проверенная. Ошибка обвязки — не «прошло»: команду,
    которая не выполнилась, нельзя зачесть как выполнившуюся чисто. Всё это
    едет человеку (`READY_FOR_HUMAN`), а не в провал: провал — это когда сама
    проверка сказала `FAIL`, и ей в эту сторону верят без ворот.

    Дальше как прежде: вердикт сильнее кода возврата (проверка, дошедшая до
    вердикта и снятая на выходе, — состоявшаяся проверка), лимит сильнее
    кода возврата (это не поломка, а «рано»).
    """
    if kind == "verify" and verdict:
        head = _verdict_head(verdict)
        if head in _PASSING_HEADS:
            state, _ = gatelog.judge(gates)
            return "VERIFIED" if state == gatelog.PASSED else "READY_FOR_HUMAN"
        return _VERDICT_OUTCOME.get(head, "READY_FOR_HUMAN")
    if waiting_limit:
        return "WAITING_LIMIT"
    if waiting_ci:
        return "WAITING_CI"
    if halted == HALT_HUMAN:
        return "CANCELLED"
    if rc == 0:
        # Сдано — не значит принято. READY_FOR_HUMAN и читается как «работа
        # уехала человеку», а не как успех: открытый MR задачей не является.
        return "READY_FOR_HUMAN"
    # OOM, таймаут (rc is None) и ненулевой код — один исход: сессия не дошла.
    # Почему именно не дошла, разбирается по журналу и по комментарию в
    # задаче; в журнале запусков нужна цифра, а не диагноз.
    return "FAILED"


def report_verdict(task: dict, verdict: str, log_path: Path) -> None:
    """Исход проверки выката — в уже висящее сообщение задачи, а не новым.

    Раньше каждая проверка постила отдельное сообщение, и чат зашумлялся: на
    одну задачу приходило и уведомление о назначении, и о сдаче, и об исходе
    проверки. Теперь строка дописывается к сообщению самой задачи — там же,
    где обновляется её статус.

    Правит сообщение events-api: индекс сообщений и исходный markdown лежат в
    его redis (RU-зеркало), куда раннеру ходу нет. Мост — список в ЛОКАЛЬНОМ
    redis, в который смотрят оба: раннер напрямую, events-api как
    `AGENT_REDIS_URL`. Ни нового секрета, ни нового порта для этого не нужно.

    Провал — исключение: он уходит ещё и отдельным сообщением. Тихо
    дописанная строка в сообщении, которое давно уехало вверх по чату, для
    недоехавшего выката слишком незаметна.
    """
    if not verdict:
        verdict = f"проверка выката прошла, но вердикта не оставила; лог: {log_path}"
    icon = _verdict_icon(verdict)
    issue_id = task.get("issue_id") or ""
    delivered = False
    if issue_id:
        try:
            redis.from_url(REDIS_URL).lpush(VERIFY_RESULTS_KEY, json.dumps({
                "issue_id": issue_id,
                "identifier": task.get("identifier") or "",
                "icon": icon,
                # В чат — подпись, а не вердикт: он бывает в несколько строк,
                # а сообщение задачи читают все и целиком. Точка проверки —
                # из той же задачи: без неё подпись рапортует о выкате на
                # непомерженной ветке (DEV-115).
                "verdict": _verdict_label(verdict, str(task.get("trigger") or "")),
                # Коммит и первая строка вердикта — воротам перед следующей
                # постановкой (deploy.preflight): повтор того же коммита они
                # отсекают и называют, чем он кончился в прошлый раз.
                "commit_sha": task.get("commit_sha") or "",
                "line": verdict[:300],
            }))
            delivered = True
        except Exception as e:
            log.warning("вердикт не доехал до events-api (%s): %s", issue_id, e)
    # Отдельное сообщение — когда правку доставить не вышло (нет issue_id,
    # лёг redis) или когда исход тревожный. Молча потерять вердикт нельзя:
    # это ровно тот случай, ради которого проверка и заведена.
    if not delivered or icon == "⚠️":
        line = f"{icon} {task.get('identifier') or '?'}: {verdict}\n{task.get('url', '')}"
        # Раньше тревожный вердикт уходил дважды: строкой в общий чат и
        # отдельным сообщением в личку. Теперь notify по умолчанию адресован
        # владельцу лично, и второй раз тому же человеку слать нечего.
        notify(line)


# ── вердикт дежурного: та же дисциплина, что у проверки выката (DEV-25) ────
#
# У `kind=verify` вердикт строгий, и по нему раннер сам выбирает сигнал. У
# дежурного — самого срочного вида сессий — до DEV-25 не было ничего:
# `ensure_incident_handoff` умеет спросить только «дошла ли задача до
# принимающего» и не отличает «прод поднят» от «не смог ничего».
#
# Файл тот же (`verdict.txt`) и читается тем же `_verdict_line`: два разбора
# одной строки разошлись бы молча. Голова считается тем же `_verdict_head` —
# первое слово, регистр вверх, класс после `/` отрезан. Классов расхождения у
# дежурного нет, но и запрета на них в слове нет: пусть `RESTORED/playbook`
# читается как `RESTORED`, а не как незнакомое слово.
_INCIDENT_ICON = {"RESTORED": "✅", "FALSE_ALARM": "ℹ️", "UNRESOLVED": "⚠️"}
# Строка на месте вердикта, которого нет. Своими словами, а не пустотой:
# сообщение всё равно уйдёт, и «дежурство кончилось ничем» человек обязан
# прочитать словами, а не догадаться по отсутствию текста.
NO_INCIDENT_VERDICT = ("дежурство кончилось, но вердикта (`verdict.txt`) не "
                       "осталось: восстановлен прод или нет — неизвестно")


def _incident_icon(verdict: str) -> str:
    """Значок дежурства по первому слову вердикта.

    Пустая строка и незнакомое слово — ⚠️, а не «наверное, всё хорошо». Это то
    же правило, что у проверки выката, и по той же причине: под ✅ сообщение
    никто не пойдёт читать, и невосстановленный прод останется незамеченным
    ровно так же, как без дежурного вообще.
    """
    return _INCIDENT_ICON.get(_verdict_head(verdict), "⚠️")


def report_incident_verdict(task: dict, verdict: str, log_path: Path) -> None:
    """Чем кончилось дежурство — отдельным сообщением, всегда.

    У проверки выката спокойный вердикт молчит в чате и дописывается строкой в
    сообщение задачи через мост events-api. Дежурному этот путь не годится, и
    не из-за техники: инцидент читают в ту минуту, когда он идёт, а сообщение
    задачи к этому времени уехало вверх по чату. Отдельное сообщение здесь —
    не шум, а единственная форма, в которой «прод подняли» доходит вовремя.
    По той же причине молчания на успехе нет: у дежурства успех — это событие,
    а не отсутствие плохих новостей.
    """
    icon = _incident_icon(verdict)
    if not verdict:
        verdict = f"{NO_INCIDENT_VERDICT}; лог: {log_path}"
    notify(f"{icon} {task.get('identifier') or '?'}: {verdict}"
           f"\n{task.get('url', '')}")


# ── приём задач: вложения из Telegram и постановка обратно в events-api ────

INTAKE_RESULTS_KEY = os.environ.get("AGENT_INTAKE_RESULTS_KEY", "agent:intake:results")
# Лимит Bot API на скачивание, не наш. events-api отсекает по file_size ещё на
# приёме, здесь — вторая линия: размер в апдейте не всегда приходит, и тогда
# правду говорит только сам getFile.
INTAKE_MAX_FILE_BYTES = 20 * 1024 * 1024
# Потолок на всё скачивание пачки. Качается оно ДО сессии, то есть вне её
# таймаута: сорок вложений по 120с сетевого ожидания держали бы слот полтора
# часа молча, а слотов два. Упёрлись — оставшиеся помечаются как не доехавшие,
# и сессия про это прочтёт.
INTAKE_DOWNLOAD_BUDGET_SEC = float(os.environ.get("AGENT_INTAKE_DOWNLOAD_BUDGET_SEC", "180"))
INTAKE_GETFILE_TIMEOUT_SEC = float(os.environ.get("AGENT_INTAKE_GETFILE_TIMEOUT_SEC", "15"))
INTAKE_DOWNLOAD_TIMEOUT_SEC = float(os.environ.get("AGENT_INTAKE_DOWNLOAD_TIMEOUT_SEC", "60"))


def _telegram_file_path(file_id: str) -> tuple[str | None, str]:
    """getFile → путь файла на серверах Telegram, либо (None, причина).

    Причина возвращается текстом, а не глотается: файл больше 20 МБ Bot API
    отдать не может («file is too big»), и сессия должна знать, что кусок
    фактуры отсутствует, — иначе она соберёт постановку по неполному набору и
    никто этого не заметит.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
    data = json.dumps({"file_id": file_id}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=INTAKE_GETFILE_TIMEOUT_SEC) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            return None, f"getFile: HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"getFile: {e}"
    if not body.get("ok"):
        return None, str(body.get("description") or "getFile отказал")
    path = ((body.get("result") or {}).get("file_path") or "")
    return (path, "") if path else (None, "getFile не вернул file_path")


def _safe_name(index: int, name: str) -> str:
    """Имя файла из Telegram — чужая строка: `../` в ней вывела бы запись за
    рабочий каталог. Берём только последний компонент и оставляем безобидные
    символы; номер спереди сохраняет порядок пересылки и разводит тёзок."""
    base = Path(name or "file").name
    safe = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in base).strip()
    return f"{index:02d}-{safe or 'file'}"[:120]


# Расширение → contentType. Мессенджеры отдают его не всегда: MAX не отдаёт
# вовсе, Telegram — не для всех типов. Linear просит contentType при загрузке,
# и «application/octet-stream» для .xlsx означает, что человек вместо таблицы
# получит непонятный бинарник.
_CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pdf": "application/pdf", ".csv": "text/csv", ".txt": "text/plain",
    ".log": "text/plain", ".json": "application/json", ".md": "text/markdown",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".zip": "application/zip",
}


def _content_type(item: dict, path: Path) -> str:
    # `mime_type` — имя из первой реализации приёма: такие куски ещё лежат в
    # очередях и в незакрытых пачках, и терять из-за переименования тип файла
    # незачем.
    return (item.get("mime") or item.get("mime_type")
            or _CONTENT_TYPES.get(path.suffix.lower())
            or "application/octet-stream")


def upload_to_linear(path: Path, item: dict) -> tuple[str, str]:
    """Файл → asset в Linear. Возвращает (assetUrl, причина отказа).

    Зачем здесь, а не в events-api: байты лежат на хосте, у контейнера 128 МБ
    лимита памяти и общего тома с хостом нет. Загрузка живёт там же, где файл.

    Linear даёт presigned URL (`fileUpload`), файл кладётся PUT-ом прямо в
    хранилище — через наш процесс байты не идут второй раз. Заголовки из
    ответа обязательны: без них хранилище отвечает 403.

    Пустая строка вместо ссылки — не авария: постановка соберётся и без
    вложения, а причина уедет человеку в комментарий. Молча терять файл
    нельзя (DEV-36: до 08.09 они терялись именно молча), но и рушить из-за
    него сессию не за чем.
    """
    if not LINEAR_API_KEY:
        return "", "LINEAR_API_KEY не задан"
    try:
        size = path.stat().st_size
        ctype = _content_type(item, path)
        q = ("mutation($ct:String!,$fn:String!,$sz:Int!){"
             " fileUpload(contentType:$ct, filename:$fn, size:$sz){"
             " success uploadFile{ uploadUrl assetUrl headers{ key value } } } }")
        body = json.dumps({"query": q, "variables": {
            "ct": ctype, "fn": path.name, "sz": size}}).encode()
        req = urllib.request.Request(
            LINEAR_API_URL, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": LINEAR_API_KEY})
        with urllib.request.urlopen(req, timeout=INTAKE_DOWNLOAD_TIMEOUT_SEC) as r:
            d = json.loads(r.read())
        if d.get("errors"):
            return "", f"fileUpload: {str(d['errors'])[:120]}"
        uf = (((d.get("data") or {}).get("fileUpload") or {}).get("uploadFile") or {})
        if not uf.get("uploadUrl"):
            return "", "fileUpload не вернул ссылку"
        put = urllib.request.Request(uf["uploadUrl"], data=path.read_bytes(),
                                     method="PUT")
        put.add_header("Content-Type", ctype)
        for h in uf.get("headers") or []:
            put.add_header(h["key"], h["value"])
        with urllib.request.urlopen(put, timeout=INTAKE_DOWNLOAD_TIMEOUT_SEC) as r:
            if r.status not in (200, 201, 204):
                return "", f"хранилище ответило {r.status}"
        return uf["assetUrl"], ""
    except Exception as e:
        return "", f"загрузка в Linear: {e}"


def download_intake_files(items: list[dict], dest: Path) -> list[dict]:
    """Скачать вложения пачки в рабочий каталог сессии. Возвращает items с
    добавленным `path` (или `error`).

    Качает раннер, а не events-api: у контейнера 128 МБ лимита памяти, общего
    тома с хостом нет, а нужны файлы именно здесь — сессия читает их с диска.
    Неудача одного файла не рушит пачку: остальные доедут, а про пропавший
    сессия прочтёт в промпте и честно скажет об этом в постановке.
    """
    dest.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + INTAKE_DOWNLOAD_BUDGET_SEC
    out, downloaded, failed, uploaded = [], 0, 0, 0
    for i, item in enumerate(items, 1):
        item = dict(item)
        # Заход по пачке бывает не первый: на «Повторить» те же элементы
        # приезжают обратно с прошлой пометкой об ошибке. Не сняв её, мы
        # показали бы сессии «НЕ ДОЕХАЛ» на файле, который в этот раз доехал:
        # _intake_item_line смотрит на error раньше, чем на path.
        item.pop("error", None)
        item.pop("path", None)
        # Два источника с разной механикой получения байтов:
        #   Telegram — file_id, по нему getFile отдаёт временный путь;
        #   MAX      — прямая ссылка уже в событии, getFile-аналога нет.
        # Ветка выбирается по тому, что реально лежит в элементе, а не по
        # полю source: очередь переживает деплой и может содержать элементы,
        # положенные кодом, который про source ещё не знал.
        file_id = item.get("file_id")
        direct = item.get("file_url")
        if not file_id and not direct:
            out.append(item)
            continue
        if time.monotonic() > deadline:
            item["error"] = "не успели скачать за отведённое время"
            failed += 1
            out.append(item)
            continue
        if direct:
            url = direct
            name_hint = item.get("file_name") or "вложение"
        else:
            path, why = _telegram_file_path(file_id)
            if not path:
                item["error"] = why
                failed += 1
                log.warning("вложение %s не скачалось: %s", item.get("file_name"), why)
                out.append(item)
                continue
            url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}"
            name_hint = item.get("file_name") or Path(path).name
        target = dest / _safe_name(i, name_hint)
        try:
            with urllib.request.urlopen(url, timeout=INTAKE_DOWNLOAD_TIMEOUT_SEC) as resp, \
                    target.open("wb") as fh:
                written = 0
                while chunk := resp.read(64 * 1024):
                    written += len(chunk)
                    if written > INTAKE_MAX_FILE_BYTES:
                        raise ValueError("файл больше 20 МБ")
                    fh.write(chunk)
        except Exception as e:
            target.unlink(missing_ok=True)
            item["error"] = f"скачивание: {e}"
            failed += 1
            log.warning("вложение %s не скачалось: %s", item.get("file_name"), e)
        else:
            item["path"] = str(target)
            downloaded += 1
            # Заливаем сразу, пока файл под рукой: рабочий каталог сессии
            # со временем подчищается, и после этого оригинал не достать
            # ниоткуда — в мессенджере ссылка тоже живёт не вечно (у MAX
            # ~30 дней). Задача в Linear переживает и то, и другое.
            asset, why = upload_to_linear(target, item)
            if asset:
                item["asset_url"] = asset
                uploaded += 1
            else:
                item["upload_error"] = why
                log.warning("вложение %s не залилось в Linear: %s",
                            item.get("file_name"), why)
        out.append(item)
    if downloaded or failed:
        log.info("вложений скачано %d, не доехало %d, залито в Linear %d",
                 downloaded, failed, uploaded)
    return out


# ── почему сессия не дошла до конца ───────────────────────────────────────
#
# Код возврата ничего не объясняет: `завершилась с кодом 1` отправляет человека
# читать session.log на сервере, хотя причина лежит в последних строках этого
# самого лога. Самая частая из них — исчерпанный лимит Claude; она особая тем,
# что повтор до сброса упадёт так же, и предлагать его нечестно.
# Только формулировки самого CLI: «You've hit your session limit», «Claude
# usage limit reached». Общее «rate limit» сюда не годится — так ругается любой
# чужой API из-под сессии, и мы бы объявили лимитом обычную поломку: настоящую
# причину из лога при этом не показали бы, а человеку предложили бы ждать
# сброса, которого не будет.
_LIMIT_MARKERS = ("session limit", "usage limit")
# «You've hit your session limit · resets 10:10pm (UTC)» — время сброса идёт
# после слова resets и до конца строки или разделителя. Формулировка у CLI
# меняется («resets at 10pm»), поэтому берём хвост строки, а не разбираем время.
_RESET_RE = re.compile(r"resets?(?:\s+at)?\s+([^\n·|]{1,40})", re.I)


def _runner_authored_comment(explanation: str, log_path: Path) -> str:
    """Тело комментария-заглушки за молчаливую сессию: пояснение + хвост лога.

    Общий кусок между ensure_handoff и ensure_incident_handoff — обе строят
    один и тот же комментарий «раннер досдал», разница только в пояснении,
    почему сессия не досчиталась сама.
    """
    tail = _log_tail(log_path, lines=60)
    return (
        f"🤖 **Досдано раннером.** {explanation}"
        + chr(10) + chr(10)
        + "Полный лог на хосте: `" + str(log_path) + "`, сырой транскрипт - `"
        + str(log_path.parent / "transcript.jsonl") + "`" + chr(10) + chr(10)
        + (("```" + chr(10) + tail + chr(10) + "```") if tail else "_лог пуст_")
    )


# Статусы трекера, у которых тип «закрыто»: `completed` и `canceled` — это
# типы Linear, имена статусов у инсталляции свои (см. PROGRESS_STATE_NAME).
_CLOSED_STATE_TYPES = ("completed", "canceled")


def _closed(issue: dict) -> bool:
    """Задача в закрытом статусе — исход 2 или 3 контракта (сделано без
    кода / выполнить нельзя): кода нет, MR нет, ревью не требуется."""
    return ((issue.get("state") or {}).get("type") or "") in _CLOSED_STATE_TYPES


def _fresh_nodes(nodes: list[dict], since: datetime) -> list[dict]:
    """Комментарии, положенные после старта сессии, по возрастанию времени.

    Отдельно от `_fresh_comments`, потому что источников два и формы у них
    разные: снимок задачи (`fetch_issue_snapshot`) отдаёт узлы внутри
    `comments.nodes` и БЕЗ текста, а `list_comments` — плоский список с
    текстом и автором. Предикат «свежий» при этом один, и третья его копия
    разошлась бы с двумя первыми молча.
    """
    return sorted((c for c in (nodes or [])
                   if (c.get("createdAt") or "") > since.isoformat()),
                  key=lambda c: c.get("createdAt") or "")


def _fresh_comments(issue: dict, since: datetime) -> list[dict]:
    """Комментарии, положенные после старта сессии, — её отчёт."""
    return _fresh_nodes(((issue.get("comments") or {}).get("nodes") or []), since)


def _handed(issue: dict, reviewer: dict) -> bool:
    """Наступил ли финал контракта работы (DEV-78, DEV-90): `Verifying` без
    смены исполнителя, закрытый статус (исходы без кода) или статус приёмки
    на принимающем (ручной путь). Одно правило на досдачу и на схему сдачи."""
    state = (issue.get("state") or {}).get("name") or ""
    assignee = ((issue.get("assignee") or {}).get("id")) or ""
    return (state == VERIFYING_STATE_NAME or _closed(issue)
            or (state == REVIEW_STATE_NAME and assignee == reviewer.get("id")))


def _move_to_reviewer(issue: dict, issue_id: str, ident: str, reviewer: dict) -> None:
    """Статус приёмки + принимающий — одной правкой. Несданная работа идёт
    человеку, не на проверку: ветки может не быть, и ворота проверки
    оставили бы задачу висеть."""
    state = (issue.get("state") or {}).get("name") or ""
    assignee = ((issue.get("assignee") or {}).get("id")) or ""
    team = issue.get("team") or {}
    states = (team.get("states") or {}).get("nodes") or []
    review_id = next((st["id"] for st in states if st.get("name") == REVIEW_STATE_NAME), "")
    fields = {}
    if reviewer.get("id") and assignee != reviewer["id"]:
        fields["assigneeId"] = reviewer["id"]
    if not review_id:
        # Громко, а не молча: на второй инсталляции статуса с таким именем не
        # было, `review_id` выходил пустым, и сданная работа оставалась в
        # «в работе» — неотличимо от идущей сессии, и никто не понимал
        # почему. Исполнителя при этом всё равно меняем: потерянный статус —
        # не повод не сдать работу вовсе.
        log.error("%s: в команде %s нет статуса %r (AGENT_REVIEW_STATE_NAME), "
                  "есть только: %s — статус задачи не сменится, передаю "
                  "принимающему без него",
                  ident, team.get("name") or team.get("id") or "?", REVIEW_STATE_NAME,
                  ", ".join(repr(st.get("name") or "") for st in states) or "ни одного")
    elif state != REVIEW_STATE_NAME:
        fields["stateId"] = review_id
    _tracker.update_issue(issue_id, fields)


def hand_failed_to_human(issue_id: str, ident: str,
                         reviewer: dict | None = None) -> None:
    """Упавшая сессия: задача уходит человеку СО СМЕНОЙ СТАТУСА.

    Раньше здесь менялся только исполнитель, и этого не хватало: 15.09.2026
    сессия DEV-44 кончилась кодом 1, раннер честно передал задачу
    принимающему — и она осталась в «In Progress», теперь ещё и на человеке.
    Четыре часа спустя обход нашёл её висящей: на доске «агент работает», на
    деле не работает никто. Исполнитель — это «чьё», статус — это «что
    происходит», и врал именно статус.

    Комментарий с причиной в тот раз не доехал вовсе (ключ личности отдал
    401), и от аварии на доске не осталось ни следа. Поэтому перевод не
    зависит ни от комментария, ни от того, дался ли исполнитель: это разные
    записи, и одна не должна отменять другую.

    Двигаем статус ТОЛЬКО из рабочего состояния. Упавшая проверка выката
    стоит в `Verifying`, и тащить её оттуда в стопку человека значит решать
    за него другую задачу; снимок не прочитался — остаётся прежнее поведение,
    один исполнитель. Молчание тут хуже лишней строки, поэтому оба случая —
    в журнал.
    """
    rev = reviewer or current_reviewer()
    try:
        issue = _handoff_state(issue_id)
    except Exception as e:
        log.warning("%s: снимок задачи не прочитан (%s) — передаю человеку "
                    "без смены статуса", ident, e)
        issue = {}
    state = (issue.get("state") or {}).get("name") or ""
    if issue and state == PROGRESS_STATE_NAME:
        # Статус и исполнитель — одной правкой, тем же путём, что у не сданной
        # работы: «в работе» без работающего врёт про состояние доски.
        _move_to_reviewer(issue, issue_id, ident, rev)
        return
    if issue:
        log.info("%s: задача в «%s», а не в «%s» — статус не трогаю, меняю "
                 "исполнителя", ident, state or "?", PROGRESS_STATE_NAME)
    linear_assign_to_reviewer(issue_id, rev)


def ensure_handoff(issue_id: str, ident: str, log_path: Path,
                   reviewer: dict, since: datetime, persona: str = "",
                   issue: dict | None = None) -> dict:
    """Досдать задачу за сессию, если она этого не сделала.

    Шаги 11-12 промпта (финальный комментарий + передача) — самое важное в
    сессии: MR человек ещё найдёт, а не сданную задачу не найдёт никто.
    Инструкцией это уже пробовали удержать, и дважды не удержалось: 31.08
    TASK-176 сессия сгорела до шага 11, 03.09 TASK-204 сессия написала в свой
    лог «переведено в In Review», хотя ни комментария, ни статуса в Linear не
    появилось — и работа с готовым MR пролежала незамеченной.

    Поэтому раннер больше не верит на слово, а проверяет факт и дописывает
    сам. Комментарий помечен, что его оставил раннер: человек должен видеть
    разницу между «сессия отчиталась» и «за сессию досдали».

    Сдачей считается то, что велит контракт (DEV-78, DEV-90), а не один
    статус приёмки: работа с кодом уходит в `VERIFYING_STATE_NAME` БЕЗ смены
    исполнителя (назначение — команда «начни работу»); исходы без кода
    закрывают задачу. До 15.09.2026 здесь ждали только `REVIEW_STATE_NAME`
    на принимающем, и каждая сессия, выполнившая контракт, «досдавалась»
    следом: за ночь 14.09 десять задач уехали из `Verifying` человеку до
    проверки — ровно то, от чего DEV-78 уводил. Статус приёмки остаётся
    законным финалом (ручной путь) и остаётся целью досдачи: сессия, не
    сдавшая ни так, ни так, отдаётся человеку, а не на проверку, — ветки у
    неё может и не быть, и ворота проверки оставили бы задачу висеть.

    С DEV-95 из `run_session` сюда приходят только сессии, у которых схема
    сдачи сошлась (`handoff_misses` пуст), — тогда это no-op со снимком; не
    сошлась — сессию возвращают (`repair_handoff`), а не дописывают. Досдача
    остаётся для прямых вызовов и для очереди, положенной раннером прошлой
    версии. `issue` — уже прочитанный снимок, чтобы не ходить в трекер дважды.

    Возвращает снимок задачи ДО правок (пустой — не прочитан): по нему
    `publish_review` отличает исход без кода от исхода без ревью.
    """
    issue = issue or _handoff_state(issue_id)
    if not issue:
        log.warning("%s: не смог прочитать задачу в Linear - сдачу не проверил", ident)
        return {}

    fresh = _fresh_comments(issue, since)
    state = (issue.get("state") or {}).get("name") or ""
    assignee = ((issue.get("assignee") or {}).get("id")) or ""
    handed = _handed(issue, reviewer)
    if fresh and handed:
        return issue

    log.warning("%s: сессия не сдала задачу (комментарий=%s, статус=%r, на=%s) "
                "- досдаю за неё", ident, bool(fresh), state, assignee or "никого")

    if not fresh:
        body = _runner_authored_comment(
            "Сессия завершилась штатно, но финального комментария в задаче "
            "не оставила - значит шаги 11-12 промпта не выполнены, и отчёт "
            "ниже взят из хвоста её лога как есть. Он может обрываться на "
            "полуслове и не заменяет отчёт: проверь MR глазами.",
            log_path,
        )
        linear_comment(issue_id, body, persona)
    if handed:
        # Статус по контракту, не хватало только отчёта — он дописан выше.
        # Трогать состояние нельзя: из `Verifying` задачу уводит проверка.
        return issue

    _move_to_reviewer(issue, issue_id, ident, reviewer)
    return issue


def handoff_misses(issue: dict, workdir: Path, since: datetime, reviewer: dict,
                   *, require_transition: bool = True,
                   require_plan: bool = True) -> list[str]:
    """Чего не хватает в сдаче рабочей сессии. Пусто — сдала.

    Требование одно: сказать человеку в задаче. 17.09.2026 отсюда сняты файл
    `handoff.md` с четырьмя разделами, шапка по схеме и `plan.md` со шлюзом
    тяжести — люди пишут в трекер, а не в файл рядом с кодом, и файл
    существовал затем, чтобы его проверял линт.

    Перевод статуса тоже не спрашиваем: проверки нет, передавать некому.

    `require_transition` и `require_plan` остались в подписи ради вызывающих
    и ни на что не влияют; убирать их — отдельная правка по всем местам.
    """
    # 17.09.2026: от сдачи осталось одно требование — сказать человеку в
    # задаче. Файл `handoff.md` с четырьмя разделами, шапка по схеме и
    # `plan.md` со шлюзом тяжести убраны: люди пишут в трекер, а не в файл
    # рядом с кодом, и файл существовал только затем, чтобы его проверял
    # линт. Перевод статуса тоже не спрашиваем — проверки нет, передавать
    # некому.
    misses: list[str] = []
    if not _fresh_comments(issue, since):
        misses.append("в задаче нет ни слова о том, что сделано — скажи "
                      "двумя-тремя фразами, что вышло и нужно ли что-то от человека")
    return misses


def _handoff_since(task: dict, started_at: datetime) -> datetime:
    """Отсечка «комментарий после старта» — старт ПЕРВОГО захода, не
    ремонтного: отчёт, положенный в первом заходе, в ремонтном не
    повторяется, и считать от нового старта значило бы гонять сессию по
    кругу за комментарием, который уже есть."""
    raw = task.get("handoff_since") or ""
    try:
        return datetime.fromisoformat(raw) if raw else started_at
    except ValueError:
        return started_at


def repair_handoff(task: dict, workdir: Path, slot: int, ident: str, issue_id: str,
                   issue: dict, misses: list[str], since: datetime, reviewer: dict,
                   persona: str, log_path: Path) -> None:
    """Сдача не по схеме: вернуть сессии список недостающего и продолжить её
    же — или, на потолке, отдать задачу человеку. За сессию не дописывать.

    Ремонт — та же механика, что продолжение после лимита: задача кладётся
    в очередь с `resume_session_id`/`resume_workdir`, и `run_session` поднимает
    ту же сессию в том же каталоге, только с промптом «чего не хватило»
    (`build_repair_prompt`), а не «лимит сброшен». Счётчик свой
    (`handoff_attempt`), не `resume_attempt`: потолок лимита про квоту, этот
    — про сдачу, и складывать их значило бы отнимать у сессии продолжение
    после лимита за то, что она один раз забыла файл.

    Потолок (`MAX_HANDOFF_ATTEMPTS`) считает неудачные сдачи подряд, включая
    первую. Дальше — человеку: помеченный комментарий, чего не хватило, и
    статус приёмки на принимающем. Некуда продолжать (нет id сессии в
    транскрипте, очередь не отвечает) — тот же исход сразу: потерять задачу
    хуже, чем показать её человеку раньше времени.
    """
    failures = int(task.get("handoff_attempt") or 0) + 1
    sid = _session_id(workdir)
    if failures < MAX_HANDOFF_ATTEMPTS and sid:
        again = dict(task, handoff_attempt=failures, handoff_misses=misses,
                     handoff_since=since.isoformat(),
                     resume_session_id=sid, resume_workdir=str(workdir))
        try:
            redis.from_url(REDIS_URL).lpush(QUEUE_KEY, json.dumps(again))
            queued = True
        except Exception as e:
            log.warning("не вернуть сессию на досдачу: %s", e)
            queued = False
        if queued:
            log.warning("[slot %d] %s → сдача не по схеме (неудачная сдача %d из %d): %s "
                        "— возвращаю сессии, не дописываю", slot, ident, failures,
                        MAX_HANDOFF_ATTEMPTS, "; ".join(misses))
            return
    why = ("" if failures >= MAX_HANDOFF_ATTEMPTS else
           " (продолжить некуда: нет id сессии в транскрипте)" if not sid else
           " (очередь не приняла продолжение)")
    log.error("[slot %d] %s → сдача не по схеме, неудачная сдача %d из %d%s: %s — "
              "задача уходит человеку", slot, ident, failures, MAX_HANDOFF_ATTEMPTS,
              why, "; ".join(misses))
    text, _ = handoff.handoff_of(workdir)
    tail = _log_tail(log_path, lines=40)
    linear_comment(
        issue_id,
        f"🤖 **Сдача не по схеме — задача передана человеку.** Сессия "
        f"закончилась без сдачи по схеме {failures} раз(а) из {MAX_HANDOFF_ATTEMPTS}"
        f"{why}. Чего не хватило в последний раз:\n"
        + "".join(f"- {m}\n" for m in misses)
        + "\nРаннер за сессию не дописывает: MR, если он есть, смотри глазами. "
        + (f"Что сессия оставила в `{handoff.HANDOFF_FILE}`:\n\n{text}\n\n" if text else "")
        + f"Лог на хосте: `{log_path}`"
        + (f"\n\n```\n{tail}\n```" if tail else ""),
        persona,
    )
    _move_to_reviewer(issue, issue_id, ident, reviewer)
    notify(f"⚠️ {ident}: сдача не по схеме {failures} раз(а) — задача у человека"
           f"\n{task.get('url', '')}")


def ensure_verify_handoff(issue_id: str, ident: str, log_path: Path,
                          since: datetime, verdict: str, persona: str = "") -> None:
    """Досдать отчёт проверки за сессию, если она его не оставила.

    Тот же класс молчания, что у рабочей сессии (TASK-176, TASK-204), на
    проверочном пути был открыт: `report_verdict` шлёт вердикт в чат, а
    комментарий в задаче — единственное место, где человек читает, ЧТО
    именно сверялось, — никто не проверял. По трассам client-b 12–14.09.2026
    семь проверок из 66 оставили комментарий без перевода задачи, а
    DEV-78 и DEV-80 — вердикт без единого слова в задаче (DEV-90).

    Здесь — только комментарий. Состояние и исполнителя по вердикту сверяет
    `ensure_verdict_route` (DEV-96), и он же правит расхождение; дублировать
    это тут значило бы завести второе правило маршрута. Порядок в
    `run_session`: сперва отчёт, потом маршрут — как «досдача, потом ревью»
    у рабочей сессии.
    """
    issue = _handoff_state(issue_id)
    if not issue:
        log.warning("%s: не смог прочитать задачу в Linear - отчёт проверки не проверил", ident)
        return
    fresh = _fresh_comments(issue, since)
    if fresh:
        return
    log.warning("%s: проверка не оставила комментария в задаче (вердикт=%r) "
                "- досдаю за неё", ident, verdict[:80])
    explanation = (
        "Проверка завершилась, но комментария в задаче не оставила (шаги 7-8 "
        "контракта) - " +
        (f"вердикт из её `verdict.txt`: «{verdict}». "
         if verdict else
         "и вердикта (`verdict.txt`) тоже нет: чем кончилась проверка, "
         "неизвестно. ") +
        "Состояние и исполнителя раннер сверяет с вердиктом отдельно "
        "(маршрут по вердикту). Отчёт ниже взят из хвоста её лога как есть - "
        "он может обрываться на полуслове."
    )
    linear_comment(issue_id, _runner_authored_comment(explanation, log_path), persona)


# Как назвать место в промахах шапки у дежурного: он сдаёт не файлом, а
# комментарием в задаче — и возврат должен звать это тем же словом, что
# контракт (шаг 8 INCIDENT_TEMPLATE).
INCIDENT_SUMMARY = "сводке дежурного (шаг 8)"


def incident_headline_misses(comments: list[dict], since: datetime) -> list[str]:
    """Чего не хватает шапке в сводке дежурного (DEV-108). Пусто — шапка есть.

    Пятая роль доехала до шапки последней, и механизм у неё свой: дежурный
    не пишет `handoff.md`, поэтому `headline.lint_file` ему нечего читать —
    его сдача это комментарий.

    Проверяется НЕ один назначенный комментарий, а есть ли шапка хоть у
    одного из оставленных после старта сессии. Дежурный по шагу 6 пишет
    комментарий на КАЖДОЕ действие, меняющее прод («перезапустил то-то,
    получил то-то»), и шапки у них нет по замыслу: требовать её от каждого
    следа значило бы ругать сессию ровно за то, что контракт ей велит.
    Сводка (шаг 8) — единственный комментарий, который обязан открываться
    шапкой, и поиск «хоть один» находит её, где бы в ленте она ни стояла.

    Слова промаха берутся у ПОСЛЕДНЕГО комментария: в штатном ходе шаг 8
    идёт после шага 6, и последний — это и есть сводка. Ни одного
    комментария после старта — пусто, а не промах: молчание сессии ловит
    `ensure_incident_handoff` отдельно, и называть его дважды значило бы
    вернуть два промаха на одно действие.
    """
    fresh = _fresh_nodes(comments, since)
    misses: list[str] = []
    for c in fresh:
        misses = headline.lint_text(str(c.get("body") or ""), INCIDENT_SUMMARY)
        if not misses:
            return []
    return misses


def note_incident_headline(issue_id: str, ident: str, since: datetime,
                           persona: str = "") -> bool:
    """Сказать в задаче, что у сводки дежурного нет шапки. True — сказал.

    Вернуть дежурную сессию за шапкой некуда: инцидент к этому времени уже
    отработан, прод восстановлен, и поднимать её заново ради подачи значило
    бы платить сессией за оформление. Выбор поэтому тот же, что у ревью
    (`_review_headline`): промолчать или назвать вслух. Молчать нельзя —
    неотмеченный пропуск неотличим от сделанного, и пятая роль тихо
    осталась бы без шапки ровно так же, как оставалась до сих пор.

    Комментарии читаются отдельным запросом, а не из снимка задачи:
    `fetch_issue_snapshot` отдаёт у комментариев только `createdAt`, без
    текста, — линтовать там нечего. Тот же метод, которым собирается лента
    заходов.

    Осечка трекера ничего не роняет: это подача, а не сдача. Инцидент уже
    отработан, и потерять его из-за неотправленного замечания хуже, чем
    остаться без замечания.
    """
    try:
        misses = incident_headline_misses(_tracker.list_comments(issue_id), since)
    except Exception as e:
        log.warning("%s: не прочитать комментарии — шапку сводки не проверил: %s",
                    ident, e)
        return False
    if not misses:
        return False
    log.warning("%s: в сводке дежурного шапки нет (%s) — говорю об этом в задаче",
                ident, "; ".join(misses)[:200])
    try:
        linear_comment(
            issue_id,
            "> У сводки дежурного нет шапки: двумя-тремя предложениями не "
            "сказано, работает ли оно сейчас, как тебе убедиться в этом "
            "самому и нужен ли ты. Ответ есть, но он внутри разбора — "
            "разбираться придётся по всему тексту.",
            persona)
    except Exception as e:
        log.warning("%s: замечание про шапку сводки не ушло в задачу: %s", ident, e)
        return False
    return True


def ensure_incident_handoff(issue_id: str, ident: str, log_path: Path,
                            reviewer: dict, since: datetime,
                            persona: str = "") -> None:
    """Досдать эскалацию за дежурного, если он не передал её ревьюеру.

    Проверка по факту комментария (а не по assignee) держалась ровно до
    шага 2 INCIDENT_TEMPLATE ("отметься, что взялся") — тот пишет комментарий
    почти сразу после старта, и сессия, упавшая посреди диагностики (шаг 4/6),
    выглядела бы сданной только потому, что оставила след в самом начале.
    Шаг 10 контракта теперь всегда переводит {identifier} на {reviewer} —
    один детерминированный финал, как у `ensure_handoff`, просто без
    требования к статусу (дежурный не открывает MR, статус приёмки ему не
    обязателен). Проверяем именно это, а не факт комментария.

    Отсутствие передачи — тот же класс молчания, что уже ловили дважды на
    обычной работе (TASK-176, TASK-204) и ради чего завели `ensure_handoff`:
    сессия отчиталась (или нет), но задача осталась висеть на агенте, у
    которого нет входящих.
    """
    issue = _handoff_state(issue_id)
    if not issue:
        log.warning("%s: не смог прочитать задачу в Linear - сдачу дежурного не проверил", ident)
        return
    # Шапка сводки (DEV-108) — ДО развилки о передаче: сводка бывает написана
    # и у сессии, которая задачу передала, и у той, которая забыла. Спрятать
    # проверку за `return` ниже значило бы проверять шапку только у
    # молчаливых, то есть почти никогда.
    note_incident_headline(issue_id, ident, since, persona)
    assignee = ((issue.get("assignee") or {}).get("id")) or ""
    if reviewer.get("id") and assignee == reviewer["id"]:
        return
    log.warning("%s: дежурная сессия не передала задачу ревьюеру (на=%s) - досдаю за неё",
                ident, assignee or "никого")
    fresh = _fresh_comments(issue, since)
    explanation = (
        "Дежурная сессия завершилась штатно, но не передала задачу "
        "ревьюеру (шаг 10 контракта) - " +
        ("след в комментариях есть, но неизвестно, чем всё закончилось."
         if fresh else
         "не оставила НИ ОДНОГО комментария после отметки о взятии в "
         "работу - неизвестно, что она нашла и что сделала.") +
        " Отчёт ниже взят из хвоста её лога как есть - он может "
        "обрываться на полуслове."
    )
    body = _runner_authored_comment(explanation, log_path)
    linear_comment(issue_id, body, persona)
    linear_assign_to_reviewer(issue_id, reviewer)


def _fmt_tool(block: dict) -> str:
    """Одна строка про вызов инструмента: имя + самое важное из аргументов."""
    name = block.get("name") or "?"
    inp = block.get("input") or {}
    for key in ("command", "file_path", "pattern", "query", "url", "prompt", "id"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            brief = " ".join(val.split())
            return f"{name}: {brief[:160]}"
    return name


def _pump_stream(stream, logfile, transcript_path, started: float,
                 breaker: RepeatBreaker | None = None) -> None:
    """Разбирает stream-json сессии: сырьё в transcript.jsonl, ход — в session.log.

    `breaker` — прерыватель (DEV-91): ему уходит каждый вызов инструмента с
    веткой (`parent_tool_use_id`, у главной сессии пусто). Здесь, а не
    отдельным проходом: транскрипт и так читается по строке, второй читатель
    того же потока — второй источник правды о том, что сессия вызывала.

    Без этого `claude -p` отдаёт только финальный текст и только в конце:
    сессия на 40 минут выглядит чёрным ящиком, `tail -f` показывать нечего, а
    разобрать постфактум «вызывала ли она такой-то инструмент» вообще нечем.
    03.09 на TASK-204 ровно это и не дало отличить «Linear не ответил» от
    «сессия написала, что перевела задачу, не переведя».

    Каждая строка помечена временем от старта — по логу видно, на что ушли
    минуты, а не только сколько их всего.
    """
    def stamp() -> str:
        sec = int(time.monotonic() - started)
        return f"[{sec // 60:02d}:{sec % 60:02d}]"

    with transcript_path.open("w") as raw:
        for line in stream:
            text = line.decode(errors="replace") if isinstance(line, bytes) else line
            raw.write(text)
            raw.flush()
            try:
                ev = json.loads(text)
            except (ValueError, TypeError):
                # Не JSON — почти наверняка stderr сессии; он тоже важен.
                if text.strip():
                    logfile.write(text if text.endswith(chr(10)) else text + chr(10))
                continue
            kind = ev.get("type")
            if kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        logfile.write(f"{stamp()} {block['text'].rstrip()}" + chr(10))
                    elif block.get("type") == "tool_use":
                        logfile.write(f"{stamp()} -> {_fmt_tool(block)}" + chr(10))
                        if breaker is not None:
                            breaker.feed(ev.get("parent_tool_use_id"),
                                         block.get("name") or "", block.get("input"))
            elif kind == "system" and ev.get("subtype") == "post_turn_summary":
                detail = (ev.get("status_detail") or "").strip()
                if detail:
                    logfile.write(f"{stamp()} . {detail}" + chr(10))
            logfile.flush()


def _log_tail(log_path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(log_path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _limit_reset(tail: str) -> str | None:
    """Хвост лога → момент сброса лимита.

    None — лимит ни при чём. "" — лимит исчерпан, а когда сбросится, в логе не
    сказано. Три состояния, а не два, потому что «повторять бесполезно» и
    «повторять бесполезно до 10:10pm» — разные сообщения человеку, но оба не
    равны «сессия просто упала».
    """
    # Время сброса ищем в той же строке, где сказано про лимит: в двадцати
    # строках хвоста «resets» может оказаться и от чужого сообщения, и тогда
    # человек получил бы уверенное, но чужое время.
    for line in tail.splitlines():
        if not any(m in line.lower() for m in _LIMIT_MARKERS):
            continue
        m = _RESET_RE.search(line)
        return m.group(1).strip(" .;,") if m else ""
    return None


# Только АБСОЛЮТНОЕ время: либо с am/pm, либо с минутами через двоеточие.
# Голое число разбирать нельзя — «resets in 5 hours» превратилось бы в «в
# 05:00», то есть в возврат втрое раньше сброса и сожжённую попытку. А если
# такой час уже прошёл, задача уснула бы на сутки: проверка MAX_DEFER_SEC
# стоит ровно на этой границе и такой случай пропустит.
# Формулировка у CLI меняется (см. _RESET_RE), поэтому всё незнакомое обязано
# уходить в None: ждать вслепую час безопаснее, чем догадываться.
_RESET_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?|(\d{1,2})\s*(am|pm)", re.I)


def _reset_at(hint: str, now: float | None = None) -> float | None:
    """«4:20am (UTC)» → момент сброса эпохой. None — разобрать не вышло.

    Разбираем то, что `_limit_reset` вернул хвостом строки. Час без даты
    всегда трактуем как БЛИЖАЙШИЙ будущий: CLI печатает время сброса, а не
    прошедшее, и «4:20am» в 03:07 — это через час с небольшим, а не вчера.

    Часовой пояс берём из самой строки: CLI пишет «(UTC)». Если не написал —
    считаем локальным временем хоста. Ошибиться поясом дороже, чем подождать
    лишнего, поэтому неразобранное отдаём как None и ждём вслепую.
    """
    if not hint:
        return None
    m = _RESET_TIME_RE.search(hint)
    if not m:
        return None
    if m.group(1) is not None:          # HH:MM, am/pm необязателен
        hour, minute = int(m.group(1)), int(m.group(2))
        ampm = (m.group(3) or "").lower()
    else:                                # H am/pm, без минут
        hour, minute = int(m.group(4)), 0
        ampm = m.group(5).lower()
    if hour > 23 or minute > 59:
        return None
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    now = now if now is not None else time.time()
    utc = "utc" in hint.lower()
    base = datetime.fromtimestamp(now, timezone.utc if utc else None)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    at = target.timestamp()
    if at <= now:
        at += 24 * 3600
    return at


def _session_id(workdir: Path) -> str | None:
    """ID сессии из её же транскрипта — им продолжают через `--resume`.

    Раннер сам ID не назначает: его выдаёт CLI и печатает в каждом событии
    stream-json, которые `_pump_stream` кладёт в transcript.jsonl. Читаем
    оттуда, а не из `~/.claude/projects/`: там имя файла зависит от того, как
    CLI закодировал путь, и это его внутреннее дело, а не наш контракт.
    """
    # Транскрипт текущего захода, а если он пуст — сохранённые предыдущие.
    # Продолжение, умершее мгновенно (лимит кончился сразу), не успевает
    # выдать ни одного события: без этого отката ID взять неоткуда, и задача
    # ушла бы человеку вместо следующей попытки — молча, без следа в логах.
    # Свежие раньше старых: ID один и тот же, но искать быстрее там, где он
    # почти всегда есть.
    saved = sorted(workdir.glob("transcript.jsonl.*"),
                   key=lambda f: f.name, reverse=True)
    for path in [workdir / "transcript.jsonl", *saved]:
        try:
            with path.open(errors="replace") as f:
                for line in f:
                    try:
                        sid = json.loads(line).get("session_id")
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if sid:
                        return sid
        except OSError:
            continue
    return None


def _defer(task: dict, resume_at: float) -> bool:
    """Отложить задачу до момента сброса. True — получилось.

    ZSET, а не список: разбирать надо по времени, а не по порядку укладки, и
    подметальщику нужно уметь спросить «что уже созрело», не вычитывая всё.
    """
    try:
        client = redis.from_url(REDIS_URL)
        # Разносим возобновления, если на то же время уже кто-то отложен:
        # одновременный старт двух тяжёлых сессий и есть механизм, который
        # выедает квоту.
        near = client.zcount(DEFERRED_KEY, resume_at - RESUME_STAGGER_SEC,
                             resume_at + RESUME_STAGGER_SEC)
        resume_at += near * RESUME_STAGGER_SEC
        client.zadd(DEFERRED_KEY, {json.dumps(task): resume_at})
        return True
    except Exception as e:
        log.warning("не отложить задачу до сброса лимита: %s", e)
        return False


def _release_deferred(client: redis.Redis, now: float | None = None) -> int:
    """Созревшие отложенные задачи → в очередь. Возвращает сколько вернул.

    Забираем через ZREM и кладём в очередь, только если ZREM подтвердил, что
    забрали именно мы: слотов несколько, и без этого одну задачу подняли бы
    двое, получив две сессии на один тикет.
    """
    now = now if now is not None else time.time()
    moved = 0
    try:
        for raw in client.zrangebyscore(DEFERRED_KEY, "-inf", now):
            if not client.zrem(DEFERRED_KEY, raw):
                continue
            client.lpush(QUEUE_KEY, raw)
            moved += 1
    except redis.RedisError as e:
        log.warning("отложенная очередь недоступна: %s", e)
    return moved


def _log_hint(tail: str, limit: int = 200) -> str:
    """Последняя непустая строка лога — та самая причина, ради которой человек
    иначе пошёл бы открывать session.log на хосте."""
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if line:
            return line[:limit]
    return ""


# Сбой инфраструктуры, а не задачи: работа не начиналась по существу, и
# отдавать её человеку не за чем — тот же запуск через минуту пройдёт сам.
#
# Перенесено из старой (досабмодульной) копии раннера — там сессия падала на
# `API Error: 529 Overloaded` через семь минут после старта и была
# переназначена на человека РОВНО ТАК ЖЕ, как выглядит успешная сдача:
# переназначение снаружи неотличимо от «работа готова», задача просто лежит у
# принимающего. Простояла так три часа, пока человек не спросил сам.
#
# Исчерпанный лимит (`_LIMIT_MARKERS` выше) сюда НЕ входит и остаётся на пути
# к человеку: до сброса повтор упадёт так же, и возвраты только жгли бы слоты.
#
# Повторять ВНУТРИ сессии нельзя — она держала бы слот и могла зависнуть. Но к
# этому моменту сессия уже мертва и слот свободен, поэтому «повтор» здесь —
# просто возврат задачи в очередь: ноль удерживаемых ресурсов, пока она ждёт.
_INFRA_MARKERS = ("529", "overloaded", "internal server error", "502 bad gateway",
                  "503 service unavailable", "connection reset by peer")

# Потолок на возвраты. Без него задача, падающая по своей причине и похоже
# сформулированной ошибкой, крутилась бы в очереди вечно, сжигая слоты.
MAX_REQUEUE_ATTEMPTS = int(os.environ.get("AGENT_MAX_REQUEUE_ATTEMPTS", "2"))

# Исчерпанный лимит — единственная причина обрыва, у которой известен СРОК
# годности: до сброса повтор упадёт так же, после сброса пройдёт. Поэтому
# задача не возвращается в общую очередь (воркер поднял бы её сразу и сжёг
# слот об тот же лимит), а кладётся в отложенную — с временем, когда её
# можно трогать.
#
# 08.09.2026 это стоило восьми часов простоя: две сессии умерли в 03:07 об
# общий счётчик квоты, сброс был в 04:20, а вернулись к ним руками в 12:10.
# Работа при этом уцелела — транскрипты на месте, и доигранные `--resume`
# сессии довели обе задачи до MR, не начиная заново.
DEFERRED_KEY = os.environ.get("AGENT_DEFERRED_KEY", "agent:deferred")
# Потолок на продолжения. Лимит может кончиться и сразу после возобновления
# (соседний слот выел остаток) — без потолка задача ходила бы по кругу.
MAX_RESUME_ATTEMPTS = int(os.environ.get("AGENT_MAX_RESUME_ATTEMPTS", "3"))
# CLI не всегда называет время сброса. Ждать вслепую час — компромисс: окно
# квоты у Claude короче суток, а лишний холостой заход стоит одного слота на
# минуту, тогда как преждевременный — ещё одной сожжённой сессии.
LIMIT_BLIND_WAIT_SEC = int(os.environ.get("AGENT_LIMIT_BLIND_WAIT_SEC", str(60 * 60)))
# Разносим одновременные возобновления. Именно параллельный старт двух тяжёлых
# слотов и выел квоту 08.09: обе сессии умерли в одну секунду. Если вернуть их
# тоже одновременно, повторится ровно то же.
RESUME_STAGGER_SEC = int(os.environ.get("AGENT_RESUME_STAGGER_SEC", "600"))
# Ниже здравого смысла: сброс дальше суток означает, что мы неверно разобрали
# время, а не что ждать сутки. Тогда честнее отдать задачу человеку.
MAX_DEFER_SEC = 24 * 3600

# ── ожидание внешних проверок (DEV-16) ─────────────────────────────────────
# Смысл — в ciwait.py. Запись ожидания живёт в ДВУХ местах: в redis (ZSET
# расписания + HASH записей) и на диске рядом с заявкой (`ci-wait-state.json`).
# Redis на client-b — снимки раз в 5-15 минут без AOF: перезагрузка хоста
# теряет свежие ключи, и без второй копии ожидание пропадало бы молча.
# Отдельный ZSET, а не отложенная очередь: там записи ждут часов и
# возвращаются в работу, здесь — опрашиваются каждые несколько минут и в
# работу чаще всего не возвращаются вовсе.
CI_WAIT_KEY = os.environ.get("AGENT_CI_WAIT_KEY", "agent:ci-wait")
CI_WAIT_ENTRIES_KEY = CI_WAIT_KEY + ":entries"
# Как часто спрашивать форж. Две минуты — компромисс между «узнать поскорее»
# и «не устроить себе лимит запросов»: опрос стоит два subprocess без сессии.
CI_WAIT_POLL_SEC = int(os.environ.get("AGENT_CI_WAIT_POLL_SEC", "120"))
# Сколько всего ждём. 90 минут, а не час: полный прогон на большом проекте
# идёт 30-60 минут, и срок ожидания обязан быть НЕ РАВЕН сроку сессии — иначе
# мы возвращаем ровно ту беду, от которой ушли (час модели, смешанный с часом
# чужой системы). Ожидание при этом не стоит ни слота, ни памяти.
CI_WAIT_MAX_SEC = int(os.environ.get("AGENT_CI_WAIT_MAX_SEC", str(90 * 60)))
# Сколько ждать появления прогона. Пайплайн заводится не мгновенно, но не
# четверть часа: не появился — CI по ветке не запускается или заявка не про
# тот коммит, и держать задачу полтора часа ради этого незачем.
CI_WAIT_APPEAR_SEC = int(os.environ.get("AGENT_CI_WAIT_APPEAR_SEC", str(15 * 60)))
# Сколько ждать сам опрос. Висящий `gh` держал бы подметальщика, а он же
# возвращает задачи после сброса лимита Claude.
CI_POLL_TIMEOUT_SEC = int(os.environ.get("AGENT_CI_POLL_TIMEOUT_SEC", "60"))
# Раз в сколько говорить наружу «всё ещё ждём»: комментарий в задачу и строка
# в канал. Исходный случай DEV-16 — 40 минут без единого сигнала.
CI_WAIT_HEARTBEAT_SEC = int(os.environ.get("AGENT_CI_WAIT_HEARTBEAT_SEC", str(30 * 60)))
# Раз в сколько сверять redis с диском: потерянный ключ замечается не позже
# этого срока, а не «когда задача не вернулась».
CI_WAIT_RECONCILE_SEC = int(os.environ.get("AGENT_CI_WAIT_RECONCILE_SEC", str(10 * 60)))
# Сколько продолжению после красных проверок дают, чтобы подняться. Пока оно
# в очереди — срок продлевается; исчезло из очереди и не поднялось — потеряно.
CI_WAKE_MAX_SEC = int(os.environ.get("AGENT_CI_WAKE_MAX_SEC", str(2 * 3600)))
# Сколько раз возвращаться к сессии с упавшими проверками. Больше двух —
# признак, что чинить должен человек, а не круг из сессий по одному месту.
MAX_CI_FIX_ATTEMPTS = int(os.environ.get("AGENT_MAX_CI_FIX_ATTEMPTS", "2"))


# ── ожидание внешних проверок: то, что ходит наружу (DEV-16) ───────────────
# Чистая логика — в ciwait.py. Здесь: redis, форж, трекер, очередь. Правило
# одно на все ветки: ожидание никогда не кончается молча. Каждый терминал —
# комментарий в задаче и строка в канале; каждая потеря — тоже.


def _ci_conn():
    """Своё подключение: сюда приходят из `run_session`, где клиента под рукой
    нет, а стоимость одного коннекта на сдачу ничтожна — ровно как у `_defer`."""
    return redis.from_url(REDIS_URL, decode_responses=True)


def _ci_request(workdir: Path, issue_id: str, ident: str, persona: str) -> dict | None:
    """Заявка сессии на ожидание проверок. None — сессия ничего не просила.

    Заявка одноразовая — файл снимается при чтении, годная она или нет.
    Продолжение после красных проверок идёт в ТОТ ЖЕ каталог: не сними мы
    её здесь, сессия, не переписавшая файл, была бы опрошена по СТАРОМУ sha —
    то есть снова получила бы уже известный красный и сожгла бы на этом все
    возвраты.

    Битая заявка НЕ игнорируется молча: сессия в этом случае уверена, что за
    неё досмотрят, и молчаливый пропуск вернул бы ровно ту беду, ради которой
    механизм заведён.
    """
    try:
        req = ciwait.read_request(workdir)
    except ciwait.BadRequest as e:
        log.error("%s: заявка на ожидание проверок негодна (%s) — "
                  "сдаю задачу обычным порядком", ident, e)
        if issue_id:
            linear_comment(
                issue_id,
                f"🤖 Сессия оставила заявку на ожидание проверок, но раннер "
                f"не смог её прочитать: {e}.\n\nЗадача сдаётся как есть — "
                f"состояние проверок никто не досмотрел, посмотри их сам.",
                persona)
        req = None
    try:
        (workdir / ciwait.REQUEST_FILE).unlink(missing_ok=True)
    except OSError:
        pass
    return req


def _ci_entry_of(client, ident: str) -> dict | None:
    """Запись ожидания по задаче из redis. None — нет или не читается."""
    raw = client.hget(CI_WAIT_ENTRIES_KEY, ident)
    if not raw:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        log.error("%s: запись ожидания в redis не разобрана: %r", ident, raw[:200])
        return None
    return entry if isinstance(entry, dict) else None


def _ci_member(ident: str, gen: str) -> str:
    """Участник расписания: задача И поколение записи. Захват через ZREM
    снимает участника, не глядя на счёт, — второй раннер со списком, прочитанным
    до того, как первый вернул запись в расписание с новым сроком, снял бы её
    и обработал сам. Поколение меняется при каждом сохранении, и устаревший
    ZREM возвращает 0: захватить можно только то, что читал."""
    return f"{ident}|{gen}"


def _ci_split(member: str) -> tuple[str, str]:
    ident, _, gen = str(member).rpartition("|")
    return (ident, gen) if ident else (str(member), "")


def _ci_store(client, entry: dict, next_at: float, now: float) -> None:
    """Запись — в обе копии. Диск ПЕРВЫМ: если redis сейчас лежит, копия на
    диске — единственное, по чему ожидание восстановят (`_ci_recover`)."""
    old = _ci_member(entry["ident"], entry["gen"]) if entry.get("gen") else ""
    entry["gen"] = uuid.uuid4().hex
    entry["next_poll_at"] = next_at
    if not ciwait.write_state(Path(entry["workdir"]), entry, now, CI_WAIT_MAX_SEC):
        log.warning("%s: копия ожидания на диск не записалась (каталог снят?)",
                    entry.get("ident"))
    client.hset(CI_WAIT_ENTRIES_KEY, entry["ident"], json.dumps(entry, ensure_ascii=False))
    if old:
        client.zrem(CI_WAIT_KEY, old)
    client.zadd(CI_WAIT_KEY, {_ci_member(entry["ident"], entry["gen"]): next_at})


def _ci_drop(client, entry: dict, state: str, detail: str, now: float) -> None:
    """Терминал: запись снята из redis, на диске остался итог с состоянием."""
    entry["state"] = state
    entry["detail"] = detail
    entry["finished_at"] = now
    ciwait.write_state(Path(entry["workdir"]), entry, now, CI_WAIT_MAX_SEC)
    try:
        client.hdel(CI_WAIT_ENTRIES_KEY, entry["ident"])
        client.zrem(CI_WAIT_KEY, _ci_member(entry["ident"], str(entry.get("gen") or "")))
    except Exception as e:
        # Терминал на диске уже записан: сверка увидит нестроевую запись в
        # redis и снимет её сама.
        log.warning("%s: запись ожидания не снята из redis (%s) — снимет сверка",
                    entry.get("ident"), e)


def _ci_enter(task: dict, workdir: Path, log_path: Path, persona: str,
              req: dict, *, cut_off: bool, issue: dict | None = None,
              since: datetime | None = None, now: float | None = None) -> bool:
    """Положить задачу в ожидание проверок. Слот при этом уже свободен:
    сессия вышла, а ожидание живёт записью, которую трогает подметальщик.

    False — только если не записалось НИКУДА (ни redis, ни диск): тогда
    ждать некому, и задача сдаётся обычным порядком. Лежащий redis при целой
    копии на диске — не отказ: сверка вернёт запись, а человеку об этом
    сказано сразу.

    Схема сдачи (DEV-95) сверяется ЗДЕСЬ, до первого комментария раннера
    (иначе его же комментарий сошёл бы за отчёт сессии), но без требования
    перевода — перевод по заявке делает раннер. Промахи едут в запись и
    разбираются после зелёных проверок (`repair_handoff`): вернуть сессию на
    ремонт, пока ожидание идёт, значило бы получить два продолжения одной
    сессии — ремонтное и по красному прогону.
    """
    now = time.time() if now is None else now
    ident = task.get("identifier") or "?"
    issue_id = task.get("issue_id") or ""
    since = since or datetime.now(timezone.utc)
    # Шлюз тяжести спрашивается по роду захода, как и на обычной сдаче
    # (DEV-109): сессия разрешения конфликта `plan.md` не пишет, и записать
    # его в промахи здесь значило бы вернуть её на ремонт после зелёных
    # проверок — за файл, которого её контракт не просил.
    misses = (handoff_misses(issue, workdir, since, current_reviewer(),
                             require_transition=False,
                             require_plan=str(task.get("kind") or "").strip() != MERGE_KIND)
              if issue else [])
    entry = {
        "ident": ident, "task": task, "workdir": str(workdir),
        "log": str(log_path), "persona": persona, "req": req,
        "state": ciwait.WAITING, "detail": "ожидание начато",
        "started_at": now, "attempts": 0, "last_poll_at": 0.0,
        "seen_runs": [], "told_unreachable": False, "told_redis_lost": False,
        "last_heartbeat_at": now,
        "fix_attempt": int(task.get("ci_fix_attempt") or 0),
        "wake_id": "", "woke_at": 0.0,
        "since": since.isoformat(), "handoff_misses": misses,
    }
    redis_error = ""
    try:
        _ci_store(_ci_conn(), entry, now + CI_WAIT_POLL_SEC, now)
    except Exception as e:
        redis_error = str(e)
        if not (workdir / ciwait.STATE_FILE).is_file():
            log.error("%s: не положить задачу в ожидание проверок ни в redis (%s), "
                      "ни на диск", ident, e)
            return False
        log.error("%s: redis недоступен (%s) — ожидание записано только на диск, "
                  "сверка вернёт его в опрос", ident, e)
    where = ciwait.progress(entry, now, CI_WAIT_MAX_SEC)
    log.info("%s → ожидание проверок: %s; опрос раз в %dс", ident, where, CI_WAIT_POLL_SEC)
    if issue_id:
        linear_comment(
            issue_id,
            f"🤖 **Ожидание проверок.** {where}.\n\n"
            + ("Сессия не уложилась в срок, но заявку оставить успела — "
               "ожидание принято. " if cut_off else "")
            + (("Сдача пока не по схеме (" + "; ".join(misses) + ") — после "
                "зелёных проверок сессия вернётся досдать, раннер за неё не "
                "допишет. ") if misses else "")
            +
            f"Сессия модели закрыта — ожидание слот не занимает. Раннер "
            f"опрашивает форж раз в {CI_WAIT_POLL_SEC // 60} мин, каждые "
            f"{CI_WAIT_HEARTBEAT_SEC // 60} мин пишет сюда, что всё ещё ждёт, "
            f"и напишет, чем кончилось: зелено — задача уйдёт на проверку, "
            f"красно — та же сессия вернётся чинить, не дождались или "
            f"прогон пропал — задача уйдёт тебе с причиной. Пока такого "
            f"комментария нет, задача НЕ сдана."
            + (f"\n\n⚠️ Redis сейчас недоступен ({redis_error}): запись "
               f"ожидания лежит только на диске (`{ciwait.STATE_FILE}` в "
               f"рабочем каталоге), раннер вернёт её в опрос при ближайшей "
               f"сверке (раз в {CI_WAIT_RECONCILE_SEC // 60} мин). Если через "
               f"{CI_WAIT_MAX_SEC // 60} мин здесь нет исхода — посмотри "
               f"проверки сам." if redis_error else ""),
            persona)
    notify(f"{'⚠️' if redis_error else '⏳'} {ident}: {where}"
           + (" — redis недоступен, запись только на диске" if redis_error else "")
           + f"\n{task.get('url', '')}")
    return True


def _ci_run(cmd: list[str]) -> tuple[int | None, str, str, str]:
    """Один вызов CLI форжа: (код, stdout, stderr, почему не запустился)."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=CI_POLL_TIMEOUT_SEC)
    except FileNotFoundError:
        return None, "", "", f"нет команды {cmd[0]!r} на хосте раннера"
    except subprocess.TimeoutExpired:
        return None, "", "", f"{cmd[0]} не ответил за {CI_POLL_TIMEOUT_SEC}с"
    except OSError as e:
        return None, "", "", f"{cmd[0]} не запустился: {e}"
    return done.returncode, done.stdout or "", done.stderr or "", ""


def _ci_poll(req: dict) -> tuple[str, str, list[str], list[str]]:
    """Один опрос форжа: (состояние, деталь, id прогонов нашего коммита, чем
    спрашивали). Команду возвращаем наружу, чтобы она попала в продолжение
    сессии: сессия должна знать, ЧЕМ раннер увидел красный.

    Ненулевой код — это «не знаем», а не «красный»: так же выглядит протухший
    токен, и объявлять по нему падение сборки значит гонять сессию чинить то,
    что не ломалось. Исключение — «нет такого» (404): репозитория нет, ждать
    нечего.
    """
    cmd = ciwait.poll_command(req)
    rc, out, err, why = _ci_run(cmd)
    if why:
        return ciwait.UNREACHABLE, why, [], cmd
    if rc != 0:
        hint = _log_hint(err or out)
        if ciwait.is_not_found(err, out):
            return ciwait.NOT_FOUND, f"форж отвечает «нет такого»: {hint}", [], cmd
        return (ciwait.UNREACHABLE,
                f"{cmd[0]} вернул код {rc}" + (f": {hint}" if hint else ""), [], cmd)
    state, detail, ids = ciwait.parse_runs(req["forge"], out, req.get("sha", ""))
    return state, detail, ids, cmd


def _ci_probe_mr(req: dict) -> tuple[str, str]:
    """Жив ли MR (или ветка, если MR в заявке нет). Закрытый, влитый, не
    найденный MR и удалённая ветка — всё это конец ожидания: будить сессию
    на несуществующей ветке нельзя. Не достучались — MR_UNKNOWN, ждём дальше."""
    cmd = ciwait.mr_command(req)
    rc, out, err, why = _ci_run(cmd)
    if why:
        return ciwait.MR_UNKNOWN, why
    has_mr = bool(ciwait.mr_number(req))
    if rc != 0:
        hint = _log_hint(err or out)
        if ciwait.is_not_found(err, out):
            return ciwait.MR_CLOSED, (("MR не найден" if has_mr else
                                       f"ветки {req.get('branch')} больше нет")
                                      + (f": {hint}" if hint else ""))
        return ciwait.MR_UNKNOWN, f"{cmd[0]} вернул код {rc}" + (f": {hint}" if hint else "")
    return ciwait.parse_mr(req["forge"], out, is_branch_probe=not has_mr)


def _hand_to_verification(issue_id: str, ident: str) -> bool:
    """Шаг 12 контракта руками раннера: задача → на проверку, исполнитель тот же.

    Сессия, отдавшая ожидание проверок раннеру, шаг 12 НЕ делает намеренно:
    перевод в проверку поднимает проверяющего, а проверять красный прогон
    нечего. Значит перевести обязан тот, кто узнал исход, — то есть мы.
    Исполнителя не трогаем по той же причине, что и сессия: назначение на
    агента ловит вебхук и поднимает НОВУЮ рабочую сессию.

    False — перевести не вышло. Исход, который нельзя проглотить: задача
    осталась бы «в работе» без работающей сессии.
    """
    issue = _handoff_state(issue_id)
    if not issue:
        log.warning("%s: не смог прочитать задачу — на проверку не перевёл", ident)
        return False
    states = ((issue.get("team") or {}).get("states") or {}).get("nodes") or []
    state_id = next((st["id"] for st in states
                     if st.get("name") == VERIFYING_STATE_NAME), "")
    if not state_id:
        log.error("%s: в трекере нет статуса %r (AGENT_VERIFYING_STATE_NAME), "
                  "есть только: %s — на проверку не перевожу",
                  ident, VERIFYING_STATE_NAME,
                  ", ".join(repr(st.get("name") or "") for st in states) or "ни одного")
        return False
    if ((issue.get("state") or {}).get("name") or "") == VERIFYING_STATE_NAME:
        return True
    try:
        _tracker.update_issue(issue_id, {"stateId": state_id})
    except Exception as e:
        log.error("%s: перевод на проверку не прошёл: %s", ident, e)
        return False
    return True


def _ci_hand_to_human(entry: dict, state: str, detail: str, why: str,
                      now: float) -> None:
    """Ожидание кончилось не так, чтобы продолжать самим — отдать человеку.

    `why` пишет вызывающий: причин несколько (срок вышел, прогон пропал, MR
    закрыт, возвраты исчерпаны, продолжение не поднялось, зелено — но перевести
    не смогли), и общая формулировка на все объясняла бы не то, что случилось.
    """
    task = entry["task"]
    issue_id = task.get("issue_id") or ""
    ident = task.get("identifier") or "?"
    req = entry.get("req") or {}
    started = float(entry.get("started_at") or now)
    head = ciwait.describe(state, detail, req)
    if issue_id:
        linear_comment(
            issue_id,
            f"🤖 **{head}**\n\n{why}\n\n"
            f"Ждали с {ciwait.hhmm(started)}, {int((now - started) // 60)} мин, "
            f"опросов {int(entry.get('attempts') or 0)}. "
            f"Лог сессии на хосте: `{entry.get('log', '?')}`",
            entry.get("persona") or "")
        # Принимающий — тот, кто принимает СЕЙЧАС, а не на момент старта
        # ожидания: за полтора часа делегирование могло смениться.
        linear_assign_to_reviewer(issue_id)
    notify(f"⚠️ {ident}: {head} — задача у тебя\n{task.get('url', '')}")


def _ci_step(client, entry: dict, now: float) -> None:
    """Один шаг ожидания по одной задаче."""
    task = entry["task"]
    ident = task.get("identifier") or "?"
    issue_id = task.get("issue_id") or ""
    req = entry.get("req") or {}
    workdir = Path(entry["workdir"])
    persona = entry.get("persona") or ""
    started = float(entry.get("started_at") or now)

    if entry.get("state") == ciwait.WAKING:
        _ci_check_wake(client, entry, now)
        return
    if entry.get("state") != ciwait.WAITING:
        # Терминал, который не сняли (redis лежал в тот момент) — снимаем.
        log.warning("%s: в расписании ожидания запись в состоянии %r — снимаю",
                    ident, entry.get("state"))
        _ci_drop(client, entry, str(entry.get("state")), str(entry.get("detail") or ""), now)
        return

    # 1. Есть ли ещё то, чего ждём. Закрытый MR или удалённая ветка —
    #    ожидание снято, сессия НЕ будится: воскрешать нечего.
    mr_state, mr_detail = _ci_probe_mr(req)
    if mr_state in (ciwait.MR_CLOSED, ciwait.MR_MERGED):
        release_worktrees(workdir)
        _ci_drop(client, entry, ciwait.CLOSED, mr_detail, now)
        why = (("MR влит руками, пока раннер ждал проверок — ожидание снято, "
                "сессию не бужу. Проверка после мержа, если она нужна, "
                "поднимается своим путём.")
               if mr_state == ciwait.MR_MERGED else
               ("Ожидание снято, сессию по этой ветке не бужу — воскрешать "
                "нечего. Если работа ещё нужна, назначь задачу заново."))
        _ci_hand_to_human(entry, ciwait.CLOSED, mr_detail, why, now)
        return

    # 2. Сами проверки.
    raw, detail, ids, cmd = _ci_poll(req)
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["last_poll_at"] = now
    seen_before = bool(entry.get("seen_runs"))
    if ids:
        entry["seen_runs"] = sorted(set(entry.get("seen_runs") or []) | set(ids))[:50]
    if raw == ciwait.NOT_FOUND:
        state, why = raw, ""
    else:
        state, why = ciwait.decide(raw, waited_sec=now - started,
                                   max_wait_sec=CI_WAIT_MAX_SEC, found=bool(ids),
                                   seen_before=seen_before,
                                   appear_sec=CI_WAIT_APPEAR_SEC)
    if why:
        detail = f"{why} ({detail})" if detail else why
    log.info("%s: ожидание проверок — %s: %s; %s", ident, state, detail,
             ciwait.progress(entry, now, CI_WAIT_MAX_SEC))

    if state in (ciwait.WAITING, ciwait.UNREACHABLE):
        entry["detail"] = detail
        entry["unreachable"] = state == ciwait.UNREACHABLE
        if state == ciwait.UNREACHABLE and not entry.get("told_unreachable"):
            # Один раз, а не на каждый опрос: недоступный форж может молчать
            # час, и комментарий раз в две минуты превратил бы задачу в лог.
            entry["told_unreachable"] = True
            if issue_id:
                linear_comment(
                    issue_id,
                    f"🤖 **Проверки не опрашиваются.** {detail}\n\n"
                    f"Задача остаётся в ожидании и сданной НЕ считается. "
                    f"Продолжаю опрашивать; сдамся в "
                    f"{ciwait.hhmm(started + CI_WAIT_MAX_SEC)}.",
                    persona)
        if now - float(entry.get("last_heartbeat_at") or started) >= CI_WAIT_HEARTBEAT_SEC:
            # Сигнал наружу по часам, а не по событию: 40 минут без единого
            # слова — исходный случай DEV-16.
            entry["last_heartbeat_at"] = now
            where = ciwait.progress(entry, now, CI_WAIT_MAX_SEC)
            if issue_id:
                linear_comment(issue_id,
                               f"🤖 **Всё ещё ждём проверки.** {where}. "
                               f"Последний опрос: {detail}.", persona)
            notify(f"⏳ {ident}: всё ещё {where}\n{task.get('url', '')}")
        try:
            _ci_store(client, entry, now + CI_WAIT_POLL_SEC, now)
        except Exception as e:
            # Запись в redis не легла. Копия на диске записана первой —
            # сверка вернёт; но молчать нельзя: снаружи это неотличимо от
            # «всё хорошо». Один раз на ожидание.
            log.error("%s: запись ожидания не легла в redis (%s) — копия на диске, "
                      "вернёт сверка", ident, e)
            if not entry.get("told_redis_lost"):
                entry["told_redis_lost"] = True
                ciwait.write_state(workdir, entry, now, CI_WAIT_MAX_SEC)
                if issue_id:
                    linear_comment(
                        issue_id,
                        f"🤖 **Ожидание чуть не потерялось:** redis не принял запись "
                        f"({e}). Копия на диске цела, раннер вернёт её в опрос при "
                        f"ближайшей сверке (раз в {CI_WAIT_RECONCILE_SEC // 60} мин). "
                        f"Если исхода здесь нет к {ciwait.hhmm(started + CI_WAIT_MAX_SEC)} "
                        f"— посмотри проверки сам.", persona)
                notify(f"⚠️ {ident}: redis не принял запись ожидания проверок — "
                       f"копия на диске, вернёт сверка\n{task.get('url', '')}")
        return

    waited_min = int((now - started) // 60)
    head = ciwait.describe(state, detail, req)
    if state == ciwait.PASSED:
        # Ветку отпускаем только там, где ожидание КОНЧИЛОСЬ: на красном она
        # нужна целой — туда вернётся продолжение той же сессии.
        release_worktrees(workdir)
        _ci_drop(client, entry, state, detail, now)
        if issue_id:
            misses = [str(m) for m in (entry.get("handoff_misses") or [])]
            if misses:
                # Сдача была не по схеме (DEV-95) — теперь, когда ожидание
                # кончилось, сессию можно вернуть досдать без риска второго
                # продолжения. Перевод сделает она сама по ремонтному промпту.
                linear_comment(
                    issue_id,
                    f"🤖 **{head}**\n\nЖдали {waited_min} мин. Сдача была не по "
                    f"схеме — возвращаю сессию досдать: " + "; ".join(misses),
                    persona)
                notify(f"✅ {ident}: {head} — сессия вернётся досдать\n{task.get('url', '')}")
                try:
                    since = datetime.fromisoformat(str(entry.get("since") or ""))
                except ValueError:
                    since = datetime.now(timezone.utc)
                issue = _handoff_state(issue_id)
                repair_handoff(task, workdir, 0, ident, issue_id, issue, misses,
                               since, current_reviewer(), persona,
                               Path(entry.get("log") or workdir / "session.log"))
                return
            linear_comment(
                issue_id,
                f"🤖 **{head}**\n\nЖдали {waited_min} мин. Работа передаётся на "
                f"проверку — шаг 12 контракта за сессию, которая его намеренно "
                f"не делала: до зелёных проверок проверять было нечего.",
                persona)
            if not _hand_to_verification(issue_id, ident):
                _ci_hand_to_human(
                    entry, state, detail,
                    "Проверки зелёные, но перевести задачу на проверку не "
                    "вышло (см. журнал раннера) — передаю тебе, чтобы она не "
                    "осталась висеть в работе.", now)
                return
        notify(f"✅ {ident}: {head} — ушла на проверку\n{task.get('url', '')}")
        return

    if state in (ciwait.TIMEOUT, ciwait.NOT_FOUND):
        release_worktrees(workdir)
        _ci_drop(client, entry, state, detail, now)
        why = (f"Это не «зелено» и не «красно», а неизвестность: CI не ответил "
               f"за {CI_WAIT_MAX_SEC // 60} мин ожидания. Посмотри проверки сам."
               if state == ciwait.TIMEOUT else
               "Ждать нечего: прогона, который раннер должен был досмотреть, "
               "на форже нет. Посмотри ветку и CI сам.")
        _ci_hand_to_human(entry, state, detail, why, now)
        return

    # Красный. Возвращаем ту же сессию с причиной, а не заводим новую задачу:
    # рабочее дерево и ветка на месте, и второй MR на ту же работу человеку
    # пришлось бы сличать руками.
    fix_try = int(entry.get("fix_attempt") or 0) + 1
    sid = _session_id(workdir)
    if fix_try > MAX_CI_FIX_ATTEMPTS or not sid:
        release_worktrees(workdir)
        _ci_drop(client, entry, ciwait.FAILED, detail, now)
        _ci_hand_to_human(
            entry, ciwait.FAILED, detail,
            "Продолжать нечем: транскрипт сессии не найден, id сессии для "
            "`--resume` взять неоткуда." if not sid else
            f"Возвраты к сессии исчерпаны ({MAX_CI_FIX_ATTEMPTS}) — чинить "
            f"дальше человеку.", now)
        return
    wake_id = uuid.uuid4().hex
    # Отсечка «комментарий после старта» для сдачи (DEV-95) — старт ПЕРВОГО
    # захода: отчёт уже положен, гонять сессию за ним второй раз незачем.
    # Промахи прошлого ремонта в задаче не едут: чинит она красный, а не
    # сдачу, и промпт должен быть про красный.
    again = dict(task, ci_fix_attempt=fix_try, ci_failure=detail, ci_request=req,
                 ci_command=cmd, resume_session_id=sid, resume_workdir=str(workdir),
                 ci_wake_id=wake_id, handoff_since=str(entry.get("since") or ""))
    again.pop("handoff_misses", None)
    entry.update(state=ciwait.WAKING, detail=detail, wake_id=wake_id, woke_at=now,
                 fix_attempt=fix_try)
    try:
        # Запись — ДО очереди. Упади раннер между этими двумя строками,
        # сторож найдёт запись без сессии и скажет об этом; в обратном
        # порядке сессия поднялась бы, а ожидание считало бы, что красный
        # ещё не разобран, и подняло бы вторую.
        _ci_store(client, entry, now + CI_WAKE_MAX_SEC, now)
        client.lpush(QUEUE_KEY, json.dumps(again))
    except Exception as e:
        log.error("%s: не вернуть задачу в работу после красных проверок: %s", ident, e)
        release_worktrees(workdir)
        _ci_drop(client, entry, ciwait.FAILED, detail, now)
        _ci_hand_to_human(entry, ciwait.FAILED, detail,
                          f"Вернуть сессию чинить не вышло ({e}) — чинить "
                          f"дальше человеку.", now)
        return
    log.info("%s: проверки упали (%s) — возвращаю сессию чинить "
             "(попытка %d из %d)", ident, detail, fix_try, MAX_CI_FIX_ATTEMPTS)
    if issue_id:
        linear_comment(
            issue_id,
            f"🤖 **{head}**\n\nЖдали {waited_min} мин. Возвращаю ту же сессию "
            f"чинить с этой причиной (попытка {fix_try} из "
            f"{MAX_CI_FIX_ATTEMPTS}) — та же ветка, тот же MR, работа не "
            f"начинается с нуля. Если продолжение не поднимется, раннер "
            f"скажет об этом здесь. Вмешательства не требуется.",
            persona)
    notify(f"🔧 {ident}: {head} — вернул сессию чинить ({fix_try}/{MAX_CI_FIX_ATTEMPTS})"
           f"\n{task.get('url', '')}")


def _ci_check_wake(client, entry: dict, now: float) -> None:
    """Сторож продолжения: красный разобран, сессия положена в очередь — но
    поднялась ли? Поднявшаяся отмечается сама (`_ci_ack_wake`) и сюда не
    попадает. Попали — значит отметки нет, и надо разобраться, почему.
    """
    task = entry["task"]
    ident = task.get("identifier") or "?"
    issue_id = task.get("issue_id") or ""
    workdir = Path(entry["workdir"])
    woke_at = float(entry.get("woke_at") or 0)
    # 1. Поднялась, но отметиться не смогла (redis лежал в ту секунду):
    #    свежий session.log — тот же факт, другим способом.
    try:
        fresh = (workdir / "session.log").stat().st_mtime >= woke_at
    except OSError:
        fresh = False
    if fresh:
        log.info("%s: продолжение поднялось (свежий session.log) — запись "
                 "ожидания снята", ident)
        _ci_drop(client, entry, ciwait.FAILED, "продолжение сессии поднялось", now)
        return
    # 2. Ещё в очереди — слоты заняты. Законно; продлеваем и говорим.
    try:
        queued = any(str(entry.get("wake_id")) in str(raw)
                     for raw in client.lrange(QUEUE_KEY, 0, -1))
    except redis.RedisError as e:
        log.warning("%s: очередь не спросить (%s) — сторож продолжения перепроверит", ident, e)
        _ci_store(client, entry, now + CI_WAIT_POLL_SEC, now)
        return
    if queued:
        waited = int((now - woke_at) // 60)
        log.warning("%s: продолжение после красных проверок ещё в очереди, "
                    "%d мин — ждёт слот", ident, waited)
        if issue_id:
            linear_comment(issue_id,
                           f"🤖 Продолжение сессии всё ещё в очереди — ждёт свободный "
                           f"слот уже {waited} мин. Ожидание живо, следующая "
                           f"проверка в {ciwait.hhmm(now + CI_WAKE_MAX_SEC)}.",
                           entry.get("persona") or "")
        notify(f"⏳ {ident}: продолжение после красных проверок ждёт слот "
               f"уже {waited} мин\n{task.get('url', '')}")
        _ci_store(client, entry, now + CI_WAKE_MAX_SEC, now)
        return
    # 3. В очереди нет, следа нет — потеряно. Молчать нельзя.
    release_worktrees(workdir)
    _ci_drop(client, entry, ciwait.WAKE_LOST, str(entry.get("detail") or ""), now)
    _ci_hand_to_human(
        entry, ciwait.WAKE_LOST, str(entry.get("detail") or ""),
        f"Продолжение сессии положили в очередь в {ciwait.hhmm(woke_at)}, но "
        f"оно не поднялось: в очереди его нет, и в рабочем каталоге следа нет. "
        f"Причина падения проверок выше. Чинить дальше человеку.", now)


def _ci_ack_wake(task: dict) -> None:
    """Продолжение после красных проверок поднялось — снять запись сторожа.

    Зовётся из `run_session` до старта процесса. Не вышло (redis лёг) —
    не беда: сторож увидит свежий `session.log`.
    """
    wake_id = str(task.get("ci_wake_id") or "")
    ident = task.get("identifier") or "?"
    if not wake_id:
        return
    try:
        client = _ci_conn()
        entry = _ci_entry_of(client, ident)
        if entry and entry.get("wake_id") == wake_id:
            _ci_drop(client, entry, ciwait.FAILED, "продолжение сессии поднялось",
                     time.time())
            log.info("%s: продолжение после красных проверок поднялось", ident)
        else:
            log.info("%s: записи сторожа с этим wake_id нет — уже снята", ident)
    except Exception as e:
        log.warning("%s: не отметить подъём продолжения в redis (%s) — сторож "
                    "увидит по session.log", ident, e)


def _ci_wait_tick(client, now: float | None = None) -> int:
    """Созревшие ожидания → опросить. Возвращает, сколько опросил.

    Забираем через ZREM и работаем, только если ZREM подтвердил, что забрали
    именно мы: иначе два подметальщика (два раннера на одном redis) опросили
    бы одну задачу дважды и вернули бы её чинить двумя сессиями.
    """
    now = time.time() if now is None else now
    polled = 0
    try:
        due = client.zrangebyscore(CI_WAIT_KEY, "-inf", now)
    except redis.RedisError as e:
        log.warning("очередь ожидания проверок недоступна: %s", e)
        return 0
    for member in due:
        ident, gen = _ci_split(member)
        try:
            if not client.zrem(CI_WAIT_KEY, member):
                continue
            entry = _ci_entry_of(client, ident)
        except redis.RedisError as e:
            log.warning("очередь ожидания проверок недоступна: %s", e)
            return polled
        if entry is None:
            # Расписание есть, записи нет: ключ потерян. Сверка восстановит
            # с диска, если копия цела, и скажет об этом.
            log.error("%s: в расписании ожидания есть, а записи в redis нет — "
                      "потеряна; сверка вернёт с диска", ident)
            continue
        if str(entry.get("gen") or "") != gen:
            # Расписание от прошлого поколения записи (два раннера, устаревший
            # список): текущее поколение стоит в расписании своим участником.
            log.warning("%s: в расписании устаревшее поколение записи — пропускаю", ident)
            continue
        polled += 1
        try:
            _ci_step(client, entry, now)
        except Exception:
            # Одно ожидание не должно уносить подметальщика: он же возвращает
            # задачи после сброса лимита Claude. Запись осталась в HASH без
            # расписания — сверка вернёт её в опрос.
            log.exception("ожидание проверок по %s сорвалось", ident)
    return polled


def _ci_recover(client, now: float | None = None) -> int:
    """Сверка трёх мест, где живёт ожидание: расписание (ZSET), записи (HASH),
    диск. Возвращает, сколько ожиданий вернул в строй.

    Зовётся при старте подметальщика и раз в `CI_WAIT_RECONCILE_SEC`. Три
    поломки, каждая — вслух:
    * запись есть, расписания нет — раннер упал посреди шага (между ZREM и
      ZADD): вернуть в опрос сейчас;
    * расписание есть, записи нет — ключ потерян: расписание снять, запись
      искать на диске;
    * на диске живое ожидание, в redis его нет — ключ потерян или хост
      перезагружен (redis client-b — снимки без AOF): восстановить, сказать в
      задачу и в канал. Слишком старая копия (дольше двух сроков) не
      восстанавливается, а отдаётся человеку с причиной: опрашивать давно
      закончившийся прогон и объявлять по нему «не ответил» — врать.
    """
    now = time.time() if now is None else now
    restored = 0
    try:
        entries = client.hgetall(CI_WAIT_ENTRIES_KEY) or {}
        scheduled = set(client.zrange(CI_WAIT_KEY, 0, -1) or [])
    except redis.RedisError as e:
        log.warning("сверка ожиданий проверок: redis недоступен: %s", e)
        return 0
    live: set[str] = set()
    current: set[str] = set()
    for ident, raw in entries.items():
        try:
            entry = json.loads(raw)
        except ValueError:
            entry = None
        if not isinstance(entry, dict) or entry.get("state") not in ciwait.LIVE:
            log.warning("%s: запись ожидания в redis нестроевая (%s) — снимаю", ident,
                        "не разобрана" if not isinstance(entry, dict) else entry.get("state"))
            client.hdel(CI_WAIT_ENTRIES_KEY, ident)
            continue
        live.add(ident)
        member = _ci_member(ident, str(entry.get("gen") or ""))
        current.add(member)
        if member not in scheduled:
            log.warning("%s: запись ожидания была без расписания (раннер упал "
                        "посреди шага?) — вернул в опрос", ident)
            client.zadd(CI_WAIT_KEY, {member: now})
            restored += 1
    for member in scheduled - current:
        ident, _ = _ci_split(member)
        if ident in live:
            log.warning("%s: в расписании устаревшее поколение записи — снято", ident)
        else:
            log.error("%s: в расписании ожидания есть, а записи нет — расписание "
                      "снято, ищу копию на диске", ident)
        client.zrem(CI_WAIT_KEY, member)

    try:
        files = sorted(WORK_ROOT.glob(f"*/{ciwait.STATE_FILE}"))
    except OSError as e:
        log.warning("сверка ожиданий: не прочитать %s: %s", WORK_ROOT, e)
        return restored
    for path in files:
        state = ciwait.read_state(path.parent)
        if not state or state.get("state") not in ciwait.LIVE:
            continue
        ident = str(state.get("ident") or "")
        if not ident or ident in live or not state.get("task") or not state.get("req"):
            continue
        entry = dict(state)
        entry["workdir"] = str(path.parent)
        started = float(entry.get("started_at") or now)
        task = entry.get("task") or {}
        if now - started > 2 * CI_WAIT_MAX_SEC:
            log.error("%s: на диске живое ожидание, которому %d мин — не восстанавливаю, "
                      "отдаю человеку", ident, int((now - started) // 60))
            _ci_drop(client, entry, ciwait.TIMEOUT,
                     f"копия ожидания найдена через {int((now - started) // 60)} мин "
                     f"после начала", now)
            _ci_hand_to_human(
                entry, ciwait.TIMEOUT, str(entry.get("detail") or ""),
                "Запись об ожидании проверок пропала из redis и нашлась на диске "
                "слишком поздно, чтобы её досматривать. Посмотри проверки сам.", now)
            continue
        try:
            _ci_store(client, entry, now, now)
        except Exception as e:
            log.error("%s: восстановить ожидание с диска не вышло: %s", ident, e)
            continue
        live.add(ident)
        restored += 1
        where = ciwait.progress(entry, now, CI_WAIT_MAX_SEC)
        log.warning("%s: запись ожидания пропала из redis — восстановлена с диска: %s",
                    ident, where)
        if task.get("issue_id"):
            linear_comment(
                task["issue_id"],
                f"🤖 **Запись об ожидании проверок пропала из redis** (рестарт "
                f"хоста или потеря ключа) — восстановлена с диска, ожидание "
                f"продолжается: {where}.", entry.get("persona") or "")
        notify(f"⚠️ {ident}: запись ожидания проверок пропала из redis — "
               f"восстановлена с диска; {where}\n{task.get('url', '')}")
    return restored


def _is_infra_failure(tail: str) -> bool:
    """Похоже ли завершение на сбой инфраструктуры, а не на провал задачи."""
    low = (tail or "").lower()
    return any(m in low for m in _INFRA_MARKERS)


def _requeue(task: dict, attempt: int) -> bool:
    """Вернуть задачу в очередь. True — получилось.

    Отдельное подключение, а не клиент воркера: сюда приходят и из путей, где
    клиента под рукой нет, а стоимость одного коннекта на аварию ничтожна.
    """
    task = dict(task, requeue_attempt=attempt)
    try:
        redis.from_url(REDIS_URL).lpush(QUEUE_KEY, json.dumps(task))
        return True
    except Exception as e:
        log.warning("не вернуть задачу в очередь: %s", e)
        return False


def _intake_draft(workdir: Path) -> tuple[dict | None, str]:
    """draft.json, оставленный сессией → (постановка, причина отказа).

    Разбирает и проверяет здесь, а не в events-api: провал должен доехать до
    человека как «задача не собралась», а не как молчание.

    Обязательны только заголовок и описание. `team_id` нужен, чтобы создать
    issue, и его отсутствие events-api разбирает у себя — как несостоявшуюся
    сборку с кнопкой «Повторить». Требовать его здесь нельзя: тогда ответ на
    пустую фактуру, который контракт велит оставлять всегда, выбрасывался бы
    целиком.

    `summary` необязателен: карточка в личке переживёт его отсутствие (там
    есть запасной путь по описанию), а вот выброшенная из-за него готовая
    постановка стоила бы человеку целой сессии.

    `reject` — исход «из этого постановки не выходит» (TASK-177). Заголовка и
    описания у него нет по смыслу: задача не создаётся, человеку показывается,
    чего не хватает. Поэтому при непустом `reject` обязательных полей нет.
    """
    try:
        raw = (workdir / "draft.json").read_text(errors="replace")
    except OSError:
        return None, "сессия не оставила draft.json"
    try:
        draft = json.loads(raw)
    except ValueError as e:
        return None, f"draft.json не разобрался ({e})"
    if not isinstance(draft, dict):
        return None, "draft.json — не объект"
    reject = str(draft.get("reject") or "").strip()
    missing = [] if reject else [k for k in ("title", "description")
                                 if not str(draft.get(k) or "").strip()]
    if missing:
        return None, f"в draft.json нет полей: {', '.join(missing)}"
    similar = draft.get("similar")
    # Незнакомое/пустое значение — "work", не "incident": ложный инцидент
    # дороже пропущенного (см. INTAKE_TEMPLATE шаг 3) - при сомнении в самом
    # разборе draft.json тоже безопаснее считать задачу обычной.
    role = str(draft.get("role") or "").strip().lower()
    if role not in ("incident", "work"):
        role = "work"
    return {
        "title": str(draft.get("title") or "").strip(),
        "summary": str(draft.get("summary") or "").strip(),
        "description": str(draft.get("description") or "").strip(),
        "reject": reject,
        "team_id": str(draft.get("team_id") or "").strip(),
        "role": role,
        "similar": [s for s in similar if isinstance(s, dict)] if isinstance(similar, list) else [],
        "question": str(draft.get("question") or "").strip(),
    }, ""


def report_intake(task: dict, workdir: Path, error: str = "",
                  limit: bool = False, limit_reset: str = "") -> None:
    """Постановку (или причину отказа) — в events-api.

    Мост тот же, что у проверки выката: список в локальном redis. Задачу
    заводит events-api, а не раннер: ключ Linear и кнопки под карточкой у
    него, и создание должно быть там же, где разбираются клики по ним.

    `limit` едет отдельным полем, а не только словами внутри `error`: по нему
    events-api решает, предлагать ли повтор. Разбирать русскую фразу на той
    стороне — значит завязать поведение кнопки на формулировку.

    Готовый `draft.json` сильнее причины обрыва. Постановка написана задолго
    до выхода из сессии, а снимают её на выходе (срок, ходы, OOM) — и раньше
    непустой `error` закорачивал чтение файла: собранная работа выбрасывалась,
    человек получал «не уложилась в таймаут» вместо задачи. То же правило уже
    действует для вердикта проверки (`is_verify and verdict` в `run_session`).
    Недописанный файл отсеивается сам: `_intake_draft` не разберёт половину
    json и вернёт причину.
    """
    draft, why = _intake_draft(workdir)
    if draft is None and error:
        why = error
    elif draft is not None and error:
        log.warning("постановка (%s) собрана до обрыва — отдаём её, а не «%s»",
                    task.get("draft_id") or "", error)
    payload = {
        "chat_id": task.get("chat_id"),
        "draft_id": task.get("draft_id") or "",
        "ok": draft is not None,
        "error": why if draft is None else "",
        "limit": bool(limit),
        "limit_reset": limit_reset,
        "draft": draft or {},
        # Пачка едет обратно двум потребителям: строчке происхождения в
        # тикете («от Ивана, картинок 2») и возврату фактуры в пачку на
        # отказе — пересылать переписку второй раз человек не должен.
        "items": [{k: v for k, v in it.items() if k != "path"}
                  for it in (task.get("items") or [])],
    }
    try:
        redis.from_url(REDIS_URL).lpush(INTAKE_RESULTS_KEY, json.dumps(payload))
    except Exception as e:
        log.error("постановка не доехала до events-api (%s): %s", task.get("draft_id"), e)
        notify(f"⚠️ постановка собрана, но не доехала до events-api: {e}\n"
               f"лог: {workdir / 'session.log'}")


# --- память ролей -----------------------------------------------------------
#
# claude привязывает память к project scope, а тот — к рабочему каталогу. У нас
# каталог свой на каждую задачу, поэтому память рождалась пустой и умирала
# вместе с задачей: 150 осиротевших scope'ов и 23 файла, которые никто больше
# не прочитает. Опыт не накапливался в принципе.
#
# Чиним не своей реализацией памяти, а подстановкой симлинка на постоянный
# каталог роли: у ролей разный опыт (приёмщику незачем знать, как дежурный
# гасит инцидент), поэтому ключ — роль, а не задача и не слот.


def _role_of(kind: str) -> str:
    """Роль сессии для имени каталога памяти. Незнакомое → work, как и везде."""
    return kind if kind in ("verify", "intake", "incident") else "work"


def _scope_slug(workdir: Path) -> str:
    """Имя project scope в claude по рабочему каталогу.

    Правило выведено по 150 существующим каталогам на хосте: КАЖДЫЙ
    не-алфанумерический символ становится дефисом, не только слэш. Первая
    версия заменяла только `/`, и путь с подчёркиванием давал мимо:
    `/Volumes/workdisk/...` claude называет `-Volumes-Ext-Disk-...`.

    Ошибка была бы молчаливой и хуже отсутствия механизма: симлинк лёг бы
    туда, куда claude не смотрит, а в лог ушло бы «память роли подключена».
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(workdir))


def _memory_entry(path: Path) -> tuple[str, str]:
    """(name, description) из frontmatter файла памяти. Пусто → по имени файла.

    Разбор один на весь раннер — `memory.entry_of`. Своя копия разбора стояла
    здесь до 14.09.2026 и разошлась с оригиналом ровно там, где это дороже
    всего: она не читала `drop: true`, и запись, помеченная владельцем как
    выключенная, продолжала стоять в индексе проектного слоя. Две реализации
    одного правила расходятся всегда; вопрос только в том, когда это заметят.
    """
    e = memory.entry_of(path)
    return e.name, e.description


def _lock_exclusive(fh) -> None:
    """Эксклюзивная блокировка файла на время read-modify-write.

    Платформа без flock — не повод не стартовать сессию: без блокировки
    остаётся прежний риск дубля в индексе, а это косметика, в отличие от
    несостоявшейся сессии.
    """
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception as e:
        log.debug("память: блокировка индекса недоступна (%s)", e)


# Заголовок собранного блока индекса. Всё НИЖЕ него — производное от файлов и
# переписывается на каждой сборке; всё ВЫШЕ — текст человека и переживает её.
INDEX_MARK = ("<!-- ниже собрано программой из полей записей. Правки в этом "
              "блоке не переживут следующую сборку: правь frontmatter файла. -->")

# Потолок описания в СТРОКЕ индекса. Сам файл не трогается — режется только
# строка каталога.
#
# Зачем: строку индекса и описание в файле писали двое, и они разошлись у 82
# записей из 101 в живом сторе client-a. Источником истины обязан быть файл —
# иначе индекс описывает позапрошлую версию записи. Но описания в файлах на
# четверть длиннее тех строк, что стояли в индексе, и честная сборка из полей
# раздула бы его там, где мы его и пытаемся уместить в потолок.
#
# Замерено на живом сторе client-b — сколько роль получает в контекст ВСЕГО
# (собранный индекс плюс блок слоёв) против наблюдавшегося потолка чтения
# 24 400 Б (DEV-29). Худшая роль — verify, у неё 59 записей:
#
#     потолок 100   21 490 Б   88% потолка
#     потолок  90   20 508 Б   84%
#     потолок  80   19 769 Б   81%
#     потолок  70   18 724 Б   77%
#
# Взято 80. Сегодня verify получает 18 457 Б (76%) при индексе, который врёт:
# строки в нём разошлись с файлами у 82 записей из 101. Потолок 80 покупает
# верный индекс за четыре процентных пункта запаса.
#
# Чего потолок НЕ делает: он не создаёт запаса. При 59 записях индекс упирается
# в число записей, а не в длину строк — даже 70 возвращает ровно сегодняшний
# уровень. Запас даёт только свёртка содержания, и это отдельная работа.
#
# Обрезается ТОЛЬКО строка каталога, сам файл не трогается. Строка отвечает на
# один вопрос — «открывать ли файл»; остальное лежит в файле, до которого шаг.
INDEX_DESC_MAX = 80


def _short(desc: str, limit: int | None = None) -> str:
    """Описание до limit знаков, по границе слова. Обрезанное помечено многоточием.

    Потолок читается на вызове, а не защёлкивается умолчанием аргумента: иначе
    правка `INDEX_DESC_MAX` не действует ни на тест, ни на живой процесс.
    """
    limit = INDEX_DESC_MAX if limit is None else limit
    if len(desc) <= limit:
        return desc
    cut = desc[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or desc[:limit]) + "…"


# Строка индекса проектного слоя: относительная ссылка. Формат не наш — так
# пишет собственный инструмент памяти агента, и индекс читают оба.
_LINK_RE = re.compile(r"^- \[(?P<name>[^\]]*)\]\((?P<file>[^)]+\.md)\)"
                      r"(?: — (?P<desc>.*))?\s*$")
# Второй формат, которым индекс писался тоже: `- имя — описание (/абс/путь.md)`.
# Он и есть причина, по которой дедупликация не работала: прежняя проверка
# искала подстроку `(файл.md)`, а в этой строке стоит `(/home/x/файл.md)` —
# совпадения нет никогда, и запись дописывалась второй раз при каждой сборке.
_PATH_RE = re.compile(r"^- (?P<name>.+?) — (?P<desc>.*) "
                      r"\((?P<file>[^()]+\.md)\)\s*$")


def _parse_index_line(line: str, known: set[str]) -> tuple[str, str] | None:
    """(имя файла, описание) — если строка описывает запись, лежащую в слое.

    Строка, ссылающаяся на файл, которого в слое нет, записью НЕ считается: её
    писал человек про что-то своё, и сборка её не тронет.
    """
    for rx in (_LINK_RE, _PATH_RE):
        m = rx.match(line)
        if not m:
            continue
        name = Path(m.group("file")).name
        if name in known:
            return name, (m.group("desc") or "").strip()
    return None


def rebuild_memory_index(mem_dir: Path) -> int:
    """Собрать MEMORY.md из полей записей. Вернуть, сколько записей в индексе.

    СОБРАТЬ, а не дописать. Прежняя версия только добавляла строки, которых «нет
    в файле», и проверяла это подстрокой `(файл.md)` — а писателей формата было
    два, и в одном из них скобки содержат абсолютный путь. Подстрока не
    находилась никогда, строка дописывалась снова, индекс отрастал после каждой
    свёртки. В живом сторе client-a это дало три записи, стоящие дважды, и
    26 591 байт при потолке 12 000 — то есть роль, не увидевшую методологию.

    Сборка из полей убирает не дубли, а их причину: имя и описание берутся из
    frontmatter файла, порядок задан видом записи, формат один.

    Что переживает сборку:
    * всё, что стоит ВЫШЕ `INDEX_MARK` — заголовки, пояснения, текст человека;
    * описание, которое человек написал в индексе, если в самом файле описания
      нет: курирование богаче пустоты, и терять его нельзя (ради этого правила
      прежняя версия и отказывалась удалять — цель верная, средство неверное).

    Что не переживает: порядок строк, дубли и записи, помеченные `drop: true`.
    """
    index = mem_dir / "MEMORY.md"
    try:
        files = [f for f in sorted(mem_dir.glob("*.md"))
                 if f.name not in memory.NOT_ENTRIES]
        known = {f.name for f in files}
        entries = [memory.entry_of(f) for f in files]
        entries = [e for e in entries if not e.dropped]
        # Ограничения первыми — тем же правилом, что и в `memory._fit`: порядок
        # в индексе и порядок обрезки должны совпадать, иначе обрезается не то,
        # что видно первым.
        entries.sort(key=lambda e: (not e.is_constraint, e.path.name))

        index.touch(exist_ok=True)
        with index.open("r+", encoding="utf-8") as fh:
            # Замок берётся ДО чтения, а читается индекс как можно позже —
            # уже после того, как записи собраны с диска. Чтение в начале
            # функции теряло строку, которую параллельная сессия дописала,
            # пока мы разбирали frontmatter; окно теперь схлопнуто до самой
            # записи. Совсем закрыть его нельзя: собственный инструмент памяти
            # агента пишет в тот же файл и про наш замок не знает.
            _lock_exclusive(fh)
            old_text = fh.read()

            # Строка, не распознанная как запись, НЕ пропадает: она уходит в
            # шапку, выше маркера. Разбирается и шапка тоже — строка,
            # ссылавшаяся на ещё не созданный файл, после его появления
            # возвращается в собранный блок, а не стоит в двух местах сразу.
            head, curated = [], {}
            body = old_text.split(INDEX_MARK, 1)
            head_src, rest = (body[0], body[1]) if len(body) == 2 else ("", old_text)
            for line in head_src.splitlines() + rest.splitlines():
                parsed = _parse_index_line(line, known)
                if parsed:
                    name, desc = parsed
                    if desc:
                        curated.setdefault(name, desc)
                elif line.strip():
                    head.append(line)

            lines = []
            for e in entries:
                desc = e.description or curated.get(e.path.name, "")
                lines.append(f"- [{e.name}]({e.path.name})"
                             + (f" — {_short(desc)}" if desc else ""))

            head_text = "\n".join(head).rstrip("\n")
            out = (head_text + "\n\n" if head_text else "") + INDEX_MARK + "\n"
            out += "\n".join(lines) + ("\n" if lines else "")

            if out == old_text:
                return len(entries)
            if len(body) == 1 and old_text.strip():
                # Первая сборка поверх старого формата. Владелец ничего не
                # удаляет: прежний файл уводится в подкаталог, а не заменяется.
                # Подкаталог индексом не читается (`glob("*.md")` нерекурсивен),
                # так что копия не станет второй записью.
                keep = mem_dir / "_before-index-rebuild"
                keep.mkdir(exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                (keep / f"MEMORY.md.{stamp}").write_text(old_text, encoding="utf-8")
            fh.seek(0)
            fh.write(out)
            fh.truncate()
        return len(entries)
    except Exception as e:
        log.warning("память: не смог пересобрать индекс в %s (%r)", mem_dir, e)
        return 0


def link_role_memory(workdir: Path, kind: str) -> None:
    """Подставить сессии постоянную память её роли вместо одноразовой.

    Ничего не делает, если стор не задан (прежнее поведение) или если на месте
    памяти уже лежит НЕ симлинк: реальный каталог мог создать предыдущий запуск
    и в нём чьи-то записи — молча подменить его симлинком значило бы их спрятать.
    """
    if not AGENT_MEMORY_STORE:
        return
    try:
        role_dir = Path(AGENT_MEMORY_STORE) / f"agent-{_role_of(kind)}"
        role_dir.mkdir(parents=True, exist_ok=True)
        indexed = rebuild_memory_index(role_dir)
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        scope = root / "projects" / _scope_slug(workdir)
        scope.mkdir(parents=True, exist_ok=True)
        mem = scope / "memory"
        if mem.is_symlink():
            if mem.resolve() == role_dir.resolve():
                return
            mem.unlink()
        elif mem.exists():
            log.warning("память: %s — реальный каталог, не подменяю симлинком", mem)
            return
        mem.symlink_to(role_dir, target_is_directory=True)
        log.info("память роли %s подключена (%s), записей в индексе %d",
                 _role_of(kind), role_dir, indexed)
    except Exception as e:
        # Память — не обязательное условие работы: сессия должна стартовать.
        log.warning("память: не смог подключить роль (%r) — сессия пойдёт "
                    "с одноразовой памятью", e)


def _index_budget() -> int:
    """Потолок на ВСЕ индексы памяти вместе, в байтах (AGENT_MEMORY_INDEX_MAX).

    Слоёв три, а в контекст грузится индекс каждого: сложение упирается в
    потолок чтения быстрее, чем один стор (DEV-29). Делит потолок между слоями
    `memory.layers_block` — проектному столько, сколько занимает, остаток
    методологии, что после неё — слою о человеке.

    ВАЖНО, и это не очевидно: сам проектный индекс этим потолком НЕ
    ограничивается. Его грузит claude из каталога памяти, на который мы
    поставили симлинк, а здесь он только вычитается из бюджета. Уменьшить эту
    переменную значит уменьшить долю методологии в контексте, а не контекст.
    Размер проектного индекса задаётся числом записей и `INDEX_DESC_MAX`.

    Читается на вызове, а не константой на импорте, ровно из-за одной буквы:
    значение правят руками в файле окружения, и `int("12к")` на импорте уронил
    бы раннер целиком — то есть память, которая «не обязательное условие
    работы», остановила бы все сессии. Мусор и неположительное → умолчание, но
    в журнал, а не молча: иначе промах выглядит как работающая настройка.
    """
    raw = os.environ.get("AGENT_MEMORY_INDEX_MAX", "").strip()
    if not raw:
        return memory.DEFAULT_INDEX_MAX
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        log.warning("память: AGENT_MEMORY_INDEX_MAX=%r — не число больше нуля, "
                    "беру умолчание %d", raw, memory.DEFAULT_INDEX_MAX)
        return memory.DEFAULT_INDEX_MAX
    return value


def _tracks() -> set[str]:
    """Дисциплины, которыми занимается эта инсталляция (AGENT_METHODOLOGY_TRACKS).

    Список через запятую: `разработка`, `маркетинг`, `контент`. Методология
    показывает инсталляции свои страницы плюс всё, что помечено `track: общее`.

    Пусто — фильтра нет, показывается всё. Это не заглушка, а решение: пока
    владелец не назвал дисциплину, показать лишнюю страницу дешевле, чем
    спрятать нужную, и промах при этом виден глазами, а не по отсутствию.

    Зачем поле вообще: базовая методология состояла из шести страниц про ветки,
    CI и ревью, и команде, которая кода не пишет, в ней не было ничего. Схема
    обязана принимать такую команду, а не переделываться под каждую — поэтому
    дисциплина стала полем записи, а не новой сборкой продукта.
    """
    raw = os.environ.get("AGENT_METHODOLOGY_TRACKS", "").strip()
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def memory_layers(kind: str) -> str:
    """Блок промпта про слои памяти, которые сессия читает, но не пишет.

    Пути знает раннер, а не prompt.py и не memory.py: методология лежит рядом с
    ядром (едет с релизом), переопределения клиента — в сторе инсталляции, слой
    о человеке — там, куда указал владелец. Стор не задан — остаётся
    методология: она есть у любой инсталляции, потому что приезжает с продуктом.

    Роль (`_role_of(kind)`) идёт и в путь к индексу, и в сборку блока: страницы
    методологии адресованы ролям полем `roles`, и дежурному правила про ветки
    и MR не едут (DEV-92).

    Как и подключение памяти, обязательным условием работы не является: не
    собрался блок — сессия идёт без него, а не не идёт вовсе.
    """
    try:
        role_index = override = None
        if AGENT_MEMORY_STORE:
            store = Path(AGENT_MEMORY_STORE)
            role_index = store / f"agent-{_role_of(kind)}" / "MEMORY.md"
            override = store / "methodology"
        return memory.layers_block(
            role_index=role_index,
            methodology_override=override,
            person_root=Path(AGENT_PERSON_MEMORY) if AGENT_PERSON_MEMORY else None,
            person_owner=AGENT_OWNER_NAME,
            budget=_index_budget(),
            tracks=_tracks(),
            role=_role_of(kind))
    except Exception as e:
        log.warning("память: не собрал блок слоёв (%r) — сессия пойдёт без него", e)
        return ""


def _keep_previous_run(workdir: Path, attempt: int) -> None:
    """Отложить лог и транскрипт прерванной сессии, прежде чем начать поверх.

    Продолжение идёт в тот же каталог, а `session.log`/`transcript.jsonl`
    открываются на запись с усечением — без этого шага первая половина работы
    стирается ровно в тот момент, когда мы её и спасаем.

    Стирается не абстрактная история: на `session.log` указывает комментарий в
    Linear, оставленный при откладывании, — человек пришёл бы по ссылке во
    вторую половину. А `_session_id` читает `transcript.jsonl`: если следующая
    сессия умрёт, не успев выдать ни одного события (лимит кончился сразу),
    пустой транскрипт дал бы None, и задача ушла бы человеку вместо третьей
    попытки — молча и без следа в логах.
    """
    for name in ("session.log", "transcript.jsonl"):
        src = workdir / name
        if not src.exists():
            continue
        try:
            src.rename(workdir / f"{name}.{attempt}")
        except OSError as e:
            # Не повод не продолжать работу: потеря истории хуже, чем её
            # отсутствие, но обе лучше брошенной задачи.
            log.warning("не сохранить %s прошлой сессии: %s", name, e)


# ── личность сессии в трекере ──────────────────────────────────────────────

# Адрес MCP-сервера трекера: сессия ходит в трекер через него, а не через
# GraphQL. Настройка, а не константа, по той же причине, что и API: у
# инсталляции может стоять свой прокси перед ним.
LINEAR_MCP_URL = (os.environ.get("LINEAR_MCP_URL", "").strip()
                  or "https://mcp.linear.app/mcp")

# Сколько вывода ревью-субсессии переносим в комментарий. Больше — это уже не
# комментарий, а лог: подробности лежат в описании MR, который ревью и смотрит.
REVIEW_COMMENT_MAX = 15000


def write_mcp_config(workdir: Path, persona: str) -> Path | None:
    """Свой MCP-конфиг сессии: трекер с ключом её личности.

    Без него сессия ходит в трекер конфигом хоста (`~/.claude.json`), то есть
    общим ключом инсталляции — и её собственные комментарии подписаны
    владельцем ключа, а не ролью. Ключа личности нет — файла тоже нет, и
    сессия работает как раньше.

    Одноимённый сервер из `--mcp-config` ПЕРЕКРЫВАЕТ запись хоста (проверено
    12.09.2026 на claude CLI: запрос ушёл на адрес из файла, с его же
    заголовком), поэтому `--strict-mcp-config` не нужен — остальные серверы
    инсталляции остаются на месте.

    Ключ кладём файлом, а не строкой в аргументах: аргументы процесса видны
    всему хосту в `ps`. Файл лежит РЯДОМ с рабочими копиями, а не внутри них
    — `git add -A` сессии его не подхватит.
    """
    key = PERSONA_KEYS.get(persona or "")
    if not key:
        # Сессия пойдёт общим ключом, и её комментарии будут подписаны
        # владельцем — по задаче это неотличимо от работы человека. Тихо
        # такое пропускать нельзя: чаще всего причина не в замысле, а в том,
        # что ключ дописали в окружение после старта юнита.
        log.warning("личность %s без ключа (%s) — сессия пойдёт в трекер "
                    "общим ключом, её комментарии подпишет владелец",
                    persona, personas.key_var(persona))
        return None
    path = workdir / "mcp.json"
    body = {"mcpServers": {"linear": {
        "type": "http", "url": LINEAR_MCP_URL,
        "headers": {"Authorization": f"Bearer {key}"}}}}
    try:
        # Права задаются в момент СОЗДАНИЯ, а не правкой после записи: между
        # `write_text` и `chmod` файл с ключом полежал бы под umask (0644), а
        # хост этот делится с чужими сервисами. `chmod` следом — ради
        # повторного запуска в том же каталоге (сессия, продолженная после
        # лимита): существующему файлу `O_CREAT` режим не меняет.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(body, ensure_ascii=False))
        path.chmod(0o600)
    except OSError as e:
        # Не повод не запускать сессию: без конфига она пойдёт общим ключом,
        # как ходила раньше. Но сказать об этом надо — иначе «почему опять
        # комментарии от владельца» выясняется глазами по самим задачам.
        log.error("не записал %s (%s) — сессия пойдёт в трекер общим ключом, "
                  "а не от личности %s", path, e, persona)
        return None
    return path


# Чем заменяем ключ, когда сессия закончилась. Слово, а не пустая строка:
# по нему видно, что конфиг был и ключ снят намеренно, а не что личность не
# настроена.
MCP_KEY_MASK = "снят после сессии"


def mask_mcp_config(path) -> bool:
    """Снять ключ личности из `mcp.json` доработавшей сессии.

    Не удаляем файл, а перезаписываем заглушкой: сам факт «сессия ходила в
    трекер от такой-то личности» остаётся видимым при разборе рабочего
    каталога, а секрета в нём больше нет. Удалять тут нечего — данных в этом
    файле нет, есть только ключ.

    Рабочие каталоги не убираются и живут месяцами, поэтому снимать ключ
    обязан тот, кто его положил, а не уборка по расписанию: между концом
    сессии и уборкой ключ лежал бы на диске сутками.

    Возвращает, получилось ли: не повод валить сессию (работа уже сделана),
    но повод сказать в журнал.
    """
    if not path:
        return False
    path = Path(path)
    try:
        body = json.loads(path.read_text())
        server = body.get("mcpServers", {}).get("linear", {})
        headers = server.get("headers", {})
        if "Authorization" in headers:
            headers["Authorization"] = f"Bearer {MCP_KEY_MASK}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(body, ensure_ascii=False))
        return True
    except (OSError, ValueError) as e:
        log.error("не снял ключ из %s (%s) — на диске остался живой ключ "
                  "личности, снять руками", path, e)
        return False


REVIEW_FILE = "review.md"


def review_of(workdir: Path) -> tuple[str, str]:
    """Схема на `review.md`: (текст ревью, причина отказа). Ровно одно из двух
    непусто.

    Отсутствие файла — это отказ, а не «ревью не было»: контракт (шаг 9)
    велит класть в файл и итог ревью, и «ревью не состоялось» одной
    строкой. Нет ни того, ни другого — значит шаг пропущен молча, и сказать
    об этом обязан раннер: по трассам client-b файл лежал в 13 рабочих
    каталогах из 29, и 16 сессий прошли к человеку как «ревью было» (DEV-93,
    DEV-90). Тот же принцип, что у `verdict.txt` (`report_verdict` не шлёт
    пустое, а называет пропажу) и у `draft.json` (`report_intake` отвечает
    «не оставила draft.json»).

    Отказ ЧИТАЕТСЯ уже не здесь: пропущен шаг или ревьюить было нечего —
    решает `publish_review` по диффу (`codetrace.code_trace`), а не по этому
    файлу. Схема на файл от этого не меняется, поэтому причина отказа
    возвращается как была.
    """
    try:
        text = (workdir / REVIEW_FILE).read_text(errors="replace").strip()
    except OSError:
        return "", f"сессия не оставила `{REVIEW_FILE}`"
    if not text:
        return "", f"`{REVIEW_FILE}` пуст"
    return text, ""


def publish_review(issue_id: str, workdir: Path, ident: str,
                   issue: dict | None = None, persona: str = "", *,
                   require_review: bool = True) -> bool:
    """Вывод ревью-субсессии — отдельным комментарием от личности ревьюера.

    Ревью внутри рабочей сессии (шаг 9 контракта) — работа другой роли, а не
    инженера. Пока его находки приезжали одним абзацем внутри отчёта
    инженера, увидеть в задаче, что ревью вообще было, можно было только на
    слово.

    Раннер не разбирает вывод сессии, а читает файл, который она обязана
    оставить, — тем же способом, что и вердикт проверки выката
    (`_verdict_line`). Файла нет или он пуст — это отказ (`review_of`), и он
    уходит в задачу помеченным комментарием: дифф едет дальше без ревью, и
    человек обязан это прочитать там же, где читает отчёт.

    Но отказ уезжает только тогда, когда ревьюить БЫЛО что, и спрашивается
    это у системы, а не у сессии (DEV-121). Ветки задачи с коммитами поверх
    базовой нет — диффа нет, `/code-review` нечего дать аргументом, и отказ
    не пишется вовсе; `codetrace.code_trace` отвечает на это по ветке и
    коммиту. Прежде вместо этого смотрели на статус задачи, и исход 4
    контракта (стратегия на согласование) проскакивал как настоящий пропуск:
    кода нет, но задача не закрыта, а ушла человеку на решение. Семь
    сообщений «Ревью не состоялось» подряд, половина из них про сессии без
    единого коммита, — и человек перестаёт читать остальные.

    Закрытая задача (исходы 2 и 3: справка, отказ) остаётся отдельной
    причиной молчать: там кода нет по самому исходу, и ветку можно не
    спрашивать. `issue` — снимок из `ensure_handoff`; нет снимка (трекер не
    ответил) — отказ не пишется: `ensure_handoff` уже сказал в журнал, что
    сдачу не проверил, и комментарий туда же не доедет.

    Неизвестность (`code_trace` вернул `None`) громкая: отказ уезжает. Ложная
    тревога стоит человеку минуты, а непроверенный дифф, выданный за
    проверенный, стоит ровно того, ради чего ревью и заведено.

    `require_review=False` — сессия разрешения конфликта (DEV-109). Дифф у
    неё есть, и `code_trace` это честно скажет, но ревьюили его в прошлый
    заход: предмет этой сессии — мерж, а не правка. Отказа поэтому нет, а
    вот текст ревью, если сессия его всё-таки оставила, публикуется как у
    всех — молчать о существующем разборе не за что.

    Ключа личности ревьюера нет — комментария с текстом ревью нет вовсе:
    вторая копия того же текста за подписью владельца ключа не показывает
    никакой мультиагентности, она просто шумит в задаче. Отказ при этом
    уходит всё равно — он не копия ничего.
    """
    text, why = review_of(workdir)
    if not text:
        if not require_review:
            log.info("%s: %s, но это заход разрешения конфликта — дифф ветки "
                     "после мержа ревьюили в прошлый заход, отказ не пишу",
                     ident, why)
            return False
        # Ровно пустая строка, а не любая ложь: `None` — это «не знаю»,
        # и молчать на нём нельзя (см. `code_trace`).
        diff = codetrace.code_trace(ident, REPOS_ROOT)
        if diff == "":
            log.info("%s: %s, но ветки задачи с коммитами нет — ревьюить нечего, "
                     "человеку не пишу", ident, why)
            return False
        if not issue:
            log.warning("%s: %s, а задачу в Linear не прочитали — отказ не пишу", ident, why)
            return False
        if _closed(issue):
            log.info("%s: %s, но задача закрыта без кода — ревью не требуется",
                     ident, why)
            return False
        found = diff or "дифф посчитать не удалось, см. журнал"
        log.warning("%s: ревью не состоялось (%s) при диффе «%s» — дифф уходит "
                    "без код-ревью, пишу отказ в задачу", ident, why, found)
        linear_comment(
            issue_id,
            f"🤖 **Ревью не состоялось:** {why}, а ревьюить было что — {found}. "
            "Дифф уходит на проверку и к человеку без код-ревью: посмотреть "
            "его придётся самому.",
            persona,
        )
        return False
    if not PERSONA_KEYS.get(personas.REVIEWER):
        # Молча тут выйти нельзя: снаружи это неотличимо от «ревью не было».
        # Отказ выше уже уехал в задачу, а вот ТЕКСТ ревью не уедет никуда —
        # и единственное место, где это видно, журнал.
        log.warning("%s: личность %s без ключа (%s) — ревью останется только "
                    "в отчёте инженера, отдельного комментария не будет",
                    ident, personas.REVIEWER, personas.key_var(personas.REVIEWER))
        return False
    if len(text) > REVIEW_COMMENT_MAX:
        text = text[:REVIEW_COMMENT_MAX] + "\n\n…обрезано, полный разбор в MR."
    linear_comment(issue_id, f"🔍 **Код-ревью по диффу ветки.**\n\n{_review_headline(text, ident)}",
                   personas.REVIEWER)
    return True


def _review_headline(text: str, ident: str) -> str:
    """Текст ревью с шапкой — или с честной пометкой, что её нет (DEV-108).

    Возврата на досдачу у ревью нет — файл читается уже после конца сессии,
    и вернуть её за шапкой некуда. Тот же случай у дежурного, и решён он так
    же (`note_incident_headline`).
    Выбор поэтому между «выкинуть ревью» и «показать как есть»: разбор диффа
    дороже своей подачи, поэтому он публикуется, а отсутствие шапки называется
    вслух — там же, первой строкой. Молча пропустить нельзя ровно по той же
    причине, по которой раннер говорит «ревью не состоялось»: неотмеченное
    неотличимо от сделанного.
    """
    misses = headline.lint_text(text, REVIEW_FILE)
    if not misses:
        return text
    log.warning("%s: в `%s` шапки нет (%s) — публикую разбор с пометкой",
                ident, REVIEW_FILE, "; ".join(misses)[:200])
    return ("> Шапки в этом ревью нет: первой строкой не стоит «что требуется "
            "от человека», и ответа двумя-тремя фразами не будет — "
            "разбираться придётся по всему тексту.\n\n" + text)


# Лента заводится со второго захода: на одном она повторяла бы сам
# комментарий, а не сворачивала переписку.
# Лента заходов выключена по умолчанию (16.09.2026, решение владельца).
# Причина: она склеивала куски чужих фраз в нечитаемый текст и уходила в
# задачу за подписью ЧЕЛОВЕКА — раннер пишет общим ключом, а не ключом
# личности. Пока и то и другое не исправлено, подача хуже её отсутствия.
# Включается явно: AGENT_TIMELINE=1.
TIMELINE_ON = os.environ.get("AGENT_TIMELINE", "").strip() in ("1", "on", "да")

# Прозрачные сессии (DEV-126): человек видит сессию в приложении и может
# войти в неё по ходу работы. Выключено по умолчанию — путь новый, а обычный
# `claude -p` держит весь конвейер. Включается явно: AGENT_TRANSPARENT=1.
#
# Разница не в флаге, а в устройстве: прозрачная сессия отвязывается сразу,
# потока не отдаёт и события итога не пишет — она не заканчивается, а встаёт
# ждать. Поэтому у неё свой присмотр (`transparent.wait`), а `_pump_stream`
# и `_wait_session` остаются для обычного пути нетронутыми.
TRANSPARENT_ON = os.environ.get("AGENT_TRANSPARENT", "").strip() in ("1", "on", "да")
# Сколько сессия может молчать, прежде чем считаем, что она спрашивает
# человека и ждёт ответа. Ноль — не ждать вовсе.
TRANSPARENT_IDLE_SEC = float(os.environ.get("AGENT_TRANSPARENT_IDLE_SEC", "300"))

TIMELINE_MIN_ROUNDS = 2


def publish_timeline(issue_id: str, ident: str, workdir: Path | None = None) -> bool:
    """Лента заходов (DEV-108) — один комментарий на задачу, правится на месте.

    Собирать её может только раннер: сессия видит свой заход и не видит
    остальных, а вопрос человека — «почему машины спорили и чем спор кончился»
    — ответа внутри одного захода не имеет.

    Пишется ОБЩИМ ключом инсталляции, без личности, и это не небрежность.
    Комментарий, созданный приложением, правит только оно же, а заходы идут
    разными личностями (работа — engineer, проверка — qa): подпиши ленту
    личностью первого захода, и второй не смог бы её обновить — вместо одной
    ленты в задаче появилось бы по ленте на роль.

    Отказ трекера ничего не ломает, и это не обещание докстроки: вся работа
    обёрнута. Лента — ПОДАЧА, а не сдача; она идёт последним действием, после
    отчёта, ревью и перевода задачи, и уронить из-за неё сессию, которая
    работу уже сдала, значило бы обменять результат на его оформление.
    """
    if not issue_id or not TIMELINE_ON:
        return False
    try:
        comments = _tracker.list_comments(issue_id)
        rounds = timeline.rounds(comments)
        if len(rounds) < TIMELINE_MIN_ROUNDS:
            return False
        body = timeline.render(comments)
        if not body:
            return False
        # Машинная часть НЕ уходит в тело комментария — уходит её адрес.
        # Дополнение владельца: «Linear — поверхность человека», и при этом
        # «ничего не удалять». Одно с другим мирит только адрес.
        where = glossary.machine_part(workdir)
        if where:
            body = f"{body}\n\n{where}"
        mine = sorted((c for c in comments
                       if str(c.get("body") or "").lstrip().startswith(timeline.MARK)),
                      key=lambda c: str(c.get("createdAt") or ""))
        if mine and mine[0].get("id"):
            # Самая старая — чтобы лента не переезжала по задаче вниз на каждом
            # заходе: человек привыкает к её месту, а не ищет её заново.
            return _tracker.update_comment(mine[0]["id"], body)
        ok = _tracker.post_comment(issue_id, body)
        if ok:
            log.info("%s: лента заходов заведена (%d заходов)", ident, len(rounds))
        return ok
    except Exception as e:
        log.warning("%s: лента заходов не собралась (%s) — сдача от этого не "
                    "страдает, но переписка машин осталась несвёрнутой", ident, e)
        return False


def fresh_workdir(root: Path, name: str) -> Path:
    """Каталог прогона, уникальный ПО ПОСТРОЕНИЮ. Создаёт его и отдаёт путь.

    Имя собирается из времени с точностью до секунды, и этого мало: 15.09.2026
    две сессии одной проверки DEV-106 стартовали с разницей в 8 мс, получили
    одно имя, а `mkdir(exist_ok=True)` молча отдал им ОДИН каталог. Они писали
    друг поверх друга `expected.md`, `gates.json`, `verdict.txt` и `stats.json`;
    запись исхода первой не осталась нигде, а `expected.md` стал смесью двух
    эталонов, внешне неотличимой от целого (DEV-110).

    Постановку чинит общий захват (`agent_factory.deploy`), но каталог обязан
    расходиться даже при ошибке постановки: пока две сессии делят каталог,
    улики уничтожаются молча, а молчание здесь дороже лишнего каталога.

    `mkdir` без `exist_ok` атомарен и между процессами — поэтому занятость
    имени узнаётся отказом, а не проверкой «есть ли каталог» перед созданием:
    между проверкой и созданием как раз и помещается вторая сессия.
    """
    root.mkdir(parents=True, exist_ok=True)
    for n in range(1, 10):
        path = root / (name if n == 1 else f"{name}-{n}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    # Девять сессий с одним именем — это уже не гонка, а что-то другое; но
    # отдать занятый каталог нельзя и здесь, поэтому имя добирается случайным
    # хвостом, а не отказом: сессия важнее красоты имени.
    path = root / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    log.warning("каталог прогона %s занят девять раз подряд — беру %s",
                name, path.name)
    return path


def _transparent_extra(cmd: list[str]) -> list[str]:
    """Флаги обычной сессии, которые нужны и прозрачной.

    `-p <промпт>`, `--output-format`, `--verbose` и `--resume` у прозрачной
    свои: первый она получает иначе, остальные к ней неприменимы. Всё прочее —
    ключ личности, потолки ходов и денег, разрешения — обязано доехать. Без
    ключа личности комментарии уходят за подписью владельца (DEV-66), без
    потолков у сессии нет ни конца, ни цены.
    """
    drop_with_value = {"-p", "--output-format", "--resume", "--model"}
    drop_alone = {"--verbose"}
    out: list[str] = []
    skip = False
    for i, item in enumerate(cmd):
        if skip:
            skip = False
            continue
        if i == 0:  # сам исполняемый файл
            continue
        if item in drop_with_value:
            skip = True
            continue
        if item in drop_alone:
            continue
        out.append(item)
    return out


def run_session(task: dict, slot: int, queue_len: int) -> None:
    ident = task.get("identifier") or "UNKNOWN"
    issue_id = task.get("issue_id") or ""
    # Тип задачи: обычная работа или проверка выката. Незнакомое значение
    # трактуем как работу — это прежнее поведение, а очередь переживает
    # деплой раннера и может содержать задачи, положенные старым кодом.
    kind = task.get("kind")
    is_verify = kind == "verify"
    is_intake = kind == "intake"
    is_incident = kind == "incident"
    # Разрешение конфликта (DEV-109): работа с кодом, но не новый круг по
    # задаче. Ставит её пересмотр стопки у приёма (`queueing.MERGE_KIND`);
    # слово одно на обе стороны, совпадение закреплено тестом.
    is_merge = kind == MERGE_KIND
    # Работа — это «всё незнакомое тоже работа», ровно как в очереди: иначе
    # задача с опечаткой в `kind` прошла бы мимо шлюза, не перестав быть
    # работой. Разрешение конфликта — тоже работа: сдача, ремонт сдачи и
    # постусловия у него те же.
    is_work = not (is_verify or is_intake or is_incident)
    # Под чьим именем эта сессия пишет в трекер. Незнакомый вид — инженер,
    # как и везде: вид задачи мог положить в очередь раннер прошлой версии.
    persona = personas.persona_of(kind or "")
    # Продолжение прерванной лимитом сессии идёт в ЕЁ рабочий каталог: там
    # лежат наработки, ради которых продолжение и затевалось, и туда же
    # привязан project scope сессии в CLI. Новый каталог означал бы новую
    # сессию рядом со старой — то есть работу с нуля.
    resume_sid = task.get("resume_session_id") or ""
    resume_dir = task.get("resume_workdir") or ""
    if resume_sid and resume_dir and Path(resume_dir).is_dir():
        workdir = Path(resume_dir)
        # Номер захода — сумма продолжений после лимита, ремонтов сдачи и
        # возвратов после красных проверок: у каждого свой счётчик, а файлы
        # прошлых заходов лежат в одном ряду.
        _keep_previous_run(workdir, (int(task.get("resume_attempt") or 0)
                                     + int(task.get("handoff_attempt") or 0)
                                     + int(task.get("ci_fix_attempt") or 0)) or 1)
    else:
        resume_sid = ""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = ("-verify" if is_verify else
                  "-intake" if is_intake else
                  "-incident" if is_incident else
                  "-merge" if is_merge else "")
        # Не `WORK_ROOT / имя`: имя занято ровно тогда, когда в ту же секунду
        # стартовала вторая сессия, и делить с ней каталог нельзя (DEV-110).
        workdir = fresh_workdir(WORK_ROOT, f"{ident}{suffix}-{stamp}")
    workdir.mkdir(parents=True, exist_ok=True)
    # Продолжение после красных проверок отмечается ДО старта процесса:
    # иначе сторож ожидания (DEV-16) решит, что оно не поднялось.
    _ci_ack_wake(task)
    link_role_memory(workdir, kind or "")
    layers = memory_layers(kind or "")
    log_path = workdir / "session.log"

    env = dict(os.environ)
    env["AGENT_ISSUE"] = ident
    # Потолки под-агентов CLI: умолчание раннера, если окружение инсталляции
    # не сказало своего. Наследуется под-агентами вместе с остальным.
    for var, value in SUBAGENT_ENV_DEFAULTS.items():
        env.setdefault(var, value)
    # Куда обвязке писать ворота (DEV-88): `logdiff.py` и `gatelog.py` берут
    # путь отсюда, а не из cwd — сессия зовёт их из любого каталога.
    env[gatelog.ENV_FILE] = str(workdir / gatelog.GATES_FILE)
    # Спрашиваем ОДИН раз на сессию и передаём дальше: делегирование могут
    # снять или переставить, пока сессия идёт час, и тогда промпт называл бы
    # одного человека, а задача уезжала бы другому.
    reviewer = current_reviewer()
    if is_verify:
        timeout_sec, max_turns = VERIFY_TIMEOUT_SEC, VERIFY_MAX_TURNS
        # Бюджет едет в промпт: срок, о котором сессия не знает, она и
        # распределить не может — только упереться в него на полуслове.
        prompt = build_verify_prompt(task, workdir, REPOS_ROOT, reviewer["name"],
                                     timeout_sec=timeout_sec, max_turns=max_turns,
                                     review_state=REVIEW_STATE_NAME,
                                     unverified_state=UNVERIFIED_STATE_NAME,
                                     progress_state=PROGRESS_STATE_NAME,
                                     done_state=DONE_STATE_NAME,
                                     unverifiable_label=UNVERIFIABLE_LABEL,
                                     max_rounds=MAX_WORK_ROUNDS,
                                     agent_hint=AGENT_NAME_HINT,
                                     memory_layers=layers)
    elif is_intake:
        # Вложения качаем ДО сессии: сессия читает их с диска, а токен бота
        # есть у раннера. Пачка с проставленными путями едет и в промпт, и
        # обратно в events-api (происхождение в тикете, возврат на отказе).
        task["items"] = download_intake_files(task.get("items") or [],
                                              workdir / "attachments")
        prompt = build_intake_prompt(task, workdir, REPOS_ROOT,
                                     known_repos=AGENT_KNOWN_REPOS,
                                     env_canon=AGENT_ENV_CANON,
                                     memory_layers=layers)
        timeout_sec, max_turns = INTAKE_TIMEOUT_SEC, INTAKE_MAX_TURNS
    elif is_merge:
        prompt = build_merge_prompt(task, workdir, REPOS_ROOT,
                                    verifying_state=VERIFYING_STATE_NAME,
                                    review_state=REVIEW_STATE_NAME,
                                    memory_layers=layers)
        timeout_sec, max_turns = SESSION_TIMEOUT_SEC, MAX_TURNS
    elif is_incident:
        prompt = build_incident_prompt(task, reviewer["name"],
                                       incident_playbook=AGENT_INCIDENT_PLAYBOOK,
                                       env_canon=AGENT_ENV_CANON,
                                       progress_state=PROGRESS_STATE_NAME,
                                       memory_layers=layers,
                                       workdir=workdir)
        timeout_sec, max_turns = INCIDENT_TIMEOUT_SEC, INCIDENT_MAX_TURNS
    else:
        prompt = build_prompt(task, workdir, REPOS_ROOT, reviewer["name"],
                              known_repos=AGENT_KNOWN_REPOS,
                              symptom_playbook=AGENT_SYMPTOM_PLAYBOOK,
                              progress_state=PROGRESS_STATE_NAME,
                              review_state=REVIEW_STATE_NAME,
                              verifying_state=VERIFYING_STATE_NAME,
                              memory_layers=layers,
                              handoff_attempts=MAX_HANDOFF_ATTEMPTS)
        timeout_sec, max_turns = SESSION_TIMEOUT_SEC, MAX_TURNS
    if resume_sid and task.get("handoff_misses"):
        # Ремонт сдачи (DEV-95): та же сессия, промпт — чего не хватило.
        prompt = build_repair_prompt(task, workdir, list(task["handoff_misses"]),
                                     int(task.get("handoff_attempt") or 1),
                                     MAX_HANDOFF_ATTEMPTS,
                                     verifying_state=VERIFYING_STATE_NAME)
    elif resume_sid and task.get("ci_failure"):
        # Продолжение после красных проверок (DEV-16): сессия ничего плохого
        # не сделала и не обрывалась — она уже сдала работу, а упал прогон,
        # которого она по контракту не ждёт. Причина и команда опроса едут в
        # текст: без них первое, что сделает продолжение, — выяснит заново
        # то, что раннер уже знает.
        prompt = ciwait.failure_prompt(ident, str(task["ci_failure"]),
                                       task.get("ci_request") or {},
                                       task.get("ci_command") or [],
                                       int(task.get("ci_fix_attempt") or 1),
                                       MAX_CI_FIX_ATTEMPTS)
    elif resume_sid:
        # Промпт продолжения, а не исходная постановка: с ней сессия начала бы
        # заново и выбросила то, ради чего мы её сохраняли. Про причину обрыва
        # говорим прямо — иначе агент решит, что это он что-то сделал не так.
        prompt = (
            f"Предыдущая сессия по {ident} оборвалась не по твоей вине — "
            f"исчерпался лимит Claude. Лимит сброшен. Продолжи ровно с того "
            f"места, где остановился: доведи задачу до конца по исходной "
            f"инструкции. Ничего не начинай заново — наработки в рабочем "
            f"дереве на месте."
        )

    mcp_config = write_mcp_config(workdir, persona)
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        *(("--mcp-config", str(mcp_config)) if mcp_config else ()),
        *(("--resume", resume_sid) if resume_sid else ()),
        "--model", CLAUDE_MODEL,
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        # Потолок в долларах старше потолка ходов: он считает и под-агентов,
        # расход которых в ходах сессии не виден вовсе.
        *(("--max-budget-usd", f"{MAX_BUDGET_USD:g}") if MAX_BUDGET_USD > 0 else ()),
        # Поток событий вместо одного финального текста: см. _pump_stream.
        "--output-format", "stream-json", "--verbose",
    ]
    # В Telegram про старт и успех не пишем: ход задачи и так виден в её
    # сообщении в чате — статус там обновляется сам. Сообщение от раннера =
    # что-то сломалось.
    what_started = ""
    if is_verify:
        what_started = " (проверка выката)"
    elif is_intake:
        what_started = f" (постановка из личики, кусков {len(task.get('items') or [])})"
    elif is_incident:
        what_started = f" (дежурный: {task.get('symptom') or '?'})"
    log.info("[slot %d] %s%s → старт, лог %s, в очереди ещё %d",
             slot, ident, what_started, log_path, queue_len)

    started = time.monotonic()
    # Отсечка для проверки сдачи: комментарий сессии - тот, что новее её старта.
    session_started_at = datetime.now(timezone.utc)
    peak_kb = [0]
    oom_before = _oom_kills()
    transcript_path = workdir / "transcript.jsonl"
    breaker = RepeatBreaker(REPEAT_BASH_LIMIT)
    halt: Halt | None = None
    _clear_stale_stop(ident)

    with log_path.open("w", buffering=1) as logfile:
        if TRANSPARENT_ON:
            # Прозрачная сессия (DEV-126): человек видит её в приложении и
            # может войти по ходу. Ниже по коду разницы нет — обёртка отдаёт
            # те же четыре вещи, что и процесс: pid, поток, ожидание, снятие.
            # Имя — то, что человек прочитает в списке: «DEV-115 · работа».
            # Флаги берём из уже собранного `cmd`, а не придумываем заново:
            # там ключ личности, потолки ходов и денег, разрешения. Соберёшь
            # их здесь — потеряешь, и сессия подпишется учёткой хоста.
            proc = transparent.attach(
                f"{ident} · {kind or 'работа'}", prompt, str(workdir),
                claude_bin=CLAUDE_BIN, model=CLAUDE_MODEL, env=env,
                resume=resume_sid, idle_sec=TRANSPARENT_IDLE_SEC,
                extra=_transparent_extra(cmd))
            if proc is None:
                # Молча уйти нельзя: задача осталась бы на агенте, у которого
                # нет входящих, и пропала бы с доски без единого слова. Именно
                # так 17.09 исчезла DEV-127 — в журнале строчка, у человека
                # ничего. Отказ запуска обязан доехать до того, кто её ставил.
                log.error("[slot %d] %s → прозрачная сессия не поднялась", slot, ident)
                if issue_id:
                    linear_comment(
                        issue_id,
                        "Не смог поднять сессию — задача не начата и ждёт тебя.\n\n"
                        "Так бывает, когда на машине не принято предупреждение о работе "
                        "без спроса разрешений, кончилось место или недоступен сам CLI. "
                        "В журнале раннера строка «прозрачная сессия не поднялась» и время.")
                    linear_assign_to_reviewer(issue_id, reviewer)
                return
            log.info("[slot %d] %s → прозрачная, %s", slot, ident,
                     proc.session.link or "ссылка ещё не выдана")
        else:
            # Своя группа процессов: всё, что породит сессия, убивается разом.
            proc = subprocess.Popen(cmd, cwd=str(workdir), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        # Трубу обязан кто-то разгребать, иначе сессия встанет на полном буфере.
        pump = threading.Thread(target=_pump_stream,
                                args=(proc.stdout, logfile, transcript_path, started,
                                      breaker),
                                daemon=True)
        pump.start()
        # Пока обёртка жива — иначе после wait() группу уже не спросить.
        # У прозрачной сессии группу НЕ берём: она общая с демоном фоновых
        # сессий и с соседними сессиями, и добивание группы снесло бы чужую
        # работу. Такую сессию тушат её собственной командой — `proc.kill`
        # у обёртки зовёт `claude stop`.
        if TRANSPARENT_ON:
            pgid = None
        else:
            try:
                pgid = os.getpgid(proc.pid)
            except (ProcessLookupError, OSError):
                pgid = None
        watch_done = threading.Event()
        watcher = threading.Thread(target=_watch_memory,
                                   args=(proc.pid, watch_done, peak_kb),
                                   daemon=True)
        watcher.start()
        try:
            # Ломтями, не одним wait: между ломтями — прерыватель и /stop.
            rc, halt = _wait_session(proc, pgid, ident, timeout_sec, breaker)
        finally:
            watch_done.set()
            watcher.join(timeout=MEM_POLL_SEC + 5)
            # Дочитываем хвост потока: последние строки — это финальный отчёт
            # сессии, ради которого лог и читают.
            pump.join(timeout=30)
            # Обёртка вышла — это ещё не значит, что вышло всё, что она
            # породила: и `claude.exe`, и его под-агенты могут пережить
            # родителя (обёртку сняли по OOM, она упала сама).
            # Такой потомок остаётся в cgroup юнита и ест общий лимит уже без
            # присмотра systemd — с OOMPolicy=continue юнит из-за него больше
            # не перезапускается. Поэтому группу добиваем на любом исходе, а
            # не только по таймауту; для дошедшей до конца сессии это no-op.
            if pgid is not None:
                _kill_session(proc, pgid)
            # Ключ личности нужен ровно на время сессии. Дальше рабочий
            # каталог живёт месяцами (уборки у него нет), и до 14.09.2026 на
            # хосте лежало 62 файла `mcp.json` с ЖИВЫМ ключом Linear — самый
            # свежий сегодняшний. Снимаем на выходе, при любом исходе.
            mask_mcp_config(mcp_config)

    took = int(time.monotonic() - started)
    peak_mb = peak_kb[0] // 1024
    oom_after = _oom_kills()
    hit_oom = (oom_before is not None and oom_after is not None
               and oom_after > oom_before)
    # Всё, из чего складывается исход, собираем ДО записи статистики: файл
    # пишется один раз, и дописывать в него «а на самом деле» из следующих
    # веток значит завести второй источник правды про один запуск. Хвост лога
    # и verdict.txt читаются дёшево (двадцать строк и одна), а ветки ниже
    # переиспользуют уже прочитанное, а не читают заново.
    verdict = _accepted_verdict(workdir, _verdict_line(workdir)) if is_verify else ""
    # Тот же файл и тот же разбор, но без `_accepted_verdict`: эталон
    # (`expected.md`) — требование к проверке выката, у дежурного его нет и
    # быть не может — он сверяется с проснувшимся продом, а не с критерием.
    incident_verdict = _verdict_line(workdir) if is_incident else ""
    criterion = (_criterion_source(workdir) or "") if is_verify else ""
    tail = _log_tail(log_path) if rc != 0 else ""
    # Снятую нами сессию не лечить как лимит и не возвращать в очередь: она
    # не упала — её остановили, и причина известна без хвоста лога.
    limit_reset = _limit_reset(tail) if rc != 0 and halt is None else None
    # Тот же вопрос, что решает ветка отложенной очереди ниже, и те же
    # слагаемые: лимит — не поломка ТОЛЬКО пока есть чем и куда продолжать.
    # Исчерпанные попытки — это возврат человеку, и в журнале он обязан
    # выглядеть возвратом, а не ожиданием.
    resume_try = int(task.get("resume_attempt") or 0) + 1
    resume_sid = _session_id(workdir) if limit_reset is not None else None
    waiting_limit = bool(limit_reset is not None and not is_intake and resume_sid
                         and resume_try <= MAX_RESUME_ATTEMPTS)
    # Заявка на ожидание проверок (DEV-16) — читается ЗДЕСЬ, до статистики и
    # до освобождения ветки: ждать прогон, отпустив рабочее дерево, значит
    # остаться без места, куда возвращать сессию, если прогон покраснеет.
    # Файл снимается при чтении, годная заявка или нет.
    ci_req = None
    if issue_id and not (is_verify or is_intake or is_incident):
        ci_req = _ci_request(workdir, issue_id, ident, persona)
    waiting_ci = False
    if ci_req is not None:
        # Принимаем заявку от сессии, которая дошла до конца сама (rc 0) или
        # которую сняли МЫ по сроку (rc None без Halt): контракт обещает ей
        # «заявка переживает твой выход» — и это ровно случай из заголовка
        # DEV-16, пуш на 55-й минуте. Снятая человеком, прерывателем, упавшая
        # или упёршаяся в лимит — идёт своим путём: лимит продолжит сессию, и
        # она перепишет заявку сама; остальные — к человеку с причиной.
        # Прозрачная сессия встала и ждёт человека (DEV-126). Это НЕ конец
        # работы и не провал: она жива, помнит всё и продолжит с того же
        # места, как только он ответит. Молчать тут нельзя — сессию через
        # несколько минут снимет подметание, и человек не узнает, что его
        # ждали. Поэтому: одна строка в задачу, задача на него, кнопка.
        if isinstance(proc, transparent.Attached) and getattr(proc, "_ended", "") == "ждёт":
            log.info("[slot %d] %s → стоит и ждёт человека", slot, ident)
            if issue_id:
                linear_comment(
                    issue_id,
                    f"Стою и жду тебя: без твоего слова дальше не еду.\n\n"
                    f"Сессия называется «{ident} · {kind or 'работа'}» — найди её в "
                    f"приложении и скажи, что делать. Я помню всё, что успела, и "
                    f"продолжу с того же места.")
                linear_assign_to_reviewer(issue_id, reviewer)
            return

        clean_exit = rc == 0 or (rc is None and halt is None)
        if waiting_limit or not clean_exit:
            log.warning("[slot %d] %s → заявка на ожидание проверок оставлена, но "
                        "сессия не дошла до конца сама (%s) — заявка не принята",
                        slot, ident, "лимит Claude" if waiting_limit else
                        "снята" if halt else f"код {rc}")
            linear_comment(
                issue_id,
                "🤖 Сессия оставила заявку на ожидание проверок, но не дошла до "
                "конца сама (причина — в следующем комментарии). Заявка не "
                "принята: проверки никто не досматривает. Продолжение сессии, "
                "если оно будет, перепишет заявку само.", persona)
        else:
            # Снимок трекера и отсечка — те же, что у сдачи по схеме ниже:
            # по ним сверяется, что сессия оставила до заявки.
            ci_issue = _handoff_state(issue_id)
            waiting_ci = _ci_enter(task, workdir, log_path, persona, ci_req,
                                   cut_off=rc is None, issue=ci_issue,
                                   since=_handoff_since(task, session_started_at))
            if not waiting_ci:
                linear_comment(
                    issue_id,
                    "🤖 Сессия оставила заявку на ожидание проверок, но раннер "
                    "не смог её записать ни в redis, ни на диск (см. журнал). "
                    "Проверки никто не досмотрит — посмотри их сам; задача "
                    "сдаётся как есть.", persona)
    # Ворота — детерминированный вход исхода (DEV-88): читаются один раз,
    # здесь же, и ложатся в статистику рядом с исходом, который по ним посчитан.
    gates = _gates_of(workdir, task) if is_verify else None
    outcome = outcome_of(rc=rc, kind=kind or "work", verdict=verdict,
                         waiting_limit=waiting_limit, gates=gates or [],
                         halted=halt.kind if halt else "", waiting_ci=waiting_ci)
    # Класс расхождения (DEV-89) — в статистику, откуда его прочтут ворота
    # следующей постановки. Слово, которого раннер не знает, — не класс:
    # считаем по голове и говорим об этом, чтобы опечатка не сошла за
    # «поле не заполнено».
    discrepancy = verdict_class(verdict) if is_verify else None
    if is_verify and verdict and _class_word(verdict) \
            and _class_word(verdict) not in _CLASS_WORDS:
        log.warning("[slot %d] %s → класс расхождения %r не разобран (знаю %s) — "
                    "считаю по голове: %s", slot, ident, _class_word(verdict),
                    "/".join(VERDICT_CLASSES), discrepancy or "без класса")
    if is_verify and verdict and _verdict_head(verdict) in _PASSING_HEADS \
            and outcome != "VERIFIED":
        # Вердикт говорит «прошло», ворота — нет. Молчать нельзя: в чате
        # уедет ✅ по голове вердикта (голова пока за моделью), и без этой
        # строки расхождение видно только в stats.json.
        state, why = gatelog.judge(gates or [])
        log.warning("[slot %d] %s → вердикт %r, но ворота: %s (%s) — в «проверено» "
                    "не записан", slot, ident, _verdict_head(verdict), state, why)
    _write_stats(workdir, slot=slot, ident=ident, kind=kind or "work", took=took,
                 peak_mb=peak_mb, rc=rc, criterion=criterion,
                 gate_criterion=(str(task.get("criteria_source") or "")
                                 if is_verify else ""),
                 outcome=outcome, gates=gates, discrepancy=discrepancy,
                 mode=str(task.get("trigger") or "") if is_verify else "",
                 halt=halt.reason if halt else "",
                 # Шлюз — только у работы: у проверки, приёма и дежурного его
                 # в контракте нет, и пустой разбор в их статистике читался бы
                 # как замечание к ним.
                 gate_plan=plan.summary(workdir) if is_work else None)
    if waiting_ci:
        # Дальше — за подметальщиком: ветка остаётся занятой (туда вернётся
        # продолжение на красном), задача остаётся в работе на агенте — это и
        # значит «ожидание». Ревью — как на обычной сдаче: файл сессия
        # оставила (или нет) до заявки; у разрешения конфликта его не
        # спрашивают вовсе (ниже).
        log.info("[slot %d] %s → ожидание проверок за раннером (сессия %s за "
                 "%dс, %s)", slot, ident,
                 "дошла до конца" if rc == 0 else "снята по сроку", took,
                 _peak_note(peak_mb))
        # Та же оговорка, что и на обычной сдаче ниже: с захода разрешения
        # конфликта ревью не спрашивают, а оставленное — публикуют.
        publish_review(issue_id, workdir, ident, ci_issue, persona,
                       require_review=not is_merge)
        return
    if rc == 0:
        # Ветку освобождаем сразу: следующий заход по этой же задаче (доработка
        # после ревью) должен суметь продолжить в ней, а не заводить вторую.
        release_worktrees(workdir)
        log.info("[slot %d] %s → готово за %dс, %s",
                 slot, ident, took, _peak_note(peak_mb))
        if is_verify:
            report_verdict(task, verdict, log_path)
            if issue_id:
                # Отчёт в задаче — по факту, а не по рассказу сессии: см.
                # ensure_verify_handoff. Строго ДО маршрута: тот сам оставляет
                # помеченный комментарий, и положенный раньше он сошёл бы за
                # отчёт молчаливой проверки.
                ensure_verify_handoff(issue_id, ident, log_path, session_started_at,
                                      verdict, persona)
                # Состояние и исполнитель — по правилу: см. ensure_verdict_route.
                # Пустой вердикт сюда тоже попадает — это «не принято», и в
                # трекере оно обязано выглядеть так же.
                ensure_verdict_route(issue_id, ident, task, verdict, criterion,
                                     reviewer, persona, gates=gates)
                # Лента — последней: она сворачивает всё, что уже легло в
                # задачу, включая вердикт этого захода.
                publish_timeline(issue_id, ident, workdir)
        elif is_intake:
            report_intake(task, workdir)
        elif is_incident:
            # Вердикт — ПЕРВЫМ делом и без оглядки на трекер: он машиночитаемый
            # итог дежурства (шаг 11 контракта), и человеку он нужен сразу, а
            # не после того, как раннер сходит в Linear сверять исполнителя.
            report_incident_verdict(task, incident_verdict, log_path)
            # Не ensure_handoff: тот требует ровно статус приёмки + reviewer,
            # а у дежурного законных исходов два (см. ensure_incident_handoff).
            # Вердикт её не заменяет: файл говорит, ЧЕМ кончилось дежурство,
            # досдача — дошла ли задача до человека. Сессия может оставить одно
            # без другого, и тогда второе всё равно обязано сработать.
            if issue_id:
                ensure_incident_handoff(issue_id, ident, log_path, reviewer,
                                        session_started_at, persona)
        elif issue_id:
            # Штатный выход - ещё не сданная задача. Сдача сверяется по схеме
            # (DEV-95): файл, комментарий, финал контракта. Не сошлось —
            # список недостающего возвращается сессии, за неё не дописывают;
            # потолок — человеку. Снимок трекера пуст — судить не по чему,
            # и это не промах сессии: дальше по прежнему пути.
            since = _handoff_since(task, session_started_at)
            issue = _handoff_state(issue_id)
            # Шлюза тяжёлой задачи у разрешения конфликта нет: его предмет —
            # чужой мерж, а не объём работы, и требовать `plan.md` с
            # вердиктом о тяжести значило бы гонять сессию на ремонт за файл,
            # которого её контракт не просит.
            misses = (handoff_misses(issue, workdir, since, reviewer,
                                     require_plan=not is_merge)
                      if issue else [])
            if misses:
                repair_handoff(task, workdir, slot, ident, issue_id, issue, misses,
                               since, reviewer, persona, log_path)
                return
            issue = ensure_handoff(issue_id, ident, log_path, reviewer, since,
                                   persona, issue=issue)
            # Ревью — строго ПОСЛЕ досдачи. `ensure_handoff` считает сдачей
            # любой свежий комментарий в задаче; положенный раньше него
            # комментарий ревьюера сошёл бы за отчёт сессии, и молчаливая
            # сессия перестала бы ловиться — ровно тот случай, ради которого
            # досдача и заведена (TASK-176, TASK-204). Снимок задачи — оттуда
            # же: по нему отказ «ревью не состоялось» не пишется на исход
            # без кода.
            # С захода разрешения конфликта ревью не спрашивают: дифф ветки
            # против основной после мержа — та же работа, которую ревьюили в
            # прошлый заход, и отказ «дифф уходит без ревью» писался бы на
            # каждый конфликт. Дифф у такой сессии есть, поэтому спросить
            # `code_trace` (DEV-121) здесь мало — род захода знает только раннер.
            publish_review(issue_id, workdir, ident, issue, persona,
                           require_review=not is_merge)
            # Лента — после отчёта и ревью, чтобы свернуть и их (DEV-108).
            publish_timeline(issue_id, ident, workdir)
        return

    # Проверка, оставившая вердикт, — состоявшаяся проверка, чем бы ни
    # кончилась сама сессия. Вердикт пишется по шагу 9, после того как задача
    # уже прокомментирована и переведена: дошли до него — работа сделана, а
    # сессию сняли на выходе (срок, ходы, обрыв). Списать это на «сессия не
    # уложилась» значило бы выбросить готовый результат и вернуть человеку
    # задачу, которая уже закрыта, — то есть соврать про исход в обе стороны.
    if is_verify and verdict:
        why = "OOM" if hit_oom else ("таймаут" if rc is None else f"код {rc}")
        log.warning("[slot %d] %s → сессия оборвалась (%s), но вердикт записан "
                    "— берём его (%s)", slot, ident, why, _peak_note(peak_mb))
        report_verdict(task, verdict, log_path)
        if issue_id:
            ensure_verify_handoff(issue_id, ident, log_path, session_started_at,
                                  verdict, persona)
            ensure_verdict_route(issue_id, ident, task, verdict, criterion,
                                 reviewer, persona)
        return

    # Сессия не дошла до конца — сама она об этом уже не расскажет, поэтому
    # объясняем причину в Linear и возвращаем задачу автору. Причина — текст
    # для человека, а не исход из словаря: `tail` и `limit_reset` для неё уже
    # прочитаны выше, там же, где считался машиночитаемый исход.
    if halt is not None:
        # Сняли мы, и причина известна без диагноза по логу — раньше OOM и
        # таймаута: снятая группа даёт rc None, и без этой ветки останов
        # читался бы как «не уложилась в таймаут».
        if halt.kind == HALT_HUMAN:
            who = f" ({halt.by})" if halt.by else ""
            reason = f"остановлена командой /stop{who}: {halt.reason}"
        else:
            reason = f"снята прерывателем: {halt.reason}"
    elif hit_oom:
        # Именно счётчик ядра, а не код возврата: OOM-killer снимает дочерний
        # `claude.exe`, а процесс-обёртка после этого возвращает обычный код,
        # по которому диагноз не поставить. Счётчик общий на юнит — если в
        # соседнем слоте шла своя задача, там будет та же пометка.
        # OOM проверяется ПЕРВЫМ, раньше таймаута: осиротевшая обёртка после
        # убитого потомка может и не выйти вовсе, и тогда исход выглядит как
        # «не уложилась в таймаут». Списать на время то, что случилось по
        # памяти, — отправить человека крутить таймаут вместо расхода памяти.
        how = "не дождались выхода" if rc is None else f"код {rc}"
        reason = (f"снята по нехватке памяти (OOM в cgroup юнита, {how}); "
                  f"лимит задан MemoryMax в agent-runner.service и общий на "
                  f"оба слота")
    elif rc is None:
        reason = "не уложилась в таймаут"
    elif limit_reset is not None:
        # Исчерпанный лимит — не поломка, и повторять до сброса бесполезно.
        # Человеку нужны ровно два факта: что кончился лимит и когда он будет.
        reason = ("исчерпан лимит Claude"
                  + (f", сброс в {limit_reset}" if limit_reset else
                     " (время сброса в логе не названо)"))
    else:
        hint = _log_hint(tail)
        reason = f"завершилась с кодом {rc}" + (f": {hint}" if hint else "")
    # Через _peak_note, а не сырым числом: иначе журнал печатает «пик памяти
    # 0 МБ» рядом с «снята по нехватке памяти», а комментарий в Linear про ту
    # же сессию — «памяти замерить не успели».
    log.error("[slot %d] %s → %s (%s)", slot, ident, reason, _peak_note(peak_mb))
    if is_intake:
        # Задачи в Linear ещё нет — комментировать и переназначать нечего.
        # Человек ждёт ответ в мессенджере, и молчание он прочитает как
        # «бот сломался», не отличив от «я не то нажал». Отвечаем туда же.
        # Причина — не приговор постановке: успела ли сессия её записать,
        # решает `report_intake` по файлу, и готовую он отдаст вместо причины.
        report_intake(task, workdir, error=f"{reason}; лог: {log_path}",
                      limit=limit_reset is not None, limit_reset=limit_reset or "")
        return
    # Обрыв проверки выката — не обрыв работы. Общая формулировка читалась бы
    # как «сессия по задаче упала» на задаче, которая уже сделана и принята.
    # Обрыв дежурного — отдельно и по той же причине наоборот: это САМАЯ
    # срочная из трёх меток (инцидент мог остаться незакрытым), и «Автономная
    # сессия» читалась бы как рядовой упавший бэклог, а не как то, что нужно
    # проверить прямо сейчас.
    what = ("Сессия проверки выката" if is_verify else
            "Дежурная сессия" if is_incident else "Автономная сессия")

    # Сбой инфраструктуры возвращаем в очередь, а не человеку: работы по
    # существу не было, и переназначение выглядело бы как сдача (см.
    # _INFRA_MARKERS выше). Задачу при этом НЕ трогаем — она остаётся на
    # агенте, чтобы её было видно как незакрытую, пока идёт повтор.
    # Исчерпанный лимит: не поломка и не вина задачи — просто рано. Работа
    # сессии цела в её транскрипте, поэтому возвращаем не задачу «с нуля», а
    # продолжение той же сессии после сброса. Человека не трогаем: ему нечего
    # тут делать, кроме как ждать вместе с нами.
    #
    # Intake сюда не попадает: у него задачи в Linear ещё нет, человек ждёт
    # ответ в мессенджере, и молчание на час он прочитает как поломку.
    if waiting_limit:
        at = _reset_at(limit_reset)
        blind = at is None
        if blind or at - time.time() > MAX_DEFER_SEC:
            at = time.time() + LIMIT_BLIND_WAIT_SEC
            blind = True
        deferred = dict(task, resume_attempt=resume_try,
                        resume_session_id=resume_sid,
                        resume_workdir=str(workdir))
        if _defer(deferred, at):
            when = datetime.fromtimestamp(at, timezone.utc).strftime("%H:%M UTC")
            log.info("[slot %d] %s → лимит Claude, продолжу сессию после %s "
                     "(попытка %d из %d)", slot, ident, when, resume_try,
                     MAX_RESUME_ATTEMPTS)
            if issue_id:
                linear_comment(
                    issue_id,
                    f"🤖 {what} остановлена не по своей вине: {reason}.\n\n"
                    f"Наработки сессии целы — вернусь к ней после "
                    f"{when}{' (время сброса в логе не названо, жду вслепую)' if blind else ''} "
                    f"и продолжу с места остановки, а не с нуля "
                    f"(попытка {resume_try} из {MAX_RESUME_ATTEMPTS}). "
                    f"Вмешательства не требуется.\n\n"
                    f"Лог на хосте: `{log_path}`",
                    persona,
                )
            notify(f"⏳ {ident}: {reason} — продолжу после {when} "
                   f"(попытка {resume_try}/{MAX_RESUME_ATTEMPTS})\n{task.get('url', '')}")
            return

    attempt = int(task.get("requeue_attempt") or 0) + 1
    if (halt is None and _is_infra_failure(tail) and attempt <= MAX_REQUEUE_ATTEMPTS
            and _requeue(task, attempt)):
        log.info("[slot %d] %s → сбой инфраструктуры, возвращена в очередь "
                 "(попытка %d из %d)", slot, ident, attempt, MAX_REQUEUE_ATTEMPTS)
        if issue_id:
            linear_comment(
                issue_id,
                f"🤖 {what} прервалась не по своей вине: {reason}.\n\n"
                f"Работа не начиналась по существу — задача возвращена в "
                f"очередь, попытка {attempt} из {MAX_REQUEUE_ATTEMPTS}. "
                f"Вмешательства не требуется.\n\n"
                f"Лог на хосте: `{log_path}`",
                persona,
            )
        notify(f"🔁 {ident}: {reason} — вернул в очередь "
               f"(попытка {attempt}/{MAX_REQUEUE_ATTEMPTS})\n{task.get('url', '')}")
        return

    if issue_id:
        linear_comment(
            issue_id,
            f"🤖 {what} {reason} (за {took // 60} мин, {_peak_note(peak_mb)}).\n\n"
            + ("Работа тут ни при чём: не доехала сама проверка того, что "
               "выкат состоялся.\n\n" if is_verify else "")
            + (f"Возвраты в очередь исчерпаны ({MAX_REQUEUE_ATTEMPTS}).\n\n"
               if attempt > MAX_REQUEUE_ATTEMPTS else "")
            + f"Лог на хосте: `{log_path}`\n\n"
            + (f"```\n{tail}\n```" if tail else ""),
            persona,
        )
        hand_failed_to_human(issue_id, ident, reviewer)
    kind_prefix = ("проверка выката — " if is_verify else
                   "дежурный — " if is_incident else "")
    notify(f"⚠️ {ident}: {kind_prefix}{reason}"
           f"\n{task.get('url', '')}")


# ── очередь ────────────────────────────────────────────────────────────────

def worker(slot: int, client: redis.Redis) -> None:
    while not _stop.is_set():
        try:
            item = client.brpop(QUEUE_KEY, timeout=5)
        except redis.TimeoutError:
            # Пустой круг ожидания, а не сбой. redis-py 8 отдаёт исключение
            # там, где 5.x возвращал None: с ним холостой опрос писал в лог
            # ошибку каждые пять секунд, и настоящее падение redis утонуло бы
            # в этом шуме.
            continue
        except redis.RedisError as e:
            log.error("redis brpop failed: %s", e)
            _stop.wait(5)
            continue
        if not item:
            continue
        try:
            task = json.loads(item[1])
        except ValueError:
            log.error("мусор в очереди, пропускаю: %r", item[1][:200])
            continue
        try:
            queue_len = client.llen(QUEUE_KEY)
        except redis.RedisError:
            queue_len = -1
        try:
            run_session(task, slot, queue_len)
        except Exception:
            # Слот важнее одной задачи: воркер не должен умереть вместе с ней.
            log.exception("сессия %s упала неожиданно", task.get("identifier"))


def sweeper(client: redis.Redis) -> None:
    """Возвращает в очередь задачи, чей срок ожидания вышел, и досматривает
    внешние проверки по тем задачам, что ждут их исхода (DEV-16).

    Отдельным потоком, а не в воркере: воркер сидит в `brpop` и, пока очередь
    пуста, не просыпается — то есть именно в тот момент, когда возвращать и
    надо, спросить некому.

    Ожидание проверок — здесь же, а не отдельным потоком: оба дела редкие,
    оба про «посмотреть, не созрело ли», и лишний поток на хосте с двумя
    слотами дороже, чем секунды на опрос форжа. Сверка redis с диском —
    первым делом при старте (рестарт раннера, перезагрузка хоста) и дальше
    раз в CI_WAIT_RECONCILE_SEC.
    """
    last_reconcile = 0.0
    while not _stop.is_set():
        moved = _release_deferred(client)
        if moved:
            log.info("отложенная очередь: вернул %d задач(и) после сброса лимита",
                     moved)
        now = time.time()
        if now - last_reconcile >= CI_WAIT_RECONCILE_SEC:
            last_reconcile = now
            try:
                restored = _ci_recover(client, now)
                if restored:
                    log.info("сверка ожиданий проверок: вернул в строй %d", restored)
            except Exception:
                log.exception("сверка ожиданий проверок сорвалась")
        _ci_wait_tick(client, now)
        _stop.wait(30)


def _harness_version() -> str:
    """Версия ядра, которой собран ЭТОТ раннер: отметка, положенная
    установщиком рядом с кодом.

    Контракт (`prompt.py`) читается один раз при старте, поэтому «какой
    версией собрана сессия» — вопрос не про то, что сейчас лежит на диске, а
    про то, что лежало в момент старта раннера. Без этой строки в журнале
    прошлые сессии нельзя размечать: неизвестно, тем ли контрактом они
    работали.
    """
    try:
        with open(Path(__file__).resolve().parent / ".installed-version") as f:
            return f.read().strip()[:8] or "неизвестна"
    except OSError:
        return "неизвестна"


def _own_unit() -> str:
    """Имя своего systemd-юнита. Пусто — запущены не под systemd."""
    try:
        with open("/proc/self/cgroup") as f:
            raw = f.read()
    except OSError:
        return ""
    for part in raw.replace("\n", "/").split("/"):
        if part.endswith(".service"):
            return part
    return ""


def _warn_if_stop_timeout_kills_sessions() -> int:
    """Хватит ли юниту терпения дождаться слива, или он убьёт сессию.

    По SIGTERM раннер перестаёт брать из очереди и ждёт уже запущенные
    (`worker` смотрит `_stop`, `main` делает `join(SESSION_TIMEOUT_SEC)`).
    Если `TimeoutStopSec` юнита меньше этого срока, systemd досылает SIGKILL
    по всей cgroup — сессия исчезает вместе с записью о цене и ходах, и
    заметить это можно только по дырке в учёте задним числом.

    До 14.09.2026 на обоих хостах стояло 300 при бюджете 3600, и четырёх
    запусков (все проверочные — у проверки бюджет длиннее всех) просто не
    стало. Поэтому несоответствие обязано быть слышно на старте, а не через
    неделю по пропавшим строкам. Возвращает потолок в секундах (0 — не
    выяснили), чтобы это можно было проверить тестом.
    """
    unit = _own_unit()
    if not unit:
        return 0
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "TimeoutStopUSec", "--value", unit],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 0
    raw = (out.stdout or "").strip()
    stop_sec = _dur_to_sec(raw)
    if not stop_sec:
        return 0
    need = max(SESSION_TIMEOUT_SEC, VERIFY_TIMEOUT_SEC, INTAKE_TIMEOUT_SEC,
               INCIDENT_TIMEOUT_SEC)
    if stop_sec <= need:
        log.error("TimeoutStopSec юнита %s = %dс, а самая длинная сессия "
                  "идёт до %dс: рестарт во время сессии добьёт её SIGKILL'ом "
                  "и запись о цене и ходах пропадёт. Поднять в юните "
                  "(STOP_TIMEOUT в installation.conf) и daemon-reload",
                  unit, stop_sec, need)
    else:
        log.info("слив: у юнита %s есть %dс на остановку при бюджете сессии "
                 "до %dс", unit, stop_sec, need)
    return stop_sec


def _switch_on(raw) -> bool:
    """`1`/`0` из окружения → включена ли точка проверки. Не задано —
    включена: так было до выключателей."""
    return str(raw or "").strip().casefold() not in SWITCH_OFF


def _report_verify_points() -> dict[str, bool]:
    """Сказать на старте, какие точки проверки включены.

    Выключенная точка молчит по замыслу — сессии нет, вердикта нет, в
    очереди пусто. Единственное место, где эту тишину видно заранее, —
    стартовая строка раннера. Обе выключены — отдельная громкая строка:
    фабрика работает без проверки вообще, и это должно бросаться в глаза,
    а не выясняться по отсутствию вердиктов через неделю. Возвращает
    раскладку, чтобы её можно было проверить тестом.
    """
    before = _switch_on(os.environ.get(VERIFY_BEFORE_MERGE_VAR))
    after = _switch_on(os.environ.get(VERIFY_AFTER_MERGE_VAR))
    if not before and not after:
        log.warning("ПРОВЕРКИ ВЫКЛЮЧЕНЫ ОБЕ (%s=0, %s=0): фабрика работает без "
                    "проверки вообще — ни до мержа, ни после; вердиктов не будет",
                    VERIFY_BEFORE_MERGE_VAR, VERIFY_AFTER_MERGE_VAR)
    else:
        word = {True: "включена", False: "выключена"}
        log.info("проверки: до мержа %s (%s=%d), после мержа %s (%s=%d)",
                 word[before], VERIFY_BEFORE_MERGE_VAR, int(before),
                 word[after], VERIFY_AFTER_MERGE_VAR, int(after))
    return {"before": before, "after": after}


CODE_REVIEW_SKILL = "code-review"


def _report_ceilings() -> dict[str, str]:
    """Потолки этой инсталляции — в журнал при старте (DEV-91): без строки
    «прерыватель выключен» и «под-агенты без потолка» замечают по счёту."""
    got = {var: os.environ.get(var, "").strip() or f"{value} (умолчание раннера)"
           for var, value in SUBAGENT_ENV_DEFAULTS.items()}
    log.info("потолки: под-агенты — глубина %s, одновременно %s; прерыватель — %s; "
             "команда /stop — ключ %s:<задача>, опрос раз в %gс",
             got["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"],
             got["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"],
             (f"{REPEAT_BASH_LIMIT} одинаковых Bash подряд" if REPEAT_BASH_LIMIT > 0
              else "ВЫКЛЮЧЕН (AGENT_REPEAT_BASH_LIMIT=0)"),
             STOP_KEY_PREFIX, STOP_POLL_SEC)
    return got


def _code_review_skill_path() -> Path:
    """Где Claude CLI ищет `/code-review` для сессий этого раннера.

    Сессия наследует окружение юнита (`env = dict(os.environ)`), значит её
    пользовательские скиллы — `~/.claude/skills/` пользователя юнита, а не
    HOME_DIR. `CLAUDE_CONFIG_DIR` переносит каталог целиком — учитываем.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    root = Path(base) if base else Path.home() / ".claude"
    return root / "skills" / CODE_REVIEW_SKILL / "SKILL.md"


def _report_code_review_skill() -> Path | None:
    """Сказать на старте, есть ли `/code-review`, которым командует контракт.

    Шаг 9 контракта рабочей сессии зовёт `/code-review` и запрещает вторую
    сессию ради ревью. Сам скилл — файл вне продукта: на первых хостах он
    лежал символьной ссылкой в личный клон владельца, и установщик о нём не
    знал (DEV-93). Без файла сессия не падает — идёт дальше без ревью, и
    единственный след — отсутствие review.md, которое читается как «ревью не
    было». Поэтому отсутствие обязано быть слышно здесь, на старте, а не
    через неделю по пустым каталогам.

    Ошибка, а не отказ: проверки, приём и дежурство скиллом не пользуются,
    и ронять ради него раннер — потерять инциденты. Возвращает путь, если
    скилл на месте, иначе None — чтобы проверялось тестом.
    """
    path = _code_review_skill_path()
    if path.is_file():
        where = str(path)
        if path.parent.is_symlink():
            where = f"{path} → {path.parent.resolve()}"
        log.info("скилл /%s: есть (%s)", CODE_REVIEW_SKILL, where)
        return path
    log.error("скилла /%s нет (%s): рабочие сессии пойдут без ревью и review.md "
              "не оставят. Поставить установщиком (install.sh кладёт его из "
              "core/skills/) и перезапустить", CODE_REVIEW_SKILL, path)
    return None


def _dur_to_sec(raw: str) -> int:
    """`5min`, `1h 10min`, `4200s`, `infinity` → секунды. Мусор → 0.

    systemd печатает срок словами и несколькими единицами сразу, а не
    числом, поэтому разбирать приходится здесь.
    """
    raw = (raw or "").strip()
    if raw == "infinity":
        return 10 ** 9
    units = {"us": 1e-6, "ms": 1e-3, "s": 1, "sec": 1, "min": 60, "m": 60,
             "h": 3600, "d": 86400, "w": 604800, "y": 31557600}
    total, i, seen = 0.0, 0, False
    while i < len(raw):
        if not raw[i].isdigit():
            i += 1
            continue
        j = i
        while j < len(raw) and raw[j].isdigit():
            j += 1
        k = j
        while k < len(raw) and raw[k].isalpha():
            k += 1
        unit = raw[j:k]
        if unit not in units:
            return 0
        total += int(raw[i:j]) * units[unit]
        seen = True
        i = k
    return int(total) if seen else 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    # После basicConfig, а не на импорте: до неё у корневого логгера нет
    # обработчиков, INFO отбрасывается вовсе (проверено по журналу
    # инсталляции — строки о личностях в нём не было ни разу), а WARNING
    # уходит сырым в stderr мимо формата.
    _report_personas()
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError as e:
        log.error("redis недоступен (%s): %s", REDIS_URL, e)
        return 1

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())

    log.info("старт: слотов %d, очередь %s, таймаут сессии %dс, контракт %s",
             MAX_SLOTS, QUEUE_KEY, SESSION_TIMEOUT_SEC, _harness_version())
    _warn_if_stop_timeout_kills_sessions()
    _report_verify_points()
    _report_code_review_skill()
    _report_ceilings()
    threads = [threading.Thread(target=worker, args=(i + 1, client), daemon=True)
               for i in range(MAX_SLOTS)]
    threads.append(threading.Thread(target=sweeper, args=(client,), daemon=True))
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads) and not _stop.is_set():
        time.sleep(1)
    _stop.set()
    for t in threads:
        t.join(timeout=SESSION_TIMEOUT_SEC)
    log.info("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
