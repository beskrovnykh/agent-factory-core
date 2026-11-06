"""Конверт записи памяти и разовый перенос живых сторов на него (DEV-85).

Замер по 267 живым записям: **ни одна** не несёт машиночитаемой даты,
источника, срока пересмотра и уровня авторитета. Сессия, которой запись
попала в контекст, не может отличить правило, которое нельзя нарушать, от
наблюдения полугодовой давности, которое давно неверно, — и читает оба
одинаково уверенно. Конверт — семь полей во frontmatter, которые этот вопрос
закрывают:

| поле | что отвечает | откуда берётся при миграции |
|------|--------------|------------------------------|
| `scope` | к чему запись применима | каталог стора (даёт вызывающий) |
| `roles` | кому адресована | каталог роли (`agent-work` → `work`) |
| `kind` | что это за знание | ЭВРИСТИКА по тексту и frontmatter |
| `source` | откуда знание | ЭВРИСТИКА: номер задачи из прозы |
| `verified` | когда последний раз проверено | ЭВРИСТИКА: дата из прозы |
| `supersedes` | что эта запись заменяет | никогда: только руками |
| `review_after` | после какой даты пересмотреть | считается из `verified` и `kind` |

Почему есть поле `migrated`
---------------------------
Мигратор читает прозу регулярками и ошибается: дата в примере команды
(`journalctl --since 2026-09-01`) для него неотличима от даты записи, а `kind`
он и вовсе выводит по косвенным признакам. Проставленное догадкой поле
выглядит в файле ровно так же, как поставленное человеком, — и следующая
сессия принимает догадку за факт. Это ровно та болезнь, от которой конверт и
заводится, только теперь с машинной печатью достоверности.

Поэтому всё, что мигратор вывел сам, перечислено поимённо в `migrated:`.
Читающий видит, чему верить: `verified: 2026-09-13` без пометки — человек так
сказал, с пометкой — программа так решила по тексту.

`scope` и `roles` в `migrated` НЕ попадают: они не выведены из прозы, а взяты
из раскладки стора (запись лежит в каталоге роли — это факт файловой системы)
и из аргумента вызывающего. `kind` попадает ВСЕГДА, даже когда эвристика
уверена: у неё нет способа отличить уверенность от совпадения слов.

Почему `ограничение` не получает `review_after`
----------------------------------------------
Срок пересмотра — признание, что знание протухает: факт о мире живёт 30 дней,
рецепт команд — 90, процедура и справка — полгода. Ограничение («не удалять
сессии», «не выдумывать номера задач») не протухает по времени вовсе: его
отменяет только владелец и только явно. Поставить ему срок значило бы
разрешить сессии считать правило устаревшим просто потому, что прошёл
квартал, — поэтому поля у него нет совсем, а не большое число в нём.

Сохранность
-----------
Стор отдаётся клиенту и правится людьми, поэтому мигратор устроен как
дописывание, а не как переписывание:

* существующие поля (`name`, `description`, `metadata`) и тело файла не
  трогаются — новые строки дописываются перед закрывающим `---`;
* поле конверта, которое в файле уже есть, не перезаписывается НИКОГДА, даже
  пустое: пустое значение — тоже чьё-то решение;
* по умолчанию работа идёт в dry-run, на диск пишет только `--apply`;
* перед записью исходный файл целиком уходит в `_before-migrate/` рядом.
  Владелец не удаляет ничего — старое уводится в подкаталог.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# Имена, которые записями НЕ считаются: индекс слоя и пояснительная записка.
# Держим тот же список, что `memory.NOT_ENTRIES`, — мигратор ходит по тем же
# каталогам, и разойтись этим двум спискам нельзя.
NOT_ENTRIES = {"MEMORY.md", "README.md"}

# Куда уезжает исходный файл перед записью нового.
BACKUP_DIR = "_before-migrate"

# Поля конверта в том порядке, в каком они дописываются в файл.
ENVELOPE = ("scope", "roles", "kind", "source", "verified", "supersedes",
            "review_after")

# Сколько живёт знание каждого вида, в днях. `ограничение` в таблице
# отсутствует намеренно — см. docstring модуля.
LIFETIME_DAYS = {
    "рецепт": 90,      # команды устаревают с первым переездом каталога
    "факт": 30,        # состояние мира меняется быстрее всего
    "процедура": 180,
    "справка": 180,
}

# ── чтение frontmatter ──────────────────────────────────────────────────────

def split_front(text: str) -> tuple[list[str] | None, str]:
    """(строки frontmatter, остаток файла начиная с закрывающего `---`).

    None вместо списка значит «frontmatter нет». Незакрытый frontmatter
    читается как отсутствующий по той же причине, что в `memory.entry_of`:
    иначе строка `name:` из прозы в теле сошла бы за поле.
    """
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end < 0:
        return None, text
    return text[3:end].split("\n")[1:], text[end:]


def top_keys(front: list[str]) -> set[str]:
    """Ключи верхнего уровня. Вложенные (`  type:` внутри `metadata:`) сюда не
    попадают: конверт живёт на верхнем уровне, и `metadata.type` ему не тёзка."""
    keys = set()
    for line in front:
        m = re.match(r"([A-Za-z_][\w-]*):", line)
        if m:
            keys.add(m.group(1))
    return keys


def own_value(front: list[str] | None, key: str) -> str | None:
    """Значение поля верхнего уровня, как оно стоит в файле. None — поля нет."""
    for line in front or []:
        m = re.match(rf"{re.escape(key)}:\s*(.*)", line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def own_kind(front: list[str] | None) -> str | None:
    """`kind`, уже стоящий в файле, — он старше любой эвристики."""
    return own_value(front, "kind") or None


def own_verified(front: list[str] | None) -> date | None:
    """`verified`, уже стоящий в файле. Нечитаемое значение — не повод считать
    его своим: молча заменить чужую дату хуже, чем оставить срок непосчитанным."""
    raw = own_value(front, "verified")
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def nested_value(front: list[str], parent: str, key: str) -> str | None:
    """Значение вложенного поля (`metadata:` → `type:`). Нужно ровно одному
    месту — эвристике `kind`, которой существующий `metadata.type` даёт лучший
    сигнал из всех имеющихся."""
    inside = False
    for line in front:
        if re.match(r"[A-Za-z_]", line):
            inside = line.startswith(f"{parent}:")
            continue
        if inside:
            m = re.match(rf"\s+{re.escape(key)}:\s*(.+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return None


# ── дата проверки ───────────────────────────────────────────────────────────

# Даты в живых записях написаны прозой и по-разному. Общее у всех регулярок —
# запрет на соседнюю цифру, точку или дефис: без него `198.51.100.22` даёт
# «13 октября», а `2026-09-14` — кусок от версии. IP-адресов и номеров версий
# в этих записях больше, чем дат.
#
# Справа запрещена не всякая точка, а только точка перед цифрой: `...2026.5` —
# продолжение номера версии, а `Зафиксировано 13.09.2026.` — конец фразы, и
# такими фразами записи заканчиваются чаще всего.
_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d-])")
_DMY = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)(?!\.\d)")
# Без года день и месяц обязаны быть двузначными: иначе версии `3.10` и `3.12`
# читаются как 3 октября и 3 декабря. Записи пишут `14.09`, а не `4.9`.
_DM = re.compile(r"(?<![\d.])(\d{2})\.(\d{2})(?!\d)(?!\.\d)")

_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
           "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11,
           "декабр": 12}
# `с 12–13 сентября`, `14 сентября 2026`. День обязателен: без него «к
# сентябрю» и «майской атаки» дали бы дату там, где её не называли. Из
# диапазона берётся правый конец — проверено позже, значит верно позже.
# Окончание — ровно одна буква и дальше не буква: иначе корень `ма` съедал бы
# «12 машин» как 12 мая.
_BY_NAME = re.compile(
    r"(?<!\d)(\d{1,2})(?:\s*[–—-]\s*(\d{1,2}))?\s+"
    r"(январ|феврал|март|апрел|ма|июн|июл|август|сентябр|октябр|ноябр|декабр)"
    r"[ьяеюйа]?(?![а-яё])(?:\s+(\d{4}))?", re.IGNORECASE)


def _day(y: int, m: int, d: int, today: date) -> date | None:
    """Дата, если она существует и не в будущем. Будущее не принимаем: поле
    отвечает «когда это проверяли», а проверить в будущем нельзя — такая дата
    в тексте всегда про что-то другое (срок, план, пример)."""
    try:
        got = date(y, m, d)
    except ValueError:
        return None
    return got if got <= today else None


def _guess_year(m: int, d: int, today: date) -> date | None:
    """Дата формы без года (`14.09`, `12–13 сентября`) — ближайшая прошедшая.

    Год берётся текущий, а если так получается будущее — прошлый. Это и есть
    главная слабость формы без года: запись прошлогоднего сентября мигратор
    пометит этим. Поэтому `verified` всегда уходит в `migrated`.
    """
    return _day(today.year, m, d, today) or _day(today.year - 1, m, d, today)


def infer_verified(text: str, today: date) -> date | None:
    """Самая поздняя дата из текста, не позднее сегодняшней.

    Самая поздняя, а не первая: запись правят дописыванием, и свежая строка
    внизу говорит о проверке больше, чем абзац, с которого всё начиналось.
    """
    found: list[date] = []
    for y, m, d in _ISO.findall(text):
        got = _day(int(y), int(m), int(d), today)
        if got:
            found.append(got)
    for d, m, y in _DMY.findall(text):
        got = _day(int(y), int(m), int(d), today)
        if got:
            found.append(got)
    for d, m in _DM.findall(text):
        got = _guess_year(int(m), int(d), today)
        if got:
            found.append(got)
    for d1, d2, name, y in _BY_NAME.findall(text):
        month = _MONTHS[name.lower()]
        day = int(d2 or d1)
        got = (_day(int(y), month, day, today) if y
               else _guess_year(month, day, today))
        if got:
            found.append(got)
    return max(found) if found else None


# ── источник ────────────────────────────────────────────────────────────────

_TASK = re.compile(r"(?<![\w-])([A-Z]{2,6})-(\d+)(?![\w-])")
# Похожи на номер задачи, но ими не являются. Список закрытый и короткий:
# расширять его на глаз опаснее, чем изредка поставить `source: UTF-8` —
# неверный источник виден человеку сразу, а выброшенный настоящий не виден
# никому.
_NOT_TASKS = {"UTF", "ISO", "RFC", "UTC", "GMT", "SHA", "AES", "RGB", "HTTP",
              "IPV", "TLS", "SSL", "USD", "GPT", "API", "URL"}


def infer_source(text: str) -> str | None:
    """Номер задачи, из которой знание пришло, — первый в тексте.

    Первый, а не самый частый: записи открываются обстоятельствами («Установлено
    14.09.2026 (DEV-16)»), а дальше ссылаются на соседние задачи по ходу
    рассказа. Самый частый выиграла бы задача, о которой запись рассуждает, а
    не та, в которой её написали.

    Ничего не нашлось — поля не будет. `наблюдение` по умолчанию мы не ставим:
    отличить наблюдение сессии от слов владельца по тексту нельзя, а выдать
    второе за первое — потерять единственный источник, который сессия не вправе
    оспаривать.
    """
    for prefix, num in _TASK.findall(text):
        if prefix not in _NOT_TASKS:
            return f"{prefix}-{num}"
    return None


# ── вид записи ──────────────────────────────────────────────────────────────

# Запрет опознаём по капсу и по прямому обращению к читателю: «нельзя» в
# рассуждении («без базы нельзя проверить») встречается втрое чаще, чем в
# правиле, и на нём эвристика объявляла бы ограничением половину стора. А
# ограничение — единственный вид без срока пересмотра: ошибиться в его пользу
# дороже всего.
_BAN = re.compile(r"НИКОГДА|НЕЛЬЗЯ|ЗАПРЕЩ|НЕ ДЕЛА|Не делай|Не трогай|Не удаля"
                  r"|не вздумай|только по явн", re.UNICODE)
_SHELL = re.compile(r"```(?:bash|sh|shell|console|zsh)\b|^\s*\$ ", re.MULTILINE)
_STEPS = re.compile(r"Как применить|Как делать|Порядок действий|Пошагово"
                    r"|сначала .{0,60}(?:потом|затем)|Шаг 1|шаги:", re.IGNORECASE)

# Как читается существующее `metadata: type:`. Это лучший сигнал из имеющихся,
# но всё-таки перевод из старой классификации в новую, а не чтение нового поля.
_TYPE_KIND = {"feedback": "ограничение", "project": "факт",
              "reference": "справка"}


def infer_kind(text: str, front: list[str] | None) -> str:
    """Вид записи по тексту и старому `metadata.type`.

    Порядок проверок — по цене ошибки, а не по частоте: запрет важнее всего
    (у него нет срока пересмотра), дальше набор команд, дальше процедура, и
    только потом старый `type`. Результат ВСЕГДА уходит в `migrated`: у
    эвристики нет способа отличить попадание от совпадения слов.
    """
    old_type = nested_value(front or [], "metadata", "type")
    if _BAN.search(text) or old_type == "feedback":
        return "ограничение"
    if _SHELL.search(text):
        return "рецепт"
    if _STEPS.search(text):
        return "процедура"
    return _TYPE_KIND.get(old_type or "", "справка")


def review_after(kind: str, verified: date | None) -> date | None:
    """Дата пересмотра. Нет у ограничения (не протухает) и у записи без
    `verified` (не от чего отсчитывать — выдуманный срок хуже отсутствующего)."""
    days = LIFETIME_DAYS.get(kind)
    if days is None or verified is None:
        return None
    return verified + timedelta(days=days)


# ── миграция файла ──────────────────────────────────────────────────────────

@dataclass
class Result:
    """Что мигратор сделал бы (или сделал) с одним файлом."""
    path: Path
    role: str
    kind: str = ""
    verified: date | None = None
    source: str | None = None
    added: dict[str, str] = field(default_factory=dict)
    guessed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)   # поля, уже стоявшие в файле
    written: bool = False
    error: str = ""


def _fmt(value) -> str:
    """Значение поля в YAML. Список — потоком (`[work]`), чтобы дописывание
    оставалось одной строкой на поле и не спорило с отступами соседей."""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def envelope(text: str, *, scope: str, role: str,
             today: date) -> tuple[dict[str, object], list[str]]:
    """Поля конверта для текста и список тех из них, что выведены догадкой.

    Возвращает ПОЛНЫЙ конверт, не глядя на то, что уже есть в файле: решение
    «это поле уже стоит, не трогаем» принимается уровнем выше, в `migrate_text`,
    и одно на все поля.
    """
    front, _ = split_front(text)
    kind = infer_kind(text, front)
    verified = infer_verified(text, today)
    source = infer_source(text)

    fields: dict[str, object] = {"scope": scope, "roles": [role], "kind": kind}
    guessed = ["kind"]
    if source:
        fields["source"] = source
        guessed.append("source")
    if verified:
        fields["verified"] = verified.isoformat()
        guessed.append("verified")
    # Срок пересмотра считается от того, что БУДЕТ стоять в файле, а не от
    # догадки: если человек уже назвал запись ограничением или проставил свою
    # дату, догадка их не вытеснит — а `review_after`, посчитанный от неё, дал
    # бы срок жизни ограничению и отсчитал его от чужого числа.
    kind = own_kind(front) or kind
    verified = own_verified(front) or verified
    after = review_after(kind, verified)
    if after:
        fields["review_after"] = after.isoformat()
        guessed.append("review_after")
    # `supersedes` не ставим вовсе: что именно запись заменяет, знает только
    # тот, кто её писал. Пустое поле здесь было бы приглашением догадаться.
    return fields, guessed


def migrate_text(text: str, *, scope: str, role: str,
                 today: date) -> tuple[str, dict[str, str], list[str], list[str]]:
    """(новый текст, дописанные поля, выведенные догадкой, оставленные как были).

    Дописывание идёт перед закрывающим `---`: всё, что было выше, остаётся
    байт в байт, тело файла не читается вовсе. Файл без frontmatter получает
    его целиком — тело при этом тоже не меняется, просто съезжает ниже.
    """
    fields, guessed = envelope(text, scope=scope, role=role, today=today)
    front, rest = split_front(text)
    if front is None:
        front, rest = [], "\n---\n\n" + text

    have = top_keys(front)
    added = {k: _fmt(fields[k]) for k in ENVELOPE if k in fields and k not in have}
    kept = [k for k in ENVELOPE if k in have]
    guessed = [g for g in guessed if g in added]
    if not added:
        return text, {}, [], kept

    lines = list(front)
    new = [f"{k}: {added[k]}" for k in added]
    # `migrated` — собственное поле мигратора, и только его он вправе
    # переписать: при повторном прогоне список пополняется, а не задваивается.
    old_migrated = [i for i, ln in enumerate(lines) if ln.startswith("migrated:")]
    names = guessed
    if old_migrated:
        was = lines[old_migrated[0]].partition(":")[2].strip().strip("[]")
        names = [n for n in was.split(",") if n.strip()] + guessed
        lines[old_migrated[0]] = f"migrated: {_fmt([n.strip() for n in names])}"
    elif guessed:
        new.append(f"migrated: {_fmt(guessed)}")
    return "---\n" + "\n".join(lines + new) + rest, added, guessed, kept


def backup_path(path: Path) -> Path:
    """Куда уедет исходный файл. Занятое имя не перезаписываем: первая копия —
    это состояние ДО первой миграции, и она ценнее любой следующей."""
    home = path.parent / BACKUP_DIR
    dest = home / path.name
    n = 2
    while dest.exists():
        dest = home / f"{path.stem}-{n}{path.suffix}"
        n += 1
    return dest


def migrate_file(path: Path, *, scope: str, role: str, today: date,
                 apply: bool) -> Result:
    """Один файл. Без `apply` на диск не идёт ничего — ни копии, ни записи."""
    res = Result(path=path, role=role)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        res.error = f"не прочитан ({e})"
        return res

    fields, _ = envelope(text, scope=scope, role=role, today=today)
    res.kind = str(fields["kind"])
    res.verified = date.fromisoformat(str(fields["verified"])) if "verified" in fields else None
    res.source = str(fields["source"]) if "source" in fields else None

    new_text, added, guessed, kept = migrate_text(text, scope=scope, role=role,
                                                  today=today)
    res.added, res.guessed, res.kept = added, guessed, kept
    if not apply or not added:
        return res
    try:
        dest = backup_path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        path.write_text(new_text, encoding="utf-8")
        res.written = True
    except OSError as e:
        res.error = f"не записан ({e})"
    return res


# ── обход стора ─────────────────────────────────────────────────────────────

def iter_notes(root: Path) -> list[tuple[Path, str]]:
    """Записи стора парами (файл, роль). Роль — из имени каталога: `agent-work`
    адресует роли `work`, и другого источника у неё нет.

    Указать можно и стор целиком, и один каталог роли: на живом сторе миграция
    пойдёт по одной роли за раз, чтобы отчёт был обозримым.
    """
    root = Path(root)
    # Каталоги ролей внутри — значит указали стор; нет ни одного — значит
    # указали саму роль. Проверять по имени нельзя: стор тоже зовётся
    # `agent-memory-client-b`, и ролью «memory-client-b» он не является.
    dirs = sorted(d for d in root.glob("agent-*") if d.is_dir())
    if not dirs:
        dirs = [root]
    notes = []
    for d in dirs:
        role = d.name.removeprefix("agent-")
        for f in sorted(d.glob("*.md")):
            if f.name not in NOT_ENTRIES:
                notes.append((f, role))
    return notes


def migrate_store(root: Path, *, scope: str, today: date,
                  apply: bool) -> list[Result]:
    """Весь стор. Каталог `_before-migrate/` внутрь обхода не попадает: `glob`
    смотрит только на файлы каталога роли, копии лежат уровнем ниже."""
    return [migrate_file(p, scope=scope, role=role, today=today, apply=apply)
            for p, role in iter_notes(root)]


def _short(path: Path, root: Path) -> str:
    """Путь относительно стора — список без даты читают глазами, и общий
    префикс в нём только мешает."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def report(results: list[Result], *, root: Path, scope: str,
           apply: bool) -> str:
    """Отчёт словами. Поимённо печатается только список без даты: это работа,
    которую придётся доделать человеком, и она должна быть видна целиком."""
    total = len(results)
    dated = [r for r in results if r.verified]
    sourced = [r for r in results if r.source]
    kinds = Counter(r.kind for r in results)
    roles = Counter(r.role for r in results)
    errors = [r for r in results if r.error]

    out = [f"стор: {root}  (scope: {scope})",
           f"режим: {'запись' if apply else 'dry-run, на диск ничего не пишется'}",
           f"просмотрено записей: {total}"]
    if not total:
        return "\n".join(out + ["записей не найдено"])
    out += [f"дата проверки выведена: {len(dated)} из {total}",
            f"источник выведен: {len(sourced)} из {total}",
            "вид записи: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()),
            "роли: " + ", ".join(f"{k} {n}" for k, n in roles.most_common())]
    # Поля, стоявшие в файле до прогона. Строка не говорит, кто их поставил
    # (на повторном прогоне это в том числе догадки прошлого) — она говорит,
    # чего мигратор НЕ касался. Кто поставил, написано в самой записи: в
    # `migrated` или мимо него.
    kept = Counter(k for r in results for k in r.kept)
    if kept:
        out.append("уже стояло в файле, не переписано: "
                   + ", ".join(f"{k} {n}" for k, n in kept.most_common()))
    if apply:
        out.append(f"записано файлов: {sum(1 for r in results if r.written)}"
                   f" (копии исходных — в {BACKUP_DIR}/)")
    else:
        out.append(f"было бы дописано полей в файлах: "
                   f"{sum(1 for r in results if r.added)}")
    undated = [r for r in results if not r.verified]
    if undated:
        out.append(f"без даты ({len(undated)}) — проставить руками:")
        out += [f"  {_short(r.path, root)}" for r in undated]
    if errors:
        out.append(f"ошибки ({len(errors)}):")
        out += [f"  {r.path}: {r.error}" for r in errors]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Перенос записей памяти на конверт (DEV-85). "
                    "По умолчанию ничего не пишет.")
    ap.add_argument("store", type=Path, help="каталог стора или каталог роли")
    ap.add_argument("--apply", action="store_true",
                    help="записать (исходные файлы уедут в _before-migrate/)")
    ap.add_argument("--scope", default=None,
                    help="значение поля scope; по умолчанию — имя стора")
    args = ap.parse_args(argv)

    root: Path = args.store
    if not root.is_dir():
        print(f"нет каталога: {root}", file=sys.stderr)
        return 2
    # Имя стора живых каталогов — `agent-memory-client-b`; областью применения
    # записи является проект, а не каталог, поэтому префикс снимаем.
    scope = args.scope or root.name.removeprefix("agent-memory-")
    results = migrate_store(root, scope=scope, today=date.today(),
                            apply=args.apply)
    print(report(results, root=root, scope=scope, apply=args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
