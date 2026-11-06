#!/usr/bin/env python3
"""Порядок вызовов проверки — по трассе, а не по слову сессии (DEV-87).

Контракт проверки требует двух упорядоченных вещей, и обе выражены не
текстом отчёта, а порядком инструментов в `transcript.jsonl`:

* эталон раньше заявки: `expected.md` записан ДО первого чтения
  комментариев задачи (`list_comments`) или MR/PR (`gh pr view`,
  `glab mr view`). Прочитанная раньше эталона заявка автора становится
  эталоном сама — так DEV-46 получил два ложных `OK` подряд;
* вердикт после комментария: `verdict.txt` записан ПОСЛЕ `save_comment`
  (`gh pr comment`, `glab mr note`). Раннер читает файл как признак, что
  задача уже прокомментирована и переведена.

Второе правило контракт исполняет давно и им же откалибрована эта машинка:
на живых трассах client-b оно соблюдалось во всех сессиях. Первое появилось
вместе с этим модулем, и до выката смотреть его по старым трассам бесполезно
— там файла нет ни у одной. Поэтому есть `--since`: считать только сессии,
поднятые после выката.

    python3 verify_order.py /home/ubuntu/agent-work            # все *-verify-*
    python3 verify_order.py --since 20260915 /home/ubuntu/agent-work
    python3 verify_order.py <каталог-сессии> [<ещё>...]

Код выхода: 0 — нарушений нет, 10 — есть (это результат, а не сбой запуска),
2 — не нашли ни одной трассы.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REFERENCE_FILE = "expected.md"
VERDICT_FILE = "verdict.txt"

# Чем читают заявку автора. Имя MCP-инструмента приходит с префиксом сервера
# (`mcp__linear__list_comments`), поэтому сверяем хвост, а не всё имя.
CLAIM_TOOL_SUFFIXES = ("list_comments",)
CLAIM_COMMANDS = re.compile(r"\b(gh\s+pr\s+view|glab\s+mr\s+view)\b")

# Чем отчитываются: комментарий в задачу и в MR/PR.
COMMENT_TOOL_SUFFIXES = ("save_comment", "create_comment")
COMMENT_COMMANDS = re.compile(r"\b(gh\s+pr\s+comment|glab\s+mr\s+note)\b")

# Инструменты, которые пишут файл по имени.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# В shell файл создаётся перенаправлением или `tee`, и смотреть надо на ЦЕЛЬ
# перенаправления, а не на всю команду: `cat expected.md` — чтение, а
# `cat >> заметка.md <<EOF ... verdict.txt ... EOF` пишет заметку, в теле
# которой файл лишь упомянут. На второй ловушке первая редакция линтера дала
# два ложных нарушения на живых трассах (DEV-83, DEV-84).
def _shell_writes(cmd: str, name: str) -> bool:
    pattern = (r"(?:>{1,2}|\btee\b(?:\s+-a)?)\s*['\"]?[^\s'\"|;&<>]*"
               + re.escape(name) + r"['\"]?(?=\s|$|[|;&<])")
    return bool(re.search(pattern, cmd))


def calls(transcript: Path) -> list[dict]:
    """Вызовы инструментов сессии по порядку: имя и вход, без ответов.

    Битые строки пропускаются, а не роняют разбор: транскрипт пишется потоком,
    и последняя строка снятой на полуслове сессии бывает недописанной.
    """
    out: list[dict] = []
    try:
        lines = transcript.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "assistant":
            continue
        content = (event.get("message") or {}).get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out.append({"name": str(block.get("name") or ""),
                            "input": block.get("input") or {}})
    return out


def _command(call: dict) -> str:
    inp = call["input"]
    return str(inp.get("command") or "") if isinstance(inp, dict) else ""


def _path(call: dict) -> str:
    inp = call["input"]
    return (str(inp.get("file_path") or inp.get("path") or "")
            if isinstance(inp, dict) else "")


def writes_file(call: dict, name: str) -> bool:
    """Создаёт ли вызов файл `name` — прямой записью или из shell."""
    if call["name"] in WRITE_TOOLS:
        return _path(call).endswith(name)
    if call["name"] == "Bash":
        return _shell_writes(_command(call), name)
    return False


def reads_claim(call: dict) -> bool:
    if call["name"].endswith(CLAIM_TOOL_SUFFIXES):
        return True
    return call["name"] == "Bash" and bool(CLAIM_COMMANDS.search(_command(call)))


def leaves_comment(call: dict) -> bool:
    if call["name"].endswith(COMMENT_TOOL_SUFFIXES):
        return True
    return call["name"] == "Bash" and bool(COMMENT_COMMANDS.search(_command(call)))


def first(seq: list[dict], pred) -> int | None:
    for i, call in enumerate(seq):
        if pred(call):
            return i
    return None


def check(workdir: Path) -> dict:
    """Индексы ключевых вызовов одной сессии и список нарушений.

    Индексы — порядковые номера вызовов инструментов, не строк файла: между
    двумя вызовами бывают десятки служебных записей, и номер строки ничего
    не говорил бы о порядке действий.
    """
    seq = calls(workdir / "transcript.jsonl")
    reference = first(seq, lambda c: writes_file(c, REFERENCE_FILE))
    claim = first(seq, reads_claim)
    comment = first(seq, leaves_comment)
    verdict = first(seq, lambda c: writes_file(c, VERDICT_FILE))
    violations: list[str] = []
    if reference is None:
        violations.append("эталон не записан")
    elif claim is not None and not reference < claim:
        violations.append(f"заявка (#{claim}) раньше эталона (#{reference})")
    if verdict is not None and comment is not None and not comment < verdict:
        violations.append(f"вердикт (#{verdict}) раньше комментария (#{comment})")
    return {"каталог": workdir.name, "вызовов": len(seq),
            "эталон": reference, "заявка": claim,
            "комментарий": comment, "вердикт": verdict,
            "нарушения": violations}


def _stamp(name: str) -> str:
    """`DEV-85-verify-20260914-164042` → `20260914164042`; нет даты — пусто."""
    m = re.search(r"(\d{8})-(\d{6})$", name)
    return (m.group(1) + m.group(2)) if m else ""


def find_sessions(paths: list[Path], since: str = "") -> list[Path]:
    """Каталоги проверок: сами каталоги сессий или корни с `*-verify-*`."""
    found: list[Path] = []
    for p in paths:
        if (p / "transcript.jsonl").is_file():
            found.append(p)
            continue
        if p.is_dir():
            found.extend(d for d in sorted(p.iterdir())
                         if d.is_dir() and "-verify-" in d.name
                         and (d / "transcript.jsonl").is_file())
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
    args = ap.parse_args(argv)
    sessions = find_sessions(args.paths, args.since)
    if not sessions:
        print("трасс не нашли", file=sys.stderr)
        return 2
    bad = 0
    for d in sessions:
        r = check(d)
        where = " ".join(f"{k}=#{v}" if v is not None else f"{k}=—"
                         for k, v in (("эталон", r["эталон"]), ("заявка", r["заявка"]),
                                      ("комментарий", r["комментарий"]),
                                      ("вердикт", r["вердикт"])))
        mark = "НАРУШЕНИЕ" if r["нарушения"] else "ok"
        print(f"{mark:9} {r['каталог']}  {where}"
              + (f"  — {'; '.join(r['нарушения'])}" if r["нарушения"] else ""))
        bad += bool(r["нарушения"])
    print(f"\nсессий: {len(sessions)}, с нарушениями: {bad}")
    return 10 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
