"""Обновление ядра по кнопке — общая часть для всех инсталляций."""
import asyncio

from agent_factory.channel import Channel
from agent_factory import updater as updater_mod
from agent_factory.updater import Updater, drift_text, short, update_button

SHA_OLD = "19e4d53a40b150a1ab62d8d4a4ec62f8d215f231"
SHA_NEW = "168a6751168d86adf6127aa06eb45df44d60dc8a"


class FakeSource:
    def __init__(self, cur=SHA_OLD, avail=SHA_NEW):
        self.cur, self.avail = cur, avail
        self.applied = []
        self.fail = None

    def current(self): return self.cur
    def available(self): return self.avail

    def apply(self, version):
        if self.fail:
            raise RuntimeError(self.fail)
        self.applied.append(version)
        return "MR !264, смержится по зелёному"


class FakeTransport:
    def __init__(self):
        self.sent, self.edited = [], []
        self.edit_ok = True

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons)); return "m1"

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((message_id, text, buttons)); return self.edit_ok

    async def answer(self, callback_id, notice=""): pass
    async def delete(self, chat_id, message_id): return True


def mk(**kw):
    src, tr = FakeSource(**{k: v for k, v in kw.items() if k in ("cur", "avail")}), FakeTransport()
    return src, tr, Updater(src, tr, notify_chat="-100",
                            may_apply=kw.get("may_apply"))


# ── заметить и сказать ───────────────────────────────────────────────────

def test_drift_is_reported_with_a_button():
    src, tr, u = mk()
    assert asyncio.run(u.check()) == SHA_NEW
    chat, text, buttons = tr.sent[0]
    assert chat == "-100" and "19e4d53a" in text and "168a6751" in text
    assert buttons[0].payload == f"update:apply:{SHA_NEW}"


def test_no_drift_means_silence():
    """Иначе сообщение приходит каждый круг проверки и его перестают читать."""
    src, tr, u = mk(cur=SHA_NEW, avail=SHA_NEW)
    assert asyncio.run(u.check()) == ""
    assert tr.sent == []


def test_versions_that_could_not_be_read_are_not_drift():
    # Пустой ответ — это «не спросили», а не «отстали». Иначе недоступная сеть
    # выглядит как новая версия и предлагает обновиться в никуда.
    src, tr, u = mk(avail="")
    assert asyncio.run(u.check()) == ""
    assert tr.sent == []


def test_a_source_that_raises_does_not_take_the_check_down():
    src, tr, u = mk()
    src.current = lambda: (_ for _ in ()).throw(RuntimeError("сеть"))
    assert asyncio.run(u.check()) == ""


# ── применить ────────────────────────────────────────────────────────────

def test_the_button_installs_exactly_the_version_it_showed():
    # Пока человек думает, в ядро может уехать ещё что-нибудь. Он согласился
    # на то, что увидел, а не на «последнюю».
    src, tr, u = mk()
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1")) == "applied"
    assert src.applied == [SHA_NEW]


def test_the_outcome_replaces_the_message_and_removes_the_button():
    src, tr, u = mk()
    asyncio.run(u.apply(SHA_NEW, 1, "m1"))
    assert "168a6751" in tr.edited[-1][1] and "MR !264" in tr.edited[-1][1]
    assert tr.edited[-1][2] is None


def test_a_failure_says_why_and_keeps_the_button():
    src, tr, u = mk()
    src.fail = "protected branch"
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1")) == "failed"
    assert "protected branch" in tr.edited[-1][1]
    assert tr.edited[-1][2], "кнопка остаётся для повтора"
    assert src.applied == []


def test_a_lost_message_does_not_hide_the_outcome():
    """Обновление случилось — человек обязан узнать, даже если править нечего."""
    src, tr, u = mk()
    tr.edit_ok = False
    asyncio.run(u.apply(SHA_NEW, 1, "m1"))
    assert tr.sent and "168a6751" in tr.sent[-1][1]


def test_who_may_press_is_the_installations_business():
    src, tr, u = mk(may_apply=lambda uid: uid == 42)
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1", user_id=7)) == "not-allowed"
    assert src.applied == []
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1", user_id=42)) == "applied"


def test_an_empty_version_installs_nothing():
    src, tr, u = mk()
    assert asyncio.run(u.apply("", 1, "m1")) == "no-version"
    assert src.applied == []


# ── как показываем версию ────────────────────────────────────────────────

def test_a_sha_is_shortened_but_a_tag_is_not():
    assert short(SHA_NEW) == "168a6751"
    assert short("0.5.0") == "0.5.0"
    assert short("v1.2.3-rc1") == "v1.2.3-rc1"


def test_the_drift_message_names_both_versions():
    assert "19e4d53a" in drift_text(SHA_OLD, SHA_NEW)
    assert "168a6751" in drift_text(SHA_OLD, SHA_NEW)


def test_the_button_label_shows_what_will_be_installed():
    assert "168a6751" in update_button(SHA_NEW)[0].text


# ── через канал: у обоих фронтов одна дорога ─────────────────────────────

class Store:
    def __init__(self): self.lists, self.vals, self.claimed = {}, {}, set()
    async def append(self, k, v): self.lists.setdefault(k, []).append(v); return 1
    async def read_all(self, k): return list(self.lists.get(k) or [])
    async def get(self, k): return self.vals.get(k)
    async def put(self, k, v, t): self.vals[k] = v
    async def expire(self, k, t): pass
    async def drop(self, k): self.lists.pop(k, None); self.vals.pop(k, None)
    async def enqueue(self, q, e): await self.append(q, e)
    async def claim(self, k, t): return k not in self.claimed


def test_the_update_button_goes_through_the_channel():
    # Разводить её по фронтам значит снова написать одно и то же дважды:
    # вторая инсталляция читает опросами, первый клиент — вебхуком.
    src, tr = FakeSource(), FakeTransport()
    ch = Channel("telegram", Store(), tr, None, "q",
                 updater=Updater(src, tr), progress_opts={"tick_sec": 0})
    assert asyncio.run(ch.on_click(f"update:apply:{SHA_NEW}", 1, "m1")) == "applied"
    assert src.applied == [SHA_NEW]


def test_a_channel_without_an_updater_says_so_instead_of_crashing():
    src, tr = FakeSource(), FakeTransport()
    ch = Channel("telegram", Store(), tr, None, "q", progress_opts={"tick_sec": 0})
    assert asyncio.run(ch.on_click(f"update:apply:{SHA_NEW}", 1, "m1")) == "no-updater"


def test_a_version_that_moved_on_while_he_thought_is_not_installed():
    """Кнопку нарисовали на 168a6751, человек ушёл пить чай, в ядро уехало
    ещё два коммита. Нажатие обязано поставить то, на что он смотрел, —
    иначе он соглашается вслепую на то, чего не видел."""
    src, tr, u = mk()
    moved = "aaaaaaaa1111111111111111111111111111aaaa"
    src.avail = moved                       # ядро ушло вперёд
    asyncio.run(u.apply(SHA_NEW, 1, "m1"))  # нажали старую кнопку
    assert src.applied == [SHA_NEW], "ставим показанное, а не текущее"


# ── откуда берётся реализация инсталляции ────────────────────────────────

def test_no_source_configured_means_no_update_button(monkeypatch):
    """Продукт не знает ни про пины, ни про установщики. Не настроено —
    обновления просто нет, а не падение на старте."""
    from agent_factory.updater import source_from_env
    monkeypatch.delenv("CORE_UPDATE_SOURCE", raising=False)
    assert source_from_env() is None


def test_a_broken_source_does_not_take_the_intake_down(monkeypatch, caplog):
    # Приём важнее обновлений: ненастроенный апдейтер не повод не принимать
    # задачи.
    from agent_factory.updater import source_from_env
    monkeypatch.setenv("CORE_UPDATE_SOURCE", "нет_такого_модуля:Класс")
    assert source_from_env() is None
    monkeypatch.setenv("CORE_UPDATE_SOURCE", "без_двоеточия")
    assert source_from_env() is None


def test_a_configured_source_is_built(monkeypatch):
    from agent_factory.updater import source_from_env
    monkeypatch.setenv("CORE_UPDATE_SOURCE", "agent_factory.test_updater:FakeSource")
    src = source_from_env()
    assert src is not None and src.current() == SHA_OLD


def test_watch_is_off_unless_an_interval_is_given():
    """У инсталляции с собственным CI сторож — это он. Второй дублировал бы
    сообщения."""
    src, tr, u = mk()
    asyncio.run(u.watch(0))
    assert tr.sent == []


# ── кому можно нажимать ──────────────────────────────────────────────────

def test_an_async_check_can_refuse():
    """Право нажать инсталляция выясняет походом в хранилище: кто СЕЙЧАС
    принимает работу — владелец или делегат. Проверка от этого асинхронная, и
    без ожидания её ответа корутина считалась бы за «разрешено»: кнопка,
    выкатывающая ядро на прод, пустила бы кого угодно."""
    async def nobody(uid):
        return False

    src, tr, u = mk(may_apply=nobody)
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1", user_id=7)) == "not-allowed"
    assert src.applied == []


def test_an_async_check_can_allow():
    async def only_42(uid):
        return uid == 42

    src, tr, u = mk(may_apply=only_42)
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1", user_id=42)) == "applied"
    assert src.applied == [SHA_NEW]


def test_a_check_that_breaks_refuses_rather_than_lets_through():
    """«Не смогли спросить» — не повод пускать: нажатие выкатывает ядро
    вместе с контрактом всех сессий."""
    def boom(uid):
        raise RuntimeError("хранилище молчит")

    src, tr, u = mk(may_apply=boom)
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1", user_id=42)) == "not-allowed"
    assert src.applied == []


# ── сообщение, которое читает человек ────────────────────────────────────

class NamedSource(FakeSource):
    """Источник, умеющий назвать версию и перечислить, что в ней."""

    def __init__(self, names=None, changes=None, **kw):
        super().__init__(**kw)
        self.names = names or {SHA_OLD: "0.6.9", SHA_NEW: "0.6.14"}
        self._changes = changes if changes is not None else [
            "право нажать обновление спрашивается и асинхронно",
            "трекер умеет назвать автора задачи",
        ]
        self.boom_describe = False
        self.boom_changes = False

    def current(self):
        if getattr(self, "fail_current", False):
            raise RuntimeError("не прочитать отметку")
        return self.cur

    def describe(self, version):
        if self.boom_describe:
            raise RuntimeError("не спросить имя")
        return self.names.get(version, "")

    def changes(self, current, available):
        if self.boom_changes:
            raise RuntimeError("не спросить изменения")
        return list(self._changes)


def named(**kw):
    src, tr = NamedSource(**kw), FakeTransport()
    return src, tr, Updater(src, tr, notify_chat="-100")


def test_the_message_names_the_versions_a_person_recognises():
    # «10cd8c2b → 544a66e2» нельзя ни оценить, ни вспомнить.
    src, tr, u = named()
    asyncio.run(u.check())
    text = tr.sent[0][1]
    assert "agent-factory: 0.6.14" in text and "сейчас 0.6.9" in text
    assert SHA_NEW[:8] not in text


def test_the_message_says_what_is_in_the_new_version():
    src, tr, u = named()
    asyncio.run(u.check())
    text = tr.sent[0][1]
    assert "• право нажать обновление спрашивается и асинхронно" in text
    assert "• трекер умеет назвать автора задачи" in text


def test_the_button_carries_the_name_and_the_sha():
    # Подпись человеку, нагрузка машине: кнопка адресует конкретную версию.
    src, tr, u = named()
    asyncio.run(u.check())
    button = tr.sent[0][2][0]
    assert "0.6.14" in button.text
    assert button.payload == f"update:apply:{SHA_NEW}"


def test_a_long_list_of_changes_is_cut():
    # Больше десятка строк — это лог, его пролистывают не читая.
    src, tr, u = named(changes=[f"правка номер {i}" for i in range(30)])
    asyncio.run(u.check())
    assert tr.sent[0][1].count("•") == updater_mod.MAX_CHANGES


def test_a_very_long_line_is_cut():
    src, tr, u = named(changes=["а" * 300])
    asyncio.run(u.check())
    line = [x for x in tr.sent[0][1].splitlines() if x.startswith("•")][0]
    assert len(line) < 120 and line.endswith("…")


def test_a_source_that_cannot_name_versions_still_reports_drift():
    # Умение называть версии необязательное: без него остаётся прежний вид.
    src, tr, u = mk()
    asyncio.run(u.check())
    assert SHA_NEW[:8] in tr.sent[0][1]
    assert "•" not in tr.sent[0][1]


def test_a_broken_describe_falls_back_to_the_sha():
    # Не смогли назвать — это не повод промолчать про расхождение.
    src, tr, u = named()
    src.boom_describe = True
    asyncio.run(u.check())
    assert SHA_NEW[:8] in tr.sent[0][1]


def test_broken_changes_do_not_take_the_message_down():
    src, tr, u = named()
    src.boom_changes = True
    asyncio.run(u.check())
    assert "agent-factory: 0.6.14" in tr.sent[0][1]


def test_the_outcome_names_the_version_too():
    src, tr, u = named()
    asyncio.run(u.apply(SHA_NEW, 1, "m1"))
    assert "0.6.14" in tr.edited[-1][1]


def test_a_repeated_failure_is_still_shown():
    """Второе нажатие с тем же исходом мессенджер правкой не пропускает —
    сообщение уже содержит этот текст. Промолчать нельзя: снаружи это
    «нажал, и ничего», и человек не знает, дошло ли нажатие."""
    src, tr, u = mk()
    src.fail = "установщик вернул 1"
    tr.edit_ok = False
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1")) == "failed"
    assert tr.sent, "исход обязан доехать хотя бы новым сообщением"
    assert "не вышло" in tr.sent[-1][1]
    assert tr.sent[-1][2], "кнопка остаётся: можно нажать ещё раз"


# ── кнопка из старого сообщения ──────────────────────────────────────────

class HistorySource(NamedSource):
    """Источник с историей: знает, какая версия старее какой."""

    ORDER = [SHA_OLD, SHA_NEW]

    def changes(self, current, available):
        if self.boom_changes:
            raise RuntimeError("не спросить изменения")
        try:
            a, b = self.ORDER.index(current), self.ORDER.index(available)
        except ValueError:
            return []
        return [f"правка {i}" for i in range(a, b)]


def with_history(installed):
    src, tr = HistorySource(cur=installed), FakeTransport()
    return src, tr, Updater(src, tr, notify_chat="-100")


def test_a_button_older_than_what_is_installed_does_not_roll_back():
    """Кнопка живёт в чате вечно, а версия — нет. Источник честно ставит то,
    что у него попросили, вместе с установщиком той версии: без проверки
    нажатие на старое сообщение ОТКАТЫВАЕТ площадку."""
    src, tr, u = with_history(installed=SHA_NEW)
    assert asyncio.run(u.apply(SHA_OLD, 1, "m1")) == "stale-button"
    assert src.applied == [], "ничего ставить не должны"
    assert "старее установленной" in tr.edited[-1][1]


def test_a_button_for_what_is_already_installed_says_so():
    src, tr, u = with_history(installed=SHA_NEW)
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1")) == "stale-button"
    assert src.applied == []
    assert "уже стоит" in tr.edited[-1][1]


def test_a_newer_button_still_installs():
    src, tr, u = with_history(installed=SHA_OLD)
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1")) == "applied"
    assert src.applied == [SHA_NEW]


def test_a_source_that_cannot_tell_the_order_does_not_block_the_update():
    # Отказ обновления хуже лишнего: не знаем порядка — ставим.
    src, tr, u = mk()          # обычный источник, без describe/changes
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1")) == "applied"


def test_an_unreadable_installed_version_does_not_block_the_update():
    src, tr, u = with_history(installed=SHA_OLD)
    src.fail_current = True
    assert asyncio.run(u.apply(SHA_NEW, 1, "m1")) == "applied"


def test_the_stale_refusal_is_shown_even_if_the_edit_fails():
    src, tr, u = with_history(installed=SHA_NEW)
    tr.edit_ok = False
    asyncio.run(u.apply(SHA_OLD, 1, "m1"))
    assert tr.sent and "старее установленной" in tr.sent[-1][1]


def test_versions_with_the_same_name_are_told_apart_by_sha():
    """Номер версии больше не уникален на коммит — поднимаем его, когда есть
    что назвать. Без различения выходит «0.6.17 старее установленной
    (0.6.17)», то есть бессмыслица."""
    src, tr, u = with_history(installed=SHA_NEW)
    src.names = {SHA_OLD: "0.6.17", SHA_NEW: "0.6.17"}
    asyncio.run(u.apply(SHA_OLD, 1, "m1"))
    text = tr.edited[-1][1]
    assert SHA_OLD[:8] in text and SHA_NEW[:8] in text


def test_different_names_are_left_alone():
    src, tr, u = with_history(installed=SHA_NEW)
    asyncio.run(u.apply(SHA_OLD, 1, "m1"))
    text = tr.edited[-1][1]
    assert "0.6.9 старее установленной (0.6.14)" in text
    assert SHA_OLD[:8] not in text


# ── одно нажатие, два места: чат и страница подписи ─────────────────────────

def test_perform_is_the_button_without_the_chat():
    src, tr, u = mk()
    code, text = asyncio.run(u.perform(SHA_NEW))
    assert code == "applied" and text.startswith("⬆️ Ядро → " + SHA_NEW[:8])
    assert src.applied == [SHA_NEW] and tr.sent == [] and tr.edited == []


def test_perform_refuses_the_same_way_apply_does():
    src, tr, u = mk(may_apply=lambda uid: uid == 42)
    assert asyncio.run(u.perform(SHA_NEW, user_id=7))[0] == "not-allowed"
    assert asyncio.run(u.perform("", user_id=42))[0] == "no-version"
    assert asyncio.run(u.perform(SHA_OLD, user_id=42))[0] == "stale-button"
    src.fail = "установщик упал"
    code, text = asyncio.run(u.perform(SHA_NEW, user_id=42))
    assert code == "failed" and "установщик упал" in text
    assert src.applied == []


def test_drift_reads_both_versions_without_writing_to_the_chat():
    src, tr, u = mk()
    assert asyncio.run(u.drift()) == (SHA_OLD, SHA_NEW)
    assert tr.sent == []
