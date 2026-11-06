#!/usr/bin/env python3
"""База знаний проекта: собрать, чего можно, спросить остальное, сказать,
чего не хватает (DEV-41).

Объявление обещает «создание базы знаний проекта» в те же семь дней, но
процесса не было: у первого клиента база собиралась руками и месяцами.
Здесь процесс, и стоит он на одной мысли, которая не является нашим вкусом:
**база знаний — это то, что сессия читает перед работой.** Значит и состав
её задан не вкусом, а точками чтения, которые уже названы в коде продукта:

| что читают                | где это названо                                    |
|---------------------------|----------------------------------------------------|
| `AGENT_KNOWN_REPOS`       | `runner.py` → `prompt.py` (`DEFAULT_KNOWN_REPOS`)   |
| `AGENT_ENV_CANON`         | то же, `DEFAULT_ENV_CANON` — без него приём врёт    |
| `CLAUDE.md` репозитория   | `prompt.py`: «ЧЕМ проверять — смотри в `CLAUDE.md`» |
| методология рядом с ядром | `memory.METHODOLOGY_BASE`                           |
| каталоги памяти ролей     | `runner.link_role_memory`                           |
| плейбук гашения симптома  | `runner.AGENT_SYMPTOM_PLAYBOOK` → шаг 6 контракта   |
| плейбук дежурного         | `runner.AGENT_INCIDENT_PLAYBOOK` → уровень 2        |

Отсюда и определение достаточности, которое можно проверить, а не обсудить:
**база достаточна, когда каждая названная точка чтения отвечает непусто.**
Пустая точка — это не «неполная документация», это сессия, которая на неё
придёт и уйдёт ни с чем.

У двух плейбуков пустота выглядит особенно безобидно: `prompt.py` подставит
честную заглушку, сессия соберётся и отработает — просто дежурный будет
восстанавливать прод с нуля каждый раз, а рабочая сессия не узнает, что
симптом здесь гасится одной командой. Поэтому «рецептов пока нет» обязано
быть ЗАПИСАНО: незаданная переменная и записанное «нет» различимы только
так, и только записанное можно потом дополнить.

Три команды:

    knowledge.py draft     --repos-root …                черновик AGENT_KNOWN_REPOS
    knowledge.py questions --repos-root … --env-file …   опросник команде
    knowledge.py audit     --repos-root … --env-file …   чего не хватает

Что выводится из репозиториев — выводится без единого вопроса: имя, форж,
языки, команда проверки, наличие CI и `CLAUDE.md`. Что не выводится —
попадает в опросник, и у каждого вопроса написано, КУДА кладётся ответ и
какой черновик уже есть.

Канон окружений (`AGENT_ENV_CANON`) — единственное поле, которое чтением
кода не собирается вовсе: на первом клиенте его неверное понимание дважды
кончилось инцидентом (TASK-184, TASK-235, см. DEV-29). Правдоподобный текст
тут неотличим от верного на глаз, поэтому аудит требует не текста, а
**проверяемости**: в строке канона должна стоять команда в обратных
кавычках, которой утверждение проверяется. Канон, где такой строки нет ни
одной, не принимается — не потому, что он короткий, а потому, что его
нечем опровергнуть.

Коды выхода `audit`: 0 — база достаточна, 1 — не хватает обязательного.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ── что читает сессия ───────────────────────────────────────────────────────

# Роли памяти. Список дублирует `runner._role_of` и `install.sh` сознательно:
# аудит гоняют и на хосте, где ядро ещё ни разу не запускалось.
ROLES = ("intake", "work", "incident", "verify")

MUST = "обязательно"
NICE = "желательно"

# Плейбуки клиента: переменная раннера, кто её читает, чем оборачивается
# пустота. Обе точки продукт называет сам (`runner._SYMPTOM_PLAYBOOK_FILE`,
# `runner._INCIDENT_PLAYBOOK_FILE`) и сам же описывает цену пустоты —
# заглушку вместо рецептов клиента, — поэтому они в базе на общих правах.
PLAYBOOKS = (
    {"переменная": "AGENT_SYMPTOM_PLAYBOOK_FILE",
     "цена": "шаг 6 рабочего контракта соберётся с заглушкой, и сессия не "
             "узнает, что симптом здесь снимается одной командой",
     "вопрос": "Чем здесь гасят симптом, не трогая кода?",
     "форма": "по строке на способ: что гасим, какой командой, чем убедиться, "
              "что помогло. Такого способа нет — так и напишите одной строкой: "
              "незаданная переменная и записанное «нет» различимы только так"},
    {"переменная": "AGENT_INCIDENT_PLAYBOOK_FILE",
     "цена": "дежурная сессия соберётся с заглушкой вместо рецептов "
             "восстановления и каждый инцидент будет разбирать с нуля",
     "вопрос": "Какие инциденты уже случались и чем их восстанавливали?",
     "форма": "по строке на класс инцидента: признак, команда восстановления, "
              "чем видно, что прод вернулся. Инцидентов ещё не было — так и "
              "напишите: файл потом дописывает дежурный"},
)

# Каталоги, которые не про проект: считать в них языки — значит объявить
# любой сайт с node_modules проектом на JavaScript чужих библиотек.
SKIP_DIRS = {".git", ".github", ".gradle", ".idea", ".next", ".venv", "__pycache__",
             "build", "coverage", "dist", "node_modules", "out", "target",
             "vendor", "venv"}

LANGS = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
         ".js": "JavaScript", ".jsx": "JavaScript", ".kt": "Kotlin",
         ".kts": "Kotlin", ".java": "Java", ".go": "Go", ".rs": "Rust",
         ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".swift": "Swift",
         ".sh": "Shell", ".html": "HTML", ".css": "CSS", ".scss": "CSS",
         ".sql": "SQL"}

# Чем репозиторий проверяет себя сам. Порядок важен: у проекта бывает и
# package.json, и Makefile, и назвать надо ту команду, которой пользуются.
# Признак — не «файл есть», а «в файле есть чем запуститься»: пустой
# package.json командой проверки не является.
def _npm_test(repo: Path) -> str:
    try:
        data = json.loads((repo / "package.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    scripts = data.get("scripts") or {}
    return "npm test" if isinstance(scripts, dict) and scripts.get("test") else ""


def _make_test(repo: Path) -> str:
    for name in ("Makefile", "makefile"):
        try:
            text = (repo / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"^test:", text, re.M):
            return "make test"
    return ""


def _has_test_files(repo: Path) -> bool:
    """Есть ли хоть один `test_*.py` — мимо каталогов зависимостей.

    `rglob` тут не годится: он уходит в `node_modules` и `.venv`, где
    тестовые файлы чужих библиотек найдутся всегда.
    """
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if any(f.startswith("test_") and f.endswith(".py") for f in files):
            return True
    return False


def _pytest(repo: Path) -> str:
    if any((repo / f).exists() for f in ("pytest.ini", "tox.ini", "pyproject.toml", "setup.cfg")):
        if _has_test_files(repo):
            return "python -m pytest"
    return ""


CHECKS = (
    ("package.json", _npm_test),
    # У pytest четыре равноправных места настройки, и репозиторий может иметь
    # любое одно: без всех четырёх маркеров проект с одним `pytest.ini`
    # выглядел бы «проверка не найдена».
    ("pyproject.toml", _pytest),
    ("pytest.ini", _pytest),
    ("tox.ini", _pytest),
    ("setup.cfg", _pytest),
    ("Makefile", _make_test),
    ("go.mod", lambda r: "go test ./..." if (r / "go.mod").exists() else ""),
    ("Cargo.toml", lambda r: "cargo test" if (r / "Cargo.toml").exists() else ""),
    ("gradlew", lambda r: "./gradlew test" if (r / "gradlew").exists() else ""),
    ("pom.xml", lambda r: "mvn test" if (r / "pom.xml").exists() else ""),
)

# Раздел про проверку в `CLAUDE.md`. Ищем не заголовок, а команду: заголовок
# «## Тестирование» с пустым телом точку чтения не закрывает, а команда
# закрывает, даже если лежит без заголовка.
CHECK_IN_CLAUDE = re.compile(
    r"pytest|npm (run )?test|yarn (run )?test|go test|cargo test|gradlew\s+\S*test|"
    r"mvn\s+\S*test|make\s+test|jest|vitest|node --test|rspec|phpunit|dotnet test|"
    # Явно названное отсутствие проверок — тоже ответ. База знаний обязана
    # отвечать на вопрос «чем проверяется», а «ничем, глазами» — ответ; без
    # этой ветки честный репозиторий без тестов выглядел бы дырой в базе.
    r"тестов нет|автотестов нет|проверок нет|тестами не покрыт|не тестируется",
    re.I)
# Стенд и выкат — вторая точка, которую называет контракт проверки
# (`prompt.py`: «Стенд ставишь ты сам; как — написано в CLAUDE.md»). Не
# обязательна: библиотеке выкатываться некуда.
DEPLOY_IN_CLAUDE = re.compile(r"стенд|выкат|деплой|\bdeploy|\bstaging\b|\bCI\b", re.I)

# Строка канона с доказательством: команда в обратных кавычках.
PROOF = re.compile(r"`[^`\n]+`")

# Схема данных и список сервисов — их чекбокс задачи называет среди того, что
# «можно вычитать из проекта, а не спрашивать у человека». Вычитываем не саму
# схему, а её АДРЕС: разбирать чужой DDL и пересказывать его в базе знаний —
# это заводить вторую копию правды, которая протухнет к первой миграции.
# Сессии нужен путь, по которому лежит настоящая; его мы и называем.
SCHEMA_FILES = re.compile(
    r"^(schema\.prisma|schema\.sql|structure\.sql|schema\.rb|.*\.dbml)$", re.I)
SCHEMA_DIRS = {"migrations", "migrate", "alembic", "prisma", "liquibase", "flyway"}
SERVICE_FILES = re.compile(
    r"^(docker-compose\.ya?ml|compose\.ya?ml|Procfile|.*\.service|"
    r"serverless\.ya?ml|Chart\.ya?ml)$", re.I)
# Только `helm`: каталог `charts` сам по себе бывает и папкой с графиками, а
# объявить выручку в CSV сервисом проекта — это положить в базу знаний
# неправду. Helm-чарт опознаётся по `Chart.yaml` выше, где бы он ни лежал.
SERVICE_DIRS = {"helm"}
# ...а если маркеров в дереве нет, ответ мог быть дан словами — в `CLAUDE.md`.
# Ищем РАЗДЕЛ, а не слова: поиск по словам здесь врёт в опасную сторону — на
# живых репозиториях Доки он «нашёл ответ про схему данных» в строке «Таблица
# отказов из постановки», а ответ про сервисы — в «Jetpack Compose». Ложное
# «отвечено» гасит настоящий вопрос молча, и это хуже лишнего вопроса.
SCHEMA_HEADING = re.compile(
    r"^#{1,6}[^\n]*(схем[аы] данных|модел[ьи] данных|миграци|хранени[ея] данных|"
    r"\bданные\b|\bschema\b|\bdata model\b)", re.I | re.M)
# «юнит» и «unit» здесь только про systemd: заголовок «## Юнит-тесты» (и его
# английский близнец) есть у половины репозиториев, и без отсечки он закрывал
# бы вопрос про сервисы вообще везде — ровно то ложное «отвечено», от
# которого уходит поиск по разделам.
SERVICES_HEADING = re.compile(
    r"^#{1,6}[^\n]*(сервис|юнит(?!\s*-?\s*тест)|что (здесь )?(крутится|запущено)|"
    r"процессы|systemd|\bservices?\b|\bdeployments?\b)", re.I | re.M)


# ── чтение репозитория ──────────────────────────────────────────────────────

def _git_dir(repo: Path) -> Path | None:
    """Каталог `.git` репозитория. У worktree `.git` — файл со ссылкой, а
    конфиг лежит в общем каталоге: без этого у каждой рабочей копии сессии
    не нашлось бы origin."""
    dot = repo / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        try:
            text = dot.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        m = re.search(r"^gitdir:\s*(.+)$", text, re.M)
        if not m:
            return None
        gd = Path(m.group(1).strip())
        if not gd.is_absolute():
            gd = (repo / gd).resolve()
        common = gd / "commondir"
        if common.exists():
            try:
                rel = common.read_text(encoding="utf-8", errors="replace").strip()
                gd = (gd / rel).resolve()
            except OSError:
                pass
        return gd if gd.is_dir() else None
    return None


_ORIGIN_SECTION = re.compile(r'\[remote "origin"\](.*?)(?=\n\[|\Z)', re.S)
_URL = re.compile(r"^\s*url\s*=\s*(.+)$", re.M)


def origin_url(repo: Path) -> str:
    """Адрес origin — из `.git/config`, без запуска git.

    Разбираем сами, а не `configparser`: git отбивает ключи табом, и
    configparser читает такую строку как продолжение предыдущего значения.
    """
    gd = _git_dir(repo)
    if gd is None:
        return ""
    try:
        text = (gd / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    sec = _ORIGIN_SECTION.search(text)
    if not sec:
        return ""
    m = _URL.search(sec.group(1))
    return m.group(1).strip() if m else ""


def forge_of(url: str) -> str:
    """Хостинг по адресу origin. Форж — не косметика: от него зависит, каким
    клиентом сессия откроет MR (`gh` против `glab`), и угадывать его по
    умолчанию контракт уже однажды заставлял."""
    if not url:
        return ""
    low = url.lower()
    for known in ("github.com", "gitlab.com", "bitbucket.org"):
        if known in low:
            return known.split(".")[0]
    m = re.search(r"(?:@|//)([^/:]+)[:/]", url)
    host = m.group(1) if m else ""
    # `git@github-client-b:…` — не другой хостинг, а псевдоним из ~/.ssh/config:
    # у Доки так разведены ключи двух организаций. Без этой ветки аудит
    # объявил бы форжем несуществующий «github-client-b».
    for known in ("github", "gitlab", "bitbucket"):
        if host.startswith(known):
            return known
    return host


def languages(repo: Path, top: int = 2) -> list[str]:
    counts: dict[str, int] = {}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            lang = LANGS.get(Path(f).suffix.lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
    return [lang for lang, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]


def check_command(repo: Path) -> tuple[str, str]:
    """(команда, откуда её взяли). Пусто — значит спросим у команды."""
    for marker, detect in CHECKS:
        if not (repo / marker).exists():
            continue
        cmd = detect(repo)
        if cmd:
            return cmd, marker
    return "", ""


def ci_of(repo: Path) -> str:
    wf = repo / ".github" / "workflows"
    if wf.is_dir() and any(wf.glob("*.y*ml")):
        return "GitHub Actions"
    if (repo / ".gitlab-ci.yml").exists():
        return "GitLab CI"
    return ""


def data_points(repo: Path, limit: int = 3) -> dict:
    """Где в клоне лежит схема данных и где перечислены сервисы.

    Отдаём ПУТИ, а не содержимое: пересказать чужой DDL в базе знаний значит
    завести вторую копию правды, которая разойдётся с первой же миграцией.
    Сессии нужен адрес настоящей — его и называем, не больше трёх на вид,
    чтобы строка `AGENT_KNOWN_REPOS` не превратилась в листинг каталога.
    """
    schema: set[str] = set()
    services: set[str] = set()
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(repo)
        for d in dirs:
            if d.lower() in SCHEMA_DIRS:
                schema.add(str(rel / d) + "/")
            if d.lower() in SERVICE_DIRS:
                services.add(str(rel / d) + "/")
        for f in files:
            if SCHEMA_FILES.match(f):
                schema.add(str(rel / f))
            if SERVICE_FILES.match(f):
                services.add(str(rel / f))
    return {"схема": sorted(schema)[:limit], "сервисы": sorted(services)[:limit]}


def claude_md(repo: Path) -> dict:
    """Состояние `CLAUDE.md` репозитория по точкам, которые называет контракт."""
    empty = {"есть": False, "проверка": False, "выкат": False,
             "схема": False, "сервисы": False}
    path = repo / "CLAUDE.md"
    if not path.exists():
        return empty
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return empty
    return {"есть": True,
            "проверка": bool(CHECK_IN_CLAUDE.search(text)),
            "выкат": bool(DEPLOY_IN_CLAUDE.search(text)),
            "схема": bool(SCHEMA_HEADING.search(text)),
            "сервисы": bool(SERVICES_HEADING.search(text))}


def answered(repos: list[dict], kind: str) -> bool:
    """Отвечено ли про схему данных (`схема`) или про сервисы (`сервисы`).

    Ответ засчитывается с двух сторон: маркер в дереве клона (миграции,
    compose, юнит) ИЛИ отдельный РАЗДЕЛ об этом в `CLAUDE.md` любого
    репозитория. Вторая сторона обязательна: у проекта с внешней БД в дереве
    не будет ничего, а ответ в базе знаний есть — спрашивать его снова значит
    требовать написанного. Раздел, а не слова: см. `SCHEMA_HEADING`.
    """
    for r in repos:
        if (r.get("данные") or {}).get(kind):
            return True
        if (r.get("claude") or {}).get(kind):
            return True
    return False


def scan_repo(repo: Path) -> dict:
    url = origin_url(repo)
    cmd, src = check_command(repo)
    return {"репозиторий": repo.name, "origin": url, "форж": forge_of(url),
            "языки": languages(repo), "проверка": cmd, "откуда": src,
            "ci": ci_of(repo), "claude": claude_md(repo),
            "данные": data_points(repo)}


def scan(repos_root: Path) -> list[dict]:
    """Все клоны в корне репозиториев. Клон — каталог с `.git`: каталоги
    рядом с ними бывают чужие (выгрузки, кэши), и объявлять их репозиториями
    проекта нельзя."""
    if not repos_root.is_dir():
        return []
    out = []
    for d in sorted(repos_root.iterdir()):
        if d.is_dir() and _git_dir(d) is not None:
            out.append(scan_repo(d))
    return out


# ── черновик AGENT_KNOWN_REPOS ──────────────────────────────────────────────

def repo_line(r: dict) -> str:
    parts = ["/".join(r["языки"]) if r["языки"] else "язык не определён"]
    parts.append("проверка: " + (r["проверка"] or "НЕ НАЙДЕНА, спросить"))
    if r["ci"]:
        parts.append("CI: " + r["ci"])
    if r["форж"]:
        parts.append("форж: " + r["форж"])
    d = r.get("данные") or {}
    if d.get("схема"):
        parts.append("схема данных: " + " ".join(d["схема"]))
    if d.get("сервисы"):
        parts.append("сервисы: " + " ".join(d["сервисы"]))
    c = r["claude"]
    parts.append("CLAUDE.md: " + ("есть" if c["есть"] and c["проверка"]
                                  else "без раздела о проверке" if c["есть"] else "НЕТ"))
    return r["репозиторий"] + " — " + ", ".join(parts)


def known_repos_draft(repos: list[dict], repos_root: Path) -> str:
    """Одной строкой: значение читается из файла окружения юнита, а туда
    многострочное не кладётся — systemd оборвёт его на первом переводе."""
    if not repos:
        return ""
    head = (f"Репозиториев проекта: {len(repos)}, все склонированы в "
            f"AGENT_REPOS_ROOT ({repos_root}).")
    body = " ".join(repo_line(r) + "." for r in repos)
    return (head + " " + body).replace('"', "'")


# ── канон окружений ─────────────────────────────────────────────────────────

def canon_lines(text: str) -> tuple[list[str], list[str]]:
    """(строки с проверкой, строки без проверки)."""
    proved, bare = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        (proved if PROOF.search(line) else bare).append(line)
    return proved, bare


# ── аудит ───────────────────────────────────────────────────────────────────

def read_env(path: Path | None) -> dict:
    """Файл окружения инсталляции. Разбор тот же, что у `install.sh`:
    `KEY=значение`, кавычки по краям снимаются."""
    if path is None or not path.exists():
        return {}
    env = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        env[key.strip()] = val
    return env


def _named(name: str, text: str) -> bool:
    """Назван ли репозиторий в тексте. По границе имени, а не подстрокой:
    `dok-engine` внутри `dok-engine-old` — это другой репозиторий, и простое
    вхождение объявило бы первый описанным, когда описан второй."""
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text) is not None


def audit(repos_root: Path, env: dict, core_dir: Path | None = None) -> dict:
    """Что из точек чтения отвечает пусто.

    `core_dir` — каталог ядра (рядом с ним лежит база методологии). По
    умолчанию — каталог этого файла: на хосте ядро раскатано плоско, и
    установленный `knowledge.py` лежит ровно там же, где `methodology/`.
    """
    core = core_dir or Path(__file__).resolve().parent
    repos = scan(repos_root)
    gaps: list[dict] = []

    def gap(level: str, what: str, where: str) -> None:
        gaps.append({"уровень": level, "чего": what, "куда": where})

    if not repos:
        gap(MUST, f"в {repos_root} нет ни одного клона репозитория проекта",
            "склонировать репозитории проекта в AGENT_REPOS_ROOT")

    known = (env.get("AGENT_KNOWN_REPOS") or "").strip()
    if not known:
        gap(MUST, "AGENT_KNOWN_REPOS пуст — сессия не знает, какие репозитории у проекта",
            "AGENT_KNOWN_REPOS (черновик: knowledge.py draft)")
    else:
        for r in repos:
            if not _named(r["репозиторий"], known):
                gap(MUST, f"репозиторий {r['репозиторий']} склонирован, но не назван "
                          f"в AGENT_KNOWN_REPOS", "AGENT_KNOWN_REPOS")

    canon = (env.get("AGENT_ENV_CANON") or "").strip()
    proved, bare = canon_lines(canon)
    if not canon:
        gap(MUST, "AGENT_ENV_CANON пуст — приём и дежурный не знают, что здесь прод",
            "AGENT_ENV_CANON (форму см. knowledge.py questions)")
    elif not proved:
        gap(MUST, "в AGENT_ENV_CANON нет ни одной строки с командой-доказательством: "
                  "правдоподобный канон неотличим от верного",
            "AGENT_ENV_CANON: в каждой строке команда в обратных кавычках")
    for line in bare:
        gap(NICE, f"строка канона без проверки: {line[:70]}",
            "AGENT_ENV_CANON: дописать команду, которой это видно")

    for r in repos:
        name, c = r["репозиторий"], r["claude"]
        if not c["есть"]:
            gap(MUST, f"{name}: нет CLAUDE.md — контракт сессии посылает читать его "
                      f"за командами проверки", f"{repos_root / name}/CLAUDE.md")
            continue
        if not c["проверка"]:
            gap(MUST, f"{name}: в CLAUDE.md не сказано, чем проверять",
                f"{repos_root / name}/CLAUDE.md, раздел про проверку")
        if not c["выкат"]:
            gap(NICE, f"{name}: в CLAUDE.md не сказано про стенд и выкат",
                f"{repos_root / name}/CLAUDE.md, раздел про стенд")

    store = (env.get("AGENT_MEMORY_STORE") or "").strip()
    if not store:
        gap(MUST, "AGENT_MEMORY_STORE не задан — памяти ролей нет, опыт сессий "
                  "умирает вместе с задачей", "AGENT_MEMORY_STORE")
    else:
        for role in ROLES:
            if not (Path(store) / f"agent-{role}").is_dir():
                gap(MUST, f"нет каталога памяти роли agent-{role}",
                    f"{store}/agent-{role} (заводит install.sh)")

    for pb in PLAYBOOKS:
        var, cost = pb["переменная"], pb["цена"]
        raw = (env.get(var) or "").strip()
        if not raw:
            gap(MUST, f"{var} не задан — {cost}",
                f"{var}: путь к файлу плейбука (что в него писать — "
                f"knowledge.py questions)")
            continue
        path = Path(raw)
        if not path.is_file():
            gap(MUST, f"{var}={raw} задан, но файла нет — {cost}", raw)
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            gap(MUST, f"{var}={raw} не читается ({exc.strerror}) — {cost}", raw)
            continue
        if not body.strip():
            gap(MUST, f"{var}={raw} — файл пуст, а пустой файл раннер отдаёт "
                      f"как незаданный: {cost}", raw)

    # Схема данных и сервисы — желательное, а не обязательное, и причина не в
    # том, что они менее важны. Отсутствие маркеров в клонах неотличимо от
    # честного «у проекта нет БД»: статическому сайту нечего показать, и
    # требовать с него схему значило бы объявить дырой правильную базу.
    if repos and not answered(repos, "схема"):
        gap(NICE, "ни в одном клоне не нашлось, где описана схема данных "
                  "(миграции, schema.sql, prisma) — если БД у проекта есть, "
                  "сессии неоткуда узнать её структуру",
            "CLAUDE.md репозитория с базой, раздел «## Схема данных»: "
            "где лежат миграции и чем накатываются")
    if repos and not answered(repos, "сервисы"):
        gap(NICE, "ни в одном клоне не нашлось списка сервисов (compose, "
                  "юниты systemd, Procfile) — что у проекта вообще запущено, "
                  "сессия знает только со слов канона",
            "AGENT_ENV_CANON и CLAUDE.md, раздел «## Сервисы»: что, где, чем смотрят")

    methodology = core / "methodology"
    if not (methodology.is_dir() and any(methodology.glob("*.md"))):
        gap(MUST, f"рядом с ядром нет базы методологии ({methodology})",
            "прогнать install.sh: базу кладёт он")

    if not (env.get("AGENT_PERSON_MEMORY") or "").strip():
        gap(NICE, "AGENT_PERSON_MEMORY не задан — сессия не знает привычек "
                  "принимающего работу", "AGENT_PERSON_MEMORY")

    must = [g for g in gaps if g["уровень"] == MUST]
    return {"репозитории": repos, "нет": gaps, "код": 1 if must else 0}


# ── опросник ────────────────────────────────────────────────────────────────

CANON_FORM = (
    "по строке на контур; в строке — что это, где живёт и КОМАНДА в обратных "
    "кавычках, которой это видно. Например: «прод — юнит `doc-engine@familydoctor` "
    "на этом хосте, проверить `systemctl is-enabled doc-engine@familydoctor`»"
)

# Вопросы, которых не закроет никакое чтение кода: они про решения и про
# опыт, а не про файлы. По одному на слой памяти — так видно, куда ответ
# кладётся и кто им потом владеет.
ALWAYS = (
    {"вопрос": "Что в этих репозиториях трогать нельзя и почему?",
     "куда": "CLAUDE.md соответствующего репозитория (слой проекта)"},
    {"вопрос": "Какие грабли команда знает наизусть, а в коде их не видно: "
               "чем ломается выкат, что чинится руками, где врут зелёные проверки?",
     "куда": "память роли work ($AGENT_MEMORY_STORE/agent-work)"},
    {"вопрос": "Кто принимает работу и в каком виде ждёт отчёт?",
     "куда": "слой о человеке ($AGENT_PERSON_MEMORY)"},
)


def playbook_text(env: dict, var: str) -> str:
    """Содержимое плейбука — так же, как его читает раннер: путь из
    переменной, пусто при любой осечке. Раннеру и пустой файл, и
    несуществующий путь одинаково дают заглушку, значит и спрашивать про них
    надо одинаково."""
    raw = (env.get(var) or "").strip()
    if not raw:
        return ""
    try:
        return Path(raw).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def questions(repos_root: Path, env: dict, core_dir: Path | None = None) -> list[dict]:
    """Опросник: только то, что не выводится из репозиториев.

    Каждый вопрос несёт черновик, если он у нас есть. Спрашивать «чем
    проверяется репозиторий», когда в `package.json` написано `npm test`, —
    это перекладывать на команду работу, которую мы умеем сделать сами; а
    подтвердить черновик дешевле, чем сочинить ответ с нуля.
    """
    result = audit(repos_root, env, core_dir)
    out: list[dict] = []

    canon = (env.get("AGENT_ENV_CANON") or "").strip()
    proved, _ = canon_lines(canon)
    if not proved:
        out.append({"вопрос": "Что здесь прод, а что нет — и чем это проверить?",
                    "куда": "AGENT_ENV_CANON", "форма": CANON_FORM, "черновик": ""})

    for pb in PLAYBOOKS:
        if playbook_text(env, pb["переменная"]):
            continue
        # Путь уже задан (а файл пуст или не доехал) — ответ кладётся в ЭТОТ
        # файл, и назвать надо его, а не переменную: иначе команда впишет
        # путь второй раз вместо того, чтобы написать плейбук.
        куда = (env.get(pb["переменная"]) or "").strip() or pb["переменная"]
        out.append({"вопрос": pb["вопрос"], "куда": куда,
                    "форма": pb["форма"], "черновик": ""})

    repos = result["репозитории"]
    if repos and not answered(repos, "схема"):
        out.append({"вопрос": "Где описана схема данных проекта — и есть ли "
                              "вообще база?",
                    "куда": "CLAUDE.md репозитория с базой, раздел «## Схема данных»",
                    "форма": "путь к миграциям или схеме и команда, которой их "
                             "накатывают. Базы нет — так и напишите; ответ "
                             "засчитывается разделом, а не упоминанием",
                    "черновик": ""})
    if repos and not answered(repos, "сервисы"):
        out.append({"вопрос": "Какие сервисы у проекта запущены и где?",
                    "куда": "AGENT_ENV_CANON (контуры) и CLAUDE.md, раздел «## Сервисы»",
                    "форма": "по строке на сервис: имя, хост, команда, которой "
                             "видно, жив он или нет",
                    "черновик": ""})

    for r in repos:
        c = r["claude"]
        if c["есть"] and c["проверка"]:
            continue
        draft = (f"проверка: {r['проверка']} (нашли в {r['откуда']})"
                 if r["проверка"] else "")
        out.append({"вопрос": f"{r['репозиторий']}: чем проверяется и где его стенд?",
                    "куда": f"{repos_root / r['репозиторий']}/CLAUDE.md",
                    "форма": "", "черновик": draft})

    if not (env.get("AGENT_KNOWN_REPOS") or "").strip():
        out.append({"вопрос": "Какие репозитории у проекта и что в каждом?",
                    "куда": "AGENT_KNOWN_REPOS", "форма": "",
                    "черновик": known_repos_draft(repos, repos_root)})

    for q in ALWAYS:
        out.append({**q, "форма": "", "черновик": ""})
    return out


# ── командная строка ────────────────────────────────────────────────────────

def _print_draft(repos: list[dict], repos_root: Path) -> int:
    if not repos:
        print(f"в {repos_root} нет клонов репозиториев — собирать нечего", file=sys.stderr)
        return 1
    for r in repos:
        print("# " + repo_line(r))
    print()
    print('AGENT_KNOWN_REPOS="' + known_repos_draft(repos, repos_root) + '"')
    return 0


def _print_questions(qs: list[dict]) -> int:
    if not qs:
        print("спрашивать нечего")
        return 0
    print(f"Вопросов команде: {len(qs)}. Ответы кладутся туда, где их читает сессия.\n")
    for i, q in enumerate(qs, 1):
        print(f"{i}. {q['вопрос']}")
        print(f"   куда: {q['куда']}")
        if q.get("форма"):
            print(f"   форма: {q['форма']}")
        if q.get("черновик"):
            print(f"   черновик: {q['черновик']}")
        print()
    return 0


def _print_audit(result: dict) -> int:
    for r in result["репозитории"]:
        print("  " + repo_line(r))
    must = [g for g in result["нет"] if g["уровень"] == MUST]
    nice = [g for g in result["нет"] if g["уровень"] == NICE]
    for g in must + nice:
        mark = "НЕТ " if g["уровень"] == MUST else "жел "
        print(f"{mark}{g['чего']}\n     → {g['куда']}")
    if must:
        print(f"\nбаза НЕ достаточна: обязательного не хватает — {len(must)}, "
              f"желательного — {len(nice)}")
    else:
        print(f"\nбаза достаточна (желательного не хватает — {len(nice)})")
    return result["код"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=("draft", "questions", "audit"),
                    metavar="команда", help="draft | questions | audit")
    ap.add_argument("--repos-root", default=os.environ.get("AGENT_REPOS_ROOT", ""),
                    help="корень клонов репозиториев проекта")
    ap.add_argument("--env-file", default="",
                    help="файл окружения инсталляции; без него берётся окружение процесса")
    args = ap.parse_args(argv)
    if not args.repos_root:
        print("не задан --repos-root (и нет AGENT_REPOS_ROOT)", file=sys.stderr)
        return 2
    repos_root = Path(args.repos_root)
    env = read_env(Path(args.env_file)) if args.env_file else dict(os.environ)

    if args.cmd == "draft":
        return _print_draft(scan(repos_root), repos_root)
    if args.cmd == "questions":
        return _print_questions(questions(repos_root, env))
    return _print_audit(audit(repos_root, env))


if __name__ == "__main__":
    sys.exit(main())
