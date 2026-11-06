"""Передача приёма из лички: кто принимает работу агента.

Смысл тот же, что у раннера: без принимающего не остаться — пустой ключ,
битая запись, вышедший срок, лежачее хранилище кончаются владельцем. Плюс
своё: срок называют по-местному, а хранится он в UTC; делегат обязан быть в
карте, иначе сдача до него не доедет; запись обязана читаться раннером (это
закреплено отдельно, в core/test_reviewer.py).
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agent_factory.people import People
from agent_factory.reviewer import (HELP, Reviewer, format_status, parse_deadline,
                                    pick_user, record_active)

ANDREY, PAVEL, KATYA, PASHA = "u-andrey", "u-pavel", "u-katya", "u-pasha"
# 09:00 UTC — это 12:00 по Москве. Пояс в тестах явный, чтобы «до 18:00»
# проверялось как местное время, а не как UTC.
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
MSK = ZoneInfo("Europe/Moscow")
USERS = [{"id": ANDREY, "name": "Андрей", "displayName": "andrey", "active": True},
         {"id": PAVEL, "name": "Иван Иванов", "displayName": "pavel", "active": True},
         {"id": KATYA, "name": "Катя", "displayName": "katya", "active": True}]


class Store:
    def __init__(self):
        self.kv, self.ttl = {}, {}
        self.fails = False

    async def get(self, key):
        if self.fails:
            raise RuntimeError("redis молчит")
        return self.kv.get(key)

    async def put(self, key, value, ttl):
        if self.fails:
            raise RuntimeError("redis молчит")
        self.kv[key] = value
        self.ttl[key] = ttl

    async def drop(self, key):
        if self.fails:
            raise RuntimeError("redis молчит")
        self.kv.pop(key, None)


class Transport:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None, rich=False):
        self.sent.append((str(chat_id), text))
        return "1"

    def to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]


class Tracker:
    def __init__(self, users):
        self._users = users
        self.calls = 0
        self.fail = ""

    def users(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.fail)
        return list(self._users)


def mk(*, people=None, users=None, tracker=True, tz="Europe/Moscow"):
    st, tp = Store(), Transport()
    tr = Tracker(USERS if users is None else users)
    clock = {"now": NOW}
    rev = Reviewer(st, people or People({ANDREY: "435", PAVEL: "777"},
                                        owner_user_id=ANDREY),
                   tp, tr if tracker else None, key="agent:reviewer:x",
                   tz_name=tz, owner_name="Андрей", now=lambda: clock["now"])
    return st, tp, tr, rev, clock


def run(c): return asyncio.run(c)


def stored(st):
    return json.loads(st.kv["agent:reviewer:x"])


# ── срок: как его называют ───────────────────────────────────────────────

def test_a_duration_in_hours_is_counted_from_now():
    assert parse_deadline("на 3 часа", NOW, MSK) == (NOW + timedelta(hours=3), None)


def test_a_duration_without_the_preposition_is_understood():
    assert parse_deadline("3 дня", NOW, MSK) == (NOW + timedelta(days=3), None)


def test_a_bare_unit_means_one_of_it():
    assert parse_deadline("на неделю", NOW, MSK) == (NOW + timedelta(days=7), None)


def test_forever_means_no_deadline_and_no_error():
    assert parse_deadline("", NOW, MSK) == (None, None)
    assert parse_deadline("навсегда", NOW, MSK) == (None, None)


def test_an_ambiguous_unit_is_refused_rather_than_guessed():
    # «м» — это и минуты, и месяцы.
    when, err = parse_deadline("на 2 м", NOW, MSK)
    assert when is None and "м" in err


def test_zero_is_refused():
    assert parse_deadline("на 0 часов", NOW, MSK)[1]


def test_a_time_today_still_ahead_stays_today():
    # Сейчас 12:00 по Москве; 18:00 — сегодня, то есть 15:00 UTC.
    when, err = parse_deadline("до 18:00", NOW, MSK)
    assert err is None and when == datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)


def test_a_time_today_that_has_passed_rolls_to_tomorrow():
    # «до 9:00», сказанное в полдень, — завтрашние 9:00, а не отказ.
    when, err = parse_deadline("до 9:00", NOW, MSK)
    assert err is None and when == datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)


def test_tomorrow_without_a_time_is_ten_in_the_morning_local():
    # 10:00 по Москве — это 07:00 UTC. Прочитанное как UTC дало бы 10:00 UTC,
    # и делегат вернул бы работу владельцу на три часа позже, чем тот ждал.
    when, err = parse_deadline("до завтра", NOW, MSK)
    assert err is None and when == datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


def test_the_zone_is_the_one_passed_in_not_a_fixed_one():
    colombo = ZoneInfo("Asia/Colombo")          # UTC+5:30
    when, _ = parse_deadline("до завтра 10:00", NOW, colombo)
    assert when == datetime(2026, 9, 2, 4, 30, tzinfo=timezone.utc)


def test_an_explicit_date_is_read_in_the_local_zone():
    when, err = parse_deadline("до 2026-09-05 12:00", NOW, MSK)
    assert err is None and when == datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)


def test_a_moment_in_the_past_is_refused():
    when, err = parse_deadline("до 2026-08-01 12:00", NOW, MSK)
    assert when is None and "прошёл" in err


def test_a_nonexistent_time_is_refused():
    assert parse_deadline("до 25:00", NOW, MSK)[1]


def test_a_nonexistent_date_is_refused():
    assert "нет" in parse_deadline("до 2026-02-30", NOW, MSK)[1]


def test_gibberish_is_refused_not_ignored():
    when, err = parse_deadline("через час", NOW, MSK)
    assert when is None and err


# ── запись ───────────────────────────────────────────────────────────────

def rec(**over):
    r = {"linear_user_id": PAVEL, "name": "Иван", "telegram_id": "777", "until": ""}
    r.update(over)
    return json.dumps(r, ensure_ascii=False)


def test_an_indefinite_record_is_active():
    assert record_active(rec(), NOW)["linear_user_id"] == PAVEL


def test_an_expired_record_is_not_a_delegate():
    assert record_active(rec(until=(NOW - timedelta(minutes=1)).isoformat()), NOW) is None


def test_a_deadline_exactly_now_is_over():
    assert record_active(rec(until=NOW.isoformat()), NOW) is None


def test_a_naive_deadline_is_read_as_utc():
    naive = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert record_active(rec(until=naive), NOW) is not None


def test_a_broken_record_is_not_a_delegate():
    assert record_active("{не json", NOW) is None
    assert record_active(json.dumps({"name": "кто-то"}), NOW) is None
    assert record_active(rec(until="вчера"), NOW) is None
    assert record_active("", NOW) is None


# ── кто принимает ────────────────────────────────────────────────────────

def test_without_a_record_the_owner_receives_the_work():
    st, tp, tr, rev, _ = mk()
    assert run(rev.user_id()) == ANDREY
    assert run(rev.chat_id()) == "435"


def test_a_record_names_the_delegate():
    st, tp, tr, rev, _ = mk()
    st.kv["agent:reviewer:x"] = rec()
    assert run(rev.user_id()) == PAVEL
    assert run(rev.chat_id()) == "777"


def test_an_expired_record_falls_back_to_the_owner_and_is_removed():
    # Иначе `/reviewer` показывал бы делегата, к которому работа уже не едет,
    # а раннер и приём расходились бы в том, кто принимает.
    st, tp, tr, rev, clock = mk()
    run(rev.handover(PAVEL, "Иван", "777", NOW + timedelta(hours=1)))
    clock["now"] = NOW + timedelta(hours=2)
    assert run(rev.user_id()) == ANDREY
    assert "agent:reviewer:x" not in st.kv


def test_a_record_still_in_force_is_kept():
    st, tp, tr, rev, clock = mk()
    run(rev.handover(PAVEL, "Иван", "777", NOW + timedelta(hours=1)))
    clock["now"] = NOW + timedelta(minutes=30)
    assert run(rev.user_id()) == PAVEL
    assert "agent:reviewer:x" in st.kv


def test_a_dead_store_means_the_owner_not_a_crash():
    st, tp, tr, rev, _ = mk()
    st.fails = True
    assert run(rev.user_id()) == ANDREY


def test_the_record_is_stored_without_a_ttl():
    # Срок живёт полем `until`. Срок на ключе — второй источник правды о том
    # же, и расхождение между ними никто бы не заметил.
    st, tp, tr, rev, _ = mk()
    run(rev.handover(PAVEL, "Иван", "777", None))
    assert st.ttl["agent:reviewer:x"] == 0


def test_the_record_carries_the_fields_the_runner_reads():
    st, tp, tr, rev, _ = mk()
    run(rev.handover(PAVEL, "Иван", "777", NOW + timedelta(days=1), set_by="435"))
    r = stored(st)
    assert r["linear_user_id"] == PAVEL and r["telegram_id"] == "777"
    assert r["name"] == "Иван" and r["set_by"] == "435"
    assert r["until"] == (NOW + timedelta(days=1)).isoformat()


# ── команда /reviewer ────────────────────────────────────────────────────

def test_without_arguments_the_status_and_the_help_are_shown():
    st, tp, tr, rev, _ = mk()
    assert run(rev.command("435", "")) == "show"
    text = tp.to("435")[0]
    assert "Андрей" in text and "владелец" in text and HELP in text


def test_handing_over_writes_the_record_and_answers_both_people():
    st, tp, tr, rev, _ = mk()
    assert run(rev.command("435", "Иван на неделю")) == "set"
    r = stored(st)
    assert r["linear_user_id"] == PAVEL and r["telegram_id"] == "777"
    assert r["until"] == (NOW + timedelta(days=7)).isoformat()
    assert "Иван" in tp.to("435")[0]
    # Делегату — в личку, иначе он узнает о новой обязанности только когда
    # получит первую сданную работу.
    assert any("передал тебе" in t for t in tp.to("777"))


def test_the_deadline_is_stored_in_utc_from_the_local_zone():
    st, tp, tr, rev, _ = mk()
    run(rev.command("435", "Иван до завтра 10:00"))
    assert stored(st)["until"] == "2026-09-02T07:00:00+00:00"
    # И в ответе оба времени: срок называют по-местному, хранят в UTC.
    assert "02.09 10:00 Europe/Moscow (07:00 UTC)" in tp.to("435")[0]


def test_the_zone_comes_from_the_installation():
    st, tp, tr, rev, _ = mk(tz="Asia/Colombo")
    run(rev.command("435", "Иван до завтра 10:00"))
    assert stored(st)["until"] == "2026-09-02T04:30:00+00:00"


def test_handing_over_for_good_stores_no_deadline():
    st, tp, tr, rev, _ = mk()
    run(rev.command("435", "Иван"))
    assert stored(st)["until"] == ""
    assert "бессрочно" in tp.to("435")[0]


def test_reset_returns_the_work_to_the_owner():
    st, tp, tr, rev, _ = mk()
    run(rev.command("435", "Иван"))
    assert run(rev.command("435", "сброс")) == "reset"
    assert "agent:reviewer:x" not in st.kv
    assert run(rev.user_id()) == ANDREY


def test_an_unknown_person_is_refused_and_nothing_is_written():
    st, tp, tr, rev, _ = mk()
    assert run(rev.command("435", "Вася")) == "not-found"
    assert "agent:reviewer:x" not in st.kv
    assert "Вася" in tp.to("435")[0]


def test_several_matches_ask_to_clarify_rather_than_guess():
    users = USERS + [{"id": PASHA, "name": "Ивар", "active": True}]
    st, tp, tr, rev, _ = mk(users=users)
    assert run(rev.command("435", "Ив")) == "ambiguous"
    assert "agent:reviewer:x" not in st.kv
    assert "Иван" in tp.to("435")[0] and "Ивар" in tp.to("435")[0]


def test_a_person_outside_the_map_is_refused_with_a_hint():
    # Лента незнакомым не пишет: сдача до такого делегата не доедет вовсе.
    st, tp, tr, rev, _ = mk()
    assert run(rev.command("435", "Катя")) == "not-in-map"
    assert "agent:reviewer:x" not in st.kv
    assert "AGENT_PEOPLE_MAP" in tp.to("435")[0]


def test_a_deactivated_person_is_not_found():
    users = [dict(u, active=(u["id"] != PAVEL)) for u in USERS]
    st, tp, tr, rev, _ = mk(users=users)
    assert run(rev.command("435", "Иван")) == "not-found"


def test_a_bad_deadline_is_refused_before_asking_the_tracker():
    st, tp, tr, rev, _ = mk()
    assert run(rev.command("435", "Иван до 25:00")) == "deadline"
    assert tr.calls == 0 and "agent:reviewer:x" not in st.kv


def test_a_deadline_without_a_name_is_refused():
    assert run(mk()[3].command("435", "на неделю")) == "no-name"


def test_without_a_tracker_nobody_can_be_found():
    st, tp, tr, rev, _ = mk(tracker=False)
    assert run(rev.command("435", "Иван")) == "no-tracker"
    assert "agent:reviewer:x" not in st.kv


def test_a_dead_tracker_is_an_answer_not_a_crash():
    st, tp, tr, rev, _ = mk()
    tr.fail = "502"
    assert run(rev.command("435", "Иван")) == "tracker"
    assert "502" in tp.to("435")[0]


def test_a_dead_store_is_reported_not_swallowed():
    st, tp, tr, rev, _ = mk()
    st.fails = True
    assert run(rev.command("435", "Иван")) == "store"
    assert "записать" in tp.to("435")[-1]


def test_the_status_names_the_delegate_and_when_the_owner_is_back():
    text = format_status({"name": "Иван", "until": "2026-09-02T07:00:00+00:00"},
                         "Андрей", "Europe/Moscow")
    assert "Иван" in text and "02.09 10:00 Europe/Moscow (07:00 UTC)" in text
    assert "снова Андрей" in text


def test_the_status_without_a_record_names_the_owner():
    assert "Андрей" in format_status(None, "Андрей", "Europe/Moscow")
    assert "владелец" in format_status(None, "", "Europe/Moscow")


# ── поиск человека ───────────────────────────────────────────────────────

def test_an_exact_name_wins_over_a_longer_match():
    users = [{"id": "1", "name": "Иван"}, {"id": "2", "name": "Иван Иванов"}]
    assert pick_user(users, "Иван")[0]["id"] == "1"


def test_a_prefix_of_any_word_of_the_name_finds_the_person():
    assert pick_user(USERS, "иван")[0]["id"] == PAVEL


def test_the_display_name_is_searched_too():
    assert pick_user(USERS, "@pavel")[0]["id"] == PAVEL


def test_yo_and_ye_are_the_same_letter():
    users = [{"id": "1", "name": "Фёдор"}]
    assert pick_user(users, "федор")[0]["id"] == "1"


def test_a_name_that_starts_like_the_preposition_is_still_a_name():
    # «Настя до завтра»: разрез идёт по «на»/«до» С ПРОБЕЛОМ после, иначе
    # имя резалось бы на «» и «стя до завтра».
    st, tp, tr, rev, _ = mk()
    assert run(rev.command("435", "Настя до завтра")) == "not-found"
    assert "«Настя»" in tp.to("435")[0]
