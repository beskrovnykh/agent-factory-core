"""Ожидание CI как состояние раннера (DEV-16).

Счастливый путь тут — не достижение. Достижение — ожидание, которое
переживает всё и никогда не молчит. Поэтому набор построен по таблице
отказов из постановки: каждая строка — отдельный тест на ВНЕСЁННУЮ
неисправность (убитый посреди шага раннер, потерянный ключ, пайплайн,
который не кончается, закрытый MR), а не на прогон по-хорошему.

Мир подменён целиком: redis — словарём с выключателем, форж — программируемым
ответчиком, трекер и канал — счётчиками. `run_session` и подметальщик зовутся
настоящие.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import redis as redis_lib

import ciwait
import handoff_lint
import runner
from agent_factory import runs
from prompt import build_prompt
from test_runner import (FakeProc, HEADLINE, TASK, session,  # noqa: F401 — фикстура
                         stats_of)

T0 = 1_800_000_000.0          # условное «сейчас», секунды
MIN = 60.0
SHA = "abc123def456"
REQ = {"forge": "github", "repo": "org/app", "branch": "agent/TASK-200",
       "sha": SHA, "mr": "https://github.com/org/app/pull/7"}


# ── мир ─────────────────────────────────────────────────────────────────────

class FakeRedis:
    """Redis в памяти: ZSET расписания, HASH записей, список очереди. Умеет
    ложиться (`broken`) и терять ключи (`flush`) — ровно то, что ломаем."""

    def __init__(self):
        self.z: dict[str, dict[str, float]] = {}
        self.h: dict[str, dict[str, str]] = {}
        self.l: dict[str, list[str]] = {}
        self.broken = False

    def _ok(self):
        if self.broken:
            raise redis_lib.RedisError("redis лежит")

    def zadd(self, key, mapping):
        self._ok(); self.z.setdefault(key, {}).update(mapping); return len(mapping)

    def zrangebyscore(self, key, lo, hi):
        self._ok()
        lo = float("-inf") if lo == "-inf" else float(lo)
        hi = float("inf") if hi == "+inf" else float(hi)
        return [m for m, s in sorted(self.z.get(key, {}).items(), key=lambda i: i[1])
                if lo <= s <= hi]

    def zrange(self, key, lo, hi):
        self._ok(); return list(self.z.get(key, {}))

    def zrem(self, key, member):
        self._ok(); return 1 if self.z.get(key, {}).pop(member, None) is not None else 0

    def hset(self, key, field, value):
        self._ok(); self.h.setdefault(key, {})[field] = value; return 1

    def hget(self, key, field):
        self._ok(); return self.h.get(key, {}).get(field)

    def hdel(self, key, field):
        self._ok(); return 1 if self.h.get(key, {}).pop(field, None) is not None else 0

    def hgetall(self, key):
        self._ok(); return dict(self.h.get(key, {}))

    def lpush(self, key, value):
        self._ok(); self.l.setdefault(key, []).insert(0, value); return 1

    def lrange(self, key, lo, hi):
        self._ok(); return list(self.l.get(key, []))

    def flush(self):
        """Хост перезагружен, снимка не было: всё потеряно."""
        self.z.clear(); self.h.clear(); self.l.clear()

    # удобства для утверждений
    @property
    def waiting(self) -> dict[str, dict]:
        return {k: json.loads(v) for k, v in self.h.get(runner.CI_WAIT_ENTRIES_KEY, {}).items()}

    @property
    def scheduled(self) -> dict[str, float]:
        return dict(self.z.get(runner.CI_WAIT_KEY, {}))

    @property
    def queue(self) -> list[dict]:
        return [json.loads(v) for v in self.l.get(runner.QUEUE_KEY, [])]


class FakeForge:
    """Ответчик за `gh`/`glab`: что прогоны, что MR — по состоянию полей."""

    def __init__(self):
        self.runs: list[dict] = []        # ответ на опрос прогонов
        self.mr = "OPEN"                  # OPEN / CLOSED / MERGED
        self.down = False                 # CLI не отвечает вовсе
        self.rc = 0                       # код возврата опроса прогонов
        self.stderr = ""
        self.branch_gone = False
        self.calls: list[list[str]] = []
        self.on_poll = None               # хук: что делать во время опроса

    def run(self, status="in_progress", conclusion="", sha=SHA, name="test", rid=1):
        return {"status": status, "conclusion": conclusion, "headSha": sha,
                "workflowName": name, "url": f"https://x/{rid}", "databaseId": rid}

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.down:
            return None, "", "", f"{cmd[0]} не ответил за 60с"
        if cmd[:2] == ["gh", "run"]:
            if self.on_poll:
                self.on_poll()
            if self.rc:
                return self.rc, "", self.stderr, ""
            return 0, json.dumps(self.runs), "", ""
        if cmd[:2] == ["gh", "pr"]:
            if self.mr == "GONE":
                return 1, "", "GraphQL: Could not resolve to a PullRequest with the number of 7.", ""
            return 0, json.dumps({"state": self.mr,
                                  "mergedAt": "2026-09-15T10:00:00Z" if self.mr == "MERGED" else None,
                                  "headRefName": "agent/TASK-200"}), "", ""
        if cmd[:2] == ["gh", "api"]:
            if self.branch_gone:
                return 1, "", "gh: Branch not found (HTTP 404)", ""
            return 0, json.dumps({"name": "agent/TASK-200"}), "", ""
        raise AssertionError(f"неожиданная команда {cmd}")


@pytest.fixture
def world(monkeypatch, tmp_path):
    fake, forge = FakeRedis(), FakeForge()
    calls = {"comments": [], "notify": [], "returned": [], "patched": [],
             "released": [], "repairs": []}
    monkeypatch.setattr(runner, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(runner.redis, "from_url", lambda url, **kw: fake)
    monkeypatch.setattr(runner, "_ci_run", forge)
    monkeypatch.setattr(runner, "linear_comment",
                        lambda i, b, persona="": calls["comments"].append(b))
    monkeypatch.setattr(runner, "notify", lambda t, chat_id="": calls["notify"].append(t))
    monkeypatch.setattr(runner, "linear_assign_to_reviewer",
                        lambda i, rev=None: calls["returned"].append(i))
    monkeypatch.setattr(runner, "current_reviewer",
                        lambda: {"id": "rev", "name": "Андрей", "telegram_id": "",
                                 "until": "", "delegated": False})
    monkeypatch.setattr(runner, "release_worktrees", lambda w: calls["released"].append(w))
    monkeypatch.setattr(runner, "_session_id", lambda w: "sid-1")
    monkeypatch.setattr(runner, "repair_handoff",
                        lambda *a, **k: calls["repairs"].append(a[6]))
    snapshot = {"state": {"name": "In Progress", "type": "started"},
                "assignee": {"id": "agent"},
                "team": {"states": {"nodes": [{"id": "ST-V", "name": runner.VERIFYING_STATE_NAME},
                                              {"id": "ST-R", "name": runner.REVIEW_STATE_NAME}]}},
                "comments": {"nodes": [{"createdAt": "2099-01-01T00:00:00Z", "body": "отчёт"}]}}
    monkeypatch.setattr(runner, "_handoff_state", lambda i: dict(snapshot))
    monkeypatch.setattr(runner._tracker, "update_issue",
                        lambda i, f: calls["patched"].append(f))
    # Схема сдачи (DEV-95) в этих тестах сходится, если тест не решил иначе.
    monkeypatch.setattr(runner, "handoff_misses", lambda *a, **k: [])
    return {"redis": fake, "forge": forge, "calls": calls, "root": tmp_path,
            "snapshot": snapshot}


def enter(world, req=None, now=T0, task=None, cut_off=False) -> tuple[dict, Path]:
    """Сессия отдала ожидание раннеру. Возвращает (запись, рабочий каталог)."""
    task = task or dict(TASK)
    workdir = world["root"] / f"{task['identifier']}-20260915-100000"
    workdir.mkdir(exist_ok=True)
    (workdir / "transcript.jsonl").write_text('{"session_id":"sid-1"}\n')
    ok = runner._ci_enter(task, workdir, workdir / "session.log", "",
                          dict(req or REQ), cut_off=cut_off, now=now,
                          issue=dict(world["snapshot"]),
                          since=datetime.fromtimestamp(now - 30 * MIN, timezone.utc))
    assert ok
    return world["redis"].waiting.get(task["identifier"]) or {}, workdir


def state_of(workdir: Path) -> dict:
    return json.loads((workdir / ciwait.STATE_FILE).read_text())


def tick(world, now) -> int:
    return runner._ci_wait_tick(world["redis"], now)


def human(world) -> list[str]:
    return world["calls"]["returned"]


# ── чистая логика ───────────────────────────────────────────────────────────

def test_only_listed_conclusions_are_green():
    """Список зелёных, не красных: незнакомый исход форжа — падение, а
    `cancelled` — тоже: отменённый прогон ничего не доказывает."""
    ok = json.dumps([{"status": "completed", "conclusion": "success", "headSha": SHA,
                      "workflowName": "t", "databaseId": 1}])
    assert ciwait.parse_runs("github", ok, SHA)[0] == ciwait.PASSED
    for concl in ("failure", "cancelled", "timed_out", "startup_failure", "чушь"):
        bad = json.dumps([{"status": "completed", "conclusion": concl, "headSha": SHA,
                           "workflowName": "t", "databaseId": 1}])
        assert ciwait.parse_runs("github", bad, SHA)[0] == ciwait.FAILED, concl
    skipped = json.dumps([{"status": "completed", "conclusion": "skipped", "headSha": SHA,
                           "workflowName": "t", "databaseId": 1}])
    assert ciwait.parse_runs("github", skipped, SHA)[0] == ciwait.PASSED


def test_a_green_run_of_a_previous_push_does_not_count():
    """На ветке висит зелёный прогон ПРЕДЫДУЩЕГО коммита: без фильтра по sha
    механизм докладывал бы «проверено» про непроверенный код."""
    stale = json.dumps([{"status": "completed", "conclusion": "success", "headSha": "old",
                         "workflowName": "t", "databaseId": 1}])
    state, _, ids = ciwait.parse_runs("github", stale, SHA)
    assert state == ciwait.WAITING and ids == []


def test_gitlab_statuses_map_the_same_way():
    def one(status):
        return json.dumps([{"id": 5, "sha": SHA, "status": status, "web_url": "https://g/5"}])
    assert ciwait.parse_runs("gitlab", one("running"), SHA)[0] == ciwait.WAITING
    assert ciwait.parse_runs("gitlab", one("success"), SHA)[0] == ciwait.PASSED
    assert ciwait.parse_runs("gitlab", one("failed"), SHA)[0] == ciwait.FAILED
    assert ciwait.parse_runs("gitlab", one("canceled"), SHA)[0] == ciwait.FAILED
    assert ciwait.parse_runs("gitlab", one("странный"), SHA)[0] == ciwait.UNREACHABLE
    assert ciwait.parse_runs("gitlab", "не json", SHA)[0] == ciwait.UNREACHABLE
    assert "sha=" + SHA in ciwait.poll_command({**REQ, "forge": "gitlab"})[-1]


def test_not_found_is_told_apart_from_unreachable():
    """Живые образцы 15.09.2026 с обоих хостов."""
    for err in ("gh: Not Found (HTTP 404)", "gh: Branch not found (HTTP 404)",
                "glab: 404 Not found (HTTP 404)", "glab: 404 Branch Not Found (HTTP 404)",
                "GraphQL: Could not resolve to a PullRequest with the number of 999."):
        assert ciwait.is_not_found(err), err
    assert not ciwait.is_not_found("error connecting to api.github.com: timeout")
    assert not ciwait.is_not_found("HTTP 401: Bad credentials")


def test_decide_never_overrides_a_final_answer_with_a_timeout():
    late = dict(waited_sec=99 * MIN, max_wait_sec=90 * MIN, found=True,
                seen_before=True, appear_sec=15 * MIN)
    assert ciwait.decide(ciwait.PASSED, **late)[0] == ciwait.PASSED
    assert ciwait.decide(ciwait.FAILED, **late)[0] == ciwait.FAILED
    assert ciwait.decide(ciwait.WAITING, **late)[0] == ciwait.TIMEOUT
    assert ciwait.decide(ciwait.UNREACHABLE, **late)[0] == ciwait.TIMEOUT


def test_a_request_is_validated_loudly(tmp_path):
    (tmp_path / ciwait.REQUEST_FILE).write_text(json.dumps({"forge": "bitbucket", "repo": "a/b", "branch": "x"}))
    with pytest.raises(ciwait.BadRequest, match="bitbucket"):
        ciwait.read_request(tmp_path)
    (tmp_path / ciwait.REQUEST_FILE).write_text(json.dumps({"forge": "github", "repo": "", "branch": ""}))
    with pytest.raises(ciwait.BadRequest, match="repo, branch"):
        ciwait.read_request(tmp_path)
    (tmp_path / ciwait.REQUEST_FILE).write_text("{не json")
    with pytest.raises(ciwait.BadRequest):
        ciwait.read_request(tmp_path)
    (tmp_path / ciwait.REQUEST_FILE).unlink()
    assert ciwait.read_request(tmp_path) is None


def test_mr_number_from_both_forges():
    assert ciwait.mr_number({"mr": "https://github.com/o/r/pull/18"}) == "18"
    assert ciwait.mr_number({"mr": "https://gitlab.com/g/p/-/merge_requests/245"}) == "245"
    assert ciwait.mr_number({"mr": ""}) == ""
    assert ciwait.mr_command({**REQ, "mr": ""})[:2] == ["gh", "api"]
    assert "merge_requests/245" in ciwait.mr_command(
        {**REQ, "forge": "gitlab", "mr": "https://gitlab.com/g/p/-/merge_requests/245"})[-1]


def test_the_failure_prompt_carries_the_cause_and_the_command():
    text = ciwait.failure_prompt("TASK-200", "test: failure (https://x/1)", REQ,
                                 ["gh", "run", "list"], 1, 2)
    assert "test: failure (https://x/1)" in text and "gh run list" in text
    assert "второй MR не открывай" in text and ciwait.REQUEST_FILE in text
    assert "возврат 1 из 2" in text


# ── счастливый путь: не достижение, но основа для поломок ───────────────────

def test_a_session_that_leaves_a_request_ends_as_waiting_ci(session, world, tmp_path):
    """Сессия вышла с кодом 0 и заявкой: исход WAITING_CI, ветка не отпущена,
    задача не сдана и не у человека — за ней смотрит раннер."""
    proc = FakeProc(rc=0)
    calls = session(proc)

    def popen(cmd, **kw):
        (Path(kw["cwd"]) / ciwait.REQUEST_FILE).write_text(json.dumps(REQ))
        return proc
    runner.subprocess.Popen = popen
    runner.run_session(dict(TASK), slot=1, queue_len=0)
    assert stats_of(tmp_path)["исход"] == "WAITING_CI"
    entry = world["redis"].waiting["TASK-200"]
    assert entry["state"] == ciwait.WAITING and entry["req"]["sha"] == SHA
    assert world["calls"]["released"] == [], "ветка нужна целой: туда вернётся сессия на красном"
    assert human(world) == [] and calls["returned"] == []
    workdir = Path(entry["workdir"])
    assert not (workdir / ciwait.REQUEST_FILE).exists(), "заявка одноразовая"
    assert state_of(workdir)["state"] == ciwait.WAITING


def test_green_checks_hand_the_task_to_verification(world):
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "success")]
    assert tick(world, T0 + 3 * MIN) == 1
    assert world["calls"]["patched"] == [{"stateId": "ST-V"}]
    assert world["redis"].waiting == {} and world["redis"].scheduled == {}
    assert state_of(workdir)["state"] == ciwait.PASSED
    assert world["calls"]["released"] == [workdir]
    assert any("зелёные" in c for c in world["calls"]["comments"])
    assert any(n.startswith("✅") for n in world["calls"]["notify"])


def test_red_checks_return_the_same_session_with_the_cause(world):
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    tick(world, T0 + 3 * MIN)
    [again] = world["redis"].queue
    assert again["resume_session_id"] == "sid-1" and again["resume_workdir"] == str(workdir)
    assert again["ci_fix_attempt"] == 1 and "failure" in again["ci_failure"]
    assert again["ci_wake_id"] and again["ci_command"][:2] == ["gh", "run"]
    assert world["calls"]["released"] == [], "рабочее дерево нужно продолжению"
    assert human(world) == []
    watch = world["redis"].waiting["TASK-200"]
    assert watch["state"] == ciwait.WAKING and watch["wake_id"] == again["ci_wake_id"]
    assert any(n.startswith("🔧") for n in world["calls"]["notify"])


def test_the_resumed_session_gets_the_failure_prompt(session, world, tmp_path):
    old = tmp_path / "TASK-200-20260915-100000"
    old.mkdir()
    calls = session(FakeProc(rc=0))
    runner.run_session(dict(TASK, resume_session_id="sid-1", resume_workdir=str(old),
                            ci_failure="test: failure", ci_request=REQ,
                            ci_command=["gh", "run", "list"], ci_fix_attempt=1,
                            ci_wake_id="w1"), slot=1, queue_len=0)
    cmd = calls["cmd"]
    assert cmd[cmd.index("--resume") + 1] == "sid-1"
    prompt = cmd[cmd.index("-p") + 1]
    assert "Проверки по TASK-200 упали" in prompt and "test: failure" in prompt
    assert calls["popen_kwargs"]["cwd"] == str(old)


def test_fix_attempts_are_capped(world):
    entry, workdir = enter(world, task=dict(TASK, ci_fix_attempt=runner.MAX_CI_FIX_ATTEMPTS))
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    tick(world, T0 + 3 * MIN)
    assert world["redis"].queue == [] and human(world) == ["issue-uuid-1"]
    assert any("исчерпаны" in c for c in world["calls"]["comments"])
    assert state_of(workdir)["state"] == ciwait.FAILED


# ── таблица отказов: каждая строка — внесённая неисправность ────────────────

def test_row1_runner_killed_mid_step_the_wait_is_picked_up_after_restart(world, caplog):
    """Раннер убит между захватом записи (ZREM) и её возвратом в расписание:
    ожидание живёт в redis, а не в памяти процесса. Новый процесс — это
    `_ci_recover` при старте подметальщика: запись без расписания
    возвращается в опрос, и зелёный доезжает до задачи."""
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "success")]
    # Внесённая неисправность: захватили и умерли. В памяти ничего не осталось
    # — ни объектов, ни потоков: всё, что есть, лежит в fake.
    assert world["redis"].zrem(runner.CI_WAIT_KEY, f"TASK-200|{entry['gen']}") == 1
    assert tick(world, T0 + 10 * MIN) == 0, "без сверки её никто не подхватит"
    with caplog.at_level(logging.WARNING):
        assert runner._ci_recover(world["redis"], T0 + 10 * MIN) == 1
    assert "без расписания" in caplog.text
    assert tick(world, T0 + 10 * MIN) == 1
    assert world["calls"]["patched"] == [{"stateId": "ST-V"}]


def test_row2_host_reboot_without_redis_snapshot_restores_from_disk(world):
    """Хост перезагружен, redis поднялся пустым (снимки без AOF): состояние
    на диске, не в памяти и не только в redis. Сверка восстанавливает
    запись и говорит об этом — в задачу и в канал, а не в журнал."""
    entry, workdir = enter(world)
    world["redis"].flush()
    world["forge"].runs = [world["forge"].run("completed", "success")]
    assert tick(world, T0 + 5 * MIN) == 0
    assert runner._ci_recover(world["redis"], T0 + 5 * MIN) == 1
    assert world["redis"].waiting["TASK-200"]["started_at"] == T0, "то же ожидание, не новое"
    assert any("пропала из redis" in c and "восстановлена" in c for c in world["calls"]["comments"])
    assert any("⚠️" in n and "восстановлена" in n for n in world["calls"]["notify"])
    assert tick(world, T0 + 5 * MIN) == 1
    assert world["calls"]["patched"] == [{"stateId": "ST-V"}]


def test_row2b_a_copy_found_too_late_is_not_polled_but_handed_over(world):
    """Копия на диске старше двух сроков (хост лежал полдня): опрашивать
    давно кончившийся прогон и писать по нему «не ответил» — врать. Человеку
    с причиной, вслух."""
    entry, workdir = enter(world)
    world["redis"].flush()
    late = T0 + 2 * runner.CI_WAIT_MAX_SEC + MIN
    assert runner._ci_recover(world["redis"], late) == 0
    assert human(world) == ["issue-uuid-1"]
    assert any("слишком поздно" in c for c in world["calls"]["comments"])
    assert state_of(workdir)["state"] == ciwait.TIMEOUT


def test_row3_ci_that_never_answers_hits_the_ceiling_and_says_how_long(world):
    """Пайплайн висит сутки: потолок ожидания, задача человеку с причиной
    «не ответил за N мин». Не вечно и не молча."""
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("in_progress")]
    now = T0
    for _ in range(200):
        now += runner.CI_WAIT_POLL_SEC
        tick(world, now)
        if human(world):
            break
    assert human(world) == ["issue-uuid-1"]
    assert now - T0 <= runner.CI_WAIT_MAX_SEC + runner.CI_WAIT_POLL_SEC
    assert now - T0 >= runner.CI_WAIT_MAX_SEC
    body = [c for c in world["calls"]["comments"] if "не закончились за отведённый срок" in c][0]
    assert f"не ответили за {runner.CI_WAIT_MAX_SEC // 60} мин" in body
    assert world["redis"].waiting == {} and world["redis"].scheduled == {}
    assert state_of(workdir)["state"] == ciwait.TIMEOUT
    assert any("⚠️" in n for n in world["calls"]["notify"])


def test_row4_a_deleted_pipeline_ends_the_wait_with_the_reason(world):
    """Прогон был в списке и пропал — удалён. Ждать того, чего нет, нельзя."""
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("in_progress")]
    tick(world, T0 + 2 * MIN)
    world["forge"].runs = []
    tick(world, T0 + 4 * MIN)
    assert human(world) == ["issue-uuid-1"]
    assert any("пропал — удалён" in c for c in world["calls"]["comments"])
    assert state_of(workdir)["state"] == ciwait.NOT_FOUND
    assert world["redis"].queue == []


def test_row4b_a_pipeline_that_never_appears_is_not_waited_for_an_hour(world):
    """Ни одного прогона нашего коммита за срок появления: CI по ветке не
    запускается или заявка не про тот коммит. Не полтора часа, а четверть."""
    entry, workdir = enter(world)
    now = T0
    while not human(world) and now < T0 + runner.CI_WAIT_MAX_SEC:
        now += runner.CI_WAIT_POLL_SEC
        tick(world, now)
    assert human(world) == ["issue-uuid-1"]
    assert now - T0 < runner.CI_WAIT_MAX_SEC
    assert any("не появилось" in c for c in world["calls"]["comments"])


def test_row4c_a_repository_the_forge_does_not_know_ends_the_wait(world):
    entry, workdir = enter(world)
    world["forge"].rc, world["forge"].stderr = 1, "gh: Not Found (HTTP 404)"
    tick(world, T0 + 2 * MIN)
    assert human(world) == ["issue-uuid-1"]
    assert any("нет такого" in c for c in world["calls"]["comments"])


def test_row5_a_lost_redis_key_is_noticed_and_said_aloud(world, caplog):
    """Ключ пропал из redis посреди ожидания (кто-то сделал FLUSHDB, снимок
    не успел). Сверка находит копию на диске, возвращает и ГОВОРИТ — не
    «задача просто не вернулась»."""
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("in_progress")]
    tick(world, T0 + 2 * MIN)
    del world["redis"].h[runner.CI_WAIT_ENTRIES_KEY]["TASK-200"]
    with caplog.at_level(logging.ERROR):
        assert tick(world, T0 + 4 * MIN) == 0
    assert "записи в redis нет" in caplog.text
    assert runner._ci_recover(world["redis"], T0 + 5 * MIN) == 1
    assert any("пропала из redis" in c for c in world["calls"]["comments"])
    restored = world["redis"].waiting["TASK-200"]
    assert restored["attempts"] == 1 and restored["started_at"] == T0


def test_row5b_redis_refusing_a_write_is_told_once_and_the_disk_copy_stays(world):
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("in_progress")]
    tick(world, T0 + 2 * MIN)
    world["redis"].broken = True
    # Захват прошёл до поломки: ломаем ровно на возврате записи.
    real_store = runner._ci_store

    def store_then_break(client, entry, next_at, now):
        world["redis"].broken = False
        world["redis"].zrem(runner.CI_WAIT_KEY, entry["ident"])
        world["redis"].broken = True
        return real_store(client, entry, next_at, now)
    world["redis"].broken = False
    runner._ci_store = store_then_break
    try:
        tick(world, T0 + 4 * MIN)
    finally:
        runner._ci_store = real_store
    assert any("не принял запись" in c for c in world["calls"]["comments"])
    assert state_of(workdir)["state"] == ciwait.WAITING
    assert state_of(workdir)["told_redis_lost"] is True
    world["redis"].broken = False
    assert runner._ci_recover(world["redis"], T0 + 6 * MIN) == 1


def test_row5c_redis_down_at_entry_the_wait_still_lives_on_disk(world):
    """Redis лёг в момент сдачи: ожидание записано на диск, человеку сказано
    сразу, сверка возвращает его в опрос, когда redis поднимется."""
    world["redis"].broken = True
    entry, workdir = enter(world)
    assert state_of(workdir)["state"] == ciwait.WAITING
    assert any("Redis сейчас недоступен" in c for c in world["calls"]["comments"])
    assert any(n.startswith("⚠️") for n in world["calls"]["notify"])
    world["redis"].broken = False
    assert runner._ci_recover(world["redis"], T0 + 10 * MIN) == 1
    world["forge"].runs = [world["forge"].run("completed", "success")]
    tick(world, T0 + 11 * MIN)
    assert world["calls"]["patched"] == [{"stateId": "ST-V"}]


def test_row6_a_closed_mr_stops_the_wait_and_does_not_wake_the_session(world):
    """MR закрыли, пока ждали, и прогон при этом красный: сессию на закрытом
    MR не будим, задача не воскресает — человеку с причиной."""
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    world["forge"].mr = "CLOSED"
    tick(world, T0 + 2 * MIN)
    assert world["redis"].queue == [], "будить некого"
    assert human(world) == ["issue-uuid-1"]
    assert any("MR закрыт" in c for c in world["calls"]["comments"])
    assert state_of(workdir)["state"] == ciwait.CLOSED
    assert world["calls"]["released"] == [workdir]


def test_row6b_a_deleted_branch_stops_the_wait(world):
    entry, workdir = enter(world, req={**REQ, "mr": ""})
    world["forge"].branch_gone = True
    tick(world, T0 + 2 * MIN)
    assert human(world) == ["issue-uuid-1"] and world["redis"].queue == []
    assert any("больше нет" in c for c in world["calls"]["comments"])


def test_row6c_a_merged_mr_ends_the_wait_without_verification_by_us(world):
    entry, workdir = enter(world)
    world["forge"].mr = "MERGED"
    tick(world, T0 + 2 * MIN)
    assert world["calls"]["patched"] == [] and human(world) == ["issue-uuid-1"]
    assert any("влит руками" in c for c in world["calls"]["comments"])


def test_row7_two_runners_on_one_redis_wake_one_session(world):
    """Два подметальщика прочитали расписание ОДНОВРЕМЕННО — оба видят одну
    созревшую запись. Забирает тот, кому ZREM ответил «да»; второй, придя со
    своим списком, обязан уйти ни с чем — одна сессия, один счёт.

    Защиты две и они страхуют друг друга: участник расписания несёт поколение
    записи (устаревший ZREM возвращает 0), а захваченная запись сверяется по
    поколению. Мутация одной из них тест не краснит — ловит вторая; сняты
    обе — второй раннер обрабатывает запись, и тест падает (проверено)."""
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    due = world["redis"].zrangebyscore(runner.CI_WAIT_KEY, "-inf", T0 + 3 * MIN)
    assert due == [f"TASK-200|{entry['gen']}"]

    class SecondRunner:
        """Тот же redis, но список созревших — тот, что прочитан до захвата."""
        def __init__(self, inner, due):
            self.inner, self.due = inner, due

        def zrangebyscore(self, *a):
            return list(self.due)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    assert tick(world, T0 + 3 * MIN) == 1
    assert runner._ci_wait_tick(SecondRunner(world["redis"], due), T0 + 3 * MIN) == 0
    assert len(world["redis"].queue) == 1
    assert sum(n.startswith("🔧") for n in world["calls"]["notify"]) == 1


def test_row8_a_continuation_that_never_comes_up_goes_to_human(world):
    """Красный разобран, сессия положена в очередь — и пропала (redis потерял
    список, раннер умер между brpop и стартом). Сторож: в очереди нет, следа
    в каталоге нет, отметки нет — человеку с причиной, а не «ждём вечно»."""
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    tick(world, T0 + 3 * MIN)
    world["redis"].l.clear()                      # очередь потеряна
    assert tick(world, T0 + 3 * MIN + runner.CI_WAKE_MAX_SEC - 1) == 0, "срок сторожа ещё не вышел"
    tick(world, T0 + 3 * MIN + runner.CI_WAKE_MAX_SEC + 1)
    assert human(world) == ["issue-uuid-1"]
    assert any("не поднялась" in c and "в очереди его нет" in c for c in world["calls"]["comments"])
    assert state_of(workdir)["state"] == ciwait.WAKE_LOST
    assert world["redis"].waiting == {}


def test_row8b_a_continuation_still_in_the_queue_is_not_an_alarm(world):
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    tick(world, T0 + 3 * MIN)
    tick(world, T0 + 3 * MIN + runner.CI_WAKE_MAX_SEC + 1)
    assert human(world) == []
    assert world["redis"].waiting["TASK-200"]["state"] == ciwait.WAKING
    assert any("ещё в очереди" in c for c in world["calls"]["comments"])


def test_row8c_a_continuation_that_came_up_acks_and_the_watch_is_dropped(world):
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    tick(world, T0 + 3 * MIN)
    [again] = world["redis"].queue
    runner._ci_ack_wake(again)
    assert world["redis"].waiting == {} and world["redis"].scheduled == {}
    assert state_of(workdir)["detail"] == "продолжение сессии поднялось"


def test_row8d_a_lost_ack_is_covered_by_the_fresh_session_log(world):
    """Redis лежал в секунду подъёма — отметки нет, но свежий session.log
    есть: тот же факт другим способом, тревоги нет."""
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    tick(world, T0 + 3 * MIN)
    world["redis"].l.clear()
    (workdir / "session.log").write_text("[00:01] поднялась\n")
    woke = world["redis"].waiting["TASK-200"]["woke_at"]
    os.utime(workdir / "session.log", (woke + 5, woke + 5))
    tick(world, T0 + 3 * MIN + runner.CI_WAKE_MAX_SEC + 1)
    assert human(world) == [] and world["redis"].waiting == {}


def test_row9_waiting_is_visible_what_how_long_and_when_we_give_up(world):
    """Исходный случай DEV-16: 40 минут без единого сигнала. Снаружи видно
    ЧТО ждём, СКОЛЬКО уже и КОГДА сдадимся — при входе, по часам и на диске."""
    entry, workdir = enter(world)
    first = world["calls"]["comments"][0]
    assert REQ["mr"] in first and "сдадимся в" in first and "коммит " + SHA[:8] in first
    assert f"{runner.CI_WAIT_MAX_SEC // 60} мин" in first
    assert world["calls"]["notify"] and world["calls"]["notify"][0].startswith("⏳")
    world["forge"].runs = [world["forge"].run("in_progress")]
    tick(world, T0 + 20 * MIN)
    assert not any("Всё ещё ждём" in c for c in world["calls"]["comments"])
    tick(world, T0 + runner.CI_WAIT_HEARTBEAT_SEC + MIN)
    beat = [c for c in world["calls"]["comments"] if "Всё ещё ждём" in c]
    assert len(beat) == 1
    assert f"уже {runner.CI_WAIT_HEARTBEAT_SEC // 60 + 1} мин" in beat[0] and "сдадимся в" in beat[0]
    assert any("всё ещё" in n for n in world["calls"]["notify"])
    disk = state_of(workdir)
    assert "сдадимся в" in disk["progress"] and disk["deadline_at"] == T0 + runner.CI_WAIT_MAX_SEC


def test_an_unreachable_forge_is_reported_once_and_the_wait_goes_on(world):
    entry, workdir = enter(world)
    world["forge"].down = True
    for i in range(1, 6):
        tick(world, T0 + i * runner.CI_WAIT_POLL_SEC)
    told = [c for c in world["calls"]["comments"] if "не опрашиваются" in c]
    assert len(told) == 1 and "сдамся в" in told[0]
    assert world["redis"].waiting["TASK-200"]["state"] == ciwait.WAITING
    world["forge"].down = False
    world["forge"].runs = [world["forge"].run("completed", "success")]
    tick(world, T0 + 6 * runner.CI_WAIT_POLL_SEC)
    assert world["calls"]["patched"] == [{"stateId": "ST-V"}]


# ── находка QA по PR #18: снятая по сроку сессия теряла заявку ──────────────

def test_a_session_cut_by_the_hour_still_hands_its_request_to_the_runner(
        session, world, tmp_path):
    """Сессия пушит на 55-й минуте, оставляет заявку, на 60-й её снимают по
    сроку: контракт обещает «заявка переживает твой выход» — и она обязана
    пережить. Раньше `rc=None` уходил общим путём «не уложилась в таймаут»
    и заявка сиротела (проверка PR #18, 14.09.2026)."""
    proc = FakeProc(timeout=True)
    calls = session(proc)

    def popen(cmd, **kw):
        (Path(kw["cwd"]) / ciwait.REQUEST_FILE).write_text(json.dumps(REQ))
        return proc
    runner.subprocess.Popen = popen
    runner.run_session(dict(TASK), slot=1, queue_len=0)
    assert proc.killed
    assert stats_of(tmp_path)["исход"] == "WAITING_CI"
    assert "TASK-200" in world["redis"].waiting
    assert calls["returned"] == [] and human(world) == []
    assert not any("не уложилась в таймаут" in c for c in world["calls"]["comments"])
    assert any("не уложилась в срок, но заявку оставить успела" in c
               for c in world["calls"]["comments"])


def test_a_halted_or_crashed_session_does_not_get_a_wait(session, world, tmp_path, monkeypatch):
    """Снятая человеком, прерывателем или упавшая сессия идёт своим путём:
    заявка снимается и об этом сказано, ожидания нет.

    Человеку она уходит переводом статуса ВМЕСТЕ с исполнителем (DEV-111):
    задача, оставшаяся в «в работе» без работающего, врёт про состояние
    доски — так 15.09.2026 четыре часа провисела DEV-44."""
    monkeypatch.setattr(runner, "_stop_requested",
                        lambda ident: runner.Halt(runner.HALT_HUMAN, "стоп", by="435"))
    proc = FakeProc(timeout=True)
    calls = session(proc)

    def popen(cmd, **kw):
        (Path(kw["cwd"]) / ciwait.REQUEST_FILE).write_text(json.dumps(REQ))
        return proc
    runner.subprocess.Popen = popen
    runner.run_session(dict(TASK), slot=1, queue_len=0)
    assert stats_of(tmp_path)["исход"] == "CANCELLED"
    assert world["redis"].waiting == {}
    assert any("Заявка не принята" in c for c in world["calls"]["comments"])
    assert world["calls"]["patched"] == [{"assigneeId": "rev", "stateId": "ST-R"}]
    assert human(world) == [], "исполнителя переставляет тот же перевод"


def test_a_broken_request_is_not_ignored_silently(session, world, tmp_path):
    proc = FakeProc(rc=0)
    calls = session(proc)

    def popen(cmd, **kw):
        (Path(kw["cwd"]) / ciwait.REQUEST_FILE).write_text('{"forge": "svn"}')
        return proc
    runner.subprocess.Popen = popen
    runner.run_session(dict(TASK), slot=1, queue_len=0)
    assert stats_of(tmp_path)["исход"] != "WAITING_CI"
    assert any("не смог её прочитать" in c for c in world["calls"]["comments"])
    assert world["redis"].waiting == {}


# ── стык со сдачей по схеме (DEV-95) ────────────────────────────────────────

def test_schema_misses_are_checked_at_entry_without_the_transition_and_repaired_after_green(
        world, monkeypatch):
    """Перевод в `Verifying` по заявке — работа раннера, и спрашивать его с
    сессии нельзя. Файл и комментарий — по-прежнему её: промахи запоминаются
    при входе (до комментариев раннера) и ремонтируются ПОСЛЕ зелёного, когда
    второго продолжения уже не будет."""
    seen = []

    def misses(issue, workdir, since, reviewer, *, require_transition=True,
               require_plan=True):
        # `require_plan` спрашивается по роду захода (DEV-109); здесь заход
        # рабочий, и шлюз с него спрашивают как обычно.
        seen.append((require_transition, require_plan))
        return ["нет файла `handoff.md`"]
    monkeypatch.setattr(runner, "handoff_misses", misses)
    entry, workdir = enter(world)
    assert seen == [(False, True)] and entry["handoff_misses"] == ["нет файла `handoff.md`"]
    assert any("Сдача пока не по схеме" in c for c in world["calls"]["comments"])
    world["forge"].runs = [world["forge"].run("completed", "success")]
    tick(world, T0 + 3 * MIN)
    assert world["calls"]["repairs"] == [["нет файла `handoff.md`"]]
    assert world["calls"]["patched"] == [], "перевод — за ремонтной сессией, не за раннером"



def test_the_wake_task_carries_the_first_start_as_the_comment_cutoff(world):
    entry, workdir = enter(world)
    world["forge"].runs = [world["forge"].run("completed", "failure")]
    tick(world, T0 + 3 * MIN)
    [again] = world["redis"].queue
    assert again["handoff_since"] == entry["since"]
    assert "handoff_misses" not in again


# ── обвязка: исход, линтер, сводка, подметальщик, контракт ──────────────────

def test_outcome_waiting_ci_exists_and_the_limit_is_older():
    common = dict(kind="work", verdict="", gates=[])
    assert runner.outcome_of(rc=0, waiting_limit=False, waiting_ci=True, **common) == "WAITING_CI"
    assert runner.outcome_of(rc=None, waiting_limit=False, waiting_ci=True, **common) == "WAITING_CI"
    assert runner.outcome_of(rc=1, waiting_limit=True, waiting_ci=True, **common) == "WAITING_LIMIT"
    assert "WAITING_CI" in runner.OUTCOMES and "WAITING_CI" in runs.OUTCOMES
    assert "WAITING_CI" not in runs.NOT_WRITTEN_BY_RUNNER, "теперь его пишет раннер"


def test_the_lint_does_not_flag_a_session_that_left_the_wait_to_the_runner():
    assert handoff_lint.RUNNER_MOVES_FILE == ciwait.STATE_FILE
    seq = [{"name": "mcp__linear__save_comment", "input": {"body": "отчёт"}}]
    flagged = handoff_lint.judge("work", seq, {"review.md"})
    assert handoff_lint.MISS_NO_TRANSITION in flagged["нарушения"]
    clean = handoff_lint.judge("work", seq, {"review.md", ciwait.STATE_FILE})
    assert clean["нарушения"] == []


def test_the_sweeper_reconciles_at_start_and_ticks_every_round(monkeypatch):
    seen = []
    monkeypatch.setattr(runner, "_release_deferred", lambda c: 0)
    monkeypatch.setattr(runner, "_ci_recover", lambda c, now: seen.append("recover") or 0)

    def tick_(c, now):
        seen.append("tick")
        if seen.count("tick") == 2:
            runner._stop.set()
        return 0
    monkeypatch.setattr(runner, "_ci_wait_tick", tick_)
    monkeypatch.setattr(runner._stop, "wait", lambda s: None)
    runner._stop.clear()
    try:
        runner.sweeper(object())
    finally:
        runner._stop.clear()
    assert seen[:2] == ["recover", "tick"] and seen.count("recover") == 1


def test_the_contract_tells_the_session_to_leave_a_request_and_not_to_wait():
    text = build_prompt(TASK, Path("/tmp/w"), Path("/repos"), "Андрей")
    flat = " ".join(text.split())
    assert "ci-wait.json" in flat and '"sha"' in flat
    assert "ждать пайплайн — работа раннера" in flat
    assert "не пиши «меня разбудят»" in flat
    assert "переживает твой выход" in flat


def test_the_request_file_and_state_file_are_a_contract_with_the_prompt():
    text = build_prompt(TASK, Path("/tmp/w"), Path("/repos"))
    assert ciwait.REQUEST_FILE in text
    assert runner.CI_WAIT_MAX_SEC > runner.SESSION_TIMEOUT_SEC, \
        "срок ожидания обязан быть НЕ РАВЕН сроку сессии — иначе та же беда"
