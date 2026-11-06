"""Числа дня для журнала разработки: три источника, один файл.

Журнал фабрики знает только про сессии, которые она сама подняла. Работа,
сделанная прямыми сессиями мимо очереди, туда не попадает вовсе — а в дни,
когда фабрика стоит на потолке, это вся работа дня. Поэтому день собирается
из трёх мест: сессии фабрики (деньги и ходы), мержи в git (что реально
приехало) и доска (что сдвинулось у человека).

Использование:
    python3 core/daily.py [ГГГГ-ММ-ДД] > journal/ГГГГ-ММ-ДД.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HOSTS = [
    ("client-b", "203.0.113.10", os.path.expanduser(
        "~/Workspace/client-a/env/umka-trend-following.pem"),
     "/home/ubuntu/agent-work-client-b", "/home/ubuntu/agent-runner-client-b"),
    ("client-a", "203.0.113.20", os.path.expanduser("~/.ssh/client-a.pem"),
     "/home/ubuntu/agent-work", "/home/ubuntu/agent-runner"),
]

REMOTE = r'''
import json, sys, time
sys.path.insert(0, "%(runner)s")
from agent_factory import sessions, runs
t0 = %(t0)f; t1 = t0 + 86400
ss = [s for s in sessions.walk("%(root)s", since=t0)
      if (s.get("когда") or t0) <= t1]
sm = sessions.summary(ss)
kinds = {}
for s in ss:
    b = kinds.setdefault(s.get("род") or "?", {"n": 0, "usd": 0.0, "ходов": 0})
    b["n"] += 1; b["usd"] += s.get("стоимость", 0.0); b["ходов"] += s.get("ходов") or 0
for b in kinds.values(): b["usd"] = round(b["usd"], 2)
verd = {}
for s in ss:
    head = (s.get("вердикт") or "").split()
    if head: verd[head[0]] = verd.get(head[0], 0) + 1
print(json.dumps({"сессий": len(ss), "usd": sm["стоимость"], "секунд": sm["секунд"],
                  "кэш": sm["кэш"], "выход": sm["выход"], "роды": kinds,
                  "вердикты": verd, "идёт": sm["идёт"]}, ensure_ascii=False))
'''


def _day_bounds(day: str) -> tuple[float, float]:
    t0 = time.mktime(time.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S"))
    return t0, t0 + 86400


def from_host(name, ip, key, root, runner, t0) -> dict:
    """Сессии фабрики за сутки. Хост молчит — говорим об этом, а не считаем ноль."""
    code = REMOTE % {"runner": runner, "root": root, "t0": t0}
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", "-i", key,
             f"ubuntu@{ip}", "python3 -"],
            input=code, capture_output=True, text=True, timeout=120)
        line = [l for l in out.stdout.splitlines() if l.startswith("{")]
        if not line:
            return {"хост": name, "ошибка": (out.stderr or "пусто").strip()[:200]}
        return {"хост": name, **json.loads(line[-1])}
    except Exception as e:  # сеть, таймаут, чужой питон — всё сюда
        return {"хост": name, "ошибка": f"{type(e).__name__}: {e}"[:200]}


def merges(day: str) -> list[dict]:
    """Что приехало в main за сутки. Источник правды о результате дня —
    журнал фабрики о прямых сессиях не знает."""
    fmt = "%H|%an|%ad|%s"
    out = subprocess.run(
        ["git", "log", "origin/main", f"--since={day} 00:00", f"--until={day} 23:59",
         "--date=format:%H:%M", f"--pretty={fmt}"],
        capture_output=True, text=True)
    got = []
    for line in out.stdout.splitlines():
        sha, who, when, subj = (line.split("|", 3) + ["", "", "", ""])[:4]
        got.append({"sha": sha[:8], "когда": when, "кто": who, "что": subj})
    return got


def _usd(x) -> str:
    return f"${x:,.2f}".replace(",", " ")


def render(data: dict) -> str:
    """День в markdown. Числа — как есть; чего нет, о том сказано словами.

    Пустое место в сводке хуже кривого числа: по молчанию нельзя отличить
    «ничего не было» от «не дотянулись до хоста»."""
    day = data["день"]
    out = [f"# День {day}", ""]

    live = [h for h in data["хосты"] if "ошибка" not in h]
    dead = [h for h in data["хосты"] if "ошибка" in h]
    total = sum(h["usd"] for h in live)
    n = sum(h["сессий"] for h in live)

    out += ["## Фабрика", ""]
    if live:
        out.append(f"**{_usd(total)}** · сессий {n} · "
                   f"{round(sum(h['секунд'] for h in live) / 3600, 1)} ч машинного времени")
        out.append("")
        out.append("| хост | сессий | расход | роды |")
        out.append("|---|---|---|---|")
        for h in live:
            kinds = ", ".join(f"{k} {v['n']}" for k, v in sorted(h["роды"].items()))
            out.append(f"| {h['хост']} | {h['сессий']} | {_usd(h['usd'])} | {kinds or '—'} |")
        out.append("")
    for h in dead:
        out.append(f"> Хост **{h['хост']}** не ответил: `{h['ошибка'].splitlines()[-1]}`")
        out.append("")

    m = data["мержи"]
    out += [f"## Приехало в main — {len(m)}", ""]
    if not m:
        out.append("Ничего. Бывает.")
    for c in m:
        out.append(f"- `{c['когда']}` {c['что']}")
    out.append("")

    out += ["## TLDR", "", "_(допиши сам)_", ""]
    return "\n".join(out)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    day = args[0] if args else time.strftime("%Y-%m-%d")
    t0, _ = _day_bounds(day)
    hosts = [from_host(*h, t0) for h in HOSTS]
    data = {"день": day, "хосты": hosts, "мержи": merges(day)}
    if "--json" in sys.argv:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
