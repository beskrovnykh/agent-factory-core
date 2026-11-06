#!/usr/bin/env python3
"""Проверка скилов: набор кейсов при каждом скиле, вклад меряется абляцией.

Зачем это вообще. Разложить контракты по файлам мало: без прогона скил
неотличим от папки с текстом, и вопрос «а он что-нибудь делает?» остаётся без
ответа. Условие владельца от 16.09.2026: каждый выделенный скил прогоняем и
имеет набор при себе.

## Формат кейсов — РОДНОЙ для `claude plugin eval`

    core/skills/<скил>/evals/<кейс>/prompt.md      фронтматтер + задача
    core/skills/<скил>/evals/<кейс>/graders/*.md   оценщики

Оценщики бесплатные и детерминированные: `regex`, `tool_used`, `file_exists`.
Судью-модель (`llm`) и сверку с эталонной транскрипцией (`baseline`) не берём
— они стоят денег и врут.

## Почему прогоняет этот файл, а не сам `claude plugin eval`

`claude plugin eval` есть в CLI (проверено на 2.1.259), но на аккаунте
инсталляции отвечает «`plugin eval` is currently in early access» и выходит с
кодом 1 — то есть кейсы написать можно, а прогнать нечем. Этот модуль гоняет
ТЕ ЖЕ файлы тем же CLI в два рукава (`claude -p --plugin-dir` против прогона
без него). Когда доступ дадут, набор поедет в родной прогон без правок, и
этот модуль станет запасным путём, а не единственным.

## Две команды

    python3 core/skilleval.py lint            разбор набора, без модели
    python3 core/skilleval.py run [--skill X] прогон: вклад и срабатывание

`lint` — это ворота в CI: модели и денег в пайплайне нет, поэтому там
проверяется то, что считается без прогона (набор разбирается, оценщики
валидны, у скила есть и положительные кейсы, и близкие промахи). Числа
пунктов «вклад» и «срабатывание» снимаются `run` руками и кладутся в отчёт.
Ворота на разбор — не ворота на поведение, и путать их нельзя.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
GRADER_TYPES = {"regex", "tool_used", "tool_order", "file_exists"}
# Права по умолчанию — только чтение. Кейсу, которому нужно больше, их
# выдаёт его собственный `allowed_tools` во фронтматтере.
DEFAULT_TOOLS = "Read,Grep,Glob,Skill"
# Разделение на обучающую и проверочную части — фиксированное между итерациями:
# случайное давало бы новую границу на каждый прогон, и «стало лучше» нельзя
# было бы отличить от «повезло с разбиением». Держится хешем имени кейса.
HOLDOUT_SHARE = 0.4
# Доля прогонов, с которой кейс срабатывания считается сошедшимся.
TRIGGER_THRESHOLD = 0.5


# Поля, значение которых — список. Список разбирается ТОЛЬКО для них, и это не
# придирка к аккуратности: `pattern: [LOGIC]` иначе становится `['LOGIC']`, а
# `str(['LOGIC'])` — рабочее регулярное выражение (класс символов), которое
# совпадает почти с чем угодно. Оценщик при этом не падает и не ловится
# линтером — он просто ВСЕГДА проходит, завышая измеренный вклад скила.
# Ошибка в сторону «всё хорошо» дороже ошибки в сторону «сломалось».
LIST_KEYS = {"tags", "tools"}


def _frontmatter(text: str) -> tuple[dict, str]:
    """Свой разбор, а не yaml: ставить зависимость ради пяти полей незачем,
    а на хосте инсталляции pyyaml может и не оказаться."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    meta: dict = {}
    for line in head.strip().split("\n"):
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if key.strip() in LIST_KEYS and val.startswith("[") and val.endswith("]"):
            meta[key.strip()] = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        elif val in ("true", "false"):
            meta[key.strip()] = val == "true"
        elif val.isdigit():
            meta[key.strip()] = int(val)
        else:
            meta[key.strip()] = val
    return meta, body.lstrip("\n")


@dataclass
class Grader:
    path: Path
    meta: dict
    def kind(self) -> str: return str(self.meta.get("type", ""))


@dataclass
class Case:
    skill: str
    name: str
    path: Path
    meta: dict
    body: str
    graders: list[Grader] = field(default_factory=list)

    @property
    def is_trigger(self) -> bool:
        return "trigger" in (self.meta.get("tags") or [])

    @property
    def expects_skill(self) -> bool:
        return bool(self.meta.get("expect_skill", True))

    @property
    def holdout(self) -> bool:
        """Проверочная часть. Хеш имени, а не случайность: граница обязана
        пережить итерацию, иначе сравнивать прогоны не с чем."""
        digest = hashlib.sha256(f"{self.skill}/{self.name}".encode()).digest()
        return digest[0] / 256.0 < HOLDOUT_SHARE


def load_cases(skill: str | None = None) -> list[Case]:
    cases: list[Case] = []
    for prompt_file in sorted(SKILLS_DIR.glob("*/evals/**/prompt.md")):
        skill_name = prompt_file.relative_to(SKILLS_DIR).parts[0]
        if skill and skill_name != skill:
            continue
        meta, body = _frontmatter(prompt_file.read_text(encoding="utf-8"))
        case = Case(skill=skill_name, name=prompt_file.parent.name,
                    path=prompt_file.parent, meta=meta, body=body)
        for g in sorted((prompt_file.parent / "graders").glob("*.md")):
            gmeta, _ = _frontmatter(g.read_text(encoding="utf-8"))
            case.graders.append(Grader(path=g, meta=gmeta))
        cases.append(case)
    return cases


# ── ворота без модели ───────────────────────────────────────────────────────

def lint(cases: list[Case]) -> list[str]:
    problems: list[str] = []
    skills_on_disk = {p.name for p in SKILLS_DIR.iterdir()
                      if p.is_dir() and (p / "SKILL.md").exists()}
    covered = {c.skill for c in cases}
    for orphan in sorted(skills_on_disk - covered):
        problems.append(f"{orphan}: скил есть, набора кейсов нет")
    for case in cases:
        where = f"{case.skill}/{case.name}"
        if not case.body.strip():
            problems.append(f"{where}: пустая задача в prompt.md")
        if not case.meta.get("max_turns"):
            problems.append(f"{where}: не задан max_turns — прогон ничем не ограничен")
        for g in case.graders:
            if g.kind() not in GRADER_TYPES:
                problems.append(f"{where}: оценщик {g.path.name} вида "
                                f"{g.kind()!r} — не из {sorted(GRADER_TYPES)}")
            if g.kind() == "regex":
                try:
                    re.compile(str(g.meta.get("pattern", "")))
                except re.error as exc:
                    problems.append(f"{where}: {g.path.name} — битое выражение: {exc}")
            if g.kind() == "tool_used" and not g.meta.get("tool"):
                problems.append(f"{where}: {g.path.name} — не назван инструмент")
        if case.is_trigger:
            if case.expects_skill and not case.graders:
                problems.append(f"{where}: положительный кейс без оценщика — "
                                "он не может дать отрицательный ответ")
        elif not case.graders:
            problems.append(f"{where}: кейс вклада без оценщиков ничего не мерит")
    # Набор без близких промахов меряет только «поднимается ли скил», а не
    # «поднимается ли он там, где надо» — то есть отвечает всегда «да».
    for skill_name in sorted(covered):
        mine = [c for c in cases if c.skill == skill_name and c.is_trigger]
        pos = [c for c in mine if c.expects_skill]
        neg = [c for c in mine if not c.expects_skill]
        if not pos:
            problems.append(f"{skill_name}: нет положительных кейсов срабатывания")
        if not neg:
            problems.append(f"{skill_name}: нет близких промахов — доля срабатывания "
                            "по одним положительным запросам всегда единица")
        if not [c for c in cases if c.skill == skill_name and not c.is_trigger]:
            problems.append(f"{skill_name}: нет ни одного кейса вклада — "
                            "абляции не из чего считать")
    return problems


# ── прогон ──────────────────────────────────────────────────────────────────

def _plugin_from_skill(skill: str, root: Path) -> Path:
    """Скил лежит в раскладке `~/.claude/skills/<имя>/SKILL.md`, а `--plugin-dir`
    ждёт плагин. Собираем обёртку во временном каталоге, не трогая репозиторий."""
    plug = root / f"plug-{skill}"
    (plug / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plug / "skills" / skill).mkdir(parents=True, exist_ok=True)
    shutil.copy(SKILLS_DIR / skill / "SKILL.md", plug / "skills" / skill / "SKILL.md")
    (plug / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": skill, "version": "0.0.0", "description": f"{skill} (прогон набора)"}),
        encoding="utf-8")
    return plug


def _run_once(case: Case, plugin: Path | None, cfg: Path, timeout: int) -> dict:
    """Один прогон. Флаги у обоих рукавов одинаковые — иначе меряется не скил."""
    cmd = ["claude", "-p", case.body,
           "--model", str(case.meta.get("model") or "claude-haiku-4-5-20251001"),
           "--max-turns", str(case.meta.get("max_turns") or 6),
           "--output-format", "stream-json", "--verbose",
           # Инструменты — из кейса, а не одним списком на всех: скил, вся
           # работа которого в запуске команды (`daily` — это `daily.py`), без
           # `Bash` был бы измерен как бесполезный, и обвязка соврала бы про
           # него в ту же сторону, что и отсутствующий оценщик. Умолчание —
           # только чтение, чтобы кейс не получал прав, которых не просил.
           "--allowedTools", str(case.meta.get("allowed_tools") or DEFAULT_TOOLS)]
    if plugin:
        cmd += ["--plugin-dir", str(plugin)]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg))
    # Оборванный прогон — это НЕ пустой прогон. Ход, упершийся в `max_turns`,
    # и обрыв по сроку возвращают ненулевой код, но поток к этому моменту уже
    # показал вызовы инструментов — а для кейса срабатывания ровно они и есть
    # предмет замера. Выбрасывать такой прогон целиком значит записать скилу
    # «не поднялся» там, где он поднялся и не успел договорить (DEV-13).
    stdout, stderr, rc = "", "", 0
    try:
        proc = subprocess.run(cmd, cwd=SKILLS_DIR / case.skill, env=env,
                              capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr, rc = "оборван по сроку", -1
    text, tools, cost = [], [], 0.0
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text.append(block["text"])
                elif block.get("type") == "tool_use":
                    tools.append(block["name"])
        elif msg.get("type") == "result":
            cost = float(msg.get("total_cost_usd") or 0.0)
            if msg.get("result"):
                text.append(str(msg["result"]))
    # Ошибкой считаем только прогон, от которого не осталось НИЧЕГО: ни текста,
    # ни вызовов. Всё прочее — наблюдение, пусть и неполное.
    error = ""
    if not text and not tools:
        error = (stderr or stdout).strip()[:160] or f"rc={rc}"
    return {"text": "\n".join(text), "tools": tools, "cost": cost, "error": error,
            "truncated": rc != 0}


def _grade(case: Case, run: dict) -> tuple[int, int]:
    """Доля прошедших оценщиков. Все бесплатные и детерминированные."""
    passed = 0
    for g in case.graders:
        kind, ok = g.kind(), False
        if kind == "regex":
            ok = re.search(str(g.meta.get("pattern", "")), run["text"]) is not None
        elif kind == "tool_used":
            ok = str(g.meta.get("tool")) in run["tools"]
        elif kind == "tool_order":
            want = [t.strip() for t in (g.meta.get("tools") or [])]
            seq, i = run["tools"], 0
            for tool in seq:
                if i < len(want) and tool == want[i]:
                    i += 1
            ok = i == len(want) and bool(want)
        elif kind == "file_exists":
            ok = (SKILLS_DIR / case.skill / str(g.meta.get("path", ""))).exists()
        passed += int(ok)
    return passed, len(case.graders)


def run(cases: list[Case], runs: int, timeout: int, workers: int) -> dict:
    # Отдельный CLAUDE_CONFIG_DIR, а не домашний: иначе в оба рукава приезжают
    # личные скилы хоста и его настройки, и меряется не проверяемый скил, а они.
    # Ключи при этом нужны — лежат они в `~/.claude/.credentials.json`, а НЕ в
    # `~/.credentials.json`: на этом промахе первый прогон дал ноль по обоим
    # рукавам, что читалось как «скил ничего не даёт». Отсюда же проверка ниже:
    # прогон без ключей обязан упасть громко, а не приписать скилу нулевой вклад.
    cfg = Path(tempfile.mkdtemp(prefix="skilleval-cfg-"))
    creds = Path.home() / ".claude" / ".credentials.json"
    if not creds.exists():
        raise SystemExit(f"нет ключей по пути {creds} — прогонять нечем")
    shutil.copy(creds, cfg / ".credentials.json")
    root = Path(tempfile.mkdtemp(prefix="skilleval-plug-"))
    try:
        return _run_all(cases, runs, timeout, workers, cfg, root)
    finally:
        # `finally`, а не две строки в конце: в `cfg` лежит КОПИЯ ключей, и
        # оборванный прогон (Ctrl-C, исключение в пуле) оставил бы её в `/tmp`
        # — а `/tmp` на хосте инсталляции общий для всех сессий сразу.
        shutil.rmtree(cfg, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def _run_all(cases: list[Case], runs: int, timeout: int, workers: int,
             cfg: Path, root: Path) -> dict:
    plugins = {s: _plugin_from_skill(s, root) for s in {c.skill for c in cases}}

    jobs = []
    for case in cases:
        arms = [("со скилом", plugins[case.skill])]
        if not case.is_trigger:          # рукав без скила нужен только вкладу
            arms.append(("без скила", None))
        for arm, plugin in arms:
            for i in range(runs):
                jobs.append((case, arm, plugin, i))

    results: dict = {}
    print(f"прогонов: {len(jobs)} (кейсов {len(cases)}, повторов {runs})", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_once, c, p, cfg, timeout): (c, a)
                   for c, a, p, _ in jobs}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            case, arm = futures[fut]
            out = fut.result()
            got, total = _grade(case, out)
            results.setdefault((case.skill, case.name, arm), []).append(
                {"score": got / total if total else 0.0,
                 "fired": "Skill" in out["tools"], "cost": out["cost"],
                 "error": out["error"]})
            done += 1
            if out["error"]:
                print(f"  ✘ {case.skill}/{case.name} [{arm}]: {out['error']}", flush=True)
            if done % 10 == 0:
                print(f"  ... {done}/{len(jobs)}", flush=True)
    return results


def report(cases: list[Case], results: dict) -> int:
    by_skill: dict[str, list[Case]] = {}
    for c in cases:
        by_skill.setdefault(c.skill, []).append(c)
    total_cost = sum(r["cost"] for runs in results.values() for r in runs)
    broken = sum(1 for runs in results.values() for r in runs if r["error"])
    verdict = 0
    if broken:
        # Сорванный прогон и «скил не помог» дают одинаковый ноль. Считать их
        # одним — значит записать поломку обвязки в вывод про скил.
        print(f"⚠ сорвано прогонов: {broken} — числа ниже посчитаны не на всех")
        verdict = 1
    for skill, mine in sorted(by_skill.items()):
        print(f"\n=== {skill} " + "=" * (60 - len(skill)))
        contrib = [c for c in mine if not c.is_trigger]
        with_, without = [], []
        for case in contrib:
            a = results.get((skill, case.name, "со скилом"), [])
            b = results.get((skill, case.name, "без скила"), [])
            sa = sum(r["score"] for r in a) / len(a) if a else 0.0
            sb = sum(r["score"] for r in b) / len(b) if b else 0.0
            with_.append(sa); without.append(sb)
            print(f"  вклад  {case.name:<24} со скилом {sa:.2f}  без {sb:.2f}  "
                  f"дельта {sa - sb:+.2f}{'  [проверочная]' if case.holdout else ''}")
        if contrib:
            ma = sum(with_) / len(with_); mb = sum(without) / len(without)
            print(f"  ВКЛАД (абляция): {ma:.2f} против {mb:.2f}, дельта {ma - mb:+.2f}")
            if ma - mb <= 0:
                print("  ⚠ вклад нулевой или отрицательный — скил не нужен, так и написать")
                verdict = 1
        triggers = [c for c in mine if c.is_trigger]
        for want, label in ((True, "положительные"), (False, "близкие промахи")):
            group = [c for c in triggers if c.expects_skill is want]
            if not group:
                continue
            fired = tot = 0
            misses = []
            for case in group:
                runs_ = results.get((skill, case.name, "со скилом"), [])
                f = sum(1 for r in runs_ if r["fired"])
                fired += f; tot += len(runs_)
                share = f / len(runs_) if runs_ else 0.0
                # Порог 0.5: модель недетерминирована, поэтому кейс считается
                # сошедшимся по ДОЛЕ прогонов, а не по одному удачному.
                if (share < TRIGGER_THRESHOLD) is want:
                    misses.append(f"{case.name}({share:.2f})")
            rate = fired / tot if tot else 0.0
            print(f"  срабатывание, {label:<16} {rate:.2f}  ({fired}/{tot})"
                  + (f"  мимо: {', '.join(misses)}" if misses else ""))
            # Код выхода — это и есть ворота (условие владельца от 16.09).
            # Промах срабатывания раньше в него не попадал: скил, поднимающийся
            # на чужих запросах, давал зелёный прогон.
            if misses:
                verdict = 1
    print(f"\nстоимость прогона: ${total_cost:.2f}")
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["lint", "run"])
    ap.add_argument("--skill")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--tag", help="гонять только кейсы с этой меткой")
    args = ap.parse_args()

    cases = load_cases(args.skill)
    if args.tag:
        cases = [c for c in cases if args.tag in (c.meta.get("tags") or [])]
    if not cases:
        print("кейсов не найдено", file=sys.stderr)
        return 1
    if args.command == "lint":
        problems = lint(cases)
        for p in problems:
            print("✘", p)
        print(f"{'✘' if problems else '✓'} кейсов {len(cases)}, "
              f"скилов {len({c.skill for c in cases})}, промахов {len(problems)}")
        return 1 if problems else 0
    return report(cases, run(cases, args.runs, args.timeout, args.workers))


if __name__ == "__main__":
    sys.exit(main())
