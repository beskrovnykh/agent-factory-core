"""Хостинг репозиториев для действий человека: открытый PR по задаче и мерж.

Зачем: до 15.09.2026 единственный способ смержить принятую работу — уйти в
GitHub руками, каждый раз с логином с телефона. Владелец: «мне каждый раз
логиниться в GitHub». Статусные сообщения в чате его устраивают, а действия
должны быть под рукой — на странице подписи, рядом со свидетельствами.

Что здесь ПРОТОКОЛ: вопросы, которые продукт задаёт инсталляции, — «есть
ли по задаче открытый PR и какой» (по одной и списком), «смержи его» и «а
смерженный по ней есть?». Ответ на вопросы про PR — словарь фактов, ответ
на мерж — строка исхода или исключение. Все методы синхронные: зовутся из
потока, чтобы не держать цикл страницы.

Третий вопрос — для сверки пропущенных мержей (`reconcile`), и он
НЕОБЯЗАТЕЛЬНЫЙ: сверка решает по git, а хостинг лишь дописывает в запись
номер PR и ссылку.

Что здесь ЭТАЛОННАЯ РЕАЛИЗАЦИЯ: GitHub через `gh` по клонам площадки
(`AGENT_REPOS_ROOT`). Едет с продуктом, а не остаётся инсталляции, потому
что двум инсталляциям на GitHub незачем держать по копии одного и того же
разбора; инсталляция на GitLab пишет свою (у первого клиента мерж уже есть
в `gitlab_api`) и подключает её той же переменной `AGENT_HOSTING_SOURCE`.

Что здесь НЕ решается: кто вправе нажать. Это страница подписи — она пускает
к действию только по подписи Telegram, требует подтверждения и пишет в
журнал, кто нажал. Сюда приходят уже с решением.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from .deploy import MAIN_BRANCHES
from .gitref import Repos

log = logging.getLogger("intake")

# Сколько ждём `gh`. Зовётся из обработчика страницы; висеть там нельзя.
GH_TIMEOUT_SEC = 20

# Сколько репозиториев спрашиваем разом на вопрос списком. Клонов площадки
# семь, и последовательный обход стоил 5 секунд на каждой отрисовке очереди.
GH_AT_ONCE = 8

# Имя ветки задачи — соглашение `agent/<TASK-N>`, с суффиксом или без:
# `agent/DEV-66`, `agent/DEV-66-v2`. `DEV-6` не должно ловить `DEV-66`.
BRANCH_PREFIX = "agent/"


class Hosting(Protocol):
    def open_request(self, identifier: str) -> dict | None:
        """Открытый PR/MR по задаче или None. Ответ: `number`, `repo` (как
        зовёт хостинг: `owner/name`), `url`, `source_ref`, `target_ref`,
        `title`, `mergeable` (True/False/None — хостинг не сказал)."""
        ...

    def open_requests(self, identifiers: list[str]) -> dict[str, dict]:
        """Открытые PR/MR сразу по списку задач: `identifier` → тот же
        словарь, что у `open_request`. Задачи, у которых открытого PR нет, в
        ответе ОТСУТСТВУЮТ — это и есть ответ «мержить нечего».

        Зачем отдельный вопрос: страница подписи спрашивает его на КАЖДУЮ
        карточку при отрисовке (DEV-120) — чтобы кнопки «Смержить» не было
        там, где мержить нечего. По одной это столько же походов в хостинг,
        сколько карточек; списком — один обход репозиториев на всю страницу.

        Метод НЕОБЯЗАТЕЛЬНЫЙ: инсталляция старой сборки его не имеет, и
        страница спросит по одной через `open_request`.
        """
        ...

    def merge(self, request: dict) -> str:
        """Смержить то, что вернул `open_request`. Возвращает строку исхода
        для человека; не вышло — исключение с причиной."""
        ...

    def merged_request(self, identifier: str) -> dict | None:
        """УЖЕ смерженный PR/MR по задаче или None. Ответ — фактами о мерже,
        той же формы, что у `deploy.merged_work`: `mr_iid`, `ref` (куда),
        `source_ref` (откуда), `project_path`, `url`, `merged_by`,
        `merged_at`.

        Спрашивает сверка (`reconcile.Reconciler`) — и только затем, чтобы
        назвать в записи номер PR и ссылку. Решает ли задача закрытием, тут
        не решается: это git (`gitref`). Поэтому метод НЕОБЯЗАТЕЛЬНЫЙ —
        инсталляция старой сборки его не имеет, и сверка скажет об этом в
        журнал, а не упадёт.
        """
        ...


def branch_names(branch: str, identifier: str) -> bool:
    """`agent/DEV-66` и `agent/DEV-66-*` — да; `agent/DEV-660` — нет."""
    if not identifier or not branch.startswith(BRANCH_PREFIX + identifier):
        return False
    tail = branch[len(BRANCH_PREFIX) + len(identifier):]
    return tail == "" or not tail[0].isdigit()


def repo_of(origin: str) -> str:
    """`owner/name` из адреса remote: https, ssh, с `.git` и без."""
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", origin.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else ""


class GitHubHosting:
    """Протокол `Hosting` поверх `gh` на хосте. Токен — из логина `gh` под
    пользователем юнита; репозитории — по клонам площадки."""

    def __init__(self, repos_root: str = "", timeout: float = GH_TIMEOUT_SEC):
        self.repos = Repos(repos_root or os.environ.get("AGENT_REPOS_ROOT", ""))
        self.timeout = timeout

    def _run(self, *args: str) -> str:
        out = subprocess.run(["gh", *args], capture_output=True, text=True,
                             timeout=self.timeout)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout or "gh не ответил").strip()[:300])
        return out.stdout

    def repositories(self) -> list[str]:
        out = []
        for clone in self.repos.clones():
            rc, origin, _ = self.repos._git(clone, "remote", "get-url", "origin")
            repo = repo_of(origin) if rc == 0 else ""
            if repo and repo not in out:
                out.append(repo)
        return out

    def _prs_of(self, repo: str) -> list:
        """Открытые PR одного репозитория. Не перечислился — строка в журнал и
        пусто: одна недоступная площадка не должна прятать PR остальных."""
        try:
            raw = self._run("pr", "list", "--repo", repo, "--state", "open",
                            "--limit", "100", "--json",
                            "number,url,headRefName,baseRefName,title,mergeable")
            return json.loads(raw or "[]")
        except Exception as e:
            log.warning("хостинг: PR в %s не перечислить: %s", repo, e)
            return []

    def _open_prs(self):
        """Открытые PR по всем репозиториям площадки, по репозиторию за раз —
        для вопроса про ОДНУ задачу: там перебор кончается на первом
        совпадении, и спрашивать остальных незачем."""
        for repo in self.repositories():
            yield repo, self._prs_of(repo)

    @staticmethod
    def _request_of(repo: str, pr: dict) -> dict:
        m = str(pr.get("mergeable") or "").upper()
        return {"number": str(pr.get("number") or ""), "repo": repo,
                "url": str(pr.get("url") or ""),
                "source_ref": str(pr.get("headRefName") or ""),
                "target_ref": str(pr.get("baseRefName") or ""),
                "title": str(pr.get("title") or ""),
                "mergeable": (True if m == "MERGEABLE" else
                              False if m == "CONFLICTING" else None)}

    def open_request(self, identifier: str) -> dict | None:
        for repo, prs in self._open_prs():
            for pr in prs:
                if branch_names(str(pr.get("headRefName") or ""), identifier):
                    return self._request_of(repo, pr)
        return None

    def open_requests(self, identifiers: list[str]) -> dict[str, dict]:
        """Один обход репозиториев на весь список задач. Задача без открытого
        PR в ответ не попадает вовсе — отсутствие ключа и значит «мержить
        нечего».

        Первый найденный PR выигрывает — так же, как в `open_request`: у
        задачи с двумя открытыми PR (`agent/DEV-66` и `agent/DEV-66-v2`)
        страница показывает тот же, который смержила бы кнопка.
        """
        want = [i for i in (str(x or "").strip() for x in identifiers or []) if i]
        if not want:
            return {}
        # Репозитории спрашиваются разом: страница ждёт самый медленный `gh`,
        # а не их сумму. На семи клонах площадки это 5 секунд против 1 — те
        # самые секунды, из-за которых человек и ждал впустую (DEV-120).
        repos = self.repositories()
        with ThreadPoolExecutor(max_workers=max(1, min(len(repos), GH_AT_ONCE))) as pool:
            answers = list(pool.map(self._prs_of, repos))
        out: dict[str, dict] = {}
        # Порядок ответов — порядок репозиториев, а не порядок ответивших:
        # у задачи с двумя открытыми PR выигрывает тот же, что и у
        # `open_request`, иначе страница и кнопка показывали бы разное.
        for repo, prs in zip(repos, answers):
            for pr in prs:
                head = str(pr.get("headRefName") or "")
                for ident in want:
                    if ident not in out and branch_names(head, ident):
                        out[ident] = self._request_of(repo, pr)
                        break
        return out

    def merged_request(self, identifier: str) -> dict | None:
        """Смерженный PR по имени ветки — фактами о мерже (`deploy.
        merged_work`). Из нескольких (`agent/DEV-66` и `agent/DEV-66-v2`
        оба влиты) берём последний по времени мержа: он и есть работа задачи.

        Заголовок и тело здесь НЕ смотрим, в отличие от разбора события.
        Событие приходит про конкретный PR, а тут мы перебираем сотню чужих:
        «DEV-81» в теле чужого PR не делает его работой DEV-81.
        """
        for repo in self.repositories():
            try:
                raw = self._run("pr", "list", "--repo", repo, "--state", "merged",
                                "--limit", "100", "--json",
                                "number,url,headRefName,baseRefName,mergedAt,mergedBy")
                prs = json.loads(raw or "[]")
            except Exception as e:
                log.warning("хостинг: смерженные PR в %s не перечислить: %s", repo, e)
                continue
            mine = [p for p in prs
                    if branch_names(str(p.get("headRefName") or ""), identifier)
                    and str(p.get("baseRefName") or "") in MAIN_BRANCHES]
            if not mine:
                continue
            pr = max(mine, key=lambda p: str(p.get("mergedAt") or ""))
            return {"mr_iid": str(pr.get("number") or ""), "project_path": repo,
                    "ref": str(pr.get("baseRefName") or ""),
                    "source_ref": str(pr.get("headRefName") or ""),
                    "url": str(pr.get("url") or ""),
                    "merged_by": str((pr.get("mergedBy") or {}).get("login") or ""),
                    "merged_at": str(pr.get("mergedAt") or "")}
        return None

    def merge(self, request: dict) -> str:
        """Мерж-коммитом, с удалением ветки: там, где удаление при мерже
        включено в репозитории, флаг лишний, а где нет (website) —
        единственный способ не копить ветки `agent/*`."""
        self._run("pr", "merge", str(request["number"]), "--repo", str(request["repo"]),
                  "--merge", "--delete-branch")
        return f"смержен PR #{request['number']} в {request['repo']}, ветка удалена"
