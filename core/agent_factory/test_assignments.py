"""Назначил задачу агенту — он взялся, без кнопки.

У клиента это приносит вебхук трекера. У площадки без публичного HTTPS
вебхуку взяться неоткуда, и раньше там работу начинала только кнопка под
карточкой — снаружи это выглядело как разное поведение одной системы.
"""
import asyncio
import json

from agent_factory.assignments import Assignments


class Store:
    def __init__(self, slow=False):
        self.lists: dict[str, list[str]] = {}
        self.claimed: set[str] = set()
        self.read_fails = False
        # Уступать управление на чтении: так два круга успевают вклиниться
        # друг в друга — иначе гонка в тесте не воспроизводится.
        self.slow = slow

    async def read_all(self, key):
        if self.read_fails:
            raise RuntimeError("redis молчит")
        got = list(self.lists.get(key) or [])
        if self.slow:
            await asyncio.sleep(0)
        return got

    async def enqueue(self, queue, element):
        self.lists.setdefault(queue, []).append(element)

    async def put(self, key, value, ttl):
        self.kv = getattr(self, "kv", {}); self.kv[key] = value

    async def get(self, key):
        return getattr(self, "kv", {}).get(key)

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class Tracker:
    def __init__(self, issues=None):
        self.issues = issues if issues is not None else [
            {"id": "u1", "identifier": "DEV-60", "title": "починить экспорт",
             "url": "https://l/DEV-60", "state": {"name": "Todo", "type": "unstarted"}}]
        self.asked = []
        self.fail = None

    def assigned_issues(self, user_id, states):
        if self.fail:
            raise RuntimeError(self.fail)
        self.asked.append((user_id, tuple(states)))
        return list(self.issues)


def mk(issues=None, agent="агент"):
    st, tr = Store(), Tracker(issues)
    return st, tr, Assignments(tr, st, "agent:queue:x", agent, interval_sec=0)


def queued(st):
    return [json.loads(x) for x in st.lists.get("agent:queue:x", [])]


# ── основное ─────────────────────────────────────────────────────────────

def test_an_assigned_task_goes_to_the_queue():
    st, tr, a = mk()
    assert asyncio.run(a.tick()) == 1
    task = queued(st)[0]
    assert task["issue_id"] == "u1" and task["identifier"] == "DEV-60"
    assert task["url"] == "https://l/DEV-60"


def test_only_not_started_states_are_asked_for():
    # Состояние задачи и есть отметка: сессия переводит её в работу, и
    # следующий круг её уже не видит. Без фильтра агент брал бы в работу всё,
    # что на нём висит, включая сделанное.
    st, tr, a = mk()
    asyncio.run(a.tick())
    assert tr.asked == [("агент", ("backlog", "unstarted"))]


def test_a_task_already_in_the_queue_is_not_queued_twice():
    # Перед ней может стоять часовая сессия, и всё это время она честно ждёт
    # слота в состоянии «не начато».
    st, tr, a = mk()
    asyncio.run(a.tick())
    st.claimed.clear()                      # захват истёк, задача всё ещё в очереди
    assert asyncio.run(a.tick()) == 0
    assert len(queued(st)) == 1


def test_overlapping_ticks_do_not_double_queue():
    # Круг может идти дольше интервала, если трекер отвечает медленно. Без
    # захвата оба круга увидели бы пустую очередь и поставили задачу дважды.
    st, tr = Store(slow=True), Tracker()
    a = Assignments(tr, st, "agent:queue:x", "агент", interval_sec=0)

    async def both():
        await asyncio.gather(a.tick(), a.tick())

    asyncio.run(both())
    assert len(queued(st)) == 1


def test_a_second_task_is_not_blocked_by_the_first():
    st, tr, a = mk(issues=[
        {"id": "u1", "identifier": "DEV-60", "title": "a", "url": "",
         "state": {"type": "unstarted"}},
        {"id": "u2", "identifier": "DEV-61", "title": "b", "url": "",
         "state": {"type": "backlog"}}])
    assert asyncio.run(a.tick()) == 2
    assert {t["issue_id"] for t in queued(st)} == {"u1", "u2"}


# ── отказы ───────────────────────────────────────────────────────────────

def test_a_tracker_that_fails_does_not_queue_anything():
    st, tr, a = mk()
    tr.fail = "401"
    assert asyncio.run(a.tick()) == 0
    assert queued(st) == []


def test_an_unreadable_queue_does_not_stop_the_work():
    # Пропустить назначение хуже, чем изредка поставить дважды: задача висит
    # на агенте, а сессии нет, и человек этого не видит.
    st, tr, a = mk()
    st.read_fails = True
    assert asyncio.run(a.tick()) == 1


def test_without_an_agent_nothing_is_polled():
    st, tr, a = mk(agent="")
    assert asyncio.run(a.tick()) == 0
    assert tr.asked == []


def test_an_issue_without_an_id_is_skipped():
    st, tr, a = mk(issues=[{"identifier": "DEV-62", "title": "без id",
                            "state": {"type": "unstarted"}}])
    assert asyncio.run(a.tick()) == 0


# ── дежурный или обычная работа ──────────────────────────────────────────

def incident_issue():
    return {"id": "u9", "identifier": "DEV-70", "title": "прод лежит",
            "url": "https://l/DEV-70",
            "description": "## Симптом\n\nAPI отдаёт 502 со вчера.\n\n## Что уже смотрели\n\nничего",
            "state": {"type": "unstarted"},
            "labels": {"nodes": [{"name": "Дежурный"}]}}


def test_a_labelled_issue_starts_a_duty_session():
    # Раньше этим занимался только вебхук у первого клиента: на площадке с
    # опросом лейбл ничего не давал, и инцидент уезжал обычной работой.
    st, tr = Store(), Tracker([incident_issue()])
    a = Assignments(tr, st, "agent:queue:x", "агент", incident_label="дежурный")
    asyncio.run(a.tick())
    task = queued(st)[0]
    assert task["kind"] == "incident"
    assert task["symptom"] == "API отдаёт 502 со вчера."
    assert "дежурный" in task["source"]


def test_the_label_is_matched_case_insensitively():
    # Лейбл ставит человек руками: «Дежурный» и «дежурный» — один и тот же.
    st, tr = Store(), Tracker([incident_issue()])
    a = Assignments(tr, st, "agent:queue:x", "агент", incident_label="дежурный")
    asyncio.run(a.tick())
    assert queued(st)[0]["kind"] == "incident"


def test_without_the_label_it_is_ordinary_work():
    st, tr = Store(), Tracker([{**incident_issue(), "labels": {"nodes": []}}])
    a = Assignments(tr, st, "agent:queue:x", "агент", incident_label="дежурный")
    asyncio.run(a.tick())
    assert "kind" not in queued(st)[0]


def test_an_installation_without_a_duty_label_never_escalates():
    # Развилки «дежурный или работа» у инсталляции может не быть вовсе.
    st, tr = Store(), Tracker([incident_issue()])
    a = Assignments(tr, st, "agent:queue:x", "агент", incident_label="")
    asyncio.run(a.tick())
    assert "kind" not in queued(st)[0]


def test_an_empty_label_name_does_not_match_an_empty_setting():
    # Без проверки «лейбл не задан» пустая строка совпала бы с пустым именем
    # лейбла, и задача уехала бы дежурному на ровном месте.
    st, tr = Store(), Tracker([{**incident_issue(),
                                "labels": {"nodes": [{"name": ""}]}}])
    a = Assignments(tr, st, "agent:queue:x", "агент", incident_label="")
    asyncio.run(a.tick())
    assert "kind" not in queued(st)[0]


def test_a_polled_assignment_leaves_a_stash_for_the_deploy_check():
    # Третий путь постановки — и тот же след: проверке выката всё равно, как
    # задача попала в очередь.
    from agent_factory.deploy import stashed_task
    st, tr, a = mk([incident_issue()])
    asyncio.run(a.tick())
    assert asyncio.run(stashed_task(st, "DEV-70"))["issue_id"] == "u9"


# ── потолок кругов (DEV-86): опрос — тот же потолок, что у вебхука ───────────

def test_the_poller_hands_an_exhausted_task_to_the_human():
    st, tr = Store(), Tracker()
    tr.assigned, tr.comments = [], []
    tr.assign = lambda issue_id, user_id: tr.assigned.append((issue_id, user_id))
    tr.comment = lambda issue_id, body: tr.comments.append(body)
    a = Assignments(tr, st, "agent:queue:x", "агент", interval_sec=0,
                    rounds=lambda ident: 3, reviewer_id="rev-1")
    assert asyncio.run(a.tick()) == 0
    assert not queued(st)
    assert tr.assigned == [("u1", "rev-1")]
    assert tr.comments[0].splitlines()[0].startswith("⛔ Агент отработал")
    assert "дальше решаешь ты" in tr.comments[0]


def test_the_poller_queues_below_the_cap():
    st, tr = Store(), Tracker()
    a = Assignments(tr, st, "agent:queue:x", "агент", interval_sec=0,
                    rounds=lambda ident: 0, reviewer_id="rev-1")
    assert asyncio.run(a.tick()) == 1
