"""Был ли у сессии дифф — по ветке задачи и коммитам на ней (DEV-121).

Раннер решает по этому, спрашивать ли с сессии код-ревью. До DEV-121 вопрос
решался наличием файла `review.md`: нет файла — значит шаг пропущен, и
человеку уезжало «Ревью не состоялось». Но у половины таких сессий ревьюить
было нечего: исход 4 контракта (стратегия на согласование) кода не пишет,
ветку оставляет пустой и задачу НЕ закрывает — то есть по статусу от
настоящего пропуска не отличается. Семь раз подряд человек прочитал про сбой,
которого не было, и перестал верить остальным сообщениям раннера.

Различие система знает сама, и спрашивать его у сессии не нужно: `/code-review`
берёт аргументом ВЕТКУ, и если на ветке задачи нет ни одного коммита поверх
базовой, ревьюить нечего буквально — не «сессия забыла написать строчку», а
нечего.

Отдельный модуль, а не функция в `runner`: правило проверяется на настоящих
репозиториях, а импорт `runner` тянет redis, трекер и окружение юнита — такой
тест пришлось бы писать через подмены, то есть проверять подмены.

Важная граница ответа: он верен, пока ветка задачи не влита. После мержа её
коммиты видны из базовой, `rev-list base..branch` даёт ноль, и сессия задним
числом выглядит как сессия без диффа. Раннеру это безразлично — он спрашивает
на выходе сессии, когда ветка ещё своя; разбору трасс полугодовой давности
этот ответ брать нельзя.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Имя задачи трекера: `DEV-121`, `TASK-204`.
TASK_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def git_out(cwd: Path, *args: str, quiet: bool = False) -> str | None:
    """git в каталоге cwd, вывод строкой. None вместо исключения и вместо
    ненулевого кода: ответ «не знаю» тут законный и обрабатывается вызывающим.

    Одна реализация на ядро (`runner._git` — она же): у уборки рабочих копий и
    у счёта коммитов требования к запуску git одинаковые, а две копии разошлись
    бы на первой же правке таймаута.
    """
    try:
        res = subprocess.run(["git", "-C", str(cwd), *args],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("git %s (%s): %s", " ".join(args), cwd, e)
        return None
    if res.returncode != 0:
        if not quiet:
            log.warning("git %s (%s): rc=%d %s", " ".join(args), cwd,
                        res.returncode, res.stderr.strip()[:200])
        return None
    return res.stdout.strip()


def base_ref(repo: Path) -> str:
    """Базовая ветка репозитория как `origin/main`; пусто — не определили.

    Сначала спрашиваем сам репозиторий (`origin/HEAD`), потому что основная
    ветка зовётся `main` не везде. Реф может быть не выставлен в клоне — тогда
    пробуем оба обычных имени, а не считаем `main` по умолчанию: ошибиться
    именем базы здесь значит посчитать всю историю ветки её собственными
    коммитами.
    """
    head = git_out(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", quiet=True)
    if head:
        return head
    for name in ("origin/main", "origin/master"):
        if git_out(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", quiet=True):
            return name
    return ""


def branch_re(key: str) -> re.Pattern:
    """Ветки задачи: `agent/DEV-121`, запасные `-v2`/`-v3` (шаг 3 контракта),
    имя, предложенное трекером (`engineer/dev-121-…`), и голое `DEV-121`.

    Разделитель после имени задачи обязателен: без него ключ `DEV-12` поймал
    бы ветку `agent/DEV-121` и приписал бы чужой дифф.
    """
    return re.compile(rf"(?:^|/){re.escape(key)}(?:[-_./].*)?$", re.I)


def code_trace(ident: str, repos_root: Path | str) -> str | None:
    """След кода, оставленный сессией задачи `ident`.

    Три разных ответа, и путать их нельзя:

    * строка — дифф есть, и она его называет («agent-factory: ветка
      agent/DEV-121, коммитов поверх origin/main — 2»). Названный дифф человек
      проверяет сам той же командой, а не верит раннеру на слово;
    * `""` — ветки задачи с коммитами нет ни в одном репозитории: ревьюить
      нечего;
    * `None` — ответа нет (имя задачи не разобрано, каталога репозиториев не
      прочитали, базовая ветка не определилась). Неизвестность трактуется как
      «дифф был»: ложная тревога дешевле непроверенного диффа, выданного за
      проверенный.

    Ветка переживает снятие рабочей копии (`runner.release_worktrees` убирает
    дерево, но не коммиты), поэтому смотрим в основные клоны, а не в каталог
    сессии: к моменту вопроса рабочей копии там уже нет. PR не спрашиваем —
    без коммитов его не бывает, а лишний ход в форж стоит сети и ключа.
    """
    key = (ident or "").strip()
    if not TASK_KEY.match(key):
        log.warning("дифф сессии не считаю: имя задачи %r не разобрано", ident)
        return None
    root = Path(repos_root)
    try:
        repos = sorted(p for p in root.iterdir() if (p / ".git").exists())
    except OSError as e:
        log.warning("дифф сессии не считаю: не прочитал %s (%s)", root, e)
        return None
    if not repos:
        log.warning("дифф сессии не считаю: в %s нет ни одного репозитория", root)
        return None
    mine = branch_re(key)
    for repo in repos:
        refs = git_out(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        if refs is None:
            return None
        branches = [b for b in refs.splitlines() if mine.search(b)]
        if not branches:
            continue
        base = base_ref(repo)
        if not base:
            log.warning("дифф сессии не считаю: базовая ветка %s не определилась", repo)
            return None
        for branch in branches:
            ahead = git_out(repo, "rev-list", "--count", f"{base}..{branch}")
            if ahead is None or not ahead.isdigit():
                log.warning("дифф сессии не считаю: %s не дал числа коммитов "
                            "на %s (%r)", repo, branch, ahead)
                return None
            if int(ahead) > 0:
                return f"{repo.name}: ветка {branch}, коммитов поверх {base} — {ahead}"
    return ""
