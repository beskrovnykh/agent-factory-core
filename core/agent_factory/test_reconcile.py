"""Сверка пропущенных мержей: работа в main — задача в `Done`.

Критерий задачи (DEV-106) слово в слово: задача, чья работа влита, уезжает
сверкой, а не руками; задача, чья работа НЕ влита, сверкой не трогается;
повторный прогон не создаёт второй записи. Каждый пункт — тест ниже.

Живой случай, с которого всё: DEV-81 влита PR #16 14.09.2026, репозиторий
подписали на события позже, и задача осталась в «In Review» навсегда.
Соседний случай, на котором сверка обязана промолчать: DEV-66 — PR #3 влит,
а рядом открыт #22 с веткой `agent/DEV-66-v2`, то есть работа задачи в main
НЕ лежит.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import pytest

from agent_factory.deploy import Verification
from agent_factory.gitref import Located, Unavailable
from agent_factory.reconcile import Reconciler, Stalled

Q = "agent:queue:test"


def run(coro):
    return asyncio.run(coro)


class Board:
    """Трекер: стопки человека, перевод состояния, комментарии."""

    def __init__(self, issues, boom=False):
        # {идентификатор: задача}; состояние живёт в самой задаче, поэтому
        # перевод в Done убирает её из стопок — как на живой доске.
        self.issues = dict(issues)
        self.moved: list[tuple] = []
        self.assigned: list[tuple] = []
        self.comments: list[tuple] = []
        self.asked: list[tuple] = []
        self.assigned: list[tuple] = []
        self.boom = boom

    def issues_awaiting(self, team_id, states):
        if self.boom:
            raise RuntimeError("Linear 502")
        self.asked.append((team_id, tuple(states)))
        return [i for i in self.issues.values()
                if (i.get("state") or {}).get("name") in states]

    def issue_by_identifier(self, ident):
        return self.issues.get(ident)

    def set_state(self, issue_id, name):
        issue = next(i for i in self.issues.values() if i["id"] == issue_id)
        was = issue["state"]["name"]
        issue["state"] = {"name": name, "type": "completed"}
        self.moved.append((issue_id, name))
        return {"from": was, "to": name, "already": was == name}

    def assign(self, issue_id, user_id):
        self.assigned.append((issue_id, user_id))
        return {"assignee": user_id}

    def comment(self, issue_id, body):
        self.comments.append((issue_id, body))

    def assign(self, issue_id, user_id):
        issue = next(i for i in self.issues.values() if i["id"] == issue_id)
        issue["assignee"] = {"id": user_id}
        self.assigned.append((issue_id, user_id))


class Store:
    """Хранилище: захват ключа старта и очередь. Подбору мержа оно не нужно
    (перевод идёт мимо него), а пересмотру сливаемости — нужно: он ставит
    сессию разрешения конфликта и держит тот же захват, что вебхук."""

    def __init__(self, busy=(), enqueue_boom=False):
        self.busy = set(busy)
        self.enqueue_boom = enqueue_boom
        self.claimed: list[str] = []
        self.released: list[str] = []
        self.queued: list[tuple] = []
        self.stash: dict = {}

    async def claim(self, key, ttl):
        self.claimed.append(key)
        return key not in self.busy

    async def release(self, key):
        self.released.append(key)
        return None

    async def enqueue(self, key, value):
        if self.enqueue_boom:
            raise RuntimeError("redis не отвечает")
        self.queued.append((key, json.loads(value)))

    async def put(self, key, value, ttl=0):
        self.stash[key] = value


class Git:
    """`gitref.Repos`: что и где нашлось по задаче."""

    def __init__(self, found=None, boom=None, main="main"):
        self.found = dict(found or {})
        self.boom = boom
        self.main = main
        self.asked: list[str] = []

    def main_of(self, repo):
        return self.main

    def locate(self, ident, ref="", sha="", project_path=""):
        self.asked.append(ident)
        if self.boom:
            raise self.boom
        return self.found.get(ident)


class Gh:
    """Хостинг: смерженный PR по задаче и открытый PR с его сливаемостью."""

    def __init__(self, prs=None, boom=False, open_prs=None):
        self.prs = dict(prs or {})
        self.boom = boom
        self.open_prs = dict(open_prs or {})
        self.asked: list[str] = []

    def merged_request(self, ident):
        if self.boom:
            raise RuntimeError("gh: HTTP 502")
        return self.prs.get(ident)

    def open_request(self, ident):
        self.asked.append(ident)
        return self.open_prs.get(ident)


def waiting(ident="DEV-81", state="In Review", issue_id=""):
    return {"id": issue_id or f"iss-{ident}", "identifier": ident,
            "title": "Android-эмулятор как рабочий инструмент",
            "state": {"name": state, "type": "started"}}


def in_main(sha="a83699e1" + "0" * 32, repo="/home/ubuntu/agent-repos/agent-factory"):
    return Located(repo=repo, ref="main", sha=sha, ancestor=True, when=1)


def on_a_branch(ref="agent/DEV-66-v2"):
    return Located(repo="/r/agent-factory", ref=ref, sha="b" * 40,
                   ancestor=False, when=2)


def pr16():
    return {"mr_iid": "16", "project_path": "beskrovnykh/agent-factory",
            "ref": "main", "source_ref": "agent/DEV-81-silent",
            "url": "https://github.com/beskrovnykh/agent-factory/pull/16",
            "merged_by": "beskrovnykh", "merged_at": "2026-09-14T00:48:21Z"}


def open_pr(ident="DEV-38", number="23", mergeable=False, source=""):
    """Открытый PR задачи, каким его отдаёт `hosting.open_request`."""
    return {"number": number, "repo": "beskrovnykh/agent-factory",
            "url": f"https://github.com/beskrovnykh/agent-factory/pull/{number}",
            "source_ref": source or f"agent/{ident}", "target_ref": "main",
            "title": "…", "mergeable": mergeable}


def mk(issues=None, found=None, prs=None, states=("In Review", "Not Verified"),
       hosting=True, repos=True, tracker=True, open_prs=None, store=None,
       restack=True, **kw):
    board = Board(issues if issues is not None else {"DEV-81": waiting()}) if tracker else None
    store = store if store is not None else Store()
    v = Verification(store, Q, None, tracker=board)
    git = Git(found if found is not None else {"DEV-81": in_main()})
    # Пересмотр сливаемости требует четверых сверх сверки мержей; `restack=
    # False` — инсталляция, у которой их нет: сверка обязана работать как
    # раньше.
    extra = dict(store=store, queue_key=Q, agent_id="agent-uuid",
                 stale_state="Not Verified", settle_sec=0) if restack else {}
    rec = Reconciler(board, v, "team-1", states=states,
                     repos=git if repos else None,
                     hosting=Gh(prs if prs is not None else {"DEV-81": pr16()},
                                open_prs=open_prs)
                     if hosting else None, **extra, **kw)
    return board, git, rec


# ── подбирает ────────────────────────────────────────────────────────────

def test_work_already_in_main_moves_the_task_to_done(caplog):
    # DEV-81: события о мерже не было и не будет. Сверка обязана сделать то
    # же, что сделало бы событие: перевод и запись «Закрыто по мержу».
    board, git, rec = mk()
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-81", "Done")]
    (iid, body), = board.comments
    assert iid == "iss-DEV-81" and "Закрыто по мержу" in body
    # Запись — та же, что у события: ссылка на PR, откуда и куда, кто смержил.
    assert "pull/16" in body and "agent/DEV-81-silent" in body and "`main`" in body
    assert "beskrovnykh" in body
    said = " | ".join(r.getMessage() for r in caplog.records)
    assert "DEV-81" in said and "события о мерже не было" in said


def test_a_branch_deleted_on_merge_is_not_printed_as_its_own_source():
    # Ветку удалили при мерже, и работа нашлась мерж-коммитом в main: «куда»
    # знаем, «откуда» — нет. Напечатать «main → main» значило бы выдумать.
    board, git, rec = mk(prs={})
    assert run(rec.tick()) == 1
    (_, body), = board.comments
    assert "`?` → `main`" in body


def test_a_merged_branch_still_alive_is_the_source_not_the_target():
    board, git, rec = mk(prs={}, found={"DEV-81": Located(
        repo="/r/agent-factory", ref="agent/DEV-81-silent", sha="c" * 40,
        ancestor=True, when=3)})
    assert run(rec.tick()) == 1
    (_, body), = board.comments
    assert "`agent/DEV-81-silent` → `?`" in body


def test_the_sweep_looks_only_at_the_human_stacks():
    # Не «все задачи команды»: в работе и на проверке задача живёт своей
    # жизнью, и закрывать её по влитому коммиту рано.
    board, git, rec = mk(states=("In Review", "Not Verified"))
    run(rec.tick())
    assert board.asked == [("team-1", ("In Review", "Not Verified"))]


def test_a_task_in_the_second_stack_is_picked_up_too():
    # «Не прошло проверку» — вторая стопка человека, и мерж старше вердикта.
    board, git, rec = mk(issues={"DEV-43": waiting("DEV-43", "Not Verified")},
                         found={"DEV-43": in_main()}, prs={})
    assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-43", "Done")]


# ── не трогает ───────────────────────────────────────────────────────────

def test_work_that_is_not_in_main_is_left_alone():
    # DEV-66: старый PR влит, но работа задачи — в невлитой ветке -v2.
    # `locate` отдаёт именно её, и сверке тут делать нечего.
    board, git, rec = mk(issues={"DEV-66": waiting("DEV-66", "Not Verified")},
                         found={"DEV-66": on_a_branch()})
    assert run(rec.tick()) == 0
    assert board.moved == [] and board.comments == []


def test_a_task_without_any_work_anywhere_is_left_alone():
    board, git, rec = mk(found={})
    assert run(rec.tick()) == 0
    assert board.moved == [] and board.comments == []


def test_silence_from_git_is_not_a_no(caplog):
    # «Не знаем» — не «не влито» и не «влито». Молчащий git не повод ни
    # закрывать задачу, ни считать её разобранной: круг спросит снова.
    board, _, rec = mk()
    rec.repos = Git(boom=Unavailable("ls-remote в /r: не ответил за 20с"))
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 0
    assert board.moved == []
    assert any("не знаем" in r.getMessage() for r in caplog.records)


def test_a_tracker_that_does_not_answer_stops_the_round(caplog):
    board, git, rec = mk()
    board.boom = True
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 0
    assert any("стопки человека не спросить" in r.getMessage() for r in caplog.records)


# ── дедуп ────────────────────────────────────────────────────────────────

def test_a_second_sweep_writes_no_second_record():
    # Переведённая задача уходит из стопок — со следующего круга сверка её
    # не видит, и второй записи взяться неоткуда.
    board, git, rec = mk()
    assert run(rec.tick()) == 1
    assert run(rec.tick()) == 0
    assert len(board.comments) == 1 and len(board.moved) == 1


def test_a_closed_task_seen_again_is_not_written_twice():
    # Тот же дедуп без всякой памяти: задача уже закрыта — «уже закрыта».
    # Так сверка и событие не спорят друг с другом.
    done = waiting()
    done["state"] = {"name": "Done", "type": "completed"}
    board, git, rec = mk(issues={"DEV-81": done}, states=("In Review", "Done"))
    assert run(rec.tick()) == 0
    assert board.moved == [] and board.comments == []


# ── хостинг только дописывает ────────────────────────────────────────────

def test_without_a_hosting_the_task_still_closes(caplog):
    # Решает git, а не хостинг: без него запись короче, но задача закрыта.
    board, git, rec = mk(hosting=False)
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-81", "Done")]
    said = " | ".join(r.getMessage() for r in caplog.records)
    assert "без ссылки" in said


def test_a_hosting_that_fails_does_not_stop_the_move(caplog):
    board, git, rec = mk()
    rec.hosting = Gh(boom=True)
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-81", "Done")]
    assert any("не ответил про смерженный PR" in r.getMessage()
               for r in caplog.records)


def test_the_hosting_does_not_decide_what_is_merged():
    # Хостинг называет смерженный PR, а работа — на невлитой ветке. Верим
    # git: иначе доработка после ревью закрывалась бы старым мержем.
    board, git, rec = mk(found={"DEV-81": on_a_branch("agent/DEV-81-v2")})
    assert run(rec.tick()) == 0
    assert board.moved == []


# ── когда сверки нет ─────────────────────────────────────────────────────

@pytest.mark.parametrize("broken,why", [
    ({"repos": False}, "клонов площадки"),
    ({"tracker": False}, "перечислять задачи в стопках"),
])
def test_the_sweep_says_out_loud_what_it_lacks(broken, why, caplog):
    _, _, rec = mk(**broken)
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 0
    assert any(why in r.getMessage() for r in caplog.records)


def test_without_a_verification_there_is_nobody_to_move_the_task(caplog):
    _, _, rec = mk()
    rec.verification = None
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 0
    assert any("переводить некому" in r.getMessage() for r in caplog.records)


def test_without_a_team_the_sweep_does_not_touch_anyones_board():
    board, git, rec = mk()
    rec.team_id = ""
    assert run(rec.tick()) == 0
    assert board.asked == []


def test_without_states_there_is_no_stack_to_sweep():
    board, git, rec = mk(states=())
    assert run(rec.tick()) == 0
    assert board.asked == []


def test_a_zero_interval_turns_the_loop_off():
    board, git, rec = mk(interval_sec=0)
    run(rec.run())
    assert board.asked == [] and board.moved == []


def test_the_loop_sweeps_at_once_and_then_sleeps(monkeypatch):
    # «Периодически И при старте»: первый круг — сразу, иначе пропущенный
    # мерж ждёт целый час после перезапуска приёма. Ждём теперь не сном, а
    # сроком на сигнале о мерже (DEV-109) — срок тот же.
    board, git, rec = mk(interval_sec=3600)
    waited = []

    async def wait_for(coro, timeout):
        waited.append(timeout)
        coro.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    with pytest.raises(asyncio.CancelledError):
        run(rec.run())
    assert board.moved == [("iss-DEV-81", "Done")] and waited == [3600]


def test_a_broken_round_does_not_kill_the_loop(monkeypatch):
    # Молчание — ровно та беда, которую сверка чинит: одна осечка не вправе
    # выключить её до следующего перезапуска.
    board, git, rec = mk(interval_sec=60)
    rounds = []

    async def boom():
        rounds.append(1)
        raise RuntimeError("круг не удался")

    async def wait_for(coro, timeout):
        coro.close()
        if len(rounds) > 1:
            raise asyncio.CancelledError
        # Срок вышел, сигнала о мерже не было — обычный ход круга.
        raise asyncio.TimeoutError

    rec.tick = boom
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    with pytest.raises(asyncio.CancelledError):
        run(rec.run())
    assert len(rounds) == 2


# ── пересмотр сливаемости: стопка «ждёт мержа» не должна врать (DEV-109) ──

async def _nosleep(_sec):
    return None


def stale_board(ident="DEV-38", state="In Review"):
    """Задача в стопке человека, чья работа лежит в невлитой ветке."""
    return ({ident: waiting(ident, state)},
            {ident: on_a_branch(f"agent/{ident}")},
            {ident: open_pr(ident)})


def test_a_branch_that_no_longer_merges_leaves_the_stack():
    # Случай задачи: DEV-38 лежит в «ждёт мержа», а кнопка отвечает отказом.
    issues, found, prs = stale_board()
    store = Store()
    board, git, rec = mk(issues=issues, found=found, open_prs=prs, store=store)
    # Подобранных мержей ноль — уведённое считается отдельно, и путать их
    # нельзя: «подобрано 1» на задаче, ушедшей чинить конфликт, врёт.
    assert run(rec.tick()) == 0
    assert board.moved == [("iss-DEV-38", "Not Verified")]
    assert board.assigned == [("iss-DEV-38", "agent-uuid")]
    (_, body), = board.comments
    assert "не вливается" in body and "PR #23" in body
    # Причина названа человеку целиком: куда ушла задача и почему круг не
    # считается — иначе он увидит переехавшую задачу без объяснения.
    assert "Not Verified" in body and "AGENT_MAX_WORK_ROUNDS" in body


def test_the_conflict_session_is_queued_as_merge_not_as_work():
    issues, found, prs = stale_board()
    store = Store()
    board, git, rec = mk(issues=issues, found=found, open_prs=prs, store=store)
    run(rec.tick())
    (queue, task), = store.queued
    assert queue == Q
    # Род `merge` — то, по чему раннер выберет контракт разрешения конфликта,
    # а журнал запусков не посчитает этот заход рабочим кругом.
    assert task["kind"] == "merge" and task["identifier"] == "DEV-38"
    assert task["ref"] == "agent/DEV-38" and task["mr_iid"] == "23"
    assert "не вливается" in task["conflict"]
    # Заначка по имени ветки — как у любого пути постановки.
    assert store.stash.get("agent:task:DEV-38")


def test_the_start_key_is_taken_before_the_agent_is_assigned():
    # Назначение агента ловит вебхук и поднимает ОБЫЧНУЮ рабочую сессию —
    # то есть вторую на ту же задачу, да ещё с потраченным кругом. Захват
    # того же ключа, что у вебхука, это глушит; взять его надо ДО назначения.
    issues, found, prs = stale_board()
    store = Store()
    board, git, rec = mk(issues=issues, found=found, open_prs=prs, store=store)
    run(rec.tick())
    assert "agent:started:iss-DEV-38" in store.claimed


def test_a_task_whose_session_is_already_running_is_left_alone(caplog):
    issues, found, prs = stale_board()
    store = Store(busy={"agent:started:iss-DEV-38"})
    board, git, rec = mk(issues=issues, found=found, open_prs=prs, store=store)
    with caplog.at_level(logging.INFO, logger="intake"):
        run(rec.tick())
    assert store.queued == [] and board.moved == [] and board.comments == []
    assert "уже идёт" in " | ".join(r.getMessage() for r in caplog.records)


def test_a_mergeable_branch_stays_in_the_stack():
    issues, found, _ = stale_board()
    board, git, rec = mk(issues=issues, found=found,
                         open_prs={"DEV-38": open_pr("DEV-38", mergeable=True)})
    assert run(rec.tick()) == 0
    assert board.moved == [] and board.comments == []


def test_a_silent_hosting_does_not_move_anything(monkeypatch):
    # «Не знаем» — не «не вливается»: протухший токен не повод заводить
    # сессию и снимать с человека то, что он готов был смержить.
    # Паузу переспроса снимаем: ждать её здесь нечего, она про живой GitHub.
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    issues, found, _ = stale_board()
    board, git, rec = mk(issues=issues, found=found,
                         open_prs={"DEV-38": open_pr("DEV-38", mergeable=None)})
    assert run(rec.tick()) == 0
    assert board.moved == [] and board.comments == []


def test_a_queue_that_refused_leaves_the_task_where_it_was(caplog):
    # Сессию поставить не вышло — уводить задачу нельзя: она осталась бы вне
    # стопки и без того, кто её починит.
    issues, found, prs = stale_board()
    store = Store(enqueue_boom=True)
    board, git, rec = mk(issues=issues, found=found, open_prs=prs, store=store)
    with caplog.at_level(logging.ERROR, logger="intake"):
        assert run(rec.tick()) == 0
    assert board.moved == [] and board.comments == []
    # Захват снят: иначе следующее законное назначение агента отбросится
    # как дубль на целый час.
    assert store.released == ["agent:started:iss-DEV-38"]


def test_without_the_four_things_the_sweep_still_picks_up_merges(caplog):
    # Инсталляция без хостинга/очереди/агента: пересмотра нет, но подбор
    # пропущенных мержей обязан работать как раньше.
    board, git, rec = mk(restack=False)
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-81", "Done")]
    assert "пересмотр сливаемости не идёт" in " | ".join(
        r.getMessage() for r in caplog.records)


def test_the_second_human_stack_is_reviewed_too():
    # «Не проверено» — тоже стопка человека, и протухшая ветка там так же
    # мешает: маршрут по вердикту кладёт в неё несливаемую работу сам.
    issues, found, prs = stale_board("DEV-13", "Not Verified")
    board, git, rec = mk(issues=issues, found=found, open_prs=prs)
    run(rec.tick())
    assert board.assigned == [("iss-DEV-13", "agent-uuid")]


def test_a_merge_wakes_the_sweep_before_its_hour_is_up(monkeypatch):
    # Стопка протухает не по часам, а в момент чужого мержа.
    board, git, rec = mk(interval_sec=3600)
    run(rec.wake("смержена работа DEV-99"))
    rounds = []

    async def tick():
        rounds.append(1)
        if len(rounds) > 1:
            raise asyncio.CancelledError
        return 0

    rec.tick = tick
    with pytest.raises(asyncio.CancelledError):
        run(rec.run())
    # Второй круг пошёл по сигналу, а не по часу: реального часа тест не ждал.
    assert len(rounds) == 2


# ── сверка «в работе»: статус есть, а сессии нет ───────────────────────────
#
# Критерий задачи (DEV-111) слово в слово: задача в `In Progress` без живой
# сессии и без места в очереди дольше порога уходит из `In Progress` с
# записью в задачу, почему; три живых случая разведены правильно — упавшая на
# разбор, непочатые в `Todo`; повторный прогон не трогает задачи, у которых
# сессия правда идёт.
#
# Живые случаи, с которых всё (обход 16.09.2026 00:13): DEV-44 — сессия
# кончилась в 20:33 с `исход=FAILED`, задача осталась «в работе»; DEV-50 и
# DEV-37 — рабочего каталога нет вовсе, статус поставлен руками, работа не
# начиналась (DEV-37 так провисел неделю).

RESULT = ('{"type":"result","subtype":"success","total_cost_usd":0.5,'
          '"num_turns":7,"duration_ms":1000,"usage":{"input_tokens":1}}\n')


def ran(root, ident, hours_ago, outcome="FAILED", finished=True, kind="work"):
    """Настоящий рабочий каталог раннера, а не выдумка про него.

    Каталоги делаем на диске и читаем их `sessions.walk` — тем же кодом, что
    в проде. Заглушка вместо него закрепила бы имена полей («идёт», «исход»),
    а не то, что сверка видит живую сессию.
    """
    name = f"{ident}-2026091{5 if hours_ago > 3 else 6}-{200830 + int(hours_ago)}"
    d = root / name
    d.mkdir(parents=True)
    t = d / "transcript.jsonl"
    # Итоговая запись — единственный признак «заход кончился». Без неё свежий
    # каталог читается как идущая сессия, старый — как брошенная.
    t.write_text('{"type":"assistant"}\n' + (RESULT if finished else ""))
    if outcome:
        (d / "stats.json").write_text(
            json.dumps({"задача": ident, "род": kind, "исход": outcome},
                       ensure_ascii=False))
    when = time.time() - hours_ago * 3600
    os.utime(t, (when, when))
    return name


def in_work(ident, hours_ago=8, assignee=None, state="In Progress"):
    """Задача, стоящая «в работе», какой её отдаёт трекер."""
    issue = {"id": f"iss-{ident}", "identifier": ident,
             "title": "…", "state": {"name": state, "type": "started"},
             "updatedAt": datetime.fromtimestamp(
                 time.time() - hours_ago * 3600, timezone.utc)
             .isoformat().replace("+00:00", "Z")}
    if assignee:
        issue["assignee"] = assignee
    return issue


AGENT = {"id": "agent-uuid"}
HUMAN = {"id": "human-uuid", "name": "Андрей"}


def desk(tmp_path, issues, queued=(), **kw):
    board = Board({i["identifier"]: i for i in issues})
    opts = {"states": ("In Progress",), "review_state": "In Review",
            "todo_state": "Todo", "agent_user_id": AGENT["id"],
            "reviewer_id": HUMAN["id"], "queued": lambda: set(queued)}
    opts.update(kw)
    return board, Stalled(board, str(tmp_path), "team-1", **opts)


# ── уводит ────────────────────────────────────────────────────────────────

def test_a_fallen_session_hands_the_task_over(tmp_path):
    """DEV-44: заход кончился четыре часа назад с FAILED, а задача всё «в
    работе». Уходит человеку — со сменой статуса, а не одного исполнителя."""
    board, rec = desk(tmp_path, [in_work("DEV-44", assignee=HUMAN)])
    ran(tmp_path, "DEV-44", hours_ago=4)

    assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-44", "In Review")]
    assert board.assigned == [("iss-DEV-44", "human-uuid")]
    (_, body), = board.comments
    assert "FAILED" in body and "4 ч" in body


def test_a_task_that_never_had_a_session_goes_back_to_todo(tmp_path):
    """DEV-37: статус поставлен руками неделю назад, каталога нет ни одного.
    Это не упавшая работа, а не начатая, и адрес у неё другой."""
    board, rec = desk(tmp_path, [in_work("DEV-37", hours_ago=24 * 7)])

    assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-37", "Todo")]
    assert board.assigned == [], "не начатую задачу человеку не вешаем"
    (_, body), = board.comments
    assert "сессии не было ни одной" in body


def test_the_three_live_cases_are_told_apart(tmp_path):
    """Все три задачи обхода 16.09 разом: одна на разбор, две в Todo, и
    живые рядом не тронуты."""
    board, rec = desk(tmp_path, [in_work("DEV-44", assignee=HUMAN),
                                 in_work("DEV-50", hours_ago=30),
                                 in_work("DEV-37", hours_ago=24 * 7),
                                 in_work("DEV-108", hours_ago=0.1, assignee=AGENT),
                                 in_work("DEV-38", hours_ago=0.1, assignee=AGENT)])
    ran(tmp_path, "DEV-44", hours_ago=4)
    ran(tmp_path, "DEV-108", hours_ago=0, outcome="", finished=False)
    ran(tmp_path, "DEV-38", hours_ago=0, outcome="", finished=False)

    assert run(rec.tick()) == 3
    assert dict(board.moved) == {"iss-DEV-44": "In Review",
                                 "iss-DEV-50": "Todo", "iss-DEV-37": "Todo"}


def test_the_record_tells_the_truth_about_the_handover(tmp_path):
    """Последняя строка записи — единственное, что человек делает руками, и
    она про сделанное, а не про задуманное: принимающего не задали — так и
    сказано; задача осталась на агенте — сказано, что назначить надо заново
    (сама сверка исполнителя не снимает: `assigneeId` у адаптера не
    обнуляется)."""
    board, rec = desk(tmp_path, [in_work("DEV-44")], reviewer_id="")
    ran(tmp_path, "DEV-44", hours_ago=4)
    assert run(rec.tick()) == 1
    assert "Исполнителя сменить не удалось" in board.comments[0][1]

    board, rec = desk(tmp_path / "2", [in_work("DEV-50", hours_ago=30,
                                               assignee=AGENT)])
    (tmp_path / "2").mkdir()
    assert run(rec.tick()) == 1
    assert "назначь заново" in board.comments[0][1]


def test_an_orphaned_session_counts_as_fallen(tmp_path):
    """Заход, оборвавшийся без итоговой записи (OOM снял обёртку), — это тоже
    «сессии нет»: исхода у него не будет никогда."""
    board, rec = desk(tmp_path, [in_work("DEV-44")])
    ran(tmp_path, "DEV-44", hours_ago=5, outcome="", finished=False)

    assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-44", "In Review")]
    assert "не оставив итоговой записи" in board.comments[0][1]


# ── не трогает ────────────────────────────────────────────────────────────

def test_a_running_session_is_left_alone(tmp_path):
    """Главный запрет: сверка не смеет отбирать задачу у идущей сессии.
    Повторный круг — тоже ничего.

    Сессия тут молчит два часа — дольше порога, но меньше срока, после
    которого заход считается брошенным. Так выглядит любой долгий шаг без
    вывода (сборка, прогон набора), и отличить его от упавшей сессии можно
    только признаком «идёт», а не сроком молчания.
    """
    board, rec = desk(tmp_path, [in_work("DEV-108", hours_ago=24, assignee=AGENT)])
    ran(tmp_path, "DEV-108", hours_ago=2, outcome="", finished=False)

    assert run(rec.tick()) == 0 and run(rec.tick()) == 0
    assert board.moved == [] and board.comments == []


def test_a_queued_task_is_left_alone(tmp_path):
    """Задача ждёт свободный слот: сессии нет и каталога нет — законно.
    Очередь глубиной 4-5 задач тут норма, и сверка без неё разбирала бы
    очередь быстрее раннера."""
    board, rec = desk(tmp_path, [in_work("DEV-50", hours_ago=30, assignee=AGENT)],
                      queued=("DEV-50",))

    assert run(rec.tick()) == 0
    assert board.moved == []


def test_waiting_for_ci_is_not_a_stall(tmp_path):
    """`WAITING_CI` и `WAITING_LIMIT` — это «в работе» по правде: заход
    кончился, но продолжение будет, и оно вернётся в ту же ветку."""
    for outcome in ("WAITING_CI", "WAITING_LIMIT"):
        root = tmp_path / outcome
        root.mkdir()
        board, rec = desk(root, [in_work("DEV-16", assignee=AGENT)])
        ran(root, "DEV-16", hours_ago=6, outcome=outcome)
        assert run(rec.tick()) == 0, outcome
        assert board.moved == []


def test_a_fresh_task_waits_out_the_threshold(tmp_path):
    """Между «задачу забрали из очереди» и «каталог появился» есть окно, и
    закрыть его нечем, кроме срока."""
    board, rec = desk(tmp_path, [in_work("DEV-50", hours_ago=0.2, assignee=AGENT)])

    assert run(rec.tick()) == 0
    assert board.moved == []


def test_a_reassigned_task_with_an_old_session_waits(tmp_path):
    """Заход кончился вчера, а задачу переназначили пять минут назад: нового
    каталога ещё нет, и старый про неё уже ничего не говорит."""
    board, rec = desk(tmp_path, [in_work("DEV-44", hours_ago=0.08, assignee=AGENT)])
    ran(tmp_path, "DEV-44", hours_ago=20)

    assert run(rec.tick()) == 0
    assert board.moved == []


def test_a_task_a_human_holds_without_any_session_is_left_alone(tmp_path):
    """Каталога нет, задача на человеке — доказательства, что это заход
    агента, нет никакого. Может, он ею и занят сам."""
    board, rec = desk(tmp_path, [in_work("DEV-9", hours_ago=48, assignee=HUMAN)])

    assert run(rec.tick()) == 0
    assert board.moved == []


def test_a_fallen_session_is_taken_even_from_a_human(tmp_path):
    """Обратный случай той же развилки: каталог есть, и он говорит FAILED.
    На человеке задача оказалась потому, что раннер её туда и передал."""
    board, rec = desk(tmp_path, [in_work("DEV-44", assignee=HUMAN)])
    ran(tmp_path, "DEV-44", hours_ago=4)

    assert run(rec.tick()) == 1


def test_an_unreadable_clock_reads_as_fresh(tmp_path):
    """Метку времени не разобрали — значит про срок молчания мы НИЧЕГО не
    знаем. Ноль тут читался бы как «молчит с начала времён», то есть «трогай
    немедленно»: неизвестность не должна работать против задачи."""
    issue = in_work("DEV-37", hours_ago=24 * 7)
    issue["updatedAt"] = "позавчера"
    board, rec = desk(tmp_path, [issue])

    assert run(rec.tick()) == 0
    assert board.moved == []


def test_an_unreadable_work_root_skips_the_round(tmp_path):
    """Нечитаемый каталог отдаёт пустой список заходов, а пустой список — это
    «сессии не было ни разу». Без этой ветки одна ошибка ФС сбросила бы в
    Todo всю доску разом."""
    board, rec = desk(tmp_path / "нет-такого", [in_work("DEV-37", hours_ago=24 * 7)])

    assert run(rec.tick()) == 0
    assert board.moved == []


def test_a_silent_queue_skips_the_round(tmp_path):
    """Лежачий redis — это «не знаю, стоит ли задача в очереди», а не «не
    стоит». То же правило, что у молчащего git в сверке мержей."""
    def boom():
        raise RuntimeError("redis: connection refused")

    board, rec = desk(tmp_path, [in_work("DEV-37", hours_ago=24 * 7)], queued=())
    rec.queued = boom

    assert run(rec.tick()) == 0
    assert board.moved == []


# ── как уводит ────────────────────────────────────────────────────────────

def test_the_record_explains_itself_and_the_move_comes_first(tmp_path):
    """Порядок: сначала перевод, потом запись. Не вышел перевод — записи нет
    вовсе: она повторялась бы каждый час по задаче, которая никуда не едет."""
    board, rec = desk(tmp_path, [in_work("DEV-44")])
    ran(tmp_path, "DEV-44", hours_ago=4)

    def refuse(issue_id, name):
        raise RuntimeError("в команде нет статуса 'In Review'")

    board.set_state = refuse
    assert run(rec.tick()) == 0
    assert board.comments == []


def test_a_lost_comment_does_not_undo_the_move(tmp_path, caplog):
    """И наоборот: трекер не принял запись (так 15.09 потерялась причина
    падения DEV-44 — 401 по ключу личности), а задача всё равно уже не
    выглядит идущей. Причина при этом обязана остаться в журнале."""
    board, rec = desk(tmp_path, [in_work("DEV-44")])
    ran(tmp_path, "DEV-44", hours_ago=4)

    def refuse(issue_id, body):
        raise RuntimeError("HTTP Error 401: Unauthorized")

    board.comment = refuse
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(rec.tick()) == 1
    assert board.moved == [("iss-DEV-44", "In Review")]
    lost = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert lost and "FAILED" in lost[0] and "DEV-44" in lost[0]


def test_the_reconciler_names_what_it_lacks(tmp_path):
    """Выключенная сверка обязана сказать, чем именно она выключена: без
    очереди — не работает вовсе, потому что ждущую слот задачу не отличить от
    брошенной."""
    _, rec = desk(tmp_path, [], queued=())
    rec.queued = None
    assert "очередь" in rec.able()
    _, rec = desk(tmp_path, [], states=())
    assert "состояния работы" in rec.able()
    _, rec = desk(tmp_path, [], todo_state="")
    assert "уводить" in rec.able()
    _, rec = desk(tmp_path, [])
    assert rec.able() == ""
