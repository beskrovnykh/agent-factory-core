"""Что есть на площадке для проверки: ветка, коммит и где он относительно main.

Воротам перед постановкой проверки (`deploy.Verification.preflight`) нужны
три факта, которые без git не узнать: есть ли ветка задачи, есть ли коммит и
влит ли он уже в релизную ветку. До ворот это выясняла сама сессия — за
$2–3 и десять минут, — и когда выяснялось «ветки нет», деньги были потрачены
(DEV-81, 14.09.2026).

Работает по клонам площадки (`AGENT_REPOS_ROOT`): ветку спрашивает у
хостинга (`ls-remote`, без записи в клон), коммит и предковость считает
локально, дотягивая недостающие объекты `fetch`'ем. В клон ничего, кроме
remote-tracking веток и объектов, не пишет — так же, как это делает каждая
сессия первым шагом.

Имя ветки задачи — соглашение `agent/<TASK-N>`, но живые ветки называются и
`agent/DEV-66-v2`, `agent/DEV-84-fix`, `agent/DEV-76-android`: суффикс
ставит рабочая сессия, когда одной ветки мало. Поэтому ищем по приставке
(`agent/DEV-66` и `agent/DEV-66-*`), а из найденного берём ту, что ещё не
влита в main — именно её надо смотреть; влиты все — самую свежую. Без
этого перевод DEV-66 в проверку 14.09 смотрел бы на смерженную первую
ветку и не заметил бы `-v2` с новой работой.

Ветки может уже не быть, а коммит — быть: хостинг удаляет ветку при мерже.
Тогда ищем в журнале main коммит, называющий задачу (`Merge pull request
#16 from …/agent/DEV-81-silent`). Нашли — наблюдать есть что, и это выкат,
а не ветка.

Три исхода поиска, и путать их нельзя: НАШЛИ, НЕТ и НЕ ЗНАЕМ. «Нет» — все
клоны ответили и ни в одном ветки/коммита не оказалось: ворота вправе не
ставить сессию. «Не знаем» (`Unavailable`) — git не отвечает, корня нет,
хостинг молчит: ворота ставят сессию как раньше, с пометкой в записи.
Отсечь настоящую проверку из-за упавшей сети хуже, чем поднять лишнюю.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("intake")

# Одна команда git — не дольше этого. Ворота работают внутри обработки
# вебхука, и висеть там минутами нельзя: трекер сочтёт доставку неудавшейся
# и повторит её.
GIT_TIMEOUT_SEC = float(os.environ.get("AGENT_GIT_TIMEOUT_SEC", "20"))


class Unavailable(RuntimeError):
    """Ответа нет: git не запускается, корня клонов нет или хостинг не
    ответил ни по одному клону. Это не «ветки нет» — это «не знаем»."""


@dataclass(frozen=True)
class Located:
    repo: str           # каталог клона, где нашлось
    ref: str            # имя ветки, как её зовёт хостинг; для коммита из main — сама main
    sha: str            # полный sha коммита
    ancestor: bool      # влит ли в origin/<main>
    when: int = 0       # время коммита, unix


class Repos:
    def __init__(self, root: str, main: str = "", timeout: float = GIT_TIMEOUT_SEC):
        self.root = str(root or "")
        self.main = str(main or "")
        self.timeout = timeout

    # ── клоны ─────────────────────────────────────────────────────────────

    def clones(self) -> list[str]:
        """Каталоги с `.git` под корнем: сам корень, дети и внуки. Внуки — из-за
        раскладки первого клиента (`<корень>/backend/<репо>`)."""
        root = Path(self.root)
        if not self.root or not root.is_dir():
            return []
        if (root / ".git").exists():
            return [str(root)]
        out = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / ".git").exists():
                out.append(str(child))
                continue
            for grand in sorted(child.iterdir()):
                if grand.is_dir() and (grand / ".git").exists():
                    out.append(str(grand))
        return out

    def ordered(self, project_path: str = "") -> list[str]:
        """Клоны в порядке правдоподобия: репозиторий события первым.
        `grp/backend/core` → `<корень>/backend/core` (правило контракта) или
        `<корень>/core` (плоская раскладка)."""
        all_ = self.clones()
        if not project_path:
            return all_
        parts = [p for p in project_path.split("/") if p]
        guesses = []
        if len(parts) > 1:
            guesses.append(os.path.join(self.root, *parts[1:]))
        if parts:
            guesses.append(os.path.join(self.root, parts[-1]))
        first = [g for g in guesses if g in all_]
        return first + [c for c in all_ if c not in first]

    # ── git ───────────────────────────────────────────────────────────────

    def _git(self, repo: str, *args: str) -> tuple[int, str, str]:
        try:
            out = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                                 text=True, timeout=self.timeout)
        except FileNotFoundError as e:
            raise Unavailable(f"git не запускается: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise Unavailable(f"git {args[0]} в {repo}: не ответил за "
                              f"{self.timeout:.0f}с") from e
        return out.returncode, out.stdout.strip(), out.stderr.strip()

    def main_of(self, repo: str) -> str:
        if self.main:
            return self.main
        rc, out, _ = self._git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
        if rc == 0 and out.startswith("refs/remotes/origin/"):
            return out[len("refs/remotes/origin/"):]
        return "main"

    def _heads(self, repo: str, ref: str) -> list[tuple[str, str]]:
        """Ветки хостинга по имени и по приставке `<имя>-*`. Ошибка — это
        `Unavailable`, а не пустой список: молчащий хостинг и отсутствие
        ветки — разные факты."""
        rc, out, err = self._git(repo, "ls-remote", "--heads", "origin",
                                 f"refs/heads/{ref}", f"refs/heads/{ref}-*")
        if rc != 0:
            raise Unavailable(f"ls-remote в {repo}: {err[:160] or rc}")
        heads = []
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name.startswith("refs/heads/"):
                heads.append((sha.strip(), name[len("refs/heads/"):]))
        return heads

    def _has(self, repo: str, sha: str) -> bool:
        rc, _, _ = self._git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        return rc == 0

    def _fetch(self, repo: str, what: str) -> bool:
        rc, _, err = self._git(repo, "fetch", "-q", "origin", what)
        if rc != 0:
            log.info("fetch %s в %s не удался: %s", what, repo, err[:160])
        return rc == 0

    def _ensure(self, repo: str, sha: str, hint: str = "") -> bool:
        """Коммит в клоне: уже есть или дотянули. `hint` — ветка, где он
        должен лежать; без неё пробуем взять по sha (GitHub и GitLab это
        умеют)."""
        if self._has(repo, sha):
            return True
        self._fetch(repo, hint or sha)
        return self._has(repo, sha)

    def _is_ancestor(self, repo: str, sha: str) -> bool:
        main = self.main_of(repo)
        # main обязана быть свежей: ответ «не влит» по вчерашней копии
        # отправил бы проверку в режим ветки на уже выкаченную работу.
        self._fetch(repo, main)
        rc, _, err = self._git(repo, "merge-base", "--is-ancestor", sha,
                               f"origin/{main}")
        if rc == 0:
            return True
        if rc == 1:
            return False
        raise Unavailable(f"merge-base в {repo}: {err[:160] or rc}")

    def _when(self, repo: str, sha: str) -> int:
        rc, out, _ = self._git(repo, "log", "-1", "--format=%ct", sha)
        try:
            return int(out) if rc == 0 else 0
        except ValueError:
            return 0

    def _merged_by_ident(self, repo: str, ident: str) -> str:
        """Коммит в main, называющий задачу: мерж-коммит хостинга или сквош
        с идентификатором в заголовке. Пусто — не нашли."""
        main = self.main_of(repo)
        self._fetch(repo, main)
        # Не `\b`: у git regex зависит от libc, а класс символов работает
        # везде. «DEV-8» не должно ловить «DEV-81», «agent/DEV-81-silent» —
        # должно.
        pattern = rf"(^|[^A-Za-z0-9])({re.escape(ident)})([^0-9]|$)"
        rc, out, _ = self._git(repo, "log", f"origin/{main}", "-n1", "-E",
                               f"--grep={pattern}", "--format=%H")
        return out.strip() if rc == 0 else ""

    # ── поиск ─────────────────────────────────────────────────────────────

    def locate(self, ident: str, ref: str = "", sha: str = "",
               project_path: str = "") -> Located | None:
        """Где лежит работа задачи. None — нет нигде; `Unavailable` — не
        знаем.

        С известным sha (событие выката) ищем сам коммит. Без него (перевод
        по статусу) — ветку по имени и приставке, а если ветки уже нет,
        коммит с именем задачи в main.
        """
        repos = self.ordered(project_path)
        if not repos:
            raise Unavailable(f"клонов под {self.root or '<не задан>'} нет")
        if sha:
            return self._locate_commit(repos, ref, sha)
        return self._locate_branch(repos, ident, ref or f"agent/{ident}")

    def _locate_commit(self, repos: list[str], ref: str, sha: str) -> Located | None:
        errors = []
        for repo in repos:
            try:
                if not self._ensure(repo, sha, ref):
                    continue
                return Located(repo=repo, ref=ref, sha=sha,
                               ancestor=self._is_ancestor(repo, sha),
                               when=self._when(repo, sha))
            except Unavailable as e:
                errors.append(str(e))
        if errors:
            raise Unavailable("; ".join(errors)[:300])
        return None

    def _locate_branch(self, repos: list[str], ident: str, ref: str) -> Located | None:
        # Хостинг спрашиваем по всем клонам разом: восемь запросов подряд —
        # это секунды внутри обработки вебхука.
        found: list[tuple[str, str, str]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, len(repos))) as pool:
            for repo, got in zip(repos, pool.map(self._heads_or_error, repos,
                                                 [ref] * len(repos))):
                if isinstance(got, Exception):
                    errors.append(str(got))
                    continue
                found.extend((repo, name, head) for head, name in got)
        if not found:
            if errors:
                # Хоть один клон промолчал — «нет» сказать нельзя: ветка
                # могла быть именно там.
                raise Unavailable("; ".join(errors)[:300])
            return self._locate_merged(repos, ident)
        candidates = []
        for repo, name, head in found:
            try:
                if not self._ensure(repo, head, name):
                    continue
                candidates.append(Located(repo=repo, ref=name, sha=head,
                                          ancestor=self._is_ancestor(repo, head),
                                          when=self._when(repo, head)))
            except Unavailable as e:
                errors.append(str(e))
        if not candidates:
            raise Unavailable("; ".join(errors)[:300] or "ветка есть, коммит не дотянулся")
        # Невлитая работа важнее влитой: её ещё никто не смотрел. Среди
        # равных — самая свежая.
        candidates.sort(key=lambda c: (c.ancestor, -c.when))
        return candidates[0]

    def _heads_or_error(self, repo: str, ref: str):
        try:
            return self._heads(repo, ref)
        except Unavailable as e:
            return e

    def _locate_merged(self, repos: list[str], ident: str) -> Located | None:
        errors = []
        for repo in repos:
            try:
                sha = self._merged_by_ident(repo, ident)
                if sha:
                    return Located(repo=repo, ref=self.main_of(repo), sha=sha,
                                   ancestor=True, when=self._when(repo, sha))
            except Unavailable as e:
                errors.append(str(e))
        if errors:
            raise Unavailable("; ".join(errors)[:300])
        return None
