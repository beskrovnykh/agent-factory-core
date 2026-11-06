"""Граница автомержа: что агент вливает сам, а что решает человек.

Часть ПРОДУКТА, но без единого имени клиента внутри. Таблицу зон приносит
одна переменная инсталляции — `AGENT_AUTOMERGE_ZONES`, там же, где
`AGENT_KNOWN_REPOS` и `AGENT_ENV_CANON`. До этого она была зашита литералом
в код первого клиента: репозитории, пути, ширина зоны — всё про него.
Вторая инсталляция с такой таблицей не заводится вовсе, а ширину зоны
определяет цена поломки, и знает её владелец инсталляции, а не автор ядра.

Устройство перенесено как было, менять его не надо:

- **Allowlist, а не denylist.** Незнакомый путь → человек. Denylist
  безопасен ровно до первого файла, о котором автор списка не подумал, и
  промах у него молчаливый: в MR ничего не написано, он просто вливается.
- **Зоны по репозиториям.** Цена поломки у каждого репозитория своя.
- **Самозапрет.** Классификатор перечисляет собственный путь, и снять этот
  запрет конфигурацией нельзя — иначе агент расширил бы себе зону тем же
  автомержем, который эта граница и ограничивает.

Что дал вынос в конфигурацию, кроме имён. Границу теперь задаёт файл
окружения инсталляции, которого нет НИ В ОДНОМ репозитории, — значит MR не
двигает её в принципе, чем бы он ни был. В коде остаётся ровно та часть
самозапрета, до которой MR ещё дотягивается: путь самого классификатора и
путь капканов на него.

Умолчание — пустой allowlist, то есть «всё через человека». Новая
инсталляция начинает с самого узкого и расширяет осознанно; забытая или
кривая переменная не открывает ничего (`zones_from_env` на разборе падает в
пустую таблицу и говорит об этом в журнал).

Кто вызывает. Продукт только отвечает на вопрос «этот MR можно влить без
человека?». Команду `/automerge` и сам мерж держит инсталляция
(`channel.commands.register`, см. commands.py): мержит она своим форжем, а
форжа продукт не знает.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Mapping, NamedTuple

log = logging.getLogger("intake")

ENV_VAR = "AGENT_AUTOMERGE_ZONES"

# Самозапрет: пути, которыми меняют саму границу. Не снимается никакой
# конфигурацией — проверяется ДО зоны, а не внутри неё. Первая пара — этот
# файл и капканы на него в репозитории продукта; `**/` — он же, если
# инсталляция держит копию у себя, на любой глубине; последняя пара — она
# же в КОРНЕ репозитория. Корень приходится называть отдельно: `**/x.py`
# требует слэша и файл `x.py` без каталога не ловит, то есть инсталляция с
# копией классификатора в корне осталась бы без самозапрета вовсе.
# Капканы охраняются наравне с кодом: MR, который их удалит, оставит
# самозапрет без доказательства, и заметить это будет нечем.
SELF_GUARD = (
    "core/agent_factory/automerge.py",
    "core/agent_factory/test_automerge.py",
    "**/automerge.py",
    "**/test_automerge.py",
    "automerge.py",
    "test_automerge.py",
)


class Zone(NamedTuple):
    """Ширина зоны одного репозитория. `deny` сильнее `allow`."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


class Decision(NamedTuple):
    automerge: bool
    reason: str
    # Путь, который решил исход: без него отказ нечем объяснить человеку, а
    # именно объяснение делает узкую зону терпимой.
    path: str = ""

    def __bool__(self) -> bool:
        # Без этого `if decide(...)` был бы истиной ВСЕГДА (непустой кортеж),
        # то есть отказ читался бы как разрешение мержить. Ошибка молчаливая
        # и в нужную сторону не падает — поэтому истинность здесь своя.
        return self.automerge


def parse_zones(raw: str) -> dict[str, Zone]:
    """«org/backend: allow=docs/**,ci/** deny=docs/secret/**; org/site: allow=**»

    Одна строка намеренно: файл окружения читает systemd (`EnvironmentFile`),
    а он многострочных значений не умеет. Разделители — `;` между зонами и
    пробел между полями, поэтому ни в имени репозитория, ни в глобе их быть
    не может.

    Непонятое — это `ValueError`, а не пропуск: опечатка в `deny=` иначе
    молча РАСШИРИЛА бы зону, а тихо расширяющаяся граница хуже отсутствующей.
    """
    zones: dict[str, Zone] = {}
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        repo, sep, rest = chunk.partition(":")
        repo = repo.strip().strip("/")
        if not sep or not repo:
            raise ValueError(f"зона без имени репозитория: {chunk!r}")
        allow: list[str] = []
        deny: list[str] = []
        for token in rest.split():
            key, eq, value = token.partition("=")
            if not eq or key not in ("allow", "deny"):
                raise ValueError(f"непонятное поле {token!r} у зоны {repo!r}")
            globs = [g.strip() for g in value.split(",") if g.strip()]
            (allow if key == "allow" else deny).extend(globs)
        if repo in zones:
            raise ValueError(f"зона {repo!r} задана дважды")
        zones[repo] = Zone(tuple(allow), tuple(deny))
    return zones


def zones_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Zone]:
    """Таблица зон из конфигурации инсталляции. Нет переменной, пусто, не
    разобралось — пустая таблица, то есть всё через человека.

    `environ` берётся явно, если передан, — включая пустой словарь. Писать
    `environ or os.environ` тут нельзя: `{}` означает «окружения нет», а не
    «спроси процесс», и на хосте с живым окружением подмена молча съезжала
    бы на настоящие значения.
    """
    src = os.environ if environ is None else environ
    raw = src.get(ENV_VAR, "") or ""
    try:
        return parse_zones(raw)
    except ValueError as exc:
        log.error("%s не разобрана (%s) — автомерж выключен целиком, "
                  "всё идёт через человека", ENV_VAR, exc)
        return {}


def decide(repo: str, paths: Iterable[str],
           zones: Mapping[str, Zone] | None = None) -> Decision:
    """Можно ли влить этот MR без человека.

    Отказ всегда конкретный: назван путь, из-за которого он случился, —
    иначе владельцу нечем решить, промах это зоны или промах агента.
    """
    table = zones_from_env() if zones is None else zones
    zone = table.get((repo or "").strip().strip("/"))
    if zone is None:
        return Decision(False, "репозитория нет в зонах автомержа")

    # Путь берётся как есть: `strip()` по нему превратил бы «docs/a.md » в
    # «docs/a.md», и файл с пробелом на конце влился бы по правилу, которое
    # написано про другой файл. Пустое отбрасываем, остальное — как пришло:
    # мусор в пути виден человеку в отказе, а не молча подгоняется под зону.
    files = [str(p) for p in (paths or ()) if str(p).strip()]
    if not files:
        # Пустой список — это чаще всего «не смогли прочитать изменения»,
        # а не «MR ничего не трогает». Мержить по неизвестности нельзя.
        return Decision(False, "изменённых файлов не видно")

    for path in files:
        if not _inside_repo(path):
            return Decision(False, "путь ведёт за пределы репозитория", path)
        if _matches(SELF_GUARD, path):
            return Decision(False, "путь задаёт саму границу автомержа", path)
        if _matches(zone.deny, path):
            return Decision(False, "путь в deny зоны", path)
        if not _matches(zone.allow, path):
            return Decision(False, "путь вне allow зоны", path)
    return Decision(True, f"все файлы ({len(files)}) в зоне «{repo}»")


# ── глобы ───────────────────────────────────────────────────────────────
# `**` пересекает `/`, `*` и `?` — нет. Разница тут не украшение: `allow=*`
# без неё разрешил бы весь репозиторий вместе с подкаталогами, а зона,
# которая шире написанного, — ровно то, ради чего этот файл существует.

_CACHE: dict[str, re.Pattern] = {}


def _rx(pattern: str) -> re.Pattern:
    rx = _CACHE.get(pattern)
    if rx is None:
        rx = re.compile(_translate(pattern))
        _CACHE[pattern] = rx
    return rx


def _translate(pattern: str) -> str:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i + 1:i + 2] == "*":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _matches(patterns: Iterable[str], path: str) -> bool:
    return any(_rx(p).fullmatch(path) for p in patterns)


def _inside_repo(path: str) -> bool:
    """Путь обязан быть относительным и не выходить наверх.

    Глобы такое не ловят: `docs/**` совпадёт с `docs/../../etc/shadow`, и
    зона, написанная про каталог документации, оказалась бы про весь диск.
    """
    if path.startswith("/") or path.startswith("\\"):
        return False
    parts = path.replace("\\", "/").split("/")
    return ".." not in parts
