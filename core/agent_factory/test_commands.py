"""Команды в личке: отрабатывают ДО пачки, и только у владельца.

Половина тестов — про границу с приёмом: команда не должна ложиться куском
постановки, а обычный текст не должен пропадать из-за того, что похож на
команду. Вторая половина — про доступ: без карты владельца нет, и команды
не работают ни у кого.
"""
import asyncio
import json

from agent_factory.channel import Channel
from agent_factory.commands import NOT_OWNER, Commands, parse
from agent_factory.intake import Item, batch_key
from agent_factory.people import People
from agent_factory.reviewer import Reviewer

ANDREY, PAVEL = "u-andrey", "u-pavel"
OWNER_CHAT, PAVEL_CHAT = "435", "777"
USERS = [{"id": ANDREY, "name": "Андрей", "active": True},
         {"id": PAVEL, "name": "Иван", "active": True}]


class Store:
    def __init__(self):
        self.lists, self.vals, self.claimed = {}, {}, set()

    async def append(self, key, value):
        self.lists.setdefault(key, []).append(value); return len(self.lists[key])

    async def read_all(self, key): return list(self.lists.get(key) or [])
    async def get(self, key): return self.vals.get(key)
    async def put(self, key, value, ttl): self.vals[key] = value
    async def expire(self, key, ttl): pass
    async def enqueue(self, queue, element): await self.append(queue, element)
    async def position(self, queue, element): return None

    async def drop(self, key):
        self.lists.pop(key, None); self.vals.pop(key, None)

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key); return True


class Transport:
    def __init__(self):
        self.sent, self._n = [], 0

    async def send(self, chat_id, text, buttons=None, rich=False):
        self._n += 1; self.sent.append((str(chat_id), text)); return f"mid.{self._n}"

    async def edit(self, chat_id, message_id, text, buttons=None, rich=False): return True
    async def delete(self, chat_id, message_id): return True
    async def answer(self, callback_id, notice=""): pass

    def to(self, chat_id):
        return [t for c, t in self.sent if c == str(chat_id)]


class Tracker:
    def users(self): return list(USERS)
    def create_issue(self, *a, **k): return {}
    def assign(self, issue_id, user_id): return {}
    def delete(self, issue_id): return ""


def mk(people=None, with_reviewer=True):
    st, tp, tk = Store(), Transport(), Tracker()
    people = people if people is not None else People(
        {ANDREY: OWNER_CHAT, PAVEL: PAVEL_CHAT}, owner_user_id=ANDREY)
    rev = (Reviewer(st, people, tp, tk, key="agent:reviewer:x", owner_name="Андрей")
           if with_reviewer else None)
    ch = Channel("telegram", st, tp, tk, "agent:queue:x", allowed={"1"},
                 progress_opts={"tick_sec": 0}, people=people, reviewer=rev)
    return st, tp, ch


def text(chat, s, frm=""):
    return Item(type="text", source="telegram", text=s, frm=frm)


def run(c): return asyncio.run(c)


def batch(st, chat):
    return st.lists.get(batch_key("telegram", chat)) or []


# ── граница с пачкой ─────────────────────────────────────────────────────

def test_a_command_does_not_land_in_the_batch():
    st, tp, ch = mk()
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/reviewer"))) == 0
    assert batch(st, OWNER_CHAT) == []
    assert "принимает" in tp.to(OWNER_CHAT)[0]


def test_ordinary_text_still_lands_in_the_batch():
    st, tp, ch = mk()
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "почини экспорт"))) == 1
    assert len(batch(st, OWNER_CHAT)) == 1


def test_a_forwarded_command_is_a_quote_not_an_order():
    # Пересланное — чужой текст, и «/reviewer» в нём — цитата.
    st, tp, ch = mk()
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/reviewer Иван", frm="Катя"))) == 1
    assert len(batch(st, OWNER_CHAT)) == 1
    assert "agent:reviewer:x" not in st.vals


def test_an_unknown_command_goes_to_the_batch():
    # Молча съесть непонятое хуже, чем показать его в счётчике.
    st, tp, ch = mk()
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/help"))) == 1


def test_the_bot_suffix_after_the_command_is_ignored():
    st, tp, ch = mk()
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/reviewer@our_bot"))) == 0
    assert batch(st, OWNER_CHAT) == []


def test_the_command_name_is_case_insensitive():
    st, tp, ch = mk()
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/Reviewer"))) == 0


def test_without_a_reviewer_the_command_is_not_built_in():
    # Инсталляция без принимающего: текст с «/reviewer» — обычный текст.
    st, tp, ch = mk(with_reviewer=False)
    assert not ch.commands.knows("reviewer")
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/reviewer"))) == 1


# ── доступ ───────────────────────────────────────────────────────────────

def test_only_the_owner_may_hand_over_the_intake():
    st, tp, ch = mk()
    assert run(ch.on_item(PAVEL_CHAT, text(PAVEL_CHAT, "/reviewer Иван"))) == 0
    assert "agent:reviewer:x" not in st.vals
    assert tp.to(PAVEL_CHAT) == [NOT_OWNER]
    # И в пачку такое не ложится: это была команда, просто не его.
    assert batch(st, PAVEL_CHAT) == []


def test_the_owner_may_hand_over_the_intake():
    st, tp, ch = mk()
    run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/reviewer Иван")))
    assert json.loads(st.vals["agent:reviewer:x"])["linear_user_id"] == PAVEL


def test_without_a_people_map_nobody_is_the_owner():
    # Владелец назван в окружении, но карты нет — и он тоже не владелец.
    # Безопаснее, чем «работает у всех».
    st, tp, ch = mk(people=People({}, owner_user_id=ANDREY))
    run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/reviewer Иван")))
    assert "agent:reviewer:x" not in st.vals
    assert tp.to(OWNER_CHAT) == [NOT_OWNER]


# ── команды инсталляции ──────────────────────────────────────────────────

def test_an_installation_registers_its_own_command():
    st, tp, ch = mk()
    got = []

    async def automerge(chat_id, args):
        got.append((str(chat_id), args)); return "set"

    ch.commands.register("automerge", automerge)
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/automerge on"))) == 0
    assert got == [(OWNER_CHAT, "on")]
    assert batch(st, OWNER_CHAT) == []


def test_an_installation_command_is_owner_only_by_default():
    st, tp, ch = mk()
    got = []

    async def automerge(chat_id, args):
        got.append(args); return "set"

    ch.commands.register("automerge", automerge)
    run(ch.on_item(PAVEL_CHAT, text(PAVEL_CHAT, "/automerge on")))
    assert got == [] and tp.to(PAVEL_CHAT) == [NOT_OWNER]


def test_an_installation_may_open_a_command_to_everyone():
    st, tp, ch = mk()
    got = []

    async def status(chat_id, args):
        got.append(str(chat_id)); return "shown"

    ch.commands.register("status", status, owner_only=False)
    run(ch.on_item(PAVEL_CHAT, text(PAVEL_CHAT, "/status")))
    assert got == [PAVEL_CHAT]


def test_the_built_in_command_survives_the_installations_registrations():
    st, tp, ch = mk()

    async def automerge(chat_id, args): return "set"

    ch.commands.register("/automerge", automerge)
    assert ch.commands.knows("reviewer") and ch.commands.knows("automerge")


# ── разбор ───────────────────────────────────────────────────────────────

def test_parse_splits_the_name_from_the_tail():
    assert parse("/reviewer Иван на неделю") == ("reviewer", "Иван на неделю")
    assert parse("/reviewer") == ("reviewer", "")
    assert parse("  /reviewer@bot  сброс ") == ("reviewer", "сброс")


def test_text_that_is_not_a_command_parses_to_nothing():
    assert parse("почини /etc/hosts") is None
    assert parse("/") is None
    assert parse("") is None


def test_dispatch_ignores_non_text_pieces():
    c = Commands(People({ANDREY: OWNER_CHAT}, owner_user_id=ANDREY), Transport())

    async def h(chat_id, args): return "x"

    c.register("reviewer", h)
    photo = Item(type="photo", source="telegram", text="/reviewer")
    assert run(c.dispatch(OWNER_CHAT, photo)) is None


# ── /stop: снять идущую сессию (DEV-91) ─────────────────────────────────────

def test_stop_is_built_in_and_owner_only():
    st, tp, ch = mk()
    assert ch.commands.knows("stop")
    assert run(ch.on_item(PAVEL_CHAT, text(PAVEL_CHAT, "/stop DEV-91 ушла не туда"))) == 0
    assert tp.to(PAVEL_CHAT) == [NOT_OWNER]
    assert not any(k.startswith("agent:stop") for k in st.vals)


def test_stop_puts_the_signal_where_the_runner_reads_it():
    st, tp, ch = mk()
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/stop dev-91 ушла не туда"))) == 0
    rec = json.loads(st.vals["agent:stop:DEV-91"])
    assert rec["reason"] == "ушла не туда" and rec["by"] == OWNER_CHAT and rec["at"] > 0
    assert "DEV-91" in tp.to(OWNER_CHAT)[0] and "ушла не туда" in tp.to(OWNER_CHAT)[0]
    assert batch(st, OWNER_CHAT) == []


def test_stop_without_a_reason_asks_for_one():
    st, tp, ch = mk()
    run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/stop DEV-91")))
    assert "причину" in tp.to(OWNER_CHAT)[0]
    assert "agent:stop:DEV-91" not in st.vals


def test_stop_without_an_identifier_explains_the_form():
    st, tp, ch = mk()
    run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/stop ушла не туда")))
    assert "/stop DEV-91" in tp.to(OWNER_CHAT)[0]
    assert not any(k.startswith("agent:stop") for k in st.vals)


def test_stop_key_prefix_and_work_root_come_from_the_runner_variables():
    from agent_factory.commands import Stop
    s = Stop.from_env(None, None, {"AGENT_STOP_KEY_PREFIX": "agent:stop:x",
                                   "AGENT_WORK_ROOT": "/w"})
    assert (s.key_prefix, s.work_root) == ("agent:stop:x", "/w")
    s = Stop.from_env(None, None, {})
    assert (s.key_prefix, s.work_root) == ("agent:stop", "")


def test_stop_refuses_when_no_session_is_running(tmp_path):
    """Рабочий каталог виден и сессии по задаче в нём нет — сигнал в пустоту
    не кладём: он бы лёг на следующую сессию."""
    from agent_factory.commands import Stop
    st, tp = Store(), Transport()
    stop = Stop(st, tp, key_prefix="agent:stop:x", work_root=str(tmp_path))
    assert run(stop.command(OWNER_CHAT, "DEV-91 ушла")) == "not-running"
    assert "не вижу" in tp.to(OWNER_CHAT)[0]
    assert st.vals == {}


def test_stop_finds_the_running_session_in_the_work_root(tmp_path):
    from agent_factory.commands import Stop
    d = tmp_path / "DEV-91-20260915-100000"
    d.mkdir()
    (d / "transcript.jsonl").write_text('{"type":"system","subtype":"init"}\n')
    st, tp = Store(), Transport()
    stop = Stop(st, tp, key_prefix="agent:stop:x", work_root=str(tmp_path))
    assert run(stop.command(OWNER_CHAT, "DEV-91 ушла не туда")) == "ok"
    assert "agent:stop:x:DEV-91" in st.vals
    assert "вслепую" not in tp.to(OWNER_CHAT)[0]


def test_stop_without_a_work_root_lays_the_signal_blind():
    from agent_factory.commands import Stop
    st, tp = Store(), Transport()
    stop = Stop(st, tp)
    assert run(stop.command(OWNER_CHAT, "DEV-91 ушла")) == "ok"
    assert "вслепую" in tp.to(OWNER_CHAT)[0]


# ── /unblock: препятствие снято (DEV-105) ───────────────────────────────────
#
# Сторож за `unobservable` не отпускал задачу ничем: точка меняется мержем, а
# проверка после мержа бывает выключена. Команда даёт условие снятия — и
# ставит проверку тем же входом, что перевод статуса, а не мимо ворот.

def unblock(tmp_path, raise_check=None, work_root=None):
    from agent_factory.commands import Unblock
    tp = Transport()
    root = str(tmp_path) if work_root is None else work_root
    return tp, Unblock(root, tp, raise_check=raise_check)


def blocked(tmp_path, ident="DEV-66", cls="unobservable"):
    """След состоявшейся проверки, кончившейся «наблюдать негде»."""
    d = tmp_path / f"{ident}-verify-20260915-224100"
    d.mkdir()
    (d / "transcript.jsonl").write_text(json.dumps({"type": "result", "total_cost_usd": 1}))
    (d / "stats.json").write_text(json.dumps(
        {"задача": ident, "род": "verify", "исход": "READY_FOR_HUMAN",
         "класс": cls, "режим": "branch"}, ensure_ascii=False))
    (d / "verdict.txt").write_text("PARTIAL/unobservable ключи личностей мертвы\n")
    return d


def test_unblock_records_who_cleared_it_and_raises_the_check(tmp_path):
    from agent_factory import runs
    d = blocked(tmp_path)
    was = {f.name: f.read_bytes() for f in d.iterdir()}
    called = []

    async def raise_check(ident):
        called.append(ident); return "queued"

    tp, cmd = unblock(tmp_path, raise_check)
    assert run(cmd.command(OWNER_CHAT, "dev-66 токены личностей выпущены заново")) == "queued"
    rec = runs.clearance(str(tmp_path), "DEV-66")
    assert rec["кто"] == OWNER_CHAT and rec["почему"] == "токены личностей выпущены заново"
    assert rec["когда"] > 0 and called == ["DEV-66"]
    assert "проверяльщик в очереди" in tp.to(OWNER_CHAT)[0]
    # Запись прошлой проверки неприкосновенна.
    assert {f.name: f.read_bytes() for f in d.iterdir()} == was


def test_unblock_refuses_when_there_is_no_block(tmp_path):
    """Последняя проверка кончилась не «наблюдать негде» — снимать нечего, и
    бумага об этом только врала бы, что сторож был."""
    from agent_factory import runs
    blocked(tmp_path, cls="implementation")
    tp, cmd = unblock(tmp_path, raise_check=None)
    assert run(cmd.command(OWNER_CHAT, "DEV-66 починили")) == "no-block"
    assert "блокировки" in tp.to(OWNER_CHAT)[0] and "implementation" in tp.to(OWNER_CHAT)[0]
    assert runs.clearance(str(tmp_path), "DEV-66") == {}


def test_unblock_refuses_without_a_reason_and_without_an_identifier(tmp_path):
    from agent_factory import runs
    blocked(tmp_path)
    tp, cmd = unblock(tmp_path)
    assert run(cmd.command(OWNER_CHAT, "DEV-66")) == "no-reason"
    assert run(cmd.command(OWNER_CHAT, "починили токены")) == "no-ident"
    assert "/unblock DEV-66" in " ".join(tp.to(OWNER_CHAT))
    assert runs.clearance(str(tmp_path), "DEV-66") == {}


def test_unblock_without_a_work_root_says_so_instead_of_clearing_blindly(tmp_path):
    """Блокировку держат следы в рабочем каталоге — без него снимать негде, и
    ответить «снял» было бы неправдой."""
    tp, cmd = unblock(tmp_path, work_root="")
    assert run(cmd.command(OWNER_CHAT, "DEV-66 починили токены")) == "no-root"
    assert "⚠️" in tp.to(OWNER_CHAT)[0]


def test_unblock_tells_the_truth_when_the_check_was_not_raised(tmp_path):
    """Сигнал записан, а ворота сессию не поставили: человек обязан это
    прочитать, иначе он ждёт проверки, которой не будет."""
    from agent_factory import runs
    blocked(tmp_path)

    async def raise_check(ident): return "skipped"

    tp, cmd = unblock(tmp_path, raise_check)
    assert run(cmd.command(OWNER_CHAT, "DEV-66 починили токены")) == "skipped"
    assert "ворота проверку не поставили" in tp.to(OWNER_CHAT)[0]
    assert runs.clearance(str(tmp_path), "DEV-66")["когда"] > 0


def test_unblock_survives_a_falling_entrance(tmp_path):
    from agent_factory import runs
    blocked(tmp_path)

    async def raise_check(ident): raise RuntimeError("redis молчит")

    tp, cmd = unblock(tmp_path, raise_check)
    assert run(cmd.command(OWNER_CHAT, "DEV-66 починили токены")) == "cleared"
    assert "redis молчит" in tp.to(OWNER_CHAT)[0]
    assert runs.clearance(str(tmp_path), "DEV-66")["когда"] > 0


def test_unblock_is_owner_only_and_absent_without_an_entrance(tmp_path):
    """Канал вешает команду, только если инсталляция дала чем поднимать
    проверку: иначе её нет вовсе, и текст с ней уходит в пачку."""
    from agent_factory.commands import Unblock
    st, tp, ch = mk()
    assert not ch.commands.knows("unblock")
    blocked(tmp_path)
    st, tp, tk = Store(), Transport(), Tracker()
    people = People({ANDREY: OWNER_CHAT, PAVEL: PAVEL_CHAT}, owner_user_id=ANDREY)
    ch = Channel("telegram", st, tp, tk, "agent:queue:x", allowed={"1"},
                 progress_opts={"tick_sec": 0}, people=people, reviewer=None,
                 unblocker=Unblock(str(tmp_path), tp))
    assert ch.commands.knows("unblock")
    assert run(ch.on_item(PAVEL_CHAT, text(PAVEL_CHAT, "/unblock DEV-66 починили"))) == 0
    assert tp.to(PAVEL_CHAT) == [NOT_OWNER]
    from agent_factory import runs
    assert runs.clearance(str(tmp_path), "DEV-66") == {}
    assert run(ch.on_item(OWNER_CHAT, text(OWNER_CHAT, "/unblock DEV-66 починили токены"))) == 0
    assert runs.clearance(str(tmp_path), "DEV-66")["почему"] == "починили токены"
    assert "переведи задачу" in tp.to(OWNER_CHAT)[0].lower()
