"""Жизнь задачи в чате: одно сообщение на задачу.

Каждый второй тест здесь про инцидент, который уже случался у первого
клиента: кнопки, оставшиеся под закрытой задачей; сдача, ушедшая в общий чат
вместо лички; дубли сообщений об одной задаче; потерянная при правке строка
статуса.
"""
import asyncio
import json

from agent_factory.intake import Button
from agent_factory.issue_feed import (KEEP, IssueFeed, issue_is_closed,
                                      status_change)
from agent_factory.people import People

AGENT = "u-agent"
TEAM = "t-dev"
ANDREY = "u-andrey"
PAVEL = "u-pavel"
KATYA = "u-katya"


class Markup:
    def safe(self, t): return (t or "").replace("*", "")
    def bold(self, t): return f"*{t}*"
    def link(self, t, u): return f"[{t}]({u})"
    def mention(self, chat, name): return f"@{name}<{chat}>"


class Transport:
    def __init__(self):
        self.markup = Markup()
        self.sent = []
        self.edits = []
        self.next_mid = 100
        self.edit_ok = True
        self.send_fails_for = ""

    async def send(self, chat_id, text, buttons=None, rich=False):
        self.next_mid += 1
        self.sent.append({"chat": str(chat_id), "text": text, "rich": rich,
                          "buttons": [b.text for b in (buttons or [])]})
        if self.send_fails_for and str(chat_id) == self.send_fails_for:
            return ""
        return str(self.next_mid)

    async def edit(self, chat_id, message_id, text, buttons=None, rich=False):
        if not self.edit_ok:
            return False
        self.edits.append({"chat": str(chat_id), "mid": str(message_id),
                           "text": text, "rich": rich,
                           "buttons": [b.text for b in (buttons or [])]})
        return True


class Store:
    def __init__(self):
        self.kv = {}
        self.lists = {}
        self.claimed = set()

    async def put(self, key, value, ttl): self.kv[key] = value
    async def get(self, key): return self.kv.get(key)
    # `drop` у redis сносит ключ, каким бы он ни был: строкой, списком,
    # захватом. Фейк, забывший про списки, соврал бы про чистку карты.
    async def drop(self, key):
        self.kv.pop(key, None)
        self.lists.pop(key, None)
        self.claimed.discard(key)
    async def append(self, key, value): self.lists.setdefault(key, []).append(value)
    async def read_all(self, key): return list(self.lists.get(key) or [])
    async def expire(self, key, ttl): pass

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class Tracker:
    def __init__(self, creator=""):
        self.assigned = []
        self.creator = creator
        self.fail = None

    def assign(self, issue_id, user_id):
        if self.fail:
            raise RuntimeError(self.fail)
        self.assigned.append((issue_id, user_id))
        return {}

    def creator_of(self, issue_id):
        return self.creator


class Reviewer:
    def __init__(self, uid=""): self.uid = uid
    async def user_id(self): return self.uid


class Handoff:
    """Сдача: инсталляция дописывает строку и вешает свои кнопки."""

    def __init__(self, active=True, boom=False):
        self.active = active
        self.boom = boom

    async def on_handoff(self, payload, text):
        if self.boom:
            raise RuntimeError("форж молчит")
        if not self.active:
            return text, []
        return text + "\nMR открыт", [Button("Мерж", "m"), Button("Отмена", "c")]


def mk(*, handoff=None, reviewer=None, notify_chat="общий", team_id="",
       creator="", people=None):
    st, tr, tp = Store(), Tracker(creator), Transport()
    feed = IssueFeed(
        st, tp, tr,
        people or People({ANDREY: "435", PAVEL: "777", KATYA: "888"},
                         owner_user_id=ANDREY, store=st),
        notify_chat=notify_chat, agent_user_id=AGENT, team_id=team_id,
        reviewer=reviewer, handoff=handoff)
    return st, tr, tp, feed


def assignment(issue="i1", who=PAVEL, stamp="s1", action="update",
               status="In Progress", state_type="started", **data):
    p = {"type": "Issue", "action": action, "webhookTimestamp": stamp,
         "data": {"id": issue, "identifier": "DEV-80", "title": "починить",
                  "url": "https://l/DEV-80", "assigneeId": who,
                  "assignee": {"id": who, "name": "Иван"},
                  "state": {"name": status, "type": state_type}, **data}}
    if action == "update":
        p["updatedFrom"] = {"assigneeId": None}
    return p


def handoff_event(who=ANDREY, stamp="h1", **kw):
    """Сдача: работа ушла ОТ агента к человеку."""
    p = assignment(who=who, stamp=stamp, **kw)
    p["updatedFrom"] = {"assigneeId": AGENT}
    return p


def run(c): return asyncio.run(c)


# ── назначение ───────────────────────────────────────────────────────────

def test_an_assignment_reaches_the_person_in_their_own_chat():
    # 05.09.2026: ЛЮБОЕ уведомление о задаче уходит человеку лично. Общий чат
    # для команды — это чужие задачи, то есть шум, а не сигнал.
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    assert len(tp.sent) == 1 and tp.sent[0]["chat"] == "777"
    assert "DEV-80" in tp.sent[0]["text"] and "@Иван<777>" in tp.sent[0]["text"]


def test_the_message_goes_out_with_markup():
    # Без разметки упоминание не пингует, а приезжает сырым синтаксисом.
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    assert tp.sent[0]["rich"] is True


def test_a_person_outside_the_map_gets_nothing():
    # Сообщение до него не доедет, а звать к чужой задаче всю команду незачем.
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment(who="u-чужой")))
    assert tp.sent == []


def test_an_edit_of_another_field_is_not_an_assignment():
    st, tr, tp, feed = mk()
    p = assignment()
    p["updatedFrom"] = {"description": "было"}
    run(feed.on_event(p))
    assert tp.sent == []


def test_an_active_character_in_the_title_cannot_break_the_message():
    # Заголовок пишет человек. Одна непарная `*` — отказ транспорта и молча
    # потерянное сообщение.
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment(title="почини *экспорт")))
    assert "*экспорт" not in tp.sent[0]["text"]


def test_the_same_delivery_twice_shows_one_message():
    # Второй раз не должно быть ни отправки, ни правки: трекер ретраит
    # доставку, а правка того же текста — это лишний запрос в мессенджер на
    # каждый ретрай.
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    run(feed.on_event(assignment()))
    assert len(tp.sent) == 1 and tp.edits == []


def test_a_reassignment_later_shows_up():
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    run(feed.on_event(assignment(stamp="s2", who=ANDREY)))
    # Второе назначение правит то же сообщение только в своём чате; в чате
    # Андрея его ещё нет, поэтому уходит новое.
    assert len(tp.sent) == 2 and tp.sent[1]["chat"] == "435"


def test_without_a_chat_for_anyone_nothing_is_sent():
    st, tr, tp, feed = mk(notify_chat="", people=People({}, owner_user_id=ANDREY))
    out = run(feed.on_event(assignment()))
    assert tp.sent == [] and out.get("sent") is not True


# ── статус правит то же сообщение ────────────────────────────────────────

def status_event(issue="i1", status="In Review", state_type="started"):
    return {"type": "Issue", "action": "update",
            "updatedFrom": {"stateId": "old"}, "webhookTimestamp": "s9",
            "data": {"id": issue, "identifier": "DEV-80",
                     "state": {"name": status, "type": state_type}}}


def test_a_status_change_edits_the_hanging_message():
    # Раньше на одну задачу приходило и назначение, и сдача, и исход
    # проверки — тремя неотличимыми сообщениями.
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    run(feed.on_event(status_event()))
    assert tp.edits and "In Review" in tp.edits[-1]["text"]
    assert len(tp.sent) == 1


def test_the_status_survives_a_later_mark():
    # Отметка о проверке выката дописывается позже статуса и не должна его
    # затирать.
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    run(feed.on_event(status_event()))
    run(feed.repaint("i1", mark="✅ выкат проверен"))
    assert "In Review" in tp.edits[-1]["text"]
    assert "выкат проверен" in tp.edits[-1]["text"]


def test_a_mark_survives_a_later_status():
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    run(feed.repaint("i1", mark="✅ выкат проверен"))
    run(feed.on_event(status_event()))
    assert "выкат проверен" in tp.edits[-1]["text"]


def test_repaint_keeps_the_buttons_it_did_not_touch():
    # Сдача приезжает одним событием, смена статуса — следующим через
    # секунду. Без сохранения кнопок «Мерж» исчезал бы сразу после появления.
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(handoff_event(who=PAVEL)))
    run(feed.on_event(status_event()))
    assert tp.edits[-1]["buttons"] == ["Мерж", "Отмена"]


def test_closing_the_issue_takes_the_buttons_away():
    # TASK-185: под закрытой задачей кнопки отвечали «не нашёл MR» и висели
    # для повтора, которому неоткуда взяться.
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(handoff_event(who=PAVEL)))
    run(feed.on_event(status_event(status="Done", state_type="completed")))
    assert tp.edits[-1]["buttons"] == []


def test_a_message_older_than_memory_is_not_edited():
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    st.kv.clear()                       # шапка протухла
    assert run(feed.repaint("i1", status="In Review")) == 0


# ── сдача работы ─────────────────────────────────────────────────────────

def test_a_handoff_adds_the_installation_buttons():
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(handoff_event()))
    assert tp.sent[0]["buttons"] == ["Мерж", "Отмена"]
    assert "MR открыт" in tp.sent[0]["text"]


def test_a_closed_issue_gets_no_handoff_buttons():
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(handoff_event(status="Done", state_type="completed")))
    assert tp.sent[0]["buttons"] == []


def test_a_failing_handoff_still_delivers_the_message():
    # Сдача без кнопок доедет; сдача, потерянная из-за осечки подписи, — нет.
    st, tr, tp, feed = mk(handoff=Handoff(boom=True))
    run(feed.on_event(handoff_event()))
    assert len(tp.sent) == 1 and tp.sent[0]["buttons"] == []


def test_without_a_handoff_there_are_no_buttons():
    # У инсталляции без хостинга репозиториев подписывать нечего.
    st, tr, tp, feed = mk()
    run(feed.on_event(handoff_event()))
    assert tp.sent[0]["buttons"] == []


def test_a_handover_between_people_gets_no_buttons():
    # Обычная переброска задачи человек→человек: подписывать нечего, а
    # кнопка «Мерж» под ней уведёт искать MR, которого нет.
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(assignment(who=ANDREY)))
    assert tp.sent[0]["buttons"] == []


def test_the_handoff_rule_looks_at_the_previous_assignee():
    # Не на автора события: агент ходит в трекер под ключом владельца, и
    # автором сдачи приезжает владелец. У первого клиента правило по автору
    # не сработало НИ РАЗУ — кнопок в проде не было ни на одной задаче.
    from agent_factory.issue_feed import is_agent_handoff
    assert is_agent_handoff({"updatedFrom": {"assigneeId": AGENT}}, AGENT)
    assert not is_agent_handoff({"updatedFrom": {"assigneeId": PAVEL}}, AGENT)
    assert not is_agent_handoff({"updatedFrom": {"title": "было"}}, AGENT)
    assert not is_agent_handoff({"updatedFrom": {"assigneeId": AGENT}}, "")
    # Площадка без агента: снятие исполнителя — «было пусто», и без явной
    # проверки пустое сравнялось бы с пустым именем агента.
    assert not is_agent_handoff({"updatedFrom": {"assigneeId": None}}, "")
    assert not is_agent_handoff({}, AGENT)


def test_a_second_handoff_edits_instead_of_posting_a_twin():
    # Ревью многостадийное: сдал → вернули → сдал снова. Каждая стадия
    # постила бы неотличимый дубль с тем же заголовком.
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(handoff_event()))
    run(feed.on_event(handoff_event(stamp="h2")))
    assert len(tp.sent) == 1 and tp.edits


def test_an_assignment_without_buttons_clears_the_old_ones():
    # Иначе клик уведёт подписывать работу по задаче, которая уехала дальше.
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(handoff_event()))
    feed.handoff = Handoff(active=False)
    run(feed.on_event(handoff_event(stamp="h2")))
    assert tp.edits[-1]["buttons"] == []


def test_a_handoff_does_not_touch_a_message_in_another_chat():
    # 31.08.2026 TASK-175: правка трогала ЛЮБОЕ сообщение задачи, в том числе
    # оставшееся в общем чате, и кнопки оказывались у всей команды.
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(assignment(who=PAVEL)))          # сообщение в чате 777
    run(feed.on_event(handoff_event(stamp="h2")))
    assert [e for e in tp.edits if e["chat"] == "777"] == []
    assert tp.sent[-1]["chat"] == "435"


def test_a_transport_that_cannot_edit_falls_back_to_a_new_message():
    # Сообщение могли удалить руками. Промолчать нельзя — человек не узнает
    # о назначении вовсе.
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment(who=ANDREY)))
    tp.edit_ok = False
    run(feed.on_event(assignment(who=ANDREY, stamp="s2")))
    assert len(tp.sent) == 2


# ── чужая команда ────────────────────────────────────────────────────────

def test_another_teams_issue_is_not_posted():
    st, tr, tp, feed = mk(team_id="t-наша")
    run(feed.on_event(assignment(teamId="t-чужая")))
    assert tp.sent == []


def test_our_teams_issue_is_posted():
    st, tr, tp, feed = mk(team_id="t-наша")
    run(feed.on_event(assignment(teamId="t-наша")))
    assert len(tp.sent) == 1


def test_without_a_team_filter_everything_is_posted():
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment(teamId="t-любая")))
    assert len(tp.sent) == 1


# ── возврат закрытой задачи автору ───────────────────────────────────────

def closed(issue="i1", who=ANDREY, stamp="c1", creator=PAVEL, **kw):
    return {"type": "Issue", "action": "update",
            "updatedFrom": {"stateId": "old"}, "webhookTimestamp": stamp,
            "data": {"id": issue, "identifier": "DEV-80", "assigneeId": who,
                     "creatorId": creator, "teamId": TEAM,
                     "state": {"name": "Done", "type": "completed"}, **kw}}


def test_a_closed_issue_goes_back_to_its_author():
    st, tr, tp, feed = mk()
    assert run(feed.on_event(closed())).get("returned") == PAVEL
    assert tr.assigned == [("i1", PAVEL)]


def test_an_issue_closed_on_someone_else_is_left_alone():
    # Двое людей ведут задачу между собой — это не «работа агента закрыта».
    # Оба не приёмники и оба в карте: иначе отказ ничего не доказывает.
    st, tr, tp, feed = mk()
    run(feed.on_event(closed(who=PAVEL, creator=KATYA)))
    assert tr.assigned == []


def test_the_delegate_counts_as_the_recipient_too():
    # Приём передают на время, и тогда задача доходит до делегата.
    st, tr, tp, feed = mk(reviewer=Reviewer(PAVEL))
    run(feed.on_event(closed(who=PAVEL, creator="u-кто-то",
                             **{"creatorId": "u-кто-то"})))
    assert tr.assigned == []    # автора нет в карте — возвращать некуда


def test_an_open_issue_is_not_returned():
    st, tr, tp, feed = mk()
    p = closed()
    p["data"]["state"] = {"name": "In Progress", "type": "started"}
    run(feed.on_event(p))
    assert tr.assigned == []


def test_an_edit_of_an_old_closed_issue_does_not_throw_it_around():
    # Иначе правка описания спустя недели швыряла бы задачу автору.
    st, tr, tp, feed = mk()
    p = closed()
    p["updatedFrom"] = {"description": "было"}
    run(feed.on_event(p))
    assert tr.assigned == []


def test_closing_in_two_mutations_still_returns():
    # Сначала статус, потом исполнитель: второе событие приезжает без
    # stateId, и по нему задача уже закрыта и уже не на агенте.
    st, tr, tp, feed = mk()
    p = closed()
    p["updatedFrom"] = {"assigneeId": AGENT}
    run(feed.on_event(p))
    assert tr.assigned == [("i1", PAVEL)]


def test_the_author_of_an_intake_issue_beats_the_tracker_creator():
    # У приёмных задач `creator` у всех один — владелец ключа, под которым
    # работает создание.
    st, tr, tp, feed = mk()
    run(feed.people.remember_author("i1", "777"))
    run(feed.on_event(closed(creator=ANDREY)))
    assert tr.assigned == [("i1", PAVEL)]


def test_an_author_who_is_the_owner_gets_nothing_back():
    # Он и так принимает работу. Закрыто на ДЕЛЕГАТЕ, иначе автор совпал бы с
    # исполнителем и проверка не понадобилась бы.
    st, tr, tp, feed = mk(reviewer=Reviewer(PAVEL))
    run(feed.on_event(closed(who=PAVEL, creator=ANDREY)))
    assert tr.assigned == []


def test_an_author_who_is_the_agent_gets_nothing_back():
    # Назначение на агента ставит новую сессию — задача пошла бы на второй
    # круг. Агента в карту заводить незачем, но если он там окажется, карта
    # перестаёт быть защитой, и нужна отдельная проверка.
    st, tr, tp, feed = mk(people=People(
        {ANDREY: "435", PAVEL: "777", AGENT: "999"}, owner_user_id=ANDREY))
    run(feed.on_event(closed(creator=AGENT)))
    assert tr.assigned == []


def test_an_author_outside_the_map_gets_nothing_back():
    # Задача уехала бы к тому, кто её не увидит.
    st, tr, tp, feed = mk()
    run(feed.on_event(closed(creator="u-чужой")))
    assert tr.assigned == []


def test_the_creator_is_asked_from_the_tracker_when_the_event_hides_it():
    st, tr, tp, feed = mk(creator=PAVEL)
    p = closed()
    p["data"].pop("creatorId")
    run(feed.on_event(p))
    assert tr.assigned == [("i1", PAVEL)]


def test_a_retried_delivery_returns_the_issue_once():
    st, tr, tp, feed = mk()
    run(feed.on_event(closed()))
    run(feed.on_event(closed()))
    assert tr.assigned == [("i1", PAVEL)]


def test_a_failed_return_may_be_retried():
    # Ретрай доставки — единственный шанс попробовать ещё раз, и с висящей
    # отметкой он молча ничего не сделает.
    st, tr, tp, feed = mk()
    tr.fail = "500"
    run(feed.on_event(closed()))
    tr.fail = None
    run(feed.on_event(closed()))
    assert tr.assigned == [("i1", PAVEL)]


# ── мелочи, на которых ломается всё остальное ────────────────────────────

def test_a_closed_state_is_recognised_by_type():
    assert issue_is_closed({"state": {"type": "completed", "name": "Готово"}})
    assert issue_is_closed({"state": {"type": "canceled", "name": "Отменено"}})


def test_a_closed_state_is_recognised_by_name_when_the_type_is_missing():
    # Без запасного признака проверка молча выродилась бы в «ничего не
    # отсеиваем», то есть в тот же баг.
    assert issue_is_closed({"state": {"name": "Done"}})


def test_an_open_state_is_not_closed():
    assert not issue_is_closed({"state": {"type": "started", "name": "In Progress"}})


def test_a_status_change_is_told_apart_from_other_edits():
    assert status_change(status_event()) == "In Review"
    p = status_event()
    p["updatedFrom"] = {"title": "было"}
    assert status_change(p) is None


def test_what_is_remembered_is_the_bare_header():
    # Агент в конце сессии меняет исполнителя И статус одним запросом — такое
    # событие одновременно назначение и смена статуса. Запомни мы
    # отрисованный текст, статус при следующей правке накопился бы второй
    # строкой под первой.
    st, tr, tp, feed = mk()
    p = assignment(who=ANDREY, status="In Review")
    p["updatedFrom"] = {"assigneeId": None, "stateId": "old"}
    run(feed.on_event(p))
    rec = json.loads(next(v for k, v in st.kv.items() if k.startswith("feed:msg:")))
    assert "In Review" not in rec["text"]
    assert "In Review" in tp.sent[0]["text"], "а в самом сообщении статус есть"


def test_a_dm_that_is_refused_falls_back_to_the_common_chat():
    # Человек не начинал разговор с ботом или заблокировал его. Доставка
    # важнее адресности: иначе о задаче не узнает никто.
    st, tr, tp, feed = mk()
    tp.send_fails_for = "777"
    run(feed.on_event(assignment()))
    assert [m["chat"] for m in tp.sent] == ["777", "общий"]


def test_a_refused_dm_without_a_common_chat_is_just_lost_loudly():
    st, tr, tp, feed = mk(notify_chat="")
    tp.send_fails_for = "777"
    out = run(feed.on_event(assignment()))
    assert out["sent"] is False and len(tp.sent) == 1


def test_the_fallback_message_is_the_one_remembered():
    # Запомнить надо тот чат, куда сообщение реально доехало, иначе правка
    # статуса пойдёт в личку, которой нет.
    st, tr, tp, feed = mk()
    tp.send_fails_for = "777"
    run(feed.on_event(assignment()))
    assert any(k.startswith("feed:msg:общий:") for k in st.kv)


# ── уборка закрытой задачи в архив ───────────────────────────────────────

class Archiving(Tracker):
    """Трекер площадки, которая умеет убирать задачи из активных.

    Пишет вызовы одним списком: порядок «сначала вернули автору, потом
    убрали» иначе не доказать, а он здесь и есть суть.
    """

    def __init__(self, creator=""):
        super().__init__(creator)
        self.archived = []
        self.calls = []

    def assign(self, issue_id, user_id):
        self.calls.append(("assign", issue_id))
        return super().assign(issue_id, user_id)

    def archive_issue(self, issue_id):
        self.calls.append(("archive", issue_id))
        self.archived.append(issue_id)
        return True


def with_archive(after_days=0, creator=PAVEL, tracker=None):
    """Лента с настоящим архиватором: проверяем связку, а не заглушку."""
    from agent_factory.archive import Archiver
    st, tp = Store(), Transport()
    tr = tracker if tracker is not None else Archiving(creator)
    arch = Archiver(st, tr, queue_key="agent:queue", agent_user_id=AGENT,
                    after_days=after_days, team_id=TEAM)
    feed = IssueFeed(
        st, tp, tr,
        People({ANDREY: "435", PAVEL: "777", KATYA: "888"},
               owner_user_id=ANDREY, store=st),
        notify_chat="общий", agent_user_id=AGENT, reviewer=Reviewer(),
        team_id=TEAM, archiver=arch)
    return st, tr, tp, feed


def test_closing_an_issue_takes_it_out_of_the_active_ones():
    # Потолок бесплатного трекера считает только неархивные задачи: без
    # уборки он приходит за месяц, и в этот день встаёт приём постановок.
    st, tr, tp, feed = with_archive()
    out = run(feed.on_event(closed()))
    assert tr.archived == ["i1"] and out.get("archived") is True


def test_an_open_issue_is_never_archived():
    # Граница: убираем ТОЛЬКО закрытые. Никаких «старше N дней в любом статусе».
    st, tr, tp, feed = with_archive()
    run(feed.on_event(assignment(who=PAVEL, status="In Progress")))
    assert tr.archived == []


def test_an_edit_of_a_long_closed_issue_does_not_archive_it_again():
    # Правка описания спустя недели — не закрытие: второй заход в трекер на
    # каждое событие по закрытой задаче это трафик без всякого смысла.
    st, tr, tp, feed = with_archive()
    p = closed(stamp="c9")
    p["updatedFrom"] = {"description": "было"}
    run(feed.on_event(p))
    assert tr.archived == []


def test_a_closing_in_two_mutations_still_archives():
    # Агент закрывает задачу и возвращает её постановщику — трекер шлёт это
    # двумя событиями, и во втором `stateId` уже нет.
    st, tr, tp, feed = with_archive()
    p = closed(stamp="c2", who=PAVEL)
    p["updatedFrom"] = {"assigneeId": AGENT}
    run(feed.on_event(p))
    assert tr.archived == ["i1"]


def test_an_issue_still_on_the_agent_is_not_archived():
    # Сессия ещё идёт: закрыла задачу, но исполнителя не сменила.
    st, tr, tp, feed = with_archive()
    run(feed.on_event(closed(who=AGENT)))
    assert tr.archived == []


def test_with_a_delay_the_feed_archives_nothing():
    # Площадка с живыми людьми просит отсрочку — тогда убирает суточный обход.
    st, tr, tp, feed = with_archive(after_days=7)
    out = run(feed.on_event(closed()))
    assert tr.archived == [] and "archived" not in out


def test_a_tracker_without_an_archive_does_not_break_the_feed():
    """У другой площадки архива может не быть вовсе. Событие закрытия должно
    отработать ровно как раньше: задача уезжает автору, сообщение правится."""
    st, tr, tp, feed = with_archive(tracker=Tracker(PAVEL))
    out = run(feed.on_event(closed()))
    assert out.get("returned") == PAVEL and "archived" not in out


def test_the_issue_goes_back_to_its_author_before_it_is_archived():
    """Порядок важен: назначение на автора трогает ту же задачу, и делать
    его после уборки значит писать в архивную."""
    st, tr, tp, feed = with_archive()
    run(feed.on_event(closed()))
    assert tr.calls == [("assign", "i1"), ("archive", "i1")]


def test_a_closed_issue_of_another_team_is_left_where_it_is():
    """Вебхук подписывают на весь воркспейс. Писать про чужую задачу — шум,
    убрать её — чужая пропавшая работа."""
    st, tr, tp, feed = with_archive()
    run(feed.on_event(closed(teamId="t-маркетинг")))
    assert tr.archived == []


# ── карта сообщений задачи: бан телеграма 17.09.2026 ─────────────────────
#
# Бота дважды за сутки забанил телеграм. Карта `feed:issue:<id>`
# дописывалась при каждой отправке и никогда не чистилась от повторов: у
# DEV-108 в ней лежало 4751 запись при двух настоящих сообщениях.
# Перерисовка на каждой смене статуса шла править их все, телеграм отвечал
# 429 и закрывал дверь на семь минут. За сутки 1101 вызов и 1103 отказа —
# не прошло вообще ничего, включая ответы на нажатия кнопок.


def refs(st, issue="i1"):
    return list(st.lists.get(f"feed:issue:{issue}") or [])


def fill(st, tp=None, issue="i1", chat="777", count=50):
    """Подкинуть в карту `count` РАЗНЫХ сообщений с живыми записями."""
    for mid in range(1000, 1000 + count):
        st.lists.setdefault(f"feed:issue:{issue}", []).append(f"{chat}:{mid}")
        st.kv[f"feed:msg:{chat}:{mid}"] = json.dumps({"text": "шапка",
                                                      "buttons": []})


def test_repainting_does_not_grow_the_message_map():
    """Одна пара «чат:сообщение» — одна запись, сколько бы раз её ни правили.

    Дубли плодила сама перерисовка: поправив сообщение, она звала `remember`,
    а тот дописывал ту же пару в конец карты.
    """
    st, tr, tp, feed = mk(handoff=Handoff())
    run(feed.on_event(assignment()))
    before = refs(st)
    assert len(before) == 1, "одно сообщение — одна запись"
    for i in range(50):
        run(feed.repaint("i1", status=f"In Review {i}", buttons=[]))
        run(feed.edit_existing("i1", "шапка", "In Review", [], only_chat="777"))
    assert refs(st) == before, f"карта выросла до {len(refs(st))} записей"


def test_an_already_bloated_map_is_cleaned_up():
    """Карта живёт тридцать дней: чинить только будущее — значит оставить
    живые задачи ломать бота ещё месяц."""
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    st.lists["feed:issue:i1"] = refs(st) * 2000          # как было у DEV-108
    run(feed.repaint("i1", status="In Review", buttons=[]))
    assert len(refs(st)) == 1, "дубли, накопленные прежним кодом, вычищены"
    assert len(tp.edits) == 1, "и правка ушла одна, а не две тысячи"


def test_the_map_has_a_ceiling():
    """Даже если что-то пойдёт не так, перерисовка не делает больше
    нескольких десятков вызовов за раз."""
    from agent_factory.issue_feed import MAX_REFS
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    # Тысяча РАЗНЫХ сообщений: дедуп тут не поможет, потолок — единственное,
    # что стоит между лентой и баном.
    fill(st, count=1000)
    run(feed.repaint("i1", status="In Review"))
    assert len(tp.edits) <= MAX_REFS, f"правок {len(tp.edits)} при потолке {MAX_REFS}"
    assert MAX_REFS <= 50, "потолок — несколько десятков, а не сотни"


class Banned(Transport):
    """Телеграм ответил «слишком часто» и сказал, сколько ждать.

    Считает ПОПЫТКИ, а не удачи: отказ в списке правок не остаётся, и по нему
    «остановилась» и «продолжила долбить» неотличимы — ровно та слепота,
    из-за которой в проде отказов было больше, чем вызовов.
    """

    def __init__(self, after=3):
        super().__init__()
        self.after = after          # с какой правки закрывается дверь
        self.wait = 0.0
        self.tries = 0

    async def edit(self, chat_id, message_id, text, buttons=None, rich=False):
        self.tries += 1
        if len(self.edits) >= self.after:
            self.wait = 420.0       # семь минут, как в проде 17.09.2026
            return False
        return await super().edit(chat_id, message_id, text, buttons, rich)

    def retry_after(self):
        return self.wait


def test_a_429_stops_the_repaint():
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    feed.transport = banned = Banned(after=3)
    fill(st, count=50)
    run(feed.repaint("i1", status="In Review"))
    # Три удачных, четвёртая — закрытая дверь. Дальше тишина, а не ещё сорок
    # шесть попыток, каждая из которых бан продлевает.
    assert banned.tries == 4, f"после отказа постучались ещё {banned.tries - 4} раз"
    assert len(banned.edits) == 3
    assert banned.wait == 420.0


def test_while_the_door_is_closed_the_feed_does_not_knock():
    """Пауза названа в секундах — её уважают, а не проверяют заново."""
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    feed.transport = banned = Banned(after=99)
    run(feed.repaint("i1", status="In Review"))
    assert banned.tries == 1
    banned.wait = 420.0                 # дверь закрыта
    run(feed.repaint("i1", status="Done"))
    run(feed.repaint("i1", status="Done"))
    assert banned.tries == 1, "в закрытую дверь не стучатся"


def test_a_plain_failure_is_not_a_ban():
    """Одно удалённое сообщение — повод пропустить его, а не бросить всё."""
    st, tr, tp, feed = mk()
    run(feed.on_event(assignment()))
    fill(st, count=5)
    tp.edit_ok = False                  # транспорт отказывает, но не банит
    assert not hasattr(tp, "retry_after"), "у этого транспорта паузы нет вовсе"
    assert run(feed.repaint("i1", status="In Review")) == 0
