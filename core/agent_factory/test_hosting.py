"""Хостинг для действий человека: открытый PR по задаче и мерж.

Эталонная реализация ходит через `gh`; здесь `gh` подменён, и проверяется
ровно то, что от неё требуется: найти PR по ИМЕНИ ВЕТКИ (`agent/DEV-66`
и `agent/DEV-66-*`, но не `agent/DEV-660`), в любом клоне площадки, и
смержить именно его, с удалением ветки.
"""
import json

import pytest

from agent_factory import hosting
from agent_factory.hosting import GitHubHosting, branch_names, repo_of


@pytest.mark.parametrize("branch,ident,want", [
    ("agent/DEV-66", "DEV-66", True),
    ("agent/DEV-66-v2", "DEV-66", True),
    ("agent/DEV-66-android", "DEV-66", True),
    ("agent/DEV-660", "DEV-66", False),
    ("agent/DEV-6", "DEV-66", False),
    ("fix/DEV-66", "DEV-66", False),
    ("agent/DEV-66", "", False),
])
def test_the_branch_rule_is_the_same_as_the_gates_use(branch, ident, want):
    assert branch_names(branch, ident) is want


@pytest.mark.parametrize("origin,want", [
    ("https://github.com/client-b-org/dok-engine.git", "client-b-org/dok-engine"),
    ("git@github.com:client-b-org/dok-engine.git", "client-b-org/dok-engine"),
    ("https://github.com/beskrovnykh/agent-factory", "beskrovnykh/agent-factory"),
    ("https://gitlab.com/grp/backend/core.git", ""),
])
def test_the_repository_name_is_read_from_any_origin_form(origin, want):
    assert repo_of(origin) == want


class Gh(GitHubHosting):
    """`gh` подменён: перечисление PR из заготовки, мерж — запись вызова."""

    def __init__(self, repos, prs, boom=(), merged=None):
        super().__init__(repos_root="/nowhere")
        self._repos, self._prs, self._boom = list(repos), prs, set(boom)
        # Смерженные лежат отдельным списком: `gh pr list` их вместе с
        # открытыми не отдаёт, и фейк, отдающий один список на оба `--state`,
        # проверял бы не то, что бывает.
        self._merged = dict(merged or {})
        self.calls = []

    def repositories(self):
        return list(self._repos)

    def _run(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "list"):
            repo = args[args.index("--repo") + 1]
            if repo in self._boom:
                raise RuntimeError("gh: HTTP 502")
            state = args[args.index("--state") + 1]
            where = self._merged if state == "merged" else self._prs
            return json.dumps(where.get(repo, []))
        return ""


def pr(number, head, base="main", mergeable="MERGEABLE"):
    return {"number": number, "url": f"https://gh/x/pull/{number}", "headRefName": head,
            "baseRefName": base, "title": f"PR {number}", "mergeable": mergeable}


def test_the_open_request_is_found_by_branch_across_the_clones():
    h = Gh(["o/first", "o/second"],
           {"o/first": [pr(1, "agent/DEV-660")],
            "o/second": [pr(7, "fix/typo"), pr(9, "agent/DEV-66-v2", mergeable="CONFLICTING")]})
    got = h.open_request("DEV-66")
    assert got == {"number": "9", "repo": "o/second", "url": "https://gh/x/pull/9",
                   "source_ref": "agent/DEV-66-v2", "target_ref": "main",
                   "title": "PR 9", "mergeable": False}


def test_without_an_open_request_the_answer_is_none():
    assert Gh(["o/r"], {"o/r": [pr(1, "agent/DEV-1")]}).open_request("DEV-2") is None


def test_a_repository_that_does_not_answer_is_skipped_not_fatal(caplog):
    h = Gh(["o/bad", "o/good"], {"o/good": [pr(3, "agent/DEV-5")]}, boom=["o/bad"])
    with caplog.at_level("WARNING", logger="intake"):
        assert h.open_request("DEV-5")["number"] == "3"
    assert any("o/bad" in r.getMessage() for r in caplog.records)


def test_an_unknown_mergeable_word_is_none_not_false():
    h = Gh(["o/r"], {"o/r": [pr(2, "agent/DEV-2", mergeable="UNKNOWN")]})
    assert h.open_request("DEV-2")["mergeable"] is None


# ── список сразу: страница спрашивает на каждую карточку (DEV-120) ───────
#
# Кнопка «Смержить» стояла там, где мержить нечего: карточка не спрашивала
# хостинг вовсе. Теперь спрашивает при отрисовке — а значит вопрос обязан
# стоить один обход репозиториев на всю страницу, а не по обходу на задачу.


def test_the_whole_page_is_one_question_to_the_hosting():
    h = Gh(["o/first", "o/second"],
           {"o/first": [pr(1, "agent/DEV-1")],
            "o/second": [pr(2, "agent/DEV-2-v2"), pr(3, "fix/typo")]})
    got = h.open_requests(["DEV-1", "DEV-2", "DEV-3"])
    assert sorted(got) == ["DEV-1", "DEV-2"]
    assert got["DEV-2"]["number"] == "2" and got["DEV-2"]["repo"] == "o/second"
    assert len([c for c in h.calls if c[:2] == ("pr", "list")]) == 2


def test_the_list_holds_the_same_branch_rule_as_the_single_question():
    """Иначе страница покажет «Смержить» на DEV-6, увидев PR ветки
    `agent/DEV-66`, — то есть ровно ту кнопку не на той задаче."""
    h = Gh(["o/r"], {"o/r": [pr(1, "agent/DEV-66"), pr(2, "fix/DEV-7"),
                             pr(3, "agent/DEV-8-v2")]})
    assert sorted(h.open_requests(["DEV-6", "DEV-7", "DEV-8", "DEV-66"])) == \
        ["DEV-66", "DEV-8"]


def test_a_task_without_a_branch_is_simply_absent():
    """Отсутствие ключа и есть ответ «мержить нечего» — ради него всё и
    затевалось. Пустой словарь вместо ключа читался бы как «PR есть»."""
    h = Gh(["o/r"], {"o/r": [pr(1, "agent/DEV-1")]})
    assert h.open_requests(["DEV-29", "DEV-53"]) == {}


def test_the_list_answers_exactly_what_the_single_question_answers():
    """Два ответа на один вопрос разойтись не должны: страница показывает
    списком тот же PR, который смержит кнопка."""
    h = Gh(["o/first", "o/second"],
           {"o/first": [pr(9, "agent/DEV-66", mergeable="CONFLICTING")],
            "o/second": [pr(10, "agent/DEV-66-v2")]})
    assert h.open_requests(["DEV-66"])["DEV-66"] == h.open_request("DEV-66")


def test_an_empty_list_costs_no_call_at_all():
    h = Gh(["o/r"], {"o/r": [pr(1, "agent/DEV-1")]})
    assert h.open_requests([]) == {} and h.calls == []


def test_a_repository_that_does_not_answer_does_not_hide_the_rest(caplog):
    h = Gh(["o/bad", "o/good"], {"o/good": [pr(3, "agent/DEV-5")]}, boom=["o/bad"])
    with caplog.at_level("WARNING", logger="intake"):
        assert h.open_requests(["DEV-5"])["DEV-5"]["number"] == "3"
    assert any("o/bad" in r.getMessage() for r in caplog.records)


def test_the_merge_targets_exactly_the_request_and_deletes_the_branch():
    h = Gh(["o/r"], {})
    out = h.merge({"number": "9", "repo": "o/second"})
    assert h.calls == [("pr", "merge", "9", "--repo", "o/second", "--merge", "--delete-branch")]
    assert "#9" in out and "o/second" in out and "ветка удалена" in out


def test_a_refused_merge_raises_with_the_reason(monkeypatch):
    import subprocess

    class Out:
        returncode, stdout, stderr = 1, "", "GraphQL: Pull request is not mergeable"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Out())
    with pytest.raises(RuntimeError) as e:
        GitHubHosting("/nowhere").merge({"number": "1", "repo": "o/r"})
    assert "not mergeable" in str(e.value)


def test_the_repositories_come_from_the_clones_origins(tmp_path, monkeypatch):
    h = GitHubHosting(str(tmp_path))
    clones = [str(tmp_path / "dok-engine"), str(tmp_path / "website"), str(tmp_path / "gl")]
    origins = {clones[0]: "git@github.com:client-b-org/dok-engine.git",
               clones[1]: "https://github.com/client-b-org/website",
               clones[2]: "https://gitlab.com/grp/core.git"}
    monkeypatch.setattr(h.repos, "clones", lambda: clones)
    monkeypatch.setattr(h.repos, "_git", lambda repo, *a: (0, origins[repo], ""))
    assert h.repositories() == ["client-b-org/dok-engine", "client-b-org/website"]


def test_the_protocol_is_wired_by_the_same_env_mechanism(monkeypatch):
    from agent_factory.updater import source_from_env
    monkeypatch.setenv("AGENT_HOSTING_SOURCE", "agent_factory.hosting:GitHubHosting")
    monkeypatch.setenv("AGENT_REPOS_ROOT", "/nowhere")
    assert isinstance(source_from_env("AGENT_HOSTING_SOURCE"), hosting.GitHubHosting)


# ── смерженный PR: чем сверка дополняет запись ───────────────────────────
#
# Сверка (`reconcile`) решает по git, а у хостинга спрашивает одно: как этот
# мерж называется у людей — номер PR, ссылка, кто нажал. Спрос маленький, но
# ошибиться в нём значит приписать задаче чужой мерж.

def merged(number, head, base="main", at="2026-09-14T00:48:21Z", by="beskrovnykh"):
    return {"number": number, "url": f"https://gh/x/pull/{number}",
            "headRefName": head, "baseRefName": base, "mergedAt": at,
            "mergedBy": {"login": by}}


def test_the_merged_request_is_found_by_branch_across_the_clones():
    h = Gh(["o/first", "o/second"], {},
           merged={"o/first": [merged(2, "agent/DEV-810")],
                   "o/second": [merged(16, "agent/DEV-81-silent")]})
    assert h.merged_request("DEV-81") == {
        "mr_iid": "16", "project_path": "o/second", "ref": "main",
        "source_ref": "agent/DEV-81-silent", "url": "https://gh/x/pull/16",
        "merged_by": "beskrovnykh", "merged_at": "2026-09-14T00:48:21Z"}


def test_of_two_merged_branches_the_later_merge_is_the_work():
    # У задачи бывает второй PR (`-v2`), и работа задачи — он.
    h = Gh(["o/r"], {}, merged={"o/r": [
        merged(3, "agent/DEV-66", at="2026-09-12T23:10:17Z"),
        merged(22, "agent/DEV-66-v2", at="2026-09-15T20:00:00Z")]})
    assert h.merged_request("DEV-66")["mr_iid"] == "22"


def test_a_merge_into_a_side_branch_is_not_the_work():
    # Влито в `release/1.2` — в основной ветке работы нет.
    h = Gh(["o/r"], {}, merged={"o/r": [merged(5, "agent/DEV-5", base="release/1.2")]})
    assert h.merged_request("DEV-5") is None


def test_a_task_named_only_in_a_title_is_not_claimed():
    # Заголовок и тело тут не смотрим: перебираем сотню чужих PR, и
    # «DEV-81» в чужом заголовке не делает его работой DEV-81.
    h = Gh(["o/r"], {}, merged={"o/r": [dict(merged(9, "fix/typo"),
                                             title="DEV-81: попутно")]})
    assert h.merged_request("DEV-81") is None


def test_nothing_merged_by_that_task_is_none():
    assert Gh(["o/r"], {}, merged={"o/r": [merged(1, "agent/DEV-1")]}) \
        .merged_request("DEV-2") is None


def test_a_repository_that_does_not_answer_does_not_hide_the_others(caplog):
    h = Gh(["o/bad", "o/good"], {}, boom=["o/bad"],
           merged={"o/good": [merged(3, "agent/DEV-5")]})
    with caplog.at_level("WARNING", logger="intake"):
        assert h.merged_request("DEV-5")["mr_iid"] == "3"
    assert any("o/bad" in r.getMessage() for r in caplog.records)
