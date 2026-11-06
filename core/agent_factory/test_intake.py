"""Тесты приёма постановок — часть продукта.

Без pytest-asyncio: асинхронные случаи гоняются через asyncio.run. Причина не
в аккуратности, а в том, что 09.09 шесть таких тестов молча ПРОПУСТИЛИСЬ —
маркер @pytest.mark.asyncio без плагина не падает, а скипается, и прогон
выглядит зелёным. Продукт не должен зависеть от того, поставил ли кто-то
плагин на конкретной машине.

Каждый случай здесь родился из настоящей поломки 08–09.09.2026, а не из
воображения: приём выносился в продукт после трёх подряд расхождений между
транспортами и нескольких дефектов, которые владелец встретил живьём.
"""
import asyncio
import json

from agent_factory.intake import (
    Button, Intake, Item, batch_key, build_task, collect_keyboard,
    control_key, control_text, is_allowed, seen_key,
    source_of,
)


# ── ключи ────────────────────────────────────────────────────────────────

def test_keys_are_namespaced_by_source():
    """Разные инсталляции живут в одном redis — на bluefreedom рядом стоит
    Док. Совпадение ключей означало бы, что пачка одного клиента доедет до
    сессии другого."""
    assert batch_key("max", 1) != batch_key("telegram", 1)
    assert control_key("max", 1) != control_key("telegram", 1)
    assert "max" in batch_key("max", 1) and "telegram" in batch_key("telegram", 1)


def test_seen_key_identifies_one_event():
    """MAX ретраит доставку до десяти раз. Ключ обязан различать события,
    иначе одно нажатие «Собрать задачу» заводит несколько сессий."""
    a = seen_key("max", "message_callback", 1788899898461, "cb1")
    b = seen_key("max", "message_callback", 1788899899641, "cb2")
    assert a != b


# ── допуск ───────────────────────────────────────────────────────────────

def test_empty_allowlist_lets_nobody_in():
    """Инсталляция, где допуск забыли настроить, должна молчать, а не
    открывать приём задач кому попало."""
    assert is_allowed(1000000003, set()) is False


def test_allowed_by_person_not_by_chat():
    assert is_allowed(1000000003, {"1000000003"}) is True
    assert is_allowed("1000000003", {1000000003}) is True, "тип не должен решать"
    assert is_allowed(999, {"1000000003"}) is False


# ── источник едет в куске ────────────────────────────────────────────────

def test_source_travels_inside_the_item():
    """09.09: задача завелась из MAX, а карточка ушла в Telegram по чужому
    chat_id — `telegram alert failed: 400`. Человек не узнал, что задача
    создана."""
    assert source_of({"items": [{"type": "text", "source": "max"}]}) == "max"


def test_items_without_source_keep_old_behaviour():
    """Очередь переживает деплой: куски, положенные кодом без `source`,
    обязаны отвечать туда же, куда отвечали раньше."""
    assert source_of({"items": [{"type": "text"}]}) == "telegram"
    assert source_of({}) == "telegram"


def test_mixed_batch_takes_the_known_source():
    assert source_of({"items": [{"type": "text"},
                                {"type": "photo", "source": "max"}]}) == "max"


# ── клавиатура общая для всех транспортов ────────────────────────────────

def test_keyboard_lives_in_the_product():
    """Смысл выноса: убрал кнопку в продукте — она исчезла у ВСЕХ
    инсталляций. Проверка владельца 09.09: «я делаю изменения в фабрике,
    убрать кнопку, и она убирается для Дока»."""
    btns = collect_keyboard()
    assert [b.payload for b in btns] == ["intake:collect", "intake:reset"]
    assert all(isinstance(b, Button) for b in btns)


# ── задача для очереди ───────────────────────────────────────────────────

def test_task_carries_source_and_identifier():
    t = build_task("max", 1000000004, [Item(type="text", source="max", text="привет")],
                   "abc123", "2026-09-09T00:00:00Z")
    assert t["kind"] == "intake"
    assert t["source"] == "max"
    assert t["identifier"] == "INTAKE-abc123", "раннеру нужно имя каталога и журнала"
    assert t["items"][0]["source"] == "max"


def test_item_omits_empty_fields():
    """Пустые поля в очереди — шум, который потом читают глазами."""
    d = Item(type="text", source="telegram", text="x").to_dict()
    assert d == {"type": "text", "source": "telegram", "text": "x"}


def test_file_item_keeps_both_addressing_ways():
    """Telegram отдаёт file_id (по нему getFile), MAX — прямую ссылку;
    getFile-аналога у него нет."""
    tg = Item(type="document", source="telegram", file_id="f1", file_name="a.xlsx")
    mx = Item(type="document", source="max", file_url="https://fd/...", file_name="a.xlsx")
    assert tg.to_dict()["file_id"] == "f1" and "file_url" not in tg.to_dict()
    assert mx.to_dict()["file_url"].startswith("https://") and "file_id" not in mx.to_dict()


# ── поведение приёма ─────────────────────────────────────────────────────

class FakeStore:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.vals: dict[str, str] = {}
        self.queue: list[str] = []
        self.claimed: set[str] = set()

    async def append(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def read_all(self, key):
        return list(self.lists.get(key, []))

    async def drop(self, key):
        # DEL снимает и захват: в redis claim — это тот же ключ.
        self.lists.pop(key, None)
        self.vals.pop(key, None)
        self.claimed.discard(key)

    async def get(self, key):
        return self.vals.get(key)

    async def put(self, key, value, ttl):
        self.vals[key] = value

    async def claim(self, key, ttl):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    async def enqueue(self, queue, element):
        self.queue.append(element)

    async def expire(self, key, ttl):
        pass


class FakeTransport:
    def __init__(self, edit_ok=True):
        self.sent: list[tuple] = []
        self.edited: list[tuple] = []
        self.answers: list[str] = []
        self.deleted: list[str] = []
        self.edit_ok = edit_ok
        self.send_fails = False
        self._n = 0

    async def send(self, chat_id, text, buttons=None):
        self._n += 1
        self.sent.append((chat_id, text, buttons))
        return "" if self.send_fails else f"mid.{self._n}"

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((message_id, text, buttons))
        return self.edit_ok

    async def answer(self, callback_id, notice=""):
        self.answers.append(notice)

    async def delete(self, chat_id, message_id):
        self.deleted.append(message_id)
        return True


def _intake(store=None, transport=None):
    return Intake(source="max", store=store or FakeStore(),
                  transport=transport or FakeTransport(),
                  queue_key="agent:queue:test", allowed={"1"})


def test_a_burst_leaves_exactly_one_live_counter():
    asyncio.run(_test_a_burst_leaves_exactly_one_live_counter())


async def _test_a_burst_leaves_exactly_one_live_counter():
    """Пересылка пачкой приходит залпом. 09.09 три сообщения за 300 мс
    обработались параллельно, все прочитали «счётчика ещё нет», и человек
    получил два сообщения с разными числами вместо одного."""
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await asyncio.gather(*(
        i.add(1, Item(type="text", source="max", text=f"кусок {n}"))
        for n in range(3)))
    assert len(tr.sent) == 3, "счётчик пересоздаётся на каждый кусок"
    assert len(tr.deleted) == 2, "прежние убираются — живой ровно один"
    assert (await st.get(control_key("max", 1))) == "mid.3"


def test_counter_is_recreated_not_edited():
    asyncio.run(_test_counter_is_recreated_not_edited())


async def _test_counter_is_recreated_not_edited():
    """Мессенджер не поднимает отредактированное сообщение вниз ленты: после
    пяти пересланных подряд кнопка оказалась бы вверху, куда надо скроллить."""
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="раз"))
    await i.add(1, Item(type="text", source="max", text="два"))
    assert len(tr.sent) == 2 and tr.deleted == ["mid.1"]
    assert tr.edited == [], "правка счётчика — это кнопка, уехавшая вверх"


def test_a_send_that_never_landed_is_not_remembered():
    asyncio.run(_test_a_send_that_never_landed_is_not_remembered())


async def _test_a_send_that_never_landed_is_not_remembered():
    """Иначе ссылка на несуществующее сообщение уедет в следующий кусок, и он
    попытается удалить его второй раз."""
    st, tr = FakeStore(), FakeTransport()
    tr.send_fails = True
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="раз"))
    assert await st.get(control_key("max", 1)) is None


def test_collect_forgets_the_control_message():
    asyncio.run(_test_collect_forgets_the_control_message())


async def _test_collect_forgets_the_control_message():
    """Иначе следующая пачка молча правит отработавшее сообщение выше по
    ленте, и человек решает, что бот не ответил (09.09, MAX)."""
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="привет"))
    assert await st.get(control_key("max", 1)) is not None
    await i.collect(1, "d1", "2026-09-09T00:00:00Z")
    assert await st.get(control_key("max", 1)) is None


def test_collect_puts_the_task_in_the_queue_and_clears_the_batch():
    asyncio.run(_test_collect_puts_the_task_in_the_queue_and_clears_the_batch())


async def _test_collect_puts_the_task_in_the_queue_and_clears_the_batch():
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="a"))
    await i.add(1, Item(type="document", source="max", file_url="u", file_name="b.xlsx"))
    got = await i.collect(1, "d1", "2026-09-09T00:00:00Z")
    assert got and len(got.items) == 2
    assert len(st.queue) == 1
    assert json.loads(st.queue[0])["identifier"] == "INTAKE-d1"
    assert got.element == st.queue[0], "элемент отдаём тот же, что положили"
    assert await st.read_all(batch_key("max", 1)) == [], "пачка очищается"


def test_collect_on_empty_batch_does_nothing():
    asyncio.run(_test_collect_on_empty_batch_does_nothing())


async def _test_collect_on_empty_batch_does_nothing():
    """Нажатие по пустой пачке не должно заводить сессию: слот дорог."""
    st = FakeStore()
    i = _intake(st)
    assert await i.collect(1, "d1", "2026-09-09T00:00:00Z") is None
    assert st.queue == []


def test_reset_clears_batch_and_control():
    asyncio.run(_test_reset_clears_batch_and_control())


async def _test_reset_clears_batch_and_control():
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="a"))
    await i.reset(1)
    assert await st.read_all(batch_key("max", 1)) == []
    assert await st.get(control_key("max", 1)) is None


def test_reset_takes_the_buttons_off_the_counter():
    asyncio.run(_test_reset_takes_the_buttons_off_the_counter())


async def _test_reset_takes_the_buttons_off_the_counter():
    """Забыть счётчик мало: сообщение остаётся в ленте с живой «Собрать
    задачу», и следующий тап приходит на пачку, которой уже нет."""
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="a"))
    await i.reset(1)
    assert tr.edited, "сообщение-счётчик должно быть погашено"
    assert tr.edited[-1][2] is None, "кнопки должны исчезнуть"


def test_collect_takes_the_buttons_off_the_counter():
    asyncio.run(_test_collect_takes_the_buttons_off_the_counter())


async def _test_collect_takes_the_buttons_off_the_counter():
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="a"))
    await i.collect(1, "d1", "2026-09-09T00:00:00Z")
    assert tr.edited[-1][2] is None
    assert await st.get(control_key("max", 1)) is None


def test_next_batch_gets_a_new_counter_message():
    asyncio.run(_test_next_batch_gets_a_new_counter_message())


async def _test_next_batch_gets_a_new_counter_message():
    """Погашенный счётчик нельзя переиспользовать: правка ушла бы в старое
    сообщение наверху ленты, и новой пачки человек бы не увидел."""
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="a"))
    await i.collect(1, "d1", "2026-09-09T00:00:00Z")
    before = len(tr.sent)
    await i.add(1, Item(type="text", source="max", text="b"))
    assert len(tr.sent) == before + 1


# ── потолки ──────────────────────────────────────────────────────────────

def test_a_full_batch_says_so_instead_of_silently_dropping():
    asyncio.run(_test_a_full_batch_says_so_instead_of_silently_dropping())


async def _test_a_full_batch_says_so_instead_of_silently_dropping():
    """Потолок защищает сессию от «переслал переписку за месяц». Но молча не
    взять нельзя: человек считает, что фактура доехала."""
    st, tr = FakeStore(), FakeTransport()
    i = Intake(source="max", store=st, transport=tr,
               queue_key="q", allowed={"1"}, max_items=2)
    for n in range(2):
        assert (await i.add(1, Item(type="text", source="max", text=str(n)))).ok
    third = await i.add(1, Item(type="text", source="max", text="лишний"))
    assert third.ok is False and "2" in third.reason
    assert len(await st.read_all(batch_key("max", 1))) == 2


def test_an_oversized_file_is_refused_on_the_spot():
    asyncio.run(_test_an_oversized_file_is_refused_on_the_spot())


async def _test_an_oversized_file_is_refused_on_the_spot():
    """Лимит платформы, а не наш. Сказать надо сразу, а не через минуту от
    сессии, которой файл не доехал."""
    st, tr = FakeStore(), FakeTransport()
    i = Intake(source="max", store=st, transport=tr, queue_key="q",
               max_file_bytes=1024)
    res = await i.add(1, Item(type="document", source="max",
                              file_name="дамп.sql", file_size=5 * 1024 * 1024))
    assert res.ok is False and "дамп.sql" in res.reason
    assert await st.read_all(batch_key("max", 1)) == []
    assert tr.sent == [], "счётчик рисовать не за что"


# ── возврат фактуры ──────────────────────────────────────────────────────

def test_restore_keeps_what_arrived_while_the_session_ran():
    asyncio.run(_test_restore_keeps_what_arrived_while_the_session_ran())


async def _test_restore_keeps_what_arrived_while_the_session_ran():
    """Пока шёл заход, человек мог переслать ещё. Без объединения эхо затёрло
    бы эти куски, и ссылки на файлы пропали бы совсем."""
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="дослал", frm="Я"))
    dropped = await i.restore(1, [{"type": "text", "message_id": 1, "text": "ушло в заход"}])
    got = [json.loads(x)["text"] for x in await st.read_all(batch_key("max", 1))]
    assert got == ["ушло в заход", "дослал"], "вернувшееся впереди, дословка следом"
    assert dropped == 0


def test_restore_does_not_duplicate_the_same_piece():
    asyncio.run(_test_restore_does_not_duplicate_the_same_piece())


async def _test_restore_does_not_duplicate_the_same_piece():
    """Тот же кусок возвращается от раннера то с пометкой об ошибке, то без."""
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await st.append(batch_key("max", 1),
                    json.dumps({"type": "photo", "file_id": "f1"}))
    await i.restore(1, [{"type": "photo", "file_id": "f1", "error": "не доехал"}])
    assert len(await st.read_all(batch_key("max", 1))) == 1


def test_restore_cuts_the_tail_and_reports_the_loss():
    asyncio.run(_test_restore_cuts_the_tail_and_reports_the_loss())


async def _test_restore_cuts_the_tail_and_reports_the_loss():
    """Режем хвост, а не голову: вернувшаяся фактура — предмет разговора, а
    хвост человек дослал только что и помнит. Молчать о потере нельзя —
    обещание «фактура не потеряна» тогда враньё."""
    st, tr = FakeStore(), FakeTransport()
    i = Intake(source="max", store=st, transport=tr, queue_key="q", max_items=2)
    for n in range(2):
        await st.append(batch_key("max", 1),
                        json.dumps({"type": "text", "message_id": 100 + n}))
    dropped = await i.restore(1, [{"type": "text", "message_id": 1},
                                  {"type": "text", "message_id": 2}])
    kept = [json.loads(x)["message_id"] for x in await st.read_all(batch_key("max", 1))]
    assert kept == [1, 2] and dropped == 2


def test_restore_of_nothing_changes_nothing():
    asyncio.run(_test_restore_of_nothing_changes_nothing())


async def _test_restore_of_nothing_changes_nothing():
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    assert await i.restore(1, []) == 0
    assert tr.sent == []


class SlowStore(FakeStore):
    """Хранилище, отвечающее с задержкой.

    Значение читается СРАЗУ, а отдаётся после уступки управления — так ведёт
    себя сеть: ответ уже получен, но действуем по нему позже, когда соседняя
    обработка успела записать своё. Иначе гонка не воспроизводится: фейк,
    читающий после сна, всегда видит чужую запись и выглядит безопасным.
    """

    async def read_all(self, key):
        got = await super().read_all(key)
        await asyncio.sleep(0)
        return got

    async def get(self, key):
        got = await super().get(key)
        await asyncio.sleep(0)
        return got


def test_two_pieces_at_once_do_not_leave_two_counters():
    asyncio.run(_test_two_pieces_at_once_do_not_leave_two_counters())


async def _test_two_pieces_at_once_do_not_leave_two_counters():
    st, tr = SlowStore(), FakeTransport()
    i = _intake(st, tr)
    await asyncio.gather(
        i.add(1, Item(type="text", source="max", text="раз")),
        i.add(1, Item(type="text", source="max", text="два")))
    assert len(await st.read_all(batch_key("max", 1))) == 2
    assert len(tr.deleted) == 1, "второй обязан убрать счётчик первого"
    assert await st.get(control_key("max", 1)) == "mid.2"


def test_a_failed_enqueue_keeps_the_batch():
    asyncio.run(_test_a_failed_enqueue_keeps_the_batch())


async def _test_a_failed_enqueue_keeps_the_batch():
    """Сбой очереди не должен стоить человеку всей пересланной фактуры."""
    st, tr = FakeStore(), FakeTransport()

    async def boom(queue, element):
        raise RuntimeError("очередь недоступна")

    st.enqueue = boom
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="дорогая фактура"))
    try:
        await i.collect(1, "d1", "2026-09-09T00:00:00Z")
    except RuntimeError:
        pass
    assert len(await st.read_all(batch_key("max", 1))) == 1, "пачка на месте"


def test_counter_is_forgotten_when_the_send_did_not_land():
    asyncio.run(_test_counter_is_forgotten_when_the_send_did_not_land())


async def _test_counter_is_forgotten_when_the_send_did_not_land():
    """Ссылка на несуществующее сообщение уехала бы в следующий кусок, и он
    попытался бы удалить его второй раз."""
    st, tr = FakeStore(), FakeTransport()
    i = _intake(st, tr)
    await i.add(1, Item(type="text", source="max", text="раз"))
    tr.send_fails = True
    await i.add(1, Item(type="text", source="max", text="два"))
    assert not await st.get(control_key("max", 1))


# ── счётчик, когда прежнее сообщение уже не спасти ───────────────────────

class DeadCounter(FakeTransport):
    """Транспорт, у которого прежнее сообщение и не удаляется, и не правится.

    Так ведёт себя Telegram, когда сообщение удалили руками или оно старше
    суток: `deleteMessage` и `editMessageText` отвечают 400.
    """

    async def delete(self, chat_id, message_id):
        self.deleted.append(message_id)
        return False

    async def edit(self, chat_id, message_id, text, buttons=None, rich=False):
        self.edited.append((message_id, text))
        return False


def test_a_counter_whose_message_is_gone_is_sent_anew():
    """12.09.2026: мёртвая ссылка в памяти глушила ответ на КАЖДЫЙ следующий
    кусок. Человек прислал шесть сообщений и не увидел ничего — пачка росла,
    счётчик не появлялся."""
    st, tp = FakeStore(), DeadCounter()
    i = Intake(source="telegram", store=st, transport=tp, queue_key="q")
    asyncio.run(st.put(control_key("telegram", 1), "мёртвое", 100))
    asyncio.run(i.add(1, Item(type="text", source="telegram", text="раз",
                              message_id="1")))
    assert tp.sent, "счётчик обязан уехать новым сообщением"
    assert asyncio.run(st.get(control_key("telegram", 1))) != "мёртвое"


def test_a_transport_that_cannot_delete_still_edits_in_place():
    # MAX удалять не умеет вовсе — там правка на месте остаётся правильной,
    # иначе в чате копятся три счётчика с разными числами.
    class NoDelete(FakeTransport):
        async def delete(self, chat_id, message_id):
            self.deleted.append(message_id)
            return False

    st, tp = FakeStore(), NoDelete()
    i = Intake(source="max", store=st, transport=tp, queue_key="q")
    asyncio.run(st.put(control_key("max", 1), "живое", 100))
    asyncio.run(i.add(1, Item(type="text", source="max", text="раз",
                              message_id="1")))
    assert tp.edited and tp.edited[-1][0] == "живое"
    assert not tp.sent, "нового счётчика быть не должно — правка прошла"


# ── что человек видит в счётчике ─────────────────────────────────────────

def test_the_counter_shows_how_many_and_of_what():
    """Состав, а не только число: человек видит, что доехало, не
    пересматривая переписку. При переносе приёма в продукт состав и значки
    потерялись, и заметили это только в проде — счётчик ничем не был закреплён."""
    text = control_text([{"type": "photo"}, {"type": "photo"}, {"type": "text"}])
    assert text.startswith("📥")
    assert "3 шт." in text
    assert "картинки: 2" in text and "текст: 1" in text
    assert "Собрать задачу" in text


def test_the_batch_buttons_keep_their_icons():
    # Кнопки человек ищет глазами по значку, а не по тексту.
    collect, reset = collect_keyboard(1)
    assert collect.text == "📝 Собрать задачу"
    assert reset.text == "🗑 Сбросить"
    assert collect.payload == "intake:collect:1"
    assert reset.payload == "intake:reset:1"
