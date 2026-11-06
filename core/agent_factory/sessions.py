"""Что фабрика делает прямо сейчас и чего это стоило.

Раннер оставляет после каждой сессии рабочий каталог: промпт, лог и
`transcript.jsonl`, последняя запись которого — итог от Claude Code со
стоимостью, токенами (включая кэш), числом ходов и длительностью. Это и есть
единственный честный источник: не оценка, а то, что выставил счётчик.

Читаем файлы, а не спрашиваем раннер: он занят сессиями, у него нет ни порта,
ни API, и заводить их ради статистики значит держать второй контракт. Каталог
один и тот же у любой инсталляции — его кладёт продуктовый раннер.
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("intake")

# Сессия старше этого без итоговой записи считается мёртвой, а не идущей:
# раннер её уже снял по таймауту, файл просто остался.
STALE_SEC = 3 * 3600


def _result(path: str) -> dict | None:
    """Итоговая запись сессии, если она есть. Читаем с конца: файл — сотни
    килобайт, а нужна последняя строка."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    for line in reversed(tail.splitlines()):
        if '"type"' not in line or '"result"' not in line:
            continue
        try:
            got = json.loads(line)
        except Exception:
            continue
        if got.get("type") == "result":
            return got
    return None


def _stop_reason(got: dict | None) -> str:
    """Нештатная причина конца захода или пусто.

    `subtype` от Claude Code — про заход целиком («success», «error_max_turns»,
    «error_during_execution»), `stop_reason` — про последний ответ модели.
    Берём первое, что не означает «дошло до конца само».
    """
    if not got:
        return ""
    sub = str(got.get("subtype") or "")
    if sub and sub != "success":
        return sub
    stop = str(got.get("stop_reason") or "")
    return "" if stop in ("", "end_turn", "stop_sequence") else stop


def walk(work_root: str, since: float = 0.0) -> list[dict]:
    """Сессии из рабочего каталога: имя, когда, идёт ли, чего стоила."""
    out = []
    try:
        names = os.listdir(work_root)
    except Exception as e:
        log.warning("рабочий каталог %s не прочитан: %s", work_root, e)
        return out
    now = time.time()
    for name in names:
        d = os.path.join(work_root, name)
        t = os.path.join(d, "transcript.jsonl")
        try:
            started = os.path.getctime(d)
            touched = os.path.getmtime(t)
        except Exception:
            continue
        if started < since:
            continue
        got = _result(t)
        usage = (got or {}).get("usage") or {}
        # Ресурсы пишет раннер отдельным файлом: в итоге от Claude Code их нет,
        # он про свою сторону, а память и слот — про нашу.
        stats = {}
        try:
            with open(os.path.join(d, "stats.json")) as f:
                stats = json.load(f)
        except Exception:
            pass
        out.append({
            "name": name,
            # Имя каталога: «DEV-62-verify-20260912-030351». Задача — до
            # первого служебного хвоста, иначе в карточке окажется «DEV-62-verify».
            "task": name.split("-2026")[0].split("-verify")[0]
                        .split("-intake")[0].split("-incident")[0]
                        .split("-merge")[0],
            "started": started,
            "touched": touched,
            "идёт": got is None and (now - touched) < STALE_SEC,
            "брошена": got is None and (now - touched) >= STALE_SEC,
            "стоимость": float((got or {}).get("total_cost_usd") or 0.0),
            "ходов": int((got or {}).get("num_turns") or 0),
            "секунд": int((got or {}).get("duration_ms") or 0) // 1000,
            "память": int(stats.get("пик_мб") or 0),
            "род": stats.get("род") or ("проверка" if "-verify-" in name
                                        else "приём" if name.startswith("INTAKE")
                                        else "merge" if "-merge-" in name
                                        else "работа"),
            "слот": int(stats.get("слот") or 0),
            # Машиночитаемый исход запуска (`runner.OUTCOMES`). Пусто — stats
            # от раннера прежней версии либо сессия ещё идёт: врать исходом по
            # коду возврата нельзя, «нет отметки» и «FAILED» — разные факты.
            "исход": str(stats.get("исход") or ""),
            # Что ответил шлюз тяжёлой задачи (DEV-56): вердикт, признаки,
            # счёт последствий. Пусто — у этого захода шлюза не было (не
            # работа) либо раннер прежней версии: «лёгкая» и «шлюза не было»
            # обязаны различаться, иначе журнал покажет пройденным то, чего
            # не случилось.
            "шлюз": stats.get("шлюз") or {},
            # Класс расхождения вердикта и точка проверки (DEV-89) — пишет
            # раннер только у проверок; по ним ворота постановки решают, есть
            # ли смысл в следующей. Пусто — работа, приём, прежний раннер.
            "класс": str(stats.get("класс") or ""),
            "режим": str(stats.get("режим") or ""),
            # Почему заход кончился — СЛОВАМИ ПОСТАВЩИКА, из итоговой записи
            # транскрипта. Не наш вывод по коду возврата: «упёрлась в ходы» и
            # «сессия сама закончила» дают один и тот же rc=0, а разница между
            # ними — ровно то, что нужно видеть в учёте. Пусто, когда всё
            # штатно: строка «end_turn» в карточке ничего не добавляет.
            "остановка": _stop_reason(got),
            "вход": int(usage.get("input_tokens") or 0),
            "выход": int(usage.get("output_tokens") or 0),
            "кэш": int(usage.get("cache_read_input_tokens") or 0)
                   + int(usage.get("cache_creation_input_tokens") or 0),
        })
    return sorted(out, key=lambda s: s["started"], reverse=True)


def summary(sessions: list[dict]) -> dict:
    """Сводка по списку. Кэш держим отдельно: он и есть основная масса
    токенов, и смешав его с входом, получаем число, по которому ничего не
    видно."""
    done = [s for s in sessions if not s["идёт"] and not s["брошена"]]
    return {
        "идёт": len([s for s in sessions if s["идёт"]]),
        "брошено": len([s for s in sessions if s["брошена"]]),
        "закончено": len(done),
        "стоимость": round(sum(s["стоимость"] for s in sessions), 2),
        "вход": sum(s["вход"] for s in sessions),
        "выход": sum(s["выход"] for s in sessions),
        "кэш": sum(s["кэш"] for s in sessions),
        "секунд": sum(s["секунд"] for s in sessions),
        "пик_мб": max([s["память"] for s in sessions] or [0]),
    }


def human_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн".replace(".0 ", " ")
    if n >= 1_000:
        return f"{n // 1000} тыс."
    return str(n)


def human_left(seconds: float) -> str:
    """Сколько ждать: «через 1 ч 20 мин», «через 7 мин», «можно сейчас»."""
    seconds = int(max(0, seconds))
    if seconds <= 0:
        return "можно сейчас"
    if seconds < 60:
        return "меньше минуты"
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"
