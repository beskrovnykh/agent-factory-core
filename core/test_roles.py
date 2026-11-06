"""Что стережёт раскладку по ролям (DEV-13).

Каждая проверка здесь — про промах, который НЕ ВИДНО иначе: ни тестами
контрактов (они смотрят отдельные фразы), ни установщиком (он отрабатывает
успешно), ни пайплайном.
"""
import ast
import pathlib
import re

import personas
import prompt

CORE = pathlib.Path(__file__).resolve().parent
ROLES = CORE / "roles"
REPO = CORE.parent


def _names_taken_from_prompt() -> set[str]:
    """Что РЕАЛЬНО берут у фасада — разбором исходников, а не списком в тесте.

    Списком это уже было: он протухает молча, потому что тест зелен и когда
    имя перестали брать, и когда начали брать новое (DEV-13).
    """
    out: set[str] = set()
    for path in CORE.rglob("*.py"):
        if ROLES in path.parents or path.name == "prompt.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "prompt":
                out.update(a.name for a in node.names)
    return out


def test_facade_gives_everything_the_core_takes_from_it():
    missing = sorted(_names_taken_from_prompt() - set(prompt.__all__))
    assert not missing, f"фасад не отдаёт того, что у него берут: {missing}"


def test_all_names_in_dunder_all_actually_resolve():
    """`import *` ломается молча: имя в `__all__` без атрибута — ImportError
    только у того, кто написал `from prompt import *`, а раннер пишет явно."""
    absent = sorted(n for n in prompt.__all__ if not hasattr(prompt, n))
    assert not absent, f"в __all__ есть имена, которых фасад не отдаёт: {absent}"


def test_module_per_kind_matches_the_live_persona_map():
    """Имя модуля = значение `kind`. Сверяем с живой картой личностей, а не со
    своим кортежем: свой список — четвёртая копия, расходящаяся без признака."""
    modules = {p.stem for p in ROLES.glob("*.py")} - {"__init__", "skills", "installation"}
    assert modules == set(personas.PERSONA_BY_KIND), (
        f"роли {sorted(modules)} против видов {sorted(personas.PERSONA_BY_KIND)}")


def test_paths_handed_to_the_session_exist():
    """Капкан на глубину пакета: `roles/` лежит на уровень глубже, чем лежал
    `prompt.py`, и `parent` вместо `parent.parent` даёт путь, которого нет.
    Сессия узнала бы об этом командой, которая не запускается."""
    for name in ("LOGDIFF_PATH", "GATE_PATH"):
        path = getattr(prompt, name)
        assert path.exists(), f"{name} = {path} — файла нет"
        assert path.parent == CORE, f"{name} указывает мимо ядра: {path}"


def test_the_shared_skill_reaches_every_assembled_contract():
    """Врезка лежит в `skills.py` ровно потому, что её читают все роли. Осталась
    бы она у одной — это уже не навык, и место ей в контракте роли.

    Смотрим на СОБРАННЫЙ промпт, а не на текст модуля: грепом по исходнику эта
    проверка проходит вхолостую — `import memory_block as _mb` содержит то же
    слово, и подмена вызова остаётся незамеченной (поймано мутацией на DEV-13).
    """
    task = {"identifier": "X", "items": []}
    built = {
        "work": prompt.build_prompt(task, "/w", "/r"),
        "verify": prompt.build_verify_prompt(task, "/w", "/r"),
        "intake": prompt.build_intake_prompt(task, "/w", "/r"),
        "incident": prompt.build_incident_prompt(task),
    }
    assert set(built) == set(personas.PERSONA_BY_KIND), "проверены не все виды"
    without = sorted(k for k, text in built.items() if prompt.MEMORY_BLOCK not in text)
    assert not without, f"память роли не доехала в контракты: {without}"


def test_contracts_have_no_shared_text_to_extract():
    """Основание, по которому общих навыков ровно один, — замер, а не мнение.

    Порог держит следующего, кому покажется, что «общие куски» надо вынести:
    выросло дословное пересечение — значит появилась настоящая общность, и
    тогда это осознанная правка теста, а не молчаливый дрейф.
    """
    from difflib import SequenceMatcher
    from itertools import combinations
    bodies = {k: getattr(prompt, t).replace(prompt.MEMORY_BLOCK, "") for k, t in (
        ("work", "TEMPLATE"), ("verify", "VERIFY_TEMPLATE"),
        ("intake", "INTAKE_TEMPLATE"), ("incident", "INCIDENT_TEMPLATE"))}
    worst = 0
    for a, b in combinations(bodies, 2):
        x, y = bodies[a], bodies[b]
        worst = max(worst, SequenceMatcher(None, x, y, autojunk=False)
                    .find_longest_match(0, len(x), 0, len(y)).size)
    assert worst < 400, (
        f"дословное пересечение контрактов выросло до {worst} символов — "
        "появилась общность, которой не было; решай, навык это или совпадение")


def test_installer_and_release_carry_core_packages():
    """Самая дорогая грабля этой правки: и установщик, и релизный архив кладут
    `core/*.py` плоским шаблоном. Подкаталог они пропускают МОЛЧА и при этом
    отрабатывают успешно — раньше это раннер не роняло, а теперь `import
    prompt` без `roles/` не собирается вовсе. Поломку видно только мёртвыми
    сессиями у клиента, поэтому она проверяется здесь, а не там."""
    marker = re.compile(r'\[ -f "\$d/__init__\.py" \] \|\| continue')
    for rel in ("install.sh", ".github/workflows/release.yml"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert marker.search(text), f"{rel} не везёт пакеты ядра каталогами"


def test_installer_delivers_every_skill_in_the_kit():
    """Грабля того же вида, что и у пакетов выше, только этажом ниже. Скилы
    установщик знал ПОИМЁННО (`SKILL_DST="$RUN_HOME/.claude/skills/code-review"`),
    и второй скил комплекта, `daily`, на хост не попадал вовсе. Признака у
    этого нет: скил, которого модель не видит, ведёт себя в точности как скил,
    которого нет, — а установщик при этом отрабатывает успешно и печатает
    «поставлен». Релизный архив (`release.yml`) везёт `core/skills` целиком,
    так что расхождение видно только на инсталляции.

    Проверяется не наличие имён, а их ОТСУТСТВИЕ в раскладке: список скилов
    установщик обязан брать с диска."""
    text = (REPO / "install.sh").read_text(encoding="utf-8")
    skills = sorted(p.name for p in (REPO / "core" / "skills").iterdir()
                    if (p / "SKILL.md").is_file())
    assert len(skills) >= 2, (
        f"скилов в комплекте {skills}: проверка «забудет второй» вхолостую — "
        "она ничего не стережёт, пока скил один")
    assert re.search(r'for d in "\$SRC"/core/skills/\*/', text), (
        "install.sh не обходит core/skills/ с диска — новый скил комплекта "
        "молча не доедет до хоста")
    laying = [ln for ln in text.splitlines()
              if "install -m 644" in ln and "SKILL.md" in ln]
    assert laying, "install.sh не кладёт ни одного SKILL.md"
    for line in laying:
        for name in skills:
            assert name not in line, (
                f"раскладка скилов поимённа ({name!r} в {line.strip()!r}): "
                "следующий скил комплекта на хост не попадёт")
