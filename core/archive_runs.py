#!/usr/bin/env python3
"""Архив записей о сессиях: перенести из рабочего каталога, а НЕ стереть.

Зачем. Рабочий каталог сессии (`<AGENT_WORK_ROOT>/<TASK>-<stamp>/`) — это
источник, а не хранилище: `runs.Ledger` прямо говорит, что он «живёт до первой
чистки диска или пересборки сервера». Пока чистки не было, он растёт: на
14.09.2026 на client-a это 3,3 ГБ при 88% занятого диска и 100% занятого swap,
на client-b — 53 МБ при 79%. Первая же уборка руками под «кончается место»
унесёт вместе с мусором транскрипты, по которым собирались калиброваться
проверку.

Поэтому уборка заводится СВОЯ и заранее, и она не удаляет. Что делает:

  1. Кладёт в отдельное дерево `<архив>/<имя каталога>/` записи о запуске:
     транскрипт, статистику, вердикт, лог сессии, тело отчёта. Крупное
     (`transcript.jsonl`, `session.log`) — сжатым: транскрипт жмётся примерно
     в десять раз, и три гигабайта становятся тремястами мегабайтами. Место
     освобождается, запись остаётся.
  2. Убирает из оригинала ТОЛЬКО то, что уже лежит в архиве и прочиталось
     оттуда обратно. Не прочиталось — оригинал остаётся на месте, и каталог
     считается неархивированным.
  3. Снимает ключ личности из `mcp.json` (см. `runner.mask_mcp_config`):
     рабочие каталоги живут месяцами, и живому ключу трекера в них не место.
  4. Оставляет в рабочем каталоге отметку `.archived` — куда уехало. Сам
     каталог не удаляется: по нему смотрят, что вообще было.

Чего НЕ делает: не удаляет рабочие каталоги, не трогает `~/.claude/projects`
(там сессии Claude CLI — их не архивирует и не удаляет никто), не трогает
каталог, по которому прямо сейчас идёт работа.

Как и `agent_workdir_reaper.py`, по умолчанию это сухой прогон: без `--apply`
скрипт только печатает, что сделал бы. Только stdlib: на инсталляции первого
клиента системный python без зависимостей.

Usage:
    archive_runs.py                       # сухой прогон
    archive_runs.py --apply               # перенести
    archive_runs.py --older-than 7 --apply
    archive_runs.py --root /home/ubuntu/agent-work \\
                    --archive /home/ubuntu/agent-runs-archive --apply

Код возврата: 0 — прошло штатно; 1 — сломан сам архиватор (нет корня, нет
прав). Пропущенные каталоги ненулевым кодом не отражаются: крон краснел бы на
своей нормальной работе.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("runs-archive")

DEFAULT_ROOT = os.environ.get("AGENT_WORK_ROOT", "/home/ubuntu/agent-work")
DEFAULT_AFTER_DAYS = float(os.environ.get("AGENT_RUNS_ARCHIVE_AFTER_DAYS", "3"))
DAY_SEC = 24 * 3600

# Что переносим. Порядок важен только для читаемости отчёта.
#
# Транскрипт и лог сжимаем: это 99% объёма и они читаются редко — при разборе,
# а не в работе. Вердикт, эталон, статистика и тело отчёта остаются как есть:
# они по сотне байт, и `zcat` ради них только мешает глазами смотреть архив.
PLAIN = ("stats.json", "verdict.txt", "expected.md", "body.md")
GZIPPED = ("transcript.jsonl", "session.log")

# Ключ личности в рабочем каталоге. Не переносим и не удаляем — снимаем.
SECRET = "mcp.json"
MASK = "снят при архивации"

MARKER = ".archived"


def procfs_available(proc_dir: str = "/proc") -> bool:
    """Можно ли вообще спросить, кто чем занят.

    Без procfs признак «по каталогу идёт работа» недоступен, а без него
    архивация сводится к «унеси файл из-под живой сессии». Поэтому проверка
    отдельная и явная: на такой машине скрипт отказывается работать, а не
    угадывает.
    """
    return os.path.isdir(proc_dir) and os.path.exists(f"{proc_dir}/self")


def busy_paths(proc_dir: str = "/proc") -> set | None:
    """Где сейчас работают живые процессы: их `cwd` и `exe`, один обход.

    Смотрим именно эти ссылки, а не время изменения каталога: сессия, которая
    десять минут думает, ничего в каталоге не меняет и по mtime выглядела бы
    законченной. И не cmdline — тот же довод, что у `agent_workdir_reaper.py`:
    в командной строке сессии лежит текст задания, а в нём путь рабочего
    каталога встречается обычным текстом, и по нему живая сессия сочлась бы
    занятой сама собой.

    Обход ОДИН на весь прогон, а не по разу на каталог: на client-a 243
    рабочих каталога и сотни процессов, и проверка «по разу на каждый»
    превращала минутную уборку в многоминутную.

    `None` — спросить не вышло. Это не «никто не занят»: снаружи такой ответ
    обязан читаться как «считаем занятыми всех».
    """
    try:
        entries = os.listdir(proc_dir)
    except OSError:
        return None
    found = set()
    for entry in entries:
        if not entry.isdigit():
            continue
        for link in ("cwd", "exe"):
            try:
                found.add(os.readlink(f"{proc_dir}/{entry}/{link}"))
            except OSError:
                continue
    return found


def is_busy(workdir: Path, where: set | None) -> bool:
    """Идёт ли по этому каталогу работа — по снимку из `busy_paths`.

    Не смогли посмотреть (`where is None`) — считаем занятым: отложить
    архивацию на сутки дешевле, чем увести файл из-под работающей сессии.
    """
    if where is None:
        return True
    target = str(workdir.resolve())
    prefix = target + "/"
    return any(p == target or p.startswith(prefix) for p in where)


def age_sec(path: Path, now: float) -> float:
    """Возраст каталога по самому свежему, что в нём есть.

    По mtime самого каталога считать нельзя: он меняется только когда в
    каталоге появляются или исчезают имена, а дописанный транскрипт его не
    трогает — законченная минуту назад сессия выглядела бы старой.
    """
    newest = path.stat().st_mtime
    try:
        for child in path.iterdir():
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return max(0.0, now - newest)


def gzip_to(src: Path, dst: Path) -> int:
    """Сжать src в dst и убедиться, что оно читается обратно.

    Возвращает число байт, прочитанных из архива. Расходится с оригиналом —
    это исключение: смысл упражнения в том, чтобы оригинал удалять ТОЛЬКО
    после доказательства, что копия целая.
    """
    with src.open("rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)
    size = src.stat().st_size
    back = 0
    with gzip.open(dst, "rb") as fin:
        while True:
            chunk = fin.read(1 << 20)
            if not chunk:
                break
            back += len(chunk)
    if back != size:
        raise OSError(f"{dst}: перечитал {back} Б вместо {size} Б")
    return back


def copy_to(src: Path, dst: Path) -> int:
    """Скопировать как есть и сверить размер. Возвращает байты копии."""
    shutil.copyfile(src, dst)
    size, back = src.stat().st_size, dst.stat().st_size
    if back != size:
        raise OSError(f"{dst}: {back} Б вместо {size} Б")
    return back


def mask_secret(workdir: Path, apply: bool) -> bool:
    """Снять ключ личности из `mcp.json`, оставив сам файл.

    Не удаляем: данных в файле нет, есть только ключ, а факт «сессия ходила в
    трекер от личности» при разборе каталога полезен. Тот же приём, что в
    `runner.mask_mcp_config`, — здесь он для каталогов, доработавших ДО того,
    как раннер научился снимать ключ за собой.
    """
    path = workdir / SECRET
    if not path.exists():
        return False
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    headers = (body.get("mcpServers", {}).get("linear", {})
               .get("headers", {}))
    token = headers.get("Authorization", "")
    if not token or MASK in token:
        return False
    if not apply:
        return True
    headers["Authorization"] = f"Bearer {MASK}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(body, ensure_ascii=False))
    return True


def archive_one(workdir: Path, archive_root: Path, apply: bool) -> dict:
    """Перенести записи одного каталога. → что сделано, для отчёта.

    Порядок жёсткий: скопировать → перечитать → и только потом убрать
    оригинал. Любая осечка на первых двух шагах оставляет оригинал на месте.
    """
    out = {"каталог": workdir.name, "перенесено": [], "освобождено": 0,
           "ключ снят": False, "осечки": []}
    dest = archive_root / workdir.name
    if apply:
        dest.mkdir(parents=True, exist_ok=True)

    for name in PLAIN + GZIPPED:
        src = workdir / name
        if not src.is_file():
            continue
        gz = name in GZIPPED
        dst = dest / (name + ".gz" if gz else name)
        size = src.stat().st_size
        if not apply:
            out["перенесено"].append(name)
            out["освобождено"] += size
            continue
        try:
            (gzip_to if gz else copy_to)(src, dst)
        except OSError as e:
            # Оригинал НЕ трогаем: неудачная копия — это ещё не архив.
            out["осечки"].append(f"{name}: {e}")
            continue
        src.unlink()
        out["перенесено"].append(name)
        out["освобождено"] += size

    out["ключ снят"] = mask_secret(workdir, apply)

    if apply and out["перенесено"]:
        # Отметка нужна человеку, который откроет пустой каталог и не поймёт,
        # унесло его уборкой или сессия ничего не оставила.
        (workdir / MARKER).write_text(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} записи о запуске "
            f"перенесены в {dest}\n"
            f"перенесено: {', '.join(out['перенесено'])}\n"
            f"транскрипт и лог сжаты gzip, читать: zcat <файл>.gz\n")
    return out


def human(nbytes: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if nbytes < 1024 or unit == "ГБ":
            return f"{nbytes:.0f} {unit}" if unit == "Б" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024.0
    return f"{nbytes:.1f} ГБ"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help=f"корень рабочих каталогов (default {DEFAULT_ROOT})")
    ap.add_argument("--archive", default=os.environ.get("AGENT_RUNS_ARCHIVE", ""),
                    help="куда переносить (default <root>-archive)")
    ap.add_argument("--older-than", type=float, default=DEFAULT_AFTER_DAYS,
                    metavar="ДНЕЙ",
                    help=f"возраст, с которого архивируем (default {DEFAULT_AFTER_DAYS})")
    ap.add_argument("--mask-only", action="store_true",
                    help="только снять ключи личности из mcp.json, ничего не "
                         "переносить: разовая уборка по уже накопленному, без "
                         "оглядки на возраст каталога")
    ap.add_argument("--apply", action="store_true",
                    help="реально переносить (без флага — сухой прогон)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    archive_root = Path(args.archive or f"{str(root).rstrip('/')}-archive")
    if not root.is_dir():
        log.error("нет корня рабочих каталогов: %s", root)
        return 1
    if args.apply and not procfs_available():
        log.error("нет /proc — не спросить, по какому каталогу идёт работа; "
                  "архивировать вслепую нельзя")
        return 1
    if archive_root == root or str(archive_root).startswith(str(root) + "/"):
        # Архив внутри источника — это не архив: следующая уборка унесёт его
        # вместе с каталогами, ради спасения которых он и заводился.
        log.error("архив %s лежит внутри %s — так нельзя", archive_root, root)
        return 1
    if args.apply and not args.mask_only:
        archive_root.mkdir(parents=True, exist_ok=True)

    now = time.time()
    cutoff = args.older_than * DAY_SEC
    can_ask_busy = procfs_available()
    where = busy_paths() if can_ask_busy else None
    if not can_ask_busy:
        log.warning("нет /proc — занятость каталогов не проверяется, "
                    "это сухой прогон и только он")
    done, freed, masked, skipped_busy, skipped_young, misses = 0, 0, 0, 0, 0, []
    for workdir in sorted(p for p in root.iterdir() if p.is_dir()):
        if args.mask_only:
            # Возраст тут ни при чём: ключ в каталоге, доработавшем минуту
            # назад, ровно так же лежит на диске. Занятость — при чём: у
            # идущей сессии конфиг из-под рук не забирают.
            if can_ask_busy and is_busy(workdir, where):
                skipped_busy += 1
                continue
            if mask_secret(workdir, args.apply):
                masked += 1
                log.info("%s%s — ключ личности снят",
                         "" if args.apply else "[dry-run] ", workdir.name)
            continue
        if (workdir / MARKER).exists():
            continue
        if age_sec(workdir, now) < cutoff:
            skipped_young += 1
            continue
        if can_ask_busy and is_busy(workdir, where):
            log.info("%s — по каталогу идёт работа, пропускаю", workdir.name)
            skipped_busy += 1
            continue
        out = archive_one(workdir, archive_root, args.apply)
        misses.extend(f"{workdir.name}/{m}" for m in out["осечки"])
        if out["ключ снят"]:
            masked += 1
        if not out["перенесено"]:
            continue
        done += 1
        freed += out["освобождено"]
        log.info("%s%s → %s (%s)", "" if args.apply else "[dry-run] ",
                 workdir.name, ", ".join(out["перенесено"]),
                 human(out["освобождено"]))

    if args.mask_only:
        log.info("%sключей снято %d, пропущено занятых %d",
                 "" if args.apply else "[dry-run] ", masked, skipped_busy)
    else:
        log.info("%sкаталогов перенесено %d, освобождено %s, ключей снято %d; "
                 "пропущено: молодых %d, занятых %d",
                 "" if args.apply else "[dry-run] ", done, human(freed), masked,
                 skipped_young, skipped_busy)
    for miss in misses:
        log.warning("не перенеслось, оригинал на месте: %s", miss)
    if not args.apply:
        log.info("это сухой прогон — ничего не тронуто, добавь --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
