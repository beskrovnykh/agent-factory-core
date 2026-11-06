"""Граница автомержа: умолчание — человек, самозапрет конфигурацией не снять.

Три вещи, ради которых этот набор стоит держать:

1. **Умолчание.** Инсталляция, которая про автомерж ничего не сказала, не
   вливает ничего. Забытая переменная не должна открывать даже щель.
2. **Самозапрет.** Даже зона `allow=**` не даёт агенту переписать сам
   классификатор — иначе он расширит себе зону тем же автомержем.
3. **Направление промаха.** Ошибка разбора, непонятный путь, пустой список
   файлов — всё это отказ, а не разрешение.
"""
import logging
from pathlib import Path

import pytest

from agent_factory.automerge import (ENV_VAR, SELF_GUARD, Decision, Zone,
                                     decide, parse_zones, zones_from_env)

BACKEND = "org/backend"
WIDE = {BACKEND: Zone(allow=("**",))}


# ── умолчание: всё через человека ───────────────────────────────────────

def test_без_конфигурации_не_мержится_ничего():
    for path in ("README.md", "docs/a.md", "src/deep/file.py"):
        d = decide(BACKEND, [path], {})
        assert not d.automerge
        assert "нет в зонах" in d.reason


def test_пустая_переменная_даёт_пустую_таблицу():
    assert zones_from_env({}) == {}
    assert zones_from_env({ENV_VAR: ""}) == {}
    assert zones_from_env({ENV_VAR: "   ;  "}) == {}


def test_пустой_словарь_окружения_не_съезжает_на_процесс(monkeypatch):
    # На хосте инсталляции в окружении сессии лежат НАСТОЯЩИЕ значения юнитов.
    # `environ or os.environ` подсунул бы их сюда, и тест «без конфигурации»
    # проверял бы чужую живую зону вместо пустой.
    monkeypatch.setenv(ENV_VAR, f"{BACKEND}: allow=**")
    assert zones_from_env({}) == {}
    assert zones_from_env() == {BACKEND: Zone(allow=("**",))}


def test_decide_без_таблицы_спрашивает_окружение(monkeypatch):
    monkeypatch.setenv(ENV_VAR, f"{BACKEND}: allow=docs/**")
    assert decide(BACKEND, ["docs/a.md"]).automerge
    assert not decide(BACKEND, ["src/a.py"]).automerge


# ── самозапрет ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "core/agent_factory/automerge.py",
    "core/agent_factory/test_automerge.py",
    "events-api/automerge.py",
    "events-api/tools/test_automerge.py",
])
def test_самозапрет_не_снимается_самой_широкой_зоной(path):
    d = decide(BACKEND, [path], WIDE)
    assert not d.automerge
    assert d.reason == "путь задаёт саму границу автомержа"
    assert d.path == path


def test_самозапрет_сильнее_явного_allow_на_этот_же_файл():
    zones = {BACKEND: Zone(allow=("core/agent_factory/automerge.py",))}
    assert not decide(BACKEND, ["core/agent_factory/automerge.py"], zones)


def test_самозапрет_портит_весь_mr_а_не_только_свой_файл():
    d = decide(BACKEND, ["docs/a.md", "core/agent_factory/automerge.py"], WIDE)
    assert not d.automerge
    assert d.path == "core/agent_factory/automerge.py"


def test_самозапрет_не_ловит_однофамильцев():
    # Запрет обязан быть узким: `automerge.py` — да, `automerge_ui.py` или
    # каталог с таким именем — нет. Иначе зона сужается там, где никто не
    # просил, и это выглядит как поломка автомержа.
    assert decide(BACKEND, ["src/automerge_ui.py"], WIDE).automerge
    assert decide(BACKEND, ["src/automerge/readme.md"], WIDE).automerge


# ── allowlist, а не denylist ────────────────────────────────────────────

def test_путь_вне_allow_идёт_к_человеку():
    zones = {BACKEND: Zone(allow=("docs/**",))}
    d = decide(BACKEND, ["src/app.py"], zones)
    assert not d.automerge
    assert d.reason == "путь вне allow зоны"
    assert d.path == "src/app.py"


def test_deny_сильнее_allow():
    zones = {BACKEND: Zone(allow=("docs/**",), deny=("docs/secret/**",))}
    assert decide(BACKEND, ["docs/a.md"], zones).automerge
    d = decide(BACKEND, ["docs/secret/keys.md"], zones)
    assert not d.automerge
    assert d.reason == "путь в deny зоны"


def test_чужой_репозиторий_не_наследует_зону():
    zones = {BACKEND: Zone(allow=("**",))}
    assert not decide("org/frontend", ["docs/a.md"], zones).automerge


def test_проверяются_все_файлы_а_не_первый():
    zones = {BACKEND: Zone(allow=("docs/**",))}
    d = decide(BACKEND, ["docs/a.md", "docs/b.md", "src/app.py"], zones)
    assert not d.automerge
    assert d.path == "src/app.py"


def test_пустой_список_файлов_не_мержится():
    d = decide(BACKEND, [], WIDE)
    assert not d.automerge
    assert "не видно" in d.reason


def test_путь_за_пределы_репозитория_не_мержится():
    zones = {BACKEND: Zone(allow=("docs/**",))}
    for path in ("docs/../../etc/shadow", "/etc/shadow", "../secrets"):
        d = decide(BACKEND, [path], zones)
        assert not d.automerge, path


# ── глобы ───────────────────────────────────────────────────────────────

def test_звёздочка_не_пересекает_слэш_а_двойная_пересекает():
    one = {BACKEND: Zone(allow=("docs/*",))}
    assert decide(BACKEND, ["docs/a.md"], one).automerge
    assert not decide(BACKEND, ["docs/inner/a.md"], one).automerge

    two = {BACKEND: Zone(allow=("docs/**",))}
    assert decide(BACKEND, ["docs/inner/a.md"], two).automerge


def test_глоб_совпадает_целиком_а_не_началом():
    zones = {BACKEND: Zone(allow=("docs/a.md",))}
    assert decide(BACKEND, ["docs/a.md"], zones).automerge
    assert not decide(BACKEND, ["docs/a.md.bak"], zones).automerge


def test_точка_в_глобе_это_точка():
    zones = {BACKEND: Zone(allow=("docs/?.md",))}
    assert decide(BACKEND, ["docs/a.md"], zones).automerge
    assert not decide(BACKEND, ["docs/axmd"], zones).automerge


# ── разбор конфигурации ─────────────────────────────────────────────────

def test_разбор_таблицы_из_одной_строки():
    raw = ("org/backend: allow=docs/**,ci/** deny=docs/secret/** ; "
           "org/site: allow=**")
    assert parse_zones(raw) == {
        "org/backend": Zone(allow=("docs/**", "ci/**"),
                            deny=("docs/secret/**",)),
        "org/site": Zone(allow=("**",)),
    }


def test_слэши_вокруг_имени_репозитория_не_считаются():
    assert parse_zones("/org/backend/: allow=docs/**") == {
        "org/backend": Zone(allow=("docs/**",))}
    assert decide("/org/backend", ["docs/a.md"],
                  parse_zones("org/backend: allow=docs/**")).automerge


@pytest.mark.parametrize("raw", [
    "org/backend allow=docs/**",       # нет двоеточия
    ": allow=docs/**",                 # нет имени репозитория
    "org/backend: dney=docs/secret/**",  # опечатка в поле
    "org/backend: allow",              # поле без значения
    "org/backend: allow=a; org/backend: allow=b",  # зона дважды
])
def test_кривая_конфигурация_это_ошибка_а_не_пропуск(raw):
    # Опечатка в `deny=` при молчаливом пропуске РАСШИРИЛА бы зону — промах
    # в единственную сторону, в которую ошибаться нельзя.
    with pytest.raises(ValueError):
        parse_zones(raw)


def test_кривая_конфигурация_гасит_автомерж_целиком_и_говорит_об_этом(caplog):
    raw = "org/backend: allow=docs/** dney=docs/secret/**"
    with caplog.at_level(logging.ERROR, logger="intake"):
        assert zones_from_env({ENV_VAR: raw}) == {}
    assert any("автомерж выключен целиком" in r.getMessage()
               for r in caplog.records)
    assert not decide("org/backend", ["docs/a.md"], zones_from_env({ENV_VAR: raw}))


# ── исход ───────────────────────────────────────────────────────────────

def test_отказ_ложный_а_разрешение_истинное():
    # `if decide(...)` у обычного NamedTuple было бы истиной ВСЕГДА: отказ
    # прочитался бы как «мержим». Промах молчаливый и ровно не в ту сторону.
    assert not Decision(False, "нет")
    assert Decision(True, "да")
    assert not decide(BACKEND, ["src/app.py"], {BACKEND: Zone(allow=("docs/**",))})


def test_самозапрет_называет_настоящий_путь_этого_модуля():
    # Литерал в SELF_GUARD гниёт при переименовании файла, и гниёт молча:
    # классификатор просто перестаёт охранять себя. Сверяем с тем, где
    # модуль лежит на самом деле.
    from pathlib import Path

    from agent_factory import automerge

    root = Path(__file__).resolve().parents[2]
    for module in (automerge.__file__, __file__):
        rel = Path(module).resolve().relative_to(root).as_posix()
        assert rel in SELF_GUARD, rel
        assert not decide("any/repo", [rel], {"any/repo": Zone(allow=("**",))})


def test_путь_не_подгоняется_под_зону_обрезкой_пробелов():
    # Имя файла с пробелом на конце — законное. Обрезка превратила бы его в
    # соседнее имя, и файл влился бы по правилу, написанному про другой.
    zones = {BACKEND: Zone(allow=("docs/a.md",))}
    d = decide(BACKEND, ["docs/a.md "], zones)
    assert not d.automerge
    assert d.path == "docs/a.md "


# ── самозапрет в корне репозитория ──────────────────────────────────────

@pytest.mark.parametrize("path", ["automerge.py", "test_automerge.py"])
def test_самозапрет_ловит_копию_классификатора_в_корне(path):
    """Глоб `**/x.py` требует слэша и файл в корне НЕ ловит.

    Инсталляция вольна держать копию классификатора где угодно, в том числе
    в корне своего репозитория. Пока корень не назван отдельной строкой,
    зона `allow=**` такую копию вливала — то есть самозапрет у неё не
    работал вовсе, а по остальным путям выглядел целым.
    """
    assert decide("org/backend", [path], WIDE) == Decision(
        False, "путь задаёт саму границу автомержа", path)


# ── имя переменной: код и заготовка окружения обязаны совпадать ─────────

def test_имя_переменной_совпадает_с_заготовкой_инсталляции():
    """Продукт читает `ENV_VAR`, а пишет её в окружение `install.sh`.

    Пара ничем не связана, кроме строки: разъедутся — настройка молча
    перестанет находиться, таблица окажется пустой, и всё пойдёт через
    человека. Направление отказа безопасное, но владелец инсталляции,
    заполнивший зону, не получит ни ошибки, ни признака.

    Умолчание проверяется здесь же: заготовка обязана быть ПУСТОЙ.
    """
    root = Path(__file__).resolve().parent.parent.parent
    install_sh = root / "install.sh"
    example = root / "installation.conf.example"
    # Без этого тест прошёл бы вхолостую на любой перестановке каталогов.
    assert install_sh.is_file(), f"не нашёл {install_sh}"
    assert example.is_file(), f"не нашёл {example}"

    assert f'\n{ENV_VAR}=""\n' in install_sh.read_text(encoding="utf-8"), (
        f"{ENV_VAR} не объявлена в заготовке окружения install.sh "
        f"или объявлена с непустым умолчанием")
    assert ENV_VAR in example.read_text(encoding="utf-8"), (
        f"{ENV_VAR} не описана в installation.conf.example")
