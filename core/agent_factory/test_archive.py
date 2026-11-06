"""Уборка закрытых задач в архив.

Половина тестов здесь про то, чего делать НЕЛЬЗЯ: убрать задачу, по которой
идёт сессия; убрать по закрытию, когда площадка попросила отсрочку; уронить
ленту там, где архива нет вовсе. Потолок бесплатного тарифа стоит одной
настройки, а задача, пропавшая из-под работающей сессии, — самой работы.
"""
import asyncio
import time

from agent_factory.archive import DAY_SEC, Archiver, closed_at, days_from

AGENT = "u-agent"
HUMAN = "u-andrey"
QUEUE = "agent:queue"
TEAM = "t1"


def issue(id="i1", identifier="DEV-80", who=HUMAN, team=TEAM, **kw):
    """Задача так, как её приносит событие ленты: исполнитель плоским полем."""
    return {"id": id, "identifier": identifier, "assigneeId": who,
            "teamId": team, **kw}


class Store:
    """Как redis: `claim` это SET NX, то есть ключ после него ЧИТАЕТСЯ.

    Разница важна ровно здесь: захват сессии архив читает, а не захватывает,
    и фейк, у которого `claim` не виден в `get`, доказывал бы обратное.
    """

    def __init__(self):
        self.kv = {}
        self.lists = {}
        self.boom = ""

    async def get(self, key):
        if self.boom == "get":
            raise RuntimeError("redis молчит")
        return self.kv.get(key)

    async def read_all(self, key):
        if self.boom == "read_all":
            raise RuntimeError("redis молчит")
        return list(self.lists.get(key) or [])

    async def claim(self, key, ttl):
        if key in self.kv:
            return False
        self.kv[key] = "1"
        return True

    async def enqueue(self, queue, element):
        self.lists.setdefault(queue, []).append(element)


class Tracker:
    """Трекер, умеющий архив."""

    def __init__(self, closed=None, boom=False, ok=True):
        self.archived = []
        self.closed = closed or []
        self.boom = boom
        self.ok = ok
        self.asked = []

    def archive_issue(self, issue_id):
        if self.boom:
            raise RuntimeError("трекер молчит")
        self.archived.append(issue_id)
        return self.ok

    def closed_issues(self, team_id, before):
        self.asked.append((team_id, before))
        return list(self.closed)


class Plain:
    """Площадка, чей трекер про архив не знает: ни одного лишнего метода."""

    def assign(self, issue_id, user_id):
        return {}


def mk(tracker=None, **kw):
    st = Store()
    tr = Tracker() if tracker is None else tracker
    kw.setdefault("team_id", TEAM)
    return st, tr, Archiver(st, tr, queue_key=QUEUE, agent_user_id=AGENT, **kw)


def run(c): return asyncio.run(c)


# ── закрытие убирает задачу ──────────────────────────────────────────────

def test_a_closed_issue_is_archived():
    # Потолок бесплатного Linear — 250 НЕархивных задач. При семи задачах в
    # сутки без уборки он приходит за месяц, и в этот день встаёт приём.
    st, tr, arch = mk()
    assert run(arch.on_closed(issue())) is True
    assert tr.archived == ["i1"]


def test_an_issue_without_an_id_is_not_archived():
    # Событие без id задачи — не повод звать трекер с пустой строкой.
    st, tr, arch = mk()
    assert run(arch.on_closed(issue(id=""))) is False
    assert tr.archived == []


def test_a_tracker_without_an_archive_is_not_an_error():
    """У другой площадки архива может не быть вовсе. Отсутствие метода — это
    «не умеет», а не сбой: лента зовёт уборку на каждом закрытии, и падение
    здесь стоило бы ей всей обработки события."""
    st, tr, arch = mk(tracker=Plain())
    assert run(arch.on_closed(issue())) is False
    assert arch.able is False


def test_a_silent_tracker_does_not_break_the_feed():
    # Архив — уборка, а не обязательство: не вышло сейчас, выйдет потом.
    st, tr, arch = mk(tracker=Tracker(boom=True))
    assert run(arch.on_closed(issue())) is False


def test_a_refusal_from_the_tracker_is_reported_as_such():
    st, tr, arch = mk(tracker=Tracker(ok=False))
    assert run(arch.on_closed(issue())) is False


# ── отсрочка ─────────────────────────────────────────────────────────────

def test_with_a_delay_the_closing_itself_archives_nothing():
    """Там, где доской пользуются пятеро живых людей, задачи, исчезающие из
    видов сразу после «Готово», меняют привычную картину. Площадка ставит
    отсрочку — и тогда убирает обход, а не закрытие."""
    st, tr, arch = mk(after_days=7)
    assert run(arch.on_closed(issue())) is False
    assert tr.archived == []


def test_the_knob_reads_as_days():
    assert days_from("3") == 3
    assert days_from(" 7 ") == 7


def test_an_unset_knob_means_right_away():
    # Умолчание — ноль: у площадки с одним человеком и агентом мгновенная
    # уборка никому не мешает, а потолок тарифа перестаёт быть сроком.
    assert days_from(None) == 0
    assert days_from("") == 0


def test_a_broken_knob_means_right_away_too():
    # Не число и отрицательное — это ноль, но с записью в журнал: тихо
    # подставленное умолчание выглядит как работающая настройка.
    assert days_from("неделя") == 0
    assert days_from("-5") == 0


# ── живая сессия ─────────────────────────────────────────────────────────

def test_an_issue_still_held_by_the_agent_is_left_alone():
    """Закрывает задачу сам агент, и «закрыл и вернул» приезжает двумя
    событиями: между ними сессия ещё работает. Убрать задачу в этот момент
    значит дать сессии дописывать в то, чего в видах уже нет."""
    st, tr, arch = mk()
    assert run(arch.on_closed(issue(who=AGENT))) is False
    assert tr.archived == []


def test_an_issue_with_a_fresh_capture_is_left_alone():
    # Захват вебхука: сессию только что поставили.
    st, tr, arch = mk()
    run(st.claim("agent:started:i1", 180))
    assert run(arch.on_closed(issue())) is False
    assert tr.archived == []


def test_reading_the_capture_does_not_take_it():
    """Захват ЧИТАЕМ. `claim` на свободном ключе занял бы его на своё окно, и
    настоящее назначение в эти минуты отбросилось бы как дубль — то есть
    работа по задаче не началась бы вовсе."""
    st, tr, arch = mk()
    run(arch.on_closed(issue()))
    assert run(st.claim("agent:started:i1", 180)) is True


def test_an_issue_waiting_in_the_queue_is_left_alone():
    # Перед задачей может стоять часовая сессия — всё это время задача честно
    # ждёт слота, и сессия по ней ещё будет.
    st, tr, arch = mk()
    run(st.enqueue(QUEUE, '{"issue_id": "i1", "identifier": "DEV-80"}'))
    assert run(arch.on_closed(issue())) is False
    assert tr.archived == []


def test_someone_elses_queue_item_does_not_shield_the_issue():
    st, tr, arch = mk()
    run(st.enqueue(QUEUE, '{"issue_id": "i2"}'))
    assert run(arch.on_closed(issue())) is True


def test_garbage_in_the_queue_does_not_stop_the_check():
    # Один нечитаемый элемент не повод считать очередь пустой или полной.
    st, tr, arch = mk()
    run(st.enqueue(QUEUE, "не json"))
    run(st.enqueue(QUEUE, '{"issue_id": "i1"}'))
    assert run(arch.on_closed(issue())) is False


def test_a_silent_store_means_hands_off():
    """Не спросили хранилище — считаем, что сессия идёт. Пропущенная уборка
    стоит одной задачи в списке; уборка под работающей сессией — работы."""
    for boom in ("get", "read_all"):
        st, tr, arch = mk()
        st.boom = boom
        assert run(arch.on_closed(issue())) is False
        assert tr.archived == []


def test_without_a_queue_key_the_rest_of_the_check_still_works():
    # Площадка может не сказать про очередь — тогда проверка беднее, но
    # оставшиеся два признака работают.
    st = Store()
    tr = Tracker()
    arch = Archiver(st, tr, agent_user_id=AGENT, team_id=TEAM)
    run(st.claim("agent:started:i1", 180))
    assert run(arch.on_closed(issue())) is False
    assert run(arch.on_closed(issue(id="i2", identifier="DEV-81"))) is True


# ── время закрытия ───────────────────────────────────────────────────────

def test_completed_and_canceled_are_dated_by_different_fields():
    # У `completed` заполнен completedAt, у `canceled` — canceledAt, и одно
    # другое не подменяет: выбрав только первое, обход не убрал бы отменённые
    # никогда.
    done = closed_at({"completedAt": "2026-09-12T21:44:21.166Z"})
    gone = closed_at({"canceledAt": "2026-09-11T10:00:00.000Z"})
    assert done > gone > 0


def test_the_update_time_is_the_last_resort():
    assert closed_at({"updatedAt": "2026-09-12T21:44:21.166Z"}) > 0


def test_an_unreadable_date_is_not_a_date():
    assert closed_at({"completedAt": "вчера"}) == 0.0
    assert closed_at({}) == 0.0


# ── обход ────────────────────────────────────────────────────────────────

def iso(ago_days):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() - ago_days * DAY_SEC,
                                  timezone.utc).isoformat().replace("+00:00", "Z")


def node(id="i1", identifier="DEV-1", team=TEAM, **kw):
    """Задача так, как её отдаёт трекер обходу: исполнитель вложенным
    объектом, команда — полем `teamId` того же запроса."""
    return {"id": id, "identifier": identifier, "teamId": team, **kw}


def sweeper(closed, **kw):
    st = Store()
    tr = Tracker(closed=closed)
    return st, tr, Archiver(st, tr, queue_key=QUEUE, agent_user_id=AGENT,
                            after_days=kw.pop("after_days", 7),
                            team_id=kw.pop("team_id", "t1"), **kw)


def test_the_sweep_takes_only_what_is_old_enough():
    st, tr, arch = sweeper([
        node("i1", "DEV-1", completedAt=iso(10)),
        node("i2", "DEV-2", completedAt=iso(1)),
        node("i3", "DEV-3", canceledAt=iso(30)),
    ])
    assert run(arch.tick()) == 2
    assert tr.archived == ["i1", "i3"]


def test_the_sweep_lets_the_tracker_pick_by_age():
    """Возраст отбирает трекер, а не мы: закрытых свежее порога бывает больше
    страницы, и тогда просеивание на своей стороне не дошло бы до старых
    НИКОГДА — обход крутился бы вхолостую каждые сутки и молчал об этом."""
    st, tr, arch = sweeper([], after_days=7)
    run(arch.tick(now=time.mktime((2026, 9, 12, 0, 0, 0, 0, 0, 0))))
    team, before = tr.asked[0]
    assert team == TEAM
    assert before.startswith("2026-09-0") and before.endswith("Z")


def test_the_sweep_skips_what_it_cannot_date():
    # «Не знаю когда» — это не «давно».
    st, tr, arch = sweeper([node("i1", "DEV-1")])
    assert run(arch.tick()) == 0
    assert tr.archived == []


def test_the_sweep_honours_a_live_session_too():
    # Правило одно на оба пути: обход не умнее ленты.
    st, tr, arch = sweeper([
        node("i1", "DEV-1", completedAt=iso(10), assignee={"id": AGENT})])
    assert run(arch.tick()) == 0
    assert tr.archived == []


def test_without_a_delay_there_is_no_sweep_at_all():
    """При нуле убирает лента, прямо на закрытии. Обход был бы вторым
    источником правды про «задача закрылась» — и разошёлся бы с первым."""
    st, tr, arch = sweeper([node(completedAt=iso(10))], after_days=0)
    assert run(arch.tick()) == 0
    assert tr.asked == []


def test_without_a_team_the_sweep_asks_nothing():
    # Рабочее пространство общее: обход без команды убирал бы чужие задачи.
    st, tr, arch = sweeper([node(completedAt=iso(10))], team_id="")
    assert run(arch.tick()) == 0
    assert tr.asked == []


def test_a_silent_tracker_does_not_break_the_sweep():
    class Mute(Tracker):
        def closed_issues(self, team_id, before):
            raise RuntimeError("трекер молчит")

    st = Store()
    tr = Mute()
    arch = Archiver(st, tr, after_days=7, team_id=TEAM)
    assert run(arch.tick()) == 0


def test_the_sweep_does_not_start_where_it_has_nothing_to_do():
    """`run` без отсрочки, без команды и без архива у трекера обязан
    ВЕРНУТЬСЯ, а не встать в вечный цикл: его заводят таской, и повисшая
    таска не отличима от работающей."""
    st = Store()
    run(asyncio.wait_for(Archiver(st, Tracker()).run(), 1))
    run(asyncio.wait_for(Archiver(st, Tracker(), after_days=7).run(), 1))
    run(asyncio.wait_for(
        Archiver(st, Plain(), after_days=7, team_id=TEAM).run(), 1))


# ── чужие задачи ─────────────────────────────────────────────────────────

def test_an_issue_of_another_team_is_never_archived():
    """Вебхук подписывают на весь воркспейс: без этой проверки закрытие в
    соседнем отделе уводило бы ЕГО задачи из активных."""
    st, tr, arch = mk()
    assert run(arch.on_closed(issue(team="t-маркетинг"))) is False
    assert tr.archived == []


def test_without_a_team_nothing_is_archived_at_all():
    """Пустой AGENT_TEAM_ID значит «не убираю», а не «убираю везде»: у ленты
    пустая команда означает «пиши про все», и унаследовать это уборке значит
    трогать чужую работу. У обхода правило то же — иначе два пути одной
    уборки жили бы по разным."""
    st, tr, arch = mk(team_id="")
    assert run(arch.on_closed(issue())) is False
    assert tr.archived == []


def test_a_task_is_ours_by_either_shape_of_the_team_field():
    # Событие ленты несёт `teamId`, ответ трекера — вложенный `team`.
    st, tr, arch = mk()
    assert arch.mine({"teamId": TEAM}) is True
    assert arch.mine({"team": {"id": TEAM}}) is True
    assert arch.mine({"teamId": "t-чужая"}) is False
    assert arch.mine({}) is False
