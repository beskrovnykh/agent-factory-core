"""Ожидание внешних проверок (CI/CD) как состояние раннера, а не сессии (DEV-16).

Почему это здесь, а не в промпте. `claude -p` — не интерактивная сессия:
фоновый монитор живёт внутри её процесса, и когда процесс дошёл до конца,
будить некого. План агента «подожду, меня разбудят» структурно невыполним, и
ни агент, ни раннер этого не замечали: процесс выходил с кодом 0, раннер писал
`→ готово`, а в задаче оставалось одно «Взялся» (TASK-218 04.09.2026,
TASK-240 08.09.2026 — вторая не дождалась зелёного пайплайна 45 секунд).

Второе, из-за чего инструкцией это не лечится: час сессии смешивает два разных
времени — работу модели и ожидание чужой системы. На большом проекте полный
прогон идёт 30-60 минут, и задача обрывается на полуслове, хотя код и ревью
готовы. Исходный случай DEV-16: сессия провисела 35-40 минут без единого
сигнала наружу, владелец починил прод руками быстрее.

Поэтому ожидание вынесено наружу. Сессия дописывает в свой рабочий каталог
`ci-wait.json` («я запушила вот эту ветку, проверки идут вот тут») и выходит —
слот модели свободен. Дальше ветку досматривает подметальщик раннера: опрос
стоит один-два subprocess раз в несколько минут и не держит ни слота, ни
памяти.

Этот модуль — только чистая логика: разбор заявки, сборка команд опроса,
разбор их вывода и решение «ждать / зелено / красно / пропал / не достучались».
Всё, что ходит в сеть, в redis и в трекер, живёт в runner.py.
"""
from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

# Имя файла-заявки. Пишет сессия (шаг 10 контракта), читает раннер — один раз,
# после чего файл снимается: заявка одноразовая.
REQUEST_FILE = "ci-wait.json"
# Машиночитаемое состояние ожидания — рядом с заявкой, перезаписывается на
# каждом переходе. Это вторая копия записи (первая — в redis): по ней ожидание
# восстанавливается, когда redis потерял ключ, и по ней же снаружи видно,
# ЧТО ждём, СКОЛЬКО уже и КОГДА сдадимся. Открытый MR сам по себе успехом не
# считается: исход задачи должен быть читаем машиной.
STATE_FILE = "ci-wait-state.json"

# Состояния ожидания. WORKING здесь нет намеренно: пока сессия жива, этот
# модуль не участвует, и заводить для неё имя значило бы врать про то, что
# раннер её состояние отслеживает.
WAITING = "WAITING_CI"          # ждём исхода проверок
WAKING = "CI_WAKING"            # красно, продолжение сессии положено в очередь
PASSED = "CI_PASSED"            # зелено — задача ушла на проверку
FAILED = "CI_FAILED"            # красно — терминально только когда чинить некому
TIMEOUT = "CI_TIMEOUT"          # срок ожидания вышел, исход неизвестен
NOT_FOUND = "CI_NOT_FOUND"      # прогон не появился или пропал
CLOSED = "CI_CLOSED"            # MR закрыт/влит или ветка удалена, пока ждали
WAKE_LOST = "CI_WAKE_LOST"      # продолжение сессии не поднялось
UNREACHABLE = "CI_UNREACHABLE"  # форж не отвечает — переходное, не терминал

# Состояния, в которых запись живёт и подметальщик её трогает. Всё остальное —
# терминал: запись снята, на диске остался только итог.
LIVE = (WAITING, WAKING)

# Форжи, чей вывод мы умеем разбирать. Это не специфика клиента (имена его
# репозиториев приезжают в заявке), а два хостинга, которыми пользуются все.
# У client-a стоит только `glab`, у client-b — только `gh`.
GITHUB = "github"
GITLAB = "gitlab"
FORGES = (GITHUB, GITLAB)

# Зелёными считаются ТОЛЬКО эти исходы, всё остальное — падение. Список
# зелёных, а не красных, потому что список красных форж волен пополнить, и
# незнакомый исход тогда молча сошёл бы за успех. `skipped`/`neutral` здесь
# потому, что джоба не запускалась по условию самого пайплайна, и объявлять
# из-за неё красный значило бы держать задачу в ожидании навсегда. А вот
# `cancelled` не здесь намеренно: отменённый прогон не доказывает, что код
# рабочий, и засчитать его зелёным — ровно та подмена, ради которой всё это
# затевалось.
_GH_OK = {"success", "skipped", "neutral"}

_GL_RUNNING = {"created", "waiting_for_resource", "preparing", "pending",
               "running", "scheduled", "manual"}
_GL_BAD = {"failed", "canceled", "cancelled"}
_GL_OK = {"success", "skipped"}

# Состояние MR/ветки — второй опрос на каждом шаге. Ждать проверок по
# закрытому MR или удалённой ветке бессмысленно, а будить сессию на
# несуществующей ветке — вредно.
MR_OPEN = "open"
MR_CLOSED = "closed"
MR_MERGED = "merged"
MR_UNKNOWN = "unknown"

# Так форжи говорят «нет такого» — в stderr CLI при ненулевом коде. Живые
# образцы 15.09.2026: `gh: Not Found (HTTP 404)`, `gh: Branch not found
# (HTTP 404)`, `GraphQL: Could not resolve to a PullRequest with the number
# of 999`, `glab: 404 Not found (HTTP 404)`, `glab: 404 Branch Not Found`.
_NOT_FOUND_RE = re.compile(r"\b404\b|not found|could not resolve to a", re.I)

_MR_URL_RE = re.compile(r"/(?:pull|merge_requests)/(\d+)")


class BadRequest(ValueError):
    """Заявка есть, но по ней нельзя опрашивать — сказать об этом громко.

    Молча проигнорировать битую заявку — худший из исходов: сессия считает,
    что за неё досмотрят, раннер не знает, что от него этого ждут, и задача
    повисает ровно так же, как без всего этого механизма.
    """


def read_request(workdir: Path) -> dict | None:
    """`ci-wait.json` из каталога сессии → нормализованная заявка.

    None — заявки нет (сессия ничего не ждёт, обычный путь). Исключение —
    заявка есть, но негодная.
    """
    path = Path(workdir) / REQUEST_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise BadRequest(f"{REQUEST_FILE} не читается: {e}") from e
    if not isinstance(raw, dict):
        raise BadRequest(f"{REQUEST_FILE} — не объект, а {type(raw).__name__}")

    forge = str(raw.get("forge") or "").strip().lower()
    if forge not in FORGES:
        raise BadRequest(
            f"forge={forge!r} — умеем только {', '.join(FORGES)}")
    repo = str(raw.get("repo") or "").strip()
    branch = str(raw.get("branch") or "").strip()
    missing = [n for n, v in (("repo", repo), ("branch", branch)) if not v]
    if missing:
        raise BadRequest(f"в заявке не заполнено: {', '.join(missing)}")
    return {
        "forge": forge,
        "repo": repo,
        "branch": branch,
        # SHA необязателен, но с ним опрос точнее: на ветке могут висеть
        # прогоны предыдущего пуша, и зелёный от них засчитался бы нашему.
        "sha": str(raw.get("sha") or "").strip(),
        "mr": str(raw.get("mr") or raw.get("pr") or "").strip(),
    }


def mr_number(req: dict) -> str:
    """Номер MR/PR из ссылки в заявке; пусто — ссылки нет или она не про MR."""
    m = _MR_URL_RE.search(str(req.get("mr") or ""))
    return m.group(1) if m else ""


def _gl_project(req: dict) -> str:
    # GitLab: путь проекта в URL кодируется.
    return req["repo"].replace("/", "%2F")


def poll_command(req: dict) -> list[str]:
    """Команда, которой спрашивают форж о состоянии проверок ветки.

    Обе — через JSON-вывод, а не человекочитаемый: разбирать таблицу, которую
    CLI волен переформатировать в следующей версии, значит однажды молча
    прочитать «зелено» там, где красно.
    """
    if req["forge"] == GITHUB:
        return ["gh", "run", "list", "--repo", req["repo"],
                "--branch", req["branch"], "--limit", "30",
                "--json", "status,conclusion,workflowName,headSha,url,databaseId"]
    # `glab api`, а не `glab ci status` — у того вывод для глаз, не для
    # разбора. Фильтр по sha — на стороне форжа: он его умеет.
    query = f"ref={req['branch']}&per_page=20"
    if req.get("sha"):
        query += f"&sha={req['sha']}"
    return ["glab", "api", f"projects/{_gl_project(req)}/pipelines?{query}"]


def mr_command(req: dict) -> list[str]:
    """Команда, которой спрашивают, жив ли MR (или ветка, если MR в заявке
    нет): закрытый MR и удалённая ветка снимают ожидание."""
    num = mr_number(req)
    if req["forge"] == GITHUB:
        if num:
            return ["gh", "pr", "view", num, "--repo", req["repo"],
                    "--json", "state,mergedAt,headRefName"]
        return ["gh", "api", f"repos/{req['repo']}/branches/{req['branch']}"]
    if num:
        return ["glab", "api", f"projects/{_gl_project(req)}/merge_requests/{num}"]
    return ["glab", "api",
            f"projects/{_gl_project(req)}/repository/branches/{req['branch']}"]


def is_not_found(stderr: str, stdout: str = "") -> bool:
    """Ненулевой код CLI — это «нет такого» или «не достучались»? Первое
    снимает ожидание, второе — нет; путать их нельзя в обе стороны."""
    return bool(_NOT_FOUND_RE.search(stderr or "") or _NOT_FOUND_RE.search(stdout or ""))


def parse_mr(forge: str, stdout: str, *, is_branch_probe: bool) -> tuple[str, str]:
    """Вывод опроса MR/ветки → (MR_OPEN / MR_CLOSED / MR_MERGED / MR_UNKNOWN, деталь)."""
    try:
        data = json.loads(stdout or "")
    except ValueError:
        return MR_UNKNOWN, "ответ форжа про MR не разобран"
    if not isinstance(data, dict):
        return MR_UNKNOWN, f"ожидался объект, пришло {type(data).__name__}"
    if is_branch_probe:
        # Ответ про ветку без ошибки — ветка есть; её «закрытость» не бывает.
        return MR_OPEN, "ветка на месте"
    state = str(data.get("state") or "").lower()
    if forge == GITHUB:
        if state == "merged" or data.get("mergedAt"):
            return MR_MERGED, "PR влит"
        if state == "closed":
            return MR_CLOSED, "PR закрыт"
        if state == "open":
            return MR_OPEN, "PR открыт"
        return MR_UNKNOWN, f"незнакомое состояние PR {state!r}"
    if state == "merged":
        return MR_MERGED, "MR влит"
    if state == "closed":
        return MR_CLOSED, "MR закрыт"
    if state in ("opened", "locked"):
        return MR_OPEN, "MR открыт"
    return MR_UNKNOWN, f"незнакомое состояние MR {state!r}"


def parse_runs(forge: str, stdout: str, sha: str = "") -> tuple[str, str, list[str]]:
    """Вывод опроса → (состояние, читаемая человеком деталь, id прогонов НАШЕГО
    коммита).

    Состояние — одно из WAITING / PASSED / FAILED / UNREACHABLE. Мусор на
    входе — это UNREACHABLE, а не «зелено»: неразобранный ответ ничего не
    доказывает. Список id пуст при WAITING значит «прогона нашего коммита в
    списке нет» — по этому `decide` отличает «ещё не появился» от «пропал».

    `sha` отсеивает прогоны ПРЕДЫДУЩИХ пушей на той же ветке: без этого
    зелёный от них засчитался бы новому коммиту — то есть механизм докладывал
    бы «проверено» про непроверенный код. У GitLab фильтр стоит в самой
    команде, здесь он повторён — лишний раз не вредит.
    """
    try:
        data = json.loads(stdout or "")
    except ValueError:
        head = (stdout or "").strip().splitlines()[:1]
        return UNREACHABLE, f"ответ форжа не разобран: {head[0][:200] if head else 'пусто'}", []
    if not isinstance(data, list):
        return UNREACHABLE, f"ожидался список прогонов, пришло {type(data).__name__}", []
    key = "headSha" if forge == GITHUB else "sha"
    runs = [r for r in data if isinstance(r, dict)]
    if sha:
        runs = [r for r in runs if str(r.get(key) or "") == sha]
    if forge == GITHUB:
        return _parse_github(runs)
    return _parse_gitlab(runs)


def _parse_github(runs: list) -> tuple[str, str, list[str]]:
    if not runs:
        # Пусто — это ещё не «нет проверок»: прогон заводится не мгновенно.
        # Различать «не успел появиться» и «не будет» по одному опросу нечем,
        # поэтому ждём, а разводит их срок появления (см. decide).
        return WAITING, "прогонов нашего коммита на ветке пока нет", []
    running, bad, seen, ids = [], [], [], []
    for run in runs:
        name = str(run.get("workflowName") or "?")
        seen.append(name)
        ids.append(str(run.get("databaseId") or run.get("url") or name))
        if str(run.get("status") or "") != "completed":
            running.append(name)
            continue
        concl = str(run.get("conclusion") or "").lower()
        if concl not in _GH_OK:
            bad.append(f"{name}: {concl or 'без исхода'} ({run.get('url') or '?'})")
    if bad:
        return FAILED, "; ".join(bad), ids
    if running:
        return WAITING, "ещё идут: " + ", ".join(sorted(set(running))), ids
    return PASSED, "зелено: " + ", ".join(sorted(set(seen))), ids


def _parse_gitlab(pipelines: list) -> tuple[str, str, list[str]]:
    if not pipelines:
        return WAITING, "пайплайнов нашего коммита на ветке пока нет", []
    head = pipelines[0]
    ids = [str(p.get("id") or p.get("web_url") or "?") for p in pipelines]
    status = str(head.get("status") or "").lower()
    where = str(head.get("web_url") or head.get("id") or "?")
    if status in _GL_RUNNING:
        return WAITING, f"пайплайн {where}: {status}", ids
    if status in _GL_BAD:
        return FAILED, f"пайплайн {where}: {status}", ids
    if status in _GL_OK:
        return PASSED, f"пайплайн {where}: {status}", ids
    return UNREACHABLE, f"пайплайн {where}: незнакомый статус {status!r}", ids


def decide(status: str, *, waited_sec: float, max_wait_sec: float,
           found: bool, seen_before: bool, appear_sec: float) -> tuple[str, str]:
    """Что делать дальше. Отделено от разбора, чтобы сроки проверялись в
    одном месте. Возвращает (состояние, причина-уточнение или пусто).

    Три срока и один факт:
    * прогон УЖЕ ответил (PASSED/FAILED) — ответ берём, никакой срок его не
      отменяет: объявить тайм-аут по готовому ответу значит его выбросить;
    * прогона нашего коммита в списке нет, хотя раньше был, — он удалён,
      ждать нечего (NOT_FOUND). То же, если его не видно `appear_sec` от
      начала: прогон заводится не мгновенно, но не четверть часа;
    * `max_wait_sec` от начала — предел на всё остальное (TIMEOUT): висящий
      сутки CI не должен держать задачу вечно.
    """
    if status in (PASSED, FAILED):
        return status, ""
    if status == WAITING and not found:
        if seen_before:
            return NOT_FOUND, "прогон нашего коммита был в списке и пропал — удалён"
        if waited_sec >= appear_sec:
            return NOT_FOUND, (f"за {int(appear_sec // 60)} мин ни одного прогона "
                               f"нашего коммита не появилось — CI по ветке не "
                               f"запускается или в заявке не тот sha/ветка")
    if waited_sec >= max_wait_sec:
        return TIMEOUT, f"проверки не ответили за {int(max_wait_sec // 60)} мин"
    return (status if status in (WAITING, UNREACHABLE) else WAITING), ""


def hhmm(ts: float) -> str:
    """Момент по часам — так его читает человек в комментарии."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def where(req: dict) -> str:
    return req.get("mr") or f"{req.get('repo', '?')}@{req.get('branch', '?')}"


def describe(state: str, detail: str, req: dict) -> str:
    """Одна строка про переход — то, что уедет в трекер и в уведомление."""
    words = {
        WAITING: "проверки идут",
        WAKING: "проверки упали, сессия возвращена чинить",
        PASSED: "проверки зелёные",
        FAILED: "проверки упали",
        TIMEOUT: "проверки не закончились за отведённый срок",
        NOT_FOUND: "прогон проверок не найден",
        CLOSED: "MR закрыт или ветка удалена, пока ждали",
        WAKE_LOST: "сессия-продолжение не поднялась",
        UNREACHABLE: "форж не отвечает",
    }
    return f"{words.get(state, state)} — {where(req)}" + (f": {detail}" if detail else "")


def progress(entry: dict, now: float, max_wait_sec: float) -> str:
    """«Что ждём, сколько уже, когда сдадимся» — одной строкой. Три факта, без
    которых снаружи не отличить работающую фабрику от мёртвой: исходный
    случай DEV-16 — 40 минут без единого сигнала."""
    req = entry.get("req") or {}
    started = float(entry.get("started_at") or now)
    deadline = started + max_wait_sec
    waited = max(0, int((now - started) // 60))
    left = max(0, int((deadline - now) // 60))
    sha = str(req.get("sha") or "")[:8]
    what = where(req) + (f", коммит {sha}" if sha else "")
    return (f"ждём {what}; уже {waited} мин (с {hhmm(started)}); "
            f"сдадимся в {hhmm(deadline)} (через {left} мин)")


def write_state(workdir: Path, entry: dict, now: float, max_wait_sec: float) -> bool:
    """Запись ожидания — на диск, рядом с заявкой, целиком.

    Пишется на каждом переходе, а не только в конце, и содержит всё, из чего
    ожидание восстанавливается: redis на client-b живёт снимками раз в
    5-15 минут без AOF, и перезагрузка хоста теряет свежие ключи. Файл на
    диске — вторая копия той же записи; `runner._ci_recover` сверяет их и
    возвращает потерянное в redis, сказав об этом вслух.
    """
    payload = dict(entry)
    payload["deadline_at"] = float(entry.get("started_at") or now) + max_wait_sec
    payload["written_at"] = now
    payload["progress"] = progress(entry, now, max_wait_sec)
    try:
        (Path(workdir) / STATE_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1))
        return True
    except OSError:
        # Каталог задачи мог уехать вместе с уборкой — это не повод ронять
        # ожидание: запись в redis жива, трекер получит свой комментарий.
        return False


def read_state(workdir: Path) -> dict | None:
    """Запись с диска; None — нет файла или он не читается."""
    try:
        data = json.loads((Path(workdir) / STATE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def failure_prompt(ident: str, detail: str, req: dict, command: list[str],
                   attempt: int, max_attempts: int) -> str:
    """Продолжение сессии с точной причиной падения, а не «посмотри сам».

    Причина и команда — в тексте: продолжение стартует с чистым контекстом, и
    без них первое, что оно сделает, — заново выяснит то, что раннер уже знает.
    """
    return (
        f"Проверки по {ident} упали после того, как ты сдала работу "
        f"(возврат {attempt} из {max_attempts}).\n\n"
        f"Где: {where(req)}\nЧто именно: {detail}\n\n"
        f"Раннер смотрел так:\n`{' '.join(shlex.quote(c) for c in command)}`\n\n"
        f"Наработки в рабочем дереве на месте, ветка та же — ничего не "
        f"начинай заново и второй MR не открывай. Разберись в причине "
        f"падения, почини, закоммить в ту же ветку и запушь. Потом доведи "
        f"задачу до конца по исходной инструкции: финальный комментарий и "
        f"передача на проверку.\n\n"
        f"Ожидание проверок после пуша — не твоя работа: перепиши "
        f"`{REQUEST_FILE}` в своём рабочем каталоге (тот же формат, новый "
        f"`sha`), и раннер досмотрит их за тебя."
    )
