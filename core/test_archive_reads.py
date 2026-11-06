"""Что фабрика читает из трекера после того, как задачи уезжают в архив.

Архивация снимает потолок бесплатного тарифа, но она же уводит задачу из
списков. Отсюда одно правило на два слоя кода: **выборка по идентификатору
обязана видеть архив, а списки — нет.**

Учёт живёт в комментариях к задаче (DEV-18), и раннер читает их по id — если
бы этот путь терял архивные, уборка стирала бы историю работы. А списки
наоборот: очередь подписи и бэклог показывают то, что ещё в работе, и
архивной задаче там делать нечего. У Linear это к тому же опасно — корзина
там тот же `archivedAt`, и `includeArchived:true` вернул бы в списки
УДАЛЁННЫЕ задачи.

Фейк ниже повторяет поведение живого API, проверенное 12.09.2026 на
воркспейсе client-b; в нём тест и есть капкан на случай, если кто-то решит
дописать `includeArchived` в список или убрать чтение по id.
"""
import json

ARCHIVED = {
    "id": "i-arch", "identifier": "DEV-62",
    "state": {"name": "Done", "type": "completed"},
    "assignee": {"id": "u-andrey"}, "creator": {"id": "u-pavel"},
    "labels": {"nodes": [{"name": "дежурный"}]},
    "team": {"id": "t1", "name": "Development",
             "states": {"nodes": [{"id": "s1", "name": "In Review"}]}},
    "comments": {"nodes": [{"body": "учёт: 3 сессии", "createdAt": "2026-09-12T03:05:50.863Z"}]},
}

LIVE = {
    "id": "i-live", "identifier": "DEV-80",
    "state": {"name": "In Review", "type": "started"},
    "assignee": {"id": "u-agent"}, "creator": {"id": "u-pavel"},
    "labels": {"nodes": []}, "comments": {"nodes": []},
}


class FakeLinear:
    """Linear, каким он оказался на живой проверке 12.09.2026.

    • `issues(...)` без `includeArchived:true` архивные не отдаёт (на доске
      client-b это было 65 задач против 72 с ним);
    • `issue(id:)` отдаёт архивную задачу целиком — статус, лейблы, автора и
      ВСЕ комментарии, — и никакого аргумента для этого не нужно.
    """

    def __init__(self):
        self.asked: list[str] = []

    def __call__(self, query, variables=None):
        self.asked.append(query)
        flat = query.replace(" ", "")
        if "issue(id:" in flat:
            return {"issue": ARCHIVED}
        if "issues(" in flat:
            nodes = [LIVE] + ([ARCHIVED] if "includeArchived:true" in flat else [])
            return {"issues": {"nodes": nodes}}
        return {}


def tracker():
    from agent_factory.results import LinearTracker
    t = LinearTracker("ключ")
    t._gql = FakeLinear()
    return t


def adapter():
    from tracker import LinearAdapter
    a = LinearAdapter("ключ")
    fake = FakeLinear()
    # У раннера свой слой: он отдаёт сырой ответ целиком, вместе с `data`.
    a._graphql = lambda q, v: {"data": fake(q, v)}
    a._fake = fake
    return a


# ── учёт обязан видеть архив ─────────────────────────────────────────────

def test_the_ledger_reads_an_archived_issue_and_its_comments():
    """Карточка учёта живёт в комментарии к задаче и обязана пережить архив.
    Раннер читает её снимком по id — этот путь и проверяем."""
    got = adapter().fetch_issue_snapshot("i-arch")
    assert got["state"]["name"] == "Done"
    assert got["comments"]["nodes"][0]["body"] == "учёт: 3 сессии"


def test_the_snapshot_is_asked_by_id_not_by_a_list():
    # Выборка списком архивную не вернула бы вовсе, и учёт молча опустел бы.
    a = adapter()
    a.fetch_issue_snapshot("i-arch")
    assert all("issues(" not in q.replace(" ", "") for q in a._fake.asked)


def test_the_per_id_reads_still_answer_about_an_archived_issue():
    # Автор, лейблы и исполнитель нужны ленте и вебхуку уже после закрытия.
    t = tracker()
    assert t.creator_of("i-arch") == "u-pavel"
    assert t.issue_label_names("i-arch") == ["дежурный"]
    assert t.issue_assignee_id("i-arch") == "u-andrey"


# ── списки архив видеть не должны ────────────────────────────────────────

def test_the_signing_queue_shows_only_live_issues():
    """Очередь подписи — про то, что ждёт человека. Архивной задаче там
    делать нечего, а у Linear в архив попадает и корзина: `includeArchived`
    вернул бы в очередь удалённые задачи."""
    got = tracker().issues_awaiting("t1", ("In Review",))
    assert [i["identifier"] for i in got] == ["DEV-80"]


def test_neither_the_backlog_nor_the_assignments_show_archived_issues():
    # Иначе агент взял бы в работу задачу, которую уже убрали.
    t = tracker()
    assert [i["identifier"] for i in t.unassigned_issues("t1")] == ["DEV-80"]
    assert [i["identifier"] for i in t.assigned_issues("u-agent", ("backlog",))] == ["DEV-80"]


def test_the_sweep_asks_only_for_issues_that_are_still_active():
    """Обход отложенного архива ищет тех, кого ещё не убрали. Попроси он
    архивные — ходил бы в трекер по второму кругу за уже сделанным."""
    t = tracker()
    t.closed_issues("t1", "2026-09-01T00:00:00Z")
    assert "includeArchived" not in json.dumps(t._gql.asked)
