#!/usr/bin/env python3
"""Линтер границ передачи — по трассе, а не по слову сессии (DEV-90).

Каждая сессия кончается передачей: работа — на проверку, проверка — человеку
или назад в работу, приём — постановкой в трекер. Контракт велит оставить
отчёт в задаче И перевести её; сессия может сделать одно без другого, и по
коду возврата этого не видно — семь проверок client-b 12–14.09.2026 написали
комментарий, не перевели задачу и вышли как ни в чём не бывало, пять из них
с кодом 0.

Линтер смотрит на то же, на что и `verify_order.py`: порядок и состав
вызовов инструментов в `transcript.jsonl`. Две вещи, которые он здесь
обязан учитывать, потому что на них первая редакция промахнулась бы:

* **в трекер ходят не только через MCP.** Когда ключ личности мёртв, сессия
  пишет в Linear из shell — `curl`/`python`/`node` с GraphQL-мутацией.
  DEV-78 (233741) оставил комментарий именно так, и по одним `mcp__linear__*`
  он выглядел бы «без комментария», а не «комментарий без перевода»;
* **закрытая задача без `review.md` — не нарушение.** Исходы 2 и 3 рабочего
  контракта (справка, отказ) кода не дают, ревьюить нечего.

    python3 handoff_lint.py /home/ubuntu/agent-work            # все каталоги
    python3 handoff_lint.py --since 20260915 /home/ubuntu/agent-work
    python3 handoff_lint.py <каталог-сессии> [<ещё>...]

Код выхода: 0 — нарушений нет, 10 — есть (это результат, а не сбой запуска),
2 — не нашли ни одной трассы.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from verify_order import calls as trace_calls, _stamp

# Комментарий в задачу: MCP-инструмент (хвост имени, префикс сервера
# отбрасываем) или GraphQL-мутация из shell.
COMMENT_TOOL_SUFFIXES = ("save_comment", "create_comment")
COMMENT_MUTATION = re.compile(r"\bcommentCreate\b")
# Перевод задачи: правка состояния или исполнителя. `save_issue` без этих
# полей — правка заголовка или создание задачи, не передача.
ISSUE_TOOL_SUFFIXES = ("save_issue", "update_issue")
TRANSITION_FIELDS = ("state", "stateId", "assignee", "assigneeId")
ISSUE_MUTATION = re.compile(r"\bissueUpdate\b")
TRANSITION_MUTATION_FIELDS = re.compile(r"\b(stateId|assigneeId)\b")

# Что каждая роль обязана оставить в рабочем каталоге.
REQUIRED_FILE = {"verify": "verdict.txt", "work": "review.md", "intake": "draft.json"}
# След ожидания проверок (DEV-16, `ciwait.STATE_FILE`): рабочая сессия,
# оставившая заявку, задачу НЕ переводит намеренно — переведёт раннер, когда
# узнает исход. Комментарий без перевода у такой сессии — не промах, а
# контракт. Строка, а не импорт: линтер гоняют и по чужим каталогам, где
# `ciwait` может не быть; совпадение держит тест.
RUNNER_MOVES_FILE = "ci-wait-state.json"
# Имена закрытых статусов по умолчанию — те, что у первого клиента. У
# инсталляции могут быть свои: `--closed`.
DEFAULT_CLOSED = ("Done", "Canceled", "Cancelled", "Duplicate")

MISS_NO_TRANSITION = "комментарий без перевода"
MISS_NO_COMMENT = "без комментария в задаче"


def kind_of(name: str) -> str:
    """Род сессии по имени каталога — так же, как его даёт раннер."""
    for kind in ("verify", "intake", "incident"):
        if f"-{kind}-" in name:
            return kind
    return "work"


def _command(call: dict) -> str:
    inp = call.get("input")
    return str(inp.get("command") or "") if isinstance(inp, dict) else ""


def leaves_task_comment(call: dict) -> bool:
    if call["name"].endswith(COMMENT_TOOL_SUFFIXES):
        return True
    return call["name"] == "Bash" and bool(COMMENT_MUTATION.search(_command(call)))


def moves_task(call: dict) -> bool:
    inp = call.get("input") if isinstance(call.get("input"), dict) else {}
    if call["name"].endswith(ISSUE_TOOL_SUFFIXES):
        return any(k in inp for k in TRANSITION_FIELDS)
    if call["name"] == "Bash":
        cmd = _command(call)
        return bool(ISSUE_MUTATION.search(cmd) and TRANSITION_MUTATION_FIELDS.search(cmd))
    return False


def last_state(seq: list[dict]) -> str:
    """Имя состояния из последнего перевода через MCP; пусто — не назвали
    (перевод из shell несёт id, а не имя) или не переводили."""
    state = ""
    for call in seq:
        inp = call.get("input") if isinstance(call.get("input"), dict) else {}
        if call["name"].endswith(ISSUE_TOOL_SUFFIXES) and inp.get("state"):
            state = str(inp["state"])
    return state


def judge(kind: str, seq: list[dict], files: set[str] | list[str],
          closed: tuple[str, ...] = DEFAULT_CLOSED, total: int | None = None) -> dict:
    """Одна сессия: что сделано на границе и чего не хватает. Чистая
    функция — по ней же гоняется журнал записанных трасс. `total` — сколько
    вызовов было в трассе вообще, если `seq` уже сведён к значимым: сессия,
    которая работала и ни слова не написала, отличается от сессии, которая
    не поднялась, ровно этим числом."""
    files = set(files)
    total = len(seq) if total is None else total
    commented = any(leaves_task_comment(c) for c in seq)
    moved = any(moves_task(c) for c in seq)
    violations: list[str] = []
    # Перевод за рабочую сессию делает раннер, если она отдала ему ожидание
    # проверок: след — файл состояния ожидания в её каталоге.
    runner_moves = kind == "work" and RUNNER_MOVES_FILE in files
    if kind in ("verify", "work", "incident"):
        if commented and not moved and not runner_moves:
            violations.append(MISS_NO_TRANSITION)
        elif total and not commented:
            violations.append(MISS_NO_COMMENT)
    required = REQUIRED_FILE.get(kind)
    if required and required not in files:
        if not (kind == "work" and last_state(seq) in closed):
            violations.append(f"нет {required}")
    return {"род": kind, "вызовов": total, "комментарий": commented,
            "перевод": moved, "состояние": last_state(seq),
            "нарушения": violations}


def _rc(workdir: Path):
    try:
        return json.loads((workdir / "stats.json").read_text()).get("код")
    except (OSError, ValueError):
        return None


def check(workdir: Path, closed: tuple[str, ...] = DEFAULT_CLOSED) -> dict:
    seq = trace_calls(workdir / "transcript.jsonl")
    files = {f for f in (*REQUIRED_FILE.values(), RUNNER_MOVES_FILE)
             if (workdir / f).is_file()}
    out = judge(kind_of(workdir.name), seq, files, closed)
    out.update({"каталог": workdir.name, "код": _rc(workdir)})
    return out


def find_sessions(paths: list[Path], since: str = "") -> list[Path]:
    """Каталоги сессий любого рода: сами каталоги или корни с ними."""
    found: list[Path] = []
    for p in paths:
        if (p / "transcript.jsonl").is_file() or (p / "stats.json").is_file():
            found.append(p)
            continue
        if p.is_dir():
            found.extend(d for d in sorted(p.iterdir())
                         if d.is_dir() and _stamp(d.name)
                         and ((d / "transcript.jsonl").is_file()
                              or (d / "stats.json").is_file()))
    if since:
        key = since.replace("-", "")
        found = [d for d in found if _stamp(d.name)[:len(key)] >= key]
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path,
                    help="каталог сессии или корень рабочих каталогов")
    ap.add_argument("--since", default="",
                    help="считать сессии не старше даты каталога, YYYYMMDD[HHMMSS]")
    ap.add_argument("--closed", default=",".join(DEFAULT_CLOSED),
                    help="имена закрытых статусов через запятую")
    args = ap.parse_args(argv)
    sessions = find_sessions(args.paths, args.since)
    if not sessions:
        print("трасс не нашли", file=sys.stderr)
        return 2
    closed = tuple(s.strip() for s in args.closed.split(",") if s.strip())
    bad = 0
    by_kind: dict[str, int] = {}
    for d in sessions:
        r = check(d, closed)
        mark = "НАРУШЕНИЕ" if r["нарушения"] else "ok"
        rc = "—" if r["код"] is None else r["код"]
        print(f"{mark:9} {r['каталог']:44} {r['род']:8} код={rc!s:4} "
              f"комментарий={int(r['комментарий'])} перевод={int(r['перевод'])}"
              + (f"  — {'; '.join(r['нарушения'])}" if r["нарушения"] else ""))
        if r["нарушения"]:
            bad += 1
            for v in r["нарушения"]:
                by_kind[v] = by_kind.get(v, 0) + 1
    print(f"\nсессий: {len(sessions)}, с нарушениями: {bad}"
          + "".join(f"\n  {v}: {n}" for v, n in sorted(by_kind.items())))
    return 10 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
