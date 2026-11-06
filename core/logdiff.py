#!/usr/bin/env python3
"""Что появилось в журнале сервиса ПОСЛЕ выката, чего не было ДО него.

Инструмент сессии-проверяльщика (VERIFY_TEMPLATE, шаг «журнал сервиса»).
Отвечает на единственный вопрос: не начал ли сервис после деплоя писать то,
чего раньше не писал.

Почему это скрипт, а не строчка в промпте «посмотри логи». В логах живого
сервиса всегда есть фон, и глазами «новое» от «так было всегда» не отличить —
шаг мгновенно превращается в генератор ложных тревог, и его перестают читать.
Сравнение двух окон делается механически, одинаково от запуска к запуску, и
на выходе даёт десяток строк вместо мегабайта журнала: сессии дешевле по
контексту, человеку — воспроизводимо.

Что делает:
  1. режет журнал на два окна вокруг момента выката;
  2. сводит каждую строку к «виду» (числа, id, timestamp'ы → плейсхолдеры),
     иначе `request 8123 failed` и `request 8124 failed` считались бы разными;
  3. печатает виды, которых в окне ДО не было вовсе (НОВОЕ) или которые стали
     идти заметно чаще (УЧАСТИЛОСЬ). Совпавшее с фоном не показывает.

Примеры:
  python3 logdiff.py --at 2026-08-31T10:12:33Z --docker infrastructure-events-api-1
  python3 logdiff.py --at 2026-08-31T10:12:33Z --unit agent-runner --all

Коды возврата: 0 — нового нет, 10 — есть находки, 2 — прочитать не вышло.
Ненулевой код здесь не «команда упала», а результат: 10 значит «есть что
посмотреть».
"""
import argparse
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

# Потолок на чтение: у болтливого сервиса окно «до» — это сотни тысяч строк, а
# сессия проверяльщика живёт минуты. Обрезку не прячем: неполное окно «до»
# завышает число «новых» видов, и в отчёте об этом сказано прямым текстом.
MAX_LINES = 200_000
# Сколько видов печатать. Больше двух десятков всё равно никто не разбирает, а
# отброшенное считаем и показываем числом — молча резать нельзя.
TOP = 15
# Во сколько раз должен вырасти темп, чтобы «было и раньше» стало находкой.
SURGE_FACTOR = 5.0
# Ниже этого числа строк всплеск не считаем: на коротком окне одна-две строки
# дают любой множитель.
SURGE_MIN_COUNT = 3

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Ведущий timestamp пишет каждый логгер по-своему; срезаем, чтобы он не попал
# в «вид» строки. Порядок важен: syslog-формат journald проверяем первым.
_LEADING = (
    re.compile(r"^\w{3}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}\s\S+\s\S+?(?:\[\d+\])?:\s*"),
    re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
               r"(?:Z|[+-]\d{2}:?\d{2})?\s*"),
    re.compile(r"^\[\d{2}/\w{3}/\d{4}(?::\d{2}){3}\s[+-]\d{4}\]\s*"),
)

# Всё, что меняется от строки к строке при одном и том же смысле.
_SUBS = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"), "<ts>"),
    (re.compile(r"\b[0-9a-fA-F]{7,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
)

# Признак того, что строка вообще стоит внимания. Без фильтра «новым» окажется
# каждый второй запрос в access-логе. Сервису, который пишет ошибки без единого
# такого слова, есть `--all`.
_SIGNAL = re.compile(
    r"error|critical|fatal|panic|traceback|exception|warn|"
    r"timeout|timed out|refused|unreachable|denied|unauthor|"
    r"fail|retry|restart|not found|no such|cannot|could not|"
    r"ошибк|критич|не удалось|таймаут|отвал",
    re.I,
)


def parse_ts(text: str) -> datetime:
    """Момент выката из того, что пришло с вебхука GitLab, → aware UTC.

    Форматов два: ISO-8601 (`2026-08-31T10:12:33Z`, `...+02:00`) и старый
    гитлабовский `2026-08-31 10:12:33 UTC`. Naive-строку считаем UTC — весь
    парк хостов живёт в UTC, а гадать тут хуже, чем принять явное правило.
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("пустой момент выката")
    if s.endswith(" UTC"):
        s = s[:-4].strip() + "+00:00"
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"не разобрал момент выката: {text!r}")
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def normalize(line: str) -> str:
    """Строка журнала → её «вид»: то, что останется одинаковым у повторов."""
    s = _ANSI.sub("", line).strip()
    for rx in _LEADING:
        cut = rx.sub("", s, count=1)
        if cut != s:
            s = cut
            break
    for rx, repl in _SUBS:
        s = rx.sub(repl, s)
    s = " ".join(s.split())
    return s[:180]


def interesting(line: str) -> bool:
    return bool(_SIGNAL.search(line))


def _run(cmd: list[str]) -> tuple[list[str], bool]:
    """Прогнать команду и вернуть (строки, обрезано ли по MAX_LINES)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    lines, truncated = [], False
    assert proc.stdout is not None
    for line in proc.stdout:
        if len(lines) >= MAX_LINES:
            truncated = True
            proc.kill()
            break
        lines.append(line.rstrip("\n"))
    proc.stdout.close()
    proc.wait()
    # -9 — это мы сами прибили процесс, упёршись в MAX_LINES.
    # Любой другой ненулевой код — отказ, даже если что-то напечаталось: docker
    # пишет «No such container» в stderr, а он у нас слит со stdout. Считать
    # такую строку журналом нельзя — она одинаково попадёт в оба окна, сойдёт
    # за фон, и опечатка в имени контейнера выдаст «нового в журнале нет».
    if proc.returncode not in (0, -9):
        detail = "\n".join(lines[:3]) or f"код {proc.returncode}"
        raise RuntimeError(" ".join(cmd) + ": " + detail)
    return lines, truncated


def read_docker(name: str, since: datetime, until: datetime) -> tuple[list[str], bool]:
    return _run(["docker", "logs", "--since", since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "--until", until.strftime("%Y-%m-%dT%H:%M:%SZ"), name])


def read_unit(name: str, since: datetime, until: datetime) -> tuple[list[str], bool]:
    # `@<epoch>` — единственный формат journalctl без часовых поясов и локали.
    return _run(["journalctl", "-u", name, "--no-pager", "-o", "cat",
                 "--since", f"@{int(since.timestamp())}",
                 "--until", f"@{int(until.timestamp())}"])


def _inspect(name: str) -> str:
    """Строка про состояние контейнера: рестарты, старт, health."""
    fmt = ("RestartCount={{.RestartCount}} Status={{.State.Status}} "
           "StartedAt={{.State.StartedAt}} Created={{.Created}} "
           "Health={{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}")
    try:
        out, _ = _run(["docker", "inspect", "-f", fmt, name])
    except RuntimeError as e:
        return f"состояние прочитать не вышло: {e}"
    return out[0] if out else "?"


def _unit_state(name: str) -> str:
    try:
        out, _ = _run(["systemctl", "show", name,
                       "-p", "NRestarts", "-p", "ActiveState",
                       "-p", "ActiveEnterTimestamp"])
    except RuntimeError as e:
        return f"состояние прочитать не вышло: {e}"
    return " ".join(x for x in out if x)


def container_created(name: str) -> datetime | None:
    """Когда контейнер создан. None — не узнали.

    Нужно для честности отчёта: `docker compose up -d` пересоздаёт контейнер, и
    журнал старого уезжает вместе с ним. Тогда окно «до» пустое не потому, что
    сервис молчал, а потому, что читать нечего, — и всё в окне «после» выглядит
    новым. Такой запуск обязан сказать об этом вслух.
    """
    try:
        out, _ = _run(["docker", "inspect", "-f", "{{.Created}}", name])
    except RuntimeError:
        return None
    try:
        return parse_ts(out[0]) if out else None
    except ValueError:
        return None


def tally(lines: list[str], use_all: bool) -> tuple[Counter, dict[str, str]]:
    """Виды строк → сколько раз встретились, плюс пример каждой."""
    counts: Counter = Counter()
    examples: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        if not use_all and not interesting(line):
            continue
        pat = normalize(line)
        if not pat:
            continue
        counts[pat] += 1
        examples.setdefault(pat, line.strip()[:200])
    return counts, examples


def classify(before: Counter, after: Counter, before_min: float, after_min: float):
    """Разложить виды из окна «после» на новые, участившиеся и фон."""
    new, surged, background = [], [], []
    for pat, cnt in after.items():
        was = before.get(pat, 0)
        if was == 0:
            new.append((cnt, pat))
            continue
        rate_after = cnt / after_min if after_min else 0.0
        rate_before = was / before_min if before_min else 0.0
        if cnt >= SURGE_MIN_COUNT and rate_after >= SURGE_FACTOR * rate_before:
            surged.append((cnt, pat, was, rate_before, rate_after))
        else:
            background.append((cnt, pat))
    new.sort(reverse=True)
    surged.sort(reverse=True)
    return new, surged, background


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(args, source_label: str, at: datetime, now: datetime,
                 before_start: datetime, before: Counter, after: Counter,
                 before_lines: int, after_lines: int, before_examples: dict,
                 after_examples: dict, warnings: list[str], state: str) -> tuple[str, int]:
    after_min = max((now - at).total_seconds() / 60.0, 0.01)
    before_min = max((at - before_start).total_seconds() / 60.0, 0.01)
    new, surged, background = classify(before, after, before_min, after_min)

    out = [
        f"источник: {source_label}",
        f"окно ПОСЛЕ выката: {_fmt(at)} … {_fmt(now)} "
        f"({after_min:.1f} мин, строк {after_lines})",
        f"окно ДО выката:    {_fmt(before_start)} … {_fmt(at)} "
        f"({before_min:.1f} мин, строк {before_lines})",
        "фильтр: " + ("все строки (--all)" if args.all
                      else "только строки с признаком ошибки; снять — --all"),
        f"состояние: {state}",
    ]
    for w in warnings:
        out.append(f"! {w}")
    out.append("")

    if new:
        out.append(f"НОВОЕ (в окне до выката такого не было) — видов {len(new)}:")
        for cnt, pat in new[:TOP]:
            out.append(f"  {cnt}×  {pat}")
            out.append(f"       пример: {after_examples.get(pat, '')}")
        if len(new) > TOP:
            out.append(f"  … и ещё {len(new) - TOP} видов, не показаны")
    else:
        out.append("НОВОЕ: нет")

    if surged:
        out.append("")
        out.append(f"УЧАСТИЛОСЬ (было и раньше, но реже) — видов {len(surged)}:")
        for cnt, pat, was, rb, ra in surged[:TOP]:
            out.append(f"  {cnt}×  {pat}")
            out.append(f"       было {was}× ({rb:.2f}/мин) → стало {ra:.2f}/мин")
        if len(surged) > TOP:
            out.append(f"  … и ещё {len(surged) - TOP} видов, не показаны")

    bg_lines = sum(c for c, _ in background)
    out.append("")
    out.append(f"ФОН (совпал с окном до выката): видов {len(background)}, "
               f"строк {bg_lines} — не показываю")

    if new or surged:
        out.append(f"ИТОГ: новых видов {len(new)}, участившихся {len(surged)} — разбираться")
        code = 10
    elif not after_lines:
        out.append("ИТОГ: в окне ПОСЛЕ выката нет ни строки — проверять нечего. "
                   "Это не «журнал чист»: так же выглядит неверно названный "
                   "источник")
        code = 0
    else:
        out.append("ИТОГ: нового в журнале нет")
        code = 0
    return "\n".join(out), code


def main(argv: list[str] | None = None) -> int:
    """Сравнить окна и записать свой код в журнал ворот сессии (DEV-88).

    Записываем сами, а не полагаемся на сессию: код в её трубе теряется
    (`logdiff … | tail; echo $?` печатает код `tail`), и из 20 проверок,
    звавших этот скрипт, ни одна свой код не показала. Файл — из окружения
    (`AGENT_GATES_FILE`, ставит раннер); нет его — скрипт зовут вне сессии,
    и записывать некуда. 10 ложится как «не прошло», 2 — как «ошибка
    обвязки»: прочитать журнал не вышло — это не «журнал чист».

    Шкалу объявляет сам скрипт, а не каждая сессия своей обёрткой
    (`gatelog.FINDINGS`, DEV-104): 10 помечается как НАХОДКА — «вот, что
    нового в журнале», а не «проверка провалилась». Красной находка остаётся,
    пока сессия её не разобрала (`gatelog.py --resolves logdiff --why …`):
    зелёный код был бы разрешением не смотреть на найденное вовсе.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        code = _compare(args)
    except SystemExit as e:
        # argparse: неверные аргументы выходят кодом 2 мимо `return`, и без
        # этой ветки сорванный вызов не оставил бы в журнале ворот ничего —
        # то есть выглядел бы как «logdiff не звали». `--help` — нулём, и
        # это не проверка: не записываем.
        code = e.code if isinstance(e.code, int) else 2
        if code == 0:
            return 0
    try:
        import gatelog
    except ImportError as e:                  # старая раскладка без модуля
        print(f"ворота logdiff не записаны: {e}", file=sys.stderr)
        return code
    gatelog.note_result("logdiff", ["logdiff.py", *args], code)
    return code


def _compare(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="logdiff.py",
        description="Что нового в журнале сервиса после выката")
    p.add_argument("--at", required=True,
                   help="момент выката, ISO-8601 (граница между окнами)")
    p.add_argument("--docker", metavar="CONTAINER", help="читать docker logs контейнера")
    p.add_argument("--unit", metavar="UNIT", help="читать journalctl юнита systemd")
    p.add_argument("--before-min", type=float, default=None,
                   help="длина окна ДО выката в минутах (по умолчанию 4× окна "
                        "после, но не меньше 30 и не больше 360)")
    p.add_argument("--all", action="store_true",
                   help="считать все строки, а не только похожие на ошибки")
    p.add_argument("--now", default=None,
                   help="конец окна ПОСЛЕ; по умолчанию текущий момент")
    args = p.parse_args(argv)

    if bool(args.docker) == bool(args.unit):
        print("нужен ровно один источник: --docker или --unit", file=sys.stderr)
        return 2
    try:
        at = parse_ts(args.at)
        now = parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if at > now:
        print(f"момент выката {_fmt(at)} в будущем относительно {_fmt(now)} — "
              "проверь --at", file=sys.stderr)
        return 2

    warnings: list[str] = []
    after_sec = (now - at).total_seconds()
    if after_sec < 60:
        warnings.append(
            f"окно после выката всего {int(after_sec)}с: то, что сыпется раз в "
            "минуту, в него ещё не попало — если тихо, перепроверь позже")
    if args.before_min is not None:
        before_min = max(args.before_min, 1.0)
    else:
        before_min = min(max(4 * after_sec / 60.0, 30.0), 360.0)
    before_start = at - timedelta(minutes=before_min)

    source_label = f"docker {args.docker}" if args.docker else f"unit {args.unit}"
    try:
        if args.docker:
            after_raw, after_cut = read_docker(args.docker, at, now)
            before_raw, before_cut = read_docker(args.docker, before_start, at)
            state = _inspect(args.docker)
            created = container_created(args.docker)
            if created and created > before_start:
                covered = max((at - created).total_seconds() / 60.0, 0.0)
                warnings.append(
                    f"контейнер создан {_fmt(created)} — позже начала окна ДО: "
                    f"фон известен только за {covered:.1f} мин из {before_min:.1f}, "
                    "и «новых» видов может оказаться больше настоящего")
        else:
            after_raw, after_cut = read_unit(args.unit, at, now)
            before_raw, before_cut = read_unit(args.unit, before_start, at)
            state = _unit_state(args.unit)
    except RuntimeError as e:
        print(f"журнал прочитать не вышло: {e}", file=sys.stderr)
        return 2

    if before_cut:
        warnings.append(
            f"окно ДО обрезано на {MAX_LINES} строк: часть фона не учтена, "
            "«новых» видов может оказаться больше настоящего")
    if after_cut:
        warnings.append(f"окно ПОСЛЕ обрезано на {MAX_LINES} строк")
    if not before_raw:
        warnings.append(
            "в окне ДО выката нет ни строки — сравнивать не с чем, всё ниже "
            "показано без фона и читается глазами. Частая причина: контейнер "
            "пересоздан выкатом (`docker compose up -d`), и журнал "
            "предшественника удалён вместе с ним")
    if not after_raw:
        # Пустое окно ПОСЛЕ и чистое окно ПОСЛЕ — разные вещи, а выглядят
        # одинаково: «нового нет». journalctl на несуществующий юнит выходит с
        # нулём и печатает пусто, так что поймать это по коду возврата нельзя.
        warnings.append(
            "в окне ПОСЛЕ выката нет ни строки: либо сервис молчит, либо "
            "источник не тот — проверь имя контейнера или юнита")

    before, before_ex = tally(before_raw, args.all)
    after, after_ex = tally(after_raw, args.all)
    report, code = build_report(args, source_label, at, now, before_start,
                                before, after, len(before_raw), len(after_raw),
                                before_ex, after_ex, warnings, state)
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
