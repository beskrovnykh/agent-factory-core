"""Проверка выката: от события хостинга до вердикта в чате.

Смысл тестов — в границе продукта и инсталляции. Инсталляция отвечает на
один вопрос («это наш выкат?»), всё остальное — заначка, дедуп по коммиту,
постановка проверяльщика, показ вердикта — обязано работать одинаково у
любой инсталляции и молчать у той, где реализации протокола нет.
"""
import asyncio
import json

import pytest

from agent_factory import deploy
from agent_factory.deploy import (LOUD_ICON, VERIFY_RESULTS_KEY, Verification,
                                  remember_task, stashed_task, task_key)


class Store:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.claimed: set[str] = set()
        self.enqueue_fails = False
        self.claim_fails = False
        self.raws: list[str] = []

    async def put(self, key, value, ttl):
        self.kv[key] = value
        self.ttl = ttl

    async def get(self, key): return self.kv.get(key)
    async def drop(self, key):
        self.kv.pop(key, None); self.claimed.discard(key)

    async def claim(self, key, ttl):
        if self.claim_fails:
            raise RuntimeError("redis молчит")
        if key in self.claimed:
            return False
        self.claimed.add(key); return True

    async def enqueue(self, queue, element):
        if self.enqueue_fails:
            raise RuntimeError("redis молчит")
        self.lists.setdefault(queue, []).append(element)

    async def pop_blocking(self, key, timeout):
        assert key == VERIFY_RESULTS_KEY
        if not self.raws:
            raise asyncio.CancelledError
        return self.raws.pop(0)


class Feed:
    def __init__(self, edited=1):
        self.repainted: list[tuple] = []
        self.edited = edited

    async def repaint(self, issue_id, status=None, mark=None, buttons=None):
        self.repainted.append((issue_id, mark))
        return self.edited


class Transport:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, chat_id, text, buttons=None, rich=False):
        self.sent.append((chat_id, text)); return "m1"


class GitHosting:
    """Инсталляция: отвечает заготовленным, считает вопросы."""

    def __init__(self, answer=None, boom=False):
        self.answer = answer
        self.boom = boom
        self.asked = 0

    def landed(self, payload):
        self.asked += 1
        if self.boom:
            raise RuntimeError("payload не той формы")
        return self.answer


LANDED = {"identifier": "TASK-200", "commit_sha": "abc123def456", "ref": "main",
          "project_path": "grp/backend/core", "mr_iid": "17",
          "pipeline_id": 9001, "pipeline_url": "https://gl/p/9001",
          "deploy_jobs": ["deploy-prod"], "deployed_at": "2026-09-12T10:00:00+00:00"}

STASH = {"issue_id": "issue-uuid-1", "identifier": "TASK-200", "kind": "incident",
         "title": "Починить экспорт", "url": "https://linear.app/x/TASK-200",
         "requested_by": "pavel-uuid", "requested_by_name": "Иван",
         "queued_at": "2026-09-01T00:00:00+00:00"}

Q = "agent:queue:x"


def run(coro):
    return asyncio.run(coro)


def mk(answer=LANDED, stash=STASH, deployment=True, **kw):
    st = Store()
    if stash:
        st.kv[task_key(stash["identifier"])] = json.dumps(stash)
    dep = GitHosting(answer) if deployment else None
    v = Verification(st, Q, dep, **kw)
    return st, dep, v


def queued(st):
    return [json.loads(x) for x in st.lists.get(Q, [])]


# ── без реализации протокола ─────────────────────────────────────────────

def test_without_a_deployment_nothing_is_queued_and_nothing_is_said():
    # Вторая инсталляция: адаптера нет вовсе, продукт обязан молчать.
    tp = Transport()
    st, _, v = mk(deployment=False, transport=tp, notify_chat="chat")
    assert run(v.on_event({"object_kind": "pipeline"})) == "no-deployment"
    assert queued(st) == [] and tp.sent == [] and st.claimed == set()


def test_no_source_configured_means_no_deployment(monkeypatch):
    from agent_factory.updater import source_from_env
    monkeypatch.delenv("AGENT_DEPLOYMENT_SOURCE", raising=False)
    assert source_from_env("AGENT_DEPLOYMENT_SOURCE") is None


# ── постановка проверяльщика ─────────────────────────────────────────────

def test_a_landed_deploy_puts_a_verifier_in_the_queue():
    st, _, v = mk()
    assert run(v.on_event({})) == "queued"
    (task,) = queued(st)
    assert task["kind"] == "verify"
    assert task["issue_id"] == "issue-uuid-1" and task["identifier"] == "TASK-200"
    assert task["commit_sha"] == "abc123def456" and task["deploy_jobs"] == ["deploy-prod"]
    assert task["requested_by"] == "pavel-uuid"


def test_the_stash_kind_does_not_leak_into_the_verifier():
    # В заначке рабочая задача, у первого клиента бывает и дежурная. Её kind
    # сюда протекать не должен: поднялась бы дежурная сессия вместо проверки.
    st, _, v = mk()
    run(v.on_event({}))
    assert queued(st)[0]["kind"] == "verify"


def test_queued_at_is_the_moment_of_the_deploy_not_of_the_work():
    # По нему сессия берёт границу «до/после», если хостинг не дал точного
    # времени: время постановки РАБОТЫ — это неделя назад.
    st, _, v = mk()
    run(v.on_event({}))
    assert queued(st)[0]["queued_at"] != STASH["queued_at"]


def test_not_our_deploy_starts_nothing():
    st, dep, v = mk(answer=None)
    assert run(v.on_event({})) == "not-ours"
    assert dep.asked == 1 and queued(st) == [] and st.claimed == set()


def test_an_answer_without_an_identifier_is_not_ours_either():
    st, _, v = mk(answer={"commit_sha": "abc"})
    assert run(v.on_event({})) == "not-ours"
    assert queued(st) == []


def test_a_broken_installation_parser_is_its_error_not_a_foreign_deploy(caplog):
    st = Store()
    v = Verification(st, Q, GitHosting(boom=True))
    assert run(v.on_event({})) == "deployment-failed"
    assert queued(st) == [] and "не отработал" in caplog.text


# ── заначка ──────────────────────────────────────────────────────────────

def test_without_a_stash_the_verifier_is_still_queued(caplog):
    # Заначка живёт 30 дней, а задача умеет пролежать в ревью дольше; ветку
    # agent/TASK-N умеет собрать и человек. Непроверенный выкат хуже, чем
    # вердикт без адреса: у первого клиента так было всегда, и 0.6.13 это
    # сузил молча.
    import logging
    caplog.set_level(logging.INFO, logger="intake")
    st, _, v = mk(stash=None)
    assert run(v.on_event({})) == "queued"
    (task,) = queued(st)
    assert task["kind"] == "verify" and task["identifier"] == "TASK-200"
    assert "issue_id" not in task and "requested_by" not in task
    assert "заначки нет" in caplog.text


def test_a_deploy_without_a_stash_is_still_checked_once():
    # Проверка поставлена — метка остаётся: второй пайплайн на том же
    # коммите не должен поднять вторую сессию, даже если заначка к тому
    # времени появилась.
    st, _, v = mk(stash=None)
    assert run(v.on_event({})) == "queued"
    st.kv[task_key("TASK-200")] = json.dumps(STASH)
    assert run(v.on_event({})) == "deduped"
    assert len(queued(st)) == 1


# ── адрес без заначки ────────────────────────────────────────────────────

def test_without_a_stash_the_issue_is_resolved_by_the_installation():
    # Без issue_id отметка не доедет до сообщения задачи, и раннер уведёт
    # вердикт целиком в личку (TASK-193, 02.09.2026). Инсталляция умеет найти
    # id по идентификатору — продукт спрашивает её.
    asked = []

    def by_identifier(ident):
        asked.append(ident); return "issue-uuid-resolved"

    st, _, v = mk(stash=None, resolve_issue=by_identifier)
    assert run(v.on_event({})) == "queued"
    assert asked == ["TASK-200"]
    assert queued(st)[0]["issue_id"] == "issue-uuid-resolved"


def test_the_resolver_may_be_async():
    async def by_identifier(ident):
        return "issue-uuid-async"

    st, _, v = mk(stash=None, resolve_issue=by_identifier)
    run(v.on_event({}))
    assert queued(st)[0]["issue_id"] == "issue-uuid-async"


def test_with_a_stash_the_tracker_is_not_asked():
    # Лишний сетевой запрос на каждый выкат.
    def must_not(ident):
        raise AssertionError("id уже в заначке")

    st, _, v = mk(resolve_issue=must_not)
    assert run(v.on_event({})) == "queued"
    assert queued(st)[0]["issue_id"] == "issue-uuid-1"


def test_a_stash_without_an_issue_id_is_completed_by_the_resolver():
    st, _, v = mk(stash={"identifier": "TASK-200", "requested_by_name": "Иван"},
                  resolve_issue=lambda ident: "issue-uuid-late")
    run(v.on_event({}))
    (task,) = queued(st)
    assert task["issue_id"] == "issue-uuid-late"
    assert task["requested_by_name"] == "Иван"


def test_a_broken_resolver_does_not_cancel_the_check(caplog):
    # Трекер молчит — проверка всё равно ставится, вердикт уйдёт отдельным
    # сообщением. Это видно в журнале.
    def boom(ident):
        raise RuntimeError("Linear 502")

    st, _, v = mk(stash=None, resolve_issue=boom)
    assert run(v.on_event({})) == "queued"
    assert "issue_id" not in queued(st)[0]
    assert "TASK-200" in caplog.text and "Linear 502" in caplog.text


def test_an_empty_answer_from_the_resolver_leaves_no_issue_id():
    st, _, v = mk(stash=None, resolve_issue=lambda ident: "")
    run(v.on_event({}))
    assert "issue_id" not in queued(st)[0]


def test_the_stash_is_written_after_the_queueing_and_read_by_identifier():
    st = Store()
    task = {"issue_id": "i1", "identifier": "DOK-5", "title": "т"}
    assert run(remember_task(st, task)) is True
    assert run(stashed_task(st, "DOK-5")) == task
    assert st.ttl == deploy.STASH_TTL_SEC


def test_a_task_without_an_identifier_is_not_stashed():
    st = Store()
    assert run(remember_task(st, {"issue_id": "i1"})) is False
    assert st.kv == {}


def test_a_store_that_cannot_write_does_not_undo_the_queueing(caplog):
    class Deaf(Store):
        async def put(self, key, value, ttl): raise RuntimeError("redis молчит")
    assert run(remember_task(Deaf(), {"identifier": "DOK-5"})) is False
    assert "DOK-5" in caplog.text


def test_a_garbled_stash_reads_as_nothing():
    st = Store()
    st.kv[task_key("TASK-1")] = "не json"
    assert run(stashed_task(st, "TASK-1")) == {}
    st.kv[task_key("TASK-1")] = "[1,2]"
    assert run(stashed_task(st, "TASK-1")) == {}


def test_a_requester_that_is_the_agent_is_dropped():
    # Иначе подтверждённая задача уехала бы назад на агента, а назначение на
    # агента — это новая рабочая сессия: круг замкнулся бы сам.
    st, _, v = mk(stash={**STASH, "requested_by": "agent-uuid",
                         "requested_by_name": "Агент"}, agent_user_id="agent-uuid")
    run(v.on_event({}))
    (task,) = queued(st)
    assert "requested_by" not in task and "requested_by_name" not in task


def test_a_human_requester_survives():
    st, _, v = mk(agent_user_id="agent-uuid")
    run(v.on_event({}))
    assert queued(st)[0]["requested_by_name"] == "Иван"


# ── дедуп ────────────────────────────────────────────────────────────────

def test_the_same_deploy_delivered_twice_gives_one_verifier():
    st, _, v = mk()
    assert run(v.on_event({})) == "queued"
    assert run(v.on_event({})) == "deduped"
    assert len(queued(st)) == 1


def test_two_pipelines_on_one_commit_give_one_verifier():
    # Ручной «Run pipeline», расписание, повторный деплой соседнего сервиса
    # — всё на том же мердж-коммите. Проверка одна на коммит.
    st = Store(); st.kv[task_key("TASK-200")] = json.dumps(STASH)
    v = Verification(st, Q, GitHosting({**LANDED, "pipeline_id": 1}))
    run(v.on_event({}))
    v.deployment = GitHosting({**LANDED, "pipeline_id": 2})
    assert run(v.on_event({})) == "deduped"
    assert len(queued(st)) == 1


def test_a_new_commit_of_the_same_task_is_verified_again():
    st = Store(); st.kv[task_key("TASK-200")] = json.dumps(STASH)
    v = Verification(st, Q, GitHosting({**LANDED, "commit_sha": "aaa"}))
    run(v.on_event({}))
    v.deployment = GitHosting({**LANDED, "commit_sha": "bbb"})
    assert run(v.on_event({})) == "queued"
    assert len(queued(st)) == 2


def test_without_a_sha_the_pipeline_id_tells_deploys_apart():
    st = Store(); st.kv[task_key("TASK-200")] = json.dumps(STASH)
    v = Verification(st, Q, GitHosting({"identifier": "TASK-200", "pipeline_id": 1}))
    run(v.on_event({}))
    v.deployment = GitHosting({"identifier": "TASK-200", "pipeline_id": 1})
    assert run(v.on_event({})) == "deduped"


def test_a_failed_enqueue_releases_the_mark():
    # Хостинг не ретраит (ответ 200), и без снятия метки повторный пайплайн
    # получил бы «deduped» — проверка заперта насовсем.
    st, _, v = mk()
    st.enqueue_fails = True
    assert run(v.on_event({})) == "enqueue-failed"
    assert st.claimed == set()
    st.enqueue_fails = False
    assert run(v.on_event({})) == "queued"


def test_a_store_that_cannot_claim_does_not_queue():
    st, _, v = mk()
    st.claim_fails = True
    assert run(v.on_event({})) == "enqueue-failed"
    assert queued(st) == []


# ── вердикт ──────────────────────────────────────────────────────────────

VERDICT = {"issue_id": "issue-uuid-1", "identifier": "TASK-200",
           "icon": "✅", "verdict": "выкат проверен"}


def test_the_verdict_is_written_into_the_issue_message():
    feed = Feed()
    st, _, v = mk(feed=feed)
    assert run(v.on_verdict(VERDICT)) == "shown"
    assert feed.repainted == [("issue-uuid-1", "✅ выкат проверен")]


def test_without_a_message_the_verdict_goes_to_the_common_chat():
    # Молчать нельзя: потерянный вердикт — размен шума на тишину, ровно то,
    # ради чего проверка и заведена.
    tp = Transport()
    st, _, v = mk(feed=Feed(edited=0), transport=tp, notify_chat="chat")
    assert run(v.on_verdict(VERDICT)) == "told"
    ((chat, text),) = tp.sent
    assert chat == "chat" and "TASK-200" in text and "выкат проверен" in text


def test_without_a_feed_at_all_the_verdict_still_reaches_the_chat():
    tp = Transport()
    st, _, v = mk(feed=None, transport=tp, notify_chat="chat")
    assert run(v.on_verdict(VERDICT)) == "told"
    assert tp.sent


def test_an_alarming_verdict_is_not_shouted_a_second_time():
    # Тревожный вердикт раннер уже отправил человеку лично. Второе сообщение
    # о том же прятало бы первое.
    tp = Transport()
    st, _, v = mk(feed=Feed(edited=0), transport=tp, notify_chat="chat")
    loud = {**VERDICT, "icon": LOUD_ICON, "verdict": "выкат НЕ подтверждён"}
    assert run(v.on_verdict(loud)) == "shouted-by-runner"
    assert tp.sent == []


def test_an_alarming_verdict_still_marks_the_issue_message():
    # Не потерять — значит и в сообщении задачи он обязан появиться: личное
    # сообщение раннера уезжает вверх, а строка в задаче остаётся.
    feed = Feed()
    st, _, v = mk(feed=feed)
    loud = {**VERDICT, "icon": LOUD_ICON, "verdict": "выкат НЕ подтверждён"}
    assert run(v.on_verdict(loud)) == "shown"
    assert feed.repainted == [("issue-uuid-1", "⚠️ выкат НЕ подтверждён")]


def test_a_verdict_with_nowhere_to_go_is_logged_not_swallowed(caplog):
    st, _, v = mk(feed=Feed(edited=0))
    assert run(v.on_verdict(VERDICT)) == "lost"
    assert "TASK-200" in caplog.text and "негде" in caplog.text


def test_a_verdict_without_an_issue_id_edits_nothing():
    feed = Feed()
    st, _, v = mk(feed=feed)
    assert run(v.on_verdict({"identifier": "TASK-200", "verdict": "x"})) == "no-issue"
    assert feed.repainted == []


def test_the_drain_reads_the_bridge_and_survives_a_broken_entry(monkeypatch):
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    feed = Feed()
    st, _, v = mk(feed=feed)
    st.raws = ["не json", json.dumps(VERDICT)]
    with pytest.raises(asyncio.CancelledError):
        run(v.run())
    assert feed.repainted == [("issue-uuid-1", "✅ выкат проверен")]


def test_the_drain_works_without_a_deployment_too():
    # Проверку могли поставить руками — вердикт обязан дойти и там, где
    # адаптера выката нет.
    feed = Feed()
    st, _, v = mk(deployment=False, feed=feed)
    st.raws = [json.dumps(VERDICT)]
    with pytest.raises(asyncio.CancelledError):
        run(v.run())
    assert feed.repainted


# ── приёмник события выката ──────────────────────────────────────────────

import hashlib as _hashlib
import hmac as _hmac

from agent_factory.deploy import DeployReceiver


class Landed:
    """Реализация инсталляции: всё считает своим выкатом."""

    def landed(self, payload):
        return {"identifier": payload.get("ref", "DEV-1"), "commit_sha": "abc"}


def _signed(secret: str, body: bytes, prefix: str = "sha256=") -> str:
    return prefix + _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()


def _receiver(secret="s3cr3t", deployment=None):
    st = Store()
    v = Verification(st, "agent:queue:x", deployment or Landed(),
                     agent_user_id="u-agent")
    return st, v, DeployReceiver(v, secret=secret)


def test_a_signed_deploy_event_queues_the_checker():
    st, v, r = _receiver()
    body = json.dumps({"ref": "DEV-9"}).encode()
    asyncio.run(st.put("agent:task:DEV-9", json.dumps({"issue_id": "i9"}), 60))
    code, out = asyncio.run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 200 and out["verify"] == "queued"


def test_a_foreign_body_is_refused():
    # Порт открыт наружу: без проверки подписи любой желающий ставит сессии.
    st, v, r = _receiver()
    body = json.dumps({"ref": "DEV-9"}).encode()
    code, out = asyncio.run(r.handle(body, "sha256=" + "0" * 64))
    assert code == 401


def test_the_prefix_is_optional():
    # У разных хостингов приставка своя или её нет вовсе.
    st, v, r = _receiver()
    body = json.dumps({"ref": "DEV-9"}).encode()
    asyncio.run(st.put("agent:task:DEV-9", json.dumps({"issue_id": "i9"}), 60))
    bare = _signed("s3cr3t", body, prefix="")
    assert asyncio.run(r.handle(body, bare))[0] == 200


def test_without_a_secret_nothing_is_accepted():
    st, v, r = _receiver(secret="")
    body = json.dumps({"ref": "DEV-9"}).encode()
    assert asyncio.run(r.handle(body, _signed("s3cr3t", body)))[0] == 503


def test_a_body_that_is_not_json_is_refused():
    st, v, r = _receiver()
    body = b"not json"
    assert asyncio.run(r.handle(body, _signed("s3cr3t", body)))[0] == 400


def test_a_non_ascii_signature_is_refused_not_crashed():
    """Сравнение подписи идёт по БАЙТАМ: на строках `compare_digest` требует
    ASCII и иначе кидает `TypeError`, а он наверху превращался в 500 со
    стектрейсом — вместо 401 и предупреждения в журнале."""
    st, v, r = _receiver()
    body = json.dumps({"ref": "DEV-9"}).encode()
    assert r.signed(body, "sha256=подпись") is False
    assert asyncio.run(r.handle(body, "sha256=подпись"))[0] == 401


class Breaks:
    """Разбор инсталляции падает — её ошибка, не «не наш выкат»."""

    def landed(self, payload):
        raise RuntimeError("ключа в событии нет")


def test_a_broken_installation_parser_does_not_answer_ok():
    """Отвечать `ok: true` на СВОЮ поломку нельзя: выкат остаётся
    непроверенным, в чате тишина, доставка у хостинга зелёная, а единственный
    след — трейс в журнале. Ровно так месяцами молчал детектор долгих
    запросов у первого клиента."""
    st, v, r = _receiver(deployment=Breaks())
    body = json.dumps({"ref": "DEV-9"}).encode()
    code, out = asyncio.run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 500 and out["ok"] is False
    assert out["verify"] == "deployment-failed"


def test_not_our_deploy_is_a_success():
    # «Это не наш выкат» — штатный исход, просить хостинг повторить незачем.
    class NotOurs:
        def landed(self, payload): return None

    st, v, r = _receiver(deployment=NotOurs())
    body = json.dumps({"ref": "DEV-9"}).encode()
    code, out = asyncio.run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 200 and out["ok"] is True and out["verify"] == "not-ours"


def test_a_failed_queueing_asks_for_a_retry():
    st, v, r = _receiver()
    st.enqueue_fails = True
    body = json.dumps({"ref": "DEV-9"}).encode()
    code, out = asyncio.run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 503 and out["ok"] is False


# ── мерж в основную ветку → Done ─────────────────────────────────────────
#
# Мерж — принятие работы человеком (решение 13.09.2026: Done = проверено и
# сошлось, ЛИБО принято сознательно). 15.09.2026 одиннадцать смерженных
# задач висели в «In Review» и «Not Verified», потому что приёмник слушал
# только прогоны. Тесты — на каждый исход, включая тихие: «закрыт без
# мержа» и «уже закрыта» обязаны быть тихими, а «переведена» — громкой.

import logging as _logging

from agent_factory.deploy import (closed_state, is_merge_event, merged_work,
                                  closed_unmerged)


class Board:
    """Трекер: задачи по идентификатору, перевод состояния, комментарии."""

    def __init__(self, issues=None, boom=False):
        self.issues = dict(issues or {})
        self.moved: list[tuple] = []
        self.comments: list[tuple] = []
        self.boom = boom

    def issue_by_identifier(self, ident):
        if self.boom:
            raise RuntimeError("Linear 502")
        return self.issues.get(ident)

    def set_state(self, issue_id, name):
        issue = next(i for i in self.issues.values() if i["id"] == issue_id)
        was = issue["state"]["name"]
        issue["state"] = {"name": name, "type": "completed"}
        self.moved.append((issue_id, name))
        return {"from": was, "to": name, "already": was == name}

    def comment(self, issue_id, body):
        self.comments.append((issue_id, body))


def in_review(ident="DEV-86", issue_id="iss-86"):
    return {ident: {"id": issue_id, "identifier": ident, "title": "Ворота",
                    "url": f"https://linear.app/x/{ident}",
                    "state": {"name": "In Review", "type": "started"}}}


def gh_pr(action="closed", merged=True, base="main", head="agent/DEV-86-preflight",
          number=29, title="Ворота перед постановкой", body="", repo="org/dok-engine",
          by="andrey"):
    return {"action": action, "number": number,
            "pull_request": {"number": number, "merged": merged, "title": title,
                             "body": body, "html_url": f"https://gh/{repo}/pull/{number}",
                             "merge_commit_sha": "feedbeef" if merged else None,
                             "merged_at": "2026-09-15T10:00:00Z" if merged else None,
                             "merged_by": {"login": by} if merged else None,
                             "base": {"ref": base}, "head": {"ref": head}},
            "repository": {"full_name": repo}}


def gl_mr(action="merge", target="master", source="agent/TASK-200", iid=17,
          title="Починить экспорт", repo="grp/backend/core"):
    return {"object_kind": "merge_request", "event_type": "merge_request",
            "user": {"username": "pavel"},
            "project": {"path_with_namespace": repo},
            "object_attributes": {"iid": iid, "action": action, "title": title,
                                  "description": "", "state": "merged" if action == "merge" else "closed",
                                  "target_branch": target, "source_branch": source,
                                  "merge_commit_sha": "c0ffee", "url": f"https://gl/{repo}/-/merge_requests/{iid}"}}


def mk_merge(issues=None, tracker=True, deployment=True, **kw):
    st = Store()
    board = Board(in_review() if issues is None else issues) if tracker else None
    v = Verification(st, Q, GitHosting(None) if deployment else None,
                     tracker=board, **kw)
    return st, board, v


def test_a_merged_pull_request_closes_the_task_and_says_so(caplog):
    st, board, v = mk_merge()
    with caplog.at_level(_logging.INFO, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "done"
    assert board.moved == [("iss-86", "Done")]
    (iid, body), = board.comments
    assert iid == "iss-86" and "Закрыто по мержу" in body and "`Done`" in body
    assert "agent/DEV-86-preflight" in body and "`main`" in body and "andrey" in body
    said = [r.getMessage() for r in caplog.records if "переведена" in r.getMessage()]
    assert said and "DEV-86" in said[0] and "Done" in said[0] and "In Review" in said[0]


def test_a_pull_request_closed_without_a_merge_changes_nothing(caplog):
    # Закрыли, не смержив — работа не принята. Ничего не переводим, а в
    # журнале одна строка: закрытие без мержа — единственный не-мерж, о
    # котором стоит сказать.
    st, board, v = mk_merge()
    with caplog.at_level(_logging.INFO, logger="intake"):
        assert run(v.on_merge(gh_pr(merged=False))) == "not-ours"
    assert board.moved == [] and board.comments == [] and st.claimed == set()
    assert any("без мержа" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "labeled", "edited"])
def test_other_pull_request_actions_are_not_merges(action):
    st, board, v = mk_merge()
    assert run(v.on_merge(gh_pr(action=action, merged=False))) == "not-ours"
    assert board.moved == []


def test_a_merge_into_a_side_branch_is_not_an_acceptance():
    # Веток развития нет по схеме, но если кто-то влил в чужую ветку —
    # это не мерж в основную, задача не принята.
    st, board, v = mk_merge()
    assert run(v.on_merge(gh_pr(base="release/1.2"))) == "not-ours"
    assert board.moved == []


def test_master_is_a_main_branch_too():
    st, board, v = mk_merge()
    assert run(v.on_merge(gh_pr(base="master"))) == "done"


def test_the_task_name_comes_from_the_branch_not_the_title():
    # Заголовок PR называет другую задачу — ветка надёжнее.
    st, board, v = mk_merge(issues={**in_review("DEV-86"), **in_review("DEV-1", "iss-1")})
    run(v.on_merge(gh_pr(head="agent/DEV-86-v2", title="DEV-1: как просили")))
    assert board.moved == [("iss-86", "Done")]


def test_dev_8_does_not_match_dev_86():
    assert merged_work(gh_pr(head="agent/DEV-86"))["identifier"] == "DEV-86"
    assert merged_work(gh_pr(head="agent/DEV-8-x"))["identifier"] == "DEV-8"


def test_without_a_name_in_the_branch_the_title_then_the_body_are_read():
    assert merged_work(gh_pr(head="fix/typo", title="DEV-86 опечатка"))["identifier"] == "DEV-86"
    assert merged_work(gh_pr(head="fix/typo", title="опечатка",
                             body="Closes DEV-86"))["identifier"] == "DEV-86"


def test_a_merge_naming_no_task_is_loud_and_leaves_the_board_alone(caplog):
    st, board, v = mk_merge()
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.on_merge(gh_pr(head="fix/typo", title="опечатка"))) == "not-ours"
    assert board.moved == []
    assert any("не видно" in r.getMessage() and "fix/typo" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("state", [{"name": "Done", "type": "completed"},
                                   {"name": "Canceled", "type": "canceled"},
                                   {"name": "Done"},            # тип не отдали
                                   {"name": "done", "type": ""}])
def test_a_task_already_closed_is_left_in_peace(state, caplog):
    # Проверяющая сессия уже поставила Done, или человек закрыл руками:
    # второй перевод и второй комментарий — шум, которого и так много.
    issues = in_review(); issues["DEV-86"]["state"] = state
    st, board, v = mk_merge(issues=issues)
    with caplog.at_level(_logging.INFO, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "already-closed"
    assert board.moved == [] and board.comments == []
    assert any("уже в" in r.getMessage() for r in caplog.records)


def test_a_custom_done_name_is_used_for_both_the_move_and_the_check():
    issues = in_review(); issues["DEV-86"]["state"] = {"name": "Готово", "type": ""}
    st, board, v = mk_merge(issues=issues, done_state="Готово")
    assert run(v.on_merge(gh_pr())) == "already-closed"
    st, board, v = mk_merge(done_state="Готово")
    assert run(v.on_merge(gh_pr())) == "done"
    assert board.moved == [("iss-86", "Готово")] and "`Готово`" in board.comments[0][1]


def test_a_task_missing_in_the_tracker_is_reported_not_swallowed(caplog):
    st, board, v = mk_merge(issues={})
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "no-issue"
    assert any("нет в трекере" in r.getMessage() for r in caplog.records)


def test_two_deliveries_of_one_merge_move_the_task_once():
    st, board, v = mk_merge()
    assert run(v.on_merge(gh_pr())) == "done"
    assert run(v.on_merge(gh_pr())) == "deduped"
    assert len(board.moved) == 1 and len(board.comments) == 1


def test_a_second_pull_request_of_the_same_task_is_its_own_event():
    # У задачи бывает второй PR (`-v2`): метка по PR, а не по задаче. Первый
    # уже закрыл задачу, второй увидит «уже закрыта» — но по своей метке.
    st, board, v = mk_merge()
    run(v.on_merge(gh_pr(number=29)))
    assert run(v.on_merge(gh_pr(number=31, head="agent/DEV-86-v2"))) == "already-closed"


def test_a_store_that_cannot_claim_does_not_block_the_move(caplog):
    # Дедуп — не единственная защита: «уже закрыта» отсекает повтор и без
    # стора. Лежачий redis не должен оставлять задачу в ревью.
    st, board, v = mk_merge()
    st.claim_fails = True
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "done"
    assert board.moved == [("iss-86", "Done")]
    assert any("дедуп не отработал" in r.getMessage() for r in caplog.records)


def test_a_tracker_failure_releases_the_mark_so_a_retry_can_finish(caplog):
    st, board, v = mk_merge()
    board.boom = True
    with caplog.at_level(_logging.ERROR, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "tracker-failed"
    assert any("статус не сменился" in r.getMessage() for r in caplog.records)
    board.boom = False
    assert run(v.on_merge(gh_pr())) == "done"


def test_a_failed_comment_does_not_undo_the_move(caplog):
    st, board, v = mk_merge()
    board.comment = lambda i, b: (_ for _ in ()).throw(RuntimeError("comments:create нет в скоупе"))
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "done"
    assert board.moved == [("iss-86", "Done")]
    assert any("комментарий не оставлен" in r.getMessage() for r in caplog.records)


def test_without_a_tracker_the_merge_is_loud_and_moves_nothing(caplog):
    st, board, v = mk_merge(tracker=False)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "no-tracker"
    assert any("не подключён" in r.getMessage() and "DEV-86" in r.getMessage()
               for r in caplog.records)


def test_a_tracker_that_cannot_move_states_is_loud_too(caplog):
    # Трекер первого клиента (`feed_wiring.Tracker`) умеет три метода, и
    # `set_state` среди них нет. Молча ничего не делать нельзя.
    class Narrow:
        def issue_by_identifier(self, ident): return in_review()[ident]
    st = Store()
    v = Verification(st, Q, GitHosting(None), tracker=Narrow())
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "no-tracker"
    assert any("не умеет менять статус" in r.getMessage() for r in caplog.records)
    assert st.claimed == set()


def test_the_installation_may_answer_the_merge_question_itself():
    # Есть `merged` у реализации протокола — её ответ старше разбора продукта.
    class Own:
        def landed(self, payload): return None
        def merged(self, payload): return {"identifier": "DEV-86", "mr_iid": "7",
                                           "ref": "main", "source_ref": "x",
                                           "project_path": "p"}
    st = Store()
    board = Board(in_review())
    v = Verification(st, Q, Own(), tracker=board)
    assert run(v.on_merge({"whatever": True})) == "done"
    assert board.moved == [("iss-86", "Done")]


def test_a_broken_installation_merge_parser_is_its_error():
    class Broken:
        def landed(self, payload): return None
        def merged(self, payload): raise KeyError("object_attributes")
    v = Verification(Store(), Q, Broken(), tracker=Board(in_review()))
    assert run(v.on_merge(gh_pr())) == "deployment-failed"


def test_without_a_deployment_the_product_parser_still_works():
    # Инсталляция без реализации протокола выката всё равно получает Done по
    # мержу: разбор события — продуктовый.
    st, board, v = mk_merge(deployment=False)
    assert run(v.on_merge(gh_pr())) == "done"


# ── GitLab: то же самое другими словами ──────────────────────────────────

def test_a_gitlab_merge_closes_the_task():
    st, board, v = mk_merge(issues=in_review("TASK-200", "iss-200"))
    assert run(v.on_merge(gl_mr())) == "done"
    assert board.moved == [("iss-200", "Done")]
    assert "pavel" in board.comments[0][1] and "`master`" in board.comments[0][1]


def test_a_gitlab_close_without_merge_changes_nothing(caplog):
    st, board, v = mk_merge(issues=in_review("TASK-200", "iss-200"))
    with caplog.at_level(_logging.INFO, logger="intake"):
        assert run(v.on_merge(gl_mr(action="close"))) == "not-ours"
    assert board.moved == [] and any("без мержа" in r.getMessage() for r in caplog.records)


def test_a_gitlab_update_of_an_already_merged_request_is_not_a_second_merge():
    # Правка метки на влитом MR приезжает как `update` с `state: merged`.
    st, board, v = mk_merge(issues=in_review("TASK-200", "iss-200"))
    ev = gl_mr(action="update"); ev["object_attributes"]["state"] = "merged"
    assert run(v.on_merge(ev)) == "not-ours"


def test_gitlab_facts_are_read_from_their_own_fields():
    facts = merged_work(gl_mr())
    assert facts["identifier"] == "TASK-200" and facts["mr_iid"] == "17"
    assert facts["project_path"] == "grp/backend/core" and facts["merged_by"] == "pavel"
    assert facts["merge_commit_sha"] == "c0ffee" and facts["ref"] == "master"


# ── форма события ────────────────────────────────────────────────────────

def test_a_workflow_run_is_not_a_merge_event_even_with_pull_requests_inside():
    ev = {"action": "completed", "workflow_run": {"pull_requests": [{"head": {"ref": "agent/DEV-1"}}]}}
    assert not is_merge_event(ev) and merged_work(ev) is None and not closed_unmerged(ev)


def test_closed_state_reads_the_type_first_and_the_name_as_a_fallback():
    assert closed_state({"name": "Готово", "type": "completed"})
    assert closed_state({"name": "Отменено", "type": "canceled"})
    assert not closed_state({"name": "In Review", "type": "started"})
    assert closed_state({"name": "Готово"}, "Готово")
    assert not closed_state({"name": "Готово"}, "Done")
    assert not closed_state({}, "Done")


# ── приёмник: тот же путь, та же подпись ─────────────────────────────────

def _merge_receiver(board=None, secret="s3cr3t"):
    st = Store()
    v = Verification(st, "agent:queue:x", Landed(), agent_user_id="u-agent",
                     tracker=board if board is not None else Board(in_review()))
    return st, v, DeployReceiver(v, secret=secret)


def test_a_signed_pull_request_event_closes_the_task():
    st, v, r = _merge_receiver()
    body = json.dumps(gh_pr()).encode()
    code, out = run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 200 and out == {"ok": True, "merge": "done"}
    assert v.tracker.moved == [("iss-86", "Done")]


def test_an_unsigned_pull_request_event_is_refused():
    st, v, r = _merge_receiver()
    body = json.dumps(gh_pr()).encode()
    assert run(r.handle(body, "sha256=" + "0" * 64))[0] == 401
    assert v.tracker.moved == []


def test_a_pull_request_closed_without_a_merge_is_a_success_for_the_hosting():
    st, v, r = _merge_receiver()
    body = json.dumps(gh_pr(merged=False)).encode()
    code, out = run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 200 and out["merge"] == "not-ours"


def test_a_workflow_run_still_goes_to_the_deploy_check():
    st, v, r = _merge_receiver()
    body = json.dumps({"ref": "DEV-9", "workflow_run": {"id": 1}}).encode()
    run(st.put("agent:task:DEV-9", json.dumps({"issue_id": "i9"}), 60))
    code, out = run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 200 and out["verify"] == "queued" and "merge" not in out


def test_a_tracker_failure_on_merge_asks_the_hosting_to_retry():
    st, v, r = _merge_receiver(board=Board(in_review(), boom=True))
    body = json.dumps(gh_pr()).encode()
    code, out = run(r.handle(body, _signed("s3cr3t", body)))
    assert code == 503 and out == {"ok": False, "merge": "tracker-failed"}


def test_a_broken_installation_merge_parser_answers_500():
    class Broken:
        def landed(self, payload): return None
        def merged(self, payload): raise RuntimeError("не та форма")
    st = Store()
    v = Verification(st, "agent:queue:x", Broken(), tracker=Board(in_review()))
    r = DeployReceiver(v, secret="s3cr3t")
    body = json.dumps(gh_pr()).encode()
    assert run(r.handle(body, _signed("s3cr3t", body)))[0] == 500


# ── мерж по кнопке: та же дорога к Done без события хостинга ─────────────

def test_a_merge_by_button_closes_the_task_and_names_who_pressed(caplog):
    st, board, v = mk_merge()
    facts = {"identifier": "DEV-86", "mr_iid": "29", "ref": "main",
             "source_ref": "agent/DEV-86-preflight", "project_path": "o/dok-engine",
             "url": "https://gh/o/dok-engine/pull/29"}
    with caplog.at_level(_logging.INFO, logger="intake"):
        assert run(v.accept_merged("DEV-86", facts, by="1000000002")) == "done"
    assert board.moved == [("iss-86", "Done")]
    assert "1000000002" in board.comments[0][1]
    assert any("переведена" in r.getMessage() and "1000000002" in r.getMessage()
               for r in caplog.records)


# ── закрытие без мержа: у задачи нет и не будет ветки (DEV-120) ──────────
#
# Задача-решение сделана честно, а мержить в ней нечего. Раньше такая задача
# оставалась в стопке «ждёт мержа» навсегда: единственная кнопка на карточке
# звала хостинг, который заведомо ничего не находил.


def test_a_task_without_a_merge_is_closed_and_the_comment_says_why(caplog):
    st, board, v = mk_merge()
    with caplog.at_level(_logging.INFO, logger="intake"):
        assert run(v.accept_done("DEV-86", by="1000000002")) == "done"
    assert board.moved == [("iss-86", "Done")]
    body = board.comments[0][1]
    assert "без мержа" in body and "1000000002" in body and "In Review" in body
    assert any("открытого PR нет" in r.getMessage() for r in caplog.records)


def test_closing_without_a_merge_twice_moves_nothing():
    st, board, v = mk_merge()
    assert run(v.accept_done("DEV-86")) == "done"
    assert run(v.accept_done("DEV-86")) == "already-closed"
    assert len(board.moved) == 1 and len(board.comments) == 1


def test_closing_a_task_the_tracker_does_not_know_is_not_a_crash():
    st, board, v = mk_merge()
    assert run(v.accept_done("DEV-404")) == "no-issue" and board.moved == []


def test_closing_without_a_tracker_is_loud(caplog):
    st, board, v = mk_merge(tracker=False)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.accept_done("DEV-86")) == "no-tracker"
    assert any("переведи руками" in r.getMessage() for r in caplog.records)


def test_closing_without_a_merge_survives_a_tracker_failure():
    st, board, v = mk_merge()
    board.boom = True
    assert run(v.accept_done("DEV-86")) == "tracker-failed"


def test_cancelling_moves_the_task_to_canceled_and_says_why(caplog):
    # Отмена — не `Done`: «принято» и «не нужно» считаются по доске раздельно.
    st, board, v = mk_merge()
    with caplog.at_level(_logging.INFO, logger="intake"):
        assert run(v.accept_canceled("DEV-86", by="1000000002")) == "canceled"
    assert board.moved == [("iss-86", "Canceled")]
    body = board.comments[0][1]
    assert "Отменено человеком" in body and "1000000002" in body
    assert "`Canceled`" in body and "In Review" in body and "НЕ тронут" in body
    assert any("переведена" in r.getMessage() for r in caplog.records)


def test_the_canceled_state_name_is_the_installations_own():
    st, board, v = mk_merge(cancel_state="Отменено")
    assert run(v.accept_canceled("DEV-86")) == "canceled"
    assert board.moved == [("iss-86", "Отменено")]


def test_cancelling_twice_moves_nothing():
    st, board, v = mk_merge()
    assert run(v.accept_canceled("DEV-86")) == "canceled"
    assert run(v.accept_canceled("DEV-86")) == "already-closed"
    assert len(board.moved) == 1 and len(board.comments) == 1


def test_cancelling_a_task_the_tracker_does_not_know_is_not_a_crash():
    st, board, v = mk_merge()
    assert run(v.accept_canceled("DEV-404")) == "no-issue" and board.moved == []


def test_cancelling_without_a_tracker_is_loud(caplog):
    st, board, v = mk_merge(tracker=False)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.accept_canceled("DEV-86")) == "no-tracker"
    assert any("переведи руками" in r.getMessage() for r in caplog.records)


def test_cancelling_survives_a_tracker_failure():
    st, board, v = mk_merge()
    board.boom = True
    assert run(v.accept_canceled("DEV-86")) == "tracker-failed"


def test_the_hook_event_after_a_button_merge_is_quiet():
    # Кнопка закрыла задачу; событие о мерже приедет позже и увидит «уже закрыта».
    st, board, v = mk_merge()
    run(v.accept_merged("DEV-86", {"mr_iid": "29", "project_path": "o/dok-engine"}))
    assert run(v.on_merge(gh_pr())) == "already-closed"
    assert len(board.moved) == 1 and len(board.comments) == 1


def test_a_merge_by_button_without_a_tracker_is_loud(caplog):
    st, board, v = mk_merge(tracker=False)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.accept_merged("DEV-86", {"mr_iid": "29"})) == "no-tracker"
    assert any("переведи руками" in r.getMessage() for r in caplog.records)


def test_a_merge_by_button_survives_a_tracker_failure():
    st, board, v = mk_merge()
    board.boom = True
    assert run(v.accept_merged("DEV-86", {"mr_iid": "29"})) == "tracker-failed"


# ── мерж будит пересмотр стопки (DEV-109) ──────────────────────────────────
#
# Задачи в стопке «ждёт мержа» протухают не по часам, а в тот момент, когда в
# основную ветку что-то уехало. Ждать часового круга значит оставить стопку
# врущей ровно тогда, когда это уже известно.

def test_a_merged_pull_request_wakes_the_sweep():
    st, board, v = mk_merge()
    woken = []
    v.after_merge = lambda why: woken.append(why)
    assert run(v.on_merge(gh_pr())) == "done"
    assert woken and "DEV-86" in woken[0]


def test_a_repeated_delivery_does_not_wake_the_sweep():
    # Второй раз тот же мерж ничего не менял: будить пересмотр на повторной
    # доставке значит гонять хостинг по всей стопке впустую.
    st, board, v = mk_merge()
    woken = []
    v.after_merge = lambda why: woken.append(why)
    run(v.on_merge(gh_pr()))
    woken.clear()
    assert run(v.on_merge(gh_pr())) in ("deduped", "already-closed")
    assert woken == []


def test_the_merge_button_wakes_the_sweep_but_the_sweep_itself_does_not():
    # Кнопка на странице подписи — настоящий мерж прямо сейчас. Подбор
    # пропущенного мержа (`reconcile`, `by` пустое) — мерж недельной
    # давности; будить на нём значит крутить лишний круг после каждой
    # подобранной задачи.
    st, board, v = mk_merge()
    woken = []
    v.after_merge = lambda why: woken.append(why)
    facts = {"mr_iid": "23", "project_path": "org/repo", "ref": "main",
             "source_ref": "agent/DEV-86", "url": "https://gh/pr/23"}
    assert run(v.accept_merged("DEV-86", facts, by="andrey")) == "done"
    assert len(woken) == 1
    board.issues["DEV-86"]["state"] = {"name": "In Review", "type": "started"}
    assert run(v.accept_merged("DEV-86", facts)) == "done"
    assert len(woken) == 1


def test_a_broken_sweep_does_not_break_the_merge(caplog):
    st, board, v = mk_merge()

    def boom(why):
        raise RuntimeError("пересмотр не поднялся")

    v.after_merge = boom
    with caplog.at_level(_logging.WARNING, logger="intake"):
        assert run(v.on_merge(gh_pr())) == "done"
    assert "пересмотр стопки не разбудить" in " | ".join(
        r.getMessage() for r in caplog.records)
