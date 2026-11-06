#!/usr/bin/env python3
"""Журнал ворот проверки: что сессия запускала и с каким кодом (DEV-88).

До этого у исхода проверки не было ни одного детерминированного входа:
`logdiff.py` выходит с кодом 10 на находку, а раннер этот код не читал
нигде. Хуже того — код терялся ещё в сессии: из 20 проверок client-b, звавших
`logdiff`, ни одна не показала его кода, потому что вызов шёл через
`| tail -25; echo rc=$?` — `$?` там принадлежит `tail`. Сессия видела
«НОВОЕ … видов 1» и печатала `rc=0`.

Поэтому свидетельства пишутся не сессией и не разбором транскрипта, а самой
обвязкой: `logdiff.py` дописывает сюда свой код сам, любую другую проверку
сессия оборачивает `python3 gatelog.py <имя> -- <команда>`. Обёртка
пропускает вывод как есть и возвращает код команды — труба и `tail` ей не
мешают, потому что код записан ДО них.

Файл — `gates.json` в рабочем каталоге сессии; путь приезжает окружением
(`AGENT_GATES_FILE`), раннер ставит его при запуске. Читает файл
`runner.outcome_of`: упавшие ворота не переписываются в «проверено» никаким
вердиктом, а `OK` без единой записи — не проверка, а рассказ.

Три исхода у каждых ворот, не два:

* **прошло** — команда выполнилась и ответила «чисто»;
* **не прошло** — команда выполнилась и нашла расхождение;
* **ошибка обвязки** — команду не удалось выполнить: не нашлась, упала на
  разборе аргументов, журнал не прочитался, сам файл ворот не разобран.

Третье никогда не считается «прошло» и никогда не сливается с «нет
свидетельств»: молчаливый откат обвязки — задокументированный источник
ложных пропусков, и при двух состояниях он неотличим от «сессия
поленилась».

Перезапуск тех же ворот ПОД ТЕМ ЖЕ ИМЕНЕМ заменяет прежнюю попытку: она
остаётся в журнале с отметкой «отменена» и исход больше не понижает (DEV-107).
Отметку ставит обёртка ДО запуска замены — то есть до того, как известен её
результат. В этом вся защита: отменить, посмотрев на результат замены, нельзя,
иначе отмена стала бы способом стереть настоящий провал — прогнал дважды,
оставил зелёное. Не запустилась замена (сессию сняли, команда не нашлась) —
отмена не засчитывается, и прежняя красная запись судится как была.

Имя — договор: одни ворота = одно имя. Другая проверка под тем же именем
затрёт прежнюю, а переименование (`pytest` → `pytest-ispravlen`) заменой НЕ
считается — в журнале останутся обе.

Красные ПО ЗАМЫСЛУ не понижают исход (DEV-104). Контрольные ворота — те, что
обязаны упасть: проба гонится против `main`, где правки ещё нет, и её код 3
доказывает, что проба ловит отсутствие фичи. Сессия объявляет это в самой
команде постановки — `--expect 3`, — и запись считается прошедшей, когда
фактический код совпал с объявленным. Умолчание не меняется: ожидание не
объявлено — ждём ноль.

Объявить можно только ЗАРАНЕЕ, и это проверяется по журналу, а не на слово
(`_declared_ahead`): ожидание засчитывается лишь у записи, сделанной обёрткой
при запуске, и только если прежние попытки тех же ворот объявляли то же
самое. Иначе достаточно было бы увидеть красное и перезапустить ворота с
`--expect <тот самый код>` — способ оправдать любой провал. Ошибка обвязки
остаётся ошибкой при любом ожидании: не отработавший запуск ничего не
доказывает.

Второй случай той же нехватки — инструмент со своей шкалой (DEV-41).
`logdiff` кодом 10 говорит «вот находки, посмотри», а не «проверка
провалилась»; шкалу объявляет он сам (`FINDINGS`), а не каждая сессия своим
`--expect`. Находка остаётся красной, пока её не РАЗОБРАЛИ: разбор — такие же
ворота, названные причиной (`--resolves <имя> --why "…"`), и ложится он после
результата, потому что судить находку, не посмотрев на неё, нельзя. Разобрать
можно только находку: провал теста разбором не гасится.

    python3 gatelog.py pytest -- python3 -m pytest -q
    python3 gatelog.py health -- curl -fsS http://127.0.0.1:3002/health
    python3 gatelog.py --expect 3 proba-main -- python3 proba.py
    python3 gatelog.py --resolves logdiff --why "фон: те же строки есть и в
        окне до выката" razbor-logdiff

Флаги обёртки ставятся ПЕРЕД именем ворот: всё, что идёт после имени, — уже
команда (иначе `--` перестал бы разделять два набора аргументов).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

GATES_FILE = "gates.json"
# Куда писать — окружением: инструмент зовётся из любого каталога, а файл
# должен лечь в рабочий каталог сессии.
ENV_FILE = "AGENT_GATES_FILE"

PASSED = "прошло"
FAILED = "не прошло"
ERROR = "ошибка обвязки"
STATUSES = (PASSED, FAILED, ERROR)
# Ответ суда над журналом, которого у отдельных ворот не бывает: записей нет.
NO_EVIDENCE = "нет свидетельств"

# Код возврата — ещё не исход: у инструментов, где ненулевой код означает
# «есть находка», провал и поломка обвязки различаются кодом. Незнакомый код
# известного инструмента — ошибка обвязки, а не провал: инструмент так не
# отвечает, значит выполнилось не то.
CODES: dict[str, dict[int, str]] = {
    "logdiff": {0: PASSED, 10: FAILED, 2: ERROR},
    "verify_order": {0: PASSED, 10: FAILED, 2: ERROR},
}
# Коды, у которых «не прошло» означает «вот находки, разберись», а не
# «проверка провалилась». Объявляет их сам инструмент — иначе каждая сессия
# должна помнить, что `logdiff` отвечает не по шкале «ноль или провал», и
# ровно этого не вспомнила проверка DEV-41: четырнадцать зелёных ворот из
# шестнадцати, вердикт `OK`, а задача не записана в «проверено» из-за двух
# кодов 10, которые означали «вот тебе находки».
#
# Красной находка остаётся до разбора: `logdiff` — единственный
# детерминированный вход про журнал службы после выката, и зелёный его код
# был бы разрешением не смотреть на найденное вовсе (ровно тот отказ, против
# которого заводились ворота). `verify_order` сюда НЕ входит: его 10 — не
# находка на разбор, а нарушение порядка проверки, то есть провал.
FINDINGS: dict[str, tuple[int, ...]] = {
    "logdiff": (10,),
}
# Код, с которым обёртка выходит, когда команду выполнить не удалось. Тот же,
# что у `logdiff` и `verify_order` для «прочитать не вышло».
HARNESS_RC = 2


@dataclass(frozen=True)
class Gate:
    """Одни ворота: что запускали, что получили, чем это считается."""
    name: str
    status: str
    command: str = ""
    rc: int | None = None
    note: str = ""
    # `сессия` — записала обвязка сессии; `постановка` — ворота перед
    # постановкой (deploy.preflight), их дописывает раннер из задачи.
    source: str = "сессия"
    at: float = 0.0
    # `started` — когда попытку ЗАПУСТИЛИ (у `at` — когда она кончилась):
    # по этому моменту судится законность отмены прежней попытки.
    started: float = 0.0
    # Когда попытку объявили отменённой; 0.0 — действующая. Отметка сама по
    # себе ещё не отмена: её законность проверяет `_superseded`.
    cancelled: float = 0.0
    # Какого кода ждали. `None` — не объявляли, то есть ждём ноль (умолчание
    # не менялось). Объявляет сессия в команде постановки; законность —
    # `_declared_ahead`.
    expected: int | None = None
    # Код инструмента означает «вот находки», а не «провал» (`FINDINGS`).
    # Красная до разбора, но разобрать её можно — в отличие от провала.
    finding: bool = False
    # Имя ворот, находку которых эта запись разбирает. Пусто — не разбор.
    resolves: str = ""

    def as_dict(self) -> dict:
        return {"ворота": self.name, "итог": self.status, "команда": self.command,
                "код": self.rc, "заметка": self.note, "источник": self.source,
                "когда": self.at, "начата": self.started,
                "отменена": self.cancelled, "ожидание": self.expected,
                "находка": self.finding, "разбирает": self.resolves}

    @classmethod
    def from_dict(cls, raw: dict) -> "Gate":
        status = str(raw.get("итог") or "")
        if status not in STATUSES:
            # Незнакомый итог — не «прошло» по умолчанию: запись есть, а что
            # она значит, неизвестно, и это ошибка обвязки, а не свидетельство.
            return cls(name=str(raw.get("ворота") or "?"), status=ERROR,
                       command=str(raw.get("команда") or ""),
                       note=f"итог не разобран: {status!r}",
                       source=str(raw.get("источник") or "сессия"),
                       started=_moment(raw.get("начата")),
                       cancelled=_moment(raw.get("отменена")))
        rc = raw.get("код")
        rc = int(rc) if isinstance(rc, int) and not isinstance(rc, bool) else None
        name = str(raw.get("ворота") or "?")
        return cls(name=name, status=status,
                   command=str(raw.get("команда") or ""),
                   rc=rc,
                   note=str(raw.get("заметка") or ""),
                   source=str(raw.get("источник") or "сессия"),
                   at=_moment(raw.get("когда")),
                   started=_moment(raw.get("начата")),
                   cancelled=_moment(raw.get("отменена")),
                   expected=_code(raw.get("ожидание")),
                   # Находку узнаём по шкале инструмента, а не по полю: в
                   # журналах, записанных до DEV-104, поля нет, а код 10 у
                   # `logdiff` значил ровно то же самое.
                   finding=bool(raw.get("находка")) or is_finding(name, rc),
                   resolves=str(raw.get("разбирает") or ""))


def _code(raw) -> int | None:
    """Код из записи: целое или ничего. `True`/`False` — не код (в JSON они
    пролезают как числа), строка «3» — тоже: поле пишет обвязка числом."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def is_finding(name: str, rc: int | None) -> bool:
    """Код инструмента означает находку на разбор, а не провал проверки."""
    return rc is not None and rc in FINDINGS.get(name, ())


def _moment(raw) -> float:
    """Отметка времени из записи. Не число — считаем, что её нет: журнал
    пишет обвязка, но правит его кто угодно, а от битой отметки не должен
    падать разбор целого журнала."""
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def status_for(name: str, rc: int | None, expected: int | None = None) -> str:
    """Исход по коду возврата. `None` — команда не завершилась (не нашлась,
    снята сигналом, не запустилась): это обвязка, не проверка.

    `expected` — объявленное сессией ожидание. Совпал код с объявленным —
    прошло, не совпал — не прошло; ноль тут ничем не лучше тройки. Ошибка
    обвязки ожиданием не гасится: код, которым известный инструмент говорит
    «выполнилось не то», остаётся ошибкой, и `None` тоже, — иначе достаточно
    было бы объявить ожидание и не запускать команду вовсе.
    """
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


def gate_path(explicit: str | os.PathLike | None = None) -> Path | None:
    """Файл ворот: явный путь, иначе из окружения, иначе никакого."""
    raw = str(explicit) if explicit else os.environ.get(ENV_FILE, "")
    return Path(raw) if raw else None


def read(path: Path) -> list[Gate]:
    """Записи журнала. Нет файла — пусто (свидетельств нет). Файл есть, а
    разобрать нельзя — ОДНА запись «ошибка обвязки»: журнал, который не
    читается, не может считаться пустым, иначе битый файл сходил бы за
    «сессия ничего не запускала»."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return []
    except OSError as e:
        return [Gate(name=path.name, status=ERROR, note=f"журнал ворот не прочитан: {e}")]
    try:
        raw = json.loads(text) if text.strip() else []
    except ValueError as e:
        return [Gate(name=path.name, status=ERROR, note=f"журнал ворот не разобран: {e}")]
    if not isinstance(raw, list):
        return [Gate(name=path.name, status=ERROR,
                     note=f"журнал ворот не разобран: ожидался список, а там {type(raw).__name__}")]
    return [Gate.from_dict(item) if isinstance(item, dict)
            else Gate(name="?", status=ERROR, note=f"запись не разобрана: {item!r}"[:200])
            for item in raw]


def _load(path: Path) -> list:
    """Сырой журнал для правки. Битый не перезаписываем молча: чтение отдаст
    его ошибкой обвязки, и эта запись обязана остаться видимой. Отказ —
    наружу, звонящий скажет о нём в stderr."""
    try:
        raw = json.loads(path.read_text(errors="replace") or "[]")
    except FileNotFoundError:
        return []
    except ValueError:
        raise OSError(f"журнал ворот {path} не разобран, дописать нельзя")
    if not isinstance(raw, list):
        raise OSError(f"журнал ворот {path}: ожидался список")
    return raw


def _save(path: Path, raw: list) -> None:
    """Заменить журнал целиком: файл маленький, а запись через временный файл
    и `os.replace` не оставляет полузаписанного JSON, если сессию снимут
    посреди записи."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def record(path: Path, gate: Gate) -> None:
    """Дописать ворота в журнал."""
    raw = _load(path)
    raw.append(gate.as_dict())
    _save(path, raw)


def supersede(path: Path, name: str, at: float) -> int:
    """Объявить прежние попытки ворот `name` отменёнными — ДО запуска замены.

    Отметка ставится заранее именно потому, что тогда она ничего не знает о
    результате замены: отменить, увидев результат, — это не отмена попытки, а
    выбор удобного из двух прогонов. Возвращает, сколько записей помечено;
    ноль — этих ворот в журнале ещё не было.
    """
    raw = _load(path)
    marked = 0
    for item in raw:
        if not isinstance(item, dict) or str(item.get("ворота") or "") != name:
            continue
        if _moment(item.get("отменена")) > 0:
            continue                      # уже отменена прежним перезапуском
        item["отменена"] = at
        marked += 1
    if marked:
        _save(path, raw)
    return marked


def _launched(gate: Gate) -> float:
    """Когда попытка началась. У записей без `начата` (журнал прежних
    сессий) — по времени результата: раньше него она точно не кончилась."""
    return gate.started or gate.at


def _superseded(gate: Gate, gates: list[Gate]) -> bool:
    """Законно ли отменена эта попытка.

    Два условия, и оба обязательны: отметка об отмене есть И под тем же
    именем нашлась попытка, ЗАПУЩЕННАЯ не раньше этой отметки. Второе и
    режет отмену задним числом: замена, запущенная до отметки, к моменту
    отметки уже известна результатом, и тогда отмена выбирает из двух
    прогонов удобный, а не заменяет попытку. Замены нет вовсе (сессию сняли
    между отменой и запуском) — отмена тоже не считается.
    """
    if gate.cancelled <= 0:
        return False
    return any(other is not gate and other.name == gate.name
               and _launched(other) >= gate.cancelled for other in gates)


def _declared_ahead(gate: Gate, gates: list[Gate]) -> bool:
    """Объявлено ли ожидание ЗАРАНЕЕ — то есть до того, как код стал известен.

    Три условия, и все обязательны:

    * ожидание вообще объявлено;
    * запись сделала обёртка своим запуском — у неё есть отметка `начата`, и
      стоит она не позже результата. Приписать `ожидание` в готовую запись
      (или в ту, что инструмент подал о себе сам через `note_result`) нельзя:
      отметки запуска там нет, и суд посчитает код как без ожидания;
    * прежние попытки ТЕХ ЖЕ ворот, ПОКАЗАВШИЕ КОД, объявляли то же самое.
      Это и режет обход через перезапуск: увидела красное, прогнала ещё раз с
      `--expect <тот самый код>` — формально объявление опять раньше своего
      запуска, а по существу код уже известен. Контрольные ворота объявляют
      ожидание с первого прогона, поэтому честный перезапуск (ошиблась в
      команде, прогнала снова) правило не задевает.

      Попытка, кончившаяся ошибкой обвязки, кода не показала (`rc` там
      `None`) — и права объявить ожидание не отнимает. Иначе опечатка в
      команде запирала бы контроль насовсем: сессия ничего не узнала, а
      объявить уже не может.
    """
    if gate.expected is None or gate.started <= 0:
        return False
    if gate.at and gate.started > gate.at:
        return False
    return all(other.expected == gate.expected for other in gates
               if other is not gate and other.name == gate.name
               and other.rc is not None and _launched(other) < gate.started)


def verdict_of(gate: Gate, gates: list[Gate]) -> str:
    """Итог записи, каким он идёт в суд.

    Ожидания нет — берём итог как записан (поведение до DEV-104 слово в
    слово). Ожидание есть — считаем заново по коду: записанному `итог` тут
    верить нельзя, потому что он и есть предмет спора, а объявление,
    сделанное задним числом, судится так, будто его не было.
    """
    if gate.expected is None:
        return gate.status
    if not _declared_ahead(gate, gates):
        return status_for(gate.name, gate.rc)
    return status_for(gate.name, gate.rc, gate.expected)


def _resolved(gate: Gate, gates: list[Gate]) -> bool:
    """Разобрана ли находка: есть ли прошедшая запись, которая её называет.

    Разбор ложится ПОСЛЕ результата, в отличие от отмены попытки, — и иначе
    быть не может: судить находку, не посмотрев на неё, нельзя. Поэтому
    защита тут другая: разобрать можно только находку (`FINDINGS`), провал
    теста разбором не гасится; у разбора должно быть что предъявить (команда
    или причина словами); и сам разбор обязан пройти — упавший разбор ничего
    не разобрал.
    """
    if not gate.finding:
        return False
    return any(other is not gate and other.resolves == gate.name
               and other.status == PASSED and (other.note or other.command)
               and _launched(other) >= gate.at for other in gates)


def effective(gates: list[Gate]) -> list[Gate]:
    """Действующие записи: всё, кроме законно отменённых попыток. Судят
    исход только они; сами отменённые из журнала никуда не деваются."""
    return [g for g in gates if not _superseded(g, gates)]


def note_result(name: str, argv: list[str], rc: int | None, path: Path | None = None,
                note: str = "") -> None:
    """Записать результат уже выполненной проверки — так о себе сообщают
    инструменты, которые сами себя и запускали (`logdiff`). Нет файла в
    окружении — записывать некуда, это не ошибка: инструмент годится и
    вне сессии. Не записалось — говорим в stderr: молчать нельзя, но и
    ронять инструмент из-за журнала — тоже.

    Прежние попытки тут НЕ отменяются, в отличие от обёртки (`run`), и по
    двум причинам. Результат здесь уже известен — отмена вышла бы задним
    числом, ровно тем, от чего защищаемся. И имя тут не выбирает сессия:
    все прогоны `logdiff` зовутся одинаково, а смотрят на разные юниты, так
    что «то же имя» не значит «та же проверка» — второй прогон стёр бы
    находку первого."""
    target = gate_path(path)
    if target is None:
        return
    gate = Gate(name=name, status=status_for(name, rc), command=" ".join(argv),
                rc=rc, note=note, at=time.time(), finding=is_finding(name, rc))
    try:
        record(target, gate)
    except OSError as e:
        print(f"ворота {name!r} не записаны в {target}: {e}", file=sys.stderr)


def run(name: str, argv: list[str], path: Path | None = None,
        expect: int | None = None, resolves: str = "") -> int:
    """Обёртка: выполнить команду, записать ворота, вернуть ЕЁ код.

    Вывод команды идёт в наши же stdout/stderr — сессия видит его как без
    обёртки. Команда не запустилась (нет такой, нет прав) — ошибка обвязки и
    код `HARNESS_RC`; та же запись, если запуск оборвался сигналом: `rc`
    отрицательный — это не ответ инструмента.

    `expect` — какого кода ждём: ворота считаются прошедшими, когда код
    совпал с ним. Объявляется здесь и только здесь, то есть до запуска
    команды; засчитывается по правилам `_declared_ahead`. Возвращаемый код —
    всегда КОД КОМАНДЫ, а не итог ворот: обёртка не должна врать пайплайну,
    в котором стоит.

    `resolves` — имя ворот, находку которых разбирает этот прогон.
    """
    target = gate_path(path)
    started = time.time()
    if target is not None:
        # Перезапуск тех же ворот отменяет прежнюю попытку — и отметка об
        # этом ложится ДО запуска, пока результат замены неизвестен. Не
        # записалось — говорим и идём дальше: прежняя попытка останется
        # действующей, то есть в худшую для этой сессии сторону.
        try:
            dropped = supersede(target, name, started)
        except OSError as e:
            print(f"прежние попытки ворот {name!r} не отменены: {e}", file=sys.stderr)
        else:
            if dropped:
                print(f"ворота {name!r}: прежних попыток отменено {dropped} — "
                      f"судиться будет эта", file=sys.stderr)
    rc: int | None
    note = ""
    try:
        rc = subprocess.call(argv)
    except (OSError, ValueError) as e:
        rc, note = None, f"команда не запустилась: {e}"
    if rc is not None and rc < 0:
        note, rc = f"снята сигналом {-rc}", None
    gate = Gate(name=name, status=status_for(name, rc, expect),
                command=" ".join(argv), rc=rc, note=note, started=started,
                at=time.time(), expected=expect, finding=is_finding(name, rc),
                resolves=resolves)
    if target is None:
        print(f"ворота {name!r}: {gate.status}, но записать некуда — "
              f"{ENV_FILE} не задан", file=sys.stderr)
    else:
        try:
            record(target, gate)
        except OSError as e:
            print(f"ворота {name!r} не записаны в {target}: {e}", file=sys.stderr)
    if gate.status == ERROR and note:
        print(f"ворота {name!r}: {note}", file=sys.stderr)
    if expect is not None and target is not None:
        # Объявление, которое суд не зачтёт, лучше назвать сейчас: узнать об
        # этом из вердикта через час — значит не успеть ничего исправить.
        try:
            live = read(target)
        except OSError:                       # журнал битый, о нём скажет суд
            live = []
        if not any(_declared_ahead(g, live) for g in live
                   if g.name == name and g.started == started):
            print(f"ворота {name!r}: ожидание {expect} НЕ зачтено — эти ворота "
                  "уже показывали код в этом журнале; контроль объявляют с "
                  "первого прогона", file=sys.stderr)
    return HARNESS_RC if rc is None else rc


def resolve(name: str, target: str, why: str, path: Path | None = None) -> int:
    """Записать РАЗБОР находки: посмотрели на находку ворот `target` и
    объясняем, почему она не про это изменение.

    Отдельный вход, потому что разбирать чаще всего нечем запускать: сессия
    прочитала находки и отвечает за вывод словами. Причина обязательна — без
    неё разбор был бы кнопкой «считать зелёным». Прогон, у которого команда
    всё-таки есть, идёт обычным путём: `run(..., resolves=target)`.
    """
    why = why.strip()
    if not target or not why:
        print("разбор находки без имени ворот и причины не принимается",
              file=sys.stderr)
        return HARNESS_RC
    file = gate_path(path)
    gate = Gate(name=name, status=PASSED, note=why, resolves=target,
                started=time.time(), at=time.time())
    if file is None:
        print(f"разбор ворот {target!r} записать некуда — {ENV_FILE} не задан",
              file=sys.stderr)
        return HARNESS_RC
    try:
        record(file, gate)
    except OSError as e:
        print(f"разбор ворот {target!r} не записан в {file}: {e}", file=sys.stderr)
        return HARNESS_RC
    return 0


def judge(gates: list[Gate]) -> tuple[str, str]:
    """Суд над журналом: один итог и причина словами.

    Порядок — порядок недоверия: одни упавшие ворота важнее любых прошедших
    (`не прошло`); ни одних упавших, но обвязка где-то сломалась — `ошибка
    обвязки`, и это не «прошло»; записей нет вовсе — `нет свидетельств`;
    только когда каждая запись прошла — `прошло`.

    Судятся ДЕЙСТВУЮЩИЕ записи: попытка, законно отменённая перезапуском тех
    же ворот, в журнале остаётся, но исход не понижает (`effective`). Сколько
    их было — в причине словами: отмена, о которой не сказано, неотличима от
    спрятанного перезапуска.

    Итог каждой записи берёт `verdict_of`: у ворот с объявленным ожиданием
    прошло — это «код совпал с объявленным», а не «код ноль» (DEV-104).
    Разобранная находка исход не понижает; неразобранная понижает, как
    раньше. И то и другое названо в причине числом: молчаливый зачёт красного
    неотличим от дыры в суде.
    """
    live = effective(gates)
    dropped = len(gates) - len(live)
    failed: list[Gate] = []
    broken: list[Gate] = []
    resolved: list[Gate] = []
    controls: list[Gate] = []
    for gate in live:
        state = verdict_of(gate, gates)
        if state == ERROR:
            broken.append(gate)
        elif state == FAILED:
            (resolved if _resolved(gate, live) else failed).append(gate)
        if _declared_ahead(gate, gates):
            controls.append(gate)
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
                    + ", ".join(g.name for g in live) + tail)


def _label(gate: Gate, gates: list[Gate] | None = None) -> str:
    """Строка о воротах в причине суда. Про объявленное ожидание сказано
    ровно то, что с ним сделал суд: зачтённое — «ждали N», приписанное задним
    числом — так и названо, иначе человек прочитает отказ как придирку."""
    code = f" (код {gate.rc})" if gate.rc is not None else ""
    if gate.expected is not None and gate.rc is not None:
        code = (f" (код {gate.rc}, ждали {gate.expected})"
                if _declared_ahead(gate, gates or [gate])
                else f" (код {gate.rc}, ожидание {gate.expected} объявлено "
                     f"задним числом — не в счёт)")
    note = f" — {gate.note}" if gate.note else ""
    if gate.finding and verdict_of(gate, gates or [gate]) == FAILED:
        note += " — находка не разобрана"
    return f"{gate.name}{code}{note}"


def summary(gates: list[Gate]) -> dict:
    """Итог для `stats.json`: суд и сами записи, машиночитаемо."""
    state, why = judge(gates)
    return {"итог": state, "почему": why, "записи": [g.as_dict() for g in gates]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Выполнить проверку и записать её код в журнал ворот сессии",
        usage="%(prog)s [--file gates.json] [--expect КОД] [--resolves ИМЯ "
              "--why ПРИЧИНА] ИМЯ -- КОМАНДА [АРГУМЕНТЫ...]")
    p.add_argument("name", help="имя ворот: logdiff, pytest, health, md5 …")
    p.add_argument("--file", default=None,
                   help=f"файл журнала; по умолчанию из ${ENV_FILE}")
    p.add_argument("--expect", type=int, default=None, metavar="КОД",
                   help="контрольные ворота: какой код ДОЛЖЕН вернуться "
                        "(например 3 у пробы против main). Объявляется до "
                        "запуска; без него ждём 0, как раньше")
    p.add_argument("--resolves", default="", metavar="ИМЯ",
                   help="эти ворота разбирают находку ворот ИМЯ")
    p.add_argument("--why", default="", metavar="ПРИЧИНА",
                   help="почему находка не про это изменение; с --resolves "
                        "и без команды это и есть разбор")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="команда после `--`")
    args = p.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    path = Path(args.file) if args.file else None
    if not command:
        if args.resolves:
            return resolve(args.name, args.resolves, args.why, path)
        print("нужна команда после `--`", file=sys.stderr)
        return HARNESS_RC
    if command[0].startswith("-"):
        # Флаг, попавший в команду: `gatelog.py имя --expect 3 -- …` разберётся
        # как «запусти программу --expect». Сказать это прямо дешевле, чем
        # оставить в журнале «команда не запустилась: нет файла --expect».
        print(f"после имени ворот идёт команда, а не флаг ({command[0]}): "
              "флаги обёртки ставятся ПЕРЕД именем", file=sys.stderr)
        return HARNESS_RC
    return run(args.name, command, path, expect=args.expect,
               resolves=args.resolves)


if __name__ == "__main__":
    sys.exit(main())
