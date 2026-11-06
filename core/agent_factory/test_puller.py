"""Бэклог горит сам — но в два потолка.

Тяга — единственное место, где фабрика начинает работу без человека. Поэтому
половина тестов здесь про то, чего она делать НЕ должна: брать больше, чем
условлено, брать задачу, которую человек ещё дописывает, и считать взятое
так, чтобы потолок можно было обойти перезапуском.
"""
import asyncio
import json

from agent_factory.puller import (DAY_SEC, Puller, TAKEN_KEY, batch_since,
                                  is_incident, next_up, rank, taken_today)

DUTY = "дежурный"
AGENT = "agent-uuid"
TEAM = "team-uuid"


def issue(ident, priority=3, age=9999, labels=(), iid=None):
    return {"id": iid or f"i-{ident}", "identifier": ident, "title": ident,
            "priority": priority, "created_at": 1_000_000 - age,
            "labels": {"nodes": [{"name": n} for n in labels]}}


class Store:
    def __init__(self, boom=False):
        self.kv, self.boom = {}, boom

    async def get(self, key):
        if self.boom:
            raise RuntimeError("redis молчит")
        return self.kv.get(key)

    async def put(self, key, value, ttl):
        if self.boom:
            raise RuntimeError("redis молчит")
        self.kv[key] = value


class Tracker:
    def __init__(self, backlog=(), in_flight=0, fail_assign=None):
        self.backlog = list(backlog)
        self._in_flight = in_flight
        self.assigned = []
        self.fail_assign = fail_assign

    def unassigned_issues(self, team_id):
        return list(self.backlog)

    def assigned_issues(self, user_id, states):
        return [{"id": f"busy-{n}"} for n in range(self._in_flight)]

    def assign(self, issue_id, user_id):
        if self.fail_assign == issue_id:
            raise RuntimeError("трекер отказал")
        self.assigned.append(issue_id)
        # Назначенная задача уходит из бэклога — как в настоящем трекере,
        # иначе тяга в тестах берёт одно и то же по кругу.
        self.backlog = [i for i in self.backlog if i["id"] != issue_id]
        return {"id": issue_id}


def mk(backlog=(), in_flight=0, **kw):
    st, tr = Store(), Tracker(backlog, in_flight)
    kw.setdefault("max_in_flight", 2)
    kw.setdefault("max_per_day", 8)
    return st, tr, Puller(tr, st, AGENT, TEAM, incident_label=DUTY, **kw)


NOW = 1_000_000


# ── порядок ──────────────────────────────────────────────────────────────

def test_an_incident_goes_before_anything_else():
    got = next_up([issue("TASK-1", priority=1), issue("TASK-2", priority=4,
                                                     labels=[DUTY])], DUTY, NOW)
    assert [i["identifier"] for i in got] == ["TASK-2", "TASK-1"]


def test_higher_priority_first_and_older_first_within_it():
    got = next_up([issue("TASK-3", priority=2, age=100_000),
                   issue("TASK-1", priority=1),
                   issue("TASK-2", priority=2, age=200_000)], DUTY, NOW)
    assert [i["identifier"] for i in got] == ["TASK-1", "TASK-2", "TASK-3"]


def test_no_priority_is_not_urgent():
    """0 в трекере значит «не решили». Считать его срочным — значит пускать
    вперёд всё, чего никто не оценил."""
    got = next_up([issue("TASK-1", priority=0), issue("TASK-2", priority=4)],
                  DUTY, NOW)
    assert [i["identifier"] for i in got] == ["TASK-2", "TASK-1"]


def test_tech_debt_is_taken_when_nothing_else_is_left():
    # Низкий приоритет не выбрасывается, он заполняет простой.
    got = next_up([issue("TASK-9", priority=4)], DUTY, NOW)
    assert [i["identifier"] for i in got] == ["TASK-9"]


# ── чего не берём ────────────────────────────────────────────────────────

def test_a_task_still_being_written_is_left_alone():
    """Человек дописывает постановку прямо сейчас — взять её на середине фразы
    худшее, что тяга может сделать."""
    assert next_up([issue("TASK-1", age=60)], DUTY, NOW, min_age_sec=600) == []


def test_a_fresh_incident_is_taken_at_once():
    # Инцидент заводится потому, что ждать нельзя: выдержка к нему не применяется.
    got = next_up([issue("TASK-1", age=5, labels=[DUTY])], DUTY, NOW,
                  min_age_sec=600)
    assert [i["identifier"] for i in got] == ["TASK-1"]


def test_a_skip_label_keeps_a_task_for_the_human():
    got = next_up([issue("TASK-1", labels=["человеку"]), issue("TASK-2")],
                  DUTY, NOW, skip_labels=("человеку",))
    assert [i["identifier"] for i in got] == ["TASK-2"]


def test_an_issue_without_an_id_is_not_taken():
    assert next_up([{"identifier": "TASK-1"}], DUTY, NOW) == []


# ── потолки ──────────────────────────────────────────────────────────────

def test_it_takes_up_to_the_concurrency_cap():
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2"), issue("TASK-3")])
    out = asyncio.run(p.once(now=NOW))
    assert out["взято"] == 2 and tr.assigned == ["i-TASK-1", "i-TASK-2"]


def test_a_busy_agent_gets_nothing_new():
    st, tr, p = mk([issue("TASK-1")], in_flight=2)
    out = asyncio.run(p.once(now=NOW))
    assert out["взято"] == 0 and "занято 2" in out["почему"]
    assert tr.assigned == []


def test_one_free_slot_takes_exactly_one():
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2")], in_flight=1)
    assert asyncio.run(p.once(now=NOW))["взято"] == 1


def test_the_daily_cap_stops_it():
    st, tr, p = mk([issue("TASK-1")])
    st.kv[TAKEN_KEY] = json.dumps([NOW - 10] * 8)
    out = asyncio.run(p.once(now=NOW))
    assert out["взято"] == 0 and "за сутки уже 8" in out["почему"]


def test_yesterdays_marks_do_not_count():
    st, tr, p = mk([issue("TASK-1")])
    st.kv[TAKEN_KEY] = json.dumps([NOW - DAY_SEC - 1] * 8)
    assert asyncio.run(p.once(now=NOW))["взято"] == 1


def test_the_daily_cap_survives_a_restart():
    """Счётчик живёт в хранилище, а не в памяти процесса: иначе потолок
    обходится перезапуском приёма, и ночь с пустым бэклогом сжигает бюджет."""
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2")])
    asyncio.run(p.once(now=NOW))
    st2, tr2 = st, Tracker([issue("TASK-3")])
    p2 = Puller(tr2, st2, AGENT, TEAM, incident_label=DUTY, max_in_flight=2,
                max_per_day=2)
    assert asyncio.run(p2.once(now=NOW))["взято"] == 0


def test_the_daily_cap_counts_taken_not_offered():
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2")])
    asyncio.run(p.once(now=NOW))
    assert len(json.loads(st.kv[TAKEN_KEY])) == 2


# ── когда вокруг ломается ────────────────────────────────────────────────

def test_a_silent_store_holds_the_pull_rather_than_letting_it_loose():
    """Нечитаемый счётчик — это «не знаю, сколько взял». Считать его нулём
    значит снять дневной потолок ровно тогда, когда за ним некому следить."""
    st, tr, p = mk([issue("TASK-1")])
    st.boom = True
    assert asyncio.run(p.once(now=NOW))["взято"] == 0


def test_a_silent_tracker_takes_nothing():
    st, tr, p = mk([issue("TASK-1")])
    tr.unassigned_issues = lambda team: (_ for _ in ()).throw(RuntimeError("нет"))
    assert asyncio.run(p.once(now=NOW))["взято"] == 0


def test_a_refused_assignment_does_not_eat_the_daily_budget():
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2")])
    tr.fail_assign = "i-TASK-1"
    out = asyncio.run(p.once(now=NOW))
    assert out["взято"] == 1 and tr.assigned == ["i-TASK-2"]
    assert len(json.loads(st.kv[TAKEN_KEY])) == 1


def test_without_an_agent_or_a_team_it_does_nothing():
    st, tr = Store(), Tracker([issue("TASK-1")])
    p = Puller(tr, st, "", TEAM)
    assert asyncio.run(p.once(now=NOW))["взято"] == 0
    p2 = Puller(tr, st, AGENT, "")
    assert asyncio.run(p2.once(now=NOW))["взято"] == 0
    assert tr.assigned == []


def test_marks_older_than_a_day_are_forgotten():
    assert taken_today([NOW - DAY_SEC - 1, NOW - 5], NOW) == [NOW - 5]


# ── ровный темп: порция за окно ──────────────────────────────────────────

def test_a_batch_is_taken_and_then_the_pull_waits():
    """Дневной потолок один не спасает от залпа: восемь задач можно взять и за
    минуту, освободись слоты подряд. Человеку нужно ровно — две за два часа."""
    st, tr, p = mk([issue(f"TASK-{i}") for i in range(1, 6)],
                   per_window=2, window_sec=7200)
    assert asyncio.run(p.once(now=NOW))["взято"] == 2
    tr._in_flight = 0                       # обе сразу закончились
    out = asyncio.run(p.once(now=NOW + 60))
    assert out["взято"] == 0 and "порция выбрана" in out["почему"]


def test_the_next_batch_comes_when_the_window_slides():
    st, tr, p = mk([issue(f"TASK-{i}") for i in range(1, 6)],
                   per_window=2, window_sec=7200)
    asyncio.run(p.once(now=NOW))
    tr._in_flight = 0
    assert asyncio.run(p.once(now=NOW + 7199))["взято"] == 0
    assert asyncio.run(p.once(now=NOW + 7201))["взято"] == 2


def test_the_countdown_only_goes_down():
    """Оно и выглядело обманом: скользящее окно считало от старейшей метки, и
    стоило взять новую задачу, как отсчёт прыгал вверх — «12 минут», потом
    «26». Пачка теперь фиксированная: окно открывает первая её задача."""
    st, tr, p = mk([issue(f"TASK-{i}") for i in range(1, 8)],
                   per_window=2, window_sec=7200)
    asyncio.run(p.once(now=NOW))                 # пачка открыта в NOW
    was = asyncio.run(p.next_batch_in(now=NOW + 600))
    later = asyncio.run(p.next_batch_in(now=NOW + 1200))
    assert was == 6600 and later == 6000         # только убывает


def test_the_waiting_answer_says_how_long():
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2"), issue("TASK-3")],
                   per_window=2, window_sec=7200)
    asyncio.run(p.once(now=NOW))
    tr._in_flight = 0
    out = asyncio.run(p.once(now=NOW + 1800))
    assert "через 90 мин" in out["почему"]


def test_the_daily_cap_still_wins_over_the_batch():
    st, tr, p = mk([issue(f"TASK-{i}") for i in range(1, 6)],
                   per_window=2, window_sec=7200)
    st.kv[TAKEN_KEY] = json.dumps([NOW - DAY_SEC + 10] * 8)
    out = asyncio.run(p.once(now=NOW))
    assert out["взято"] == 0 and "за сутки" in out["почему"]


def test_without_a_batch_size_the_pace_is_off():
    # Ноль в порции — прежнее поведение: берём, как только есть слот.
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2"), issue("TASK-3")])
    assert asyncio.run(p.once(now=NOW))["взято"] == 2
    tr._in_flight = 0
    assert asyncio.run(p.once(now=NOW + 60))["взято"] == 1


# ── потолки правятся на лету ─────────────────────────────────────────────

def test_limits_can_be_changed_without_a_restart():
    """Перезапуск приёма ради «возьми сегодня не восемь, а четыре» — это
    оборванная сессия и потерянный час работы."""
    st, tr, p = mk([])
    got = asyncio.run(p.tune({"max_per_day": 4, "per_window": 1}))
    assert (got["max_per_day"], got["per_window"]) == (4, 1)
    assert (p.max_per_day, p.per_window) == (4, 1)


def test_changed_limits_survive_a_restart():
    st, tr, p = mk([])
    asyncio.run(p.tune({"max_per_day": 3}))
    again = Puller(tr, st, AGENT, TEAM, max_in_flight=2, max_per_day=8)
    asyncio.run(again._restore())
    assert again.max_per_day == 3


def test_nonsense_is_refused_not_applied():
    # Страница открыта в интернете: опечатка в поле не должна превращаться в
    # сотню задач за ночь.
    st, tr, p = mk([])
    got = asyncio.run(p.tune({"max_per_day": 500, "max_in_flight": "две",
                              "window_sec": 10, "чужое": 1}))
    assert got == p.limits()
    assert p.max_per_day == 8 and p.max_in_flight == 2


def test_a_saved_limit_out_of_bounds_is_ignored_on_restore():
    st, tr, p = mk([])
    st.kv[p.limits_key] = json.dumps({"max_per_day": 999, "per_window": 2})
    asyncio.run(p._restore())
    assert p.max_per_day == 8 and p.per_window == 2


def test_the_countdown_says_when_the_next_batch_comes():
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2"), issue("TASK-3")],
                   per_window=2, window_sec=7200)
    assert asyncio.run(p.next_batch_in(now=NOW)) == 0
    asyncio.run(p.once(now=NOW))
    assert asyncio.run(p.next_batch_in(now=NOW + 1800)) == 5400


def test_without_a_pace_the_countdown_is_zero():
    st, tr, p = mk([issue("TASK-1")])
    asyncio.run(p.once(now=NOW))
    assert asyncio.run(p.next_batch_in(now=NOW + 10)) == 0


def test_a_batch_opened_by_its_first_task_not_by_the_clock():
    marks = [NOW, NOW + 30]
    start, taken = batch_since(marks, NOW + 60, 7200)
    assert start == NOW and taken == 2


def test_an_expired_batch_opens_a_new_one():
    marks = [NOW, NOW + 30]
    start, taken = batch_since(marks, NOW + 7201, 7200)
    assert (start, taken) == (0.0, 0)


def test_a_half_filled_batch_still_has_room():
    st, tr, p = mk([issue("TASK-1"), issue("TASK-2")], per_window=2,
                   window_sec=7200, max_in_flight=1)
    assert asyncio.run(p.once(now=NOW))["взято"] == 1       # слот один
    tr._in_flight = 0
    assert asyncio.run(p.once(now=NOW + 60))["взято"] == 1   # добор в ту же пачку
    assert asyncio.run(p.next_batch_in(now=NOW + 61)) == 7139


# ── потолок незакрытых: упор в приёмку, а не в производство ──────────────

class Counting(Tracker):
    """Трекер, который умеет сказать, сколько у команды не закрыто."""

    def __init__(self, backlog=(), in_flight=0, open_now=0, boom=False):
        super().__init__(backlog, in_flight)
        self.open_now, self.boom, self.asked = open_now, boom, 0

    def open_count(self, team_id, states):
        self.asked += 1
        self.states = states
        if self.boom:
            raise RuntimeError("трекер молчит")
        return self.open_now


def mk_open(open_now=0, backlog=(), max_open=7, boom=False, **kw):
    st, tr = Store(), Counting(backlog, 0, open_now, boom)
    kw.setdefault("max_in_flight", 2)
    kw.setdefault("max_per_day", 0)
    return st, tr, Puller(tr, st, AGENT, TEAM, incident_label=DUTY,
                          max_open=max_open, **kw)


def test_the_pull_stops_when_the_review_queue_is_full():
    """Узкое место — приёмка, а не производство: агенты сдают быстрее, чем
    человек подписывает. Не разобрал — новое не берут."""
    st, tr, p = mk_open(open_now=7, backlog=[issue("TASK-1")])
    out = asyncio.run(p.once(now=NOW))
    assert out["взято"] == 0 and "у человека 7 из 7" in out["почему"]
    assert tr.assigned == []


def test_the_pull_resumes_once_the_queue_is_drained():
    # Разобрал — снова берут, без перезапуска и без ручки.
    st, tr, p = mk_open(open_now=7, backlog=[issue("TASK-1")])
    assert asyncio.run(p.once(now=NOW))["взято"] == 0
    tr.open_now = 5
    assert asyncio.run(p.once(now=NOW + 60))["взято"] == 1


def test_only_the_review_states_are_counted():
    """Считаем имена, а не тип: «In Progress» и «In Review» у Linear оба
    `started`, а копится только второе — работу ограничивают слоты."""
    st, tr, p = mk_open(open_now=1, backlog=[issue("TASK-1")],
                        open_states=("На приёмке", "In Review"))
    asyncio.run(p.once(now=NOW))
    assert tr.states == ("На приёмке", "In Review")


def test_room_below_the_cap_is_used():
    st, tr, p = mk_open(open_now=5, backlog=[issue(f"TASK-{i}") for i in range(4)])
    # Места два (слоты), запас по незакрытым тоже два — берём два.
    assert asyncio.run(p.once(now=NOW))["взято"] == 2


def test_the_cap_bounds_the_batch_even_with_free_slots():
    st, tr, p = mk_open(open_now=6, backlog=[issue(f"TASK-{i}") for i in range(4)])
    assert asyncio.run(p.once(now=NOW))["взято"] == 1      # до потолка одна


def test_a_silent_tracker_does_not_read_as_free():
    """«Не знаю, сколько не закрыто» не должно означать «свободно»."""
    st, tr, p = mk_open(open_now=0, backlog=[issue("TASK-1")], boom=True)
    out = asyncio.run(p.once(now=NOW))
    assert out["взято"] == 0 and "не спросить" in out["почему"]


def test_without_the_cap_the_tracker_is_not_asked():
    st, tr, p = mk_open(open_now=99, backlog=[issue("TASK-1")], max_open=0)
    assert asyncio.run(p.once(now=NOW))["взято"] == 1
    assert tr.asked == 0


def test_a_tracker_that_cannot_count_does_not_block_the_pull():
    # Площадка со своим трекером без такого метода работает как раньше.
    st, tr = Store(), Tracker([issue("TASK-1")], 0)
    p = Puller(tr, st, AGENT, TEAM, max_in_flight=2, max_per_day=0, max_open=7)
    assert asyncio.run(p.once(now=NOW))["взято"] == 1


def test_the_daily_cap_can_be_turned_off():
    st, tr, p = mk_open(open_now=0, backlog=[issue("TASK-1"), issue("TASK-2")],
                        max_open=7)
    assert p.max_per_day == 0
    assert asyncio.run(p.once(now=NOW))["взято"] == 2


# ── потолок — сумма двух стопок человека, кто бы ни был исполнителем ───────
#
# Решение 13.09.2026 (DEV-96): с провалом, уезжающим в «не проверено» в любом
# режиме, у человека утром две стопки — одну мержит, вторую разбирает. Потолок
# считает обе; «проверка идёт» не считается никогда.

class Board(Tracker):
    """Трекер, считающий ровно как настоящий `open_count`: по имени
    состояния, без оглядки на исполнителя."""

    def __init__(self, backlog=(), rows=()):
        super().__init__(backlog, 0)
        self.rows, self.states = list(rows), None

    def open_count(self, team_id, states):
        self.states = states
        return sum(1 for state, _who in self.rows if state in states)


def rows(review=0, unverified=0, verifying=0, unverified_on_agent=0):
    return ([("In Review", "human")] * review
            + [("Not Verified", "human")] * unverified
            + [("Not Verified", AGENT)] * unverified_on_agent
            + [("Verifying", "qa")] * verifying)


def test_both_stacks_count_toward_the_cap():
    """4 на приёмке + 3 в «не проверено» = 7: тяга упирается, хотя первая
    стопка одна и не полная."""
    tr = Board([issue("TASK-1")], rows(review=4, unverified=3))
    p = Puller(tr, Store(), AGENT, TEAM, max_in_flight=2, max_per_day=0, max_open=7)
    out = asyncio.run(p.once(now=NOW))
    assert out["взято"] == 0 and "7 из 7" in out["почему"]
    assert set(p.open_states) == {"In Review", "Not Verified"}


def test_the_assignee_does_not_change_the_count():
    """«Не проверено» на агенте место у человека занимает так же: он её
    разберёт, когда агент сдастся."""
    tr = Board([issue("TASK-1")], rows(review=4, unverified=1, unverified_on_agent=2))
    p = Puller(tr, Store(), AGENT, TEAM, max_in_flight=2, max_per_day=0, max_open=7)
    assert asyncio.run(p.once(now=NOW))["взято"] == 0
    # Настоящий счётчик знает только команду и состояния — исполнителя ему
    # не передают, значит и считать по нему он не может.
    assert tr.states is not None


def test_verifying_is_never_counted_even_if_configured():
    """Иначе проверка блокирует сама себя: проверки ждут в очереди, тяга ждёт
    проверок."""
    tr = Board([issue("TASK-1")], rows(review=3, unverified=2, verifying=5))
    p = Puller(tr, Store(), AGENT, TEAM, max_in_flight=2, max_per_day=0, max_open=7,
               open_states=("In Review", "Not Verified", "Verifying"))
    assert "Verifying" not in p.open_states
    assert asyncio.run(p.once(now=NOW))["взято"] == 1
    assert "Verifying" not in tr.states


def test_the_installation_names_its_own_states_and_the_checking_one_is_still_dropped():
    tr = Board([issue("TASK-1")], [("На приёмке", "h"), ("Не проверено", "h"),
                                  ("Проверяется", "qa")] * 3)
    p = Puller(tr, Store(), AGENT, TEAM, max_in_flight=2, max_per_day=0, max_open=7,
               open_states=("На приёмке", "Не проверено", "проверяется"),
               never_count=("Проверяется",))
    assert p.open_states == ("На приёмке", "Не проверено")
    assert asyncio.run(p.once(now=NOW))["взято"] == 1      # 6 < 7


def test_the_limits_line_names_both_stacks():
    p = Puller(Tracker(), Store(), AGENT, TEAM, max_open=7)
    assert "не больше 7 у человека (In Review + Not Verified)" in p.limits_line()
