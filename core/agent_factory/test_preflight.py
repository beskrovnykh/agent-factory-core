"""Ворота перед постановкой проверки (DEV-86).

Каждый тест — пункт критерия приёмки задачи: ворота зовутся до постановки в
обеих точках; несуществующая задача, отсутствующая ветка и повтор того же
коммита сессию не поднимают; режим выбирается по предковости, а не по слову
в событии; отсутствие критерия и молчание трекера/git сессию НЕ отменяют.

Отдельно — прогон решения ворот по журналу 56 записанных проверок client-b за
12–14.09.2026 (`testdata/verify-journal-client-b-2026-09-13.json`): семь
сессий «выката не было», давших настоящую проверку поведения в ветке,
обязаны получить контракт ветки, а не отсев.
"""
import asyncio
import inspect
import json
import logging
import os
import time
from pathlib import Path

import pytest

from agent_factory import deploy, runs, sessions
from agent_factory.deploy import (CRITERIA_FALLBACK, CRITERIA_NONE, CRITERIA_TASK,
                                  CRITERIA_UNKNOWN, SKIP_ICON, UNKNOWN, Ok, Skip,
                                  Verification, criteria_source, decide, mode_for,
                                  write_skip_record)
from agent_factory.gitref import Located, Unavailable
from prompt import build_verify_prompt

Q = "agent:queue:x"
SHA = "5176388a4ed8d96c358b2d0af9f6b285bbaa9730"
SHA2 = "ced9938f02404aa530804879db5ee096b722172a"


class Store:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.claimed: set[str] = set()
        self.enqueue_fails = False
        self.claim_boom_on = ""

    async def put(self, key, value, ttl): self.kv[key] = value
    async def get(self, key): return self.kv.get(key)
    async def drop(self, key):
        self.kv.pop(key, None); self.claimed.discard(key)

    async def claim(self, key, ttl):
        # Стор умеет молчать выборочно: захват постановки и метки ворот —
        # разные ключи, и падение одного не должно требовать падения всех.
        if self.claim_boom_on and key.startswith(self.claim_boom_on):
            raise RuntimeError("redis молчит")
        if key in self.claimed:
            return False
        self.claimed.add(key); return True

    async def enqueue(self, queue, element):
        if self.enqueue_fails:
            raise RuntimeError("redis молчит")
        self.lists.setdefault(queue, []).append(element)


class Feed:
    def __init__(self):
        self.repainted = []

    async def repaint(self, issue_id, status=None, mark=None, buttons=None):
        self.repainted.append((issue_id, mark)); return 1


class Transport:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None, rich=False):
        self.sent.append((chat_id, text)); return "m1"


class Repos:
    """Площадка: отвечает заготовленным, помнит, о чём спрашивали."""

    def __init__(self, answer=UNKNOWN, boom=False):
        self.answer = answer
        self.boom = boom
        self.asked = []

    def locate(self, ident, ref="", sha="", project_path=""):
        self.asked.append((ident, ref, sha, project_path))
        if self.boom:
            raise Unavailable("хостинг молчит")
        return self.answer


class Hosting:
    def __init__(self, answer):
        self.answer = answer

    def landed(self, payload):
        return self.answer


ISSUE = {"id": "uuid-20", "identifier": "DEV-20", "title": "Уведомления",
         "url": "https://linear.app/x/DEV-20",
         "description": "## Что сделать\n\nвынести\n\n## Критерий приёмки\n\n| что |"}

LANDED = {"identifier": "DEV-20", "commit_sha": SHA, "ref": "main",
          "project_path": "org/dok-engine", "deploy_jobs": ["deploy"],
          "pipeline_id": "1", "pipeline_url": "https://gh/run/1"}


def branch(sha=SHA, ref="agent/DEV-20", ancestor=False):
    return Located(repo="/r/dok-engine", ref=ref, sha=sha, ancestor=ancestor, when=1)


def event(updated="2026-09-13T10:00:00.000Z", ident="DEV-20"):
    return {"type": "Issue", "action": "update", "updatedFrom": {"stateId": "было"},
            "data": {"id": "uuid-20", "identifier": ident, "title": "Уведомления",
                     "url": "https://linear.app/x/DEV-20", "updatedAt": updated,
                     "state": {"name": "Verifying", "type": "started"}}}


class Gh:
    """Хостинг: вливается ли ветка задачи (DEV-109). `None` — не ответил."""

    def __init__(self, mergeable=None, boom=False):
        self.mergeable = mergeable
        self.boom = boom
        self.asked: list[str] = []

    def open_request(self, ident):
        self.asked.append(ident)
        if self.boom:
            raise RuntimeError("gh: HTTP 502")
        return {"number": "23", "repo": "org/dok-engine", "url": "https://gh/pr/23",
                "source_ref": f"agent/{ident}", "target_ref": "main",
                "title": "…", "mergeable": self.mergeable}


def mk(tmp_path, *, issue=ISSUE, located=UNKNOWN, lookup_boom=False, git_boom=False,
       deployment=None, work_root=True, hosting=None):
    st = Store()

    def lookup(ident):
        if lookup_boom:
            raise RuntimeError("трекер лёг")
        return issue

    v = Verification(st, Q, deployment, lookup=lookup, feed=Feed(), transport=Transport(),
                     notify_chat="chat-1", repos=Repos(located, boom=git_boom),
                     work_root=str(tmp_path) if work_root else "", hosting=hosting)
    return st, v


def queued(st):
    return [json.loads(x) for x in st.lists.get(Q, [])]


def run(coro):
    return asyncio.run(coro)


def skip_records(tmp_path):
    return sorted(p for p in Path(tmp_path).glob("*-verify-*/verdict.txt"))


# ── ворота зовутся до постановки, в обеих точках ─────────────────────────────

def test_the_gate_is_called_before_each_queueing():
    """Не «где-то в модуле», а перед КАЖДЫМ `store.enqueue` обоих входов.
    Убери вызов из одного — тест покраснеет, потому что порядок в исходнике
    проверяется по обоим методам."""
    for method in (Verification.on_state, Verification.on_event):
        src = inspect.getsource(method)
        assert "self.preflight(" in src, method.__name__
        assert src.index("self.preflight(") < src.index("store.enqueue("), method.__name__


def test_without_a_task_in_the_tracker_no_session_is_raised_by_state(tmp_path, caplog):
    st, v = mk(tmp_path, issue=None)
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(v.on_state(event(ident="DEV-9999"))) == "skipped"
    assert not queued(st)
    assert "DEV-9999 нет в трекере" in caplog.text
    # verdict.txt написан кодом, в той же форме, что у сессии.
    (rec,) = skip_records(tmp_path)
    assert rec.read_text().startswith("SKIP задачи DEV-9999 нет в трекере")
    assert rec.parent.name.startswith("DEV-9999-verify-")


def test_without_a_task_in_the_tracker_no_session_is_raised_by_event(tmp_path):
    st, v = mk(tmp_path, issue=None, deployment=Hosting({**LANDED, "identifier": "DEV-9999"}))
    assert run(v.on_event({"x": 1})) == "skipped"
    assert not queued(st)
    # Метку коммита сняли: появится задача — тот же коммит проверится.
    assert Verification.verified_key("DEV-9999", SHA) not in st.claimed


def test_without_a_branch_no_session_is_raised(tmp_path, caplog):
    st, v = mk(tmp_path, located=None)
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(v.on_state(event())) == "skipped"
    assert not queued(st)
    assert "ни ветки agent/DEV-20, ни коммита" in caplog.text
    (rec,) = skip_records(tmp_path)
    assert "ни ветки agent/DEV-20" in rec.read_text()


def test_a_repeat_of_the_same_commit_is_verified_again_with_the_prior_verdict(tmp_path):
    """Та же голова ветки, второй перевод в проверку → СТАВИМ снова, а прежний
    вердикт едет фактом.

    Решение владельца 15.09.2026: повторные проверки находят пропущенное. Из
    девяти повторов в журнале в пяти второй вердикт отличался от первого, а у
    DEV-16 и DEV-18 вторая проверка нашла поломки, которые первая пропустила."""
    st, v = mk(tmp_path, located=branch())
    assert run(v.on_state(event("2026-09-13T20:46:00.000Z"))) == "queued"
    (task,) = queued(st)
    assert task["commit_sha"] == SHA
    # Вердикт пришёл по мосту — с коммитом, как его шлёт раннер.
    run(v.on_verdict({"issue_id": "uuid-20", "identifier": "DEV-20", "icon": "🟡",
                      "verdict": "выкат доехал, поведение проверить было нечем",
                      "commit_sha": SHA, "line": "PARTIAL 903 теста зелёные, PR открыт"}))
    assert run(v.on_state(event("2026-09-13T23:53:00.000Z"))) == "queued"
    assert len(queued(st)) == 2
    # Прежний вердикт — факт для проверяющего, а не повод не ставить.
    assert queued(st)[-1]["prior_verdict"] == "PARTIAL 903 теста зелёные, PR открыт"


def test_a_new_commit_of_the_same_task_is_verified_again(tmp_path):
    st, v = mk(tmp_path, located=branch())
    run(v.on_state(event("2026-09-13T20:46:00.000Z")))
    v.repos.answer = branch(sha=SHA2)
    assert run(v.on_state(event("2026-09-14T02:47:00.000Z"))) == "queued"
    assert [t["commit_sha"] for t in queued(st)] == [SHA, SHA2]


def test_a_repeat_before_the_verdict_arrives_is_not_a_second_check(tmp_path, caplog):
    """Повтор того же коммита ДО того, как пришёл вердикт, — не вторая
    проверка, а дубль первой: отказываем и называем причину (DEV-110).

    До 16.09.2026 этот случай ставил вторую сессию, и ровно он уничтожил
    запись исхода DEV-106: выкат и статус пришли на один коммит 7715c223 с
    разницей в 52 секунды, обе сессии поднялись в два слота и писали в один
    каталог. Две проверки одного коммита, идущие в одно время, отвечают на
    один вопрос по одним данным — второму ответу взяться неоткуда.

    Повтор ПОСЛЕ вердикта остаётся разрешённым, это соседний тест: там захват
    снимает сам вердикт.
    """
    st, v = mk(tmp_path, located=branch())
    run(v.on_state(event("2026-09-13T20:46:00.000Z")))
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(v.on_state(event("2026-09-13T20:47:00.000Z"))) == "deduped"
    assert len(queued(st)) == 1
    assert f"коммита {SHA[:8]} уже поставлена" in caplog.text


# ── общий захват постановки: оба входа, одна сессия (DEV-110) ────────────────
#
# 15.09.2026 DEV-106 встала в очередь дважды: `выкат DEV-106 (7715c223) →
# проверяльщик в очереди` в 23:23:06 и `проверка DEV-106: статус →
# проверяльщик в очереди (режим branch, 7715c223)` в 23:23:58. Один коммит,
# два входа, две метки — и оба дедупа прошли, потому что метки у входов
# РАЗНЫЕ. Ниже — тот же порядок событий, в обе стороны.

def test_both_entrances_on_one_commit_raise_one_session(tmp_path, caplog):
    """Выкат, следом статус — как было у DEV-106."""
    st, v = mk(tmp_path, located=branch(), deployment=Hosting(LANDED))
    assert run(v.on_event({"x": 1})) == "queued"
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(v.on_state(event())) == "deduped"
    assert len(queued(st)) == 1
    # Причина названа, и в ней — коммит: молчаливый отказ неотличим от
    # потерянного события, а его и искали бы вместо гонки.
    assert f"коммита {SHA[:8]} уже поставлена" in caplog.text


def test_both_entrances_in_the_other_order_raise_one_session_too(tmp_path):
    """Статус, следом выкат — эта сторона была закрыта и ДО DEV-110, и тест
    держит её закрытой, а не доказывает захват.

    Дыра односторонняя, и это стоит знать: ворота входа по статусу сами
    занимают метку коммита (`preflight` → `claim`), поэтому пришедший следом
    выкат отбивался своим же дедупом. В обратную сторону метку занимал выкат,
    а ворота статуса, не сумев её взять, читали это как «вердикта ещё нет» и
    ставили вторую сессию — так и вышло у DEV-106.
    """
    st, v = mk(tmp_path, located=branch(), deployment=Hosting(LANDED))
    assert run(v.on_state(event())) == "queued"
    assert run(v.on_event({"x": 1})) == "deduped"
    assert len(queued(st)) == 1


def test_the_verdict_releases_the_claim_so_a_repeat_is_allowed(tmp_path):
    """Захват держит «поставлена и не ответила», а не «этот коммит видели».

    Пришёл вердикт — повтор того же коммита снова ставится: это решение
    владельца 15.09.2026 (из девяти повторов в пяти второй вердикт отличался
    от первого), и захват его не отменяет.
    """
    st, v = mk(tmp_path, located=branch(), deployment=Hosting(LANDED))
    assert run(v.on_state(event("2026-09-13T20:46:00.000Z"))) == "queued"
    run(v.on_verdict({"issue_id": "uuid-20", "identifier": "DEV-20", "icon": "🟡",
                      "verdict": "поведение проверить было нечем", "commit_sha": SHA,
                      "line": "PARTIAL стенда нет"}))
    assert run(v.on_state(event("2026-09-13T23:53:00.000Z"))) == "queued"
    assert len(queued(st)) == 2


def test_a_failed_enqueue_releases_the_common_claim(tmp_path):
    """Захват ставится ДО очереди. Не снять его на провале постановки значит
    запереть проверку коммита на два часа при том, что сессии не было."""
    st, v = mk(tmp_path, located=branch())
    st.enqueue_fails = True
    assert run(v.on_state(event("2026-09-13T20:46:00.000Z"))) == "enqueue-failed"
    st.enqueue_fails = False
    assert run(v.on_state(event("2026-09-13T20:47:00.000Z"))) == "queued"


def test_a_silent_store_does_not_lock_the_check_out(tmp_path):
    """Стор молчит на захвате → 503 приёмнику, трекер повторит доставку, и
    повтор обязан пройти.

    Ловушка тут в том, что метка перехода и метка коммита к этому моменту уже
    заняты воротами: не сняв их, мы ответили бы «повторите» и сами же отбили
    бы повтор как дубль — сессии не было бы вовсе, и в журнале это выглядело
    бы как обычный дедуп.
    """
    st, v = mk(tmp_path, located=branch())
    st.claim_boom_on = "agent:verify:pending:"
    assert run(v.on_state(event("2026-09-13T20:46:00.000Z"))) == "enqueue-failed"
    assert not queued(st)
    st.claim_boom_on = ""
    assert run(v.on_state(event("2026-09-13T20:46:00.000Z"))) == "queued"


def test_a_silent_store_does_not_lock_the_commit_out_by_deploy(tmp_path):
    """То же со стороны выката: хостинг не ретраит вовсе, и метка коммита,
    оставшись занятой, заперла бы проверку на 30 суток — обоими входами."""
    st, v = mk(tmp_path, located=branch(), deployment=Hosting(LANDED))
    st.claim_boom_on = "agent:verify:pending:"
    assert run(v.on_event({"x": 1})) == "enqueue-failed"
    assert Verification.verified_key("DEV-20", SHA) not in st.claimed
    st.claim_boom_on = ""
    assert run(v.on_state(event())) == "queued"


def test_without_a_commit_nothing_is_locked(tmp_path):
    """Коммит не сверен (git молчит, клонов площадки нет) — отличить повтор
    от новой работы нечем, и запирать нельзя: ворота в этом же случае сессию
    ставят, а не отсеивают."""
    st, v = mk(tmp_path, git_boom=True)
    assert run(v.on_state(event("2026-09-13T20:46:00.000Z"))) == "queued"
    assert run(v.on_state(event("2026-09-13T20:47:00.000Z"))) == "queued"
    assert len(queued(st)) == 2


# ── режим — по предковости ───────────────────────────────────────────────────

def test_a_branch_not_in_main_gets_the_branch_contract(tmp_path):
    st, v = mk(tmp_path, located=branch(ancestor=False))
    run(v.on_state(event()))
    (task,) = queued(st)
    assert task["trigger"] == "branch"
    assert "ДО МЕРЖА" in build_verify_prompt(task, Path("/w"), Path("/r"))


def test_a_merged_commit_gets_the_deploy_contract_even_by_state(tmp_path):
    st, v = mk(tmp_path, located=branch(ancestor=True))
    run(v.on_state(event()))
    (task,) = queued(st)
    assert task["trigger"] == "state"
    assert "ПОСЛЕ МЕРЖА" in build_verify_prompt(task, Path("/w"), Path("/r"))


def test_the_event_trigger_does_not_decide_the_mode():
    """Подмени предковость на `trigger` — режимы перепутаются: событие
    зелёного CI на ветке говорит `branch`, а коммит уже влит."""
    assert mode_for("event", "branch", ancestor=True) == "deploy"
    assert mode_for("event", "", ancestor=False) == "branch"
    assert mode_for("event", "build", ancestor=True) == "build"
    assert mode_for("state", "branch", ancestor=True) == "state"
    # Предковость неизвестна — только тогда слово события что-то значит.
    assert mode_for("event", "branch", ancestor=None) == "branch"
    assert mode_for("event", "", ancestor=None) == "deploy"
    assert mode_for("state", "branch", ancestor=None) == "branch"


def test_a_deploy_event_on_an_unmerged_commit_is_a_branch_check(tmp_path):
    st, v = mk(tmp_path, located=branch(ancestor=False),
               deployment=Hosting({**LANDED, "trigger": "branch", "deploy_jobs": []}))
    run(v.on_event({}))
    (task,) = queued(st)
    assert task["trigger"] == "branch"


def test_a_deploy_event_on_a_merged_commit_is_a_deploy_check(tmp_path):
    st, v = mk(tmp_path, located=branch(ref="main", ancestor=True),
               deployment=Hosting({**LANDED, "trigger": "branch"}))
    run(v.on_event({}))
    (task,) = queued(st)
    assert task["trigger"] == "deploy"


# ── ворота не режут полезное ─────────────────────────────────────────────────

def test_no_deploy_and_an_open_pr_is_not_a_reason_to_skip(tmp_path):
    """«Выката не было, PR открыт» — это и есть проверка ветки, ради которой
    всё заведено."""
    st, v = mk(tmp_path, located=branch(ancestor=False))
    assert run(v.on_state(event())) == "queued"
    (task,) = queued(st)
    assert task["trigger"] == "branch" and task["commit_sha"] == SHA


def test_a_missing_criterion_does_not_cancel_the_session(tmp_path):
    st, v = mk(tmp_path, issue={**ISSUE, "description": "просто текст"}, located=branch())
    assert run(v.on_state(event())) == "queued"
    assert queued(st)[0]["criteria_source"] == CRITERIA_NONE


def test_the_source_of_the_criterion_is_written_into_the_task(tmp_path):
    st, v = mk(tmp_path, located=branch())
    run(v.on_state(event()))
    assert queued(st)[0]["criteria_source"] == CRITERIA_TASK


def test_criteria_source_reads_the_description():
    assert criteria_source("## Критерий приёмки\n| x |") == CRITERIA_TASK
    assert criteria_source("## Критерии приёмки") == CRITERIA_TASK
    assert criteria_source("## Что сделать\n- то") == CRITERIA_FALLBACK
    assert criteria_source("## Что решить") == CRITERIA_FALLBACK
    assert criteria_source("## Ожидаемое поведение") == CRITERIA_FALLBACK
    assert criteria_source("## Проблема\nвсё плохо") == CRITERIA_NONE
    assert criteria_source("") == CRITERIA_NONE


def test_a_silent_tracker_does_not_skip(tmp_path):
    """Трекер лёг — это не «задачи нет». Сессия ставится, а что не сверено —
    в записи."""
    st, v = mk(tmp_path, lookup_boom=True, located=branch())
    assert run(v.on_state(event())) == "queued"
    (task,) = queued(st)
    assert task["criteria_source"] == CRITERIA_UNKNOWN
    assert any("трекер не ответил" in n for n in task["preflight_notes"])


def test_silent_git_does_not_skip_and_keeps_the_old_mode(tmp_path):
    st, v = mk(tmp_path, git_boom=True)
    assert run(v.on_state(event())) == "queued"
    (task,) = queued(st)
    assert task["trigger"] == "branch" and "commit_sha" not in task
    assert any("git не ответил" in n for n in task["preflight_notes"])


def test_without_git_at_all_the_task_still_goes_out(tmp_path):
    st = Store()
    v = Verification(st, Q, lookup=lambda i: ISSUE)
    assert run(v.on_state(event())) == "queued"
    assert any("git не подключён" in n for n in queued(st)[0]["preflight_notes"])


# ── что ворота кладут в задачу ───────────────────────────────────────────────

def test_the_exact_branch_and_commit_replace_the_convention(tmp_path):
    """`agent/DEV-66` смержена, работа лежит в `agent/DEV-66-v2` — ворота
    нашли её и назвали проверяющему точную ветку и коммит."""
    st, v = mk(tmp_path, located=branch(ref="agent/DEV-20-v2", sha=SHA2))
    run(v.on_state(event()))
    (task,) = queued(st)
    assert task["ref"] == "agent/DEV-20-v2" and task["commit_sha"] == SHA2
    assert "Проверяемый коммит: ced9938f" in build_verify_prompt(task, Path("/w"), Path("/r"))


def test_the_issue_address_comes_from_the_tracker_when_the_event_has_none(tmp_path):
    st, v = mk(tmp_path, located=branch(ref="main", ancestor=True),
               deployment=Hosting(LANDED))
    run(v.on_event({}))
    (task,) = queued(st)
    assert task["issue_id"] == "uuid-20" and task["title"] == "Уведомления"


def test_the_rounds_of_the_task_are_written_into_the_record(tmp_path):
    for stamp in ("20260913-211228", "20260913-214657", "20260914-003841"):
        d = tmp_path / f"DEV-20-{stamp}"
        d.mkdir()
        (d / "transcript.jsonl").write_text(json.dumps({"type": "result", "total_cost_usd": 1}))
    st, v = mk(tmp_path, located=branch())
    run(v.on_state(event()))
    assert queued(st)[0]["rounds"] == 3


def test_a_skip_is_shown_in_the_issue_message(tmp_path):
    st, v = mk(tmp_path, located=None)
    run(v.on_state(event()))
    ((issue_id, mark),) = v.feed.repainted
    assert issue_id == "uuid-20" and mark.startswith(f"{SKIP_ICON} проверка не поднята")


def test_a_skip_without_an_issue_goes_to_the_common_chat(tmp_path):
    st, v = mk(tmp_path, issue=None, deployment=Hosting({**LANDED, "identifier": "DEV-9999"}))
    run(v.on_event({}))
    ((chat, text),) = v.transport.sent
    assert chat == "chat-1" and "DEV-9999" in text and "нет в трекере" in text


def test_a_failed_queueing_after_the_gate_releases_the_commit_mark(tmp_path):
    st, v = mk(tmp_path, located=branch())
    st.enqueue_fails = True
    assert run(v.on_state(event())) == "enqueue-failed"
    assert Verification.verified_key("DEV-20", SHA) not in st.claimed
    st.enqueue_fails = False
    assert run(v.on_state(event("2026-09-13T20:47:00.000Z"))) == "queued"


def test_a_skip_record_is_a_zero_cost_run_in_the_journal(tmp_path):
    path = write_skip_record(str(tmp_path), {"identifier": "DEV-20", "commit_sha": SHA},
                             Skip("коммит уже проверяли", prior="OK всё сошлось", seen=True))
    (got,) = sessions.walk(str(tmp_path))
    assert got["task"] == "DEV-20" and got["род"] == "verify"
    assert got["стоимость"] == 0 and got["исход"] == "CANCELLED"
    assert not got["идёт"] and not got["брошена"]
    # Отсев — не рабочий заход: круги от него не растут.
    (row,) = runs.by_task([got])
    assert row["рабочих заходов"] == 0 and row["заходов"] == 1
    assert json.loads((Path(path) / "stats.json").read_text())["коммит"] == SHA


# ── прогон по журналу записанных сессий ──────────────────────────────────────

JOURNAL = Path(__file__).parent / "testdata" / "verify-journal-client-b-2026-09-13.json"


def replay(records):
    """Решение ворот по фактам «на момент постановки», в порядке журнала.
    Стор — множество проверенных (задача, коммит), как в redis."""
    seen: dict[tuple, str] = {}
    out = []
    for r in records:
        located = (Located(repo="", ref=r["ref"], sha=r["sha"], ancestor=r["ancestor"])
                   if r["found"] else None)
        key = (r["ident"], r["sha"])
        verdict = decide(entry=r["entry"], hint=r["hint"], ident=r["ident"], ref=r["ref"],
                         exists=r["exists"], located=located,
                         seen=seen.get(key) if key in seen else None,
                         criteria=r["criteria"])
        if isinstance(verdict, Ok):
            seen[key] = r["verdict"]
        out.append((r, verdict))
    return out


@pytest.fixture(scope="module")
def journal():
    return json.loads(JOURNAL.read_text())


def test_the_journal_has_the_fifty_six_recorded_sessions(journal):
    assert len(journal) == 56
    assert all(r["session"] for r in journal)


def test_the_seven_no_deploy_sessions_get_the_branch_contract_not_a_skip(journal):
    """Семь сессий «выката не было» 13–14.09, первые на своём коммите и
    давшие проверку поведения в ветке (1216 тестов у DEV-16, 903 у DEV-43,
    340 с шестью мутациями у DEV-48). Ворота обязаны их ПОДНЯТЬ в режиме
    ветки."""
    seven = {"DEV-16-verify-20260914-005411", "DEV-18-verify-20260914-005943",
             "DEV-43-verify-20260913-205432", "DEV-46-verify-20260914-000721",
             "DEV-48-verify-20260913-205728", "DEV-20-verify-20260913-204803",
             "DEV-59-verify-20260913-192231"}
    got = {r["session"]: v for r, v in replay(journal) if r["session"] in seven}
    assert set(got) == seven
    for name, v in got.items():
        assert isinstance(v, Ok) and v.mode == "branch", (name, v)


def test_what_the_gate_cuts_in_the_journal_and_nothing_else(journal):
    """4 из 56: только задачи, которых в трекере ещё не было (DEV-77, 80, 83
    и 84 завели ПОСЛЕ их проверок). Всё остальное — 52 сессии — поднято.

    Повтор коммита НЕ отсекается: решение владельца 15.09.2026. Из девяти
    повторов в пяти второй вердикт отличался от первого, а у DEV-16 и DEV-18
    вторая проверка нашла поломки, пропущенные первой. Числа закреплены
    намеренно: изменились — значит изменилось правило."""
    skipped = {r["session"]: v for r, v in replay(journal) if isinstance(v, Skip)}
    no_task = {n for n, v in skipped.items() if "нет в трекере" in v.reason}
    assert no_task == {"DEV-77-verify-20260913-192059", "DEV-80-verify-20260913-230650",
                       "DEV-83-verify-20260914-010142", "DEV-84-verify-20260914-011923"}
    assert not [n for n, v in skipped.items() if v.seen], "повторы отсекаться не должны"
    assert len(skipped) == 4
    assert sum(isinstance(v, Ok) for _, v in replay(journal)) == 52


def test_no_first_check_of_a_commit_is_cut_by_no_deploy_wording(journal):
    """Ни одна сессия, первая на своём коммите, не отсечена — какими бы
    словами про выкат ни начинался её вердикт."""
    first = set()
    for r, v in replay(journal):
        if (r["ident"], r["sha"]) in first:
            continue
        first.add((r["ident"], r["sha"]))
        if r["exists"] and r["found"]:
            assert isinstance(v, Ok), (r["session"], v)


def test_modes_in_the_journal_follow_ancestry(journal):
    for r, v in replay(journal):
        if not isinstance(v, Ok):
            continue
        if r["ancestor"] is False:
            assert v.mode == "branch", r["session"]
        elif r["entry"] == "state":
            assert v.mode == "state", r["session"]
        else:
            assert v.mode == "deploy", r["session"]


# ── стыковка с DEV-87 и контрактом ───────────────────────────────────────────

def test_the_gate_speaks_the_runner_words_for_the_criterion():
    """Первая строка `expected.md` и ответ ворот ложатся в один `stats.json`
    и сравнимы только на одном словаре."""
    import runner
    assert (CRITERIA_TASK, CRITERIA_FALLBACK, CRITERIA_NONE) == runner.CRITERION_SOURCES


def test_the_rounds_are_told_to_the_checker_as_a_fact():
    """Счёт кругов — код по каталогам раннера, а не сессия по комментариям:
    число едет в контракт строкой фактов, и правило про третий возврат
    отсылает к ней."""
    task = {"identifier": "DEV-20", "trigger": "branch", "ref": "agent/DEV-20", "rounds": 3}
    text = build_verify_prompt(task, Path("/w"), Path("/r"))
    assert "Рабочих заходов по задаче (по журналу раннера, не по комментариям): 3" in text
    flat = " ".join(text.split())
    assert "круги считай по строке «Рабочих заходов по задаче» в фактах выше" in flat
    # Порог — числом из той же настройки, что у ворот и вебхука, а не
    # «третий» прописью: у инсталляции он свой.
    assert "заходов 3 или больше — назначь" in flat
    # Без ворот строки нет — сессия считает по комментариям, как раньше.
    without = build_verify_prompt({"identifier": "DEV-20", "trigger": "branch"},
                                  Path("/w"), Path("/r"))
    assert "Рабочих заходов по задаче (по журналу раннера" not in without


def test_a_commit_named_by_the_event_but_absent_everywhere_is_a_skip(tmp_path, caplog):
    """Событие выката назвало коммит, а его нет ни в одном клоне площадки:
    наблюдать нечего, и причина называет именно коммит, а не ветку."""
    st, v = mk(tmp_path, located=None, deployment=Hosting(LANDED))
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(v.on_event({})) == "skipped"
    assert not queued(st)
    assert f"коммита {SHA[:8]} задачи DEV-20 нет ни в одном клоне" in caplog.text
    # Метка коммита снята: появится коммит — проверится.
    assert Verification.verified_key("DEV-20", SHA) not in st.claimed


# ── повторное unobservable не ставится (DEV-89) ─────────────────────────────
#
# DEV-43: четыре `PARTIAL` подряд за $21,45 — у agent-factory нет стенда,
# наблюдать негде. Кончилась последняя проверка задачи классом `unobservable`
# в той же точке — следующую ворота не поднимают.

def verified(root, ident, stamp, *, cls="", mode="branch",
             verdict="PARTIAL выката не было", outcome="READY_FOR_HUMAN"):
    """След состоявшейся проверки в рабочем каталоге: как его оставляет
    раннер (`stats.json` с классом и точкой, `verdict.txt`)."""
    d = Path(root) / f"{ident}-verify-{stamp}"
    d.mkdir(parents=True)
    (d / "transcript.jsonl").write_text(json.dumps({"type": "result", "total_cost_usd": 1}))
    stats = {"задача": ident, "род": "verify", "исход": outcome}
    if cls:
        stats.update({"класс": cls, "режим": mode})
    (d / "stats.json").write_text(json.dumps(stats, ensure_ascii=False))
    (d / "verdict.txt").write_text(verdict + "\n")
    return d


def skips(tmp_path):
    return [p for p in skip_records(tmp_path) if p.read_text().startswith("SKIP")]


def test_after_an_unobservable_check_the_next_at_the_same_point_is_not_raised(tmp_path, caplog):
    verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable", mode="branch")
    st, v = mk(tmp_path, located=branch())
    with caplog.at_level(logging.INFO):
        assert run(v.on_state(event())) == "skipped"
    assert queued(st) == []
    (rec,) = skips(tmp_path)
    text = rec.read_text()
    assert "unobservable" in text and "до мержа" in text
    assert "прежний вердикт: PARTIAL выката не было" in text
    # Метку коммита ворота поставили сами — и сняли: проверки не было.
    assert Verification.verified_key("DEV-20", SHA) not in st.claimed


def test_the_same_rule_holds_for_the_deploy_event(tmp_path):
    verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable", mode="deploy")
    st, v = mk(tmp_path, located=branch(ref="main", ancestor=True),
               deployment=Hosting(LANDED))
    assert run(v.on_event({})) == "skipped"
    assert queued(st) == [] and len(skips(tmp_path)) == 1
    assert Verification.verified_key("DEV-20", SHA) not in st.claimed


def test_a_different_point_is_checked_again(tmp_path):
    """«Выката не было» до мержа не значит, что прод после выката наблюдать
    негде: DEV-59, DEV-20, DEV-4, DEV-48 — до мержа нечего, после — `OK`."""
    verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable", mode="branch")
    st, v = mk(tmp_path, located=branch(ref="main", ancestor=True))
    assert run(v.on_state(event())) == "queued"
    assert queued(st)[0]["trigger"] == "state"


@pytest.mark.parametrize("cls, verdict", [
    ("implementation", "FAIL ручка отдала 500"),
    ("specification", "FAIL/spec три пункта недостижимы"),
    ("", "PARTIAL выката не было"),        # проверка до классов: поля нет
])
def test_other_classes_and_checks_before_the_classes_do_not_block(tmp_path, cls, verdict):
    verified(tmp_path, "DEV-20", "20260913-205432", cls=cls, verdict=verdict)
    st, v = mk(tmp_path, located=branch())
    assert run(v.on_state(event())) == "queued"
    assert len(queued(st)) == 1 and skips(tmp_path) == []


def test_a_skip_does_not_hide_the_check_that_caused_it(tmp_path):
    # Отсев — не проверка: второй перевод в проверку видит ту же
    # unobservable-проверку и тоже не поднимает сессию.
    verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable")
    st, v = mk(tmp_path, located=branch())
    assert run(v.on_state(event())) == "skipped"
    assert run(v.on_state(event("2026-09-13T21:00:00.000Z"))) == "skipped"
    # Записей отсева может оказаться одна: `write_skip_record` именует
    # каталог с точностью до секунды, и два отсева в одну секунду ложатся в
    # один. Правило — в исходах выше; здесь важно, что след есть.
    assert queued(st) == [] and len(skips(tmp_path)) >= 1


def test_a_running_check_is_not_the_last_one(tmp_path):
    # Идущая сессия исхода ещё не записала; последняя состоявшаяся — та, что до неё.
    verified(tmp_path, "DEV-20", "20260913-205432", cls="implementation", verdict="FAIL x")
    d = tmp_path / "DEV-20-verify-20260914-000003"
    d.mkdir()
    (d / "transcript.jsonl").write_text("")
    assert runs.last_verify(str(tmp_path), "DEV-20")["класс"] == "implementation"


def test_last_verify_reads_the_newest_finished_check(tmp_path):
    verified(tmp_path, "DEV-20", "20260913-205432", cls="implementation", verdict="FAIL x")
    verified(tmp_path, "DEV-20", "20260914-000003", cls="unobservable", verdict="PARTIAL y")
    write_skip_record(str(tmp_path), {"identifier": "DEV-20"}, Skip("повтор"))
    got = runs.last_verify(str(tmp_path), "DEV-20")
    assert got["класс"] == "unobservable" and got["режим"] == "branch"
    assert got["вердикт"] == "PARTIAL y" and got["name"].endswith("000003")
    assert runs.last_verify(str(tmp_path), "DEV-21") == {}
    assert runs.last_verify("", "DEV-20") == {}


# ── блокировку снимает сигнал человека (DEV-105) ────────────────────────────
#
# Точка меняется только мержем, а проверка после мержа может быть выключена —
# тогда `unobservable` запирал задачу навсегда (DEV-66: препятствием были
# мёртвые токены личностей, их починили в тот же вечер). Условие снятия —
# явный сигнал, и он отпускает ТОЛЬКО проверки, кончившиеся раньше него.

def test_a_signal_newer_than_the_blocking_check_lets_the_next_one_through(tmp_path, caplog):
    d = verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable", mode="branch")
    was = {f.name: f.read_bytes() for f in d.iterdir()}
    runs.clear_block(str(tmp_path), "DEV-20", "owner-1", "токены починены",
                     at=time.time() + 60)
    st, v = mk(tmp_path, located=branch())
    with caplog.at_level(logging.INFO, logger="intake"):
        assert run(v.on_state(event())) == "queued"
    task = queued(st)[0]
    assert task["trigger"] == "branch"
    # Видно, на каком основании сессия поднята: сторож снят, а не обойдён.
    note = [n for n in task.get("preflight_notes") or [] if "снята" in n]
    assert note and "owner-1" in note[0] and "токены починены" in note[0]
    assert "блокировка «unobservable» снята сигналом" in caplog.text
    # Запись прошлой проверки НЕ ТРОНУТА: она свидетельство состоявшегося.
    assert {f.name: f.read_bytes() for f in d.iterdir()} == was


def test_a_signal_older_than_the_blocking_check_does_not_release_it(tmp_path):
    """Лежалый сигнал не отпирает задним числом: проверка после него снова
    сказала «наблюдать негде», и сторож стоит."""
    verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable", mode="branch")
    runs.clear_block(str(tmp_path), "DEV-20", "owner-1", "чинили вчера",
                     at=time.time() - 60)
    st, v = mk(tmp_path, located=branch())
    assert run(v.on_state(event())) == "skipped"
    assert queued(st) == []
    (rec,) = skips(tmp_path)
    assert "старше этой проверки" in rec.read_text()


def test_the_next_unobservable_check_arms_the_block_again(tmp_path):
    """Сигнал не одноразовый ключ и не вечная индульгенция: он отпускает то,
    что было ДО него. Новая `unobservable` запирает задачу снова."""
    verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable", mode="branch")
    runs.clear_block(str(tmp_path), "DEV-20", "owner-1", "стенд подняли",
                     at=time.time() - 60)
    verified(tmp_path, "DEV-20", "20260914-000003", cls="unobservable", mode="branch",
             verdict="PARTIAL стенд снова занят")
    st, v = mk(tmp_path, located=branch())
    assert run(v.on_state(event())) == "skipped"
    assert queued(st) == []


def test_the_skip_says_how_to_clear_the_block(tmp_path):
    """Иначе про условие снятия знает только код: человек видит «не ставится»
    и ничего не может сделать — ровно то, с чего началась DEV-105."""
    verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable", mode="branch")
    st, v = mk(tmp_path, located=branch())
    assert run(v.on_state(event())) == "skipped"
    (rec,) = skips(tmp_path)
    assert f"/{runs.UNBLOCK_COMMAND} DEV-20" in rec.read_text()


def test_a_signal_does_not_raise_anything_by_itself(tmp_path):
    """Сигнал — факт, а не постановка: ворота продолжают отсеивать по своим
    правилам, и задача без ветки и коммита сессии не получает."""
    verified(tmp_path, "DEV-20", "20260913-205432", cls="unobservable", mode="branch")
    runs.clear_block(str(tmp_path), "DEV-20", "owner-1", "токены починены",
                     at=time.time() + 60)
    st, v = mk(tmp_path, located=None)
    assert run(v.on_state(event())) == "skipped"
    assert queued(st) == []


def test_a_signal_is_read_by_the_task_it_names(tmp_path):
    runs.clear_block(str(tmp_path), "DEV-20", "owner-1", "снято", at=111.0)
    assert runs.clearance(str(tmp_path), "DEV-20")["когда"] == 111.0
    assert runs.clearance(str(tmp_path), "dev-20")["почему"] == "снято"
    assert runs.clearance(str(tmp_path), "DEV-21") == {}
    assert runs.clearance("", "DEV-20") == {} and runs.clearance(str(tmp_path), "") == {}
    # Идентификатор приезжает из сообщения человека и становится именем файла.
    with pytest.raises(ValueError):
        runs.clear_block(str(tmp_path), "../../etc/passwd", "owner-1", "снято")
    # Битую запись читаем как «сигнала не было», а не падаем на воротах.
    (tmp_path / "_clearances" / "DEV-20.json").write_text("{не json")
    assert runs.clearance(str(tmp_path), "DEV-20") == {}


def test_the_newest_check_wins_even_when_both_start_in_the_same_second(tmp_path):
    """Обе записи заведены в одну секунду — порядок решает штамп в имени, а не
    то, в каком порядке каталоги отдала файловая система."""
    verified(tmp_path, "DEV-20", "20260914-000003", cls="unobservable", verdict="PARTIAL y")
    verified(tmp_path, "DEV-20", "20260913-205432", cls="implementation", verdict="FAIL x")
    for d in (tmp_path / "DEV-20-verify-20260914-000003",
              tmp_path / "DEV-20-verify-20260913-205432"):
        os.utime(d, (1_700_000_000, 1_700_000_000))
    assert runs.last_verify(str(tmp_path), "DEV-20")["класс"] == "unobservable"


@pytest.mark.parametrize("cleared, prior, skipped", [
    (0.0, 100.0, True),        # сигнала не было — сторож держит
    (90.0, 100.0, True),       # сигнал старше блокирующей проверки
    (100.0, 100.0, True),      # ровно та же секунда — не позже, значит не снимает
    (110.0, 100.0, False),     # сигнал свежее — ставим
])
def test_decide_releases_only_on_a_signal_newer_than_the_check(cleared, prior, skipped):
    got = decide(entry="state", hint="branch", ident="DEV-20", ref="agent/DEV-20",
                 exists=True, located=branch(ancestor=False), seen=None,
                 prior_class="unobservable", prior_mode="branch",
                 prior_line="PARTIAL выката не было", prior_at=prior,
                 cleared_at=cleared, cleared_by="owner-1", cleared_why="токены")
    assert isinstance(got, Skip) == skipped


@pytest.mark.parametrize("prior_class, prior_mode, ancestor, skipped", [
    ("unobservable", "branch", False, True),     # та же точка — до мержа
    ("unobservable", "deploy", True, True),      # та же точка — после мержа
    ("unobservable", "state", True, True),       # state и deploy — одна точка
    ("unobservable", "branch", True, False),     # точка сменилась
    ("unobservable", "deploy", False, False),
    ("specification", "branch", False, False),
    ("implementation", "branch", False, False),
    ("", "", False, False),
])
def test_decide_blocks_only_the_same_class_at_the_same_point(prior_class, prior_mode,
                                                             ancestor, skipped):
    got = decide(entry="state", hint="branch", ident="DEV-20", ref="agent/DEV-20",
                 exists=True, located=branch(ancestor=ancestor), seen=None,
                 prior_class=prior_class, prior_mode=prior_mode,
                 prior_line="PARTIAL выката не было", claim="agent:verify:DEV-20:x")
    assert isinstance(got, Skip) == skipped
    if skipped:
        assert got.prior == "PARTIAL выката не было" and got.claim == "agent:verify:DEV-20:x"
        assert not got.seen


def test_the_rule_sits_at_the_gate_fork_not_in_the_runner():
    # Та же развилка, что ворота DEV-86: решение — в `decide`, а не в
    # раннере и не в сессии.
    src = inspect.getsource(deploy.decide)
    assert "CLASS_UNOBSERVABLE" in src and "point_of(prior_mode) == point_of(mode)" in src


# ── сливаемость ветки: факт для маршрута вердикта (DEV-109) ─────────────────
#
# Стопка «ждёт мержа» обещает, что нажатие сработает, и 15.09.2026 оно не
# сработало трижды. Спрашивают здесь, применяет раннер: ворота собирают факты,
# решений о стопке не принимают.

def test_the_gate_asks_the_hosting_about_a_branch_that_is_not_merged(tmp_path):
    gh = Gh(mergeable=False)
    st, v = mk(tmp_path, located=branch(), hosting=gh)
    run(v.on_state(event()))
    (task,) = queued(st)
    assert gh.asked == ["DEV-20"] and task["mergeable"] is False
    # Сессию это не отменяет: несливаемую ветку проверять можно и нужно.
    assert task["trigger"] == "branch"


def test_a_mergeable_branch_is_named_as_such(tmp_path):
    st, v = mk(tmp_path, located=branch(), hosting=Gh(mergeable=True))
    run(v.on_state(event()))
    assert queued(st)[0]["mergeable"] is True


def test_work_already_in_main_is_not_asked_about_merging(tmp_path):
    # Коммит влит — мержить нечего, и лишний поход в хостинг на каждую
    # проверку выката не нужен.
    gh = Gh(mergeable=False)
    st, v = mk(tmp_path, located=branch(ancestor=True), hosting=gh)
    run(v.on_state(event()))
    assert gh.asked == [] and "mergeable" not in queued(st)[0]


def test_a_silent_hosting_is_a_note_not_a_verdict(tmp_path):
    # Молчание — не отказ: поля в задаче нет вовсе, и раннер прочтёт это как
    # «не спрашивали», а в записи запуска видно, чего ворота не сверили.
    st, v = mk(tmp_path, located=branch(), hosting=Gh(boom=True))
    run(v.on_state(event()))
    (task,) = queued(st)
    assert "mergeable" not in task
    assert any("сливаемость" in n for n in task["preflight_notes"])


def test_without_a_hosting_the_gate_says_so(tmp_path):
    st, v = mk(tmp_path, located=branch())
    run(v.on_state(event()))
    (task,) = queued(st)
    assert "mergeable" not in task
    assert any("сливаемость" in n for n in task["preflight_notes"])


def test_a_missing_hosting_and_a_silent_one_read_differently(tmp_path):
    # Запись запуска читает человек: «не подключён» чинится настройкой,
    # «не ответил» — сетью, и одинаковой строкой их путать нельзя.
    st, v = mk(tmp_path, located=branch())
    run(v.on_state(event()))
    (task,) = queued(st)
    assert any("не подключён" in n for n in task["preflight_notes"])
    st2, v2 = mk(tmp_path, located=branch(), hosting=Gh(boom=True))
    run(v2.on_state(event(updated="2026-09-13T11:00:00.000Z")))
    (task2,) = queued(st2)
    assert any("не ответил" in n for n in task2["preflight_notes"])
