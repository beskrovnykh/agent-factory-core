"""Дифф сессии — по ветке задачи и коммитам на ней (DEV-121).

Проверяется на настоящих репозиториях, а не на подменах `subprocess`: вопрос
здесь ровно про то, что скажет git, и подмена git проверяла бы подмену. Цена —
несколько `git init` в tmp_path, это десятки миллисекунд.
"""
import subprocess

import pytest

import codetrace


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True, timeout=60)


@pytest.fixture
def repos(tmp_path):
    """Каталог репозиториев с одним клоном: `origin` — настоящий, локальный,
    чтобы у клона был `origin/HEAD` и `origin/main`, как у живых копий."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    git(upstream, "config", "user.email", "t@t")
    git(upstream, "config", "user.name", "t")
    (upstream / "README").write_text("основа\n")
    git(upstream, "add", "README")
    git(upstream, "commit", "-qm", "основа")

    root = tmp_path / "repos"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", str(upstream), str(root / "dok-engine")],
                   check=True, capture_output=True, text=True, timeout=120)
    clone = root / "dok-engine"
    git(clone, "config", "user.email", "t@t")
    git(clone, "config", "user.name", "t")
    return root, clone


def commit_on(clone, branch, name="правка"):
    git(clone, "checkout", "-q", "-b", branch, "origin/main")
    (clone / name).write_text("код\n")
    git(clone, "add", name)
    git(clone, "commit", "-qm", name)
    git(clone, "checkout", "-q", "main")


def test_a_branch_with_a_commit_is_a_diff_and_gets_named(repos):
    """Строка-след не только отвечает «да», но и называет, где смотреть:
    человек проверяет дифф сам, а не верит раннеру на слово."""
    root, clone = repos
    commit_on(clone, "agent/DEV-121")
    trace = codetrace.code_trace("DEV-121", root)
    assert "dok-engine" in trace and "agent/DEV-121" in trace
    assert "origin/main" in trace and "1" in trace


def test_an_empty_branch_is_not_a_diff(repos):
    """Исход 4 контракта: ветку сессия завела (шаг 3), а кода не написала —
    ревьюить на такой ветке нечего буквально, `/code-review` получил бы её
    пустой."""
    root, clone = repos
    git(clone, "branch", "agent/DEV-21", "origin/main")
    assert codetrace.code_trace("DEV-21", root) == ""


def test_no_branch_at_all_is_not_a_diff(repos):
    """Так кончились DEV-21 и DEV-119: ни ветки, ни коммита."""
    root, _ = repos
    assert codetrace.code_trace("DEV-119", root) == ""


def test_a_longer_key_does_not_borrow_the_shorter_ones_diff(repos):
    """`DEV-12` и `DEV-121` — разные задачи. Без разделителя после имени
    задачи ветка `agent/DEV-121` приписалась бы задаче `DEV-12`, и сессия без
    диффа получила бы замечание за чужую работу."""
    root, clone = repos
    commit_on(clone, "agent/DEV-121")
    assert codetrace.code_trace("DEV-12", root) == ""
    assert codetrace.code_trace("DEV-121", root)


def test_the_fallback_branch_names_count_too(repos):
    """Запасные имена шага 3 (`-v2`, `-v3`) и имя, предложенное трекером."""
    root, clone = repos
    commit_on(clone, "agent/DEV-44-v2")
    assert "agent/DEV-44-v2" in codetrace.code_trace("DEV-44", root)
    commit_on(clone, "engineer/dev-55-revyu-ne-sostoyalos", name="ещё")
    assert "dev-55" in codetrace.code_trace("DEV-55", root)


def test_a_merged_branch_stops_looking_like_a_diff(repos):
    """Граница ответа, названная в модуле: после мержа коммиты ветки видны из
    базовой, и задним числом сессия выглядит как сессия без диффа. Раннер
    спрашивает на выходе сессии, когда ветка ещё своя; разбору старых трасс
    этот ответ брать нельзя."""
    root, clone = repos
    commit_on(clone, "agent/DEV-66")
    assert codetrace.code_trace("DEV-66", root)
    git(clone, "checkout", "-q", "main")
    git(clone, "merge", "-q", "--no-edit", "agent/DEV-66")
    # Мерж «на форже» — это ровно то, что клон увидит следующим fetch: базовая
    # ветка origin переезжает на коммит, из которого ветка уже достижима.
    git(clone, "update-ref", "refs/remotes/origin/main", "main")
    assert codetrace.code_trace("DEV-66", root) == ""


def test_a_repo_without_origin_head_still_answers(repos):
    """Реф `origin/HEAD` в клоне может быть не выставлен — тогда база
    подбирается по обычным именам, а не считается `main` по умолчанию."""
    root, clone = repos
    commit_on(clone, "agent/DEV-77")
    git(clone, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    assert codetrace.base_ref(clone) == "origin/main"
    assert "agent/DEV-77" in codetrace.code_trace("DEV-77", root)


def test_an_unanswerable_question_is_not_a_no(tmp_path, repos):
    """Три причины не знать ответа — и все три дают `None`, а не «диффа нет»:
    молчание на неизвестности выдало бы непроверенный дифф за проверенный."""
    root, _ = repos
    assert codetrace.code_trace("не-задача", root) is None
    assert codetrace.code_trace("", root) is None
    assert codetrace.code_trace("DEV-1", tmp_path / "нет-такого") is None
    empty = tmp_path / "пусто"
    empty.mkdir()
    assert codetrace.code_trace("DEV-1", empty) is None


def test_a_base_branch_that_cannot_be_named_is_unknown_not_absent(tmp_path):
    """Репозиторий без `origin` вовсе: базу назвать нечем, число коммитов
    поверх неё посчитать нельзя — значит ответа нет."""
    root = tmp_path / "repos"
    (root / "своё").mkdir(parents=True)
    solo = root / "своё"
    git(solo, "init", "-q", "-b", "main")
    git(solo, "config", "user.email", "t@t")
    git(solo, "config", "user.name", "t")
    (solo / "f").write_text("x\n")
    git(solo, "add", "f")
    git(solo, "commit", "-qm", "первый")
    git(solo, "checkout", "-q", "-b", "agent/DEV-88")
    assert codetrace.base_ref(solo) == ""
    assert codetrace.code_trace("DEV-88", root) is None

