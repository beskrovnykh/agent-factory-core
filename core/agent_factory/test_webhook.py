"""Событие трекера приходит само.

Назначил задачу агенту — сессия стартует, без кнопки и без круга опроса.
Половина тестов здесь про то, чего делать НЕЛЬЗЯ: завести сессию по чужому
телу, завести вторую на то же назначение, проглотить неудачу молча.
"""
import asyncio
import hashlib
import hmac
import json

import pytest

from agent_factory import webhook
from agent_factory.webhook import Receiver, Server

SECRET = "s3cr3t"
AGENT = "agent-uuid"


class Store:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.claimed: set[str] = set()
        self.enqueue_fails = False
        self.claim_fails = False

    async def claim(self, key, ttl):
        if self.claim_fails:
            raise RuntimeError("redis молчит")
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    async def drop(self, key):
        self.claimed.discard(key)

    async def enqueue(self, queue, element):
        if self.enqueue_fails:
            raise RuntimeError("redis молчит")
        self.lists.setdefault(queue, []).append(element)

    async def put(self, key, value, ttl):
        self.kv = getattr(self, "kv", {}); self.kv[key] = value

    async def get(self, key):
        return getattr(self, "kv", {}).get(key)


class Tracker:
    def __init__(self, labels=None):
        self.labels = labels if labels is not None else []
        self.asked = []
        self.fail = None

    def issue_label_names(self, issue_id):
        self.asked.append(issue_id)
        if self.fail:
            raise RuntimeError(self.fail)
        return list(self.labels)


def mk(tracker=None, incident_label="дежурный", agent=AGENT, **kw):
    st = Store()
    return st, Receiver(st, "agent:queue:x", agent, secret=SECRET,
                        tracker=tracker, incident_label=incident_label, **kw)


def assignment(issue_id="i1", stamp="2026-09-11T10:00:00Z", assignee=AGENT,
               action="update", labels=None, **data):
    payload = {
        "type": "Issue", "action": action,
        "webhookTimestamp": stamp,
        "actor": {"id": "who-1", "name": "Андрей"},
        "data": {"id": issue_id, "identifier": "DEV-80", "title": "починить",
                 "url": "https://l/DEV-80", "assigneeId": assignee, **data},
    }
    if labels is not None:
        payload["data"]["labels"] = labels
    if action == "update":
        payload["updatedFrom"] = {"assigneeId": None}
    return payload


def signed(payload):
    body = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def queued(st):
    return [json.loads(x) for x in st.lists.get("agent:queue:x", [])]


def run(coro):
    return asyncio.run(coro)


# ── подпись ──────────────────────────────────────────────────────────────

def test_a_signed_assignment_starts_the_work():
    st, r = mk()
    code, body = run(r.handle(*signed(assignment())))
    assert code == 200 and body["agent"] == "queued"
    task = queued(st)[0]
    assert task["issue_id"] == "i1" and task["identifier"] == "DEV-80"
    assert task["url"] == "https://l/DEV-80"


def test_a_foreign_body_starts_nothing():
    # Порт открыт наружу: без проверки подписи любой желающий заводит сессии.
    st, r = mk()
    body, _ = signed(assignment())
    code, _ = run(r.handle(body, "0" * 64))
    assert code == 401 and queued(st) == []


def test_a_body_changed_after_signing_is_refused():
    st, r = mk()
    body, sig = signed(assignment())
    code, _ = run(r.handle(body.replace(b"i1", b"i2"), sig))
    assert code == 401 and queued(st) == []


def test_an_empty_signature_is_refused():
    st, r = mk()
    body, _ = signed(assignment())
    assert run(r.handle(body, ""))[0] == 401


def test_without_a_secret_nothing_is_accepted():
    # Иначе приёмник с недозаполненным окружением принимает что угодно.
    st = Store()
    r = Receiver(st, "agent:queue:x", AGENT, secret="")
    body, sig = signed(assignment())
    code, _ = run(r.handle(body, sig))
    assert code == 503 and queued(st) == []


def test_verify_is_false_on_an_empty_secret():
    assert not webhook.verify("", b"{}", "abc")


def test_a_body_that_is_not_json_is_refused():
    st, r = mk()
    body = b"not json"
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    code, _ = run(r.handle(body, sig))
    assert code == 400 and queued(st) == []


def test_a_json_body_that_is_not_an_object_is_refused():
    st, r = mk()
    body = b"[1,2,3]"
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert run(r.handle(body, sig))[0] == 400


# ── что считается назначением ────────────────────────────────────────────

def test_an_edit_of_another_field_is_not_an_assignment():
    # Без этого любой комментарий или смена статуса на задаче агента заводили
    # бы новую сессию поверх идущей.
    st, r = mk()
    p = assignment()
    p["updatedFrom"] = {"description": "было"}
    code, body = run(r.dispatch(p))
    assert code == 200 and body["ignored"] and queued(st) == []


def test_a_new_issue_created_on_the_agent_starts_the_work():
    st, r = mk()
    run(r.dispatch(assignment(action="create")))
    assert len(queued(st)) == 1


def test_removing_the_assignee_starts_nothing():
    st, r = mk()
    p = assignment(assignee=None)
    assert run(r.dispatch(p))[1]["ignored"] and queued(st) == []


def test_an_assignment_to_a_human_starts_nothing():
    st, r = mk()
    assert run(r.dispatch(assignment(assignee="someone-else")))[1]["ignored"]
    assert queued(st) == []


def test_events_about_other_entities_are_ignored():
    st, r = mk()
    p = assignment()
    p["type"] = "Comment"
    assert run(r.dispatch(p))[1]["ignored"] and queued(st) == []


def test_an_installation_without_an_agent_starts_nothing():
    st, r = mk(agent="")
    assert run(r.dispatch(assignment()))[1]["ignored"] and queued(st) == []


def test_an_assignment_without_an_issue_id_is_skipped():
    st, r = mk()
    p = assignment()
    p["data"].pop("id")
    assert run(r.dispatch(p))[1]["agent"] == "no-issue-id"
    assert queued(st) == []


def test_the_assignee_may_come_as_an_object():
    st, r = mk()
    p = assignment(assignee=None)
    p["data"]["assignee"] = {"id": AGENT}
    run(r.dispatch(p))
    assert len(queued(st)) == 1


# ── дедуп ────────────────────────────────────────────────────────────────

def test_the_same_delivery_twice_starts_one_session():
    # Трекер ретраит доставку: у ретрая отметка события та же.
    st, r = mk()
    body, sig = signed(assignment())
    assert run(r.handle(body, sig))[1]["agent"] == "queued"
    assert run(r.handle(body, sig))[1]["agent"] == "deduped"
    assert len(queued(st)) == 1


def test_two_deliveries_of_one_action_start_one_session():
    # 11.09 на одно действие человека приехало две доставки с РАЗНЫМИ
    # отметками: по TASK-285 и TASK-288 стартовало по две сессии, две ветки,
    # два «взялся». Ключ по отметке их не ловит — ловит окно по задаче.
    st, r = mk()
    run(r.dispatch(assignment(stamp="...:00Z")))
    out = run(r.dispatch(assignment(stamp="...:01Z")))
    assert out[1]["agent"] == "deduped"
    assert len(queued(st)) == 1


def test_a_deliberate_reassignment_later_starts_a_session():
    # Упавшую сессию человек перезапускает переназначением. Окно закончилось —
    # задача едет снова.
    st, r = mk(start_window_sec=180)
    run(r.dispatch(assignment(stamp="a")))
    st.claimed.discard("agent:started:i1")          # окно истекло
    assert run(r.dispatch(assignment(stamp="b")))[1]["agent"] == "queued"
    assert len(queued(st)) == 2


def test_a_late_retry_of_the_same_delivery_starts_nothing():
    # Ретраи идут с нарастающей паузой и переживают окно по задаче. Отличить
    # их от осознанного переназначения можно только по отметке события — она
    # у ретрая та же.
    st, r = mk(start_window_sec=180)
    run(r.dispatch(assignment(stamp="a")))
    st.claimed.discard("agent:started:i1")          # окно истекло
    assert run(r.dispatch(assignment(stamp="a")))[1]["agent"] == "deduped"
    assert len(queued(st)) == 1


def test_the_window_key_is_the_one_the_poller_uses():
    # Иначе вебхук и опрос назначений поставят задачу по разу каждый.
    st, r = mk()
    run(r.dispatch(assignment()))
    assert "agent:started:i1" in st.claimed


def test_another_issue_is_not_blocked_by_the_window():
    st, r = mk()
    run(r.dispatch(assignment(issue_id="i1")))
    run(r.dispatch(assignment(issue_id="i2")))
    assert len(queued(st)) == 2


# ── отказы ───────────────────────────────────────────────────────────────

def test_a_failed_enqueue_asks_the_tracker_to_repeat():
    # Задача висит в трекере на агенте, а сессии нет. Ответить 200 значит
    # сказать «принято» и потерять её молча.
    st, r = mk()
    st.enqueue_fails = True
    code, body = run(r.dispatch(assignment()))
    assert code == 503 and body["agent"] == "enqueue-failed"


def test_a_failed_enqueue_releases_the_claims():
    # Иначе повтор доставки съест ключ отметки, новая доставка — окно, и
    # задача не поедет вообще.
    st, r = mk()
    st.enqueue_fails = True
    run(r.dispatch(assignment()))
    assert st.claimed == set()
    st.enqueue_fails = False
    assert run(r.dispatch(assignment()))[1]["agent"] == "queued"
    assert len(queued(st)) == 1


def test_a_store_that_cannot_claim_does_not_queue():
    st, r = mk()
    st.claim_fails = True
    code, body = run(r.dispatch(assignment()))
    assert code == 503 and queued(st) == []


# ── дежурный или обычная работа ──────────────────────────────────────────

def test_a_labelled_issue_starts_a_duty_session():
    st, r = mk()
    run(r.dispatch(assignment(
        labels=[{"name": "Дежурный"}],
        description="## Симптом\n\nAPI отдаёт 502 со вчера.")))
    task = queued(st)[0]
    assert task["kind"] == "incident"
    assert task["symptom"] == "API отдаёт 502 со вчера."


def test_without_the_label_it_is_ordinary_work():
    st, r = mk()
    run(r.dispatch(assignment(labels=[])))
    assert "kind" not in queued(st)[0]


def test_labels_absent_from_the_event_are_read_from_the_tracker():
    # Трекер кладёт лейблы не во все события. Без дочитывания инцидент молча
    # уехал бы обычной работой.
    tr = Tracker(labels=["дежурный"])
    st, r = mk(tracker=tr)
    run(r.dispatch(assignment()))
    assert tr.asked == ["i1"]
    assert queued(st)[0]["kind"] == "incident"


def test_an_empty_label_id_list_is_an_honest_no_labels():
    # Пустой `labelIds` — это «лейблов нет», а не «событие их не несёт»:
    # лишний запрос в трекер на каждое назначение не нужен.
    tr = Tracker(labels=["дежурный"])
    st, r = mk(tracker=tr)
    run(r.dispatch(assignment(labelIds=[])))
    assert tr.asked == []
    assert "kind" not in queued(st)[0]


def test_labels_are_read_only_after_the_dedup():
    # Ретраи идут пачкой; платить за них запросом в трекер незачем.
    tr = Tracker()
    st, r = mk(tracker=tr)
    run(r.dispatch(assignment(stamp="a")))
    run(r.dispatch(assignment(stamp="a")))
    assert tr.asked == ["i1"]


def test_a_tracker_that_cannot_answer_still_queues_the_task():
    # Очередь важнее развилки: без неё сессии не будет вовсе.
    tr = Tracker()
    tr.fail = "401"
    st, r = mk(tracker=tr)
    assert run(r.dispatch(assignment()))[1]["agent"] == "queued"
    assert "kind" not in queued(st)[0]


def test_labels_without_names_are_read_from_the_tracker():
    # Событие принесло лейблы без имён — это не «пусто».
    tr = Tracker(labels=["дежурный"])
    st, r = mk(tracker=tr)
    run(r.dispatch(assignment(labels=[{"id": "l1"}])))
    assert tr.asked == ["i1"] and queued(st)[0]["kind"] == "incident"


def test_an_installation_without_a_duty_label_never_escalates():
    st, r = mk(incident_label="")
    run(r.dispatch(assignment(labels=[{"name": "дежурный"}])))
    assert "kind" not in queued(st)[0]


# ── постановщик ──────────────────────────────────────────────────────────

def test_the_person_who_assigned_is_carried_into_the_task():
    # Отказ «делать нечего» возвращается постановщику, а не тому, кто
    # принимает работу. В самой задаче его не видно — только в событии.
    st, r = mk()
    run(r.dispatch(assignment()))
    task = queued(st)[0]
    assert task["requested_by_name"] == "Андрей"
    assert task["requested_by"] == "who-1"


def test_an_event_without_an_actor_still_names_the_source():
    st, r = mk()
    p = assignment()
    p.pop("actor")
    run(r.dispatch(p))
    assert queued(st)[0]["requested_by_name"] == "назначено в трекере"
    assert "requested_by" not in queued(st)[0]


# ── послесловие инсталляции ──────────────────────────────────────────────

def test_the_installation_hook_sees_the_queued_task():
    st, r = mk()
    seen = []

    async def hook(task):
        seen.append(task)

    r.on_queued = hook
    run(r.dispatch(assignment()))
    assert seen and seen[0]["identifier"] == "DEV-80"


def test_a_failing_hook_does_not_undo_the_queueing():
    st, r = mk()

    async def hook(task):
        raise RuntimeError("некуда записать")

    r.on_queued = hook
    assert run(r.dispatch(assignment()))[1]["agent"] == "queued"
    assert len(queued(st)) == 1


# ── HTTP-вход ────────────────────────────────────────────────────────────

async def request(server, raw: bytes, whole: bool = False):
    """Постучаться в поднятый сервер настоящим сокетом: разбор запроса
    написан руками, и проверять его подделкой ридера бессмысленно.

    `whole` — вернуть ответ целиком, байтами: у страницы тело не JSON.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    if whole:
        return data
    head, _, body = data.partition(b"\r\n\r\n")
    code = int(head.split(b"\r\n")[0].split(b" ")[1])
    return code, (json.loads(body) if body else {})


def post(path: str, body: bytes, sig: str = "", extra: str = "") -> bytes:
    return (f"POST {path} HTTP/1.1\r\nHost: x\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Linear-Signature: {sig}\r\n{extra}\r\n").encode() + body


def served(fn, **kw):
    """Поднять сервер на свободном порту, сходить в него, погасить."""
    async def main():
        st = Store()
        srv = Server(Receiver(st, "agent:queue:x", AGENT, secret=SECRET),
                     bind="127.0.0.1", port=0, **kw)
        task = asyncio.create_task(srv.run())
        for _ in range(200):                     # дождаться сокета
            if srv.port:
                break
            await asyncio.sleep(0.01)
        try:
            return await fn(srv, st)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    return asyncio.run(main())


def test_a_signed_post_over_http_queues_the_task():
    async def go(srv, st):
        body, sig = signed(assignment())
        code, out = await request(srv, post("/linear", body, sig))
        return code, out, queued(st)

    code, out, q = served(go)
    assert code == 200 and out["agent"] == "queued" and len(q) == 1


def test_a_wrong_signature_over_http_is_refused():
    async def go(srv, st):
        body, _ = signed(assignment())
        code, _ = await request(srv, post("/linear", body, "deadbeef"))
        return code, queued(st)

    code, q = served(go)
    assert code == 401 and q == []


def test_another_path_is_not_the_webhook():
    async def go(srv, st):
        body, sig = signed(assignment())
        return (await request(srv, post("/whatever", body, sig)))[0], queued(st)

    code, q = served(go)
    assert code == 404 and q == []


def test_the_path_is_a_parameter_of_the_installation():
    # Адрес — настройка, а не строчка в коде: у каждой инсталляции свой.
    async def go(srv, st):
        body, sig = signed(assignment())
        return (await request(srv, post("/hooks/tracker", body, sig)))[0]

    assert served(go, path="/hooks/tracker") == 200


def test_health_answers_without_a_signature():
    async def go(srv, st):
        return (await request(
            srv, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"))[0]

    assert served(go) == 200


def test_a_get_on_the_webhook_path_is_not_accepted():
    async def go(srv, st):
        return (await request(
            srv, b"GET /linear HTTP/1.1\r\nHost: x\r\n\r\n"))[0]

    assert served(go) == 404


def test_a_query_string_does_not_break_the_path():
    async def go(srv, st):
        body, sig = signed(assignment())
        return (await request(srv, post("/linear?x=1", body, sig)))[0]

    assert served(go) == 200


def test_a_huge_body_is_refused_without_reading_it():
    # Открытый порт не должен давать способ занять память процесса приёма.
    async def go(srv, st):
        raw = (f"POST /linear HTTP/1.1\r\nHost: x\r\n"
               f"Content-Length: {webhook.MAX_BODY_BYTES + 1}\r\n\r\n").encode()
        return (await request(srv, raw))[0]

    assert served(go) == 413


def test_a_chunked_body_is_refused_loudly():
    # Разобрать его неправильно значит не сойтись подписью — и выглядело бы
    # это как чужой запрос, а не как неподдержанная кодировка.
    async def go(srv, st):
        raw = (b"POST /linear HTTP/1.1\r\nHost: x\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n")
        return (await request(srv, raw))[0]

    assert served(go) == 411


def test_a_broken_content_length_is_refused():
    async def go(srv, st):
        raw = (b"POST /linear HTTP/1.1\r\nHost: x\r\n"
               b"Content-Length: abc\r\n\r\n")
        return (await request(srv, raw))[0]

    assert served(go) == 400


def test_the_signature_header_is_matched_case_insensitively():
    # Прокси нормализует регистр заголовков как ему угодно.
    async def go(srv, st):
        body, sig = signed(assignment())
        raw = (f"POST /linear HTTP/1.1\r\nHost: x\r\n"
               f"Content-Length: {len(body)}\r\n"
               f"LINEAR-SIGNATURE: {sig}\r\n\r\n").encode() + body
        return (await request(srv, raw))[0]

    assert served(go) == 200


def test_a_garbage_request_line_does_not_kill_the_server():
    async def go(srv, st):
        first = (await request(srv, b"HELLO\r\n\r\n"))[0]
        body, sig = signed(assignment())
        second = (await request(srv, post("/linear", body, sig)))[0]
        return first, second

    first, second = served(go)
    assert first == 400 and second == 200


# ── одно событие, два потребителя ────────────────────────────────────────

class Feed:
    def __init__(self, boom=False):
        self.seen = []
        self.boom = boom

    async def on_event(self, payload):
        if self.boom:
            raise RuntimeError("лента упала")
        self.seen.append(payload)
        return {"sent": True}


def test_an_event_for_a_human_goes_to_the_feed():
    # Один вебхук на двух потребителей: очередь и человека. Второй адрес и
    # второй секрет ради этого заводить незачем.
    st, r = mk()
    r.feed = Feed()
    code, body = run(r.dispatch(assignment(assignee="someone-else")))
    assert code == 200 and body["sent"] is True
    assert len(r.feed.seen) == 1


def test_a_status_change_goes_to_the_feed():
    st, r = mk()
    r.feed = Feed()
    p = assignment()
    p["updatedFrom"] = {"stateId": "было"}
    run(r.dispatch(p))
    assert len(r.feed.seen) == 1


def test_an_assignment_to_the_agent_is_not_shown_to_the_human():
    # Сессия сама отчитается в задаче; сообщение «назначено на агента» — шум
    # о собственном действии человека.
    st, r = mk()
    r.feed = Feed()
    code, body = run(r.dispatch(assignment()))
    assert body["agent"] == "queued" and r.feed.seen == []


def test_a_broken_feed_does_not_make_the_tracker_retry():
    # Повтор доставки нужен очереди, а не чату: у ленты своя память о том,
    # что уже показано, и ретрай показал бы то же самое второй раз.
    st, r = mk()
    r.feed = Feed(boom=True)
    code, body = run(r.dispatch(assignment(assignee="someone-else")))
    assert code == 200 and body.get("feed-failed") is True


def test_without_a_feed_nothing_is_shown_and_nothing_breaks():
    st, r = mk()
    code, body = run(r.dispatch(assignment(assignee="someone-else")))
    assert code == 200 and body["ignored"]


# ── заначка для проверки выката ──────────────────────────────────────────

def test_a_queued_assignment_leaves_a_stash_for_the_deploy_check():
    # Выкат случится через часы-дни, по событию хостинга, где известно
    # только имя ветки. Заначка — продукт, а не послесловие инсталляции.
    from agent_factory.deploy import stashed_task
    st, r = mk()
    run(r.dispatch(assignment(identifier="TASK-9")))
    stash = run(stashed_task(st, "TASK-9"))
    assert stash["issue_id"] == "i1" and stash["identifier"] == "TASK-9"


def test_a_deduped_delivery_does_not_rewrite_the_stash():
    from agent_factory.deploy import task_key
    st, r = mk()
    run(r.dispatch(assignment(identifier="TASK-9")))
    st.kv[task_key("TASK-9")] = "первая"
    run(r.dispatch(assignment(identifier="TASK-9")))
    assert st.kv[task_key("TASK-9")] == "первая"


# ── подпись не обязана быть похожей на подпись ───────────────────────────

def test_a_non_ascii_signature_is_refused_not_crashed():
    """`hmac.compare_digest` на СТРОКАХ требует ASCII и на чём угодно другом
    кидает `TypeError`. Заголовки читаются как latin-1, так что один байт
    >0x7F в подписи давал 500 со стектрейсом вместо 401, предупреждение
    «подпись не сошлась» не писалось никогда, а трекер ретраил доставку,
    которую надо было отвергнуть. Снаружи — способ лить трейсы одним curl."""
    assert webhook.verify(SECRET, b"{}", "подпись") is False


def test_a_non_ascii_signature_over_http_is_a_clean_401():
    async def go(srv, st):
        body, _ = signed(assignment())
        head = (f"POST /linear HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Linear-Signature: подпись\r\n\r\n").encode("utf-8")
        code, out = await request(srv, head + body)
        return code, out, queued(st)

    code, out, q = served(go)
    assert code == 401 and out["error"] == "подпись не сошлась" and q == []


def test_a_signature_longer_than_the_digest_is_just_wrong():
    # Сравнение байтов не должно падать и на длине не в 64 символа.
    assert webhook.verify(SECRET, b"{}", "a" * 200) is False


# ── один порт, несколько путей ───────────────────────────────────────────

def test_two_receivers_on_one_path_refuse_to_start():
    """Пути задаются независимыми ручками (`TRACKER_WEBHOOK_PATH`,
    `DEPLOY_WEBHOOK_PATH`). Совпадение молча затирало приёмник трекера
    деплойным: порт жив, /health отвечает 200, а события трекера получают 401
    по чужому секрету — то есть работа не начинается ни по одной задаче."""
    st = Store()
    with pytest.raises(ValueError):
        Server(Receiver(st, "agent:queue:x", AGENT, secret=SECRET),
               path="/hook", routes={"/hook": (object(), "x-sig")})


def test_the_health_of_a_path_that_was_not_raised_is_not_ok():
    """Иначе проверка живости не умеет заметить ровно ту поломку, под которую
    её и добавили: секрет выката задан, разбирать событие нечем, путь не
    поднят — а `/deploy/health` бодро отвечает 200."""
    async def go(srv, st):
        live = await request(
            srv, b"GET /linear/health HTTP/1.1\r\nHost: x\r\n\r\n")
        dead = await request(
            srv, b"GET /deploy/health HTTP/1.1\r\nHost: x\r\n\r\n")
        return live[0], dead[0]

    live, dead = served(go)
    assert live == 200 and dead == 404


def test_the_port_is_taken_before_anything_is_served():
    """`start` отдельно от `run`: инсталляция должна узнать про занятый порт
    СЕЙЧАС. Внутри таски эта ошибка была невидима — процесс жив, в журнале
    «вебхук слушает», а постановки задачи нет ни одной."""
    async def main():
        st = Store()
        first = Server(Receiver(st, "agent:queue:x", AGENT, secret=SECRET),
                       bind="127.0.0.1", port=0)
        await first.start()
        second = Server(Receiver(st, "agent:queue:x", AGENT, secret=SECRET),
                        bind="127.0.0.1", port=first.port)
        try:
            with pytest.raises(OSError):
                await second.start()
        finally:
            first.srv.close()

    asyncio.run(main())


# ── страницы на том же порту ─────────────────────────────────────────────

class Page:
    """Минимальная страница: отвечает как договорено — (тип, тело)."""

    def __init__(self):
        self.seen = []

    async def handle(self, method, target, headers, body):
        self.seen.append((method, target, body))
        return 200, ("text/html; charset=utf-8", "<h1>тут</h1>")


def test_a_page_answers_html_on_the_same_port():
    """Второй порт пришлось бы выставлять прокси и открывать в брандмауэре —
    ради страницы это отдельный разговор с админом на каждую инсталляцию."""
    async def go(srv, st):
        srv.pages["/queue"] = Page()
        raw = await request(srv, b"GET /queue?t=x HTTP/1.1\r\nHost: x\r\n\r\n",
                            whole=True)
        return raw

    out = served(go)
    assert b"200 OK" in out and b"text/html" in out and "тут".encode() in out


def test_a_page_sees_the_query_and_the_headers():
    async def go(srv, st):
        p = Page()
        srv.pages["/queue"] = p
        await request(srv, b"GET /queue?t=secret HTTP/1.1\r\nHost: x\r\n"
                           b"Cookie: signing=abc\r\n\r\n", whole=True)
        return p.seen

    seen = served(go)
    assert seen[0][0] == "GET" and "t=secret" in seen[0][1]


def test_an_unknown_page_is_still_a_404():
    async def go(srv, st):
        srv.pages["/queue"] = Page()
        return (await request(srv, b"GET /nope HTTP/1.1\r\nHost: x\r\n\r\n"))[0]

    assert served(go) == 404


def test_a_page_does_not_shadow_the_event_receiver():
    # Приёмник события и страница живут в разных реестрах: путь трекера
    # обязан остаться приёмником, что бы ни повесили рядом.
    async def go(srv, st):
        srv.pages["/queue"] = Page()
        body, sig = signed(assignment())
        code, out = await request(srv, post("/linear", body, sig))
        return code, out

    code, out = served(go)
    assert code == 200 and out["agent"] == "queued"


# ── потолок кругов (DEV-86) ───────────────────────────────────────────────

class CappingTracker(Tracker):
    def __init__(self):
        super().__init__()
        self.assigned, self.comments = [], []

    def assign(self, issue_id, user_id):
        self.assigned.append((issue_id, user_id)); return {"already": False}

    def comment(self, issue_id, body):
        self.comments.append((issue_id, body))


def test_the_third_return_goes_to_the_human_not_to_the_agent():
    """Три рабочих захода уже были — следующее назначение на агента сессию
    не поднимает: задача уходит принимающему с комментарием. Решает код по
    следам заходов, а не модель по прозе."""
    tr = CappingTracker()
    st, r = mk(tracker=tr, rounds=lambda ident: 3, reviewer_id="rev-1")
    assert run(r.enqueue(assignment())) == "rounds-exhausted"
    assert not queued(st)
    assert tr.assigned == [("i1", "rev-1")]
    ((_, body),) = tr.comments
    # Проверяем не буквы «Круги исчерпаны», а то, ради чего комментарий
    # пишется: первой строкой — исполнимое действие, число заходов названо, и
    # машинная фраза не оставлена без человеческой расшифровки (DEV-108).
    assert body.splitlines()[0].startswith("⛔ Агент отработал")
    assert "положенное число заходов (3)" in body
    # «Круги исчерпаны» — машинная формулировка; человеку то же
    # говорится словами (DEV-108, таблица расшифровок владельца).
    assert "отработал по" in body and "дальше решаешь ты" in body
    # Окно снято: следующее назначение — решение человека, оно должно пройти.
    assert "agent:started:i1" not in st.claimed


def test_below_the_cap_the_work_goes_on():
    tr = CappingTracker()
    st, r = mk(tracker=tr, rounds=lambda ident: 0, reviewer_id="rev-1")
    assert run(r.enqueue(assignment())) == "queued"
    assert queued(st) and not tr.assigned


def test_the_reviewer_may_give_another_round_by_hand():
    """Назначил сам принимающий — его право дать четвёртый круг."""
    tr = CappingTracker()
    st, r = mk(tracker=tr, rounds=lambda ident: 5, reviewer_id="who-1")
    assert run(r.enqueue(assignment())) == "queued"
    assert queued(st) and not tr.assigned


def test_the_reviewer_may_be_asked_asynchronously():
    async def who():
        return "who-1"
    tr = CappingTracker()
    st, r = mk(tracker=tr, rounds=lambda ident: 5, reviewer_id=who)
    assert run(r.enqueue(assignment())) == "queued"


def test_a_broken_round_counter_does_not_block_the_work(caplog):
    def boom(ident):
        raise RuntimeError("каталог не читается")
    tr = CappingTracker()
    st, r = mk(tracker=tr, rounds=boom, reviewer_id="rev-1")
    assert run(r.enqueue(assignment())) == "queued"
    assert "круги не сосчитаны" in caplog.text


def test_a_tracker_that_cannot_reassign_still_stops_the_round(caplog):
    class Deaf(CappingTracker):
        def assign(self, issue_id, user_id):
            raise RuntimeError("трекер лёг")
    st, r = mk(tracker=Deaf(), rounds=lambda ident: 3, reviewer_id="rev-1")
    assert run(r.enqueue(assignment())) == "rounds-exhausted"
    assert not queued(st) and "переназначить не вышло" in caplog.text
