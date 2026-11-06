"""Что стережёт ворота на наборы скилов (DEV-13).

Ворота в CI — единственное, что стоит между «скил есть» и «скил проверяем».
Если они проходят вхолостую, набор превращается в папку с текстом, а пайплайн
об этом молчит зелёным.
"""
import pathlib
import textwrap

import pytest

import skilleval


def _case(tmp_path, skill="s", name="c", front="", body="задача", graders=()):
    d = tmp_path / skill / "evals" / name
    (d / "graders").mkdir(parents=True)
    (d / "prompt.md").write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")
    for fname, text in graders:
        (d / "graders" / fname).write_text(textwrap.dedent(text), encoding="utf-8")
    (tmp_path / skill / "SKILL.md").write_text("---\nname: s\n---\n", encoding="utf-8")
    return d


@pytest.fixture
def skills(tmp_path, monkeypatch):
    monkeypatch.setattr(skilleval, "SKILLS_DIR", tmp_path)
    return tmp_path


def _full_set(skills):
    """Минимальный НАБОР, проходящий ворота: вклад + положительный + промах."""
    _case(skills, name="contrib", front="max_turns: 4",
          graders=[("r.md", "---\ntype: regex\npattern: ok\n---\n")])
    _case(skills, name="t-pos", front="max_turns: 2\ntags: [trigger]\nexpect_skill: true",
          graders=[("s.md", "---\ntype: tool_used\ntool: Skill\n---\n")])
    _case(skills, name="t-neg", front="max_turns: 2\ntags: [trigger]\nexpect_skill: false")


def test_a_correct_set_passes(skills):
    _full_set(skills)
    assert skilleval.lint(skilleval.load_cases()) == []


def test_skill_without_any_cases_is_caught(skills):
    _full_set(skills)
    (skills / "lonely").mkdir()
    (skills / "lonely" / "SKILL.md").write_text("---\nname: lonely\n---\n", encoding="utf-8")
    assert any("lonely" in p and "набора кейсов нет" in p
               for p in skilleval.lint(skilleval.load_cases()))


def test_broken_regex_is_caught(skills):
    _full_set(skills)
    _case(skills, name="bad", front="max_turns: 2",
          graders=[("r.md", "---\ntype: regex\npattern: (незакрытая\n---\n")])
    assert any("битое выражение" in p for p in skilleval.lint(skilleval.load_cases()))


def test_unknown_grader_type_is_caught(skills):
    _full_set(skills)
    _case(skills, name="bad", front="max_turns: 2",
          graders=[("g.md", "---\ntype: vibes\n---\n")])
    assert any("vibes" in p for p in skilleval.lint(skilleval.load_cases()))


def test_a_set_without_near_misses_is_caught(skills):
    """Самая важная из ворот. По одним положительным запросам доля
    срабатывания всегда единица — такая проверка не может дать отрицательный
    ответ, то есть не проверяет ничего."""
    _case(skills, name="contrib", front="max_turns: 4",
          graders=[("r.md", "---\ntype: regex\npattern: ok\n---\n")])
    _case(skills, name="t-pos", front="max_turns: 2\ntags: [trigger]\nexpect_skill: true",
          graders=[("s.md", "---\ntype: tool_used\ntool: Skill\n---\n")])
    assert any("близких промахов" in p for p in skilleval.lint(skilleval.load_cases()))


def test_a_set_without_contribution_cases_is_caught(skills):
    _case(skills, name="t-pos", front="max_turns: 2\ntags: [trigger]\nexpect_skill: true",
          graders=[("s.md", "---\ntype: tool_used\ntool: Skill\n---\n")])
    _case(skills, name="t-neg", front="max_turns: 2\ntags: [trigger]\nexpect_skill: false")
    assert any("кейса вклада" in p for p in skilleval.lint(skilleval.load_cases()))


def test_a_positive_trigger_case_without_a_grader_is_caught(skills):
    _full_set(skills)
    _case(skills, name="t-pos-2", front="max_turns: 2\ntags: [trigger]\nexpect_skill: true")
    assert any("без оценщика" in p for p in skilleval.lint(skilleval.load_cases()))


def test_missing_turn_limit_is_caught(skills):
    """Прогон без потолка ходов — это счёт, который никто не закрывал."""
    _full_set(skills)
    _case(skills, name="unbounded", front="tags: [x]",
          graders=[("r.md", "---\ntype: regex\npattern: ok\n---\n")])
    assert any("max_turns" in p for p in skilleval.lint(skilleval.load_cases()))


def test_holdout_split_is_stable_across_runs(skills):
    """Граница обучающей и проверочной частей фиксирована: случайная давала бы
    новое разбиение на каждый прогон, и «стало лучше» нельзя было бы отличить
    от «повезло с разбиением»."""
    _full_set(skills)
    first = {c.name: c.holdout for c in skilleval.load_cases()}
    second = {c.name: c.holdout for c in skilleval.load_cases()}
    assert first == second and set(first.values()) <= {True, False}


def test_grading_counts_only_what_actually_happened(skills):
    case = skilleval.Case(skill="s", name="c", path=skills, meta={}, body="b", graders=[
        skilleval.Grader(skills / "a.md", {"type": "regex", "pattern": r"\[LOGIC\]"}),
        skilleval.Grader(skills / "b.md", {"type": "tool_used", "tool": "Skill"}),
    ])
    hit = {"text": "нашёл [LOGIC] ошибку", "tools": ["Skill"], "cost": 0.0, "error": ""}
    miss = {"text": "выглядит нормально", "tools": ["Read"], "cost": 0.0, "error": ""}
    assert skilleval._grade(case, hit) == (2, 2)
    assert skilleval._grade(case, miss) == (0, 2)


def test_a_broken_run_is_not_reported_as_zero_contribution(skills, capsys):
    """Сорванный прогон и «скил не помог» дают одинаковый ноль баллов.

    Считать их одним — значит записать поломку обвязки в вывод про скил.
    Так и вышло на первом прогоне DEV-13: ключи искались не по тому пути, все
    прогоны падали, и таблица показала честный на вид «вклад +0.00».
    """
    _full_set(skills)
    cases = skilleval.load_cases()
    results = {(c.skill, c.name, arm): [{"score": 0.0, "fired": False, "cost": 0.0,
                                         "error": "Not logged in"}]
               for c in cases for arm in ("со скилом", "без скила")}
    assert skilleval.report(cases, results) == 1
    assert "сорвано прогонов" in capsys.readouterr().out


def test_a_truncated_run_still_counts_as_an_observation(skills, monkeypatch):
    """Ход, упершийся в `max_turns`, и обрыв по сроку возвращают ненулевой код,
    но поток к этому моменту уже показал вызов скила. Для кейса срабатывания
    предмет замера — ровно этот вызов. Выбросив такой прогон, обвязка запишет
    скилу «не поднялся» там, где он поднялся и не успел договорить."""
    import json as _json
    import subprocess as _sp

    stream = "\n".join(_json.dumps(m) for m in [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "s:s"}}]}},
    ])

    def fake_run(*a, **kw):
        raise _sp.TimeoutExpired(cmd="claude", timeout=1, output=stream)

    monkeypatch.setattr(skilleval.subprocess, "run", fake_run)
    case = skilleval.Case(skill="s", name="c", path=skills, meta={}, body="b")
    out = skilleval._run_once(case, None, skills, timeout=1)
    assert out["tools"] == ["Skill"], "вызов скила потерян при обрыве"
    assert not out["error"], "оборванный, но говорящий прогон помечен ошибкой"
    assert out["truncated"] is True


# ── находки собственного код-ревью (DEV-13) ─────────────────────────────────

def test_a_bracketed_regex_is_not_swallowed_as_a_list():
    """`pattern: [LOGIC]` разбирался как список, а `str(['LOGIC'])` — рабочее
    регулярное выражение (класс символов), совпадающее почти с чем угодно.

    Оценщик при этом не падал и линтером не ловился: он ВСЕГДА проходил,
    завышая измеренный вклад скила. Ошибка в сторону «всё хорошо» — худшая из
    возможных для обвязки, которая существует ради честного числа.
    """
    meta, _ = skilleval._frontmatter("---\ntype: regex\npattern: [LOGIC]\n---\n\nx\n")
    assert meta["pattern"] == "[LOGIC]", "выражение снова разобрано как список"


def test_list_valued_keys_are_still_lists():
    meta, _ = skilleval._frontmatter("---\ntags: [trigger, slow]\n---\n\nx\n")
    assert meta["tags"] == ["trigger", "slow"]


def test_a_case_can_ask_for_the_tools_it_needs(skills, monkeypatch):
    """Скил, вся работа которого в запуске команды, без `Bash` был бы измерен
    как бесполезный — обвязка соврала бы про него в сторону «не нужен»."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        raise skilleval.subprocess.TimeoutExpired(cmd="claude", timeout=1, output="")

    monkeypatch.setattr(skilleval.subprocess, "run", fake_run)
    case = skilleval.Case(skill="s", name="c", path=skills,
                          meta={"allowed_tools": "Bash,Read,Skill"}, body="b")
    skilleval._run_once(case, None, skills, timeout=1)
    assert "Bash,Read,Skill" in seen["cmd"], "кейс не получил запрошенных инструментов"
    default_case = skilleval.Case(skill="s", name="c", path=skills, meta={}, body="b")
    skilleval._run_once(default_case, None, skills, timeout=1)
    assert skilleval.DEFAULT_TOOLS in seen["cmd"], "умолчание прав потеряно"
    assert "Bash" not in skilleval.DEFAULT_TOOLS, "кейс получает права, которых не просил"


def test_the_copy_of_credentials_is_removed_even_when_the_run_blows_up(skills, monkeypatch):
    """В рабочем каталоге прогона лежит КОПИЯ ключей, а `/tmp` на хосте
    инсталляции общий для всех сессий сразу. Оборванный прогон обязан унести
    её с собой."""
    made = []
    real_mkdtemp = skilleval.tempfile.mkdtemp
    monkeypatch.setattr(skilleval.tempfile, "mkdtemp",
                        lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])
    monkeypatch.setattr(skilleval.Path, "home", staticmethod(lambda: skills))
    (skills / ".claude").mkdir(parents=True, exist_ok=True)
    (skills / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(skilleval, "_run_all",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("оборвалось")))
    with pytest.raises(RuntimeError):
        skilleval.run([], runs=1, timeout=1, workers=1)
    assert made, "временный каталог не создавался — проверка вхолостую"
    left = [d for d in made if pathlib.Path(d).exists()]
    assert not left, f"копия ключей осталась в /tmp: {left}"


def test_a_skill_firing_on_someone_elses_request_fails_the_run(skills, capsys):
    """Код выхода прогона — это и есть ворота. Скил, поднимающийся на близком
    промахе, раньше давал зелёный прогон."""
    _case(skills, name="contrib", front="max_turns: 4",
          graders=[("r.md", "---\ntype: regex\npattern: ok\n---\n")])
    _case(skills, name="t-neg", front="max_turns: 2\ntags: [trigger]\nexpect_skill: false")
    cases = skilleval.load_cases()
    good = {"score": 1.0, "fired": False, "cost": 0.0, "error": ""}
    results = {("s", "contrib", "со скилом"): [dict(good)],
               ("s", "contrib", "без скила"): [dict(good, score=0.0)],
               ("s", "t-neg", "со скилом"): [dict(good, fired=True)]}
    assert skilleval.report(cases, results) == 1
    assert "мимо: t-neg" in capsys.readouterr().out
