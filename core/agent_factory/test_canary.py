"""Канарейка обязана краснеть — иначе она зелёный огонёк, а не проверка.

Три поломки маршрута за 14–15.09.2026 (код возврата в трубе, досдача по не
тому статусу, отсутствующий перевод в `Done` по мержу) выглядели работающими
и не уронили ни одного теста. Поэтому здесь по тесту на каждую строку таблицы
«чем покраснеет» из постановки: пропуск суток, остановка на стадии с её
именем, лишние круги, доезд до конца, — и отдельно на то, что канарейка не
ходит в чужие репозитории.
"""
import asyncio
import calendar
import json

from agent_factory.canary import (Canary, DONE, LAST_KEY, RUN_KEY, SINCE_KEY,
                                  Stages, rounds_of, stage_of)

HOUR = 2
DAY = 24 * 3600


def run(coro):
    return asyncio.run(coro)


def at(hour: int, day: int = 16, minute: int = 0) -> float:
    return float(calendar.timegm((2026, 9, day, hour, minute, 0, 0, 0, 0)))


class Store:
    def __init__(self):
        self.kv, self.claimed = {}, set()

    async def get(self, key):
        return self.kv.get(key)

    async def put(self, key, value, ttl):
        self.kv[key] = value

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class Transport:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append(text)
        return "mid-1"

    @property
    def last(self):
        return self.sent[-1] if self.sent else ""


class Tracker:
    """Доска, которой можно сказать, в каком состоянии задача сейчас."""

    def __init__(self, state="Todo", kind="unstarted", boom=""):
        self.state, self.kind, self.boom = state, kind, boom
        self.created, self.assigned = [], []
        self.gone = False

    def create_issue(self, team_id, title, description, role="", project_id=""):
        if self.boom == "create":
            raise RuntimeError("трекер отказал")
        self.created.append({"team": team_id, "title": title,
                             "description": description, "project": project_id})
        return {"id": "i-1", "identifier": "DEV-900",
                "url": "https://linear.app/x/issue/DEV-900"}

    def assign(self, issue_id, user_id):
        self.assigned.append((issue_id, user_id))
        return {"already": False}

    def issue_by_identifier(self, identifier):
        if self.boom == "read":
            raise RuntimeError("трекер молчит")
        if self.gone:
            return None
        return {"id": "i-1", "identifier": identifier,
                "state": {"name": self.state, "type": self.kind}}


class Hosting:
    def __init__(self, pr=None, boom=False):
        self.pr, self.boom = pr, boom
        self.merged = []

    def open_request(self, identifier):
        return self.pr

    def merge(self, request):
        if self.boom:
            raise RuntimeError("PR конфликтует")
        self.merged.append(request)
        return f"смержен PR #{request['number']}"


PR = {"number": "7", "repo": "org/canary", "url": "u",
      "source_ref": "agent/DEV-900", "target_ref": "main"}


def mk(tracker=None, hosting=None, **kw):
    st, tp = Store(), Transport()
    tr = tracker or Tracker()
    opts = {"repo": "org/canary", "path": "canary/heartbeat.md", **kw}
    c = Canary(tr, st, tp, team_id="t-1", agent_user_id="u-agent",
               notify_chat="chat-1", hosting=hosting, hour_utc=HOUR, **opts)
    return c, st, tp, tr


def flying(store, now, stage="работа", started=None, trail=None):
    """Положить в хранилище идущий прогон на нужной стадии."""
    store.kv[RUN_KEY] = json.dumps(
        {"issue": "DEV-900", "id": "i-1", "url": "u",
         "начата": started if started is not None else now,
         "стадия": stage, "с": now,
         "след": trail or [{"стадия": stage, "когда": now}], "сказано": []},
        ensure_ascii=False)


def last_of(store):
    return json.loads(store.kv.get(LAST_KEY) or "{}")


def run_of(store):
    return json.loads(store.kv.get(RUN_KEY) or "{}")


# ── строка таблицы: «канарейка идёт по расписанию» ───────────────────────

def test_the_canary_takes_off_at_its_hour():
    c, st, tp, tr = mk()
    got = run(c.once(at(HOUR)))
    assert got == {"старт": "DEV-900"}
    assert tr.assigned == [("i-1", "u-agent")]
    # Предмет обязан быть тривиальным и детерминированным: в тексте задачи
    # назван ровно один файл и ровно одна строка.
    body = tr.created[0]["description"]
    assert "canary/heartbeat.md" in body and "org/canary" in body
    assert run_of(st)["стадия"] == "заведена"


def test_it_does_not_take_off_outside_its_hour():
    c, st, tp, tr = mk()
    assert run(c.once(at(HOUR + 3))) == {}
    assert tr.created == []


def test_it_does_not_take_off_twice_in_a_day():
    """Приём перезапускают; два процесса в час старта завели бы две задачи."""
    c, st, tp, tr = mk()
    run(c.once(at(HOUR)))
    st.kv[RUN_KEY] = "{}"          # первый прогон как будто уже кончился
    st.kv[LAST_KEY] = "{}"         # и даже следа о нём не осталось
    assert run(c.once(at(HOUR, minute=30))) == {}
    assert len(tr.created) == 1


def test_a_skipped_day_is_heard():
    c, st, tp, tr = mk()
    st.kv[LAST_KEY] = json.dumps({"начата": at(HOUR, day=14), "issue": "DEV-800",
                                  "исход": "доехала"}, ensure_ascii=False)
    got = run(c.once(at(HOUR + 5, day=16)))
    assert got["пропуск"]
    assert "не летала" in tp.last and "DEV-800" in tp.last


def test_a_skipped_day_is_said_once_a_day():
    c, st, tp, tr = mk()
    st.kv[LAST_KEY] = json.dumps({"начата": at(HOUR, day=14)}, ensure_ascii=False)
    run(c.once(at(HOUR + 5, day=16)))
    run(c.once(at(HOUR + 6, day=16)))
    assert len(tp.sent) == 1


def test_a_canary_that_never_flew_at_all_is_heard_too():
    """Худший случай расписания: не взлетала НИ РАЗУ. Сравнивать не с чем, и
    без отметки «смотрю с» молчание читалось бы как «всё цело»."""
    c, st, tp, tr = mk()
    run(c.once(at(HOUR + 1, day=14)))          # первый тик: отметились
    assert json.loads(st.kv[SINCE_KEY])["когда"] == at(HOUR + 1, day=14)
    got = run(c.once(at(HOUR + 2, day=16)))    # через двое суток
    assert got["пропуск"]
    assert "прогонов не было ни одного" in tp.last


def test_a_take_off_that_failed_is_not_silence():
    c, st, tp, tr = mk(tracker=Tracker(boom="create"))
    got = run(c.once(at(HOUR)))
    assert "ошибка" in got
    assert "не завелась" in tp.last
    assert last_of(st)["исход"] == "не завелась"


# ── строка таблицы: «маршрут цел — доехала до Done» ──────────────────────

def test_it_walks_the_whole_route_and_says_how_long():
    c, st, tp, tr = mk(hosting=Hosting(PR))
    now = at(HOUR)
    run(c.once(now))
    for i, (state, kind) in enumerate(
            [("In Progress", "started"), ("Verifying", "started"),
             ("In Review", "started"), ("Done", "completed")], start=1):
        tr.state, tr.kind = state, kind
        got = run(c.once(now + i * 600))
    assert got == {"итог": "доехала", "кругов": 1}
    assert "доехала" in tp.last and "кругов 1" in tp.last
    assert "заведена" in tp.last and "подпись" in tp.last   # след в тревоге
    assert last_of(st)["исход"] == "доехала"
    assert run_of(st) == {}            # место свободно, завтра летит следующая


def test_the_route_is_walked_by_name_because_types_do_not_differ():
    """`In Progress`, `Verifying`, `In Review`, `Not Verified` — один тип
    `started`. По типу маршрут не разложить, и стадии совпали бы все."""
    assert stage_of("In Progress", "started") == "работа"
    assert stage_of("Verifying", "started") == "проверка"
    assert stage_of("In Review", "started") == "подпись"
    assert stage_of("Not Verified", "started") == "провалена"


def test_the_live_board_has_no_state_the_canary_does_not_know():
    """Имена статусов команды Development, как их отдаёт Linear."""
    live = {"Backlog": "заведена", "Todo": "заведена", "In Progress": "работа",
            "Verifying": "проверка", "In Review": "подпись",
            "Not Verified": "провалена", "Done": "доехала",
            "Canceled": "снята"}
    for name, stage in live.items():
        assert stage_of(name, "") == stage, name
    # Девятый статус доски — «Duplicate», тип `duplicate`. Он закрывающий, и
    # для прогона это конец, а не незнакомая стадия: иначе канарейка ждала бы
    # движения от закрытой задачи до самого срока.
    assert stage_of("Duplicate", "duplicate") == "снята"


def test_a_renamed_closing_state_still_counts_as_arrival():
    assert stage_of("Готово", "completed") == DONE
    assert stage_of("Отменено", "canceled") == "снята"


def test_an_unknown_state_is_red_and_not_silence():
    c, st, tp, tr = mk(tracker=Tracker(state="Blocked", kind="started"))
    flying(st, at(HOUR))
    got = run(c.once(at(HOUR, minute=10)))
    assert got["тревога"] == "вне маршрута"
    assert "Blocked" in tp.last and "не на маршруте" in tp.last
    # Второй раз про то же не долбим.
    run(c.once(at(HOUR, minute=20)))
    assert len(tp.sent) == 1


def test_a_failed_verdict_on_a_one_line_task_is_red():
    c, st, tp, tr = mk(tracker=Tracker(state="Not Verified", kind="started"))
    flying(st, at(HOUR), stage="проверка")
    got = run(c.once(at(HOUR, minute=5)))
    assert got == {"итог": "провалена"}
    assert "провалена" in tp.last
    assert last_of(st)["исход"] == "провалена"


def test_a_vanished_issue_ends_the_run_loudly():
    tr = Tracker()
    tr.gone = True
    c, st, tp, _ = mk(tracker=tr)
    flying(st, at(HOUR))
    assert run(c.once(at(HOUR, minute=5))) == {"итог": "задача исчезла"}
    assert "задачи нет в трекере" in tp.last


def test_a_silent_tracker_is_not_a_verdict():
    """«Трекер молчит» и «маршрут встал» обязаны различаться: закрыть прогон
    по упавшей сети значит соврать про дорогу."""
    c, st, tp, tr = mk(tracker=Tracker(boom="read"))
    flying(st, at(HOUR))
    assert run(c.once(at(HOUR, minute=5))) == {}
    assert tp.sent == []
    assert run_of(st)["issue"] == "DEV-900"


# ── строка таблицы: «встала на стадии — сказано, на какой» ───────────────

def test_a_stalled_stage_is_named():
    c, st, tp, tr = mk(tracker=Tracker(state="In Progress", kind="started"))
    now = at(HOUR)
    flying(st, now, stage="работа")
    got = run(c.once(now + 5401))
    assert "встала на стадии «работа»" in got["тревога"]
    assert "работа" in tp.last and "без движения" in tp.last


def test_a_stalled_stage_is_said_once():
    c, st, tp, tr = mk(tracker=Tracker(state="In Progress", kind="started"))
    now = at(HOUR)
    flying(st, now, stage="работа")
    run(c.once(now + 5401))
    run(c.once(now + 5500))
    assert len(tp.sent) == 1


def test_a_stage_inside_its_deadline_is_quiet():
    c, st, tp, tr = mk(tracker=Tracker(state="In Progress", kind="started"))
    now = at(HOUR)
    flying(st, now, stage="работа")
    assert run(c.once(now + 600))["тревога"] == ""
    assert tp.sent == []


def test_a_merge_that_never_closes_the_issue_is_red():
    """Ровно поломка 15.09.2026: перевода в `Done` по мержу не существовало.
    PR влит, задача навсегда в «In Review» — и это обязано покраснеть."""
    hosting = Hosting(PR)
    c, st, tp, tr = mk(tracker=Tracker(state="In Review", kind="started"),
                       hosting=hosting)
    now = at(HOUR)
    flying(st, now, stage="подпись")
    run(c.once(now + 60))
    assert hosting.merged == [PR]
    got = run(c.once(now + 3700))
    assert "встала на стадии «подпись»" in got["тревога"]


def test_the_canary_gives_up_so_tomorrow_can_fly():
    c, st, tp, tr = mk(tracker=Tracker(state="In Progress", kind="started"))
    now = at(HOUR)
    flying(st, now, stage="работа", started=now)
    run(c.once(now + 5401))            # первая тревога
    got = run(c.once(now + 6 * 3600))  # общий срок вышел
    assert got["тревога"] == "брошена"
    assert run_of(st) == {}
    assert last_of(st)["исход"] == "встала на «работа»"


# ── строка таблицы: «кругов не больше нормы» ─────────────────────────────

def test_rounds_are_counted_by_entries_into_work():
    trail = [{"стадия": "заведена"}, {"стадия": "работа"},
             {"стадия": "проверка"}, {"стадия": "работа"},
             {"стадия": "проверка"}, {"стадия": "работа"}]
    assert rounds_of(trail) == 3
    assert rounds_of([{"стадия": "заведена"}]) == 0


def test_three_rounds_are_red_even_though_it_arrived():
    c, st, tp, tr = mk(tracker=Tracker(state="Done", kind="completed"))
    now = at(HOUR)
    flying(st, now, stage="проверка", started=now - 3600,
           trail=[{"стадия": "заведена", "когда": now - 3600},
                  {"стадия": "работа", "когда": now - 3400},
                  {"стадия": "проверка", "когда": now - 3000},
                  {"стадия": "работа", "когда": now - 2000},
                  {"стадия": "проверка", "когда": now - 1500},
                  {"стадия": "работа", "когда": now - 900},
                  {"стадия": "проверка", "когда": now}])
    got = run(c.once(now + 60))
    assert got == {"итог": "доехала не с первого захода", "кругов": 3}
    assert "кругов 3" in tp.last and "норме 1" in tp.last
    assert "🐤" not in tp.last


# ── мерж: только свой репозиторий, только своя ветка ─────────────────────

def test_it_merges_its_own_pull_request():
    hosting = Hosting(PR)
    c, st, tp, tr = mk(tracker=Tracker(state="In Review", kind="started"),
                       hosting=hosting)
    now = at(HOUR)
    flying(st, now, stage="подпись")
    got = run(c.once(now + 60))
    assert hosting.merged == [PR]
    assert "смержен PR #7" in got["мерж"]


def test_it_never_merges_a_foreign_repository():
    """Граница узкая намеренно: канарейка живёт в репозитории-пустышке, и
    боевой PR она не вливает, даже если он назван её задачей."""
    hosting = Hosting({**PR, "repo": "client-b-org/dok-engine"})
    c, st, tp, tr = mk(tracker=Tracker(state="In Review", kind="started"),
                       hosting=hosting)
    now = at(HOUR)
    flying(st, now, stage="подпись")
    got = run(c.once(now + 60))
    assert hosting.merged == []
    assert "org/canary" in got["мерж"] and "dok-engine" in tp.last


def test_it_never_merges_a_branch_not_named_by_its_issue():
    hosting = Hosting({**PR, "source_ref": "feature/cleanup"})
    c, st, tp, tr = mk(tracker=Tracker(state="In Review", kind="started"),
                       hosting=hosting)
    flying(st, at(HOUR), stage="подпись")
    run(c.once(at(HOUR, minute=1)))
    assert hosting.merged == []
    assert "не названа задачей" in tp.last


def test_it_never_merges_into_something_other_than_the_main_branch():
    hosting = Hosting({**PR, "target_ref": "release/2026-09"})
    c, st, tp, tr = mk(tracker=Tracker(state="In Review", kind="started"),
                       hosting=hosting)
    flying(st, at(HOUR), stage="подпись")
    run(c.once(at(HOUR, minute=1)))
    assert hosting.merged == []
    assert "не основная ветка" in tp.last


def test_a_merge_that_did_not_go_through_is_heard():
    hosting = Hosting(PR, boom=True)
    c, st, tp, tr = mk(tracker=Tracker(state="In Review", kind="started"),
                       hosting=hosting)
    flying(st, at(HOUR), stage="подпись")
    got = run(c.once(at(HOUR, minute=1)))
    assert "мерж не прошёл" in got["мерж"] and "мерж не прошёл" in tp.last


def test_without_hosting_the_signing_stage_waits_for_a_human():
    """Срок на «подписи» мерил бы не маршрут, а сон владельца."""
    c, st, tp, tr = mk(tracker=Tracker(state="In Review", kind="started"))
    now = at(HOUR)
    flying(st, now, stage="подпись")
    assert run(c.once(now + 3700))["тревога"] == ""
    assert tp.sent == []


# ── выключенная канарейка говорит, почему ────────────────────────────────

def test_without_a_dummy_repository_there_is_no_canary():
    c, st, tp, tr = mk(repo="")
    assert not c.able
    assert run(c.once(at(HOUR))) == {"выключена": True}
    assert tr.created == []
    assert "AGENT_CANARY_REPO" in c.line()


def test_a_working_canary_says_what_it_does():
    c, st, tp, tr = mk(hosting=Hosting(PR))
    assert "02:00 UTC" in c.line() and "org/canary" in c.line()
    assert "мержит сама" in c.line()


def test_installation_may_rename_the_states():
    stages = Stages(progress="В работе", done="Готово")
    assert stage_of("В работе", "started", stages) == "работа"
    assert stage_of("Готово", "completed", stages) == DONE


# ── находки собственного код-ревью (DEV-103) ────────────────────────────

def test_waiting_for_a_human_is_not_a_red_route():
    """Без хостинга мерж за человеком, и общий срок не имеет права красить
    этим маршрут: канарейка мерила бы не дорогу, а сон владельца."""
    c, st, tp, tr = mk(tracker=Tracker(state="In Review", kind="started"))
    now = at(HOUR)
    flying(st, now, stage="подпись", started=now - 7 * 3600)
    got = run(c.once(now))
    assert got["тревога"] == "ждёт человека"
    assert "🔴" not in tp.last and "мерж за человеком" in tp.last
    # Место всё равно освобождено — завтрашняя канарейка обязана вылететь.
    assert run_of(st) == {}
    assert last_of(st)["исход"] == "ждёт человека"


def test_arriving_without_ever_working_is_red():
    """Самая тихая поломка: `Done` есть, а сессии не было. Зелёный отчёт тут
    был бы враньём — маршрут срезан."""
    c, st, tp, tr = mk(tracker=Tracker(state="Done", kind="completed"))
    now = at(HOUR)
    flying(st, now, stage="заведена", started=now - 600,
           trail=[{"стадия": "заведена", "когда": now - 600}])
    got = run(c.once(now))
    assert got == {"итог": "доехала мимо работы", "кругов": 0}
    assert "НЕ побывав в работе" in tp.last and "🐤" not in tp.last


def test_arriving_without_ever_being_verified_is_red():
    """Работа была, `Done` есть, а проверки на следе нет ни одной.

    Дорога сюда короткая и живая: сверка «в работе» (`reconcile.Stalled`)
    уводит задачу без сессии сразу в стопку человека, то есть на «подпись»,
    минуя проверку, — а свой PR канарейка мержит сама. Её срок на работе
    (90 мин) длиннее срока молчания у сверки (60 мин), так что сверка успевает
    первой, и без этой проверки прогон был бы зелёным.
    """
    c, st, tp, tr = mk(tracker=Tracker(state="Done", kind="completed"))
    now = at(HOUR)
    flying(st, now, stage="подпись", started=now - 3600,
           trail=[{"стадия": "заведена", "когда": now - 3600},
                  {"стадия": "работа", "когда": now - 3300},
                  {"стадия": "подпись", "когда": now - 600}])
    got = run(c.once(now))
    assert got == {"итог": "доехала мимо проверки", "кругов": 1}
    assert "проверки на маршруте не было" in tp.last and "🐤" not in tp.last


def test_a_full_route_with_verification_stays_green():
    """Обратная сторона той же проверки: полный след с «проверкой» обязан
    остаться зелёным, иначе новая тревога кричала бы на здоровом маршруте."""
    c, st, tp, tr = mk(tracker=Tracker(state="Done", kind="completed"))
    now = at(HOUR)
    flying(st, now, stage="подпись", started=now - 3600,
           trail=[{"стадия": "заведена", "когда": now - 3600},
                  {"стадия": "работа", "когда": now - 3300},
                  {"стадия": "проверка", "когда": now - 1200},
                  {"стадия": "подпись", "когда": now - 600}])
    got = run(c.once(now))
    assert got == {"итог": "доехала", "кругов": 1}
    assert "🐤" in tp.last


def test_a_tracker_that_never_answers_does_not_hold_the_place_forever():
    """Пока прогон висит, обход не доходит до `_idle` — то есть молчит и
    тревога о пропуске суток. Общий срок обязан действовать и здесь."""
    c, st, tp, tr = mk(tracker=Tracker(boom="read"))
    now = at(HOUR)
    flying(st, now, started=now - 7 * 3600)
    assert run(c.once(now)) == {}
    assert run_of(st) == {}
    assert "бросаю присмотр" in tp.last and "трекер молчит" in tp.last


def test_an_off_route_issue_does_not_hold_the_place_forever():
    c, st, tp, tr = mk(tracker=Tracker(state="Blocked", kind="started"))
    now = at(HOUR)
    flying(st, now, started=now - 7 * 3600)
    run(c.once(now))                  # первый раз: сказали «вне маршрута»
    run(c.once(now + 120))            # второй: срок вышел, место освобождаем
    assert run_of(st) == {}
    assert last_of(st)["исход"] == "вне маршрута"
