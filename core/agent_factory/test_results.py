"""Тесты второй половины приёма: черновик → задача → карточка."""
import asyncio
import json

from agent_factory import intake
from agent_factory.intake import Intake
from agent_factory.results import Cards, Results, work_task


class FakeStore:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.claimed: set[str] = set()

    async def append(self, key, value):
        self.lists.setdefault(key, []).append(value); return len(self.lists[key])

    async def read_all(self, key): return list(self.lists.get(key) or [])
    async def drop(self, key):
        self.lists.pop(key, None)
        self.claimed.discard(key)  # DEL снимает и захват
    async def get(self, key): return getattr(self, "kv", {}).get(key)
    async def put(self, key, value, ttl):
        self.kv = getattr(self, "kv", {}); self.kv[key] = value
    async def enqueue(self, queue, element): await self.append(queue, element)
    async def expire(self, key, ttl): pass

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key); return True


class FakeTransport:
    def __init__(self):
        self.sent: list[tuple] = []
        self.edited: list[tuple] = []
        self.edit_ok = True
        self.deleted: list = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons)); return "m1"

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((chat_id, message_id, text, buttons)); return self.edit_ok

    async def answer(self, callback_id, notice=""): pass
    async def delete(self, chat_id, message_id):
        self.deleted.append(message_id); return True


class FakeTracker:
    def __init__(self):
        self.created = []
        self.assigned = []
        self.deleted = []
        self.comments = []
        self.fail = None
        self.comment_fail = None
        self.already = False

    def create_issue(self, team_id, title, description, role=""):
        if self.fail:
            raise RuntimeError(self.fail)
        self.created.append((team_id, title, description, role))
        return {"id": "uuid-1", "identifier": "DEV-77",
                "url": "https://linear.app/x/DEV-77"}

    def assign(self, issue_id, user_id):
        if self.fail:
            raise RuntimeError(self.fail)
        self.assigned.append((issue_id, user_id))
        return {"id": issue_id, "identifier": "DEV-77", "url": "u",
                "already": self.already}

    def comment(self, issue_id, body):
        if self.comment_fail:
            raise RuntimeError(self.comment_fail)
        self.comments.append((issue_id, body))

    def delete(self, issue_id):
        if self.fail:
            raise RuntimeError(self.fail)
        self.deleted.append(issue_id); return "DEV-77"


def mk(**kw):
    st, tr, tp = FakeStore(), FakeTracker(), FakeTransport()
    ink = Intake(source="telegram", store=st, transport=tp, queue_key="q")
    return st, tr, tp, Results(ink, st, tp, tr, kw.get("notify", ""))


OK = {"chat_id": 42, "draft_id": "abc", "ok": True,
      "draft": {"team_id": "T", "title": "Починить экспорт",
                "description": "Падает на пустом периоде", "summary": "Экспорт падает"},
      "items": [{"type": "text", "from": "Иван"}]}


# ── удачный путь ─────────────────────────────────────────────────────────

def test_creates_issue_and_shows_card_with_buttons():
    st, tr, tp, res = mk()
    assert asyncio.run(res.handle(OK)) == "created"
    assert tr.created and tr.created[0][0] == "T"
    chat, text, buttons = tp.sent[0]
    assert chat == 42 and "DEV-77" in text
    assert [b.payload for b in buttons] == ["intake:agent:uuid-1", "intake:delete:uuid-1"]


def test_the_card_is_remembered_so_a_reply_can_reach_the_issue(monkeypatch):
    """Связь пишется на сквозном пути, а не только в своей функции: между
    отправкой карточки и запоминанием стоит идентификатор сообщения, и потерять
    его можно ровно здесь (DEV-74)."""
    st, tr, tp, res = mk()
    asyncio.run(res.handle(OK))
    saved = json.loads(st.kv[intake.answer_key("telegram", 42, "m1")])
    assert saved == {"id": "uuid-1", "identifier": "DEV-77"}


def test_issue_body_carries_origin():
    """Кто прислал фактуру — единственное место в тикете, где это видно."""
    st, tr, tp, res = mk()
    asyncio.run(res.handle(OK))
    body = tr.created[0][2]
    assert "Иван" in body and "Падает на пустом периоде" in body


def test_intake_writes_its_own_reading_of_the_task():
    """Разбор приёма — отдельный комментарий, а не часть описания.

    Решения приёма (роль, приоритет, репозиторий) уезжают в задачу молча, и
    ошибку назначения по ней не поймать. Комментарий появляется ДО карточки:
    человек открывает задачу по ссылке из неё.
    """
    st, tr, tp, res = mk()
    asyncio.run(res.handle(OK))
    assert [i for i, _ in tr.comments] == ["uuid-1"]
    body = tr.comments[0][1]
    assert "обычная работа" in body and "Репозиторий" in body


def test_incident_reading_names_the_priority_it_got():
    st, tr, tp, res = mk()
    asyncio.run(res.handle({**OK, "draft": {**OK["draft"], "role": "incident",
                                            "repo": "events-api"}}))
    body = tr.comments[0][1]
    assert "инцидент" in body and "Urgent" in body and "events-api" in body


def test_a_lost_reading_does_not_cost_the_card():
    """Задача уже заведена: без карточки человек не узнает ни номера, ни
    ссылки, а без разбора — только не увидит, как приём её понял."""
    st, tr, tp, res = mk()
    tr.comment_fail = "трекер отказал"
    assert asyncio.run(res.handle(OK)) == "created"
    assert tp.sent and "DEV-77" in tp.sent[0][1]


def test_an_adapter_without_comment_is_not_read_as_a_refusal(caplog):
    """`comment` приехал в протокол `Tracker` позже прочих методов, и у
    стороннего адаптера его может не быть вовсе. В общем `except` это
    читалось как «трекер отказал», и чинить пошли бы не туда."""
    st, tr, tp, res = mk()

    class OldAdapter:
        """Адаптер первого клиента: всё, кроме `comment`."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "comment":
                raise AttributeError(name)
            return getattr(self._inner, name)

    res.tracker = OldAdapter(tr)
    with caplog.at_level("WARNING"):
        assert asyncio.run(res.handle(OK)) == "created"
    assert "не умеет comment" in caplog.text
    assert tr.comments == []
    # Карточка — не заложник разбора: задача заведена, ссылка человеку нужна.
    assert tp.sent and "DEV-77" in tp.sent[0][1]


def test_card_keeps_link_even_when_trimmed():
    """Без сути человек откроет задачу; без ссылки он остаётся с номером."""
    draft = {**OK["draft"], "summary": "щ" * (intake.CARD_LIMIT + 500)}
    text = intake.card_text({"identifier": "DEV-77", "url": "https://l/DEV-77"}, draft)
    assert text.endswith("https://l/DEV-77")
    assert "…" in text


def test_notify_chat_gets_a_line_when_configured():
    st, tr, tp, res = mk(notify="-100")
    asyncio.run(res.handle(OK))
    assert any(str(c) == "-100" for c, _, _ in tp.sent)


def test_no_notify_chat_means_no_second_message():
    st, tr, tp, res = mk()
    asyncio.run(res.handle(OK))
    assert len(tp.sent) == 1


# ── неудачные исходы ─────────────────────────────────────────────────────

def test_failed_session_is_reported_not_swallowed():
    """Человек ждёт черновик. Молчание он читает как «бот умер»."""
    st, tr, tp, res = mk()
    r = asyncio.run(res.handle({"chat_id": 42, "draft_id": "a", "ok": False,
                                "error": "сессия упала"}))
    assert r == "failed" and "сессия упала" in tp.sent[0][1]


def test_limit_is_told_apart_from_a_plain_failure():
    """Исчерпанный лимит — не поломка: раннер вернётся к пачке сам, и человеку
    надо сказать именно это, иначе он будет пересылать заново."""
    st, tr, tp, res = mk()
    asyncio.run(res.handle({"chat_id": 42, "draft_id": "a", "ok": False,
                            "error": "лимит", "limit": True,
                            "limit_reset": "04:20 UTC"}))
    assert "04:20 UTC" in tp.sent[0][1]


def test_reject_creates_nothing_and_returns_the_batch():
    """Отказ «из этого задачи не выходит» обязан вернуть фактуру: иначе
    пересланная переписка теряется и человек шлёт её второй раз."""
    st, tr, tp, res = mk()
    r = asyncio.run(res.handle({**OK, "draft": {"reject": "непонятно, что чинить"}}))
    assert r == "rejected"
    assert tr.created == []
    assert len(st.lists[intake.batch_key("telegram", 42)]) == 1
    assert tp.sent[-1][2], "под вернувшейся пачкой должна быть кнопка"


def test_reject_batch_survives_a_second_collect():
    """Вернувшийся кусок должен быть валидным JSON — его прочитает `collect`."""
    st, tr, tp, res = mk()
    asyncio.run(res.handle({**OK, "draft": {"reject": "мало данных"}}))
    raw = st.lists[intake.batch_key("telegram", 42)][0]
    assert json.loads(raw)["from"] == "Иван"


def test_local_path_is_not_returned_to_the_batch():
    """`path` — это файл в рабочем каталоге сессии, которого уже нет."""
    st, tr, tp, res = mk()
    asyncio.run(res.handle({**OK, "draft": {"reject": "x"},
                            "items": [{"type": "photo", "path": "/tmp/gone.jpg"}]}))
    assert "path" not in json.loads(st.lists[intake.batch_key("telegram", 42)][0])


def test_missing_team_does_not_create_anything():
    st, tr, tp, res = mk()
    r = asyncio.run(res.handle({**OK, "draft": {**OK["draft"], "team_id": ""}}))
    assert r == "no-team" and tr.created == []


def test_tracker_refusal_is_shown_to_the_person():
    st, tr, tp, res = mk()
    tr.fail = "403"
    r = asyncio.run(res.handle(OK))
    assert r == "create-failed" and "403" in tp.sent[0][1]


def test_result_without_address_is_dropped():
    st, tr, tp, res = mk()
    assert asyncio.run(res.handle({"ok": True})) == "no-address"
    assert tp.sent == []


# ── кнопки под карточкой ─────────────────────────────────────────────────

def cards(agent="agent-uuid"):
    st, tr, tp = FakeStore(), FakeTracker(), FakeTransport()
    return st, tr, tp, Cards(st, tp, tr, "agent:queue:x", agent)


def test_agent_button_assigns_and_queues_the_work():
    """У инсталляции может не быть вебхука от трекера — тогда назначение
    никем не подхватится, и кнопка окажется пустой."""
    st, tr, tp, c = cards()
    r = asyncio.run(c.handle("agent", "uuid-1", 42, "m1", "Андрей"))
    assert r == "assigned"
    assert tr.assigned == [("uuid-1", "agent-uuid")]
    queued = json.loads(st.lists["agent:queue:x"][0])
    assert queued["issue_id"] == "uuid-1" and queued["identifier"] == "DEV-77"


def test_already_assigned_does_not_queue_a_second_session():
    """Повторное назначение тем же значением дало бы вторую сессию на ту же
    задачу — два worktree одной ветки."""
    st, tr, tp, c = cards()
    tr.already = True
    asyncio.run(c.handle("agent", "uuid-1", 42, "m1", "Андрей"))
    assert st.lists.get("agent:queue:x") is None


def test_second_tap_is_serialized_away():
    st, tr, tp, c = cards()
    asyncio.run(c.handle("agent", "uuid-1", 42, "m1", "Андрей"))
    assert asyncio.run(c.handle("agent", "uuid-1", 42, "m1", "Андрей")) == "already-running"
    assert len(tr.assigned) == 1


def test_agent_button_removes_both_buttons():
    """«Удалить» под уже идущей работой удалила бы её вместе с задачей."""
    st, tr, tp, c = cards()
    asyncio.run(c.handle("agent", "uuid-1", 42, "m1", "Андрей"))
    assert tp.edited[0][3] is None


def test_falls_back_to_a_new_message_when_edit_fails():
    """Исход обязан доехать: правка могла не пройти на старом сообщении."""
    st, tr, tp, c = cards()
    tp.edit_ok = False
    asyncio.run(c.handle("agent", "uuid-1", 42, "m1", "Андрей"))
    assert tp.sent and "DEV-77" in tp.sent[0][1]


def test_delete_says_which_issue_went():
    """Карточек в личке несколько подряд — «удалено» без номера бесполезно."""
    st, tr, tp, c = cards()
    r = asyncio.run(c.handle("delete", "uuid-1", 42, "m1", "Андрей"))
    assert r == "deleted" and "DEV-77" in tp.edited[0][2]


def test_failed_delete_says_the_issue_is_still_there():
    st, tr, tp, c = cards()
    tr.fail = "нет прав"
    r = asyncio.run(c.handle("delete", "uuid-1", 42, "m1", "Андрей"))
    assert r == "delete-failed" and "на месте" in tp.sent[0][1]


def test_no_agent_configured_is_said_out_loud():
    """Клик, после которого ничего не происходит, читается как «бот сломался»."""
    st, tr, tp, c = cards(agent="")
    r = asyncio.run(c.handle("agent", "uuid-1", 42, "m1", "Андрей"))
    assert r == "agent-not-configured" and tr.assigned == []
    assert tp.sent, "молчать здесь нельзя"


def test_work_task_carries_what_the_runner_needs():
    t = work_task({"id": "u", "identifier": "DEV-77", "url": "l"}, "Андрей")
    assert t["issue_id"] == "u" and t["identifier"] == "DEV-77" and t["queued_at"]


# ── лейбл дежурного и хук инсталляции ────────────────────────────────────

class FakeGql:
    """Linear, отвечающий заранее заданным. Проверяем, ЧТО именно уходит."""

    def __init__(self, labels=None, create_ok=True):
        self.calls: list[tuple] = []
        self.labels = labels if labels is not None else []
        self.create_ok = create_ok

    def __call__(self, query, variables, key=""):
        self.calls.append((query, variables))
        if "issueLabels(" in query:
            return {"issueLabels": {"nodes": self.labels}}
        if "issueLabelCreate" in query:
            if not self.create_ok:
                return {"issueLabelCreate": {"success": False}}
            return {"issueLabelCreate": {"success": True,
                                         "issueLabel": {"id": "новый-лейбл"}}}
        if "states(" in query:
            return {"team": {"states": {"nodes": [{"id": "st", "type": "backlog"}]}}}
        return {"issueCreate": {"success": True,
                                "issue": {"id": "u", "identifier": "X-1", "url": "l"}}}


def tracker_with(labels=None, label_name="дежурный"):
    from agent_factory.results import LinearTracker
    t = LinearTracker("ключ", incident_label=label_name)
    gql = FakeGql(labels)
    t._gql = gql
    return t, gql


def created_input(gql):
    return next(v["input"] for q, v in gql.calls if "issueCreate" in q)


def test_incident_gets_the_label_of_its_own_team():
    """Есть командный и общий — вешаем командный: это тот же лейбл, который
    человек видит в фильтрах своей доски."""
    t, gql = tracker_with([{"id": "общий", "name": "Дежурный", "team": None},
                           {"id": "мой", "name": "дежурный", "team": {"id": "T"}}])
    t.create_issue("T", "падает прод", "тело", role="incident")
    assert created_input(gql)["labelIds"] == ["мой"]


def test_a_workspace_label_counts_too():
    """Лейбл уровня воркспейса у команды есть, но в её списке не виден."""
    t, gql = tracker_with([{"id": "общий", "name": "дежурный", "team": None}])
    t.create_issue("T", "падает", "тело", role="incident")
    assert created_input(gql)["labelIds"] == ["общий"]


def test_a_label_of_a_foreign_team_is_not_reused():
    """Создание задачи с чужим лейблом откажет, и задачи не будет вовсе."""
    t, gql = tracker_with([{"id": "чужой", "name": "дежурный", "team": {"id": "ДРУГАЯ"}}])
    t.create_issue("T", "падает", "тело", role="incident")
    assert created_input(gql)["labelIds"] == ["новый-лейбл"]


def test_a_task_without_a_label_beats_no_task_at_all():
    """Потеря маршрута — плохо, несозданная задача — хуже."""
    t, gql = tracker_with([], label_name="дежурный")
    gql.create_ok = False
    issue = t.create_issue("T", "падает", "тело", role="incident")
    assert issue["identifier"] == "X-1"
    assert "labelIds" not in created_input(gql)


def test_no_label_configured_means_no_labels_at_all():
    """Развилки «дежурный или работа» у инсталляции может не быть — выдумывать
    её не надо."""
    t, gql = tracker_with([], label_name="")
    t.create_issue("T", "падает", "тело", role="incident")
    assert "labelIds" not in created_input(gql)
    assert not any("issueLabels(" in q for q, _ in gql.calls), "лишний запрос"


def test_ordinary_work_never_gets_the_incident_label():
    t, gql = tracker_with([{"id": "мой", "name": "дежурный", "team": {"id": "T"}}])
    t.create_issue("T", "обычная работа", "тело", role="work")
    assert "labelIds" not in created_input(gql)


def test_installation_hook_sees_the_created_issue():
    seen = {}

    async def on_created(issue, chat_id, draft, items):
        seen.update(issue=issue["identifier"], chat=chat_id)

    st, tr, tp = FakeStore(), FakeTracker(), FakeTransport()
    ink = Intake(source="telegram", store=st, transport=tp, queue_key="q")
    res = Results(ink, st, tp, tr, on_created=on_created)
    asyncio.run(res.handle(OK))
    assert seen == {"issue": "DEV-77", "chat": 42}


def test_a_broken_hook_does_not_cost_the_card():
    """Задача уже создана, и человек обязан получить на неё ссылку."""

    async def boom(*a):
        raise RuntimeError("некуда писать")

    st, tr, tp = FakeStore(), FakeTracker(), FakeTransport()
    ink = Intake(source="telegram", store=st, transport=tp, queue_key="q")
    res = Results(ink, st, tp, tr, on_created=boom)
    assert asyncio.run(res.handle(OK)) == "created"
    assert any("DEV-77" in t for _, t, _ in tp.sent)


# ── автор задачи ─────────────────────────────────────────────────────────

class GqlSpy:
    """Трекер с подменённым транспортом: проверяем запрос и разбор ответа."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def __call__(self, query, variables, key=""):
        self.asked.append((query, variables))
        return self.answer


def tracker_answering(answer):
    from agent_factory.results import LinearTracker
    t = LinearTracker("ключ")
    t._gql = GqlSpy(answer)
    return t


def test_the_creator_is_read_from_the_tracker():
    # Лента возвращает закрытую работу тому, кто её принёс. В событии закрытия
    # автора может не быть — тогда его спрашивают у трекера.
    t = tracker_answering({"issue": {"creator": {"id": "u-pavel"}}})
    assert t.creator_of("i1") == "u-pavel"
    query, variables = t._gql.asked[0]
    assert "creator" in query and variables == {"id": "i1"}


def test_an_issue_without_a_creator_gives_an_empty_answer():
    # Пусто — это «не знаю», и лента просто не вернёт задачу. Падать нельзя:
    # возврат автору — удобство, а не обязательство.
    assert tracker_answering({"issue": {}}).creator_of("i1") == ""
    assert tracker_answering({}).creator_of("i1") == ""


# ── разбор результатов на несколько мессенджеров ─────────────────────────
#
# У первого клиента два мессенджера, а список результатов у раннера один.
# Ответ обязан уйти в тот, из которого человек прислал фактуру.

from agent_factory.results import dispatch, drain


class DrainStore(FakeStore):
    """Отдаёт заготовленные результаты, а когда они кончились — снимает
    цикл, как это сделал бы останов процесса."""

    def __init__(self, raws):
        super().__init__()
        self.raws = list(raws)

    async def pop_blocking(self, key, timeout):
        assert key == intake.RESULTS_KEY
        if not self.raws:
            raise asyncio.CancelledError
        return self.raws.pop(0)


def two_messengers():
    st = DrainStore([])
    chans = {}
    for source in ("telegram", "max"):
        tp = FakeTransport()
        ink = Intake(source=source, store=st, transport=tp, queue_key="q")
        chans[source] = (tp, Results(ink, st, tp, FakeTracker()))
    return st, chans


def run_drain(st, chans):
    try:
        asyncio.run(drain(st, {s: r for s, (_, r) in chans.items()}))
    except asyncio.CancelledError:
        pass


def test_a_result_answers_in_the_messenger_the_batch_came_from():
    st, chans = two_messengers()
    from_max = {**OK, "items": [{"type": "text", "from": "Иван", "source": "max"}]}
    st.raws = [json.dumps(from_max)]
    run_drain(st, chans)
    assert chans["max"][0].sent and not chans["telegram"][0].sent


def test_an_unknown_source_is_not_answered_into_a_foreign_messenger(caplog):
    # id чатов в Telegram и MAX — числа из разных пространств: отдать
    # результат «хоть кому-нибудь» значит показать чужую переписку не тому.
    st, chans = two_messengers()
    stray = {**OK, "items": [{"type": "text", "source": "viber"}]}
    assert asyncio.run(dispatch(stray, {s: r for s, (_, r) in chans.items()})) == "no-channel"
    assert not chans["max"][0].sent and not chans["telegram"][0].sent
    assert "viber" in caplog.text


def test_a_single_messenger_takes_results_without_a_source():
    # Куски, положенные до появления `source`, отвечают в единственный
    # мессенджер — как и раньше, когда он был один.
    st = DrainStore([])
    tp = FakeTransport()
    ink = Intake(source="max", store=st, transport=tp, queue_key="q")
    res = Results(ink, st, tp, FakeTracker())
    assert asyncio.run(dispatch(OK, {"max": res})) == "created"
    assert tp.sent


def test_run_on_one_channel_is_the_same_drain():
    st = DrainStore([json.dumps(OK)])
    tp = FakeTransport()
    ink = Intake(source="telegram", store=st, transport=tp, queue_key="q")
    res = Results(ink, st, tp, FakeTracker())
    try:
        asyncio.run(res.run())
    except asyncio.CancelledError:
        pass
    assert tp.sent


def test_a_broken_result_does_not_stop_the_drain(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    st, chans = two_messengers()
    st.raws = ["не json", json.dumps(OK)]
    run_drain(st, chans)
    assert chans["telegram"][0].sent


def test_a_drain_without_messengers_is_refused():
    import pytest
    with pytest.raises(ValueError):
        asyncio.run(drain(DrainStore([]), {}))


async def _no_sleep(_):
    pass


def test_the_agent_button_leaves_a_stash_for_the_deploy_check():
    # Тот же след, что оставляет вебхук: без него выкат работы, отданной
    # кнопкой, проверен не будет.
    from agent_factory.deploy import stashed_task
    st, tr, tp, c = cards()
    asyncio.run(c.handle("agent", "uuid-1", 42, "m1", "Иван"))
    assert asyncio.run(stashed_task(st, "DEV-77"))["issue_id"] == "uuid-1"


# ── проект заведённой задачи ─────────────────────────────────────────────

class ProjectSpy:
    """Трекер, запоминающий, с каким проектом его позвали."""

    def __init__(self):
        self.calls = []

    def create_issue(self, team_id, title, description, role="", project_id=""):
        self.calls.append({"team": team_id, "project": project_id})
        return {"id": "i1", "identifier": "DEV-1", "url": "https://l/DEV-1"}

    def assign(self, issue_id, user_id): return {}
    def delete(self, issue_id): return "ok"


def test_the_installation_default_project_is_used():
    """Без проекта задачу не отфильтровать: в доске она есть, в списке проекта
    нет. 12.09.2026 так накопилось десять бесхозных задач, и шесть из них
    завёл сам приём."""
    from agent_factory.results import LinearTracker
    t = LinearTracker("ключ")
    sent = {}
    t._gql = lambda q, v, key="": (sent.update(v) or
                           {"issueCreate": {"success": True,
                                            "issue": {"id": "i", "identifier": "DEV-1",
                                                      "url": "u"}}}
                           if "issueCreate" in q else {})
    t.create_issue("t1", "заголовок", "описание", project_id="p1")
    assert sent["input"]["projectId"] == "p1"


def test_without_a_project_nothing_is_sent():
    # У площадки без проектов это единственный возможный исход — поле не
    # должно появляться пустым, иначе трекер откажет.
    from agent_factory.results import LinearTracker
    t = LinearTracker("ключ")
    sent = {}
    t._gql = lambda q, v, key="": (sent.update(v) or
                           {"issueCreate": {"success": True,
                                            "issue": {"id": "i", "identifier": "DEV-1",
                                                      "url": "u"}}}
                           if "issueCreate" in q else {})
    t.create_issue("t1", "заголовок", "описание")
    assert "projectId" not in sent["input"]


def test_a_tracker_that_knows_nothing_about_projects_still_works():
    """У первого клиента свой адаптер трекера, и про проекты он не знает.
    Лишний аргумент сломал бы ему создание задачи вообще — поэтому проект
    передаётся только когда он задан."""
    import asyncio
    spy = ProjectSpy()

    class OldTracker:
        def __init__(self): self.calls = []
        def create_issue(self, team_id, title, description, role=""):
            self.calls.append(team_id)
            return {"id": "i1", "identifier": "DEV-1", "url": "u"}

    from agent_factory.results import Results
    old = OldTracker()
    r = Results(None, None, None, old)          # проект не задан
    got = r._create("t1", {"title": "з"}, [])
    assert got["identifier"] == "DEV-1" and old.calls == ["t1"]

    r2 = Results(None, None, None, spy, project_id="p9")
    r2._create("t1", {"title": "з"}, [])
    assert spy.calls[-1]["project"] == "p9"


def test_the_draft_does_not_get_to_name_the_project():
    """Поля `project_id` в контракте черновика нет, писать его некому — а
    заведись оно, значение придёт от модели и может быть выдумано. Трекер на
    неизвестный проект отказывает в создании ЦЕЛИКОМ: вместо задачи не в том
    проекте человек не получил бы задачи вовсе."""
    spy = ProjectSpy()
    from agent_factory.results import Results
    r = Results(None, None, None, spy, project_id="из-настройки")
    r._create("t1", {"title": "з", "project_id": "из-черновика"}, [])
    assert spy.calls[-1]["project"] == "из-настройки"


# ── инцидент заводится срочным ───────────────────────────────────────────

class PrioritySpy:
    """Трекер, который запоминает вход задачи целиком."""

    def __init__(self):
        self.inputs = []

    def _gql(self, q, v=None, key=""):
        if "issueCreate" in q:
            self.inputs.append(v["input"])
            return {"issueCreate": {"success": True,
                                    "issue": {"id": "i1", "identifier": "TASK-1",
                                              "url": "u"}}}
        return {}


def _tracker_that_records_input():
    from agent_factory.results import LinearTracker
    t = LinearTracker.__new__(LinearTracker)
    t._incident_label = ""
    t._author_key = ""
    spy = PrioritySpy()
    t._gql = spy._gql
    return t, spy


def test_an_incident_is_created_urgent():
    """Инцидент, лежащий в общей куче с обычным приоритетом, ничем от неё не
    отличается: 12.09.2026 TASK-302 так и пролежала 50 минут. 1 — Urgent."""
    t, spy = _tracker_that_records_input()
    t.create_issue("t1", "заголовок", "описание", role="incident")
    assert spy.inputs[-1]["priority"] == 1


def test_ordinary_work_keeps_the_default_priority():
    # Срочность — признак инцидента, а не способ выделиться в списке.
    t, spy = _tracker_that_records_input()
    t.create_issue("t1", "заголовок", "описание", role="work")
    assert "priority" not in spy.inputs[-1]


def test_the_real_tracker_implements_the_whole_protocol():
    """Сторож на класс ошибок: метод объявлен в протоколе, а реализации у
    боевого трекера нет — фейки в тестах это скрывают полностью. 12.09.2026
    так и вышло: тело `issue_assignee_id` уехало В протокол, а `LinearTracker`
    остался без него, и напоминание про инцидент падало бы на живом."""
    from agent_factory.results import LinearTracker, Tracker

    declared = [n for n in Tracker.__dict__ if not n.startswith("_")]
    assert "issue_assignee_id" in declared and "create_issue" in declared
    missing = [n for n in declared if not callable(getattr(LinearTracker, n, None))]
    assert not missing, f"в протоколе есть, у трекера нет: {missing}"


def test_the_protocol_declares_nothing_it_can_run():
    """Протокол — объявление, а не база с логикой. Тело в нём означает, что
    инсталляция со своим трекером молча унаследует чужой запрос — и узнает об
    этом на живом."""
    import ast
    import inspect

    from agent_factory.results import Tracker

    tree = ast.parse(inspect.getsource(Tracker)).body[0]
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        steps = [n for n in node.body
                 if not (isinstance(n, ast.Expr)
                         and isinstance(n.value, ast.Constant))]
        assert all(isinstance(n, ast.Pass) or (
            isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            for n in steps), f"{node.name} в протоколе с телом"


# ── архив ────────────────────────────────────────────────────────────────

def test_an_issue_is_archived_by_the_tracker_mutation():
    # `issueArchive` — не удаление: задача остаётся читаемой целиком, вместе
    # с комментариями, в которых живёт учёт (проверено на DEV-62 12.09.2026).
    t = tracker_answering({"issueArchive": {"success": True}})
    assert t.archive_issue("i1") is True
    query, variables = t._gql.asked[0]
    assert "issueArchive" in query and variables == {"id": "i1"}


def test_a_refused_archive_is_not_reported_as_done():
    """Молчаливое «убрал» на отказе трекера — это потолок тарифа, который
    считает задачи, и журнал, который считает их убранными."""
    import pytest
    t = tracker_answering({"issueArchive": {"success": False}})
    with pytest.raises(RuntimeError):
        t.archive_issue("i1")


def test_closed_issues_carry_both_closing_times():
    """Обход отложенного архива меряет возраст закрытия. У `completed` стоит
    completedAt, у `canceled` — canceledAt: возьми запрос только первое, и
    отменённые не уехали бы в архив никогда."""
    t = tracker_answering({"issues": {"nodes": [{"id": "i1"}]}})
    assert t.closed_issues("t1", "2026-09-01T00:00:00Z") == [{"id": "i1"}]
    query, variables = t._gql.asked[0]
    assert "completedAt" in query and "canceledAt" in query
    assert "completed" in query and "canceled" in query
    assert variables == {"t": "t1", "d": "2026-09-01T00:00:00Z"}


def test_the_age_is_picked_by_the_tracker_not_by_us():
    """`orderBy:updatedAt` отдаёт СВЕЖИЕ первыми: сотня свежезакрытых
    вытеснила бы из страницы всё, что пора убирать, и обход крутился бы
    вхолостую каждые сутки, ничего об этом не говоря."""
    t = tracker_answering({"issues": {"nodes": []}})
    t.closed_issues("t1", "2026-09-01T00:00:00Z")
    query, _ = t._gql.asked[0]
    assert "completedAt:{lt:$d}" in query and "canceledAt:{lt:$d}" in query
