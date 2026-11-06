"""Свидетельства проверки для страницы подписи (DEV-94).

Человек на странице подписи видел прозу, которую сессия написала о себе, и
ничего больше. Оба ложных `OK` по DEV-46 нашли ручным перезапуском, а не при
подписи: подписывающему было не на что смотреть, кроме рассказа. Здесь
собирается то, что рассказом не является, — из рабочего каталога последней
состоявшейся проверки задачи:

* `gates.json` — журнал ворот: что запускалось, какой командой, с каким
  кодом, чем это считается. Пишет обвязка (`gatelog.py`, `logdiff.py`), не
  модель. Раннер уже вынес по нему суд в `stats.json` (`ворота сессии`,
  вместе с воротами постановки) — этот разбор и берём первым: по нему
  считался исход, и показывать другой значит завести вторую правду. Нет
  разбора (старый раннер, раннер не дописал статистику) — читаем файл сами;
* `expected.md` — эталон, записанный проверкой ДО чтения заявки автора:
  место, команда, ожидаемое, чем покраснеет. Разбирается по пунктам;
* `verdict.txt` — первая строка вердикт, дальше по строке на пункт эталона:
  «<место>: видел <что>, сошлось / не сошлось / не проверял». Строки
  сверяются с пунктами ПО ПОРЯДКУ: пункт без строки виден как «без ответа»,
  строка без пункта — как ответ на то, чего в эталоне не было.

Пустое здесь — тоже ответ, и оно называется своими словами («свидетельств
нет: gates.json не записан»), а не пропускается: молчание подписывающий
прочитал бы как «всё в порядке». На 15.09.2026 это основной случай, а не
краевой: у 66 проверок client-b нет ни одного `gates.json` — прод ещё на
раннере до DEV-87/88.

Слова состояний — те же, что у `gatelog.py`; пакет приёма раннер не
импортирует (он уезжает зависимостью в чужой контейнер), поэтому копия, а
совпадение закреплено тестом.
"""
from __future__ import annotations

import json
import logging
import os
import re

from . import sessions as sess

log = logging.getLogger("intake")

GATES_FILE = "gates.json"
REFERENCE_FILE = "expected.md"
VERDICT_FILE = "verdict.txt"
# Поле разбора ворот в `stats.json` (`runner._write_stats`).
GATES_FIELD = "ворота сессии"

# Шкалы инструментов — копия `gatelog.CODES`.
CODES: dict[str, dict[int, str]] = {
    "logdiff": {0: "прошло", 10: "не прошло", 2: "ошибка обвязки"},
    "verify_order": {0: "прошло", 10: "не прошло", 2: "ошибка обвязки"},
}

# Итоги ворот — копия `gatelog.PASSED/FAILED/ERROR/NO_EVIDENCE`.
PASSED = "прошло"
FAILED = "не прошло"
ERROR = "ошибка обвязки"
NO_EVIDENCE = "нет свидетельств"
STATUSES = (PASSED, FAILED, ERROR)
# Коды, означающие «вот находки, разберись» — копия `gatelog.FINDINGS`
# (DEV-104). Совпадение закреплено тестом там же, где совпадение слов.
FINDINGS: dict[str, tuple[int, ...]] = {
    "logdiff": (10,),
}

# Ответ по пункту эталона — по слову в строке вердикта (contract, шаг 9).
# «не сошлось» ищется раньше «сошлось»: второе — подстрока первого.
MATCHED = "сошлось"
MISMATCHED = "не сошлось"
UNCHECKED = "не проверял"
NO_ANSWER = "без ответа"
NO_ITEM = "пункта в эталоне нет"
ANSWERS = (MISMATCHED, MATCHED, UNCHECKED)

# Первая строка `expected.md` — откуда критерий (`runner.CRITERION_SOURCES`).
CRITERION_SOURCES = ("задача", "запасной", "нет")
_CRITERION_LINE = re.compile(
    r"^[\s#>*_-]*Источник критерия\s*[:—-]?\s*[*_`]*\s*[:—-]?\s*[*_`]*\s*"
    r"(" + "|".join(CRITERION_SOURCES) + r")\b",
    re.IGNORECASE | re.MULTILINE)

# Четыре поля пункта эталона — как их называет контракт, и как их пишут
# сессии: `- **место** — …`, `Место: …`, `**Ожидаемое значение:** 200`.
FIELDS = (
    ("место", ("место",)),
    ("команда", ("команда",)),
    ("ожидаемое", ("ожидаемое значение", "ожидаемое", "ожидание")),
    ("покраснеет", ("чем покраснеет", "покраснеет")),
)
_FIELD_LINE = re.compile(
    r"^[\s>*_`#\d.)-]*(?P<name>[А-Яа-яЁё ]+?)[*_`]*\s*[:—-]\s*(?P<rest>.*)$")


# ── какая проверка ───────────────────────────────────────────────────────

def last_checks(work_root: str, idents) -> dict[str, dict]:
    """Последняя СОСТОЯВШАЯСЯ проверка каждой задачи, одним обходом каталога.

    Правило то же, что у `runs.last_verify` (совпадение закреплено тестом):
    род `verify`, не идёт, не отсев ворот (`CANCELLED`). Один обход на все
    задачи, а не вызов `last_verify` на каждую: он читает хвост транскрипта
    каждой сессии, и тридцать карточек страницы дали бы тридцать обходов.
    """
    wanted = {str(i) for i in idents if i}
    out: dict[str, dict] = {}
    if not work_root or not wanted:
        return out
    # Порядок — тот же, что у `runs.last_verify` (DEV-105): по времени старта,
    # а при равенстве по имени каталога. `sessions.walk` сортирует по ctime, и
    # у двух каталогов одной секунды порядок решала бы файловая система — тогда
    # ворота блокировали бы по одной проверке, а карточка подписи показывала
    # свидетельства другой.
    for s in sorted(sess.walk(work_root), reverse=True,
                    key=lambda x: (x.get("started") or 0.0, x.get("name") or "")):
        task = str(s.get("task") or "")
        if task not in wanted or task in out:
            continue
        if str(s.get("род") or "") != "verify":
            continue
        if s.get("идёт") or str(s.get("исход") or "") == "CANCELLED":
            continue
        out[task] = s
    return out


# ── ворота ───────────────────────────────────────────────────────────────

def _gate(raw) -> dict:
    """Запись журнала → запись карточки. Незнакомый итог — ошибка обвязки,
    не «прошло» (та же развилка, что `gatelog.Gate.from_dict`)."""
    if not isinstance(raw, dict):
        return {"ворота": "?", "итог": ERROR, "команда": "", "код": None,
                "заметка": f"запись не разобрана: {raw!r}"[:200], "источник": "сессия"}
    status = str(raw.get("итог") or "")
    rc = raw.get("код")
    rc = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
    name = str(raw.get("ворота") or "?")
    expected = raw.get("ожидание")
    out = {"ворота": name, "итог": status,
           "команда": str(raw.get("команда") or ""),
           "код": rc,
           "заметка": str(raw.get("заметка") or ""),
           "источник": str(raw.get("источник") or "сессия"),
           # Времена нужны не карточке, а суду над отменой: по ним видно,
           # объявили её до запуска замены или уже зная её результат.
           "когда": _moment(raw.get("когда")),
           "начата": _moment(raw.get("начата")),
           "отменена": _moment(raw.get("отменена")),
           # Ожидаемый код, находка и разбор — DEV-104. Находку узнаём по
           # шкале инструмента, а не по полю: в журналах до DEV-104 поля нет.
           "ожидание": (expected if isinstance(expected, int)
                        and not isinstance(expected, bool) else None),
           "находка": bool(raw.get("находка")) or _is_finding(name, rc),
           "разбирает": str(raw.get("разбирает") or "")}
    if status not in STATUSES:
        out["итог"], out["заметка"] = ERROR, f"итог не разобран: {status!r}"
    return out


def _is_finding(name: str, rc: int | None) -> bool:
    """Код инструмента означает находку на разбор, а не провал проверки."""
    return rc is not None and rc in FINDINGS.get(name, ())


def _status_for(name: str, rc: int | None, expected: int | None = None) -> str:
    """Исход по коду — копия `gatelog.status_for` (DEV-104). Нужна суду:
    у ворот с объявленным ожиданием записанному итогу верить нельзя, он и
    есть предмет спора."""
    if rc is None:
        return ERROR
    known = CODES.get(name)
    if known is not None and known.get(rc, ERROR) == ERROR:
        return ERROR
    if expected is not None:
        return PASSED if rc == expected else FAILED
    if known is not None:
        return known[rc]
    return PASSED if rc == 0 else FAILED


def _moment(raw) -> float:
    """Отметка времени из записи; не число — считаем, что её нет."""
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _superseded(gate: dict, gates: list[dict]) -> bool:
    """Законно ли отменена попытка — копия `gatelog._superseded` (DEV-107).

    Отмена засчитывается, только когда под тем же именем есть попытка,
    запущенная не раньше отметки об отмене: отметить задним числом, увидев
    результат замены, — это выбор удобного прогона, а не замена попытки.
    """
    at = gate.get("отменена") or 0.0
    if at <= 0:
        return False
    return any(other is not gate and other["ворота"] == gate["ворота"]
               and (other.get("начата") or other.get("когда") or 0.0) >= at
               for other in gates)


def read_gates(workdir: str) -> list[dict] | None:
    """Журнал ворот из файла. `None` — файла нет; битый файл — ОДНА запись
    «ошибка обвязки», а не пустой список: битое и пустое — разные факты."""
    path = os.path.join(workdir, GATES_FILE)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        return [_broken(f"журнал ворот не прочитан: {e}")]
    try:
        raw = json.loads(text) if text.strip() else []
    except ValueError as e:
        return [_broken(f"журнал ворот не разобран: {e}")]
    if not isinstance(raw, list):
        return [_broken(f"журнал ворот не разобран: ожидался список, "
                        f"а там {type(raw).__name__}")]
    return [_gate(item) for item in raw]


def _broken(note: str) -> dict:
    return {"ворота": GATES_FILE, "итог": ERROR, "команда": "", "код": None,
            "заметка": note, "источник": "сессия"}


def _declared_ahead(g: dict, gates: list[dict]) -> bool:
    """Объявлено ли ожидание заранее — копия `gatelog._declared_ahead`
    (DEV-104): запись сделала обёртка своим запуском, и прежние попытки тех
    же ворот объявляли то же самое."""
    if g.get("ожидание") is None or (g.get("начата") or 0.0) <= 0:
        return False
    if g.get("когда") and g["начата"] > g["когда"]:
        return False
    return all(o.get("ожидание") == g["ожидание"] for o in gates
               if o is not g and o["ворота"] == g["ворота"]
               and o.get("код") is not None
               and (o.get("начата") or o.get("когда") or 0.0) < g["начата"])


def _verdict_of(g: dict, gates: list[dict]) -> str:
    """Итог записи, каким он идёт в суд — копия `gatelog.verdict_of`."""
    if g.get("ожидание") is None:
        return g["итог"]
    if not _declared_ahead(g, gates):
        return _status_for(g["ворота"], g.get("код"))
    return _status_for(g["ворота"], g.get("код"), g["ожидание"])


def _resolved(g: dict, gates: list[dict]) -> bool:
    """Разобрана ли находка — копия `gatelog._resolved` (DEV-104)."""
    if not g.get("находка"):
        return False
    return any(o is not g and o.get("разбирает") == g["ворота"]
               and o["итог"] == PASSED and (o.get("заметка") or o.get("команда"))
               and (o.get("начата") or o.get("когда") or 0.0) >= (g.get("когда") or 0.0)
               for o in gates)


def judge(gates: list[dict]) -> tuple[str, str]:
    """Суд над журналом — копия `gatelog.judge`, тот же порядок недоверия:
    упавшие важнее прошедших, сломанная обвязка не «прошло», пусто — нет
    свидетельств. Судятся действующие записи: отменённая перезапуском
    попытка на карточке остаётся, но исход не понижает (DEV-107). Ворота с
    объявленным ожиданием прошли, когда код совпал с объявленным, а не когда
    он ноль; разобранная находка исход не понижает (DEV-104)."""
    live = [g for g in gates if not _superseded(g, gates)]
    dropped = len(gates) - len(live)
    failed: list[dict] = []
    broken: list[dict] = []
    resolved: list[dict] = []
    controls: list[dict] = []
    for g in live:
        state = _verdict_of(g, gates)
        if state == ERROR:
            broken.append(g)
        elif state == FAILED:
            (resolved if _resolved(g, live) else failed).append(g)
        if _declared_ahead(g, gates):
            controls.append(g)
    tail = "".join((f"; отменённых попыток: {dropped}" if dropped else "",
                    f"; разобранных находок: {len(resolved)}" if resolved else "",
                    f"; контрольных ворот: {len(controls)}" if controls else ""))
    if failed:
        return FAILED, ("упали ворота: "
                        + ", ".join(_label(g, gates) for g in failed) + tail)
    if broken:
        return ERROR, ("обвязка не отработала: "
                       + ", ".join(_label(g, gates) for g in broken) + tail)
    if not live:
        return NO_EVIDENCE, ("в журнале ворот ни одной действующей записи" + tail
                             if dropped else "в журнале ворот ни одной записи")
    return PASSED, (f"прошли все ворота ({len(live)}): "
                    + ", ".join(g["ворота"] for g in live) + tail)


def _label(g: dict, gates: list[dict] | None = None) -> str:
    code = f" (код {g['код']})" if g.get("код") is not None else ""
    if g.get("ожидание") is not None and g.get("код") is not None:
        code = (f" (код {g['код']}, ждали {g['ожидание']})"
                if _declared_ahead(g, gates or [g])
                else f" (код {g['код']}, ожидание {g['ожидание']} объявлено "
                     f"задним числом — не в счёт)")
    note = f" — {g['заметка']}" if g.get("заметка") else ""
    if g.get("находка") and _verdict_of(g, gates or [g]) == FAILED:
        note += " — находка не разобрана"
    return f"{g['ворота']}{code}{note}"


def gates_of(workdir: str, stats: dict) -> dict | None:
    """Ворота проверки: разбор раннера из `stats.json`, иначе сам файл.

    `None` — свидетельств нет вовсе: ни разбора, ни файла. Это и есть
    строка «свидетельств нет» на карточке. Разбор раннера с пустыми записями
    — тоже `None`: «нет свидетельств» там уже сказано его словами, а
    показывать пустую таблицу с подписью — то же молчание.
    """
    got = stats.get(GATES_FIELD)
    if isinstance(got, dict) and isinstance(got.get("записи"), list):
        records = _marked([_gate(r) for r in got["записи"]])
        if not records:
            return None
        state, why = judge(records)
        return {"итог": str(got.get("итог") or state),
                "почему": str(got.get("почему") or why), "записи": records}
    records = read_gates(workdir)
    if records is None:
        return None
    if not records:
        return None
    records = _marked(records)
    state, why = judge(records)
    return {"итог": state, "почему": why, "записи": records}


def _marked(records: list[dict]) -> list[dict]:
    """Проставить, как суд прочитал каждую строку. Строки остаются на
    карточке все (перезапуск и находки не прячут), но читаться как
    действующие они не должны: иначе человек увидит красную строку там, где
    исход зелёный.

    * `заменена` — попытка законно отменена перезапуском (DEV-107);
    * `контроль` — ворота объявили ожидаемый код, и суд его зачёл: красный
      код здесь означает, что проба сработала как задумано (DEV-104). Контроль
      с НЕзачтённым ожиданием контролем не помечается — иначе карточка
      успокаивала бы там, где исход понижен;
    * `разобрана` — находка инструмента, снятая разбором.
    """
    live = [r for r in records if not _superseded(r, records)]
    for r in records:
        r["заменена"] = _superseded(r, records)
        r["контроль"] = _declared_ahead(r, records)
        r["разобрана"] = any(r is x for x in live) and _resolved(r, live)
    return records


# ── эталон и построчный ответ ────────────────────────────────────────────

def criterion_source(text: str) -> str:
    """`задача` / `запасной` / `нет` из первой строки эталона; пусто — строки
    по форме нет."""
    m = _CRITERION_LINE.search(text or "")
    return m.group(1).lower() if m else ""


def _field_of(line: str) -> tuple[str, str]:
    """Строка эталона → (поле, остаток). Не поле — пусто."""
    m = _FIELD_LINE.match(line)
    if not m:
        return "", ""
    name = m.group("name").strip().casefold()
    for key, names in FIELDS:
        if name in names:
            # Разметка снимается: в ячейке таблицы обратные кавычки и
            # звёздочки — шум, а не смысл. Снимать «только по краям» нельзя:
            # `GET /health` на стенде — и одна кавычка остаётся висеть.
            rest = m.group("rest").strip().strip("*_").replace("`", "").strip()
            return key, rest
    return "", ""


def _table_items(text: str) -> list[dict]:
    """Эталон таблицей: строка с четырьмя ячейками — пункт. Заголовок и
    разделитель `|---|` не считаются."""
    items = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4 or all(set(c) <= set("-: ") for c in cells):
            continue
        head = " ".join(cells).casefold()
        if "место" in head and "команда" in head:
            continue                                   # шапка таблицы
        items.append({"место": cells[0], "команда": cells[1],
                      "ожидаемое": cells[2], "покраснеет": cells[3]})
    return items


def expected_items(text: str) -> list[dict]:
    """Пункты эталона: с «места» начинается новый, остальные поля — его.
    Строка без метки продолжает последнее поле. Ничего не разобралось —
    пробуем таблицу; и её нет — пусто, карточка покажет файл как есть."""
    items: list[dict] = []
    field = ""
    for line in (text or "").splitlines():
        key, rest = _field_of(line)
        if key == "место" or (key and not items):
            items.append({"место": "", "команда": "", "ожидаемое": "", "покраснеет": ""})
        if key:
            field = key
            items[-1][field] = (items[-1][field] + " " + rest).strip()
            continue
        if items and field and line.strip():
            items[-1][field] = (items[-1][field] + " " + line.strip()).strip()
    items = [i for i in items if any(i.values())]
    return items or _table_items(text or "")


def answer_of(line: str) -> str:
    """Ответ по строке вердикта: слово из контракта или пусто."""
    low = line.casefold()
    for word in ANSWERS:
        if word in low:
            return word
    if "сошлись" in low:
        return MISMATCHED if "не сошлись" in low else MATCHED
    return ""


def verdict_rows(text: str) -> tuple[str, list[dict]]:
    """`verdict.txt` → (первая строка, строки-ответы). Ответом считается
    строка со словом контракта; остальное — проза, и в сверку не идёт."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "", []
    head, rest = lines[0], lines[1:]
    rows = []
    for ln in rest:
        got = answer_of(ln)
        if got:
            rows.append({"текст": ln.lstrip("-*• ").strip(), "ответ": got})
    return head[:300], rows


def pair(items: list[dict], rows: list[dict]) -> list[dict]:
    """Пункты эталона против строк вердикта, по порядку. Пункт без строки —
    `без ответа`; строка без пункта — `пункта в эталоне нет`. Оба случая
    видны строкой, а не пропадают."""
    out = []
    for i in range(max(len(items), len(rows))):
        item = items[i] if i < len(items) else None
        row = rows[i] if i < len(rows) else None
        out.append({
            "место": (item or {}).get("место", ""),
            "ожидаемое": (item or {}).get("ожидаемое", ""),
            "команда": (item or {}).get("команда", ""),
            "ответ": (row["ответ"] if row else NO_ANSWER) if item else NO_ITEM,
            "текст": row["текст"] if row else "",
        })
    return out


def _read(workdir: str, name: str) -> str | None:
    try:
        with open(os.path.join(workdir, name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# ── всё вместе ───────────────────────────────────────────────────────────

def of_session(work_root: str, session: dict) -> dict:
    """Свидетельства одной проверки — всё, что карточке нужно показать."""
    workdir = os.path.join(work_root, session["name"])
    stats = {}
    try:
        with open(os.path.join(workdir, "stats.json"), encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        pass
    if not isinstance(stats, dict):
        stats = {}
    reference = _read(workdir, REFERENCE_FILE)
    verdict = _read(workdir, VERDICT_FILE)
    head, rows = verdict_rows(verdict or "")
    items = expected_items(reference or "")
    return {
        "сессия": session["name"],
        "когда": float(session.get("touched") or session.get("started") or 0.0),
        "исход": str(session.get("исход") or stats.get("исход") or ""),
        "режим": str(session.get("режим") or ""),
        "вердикт": head,
        "ворота": gates_of(workdir, stats),
        # `None` — файла нет; пункты пустые при живом файле — форма не
        # разобрана, тогда карточка показывает текст как есть.
        "эталон": None if reference is None else {
            "источник": criterion_source(reference),
            "пункты": items,
            "текст": reference if not items else "",
        },
        "критерий по воротам": str(stats.get("критерий по воротам") or ""),
        "сверка": pair(items, rows) if reference is not None else [],
        "ответов": len(rows),
    }


def collect(work_root: str, idents) -> dict[str, dict | None]:
    """Свидетельства по задачам: `{идентификатор: свидетельства | None}`.
    `None` — состоявшейся проверки по задаче не было."""
    idents = [str(i) for i in idents if i]
    found = last_checks(work_root, idents)
    out: dict[str, dict | None] = {}
    for ident in idents:
        s = found.get(ident)
        if s is None:
            out[ident] = None
            continue
        try:
            out[ident] = of_session(work_root, s)
        except Exception as e:                      # каталог снят под ногами
            log.warning("свидетельства %s не прочитаны: %s", s.get("name"), e)
            out[ident] = None
    return out
