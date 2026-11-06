"""Две точки проверки — два независимых выключателя (DEV-97).

Проверка ставится в два момента: ДО мержа — ветка на dev-стенде, и ПОСЛЕ
мержа — выкат на прод. Владельцу нужен только первый: после мержа он проверяет
сам и токены на это не тратит. Второй остаётся в коде и под тестами —
выключается только его запуск, переменной, без выката.

Каждый тест — строка критерия приёмки задачи: выключенная точка сессию в
очередь не ставит вовсе; включённая — ставит как раньше; вебхук выката при
выключенной точке отвечает 200 и пишет причину; обратное включение работает;
обе выключены — слышно. Точка — по РЕЖИМУ, который выбрали ворота (DEV-86), а
не по тому, кто позвал.
"""
import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from agent_factory import deploy
from agent_factory.deploy import (POINT_AFTER, POINT_BEFORE, VERIFY_AFTER_MERGE_VAR,
                                  VERIFY_BEFORE_MERGE_VAR, DeployReceiver, Verification,
                                  point_of, switch_of, switch_on, verify_points)
from agent_factory.gitref import Located
from prompt import build_verify_prompt

Q = "agent:queue:x"
SHA = "5176388a4ed8d96c358b2d0af9f6b285bbaa9730"


class Store:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.claimed: set[str] = set()

    async def put(self, key, value, ttl): self.kv[key] = value
    async def get(self, key): return self.kv.get(key)
    async def drop(self, key):
        self.kv.pop(key, None); self.claimed.discard(key)

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key); return True

    async def enqueue(self, queue, element):
        self.lists.setdefault(queue, []).append(element)


class Repos:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def locate(self, ident, ref="", sha="", project_path=""):
        self.asked.append(ident); return self.answer


class Hosting:
    def __init__(self, answer):
        self.answer = answer

    def landed(self, payload):
        return self.answer


class Tracker:
    def __init__(self):
        self.asked = []

    def __call__(self, ident):
        self.asked.append(ident); return ISSUE


ISSUE = {"id": "uuid-20", "identifier": "DEV-20", "title": "Уведомления",
         "url": "https://linear.app/x/DEV-20",
         "description": "## Что сделать\n\nвынести\n\n## Критерий приёмки\n\n| что |"}

LANDED = {"identifier": "DEV-20", "commit_sha": SHA, "ref": "main",
          "project_path": "org/dok-engine", "deploy_jobs": ["deploy"],
          "pipeline_id": "1", "pipeline_url": "https://gh/run/1"}


def merged():
    return Located(repo="/r/dok-engine", ref="main", sha=SHA, ancestor=True, when=1)


def unmerged():
    return Located(repo="/r/dok-engine", ref="agent/DEV-20", sha=SHA, ancestor=False, when=1)


def event(updated="2026-09-15T10:00:00.000Z"):
    return {"type": "Issue", "action": "update", "updatedFrom": {"stateId": "было"},
            "data": {"id": "uuid-20", "identifier": "DEV-20", "title": "Уведомления",
                     "url": "https://linear.app/x/DEV-20", "updatedAt": updated,
                     "state": {"name": "Verifying", "type": "started"}}}


def mk(tmp_path, *, before=True, after=True, located, landed=LANDED, store=None):
    st = store or Store()
    tracker, repos = Tracker(), Repos(located)
    v = Verification(st, Q, Hosting(landed), lookup=tracker, repos=repos,
                     work_root=str(tmp_path),
                     points={POINT_BEFORE: before, POINT_AFTER: after})
    v.tracker, v.repos_fake = tracker, repos
    return st, v


def queued(st):
    return [json.loads(x) for x in st.lists.get(Q, [])]


def run(coro):
    return asyncio.run(coro)


def skip_records(tmp_path):
    return [p for p in Path(tmp_path).iterdir() if (p / "verdict.txt").exists()]


@pytest.fixture
def said(monkeypatch):
    """Что приём написал в журнал. Не `caplog`: `test_telegram.py` подменяет
    `info` у логгера «intake» насовсем, и после него `caplog` слеп — а этот
    файл по алфавиту идёт следом."""
    lines = []
    monkeypatch.setattr(deploy.log, "info", lambda m, *a: lines.append(m % a))
    return lines


# ── после мержа выключена: сессия не ставится вовсе ─────────────────────────

def test_after_merge_off_the_deploy_event_queues_nothing(tmp_path, said):
    """Строка критерия: «после мержа сессия не ставится» — в очереди пусто, в
    журнале причина. Не «ставит и снимает»: в очередь не попадает ничего."""
    st, v = mk(tmp_path, before=True, after=False, located=merged())
    assert run(v.on_event({})) == "disabled"
    assert queued(st) == []
    line = next(x for x in said if "по настройке" in x)
    assert POINT_AFTER in line and f"{VERIFY_AFTER_MERGE_VAR}=0" in line
    # Это настройка, а не отсев по фактам: записи CANCELLED в журнале запусков
    # и отметки ⏭ в сообщении задачи нет — иначе каждый выкат рисовал бы её.
    assert skip_records(tmp_path) == []


def test_after_merge_off_leaves_no_marks_behind(tmp_path):
    """Метки перехода и коммита сняты: выключатель не должен запирать проверку
    этого коммита на 30 суток — включат переменной, и повторный пайплайн
    обязан поставить сессию."""
    st, v = mk(tmp_path, before=True, after=False, located=merged())
    run(v.on_event({}))
    assert st.claimed == set()


# ── до мержа включена: ставится как раньше ───────────────────────────────────

def test_before_merge_on_the_state_transition_queues_as_before(tmp_path):
    """Строка критерия: «до мержа ставится как раньше» — сессия в очереди,
    режим ветки. Выключенная вторая точка первой не мешает."""
    st, v = mk(tmp_path, before=True, after=False, located=unmerged())
    assert run(v.on_state(event())) == "queued"
    (task,) = queued(st)
    assert task["trigger"] == "branch" and task["ref"] == "agent/DEV-20"
    assert "ДО МЕРЖА" in build_verify_prompt(task, Path("/w"), Path("/r"))


# ── вебхук не ломается ───────────────────────────────────────────────────────

def _signed(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_the_deploy_webhook_answers_200_and_names_the_reason_when_off(tmp_path, said):
    """Строка критерия: «вебхук не ломается» — 200 и запись в журнал. 500 или
    молчание значили бы, что хостинг начнёт ретраить доставку, которую мы
    сами решили не обрабатывать."""
    st, v = mk(tmp_path, before=True, after=False, located=merged())
    r = DeployReceiver(v, secret="s3cr3t")
    body = json.dumps({"ref": "main"}).encode()
    code, out = run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 200 and out == {"ok": True, "verify": "disabled"}
    assert any("по настройке" in x for x in said)
    assert queued(st) == []


# ── обратное включение ───────────────────────────────────────────────────────

def test_after_merge_on_the_deploy_event_queues_the_deploy_check(tmp_path):
    """Строка критерия: «обратное включение работает» — сессия встаёт, режим
    выката. Тот же код, тот же контракт `GOAL_DEPLOY`."""
    st, v = mk(tmp_path, before=True, after=True, located=merged())
    assert run(v.on_event({})) == "queued"
    (task,) = queued(st)
    assert task["trigger"] == "deploy" and task["commit_sha"] == SHA
    assert "ПОСЛЕ МЕРЖА" in build_verify_prompt(task, Path("/w"), Path("/r"))


def test_a_commit_refused_while_off_is_checked_once_the_point_is_on(tmp_path):
    """Выключатель не односторонний: коммит, отброшенный при выключенной точке,
    после включения проверяется — а не получает «deduped» по оставленной
    метке. Один и тот же стор, тот же выкат, переменная поменялась."""
    st, v_off = mk(tmp_path, after=False, located=merged())
    assert run(v_off.on_event({})) == "disabled"
    _, v_on = mk(tmp_path, after=True, located=merged(), store=st)
    assert run(v_on.on_event({})) == "queued"
    assert len(queued(st)) == 1


def test_the_switch_is_read_from_the_environment(tmp_path, monkeypatch):
    """Включается переменной, без выката: приём читает окружение при создании,
    и никакого другого рубильника нет."""
    monkeypatch.setenv(VERIFY_BEFORE_MERGE_VAR, "1")
    monkeypatch.setenv(VERIFY_AFTER_MERGE_VAR, "0")
    st = Store()
    v = Verification(st, Q, Hosting(LANDED), lookup=Tracker(), repos=Repos(merged()),
                     work_root=str(tmp_path))
    assert v.points == {POINT_BEFORE: True, POINT_AFTER: False}
    assert run(v.on_event({})) == "disabled"
    monkeypatch.setenv(VERIFY_AFTER_MERGE_VAR, "1")
    v = Verification(st, Q, Hosting(LANDED), lookup=Tracker(), repos=Repos(merged()),
                     work_root=str(tmp_path))
    assert run(v.on_event({})) == "queued"


# ── точка — по режиму, не по входу ───────────────────────────────────────────

def test_a_state_transition_on_a_merged_commit_is_the_after_merge_point(tmp_path):
    """Перевод по статусу на уже влитом коммите — проверка ПОСЛЕ мержа
    (DEV-86: режим по предковости). Выключатель «после мержа» обязан её
    остановить, иначе он экономит не там, где обещал."""
    st, v = mk(tmp_path, before=True, after=False, located=merged())
    assert run(v.on_state(event())) == "disabled"
    assert queued(st) == [] and st.claimed == set()


def test_a_deploy_event_on_an_unmerged_commit_is_the_before_merge_point(tmp_path):
    """Событие хостинга о зелёном прогоне на ветке — проверка ДО мержа: при
    выключенной второй точке она ставится, и в режиме ветки."""
    st, v = mk(tmp_path, before=True, after=False, located=unmerged(),
               landed={**LANDED, "trigger": "branch", "deploy_jobs": []})
    assert run(v.on_event({})) == "queued"
    assert queued(st)[0]["trigger"] == "branch"


def test_before_merge_off_alone_stops_only_the_branch_check(tmp_path):
    """Выключатели независимы: первая выключена, вторая включена — ветка не
    проверяется, выкат проверяется."""
    st, v = mk(tmp_path, before=False, after=True, located=unmerged())
    assert run(v.on_state(event())) == "disabled"
    st2, v2 = mk(tmp_path, before=False, after=True, located=merged())
    assert run(v2.on_event({})) == "queued"


def test_the_point_of_each_mode():
    assert point_of("branch") == POINT_BEFORE
    for mode in ("state", "build", "deploy"):
        assert point_of(mode) == POINT_AFTER
    assert switch_of(POINT_BEFORE) == VERIFY_BEFORE_MERGE_VAR
    assert switch_of(POINT_AFTER) == VERIFY_AFTER_MERGE_VAR


# ── обе выключены ────────────────────────────────────────────────────────────

def test_with_both_points_off_nothing_is_queued_and_no_one_is_asked(tmp_path, said):
    """Ставить некуда — ворота не зовутся: ни трекер, ни git, ни метки. В
    журнале при этом строка на каждый вход: фабрика без проверки не должна
    быть тихой."""
    st, v = mk(tmp_path, before=False, after=False, located=merged())
    assert run(v.on_state(event())) == "disabled"
    assert run(v.on_event({})) == "disabled"
    assert queued(st) == [] and st.claimed == set()
    assert v.tracker.asked == [] and v.repos_fake.asked == []
    lines = [x for x in said if "выключены обе" in x]
    assert len(lines) == 2
    assert all(VERIFY_BEFORE_MERGE_VAR in x and VERIFY_AFTER_MERGE_VAR in x for x in lines)


def test_with_both_points_off_a_foreign_event_is_still_not_ours(tmp_path):
    # «Не наш выкат» остаётся «не наш» — выключатель не подменяет разбор.
    st, v = mk(tmp_path, before=False, after=False, located=merged(), landed=None)
    assert run(v.on_event({})) == "not-ours"


# ── значения ─────────────────────────────────────────────────────────────────

def test_unset_means_on_as_before():
    assert verify_points({}) == {POINT_BEFORE: True, POINT_AFTER: True}


@pytest.mark.parametrize("raw", ["0", " 0 ", "false", "No", "off"])
def test_zero_means_off(raw):
    assert switch_on(raw) is False


@pytest.mark.parametrize("raw", ["1", "", None, "yes", "true"])
def test_anything_but_a_deliberate_no_means_on(raw):
    assert switch_on(raw) is True


def test_the_agreed_layout_reads_as_intended():
    """Раскладка обеих инсталляций после правки: до мержа 1, после мержа 0."""
    env = {VERIFY_BEFORE_MERGE_VAR: "1", VERIFY_AFTER_MERGE_VAR: "0"}
    assert verify_points(env) == {POINT_BEFORE: True, POINT_AFTER: False}


# ── код второй точки цел ─────────────────────────────────────────────────────

def test_the_after_merge_contract_is_intact_behind_the_switch():
    """Выключается запуск, а не код: контракт после мержа со всеми его частями
    на месте и собирается."""
    import prompt
    for name in ("GOAL_DEPLOY", "FACTS_DEPLOY", "RETRY_STEP_DEPLOY", "OUTCOMES_DEPLOY"):
        assert getattr(prompt, name)
    text = build_verify_prompt({"identifier": "DEV-20", "kind": "verify", "trigger": "deploy",
                                "commit_sha": SHA, "ref": "main"}, Path("/w"), Path("/r"))
    assert "ПОСЛЕ МЕРЖА" in text
    assert deploy.Verification.on_event is not None
