"""Ветка и коммит на площадке — на настоящем git, во временном каталоге.

Фейком тут не обойтись: смысл модуля в том, как именно он спрашивает
хостинг (`ls-remote` по приставке), дотягивает объекты и считает
предковость. «origin» — голый репозиторий рядом, клон — как у инсталляции.
"""
import subprocess

import pytest

from agent_factory.gitref import Repos, Unavailable

ENV_ARGS = ["-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    out = subprocess.run(["git", *ENV_ARGS, "-C", str(cwd), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def commit(repo, name, msg=None):
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg or name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def site(tmp_path):
    """origin + клон `dok-engine` под корнем, main с одним коммитом."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    root = tmp_path / "repos"
    root.mkdir()
    clone = root / "dok-engine"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "-b", "main")
    commit(clone, "base")
    git(clone, "push", "-q", "-u", "origin", "main")
    git(clone, "remote", "set-head", "origin", "main")
    return root, clone


def branch_with_commit(clone, name, file):
    git(clone, "checkout", "-q", "-b", name, "main")
    sha = commit(clone, file)
    git(clone, "push", "-q", "-u", "origin", name)
    git(clone, "checkout", "-q", "main")
    return sha


def test_an_unmerged_branch_is_found_with_its_head_and_is_not_an_ancestor(site):
    root, clone = site
    sha = branch_with_commit(clone, "agent/DEV-1", "work")
    got = Repos(str(root)).locate("DEV-1")
    assert got.ref == "agent/DEV-1" and got.sha == sha and got.ancestor is False
    assert got.repo == str(clone) and got.when > 0


def test_a_merged_branch_is_an_ancestor(site):
    root, clone = site
    sha = branch_with_commit(clone, "agent/DEV-1", "work")
    git(clone, "merge", "-q", "--no-ff", "-m", "Merge pull request #1 from x/agent/DEV-1",
        "agent/DEV-1")
    git(clone, "push", "-q", "origin", "main")
    got = Repos(str(root)).locate("DEV-1")
    assert got.sha == sha and got.ancestor is True


def test_a_branch_deleted_after_the_merge_is_found_by_the_merge_commit(site):
    root, clone = site
    branch_with_commit(clone, "agent/DEV-1-silent", "work")
    git(clone, "merge", "-q", "--no-ff", "-m",
        "Merge pull request #16 from x/agent/DEV-1-silent", "agent/DEV-1-silent")
    merge = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "push", "-q", "origin", "--delete", "agent/DEV-1-silent")
    got = Repos(str(root)).locate("DEV-1")
    assert got.sha == merge and got.ancestor is True and got.ref == "main"


def test_the_unmerged_suffix_branch_wins_over_the_merged_plain_one(site):
    """`agent/DEV-1` влита, работа продолжилась в `agent/DEV-1-v2`: смотреть
    надо её."""
    root, clone = site
    branch_with_commit(clone, "agent/DEV-1", "work")
    git(clone, "merge", "-q", "--no-ff", "-m", "Merge pull request #1 from x/agent/DEV-1",
        "agent/DEV-1")
    git(clone, "push", "-q", "origin", "main")
    v2 = branch_with_commit(clone, "agent/DEV-1-v2", "more")
    got = Repos(str(root)).locate("DEV-1")
    assert got.ref == "agent/DEV-1-v2" and got.sha == v2 and got.ancestor is False


def test_a_neighbouring_number_is_not_the_same_task(site):
    root, clone = site
    branch_with_commit(clone, "agent/DEV-10", "other")
    assert Repos(str(root)).locate("DEV-1") is None


def test_a_task_without_a_branch_or_a_commit_is_nowhere(site):
    root, _ = site
    assert Repos(str(root)).locate("DEV-9") is None


def test_a_known_commit_is_checked_for_ancestry(site):
    root, clone = site
    on_branch = branch_with_commit(clone, "agent/DEV-1", "work")
    git(clone, "merge", "-q", "--no-ff", "-m", "Merge pull request #1", "agent/DEV-1")
    merge = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "-q", "origin", "main")
    repos = Repos(str(root))
    assert repos.locate("DEV-1", ref="main", sha=merge).ancestor is True
    # Голова ветки тоже влита мержем — предок.
    assert repos.locate("DEV-1", ref="agent/DEV-1", sha=on_branch).ancestor is True


def test_an_unknown_commit_is_nowhere(site):
    root, _ = site
    assert Repos(str(root)).locate("DEV-1", ref="main", sha="0" * 40) is None


def test_a_commit_pushed_but_not_fetched_is_pulled_in(site, tmp_path):
    """Клон отстал от хостинга: ветку запушила рабочая сессия из своего
    каталога. Ворота дотягивают её сами."""
    root, clone = site
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    git(other, "checkout", "-q", "-b", "agent/DEV-1", "origin/main")
    sha = commit(other, "elsewhere")
    git(other, "push", "-q", "-u", "origin", "agent/DEV-1")
    got = Repos(str(root)).locate("DEV-1")
    assert got.sha == sha and got.ancestor is False


def test_the_repo_of_the_event_is_asked_first(site):
    root, clone = site
    branch_with_commit(clone, "agent/DEV-1", "work")
    (root / "other").mkdir()
    git(root, "init", "-q", str(root / "other"))
    repos = Repos(str(root))
    assert repos.ordered("org/dok-engine")[0] == str(clone)
    assert repos.ordered("grp/backend/dok-engine")[0] == str(clone)
    assert set(repos.clones()) == {str(clone), str(root / "other")}


def test_no_clones_is_not_knowing_not_absence(tmp_path):
    with pytest.raises(Unavailable):
        Repos(str(tmp_path / "nowhere")).locate("DEV-1")
    (tmp_path / "empty").mkdir()
    with pytest.raises(Unavailable):
        Repos(str(tmp_path / "empty")).locate("DEV-1")


def test_a_silent_remote_is_not_knowing_not_absence(site):
    root, clone = site
    git(clone, "remote", "set-url", "origin", str(root / "gone.git"))
    with pytest.raises(Unavailable):
        Repos(str(root)).locate("DEV-1")
