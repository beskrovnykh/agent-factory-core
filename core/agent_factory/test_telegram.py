"""Тесты телеграмного приёма. Разбор входящего — чистая функция, её и проверяем."""
import asyncio
import json
import logging
import urllib.error

from agent_factory import telegram
from agent_factory.intake import Item
from agent_factory.telegram import TelegramTransport, item_from_message


# ── разбор сообщения ─────────────────────────────────────────────────────

def test_plain_text():
    it = item_from_message({"message_id": 1, "text": "почини экспорт"})
    assert it and it.type == "text" and it.source == "telegram"
    assert it.text == "почини экспорт"


def test_forward_new_format_keeps_author():
    """Происхождение важно: постановка без «от кого» теряет половину смысла."""
    it = item_from_message({"message_id": 2, "text": "сломалось",
        "forward_origin": {"type": "user",
                           "sender_user": {"first_name": "Иван", "last_name": ""}}})
    assert it.frm == "Иван"


def test_forward_legacy_format_still_works():
    """В чатах живут сообщения, пересланные старыми клиентами."""
    it = item_from_message({"message_id": 3, "text": "x",
                            "forward_from": {"first_name": "Иван"}})
    assert it.frm == "Иван"


def test_forward_from_hidden_account():
    """У закрытого профиля Telegram отдаёт только имя строкой."""
    it = item_from_message({"message_id": 4, "text": "x",
                            "forward_sender_name": "Аноним"})
    assert it.frm == "Аноним"


def test_photo_takes_the_largest_size():
    """Мелкое превью читать бесполезно — модель не разберёт, что на нём."""
    it = item_from_message({"message_id": 5, "caption": "вот так выглядит",
        "photo": [{"file_id": "small", "file_size": 100},
                  {"file_id": "big", "file_size": 90000}]})
    assert it.type == "photo" and it.file_id == "big"
    assert it.text == "вот так выглядит", "подпись — часть постановки"


def test_document_keeps_name_and_mime():
    it = item_from_message({"message_id": 6,
        "document": {"file_id": "f1", "file_name": "отчёт.xlsx",
                     "mime_type": "application/vnd.ms-excel", "file_size": 16323}})
    assert it.type == "document" and it.file_name == "отчёт.xlsx"
    assert it.mime.startswith("application/")


def test_empty_message_is_not_collected():
    """Служебное событие не должно засорять пачку куском без содержания."""
    assert item_from_message({"message_id": 7}) is None
    assert item_from_message({"message_id": 8, "text": "   "}) is None


# ── клавиатура ───────────────────────────────────────────────────────────

def test_empty_markup_removes_keyboard():
    """Пустой список СНИМАЕТ клавиатуру. Без этого под разобранной пачкой
    остаётся живая кнопка «Собрать задачу» — второй заход на те же куски."""
    assert TelegramTransport._markup(None) == {"inline_keyboard": []}


def test_markup_carries_payload():
    from agent_factory.intake import collect_keyboard
    m = TelegramTransport._markup(collect_keyboard())
    payloads = [b[0]["callback_data"] for b in m["inline_keyboard"]]
    assert payloads == ["intake:collect", "intake:reset"]


# ── правка без message_id ────────────────────────────────────────────────

def test_edit_refuses_empty_message_id():
    """09.09 на MAX пустой mid уходил в запрос и возвращался «Missing required
    parameter» — правка молча не применялась, счётчик застревал."""
    sent = []
    t = TelegramTransport("x")
    t._call = lambda m, p: sent.append(m) or {"ok": True}
    assert asyncio.run(t.edit(1, "", "текст")) is False
    assert sent == [], "запрос слаться не должен"


# ── паритет разбора с первой реализацией ─────────────────────────────────

def test_video_is_refused_with_a_reason():
    """Молча съеденный кусок хуже отказа: постановка соберётся без него, и
    понять это можно будет только по итоговому черновику."""
    it = item_from_message({"message_id": 9, "video": {"file_id": "v"}})
    assert it.type == "unsupported" and "видео" in it.reason


def test_voice_is_recognised_as_voice_not_as_a_file():
    for field in ("voice", "audio", "video_note"):
        it = item_from_message({"message_id": 9, field: {"file_id": "a",
                                                         "mime_type": "audio/ogg"}})
        assert it.type == "voice", field


def test_document_mime_travels_under_the_key_the_runner_reads():
    it = item_from_message({"message_id": 6, "document": {
        "file_id": "f", "file_name": "о.xlsx", "mime_type": "application/x"}})
    assert it.to_dict()["mime"] == "application/x"


def test_message_id_travels_with_the_piece():
    """Без него вернувшийся от раннера кусок считается новым и ложится в
    пачку вторым."""
    from agent_factory.intake import item_id
    a = item_from_message({"message_id": 42, "text": "раз"}).to_dict()
    b = dict(a, error="не доехал")
    assert item_id(a) == item_id(b)


def test_a_caption_under_a_photo_is_the_text():
    it = item_from_message({"message_id": 5, "caption": "вот тут",
                            "photo": [{"file_id": "b", "file_size": 9}]})
    assert it.text == "вот тут" and it.mime == "image/jpeg"


def test_unsupported_is_not_collected_but_explained():
    import asyncio
    from agent_factory.intake import Intake, Item

    class St:
        async def read_all(self, k): return []
        async def append(self, k, v): return 1
        async def expire(self, k, t): pass
        async def get(self, k): return None
        async def put(self, k, v, t): pass
        async def drop(self, k): pass

    i = Intake(source="telegram", store=St(), transport=None, queue_key="q")
    res = asyncio.run(i.add(1, Item(type="unsupported", source="telegram",
                                    reason="видео вне скоупа")))
    assert res.ok is False and "видео вне скоупа" in res.reason


def test_the_front_starts_up_and_reports_what_it_serves():
    """Строчка запуска обращается к каналу и списку допущенных. 10.09 после
    переноса логики в канал она осталась на старом поле, и приём падал на
    первой же секунде — тестами это место закрыто не было."""
    import logging

    from agent_factory.telegram import TelegramIntake

    app = TelegramIntake(token="т", redis_url="redis://127.0.0.1:6379/0",
                         queue_key="agent:queue:x", allowed={"1"})
    said = []
    logging.getLogger("intake").info = lambda m, *a: said.append(m % a)
    app._say_started()
    assert "agent:queue:x" in said[0] and "1" in said[0]


# ── вебхук трекера: чем его включает инсталляция ─────────────────────────

def _app(**kw):
    from agent_factory.telegram import TelegramIntake
    return TelegramIntake(token="т", redis_url="redis://127.0.0.1:6379/0",
                          queue_key="agent:queue:x", allowed={"1"}, **kw)


def test_without_a_port_there_is_no_webhook():
    # Инсталляции, до которой снаружи не достучаться, порт не нужен вовсе.
    assert _app().webhook is None


def test_an_address_and_a_secret_are_all_it_takes():
    # Ровно это и есть «параметризуется адресом»: ни строчки кода на
    # инсталляцию.
    app = _app(webhook_port=8787, webhook_path="/linear",
               webhook_secret="s", agent_user_id="a1")
    assert app.webhook and app.webhook.port == 8787
    assert app.webhook.path == "/linear"
    assert app.webhook.receiver.queue_key == "agent:queue:x"


def test_a_webhook_without_a_secret_is_not_raised():
    # Открытый порт, заводящий сессии по чужому телу, хуже отсутствия
    # вебхука. Слушающий и всё отвергающий — хуже обоих: снаружи он выглядит
    # работающим.
    assert _app(webhook_port=8787, agent_user_id="a1").webhook is None


def test_a_webhook_without_an_agent_is_not_raised():
    assert _app(webhook_port=8787, webhook_secret="s").webhook is None


def test_the_startup_line_names_every_way_work_can_start():
    # Каким путём задача попадает в работу — видно сразу, а не по отсутствию
    # сессии через час.
    import logging

    app = _app(webhook_port=8787, webhook_secret="s", agent_user_id="a1")
    said = []
    logging.getLogger("intake").info = lambda m, *a: said.append(m % a)
    app._say_started()
    assert "вебхук" in said[0] and "8787" in said[0]


# ── сборка из окружения ──────────────────────────────────────────────────

def _env(monkeypatch, **kw):
    from agent_factory import telegram as tg
    base = {"TELEGRAM_BOT_TOKEN": "т", "INTAKE_ALLOWED_USER_IDS": "1",
            "LINEAR_API_KEY": "k", "AGENT_QUEUE_KEY": "agent:queue:x",
            "CORE_UPDATE_SOURCE": ""}
    for k in ("AGENT_INCIDENT_LABEL", "AGENT_LINEAR_USER_ID", "AGENT_TEAM_ID",
              "AGENT_PULL_MAX_IN_FLIGHT", "AGENT_PULL_MAX_PER_DAY",
              "AGENT_PULL_EVERY_SEC", "AGENT_PULL_MIN_AGE_SEC",
              "AGENT_PULL_SKIP_LABELS", "AGENT_PULL_PER_WINDOW", "AGENT_PULL_WINDOW_SEC", "AGENT_PULL_MAX_OPEN", "SIGNING_TOKEN", "SIGNING_PATH",
              "SIGNING_STATES", "SIGNING_WORKING_STATES", "AGENT_WORK_ROOT", "SIGNING_ALLOWED_USER_IDS",
              "AGENT_UNVERIFIED_STATE_NAME", "AGENT_VERIFYING_STATE_NAME",
              "TRACKER_WEBHOOK_PORT", "TRACKER_WEBHOOK_SECRET",
              "AGENT_POLL_ASSIGNMENTS_SEC", "AGENT_NOTIFY_CHAT_ID",
              "AGENT_PROJECT_ID", "DEPLOY_WEBHOOK_SECRET",
              "DEPLOY_WEBHOOK_PATH", "AGENT_DEPLOYMENT_SOURCE",
              "AGENT_ARCHIVE_AFTER_DAYS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**base, **kw}.items():
        monkeypatch.setenv(k, v)
    return tg.app_from_env()


def test_the_duty_label_reaches_the_tracker_too(monkeypatch):
    """Лейбл нужен ДВУМ потребителям: приём вешает его на создаваемую задачу,
    раннер по нему поднимает дежурную сессию. Пока трекер собирался без него,
    инцидент из приёма уезжал обычной работой, и увидеть это можно было
    только по промпту сессии."""
    app = _env(monkeypatch, AGENT_INCIDENT_LABEL="дежурный")
    assert app.channel.cards.incident_label == "дежурный"
    assert app.channel.results.tracker._incident_label == "дежурный"


def test_an_installation_without_a_duty_label_gets_none_anywhere(monkeypatch):
    # Развилки «дежурный или работа» у инсталляции может не быть вовсе, и
    # выдумывать её нельзя ни на одной из сторон.
    app = _env(monkeypatch, AGENT_INCIDENT_LABEL="")
    assert app.channel.cards.incident_label == ""
    assert app.channel.results.tracker._incident_label == ""


def test_the_chat_signoff_is_wired_into_the_feed_and_the_channel(monkeypatch):
    """Кнопки «Слить»/«Отменить» под сдачей (DEV-123) появляются только если
    подпись доехала до ДВУХ мест: до ленты (она вешает клавиатуру) и до
    канала (он разбирает нажатие). Доедет в одно — кнопка есть и не
    работает, или работает и её никто не видит."""
    app = _env(monkeypatch, AGENT_PEOPLE_MAP='{"u-andrey":"1000000002"}',
               AGENT_REVIEWER_LINEAR_USER_ID="u-andrey",
               AGENT_HOSTING_SOURCE="agent_factory.hosting:GitHubHosting",
               SIGNING_ALLOWED_USER_IDS="1000000002")
    assert app.feed is not None and app.feed.handoff is app.signoff
    assert app.channel.signoff is app.signoff
    assert app.signoff.hosting is app.hosting
    assert app.signoff.verification is app.verification
    # Нажать может тот, кто ПОДПИСЫВАЕТ работу, а не любой допущенный в
    # приём: приём — это «положить кусок в пачку», а здесь мерж в main.
    assert app.signoff.may_act("1000000002") and not app.signoff.may_act("1")


def test_without_a_signing_list_the_intake_access_decides(monkeypatch):
    # Список не задан — подпись из чата работает у тех же, кого пускает
    # приём (как кнопка обновления ядра), а не ни у кого.
    app = _env(monkeypatch, AGENT_PEOPLE_MAP='{"u-andrey":"1000000002"}',
               AGENT_REVIEWER_LINEAR_USER_ID="u-andrey")
    assert app.signoff.may_act("1") and not app.signoff.may_act("1000000002")
    # Хостинга нет — мержить нечем, и кнопки «Слить» не будет ни у одной
    # задачи; «Отменить» от хостинга не зависит.
    assert app.signoff.hosting is None


def test_a_missing_token_builds_nothing(monkeypatch):
    assert _env(monkeypatch, TELEGRAM_BOT_TOKEN="") is None


def test_an_empty_allow_list_builds_nothing(monkeypatch):
    # Приём заводит задачи в трекере и жжёт сессии: открытым его оставлять
    # нельзя, и «пустой список = пускаем всех» было бы ровно этим.
    assert _env(monkeypatch, INTAKE_ALLOWED_USER_IDS="") is None


# ── лента задачи в чате ──────────────────────────────────────────────────

def test_the_feed_is_wired_to_the_webhook(monkeypatch):
    # Одно событие трекера — два потребителя: очередь и человек. Второй
    # вебхук ради этого заводить незачем.
    app = _env(monkeypatch, TRACKER_WEBHOOK_PORT="8787",
               TRACKER_WEBHOOK_SECRET="s", AGENT_LINEAR_USER_ID="a1",
               AGENT_NOTIFY_CHAT_ID="-100")
    assert app.feed is not None
    assert app.webhook.receiver.feed is app.feed


def test_the_second_entrance_to_checking_is_wired_to_the_webhook(monkeypatch):
    # Проверка по смене статуса — единственный способ проверить то, у чего
    # выката не бывает вовсе (клиент, библиотека). Между настройкой и
    # приёмником не должно быть места, где имя состояния теряется (DEV-17).
    app = _env(monkeypatch, TRACKER_WEBHOOK_PORT="8787",
               TRACKER_WEBHOOK_SECRET="s", AGENT_LINEAR_USER_ID="a1",
               AGENT_NOTIFY_CHAT_ID="-100",
               AGENT_VERIFYING_STATE_NAME="Verifying")
    assert app.webhook.receiver.verification is app.verification
    assert app.webhook.receiver.verify_state == "Verifying"


def test_without_the_state_name_the_second_entrance_stays_closed(monkeypatch):
    # Площадка, которая про него не знает, работает ровно как раньше.
    app = _env(monkeypatch, TRACKER_WEBHOOK_PORT="8787",
               TRACKER_WEBHOOK_SECRET="s", AGENT_LINEAR_USER_ID="a1",
               AGENT_NOTIFY_CHAT_ID="-100")
    assert app.webhook.receiver.verify_state == ""


def test_the_people_map_comes_from_the_installation(monkeypatch):
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100",
               AGENT_PEOPLE_MAP='{"u-1": 1000000002}',
               AGENT_REVIEWER_LINEAR_USER_ID="u-1")
    assert app.people.chat_of("u-1") == "1000000002"
    assert app.people.is_owner(1000000002)


def test_without_anywhere_to_write_there_is_no_feed(monkeypatch):
    # Ни карты, ни общего чата — писать некуда, и заводить ленту незачем.
    app = _env(monkeypatch)
    assert app.feed is None


def test_a_common_chat_alone_is_enough_for_a_feed(monkeypatch):
    # Площадка из одного человека карту не заполняет, но новости о задачах
    # ей нужны так же.
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100")
    assert app.feed is not None


def test_the_people_map_knows_the_store(monkeypatch):
    # Без хранилища не работает память об авторах приёмных задач, и возврат
    # закрытой задачи автору молча перестаёт находить того, кто её принёс.
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100")
    assert app.people.store is app.store


# ── передача приёма ──────────────────────────────────────────────────────

def _env_reviewer(monkeypatch, **kw):
    for k in ("AGENT_REVIEWER_KEY", "AGENT_REVIEWER_TZ", "AGENT_REVIEWER_NAME"):
        monkeypatch.delenv(k, raising=False)
    return _env(monkeypatch, **kw)


def test_the_reviewer_key_and_zone_come_from_the_installation(monkeypatch):
    app = _env_reviewer(monkeypatch, AGENT_REVIEWER_KEY="agent:reviewer:x",
                        AGENT_REVIEWER_TZ="Asia/Colombo", AGENT_REVIEWER_NAME="Андрей")
    assert app.reviewer.key == "agent:reviewer:x"
    assert app.reviewer.tz_name == "Asia/Colombo"
    assert app.reviewer.owner_name == "Андрей"


def test_an_empty_reviewer_key_is_the_runners_default(monkeypatch):
    # Пустая строка в файле окружения — умолчание, а не ключ с пустым именем:
    # раннер читает ту же переменную так же.
    app = _env_reviewer(monkeypatch, AGENT_REVIEWER_KEY="")
    assert app.reviewer.key == "agent:reviewer"
    assert app.reviewer.tz_name == "Europe/Moscow"


def test_the_feed_and_the_command_share_one_reviewer(monkeypatch):
    # «Кому пишем» и «кого показываем» не должны разойтись.
    app = _env_reviewer(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100")
    assert app.feed.reviewer is app.reviewer
    assert app.channel.reviewer is app.reviewer
    assert app.channel.commands.knows("reviewer")


def test_the_reviewer_knows_the_owner_from_the_people_map(monkeypatch):
    # Одна карта на всё: владелец у команды тот же, что у ленты.
    app = _env_reviewer(monkeypatch, AGENT_PEOPLE_MAP='{"u-1": 1000000002}',
                        AGENT_REVIEWER_LINEAR_USER_ID="u-1")
    assert app.reviewer.people is app.people
    assert app.reviewer.people.is_owner(1000000002)


# ── проверка выката ──────────────────────────────────────────────────────

def test_without_a_deployment_source_the_verification_has_no_deployment(monkeypatch):
    # Вторая инсталляция: адаптера нет, продукт молчит, но не падает.
    monkeypatch.delenv("AGENT_DEPLOYMENT_SOURCE", raising=False)
    app = _env(monkeypatch)
    assert app.verification.deployment is None


class FakeDeployment:
    def landed(self, payload): return None


def test_the_deployment_comes_from_the_installation(monkeypatch):
    app = _env(monkeypatch, AGENT_DEPLOYMENT_SOURCE="agent_factory.test_telegram:FakeDeployment")
    assert isinstance(app.verification.deployment, FakeDeployment)


def test_the_verification_writes_into_the_same_feed_as_the_webhook(monkeypatch):
    # Вердикт дописывается в то же сообщение задачи, которое завела лента.
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100")
    assert app.feed is not None and app.verification.feed is app.feed
    assert app.verification.notify_chat == "-100"


def test_the_startup_line_says_whether_deploys_are_watched():
    import logging

    app = _app(deployment=FakeDeployment())
    said = []
    logging.getLogger("intake").info = lambda m, *a: said.append(m % a)
    app._say_started()
    assert "FakeDeployment" in said[0]


def test_the_front_drains_verdicts_alongside_results():
    # Иначе вердикты копятся в мосте и до чата не доезжают — это и было
    # состояние второй инсталляции до появления `Verification`.
    import asyncio

    app = _app()
    started = []

    async def rec(name):
        started.append(name)

    app.results.run = lambda: rec("results")
    app.verification.run = lambda: rec("verification")

    async def no_poll(): pass
    app.poll = no_poll
    asyncio.run(app.run())
    assert "verification" in started and "results" in started


# ── транспорт обязан отвечать то, что обещает протокол ───────────────────

def _transport_with(answers):
    """Транспорт с подменённым походом в сеть: отдаёт заготовленные ответы."""
    tp = TelegramTransport("т")
    calls = []

    def fake(method, payload):
        calls.append((method, payload))
        return answers.get(method, {})

    tp._call = fake
    return tp, calls


def test_delete_says_whether_it_deleted():
    """Счётчик пачки не правится, а пересоздаётся внизу ленты, и `repaint`
    решает это по ответу `delete`. Молчаливый `None` он читает как «транспорт
    удалять не умеет» — и идёт править ТОЛЬКО ЧТО УДАЛЁННОЕ сообщение: правка
    отвечает 400, функция выходит до отправки, счётчик исчезает. 12.09.2026
    так ломалась любая пачка из двух и более сообщений."""
    tp, calls = _transport_with({"deleteMessage": {"ok": True}})
    assert asyncio.run(tp.delete(1, "10")) is True
    assert calls[0][0] == "deleteMessage"


def test_delete_says_when_it_could_not():
    # Пустой ответ — это отказ сети или «сообщения нет»: правим на месте.
    tp, _ = _transport_with({})
    assert asyncio.run(tp.delete(1, "10")) is False


def test_send_returns_the_message_id_as_a_string():
    tp, _ = _transport_with({"sendMessage": {"result": {"message_id": 77}}})
    assert asyncio.run(tp.send(1, "привет")) == "77"


def test_send_returns_empty_when_the_message_did_not_go():
    tp, _ = _transport_with({})
    assert asyncio.run(tp.send(1, "привет")) == ""


def test_edit_says_whether_it_edited():
    tp, _ = _transport_with({"editMessageText": {"ok": True}})
    assert asyncio.run(tp.edit(1, "10", "новый текст")) is True
    tp, _ = _transport_with({})
    assert asyncio.run(tp.edit(1, "10", "новый текст")) is False


def test_every_transport_answers_in_the_promised_shape():
    """Сторож на весь класс ошибок: фейки в тестах возвращают по протоколу, а
    живой транспорт возвращал `None` — и расхождение заметили только в проде.
    Проверяем оба транспорта одной меркой."""
    from agent_factory.max_messenger import MaxTransport

    tg, _ = _transport_with({"sendMessage": {"result": {"message_id": 1}},
                             "editMessageText": {"ok": True},
                             "deleteMessage": {"ok": True}})
    mx = MaxTransport("т")
    mx._call = lambda *a, **k: {}

    for name, tp in (("telegram", tg), ("max", mx)):
        assert isinstance(asyncio.run(tp.send(1, "x")), str), name
        assert isinstance(asyncio.run(tp.edit(1, "10", "x")), bool), name
        assert isinstance(asyncio.run(tp.delete(1, "10")), bool), name
        assert hasattr(tp, "markup"), name


def test_the_button_does_not_queue_when_the_webhook_will():
    """Иначе задача уезжает в очередь дважды — кнопкой и вебхуком, — раннер
    берёт её два раза, открывает две ветки и дважды отчитывается. 11.09 так
    вышло у первого клиента (TASK-285, TASK-288), 12.09 — на DEV-62."""
    app = _app(webhook_port=8787, webhook_secret="s", agent_user_id="a1",
               notify_chat="-100")
    assert app.webhook is not None
    assert app.channel.cards.enqueue_on_assign is False


def test_without_a_webhook_the_button_is_the_only_way():
    # У площадки без вебхука иначе задачу не поставит никто.
    app = _app(agent_user_id="a1", notify_chat="-100")
    assert app.webhook is None
    assert app.channel.cards.enqueue_on_assign is True


# ── второй путь: событие выката ──────────────────────────────────────────

class SaysLanded:
    def landed(self, payload): return {"identifier": "DEV-1"}


def test_the_deploy_path_rides_the_same_port():
    """Второй порт пришлось бы выставлять прокси и открывать в брандмауэре —
    разговор с админом инсталляции на каждое новое событие."""
    app = _app(webhook_port=8787, webhook_secret="s", agent_user_id="a1",
               notify_chat="-100", deploy_secret="d", deployment=SaysLanded())
    assert app.webhook is not None
    assert set(app.webhook.routes) == {"/linear", "/deploy"}


def test_without_a_way_to_read_the_event_the_path_is_not_raised():
    # Секрет есть, а разбирать событие нечем: открытый путь, который всегда
    # отвечает «не наш выкат», выглядит снаружи работающим.
    app = _app(webhook_port=8787, webhook_secret="s", agent_user_id="a1",
               notify_chat="-100", deploy_secret="d")
    assert set(app.webhook.routes) == {"/linear"}


def test_the_deploy_path_is_a_parameter():
    app = _app(webhook_port=8787, webhook_secret="s", agent_user_id="a1",
               notify_chat="-100", deploy_secret="d", deployment=SaysLanded(),
               deploy_path="/github")
    assert "/github" in app.webhook.routes


def test_every_new_knob_is_actually_read_from_the_environment(monkeypatch):
    """Сторож на класс ошибок: параметр добавлен в конструктор, а в сборку из
    окружения его забыли пробросить. Снаружи это выглядит как «настроил, а не
    работает» — 12.09.2026 так молча потерялись и проект задачи, и секрет
    вебхука выката."""
    app = _env(monkeypatch, AGENT_PROJECT_ID="p-777",
               DEPLOY_WEBHOOK_SECRET="d-secret", DEPLOY_WEBHOOK_PATH="/gh",
               AGENT_NOTIFY_CHAT_ID="-100", AGENT_LINEAR_USER_ID="a1",
               TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s")
    assert app.channel.results.project_id == "p-777"
    assert app.deploy_secret == "d-secret"
    assert app.deploy_path == "/gh"


def test_the_button_asks_the_webhook_itself_not_three_settings():
    """Флаг «вебхук есть» — ФАКТ, а не предсказание по порту, секрету и агенту.
    Условий, при которых вебхук не поднимается, больше трёх и они меняются;
    разойдись предсказание с `_webhook` — и задача назначена, а работать нечем:
    вебхука нет, кнопка выключена, в журнале ни слова.

    Здесь отказ подняться изображён подменой `_webhook` — ровно так выглядит
    ЛЮБОЕ будущее условие, которого в предсказании нет."""
    from agent_factory.telegram import TelegramIntake

    class RefusesToRaiseIt(TelegramIntake):
        def _webhook(self, *a, **kw):
            return None

    app = RefusesToRaiseIt(token="т", redis_url="redis://127.0.0.1:6379/0",
                           queue_key="agent:queue:x", allowed={"1"},
                           webhook_port=8787, webhook_secret="s",
                           agent_user_id="a1", notify_chat="-100")
    assert app.webhook is None
    assert app.channel.cards.enqueue_on_assign is True


def test_two_paths_on_one_port_do_not_start_the_intake():
    """Адрес трекера и адрес выката — независимые ручки. Совпали — приёмник
    трекера затирался деплойным, и все события трекера получали 401 по чужому
    секрету: порт жив, /health отвечает 200, работа не начинается."""
    import pytest as _pytest

    with _pytest.raises(ValueError):
        _app(webhook_port=8787, webhook_secret="s", agent_user_id="a1",
             notify_chat="-100", deploy_secret="d", deployment=SaysLanded(),
             deploy_path="/linear")


def test_a_webhook_that_did_not_come_up_gives_the_button_back():
    """Порт занят (остался прежний процесс, порт держит сосед) — `OSError`
    падал внутри таски, которую никто не ждёт: процесс жив, в журнале «вебхук
    слушает», а постановки задачи нет НИ ОДНОЙ. Снаружи — «назначил и
    ничего»."""
    import asyncio

    sent = []
    app = _app(webhook_port=8787, webhook_secret="s", agent_user_id="a1",
               notify_chat="-100")
    assert app.channel.cards.enqueue_on_assign is False

    async def boom():
        raise OSError("адрес уже используется")

    app.webhook.start = boom

    async def said(chat, text, *a, **k):
        sent.append((chat, text))
        return "1"

    async def go():
        app.transport.send = said
        await app._serve_webhook()

    asyncio.run(go())
    assert app.webhook is None
    assert app.channel.cards.enqueue_on_assign is True
    assert sent and sent[0][0] == "-100" and "кнопкой" in sent[0][1]


def test_a_webhook_that_came_up_keeps_the_button_quiet():
    import asyncio
    import socket

    with socket.socket() as s:                   # свободный порт
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    app = _app(webhook_port=free, webhook_secret="s", agent_user_id="a1",
               notify_chat="-100")
    sent = []

    async def said(*a, **k):
        sent.append(a)
        return "1"

    async def go():
        app.transport.send = said
        await app._serve_webhook()
        app.webhook.srv.close()

    asyncio.run(go())
    assert app.webhook is not None and sent == []
    assert app.channel.cards.enqueue_on_assign is False


def test_the_startup_line_does_not_promise_a_button_that_only_assigns():
    # Иначе строчка запуска врёт ровно в том месте, ради которого её читают.
    import logging

    app = _app(webhook_port=8787, webhook_secret="s", agent_user_id="a1")
    said = []
    logging.getLogger("intake").info = lambda m, *a: said.append(m % a)
    app._say_started()
    assert "кнопка (только назначает)" in said[0]


def test_an_unclaimed_incident_is_watched_by_the_front(monkeypatch):
    """Иначе присмотр есть в продукте и не запущен ни у кого: инцидент лежит
    молча ровно как 12.09.2026 (TASK-302, 50 минут без единого слова)."""
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100",
               AGENT_LINEAR_USER_ID="a1")
    assert app.channel.duty is not None
    # Напоминание уходит в общий чат: инцидент касается всех, а карточка
    # лежит у того одного, кто его прислал.
    assert app.channel.duty.to_chat == "-100"


def test_without_a_tracker_there_is_nobody_to_ask_about_the_assignee(monkeypatch):
    # Площадка без трекера: спросить «взят ли инцидент» не у кого, и
    # напоминать вслепую значит долбить по уже сделанному.
    app = _app(notify_chat="-100")
    assert app.channel.duty is None


def test_the_startup_line_says_incidents_are_watched(monkeypatch):
    import logging

    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100",
               AGENT_LINEAR_USER_ID="a1")
    said = []
    logging.getLogger("intake").info = lambda m, *a: said.append(m % a)
    app._say_started()
    assert "присмотр за инцидентами" in said[0]


def test_the_pull_is_off_until_the_installation_asks_for_it(monkeypatch):
    # Площадка, где работу начинает только человек, остаётся как была.
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1")
    assert app.puller is None


def test_the_pull_limits_come_from_the_installation(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               AGENT_PULL_MAX_IN_FLIGHT="2", AGENT_PULL_MAX_PER_DAY="8")
    assert app.puller is not None
    assert (app.puller.max_in_flight, app.puller.max_per_day) == (2, 8)
    assert app.puller.agent_user_id == "a1" and app.puller.team_id == "t1"


def test_without_a_team_there_is_no_backlog_to_pull_from(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1",
               AGENT_PULL_MAX_PER_DAY="8")
    assert app.puller is None


def test_the_startup_line_names_the_pull_and_its_limits(monkeypatch):
    import logging

    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               AGENT_PULL_MAX_PER_DAY="8", AGENT_NOTIFY_CHAT_ID="-100")
    said = []
    logging.getLogger("intake").info = lambda m, *a: said.append(m % a)
    app._say_started()
    assert "тяга (2 сразу, 8 в сутки)" in said[0]


def test_every_pull_knob_is_read_and_offered_by_the_installer(monkeypatch):
    """Две половины одной ошибки: ручка есть в коде, но её нет в заготовке
    окружения — или наоборот. И то и другое выглядит как «настроил, а не
    работает»."""
    import pathlib

    from agent_factory import telegram as tg

    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               AGENT_PULL_MAX_IN_FLIGHT="3", AGENT_PULL_MAX_PER_DAY="9",
               AGENT_PULL_EVERY_SEC="30", AGENT_PULL_MIN_AGE_SEC="120",
               AGENT_PULL_SKIP_LABELS="человеку, потом")
    p = app.puller
    assert (p.max_in_flight, p.max_per_day) == (3, 9)
    assert (p.interval_sec, p.min_age_sec) == (30, 120)
    assert p.skip_labels == ("человеку", "потом")

    installer = (pathlib.Path(tg.__file__).parent.parent.parent
                 / "install.sh").read_text()
    for knob in ("AGENT_PULL_MAX_PER_DAY", "AGENT_PULL_MAX_IN_FLIGHT",
                 "AGENT_PULL_EVERY_SEC", "AGENT_PULL_MIN_AGE_SEC",
                 "AGENT_PULL_SKIP_LABELS"):
        assert knob in installer, f"{knob} не предложен установщиком"


def test_the_signing_queue_rides_the_webhook_port(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s",
               SIGNING_TOKEN="tok", AGENT_NOTIFY_CHAT_ID="-100")
    assert set(app.webhook.pages) == {"/queue",
                                      "/queue/merge", "/queue/merge/confirm",
                                      "/queue/done", "/queue/done/confirm",
                                      "/queue/update", "/queue/update/confirm",
                                      "/queue/data", "/queue/limits",
                                      "/queue/config", "/queue/runs",
                                      "/queue/mark", "/queue/auth"}
    assert app.webhook.pages["/queue"].team_id == "t1"


def test_without_a_token_the_queue_is_not_served(monkeypatch):
    """Адрес, отдающий постановки и обсуждение любому встречному, хуже
    отсутствия страницы."""
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s")
    assert app.webhook.pages == {}


def test_the_queue_path_and_states_are_parameters(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s",
               SIGNING_TOKEN="tok", SIGNING_PATH="/подпись",
               SIGNING_STATES="На приёмке, In Review")
    assert "/подпись" in app.webhook.pages
    assert app.webhook.pages["/подпись"].states == ("На приёмке", "In Review")


def test_the_page_knows_which_state_means_checking(monkeypatch):
    """Имя состояния проверки доезжает от приёма до страницы. Не доехало бы —
    вкладка «На проверке» осталась бы пустой при полной очереди, и задачи на
    проверке снова пропали бы со страницы (DEV-78)."""
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s",
               SIGNING_TOKEN="tok")
    assert app.webhook.pages["/queue"].checking == ("Verifying",)


def test_the_intake_picks_up_persona_keys_to_watch(monkeypatch):
    """Ключи должны доехать от окружения до приёма, иначе сторож смотрит в
    пустой словарь и молчит — а молчащий сторож хуже отсутствующего: он
    создаёт уверенность, что за личностями следят."""
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               LINEAR_API_KEY_QA="ключ-qa", LINEAR_API_KEY_DUTY="ключ-duty")
    assert app.persona_keys == {"qa": "ключ-qa", "duty": "ключ-duty"}


def test_the_intake_keeps_the_tracker_for_the_watch(monkeypatch):
    """Трекер раскладывался по подобъектам и в `self` не попадал — сторож звал
    несуществующее поле и объявлял мёртвыми всех. Поле должно быть."""
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               LINEAR_API_KEY="общий", LINEAR_API_KEY_QA="k")
    assert app._tracker is not None
    assert hasattr(app._tracker, "whoami")


def test_without_persona_keys_the_watch_is_not_started(monkeypatch):
    """Инсталляция без личностей: сторожу нечего сторожить, и заводить его
    незачем — иначе он ежечасно ходит в трекер ни за чем."""
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1")
    assert app.persona_keys == {}


def test_every_signing_knob_is_offered_by_the_installer():
    import pathlib

    from agent_factory import telegram as tg

    installer = (pathlib.Path(tg.__file__).parent.parent.parent
                 / "install.sh").read_text()
    for knob in ("SIGNING_TOKEN", "SIGNING_PATH", "SIGNING_STATES"):
        assert knob in installer, f"{knob} не предложен установщиком"


def test_the_working_states_come_from_the_installation(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s",
               SIGNING_TOKEN="tok", SIGNING_WORKING_STATES="Делается, Правится")
    page = app.webhook.pages["/queue"]
    assert page.working == ("Делается", "Правится")
    import pathlib

    from agent_factory import telegram as tg
    installer = (pathlib.Path(tg.__file__).parent.parent.parent
                 / "install.sh").read_text()
    assert "SIGNING_WORKING_STATES" in installer


def test_the_pace_comes_from_the_installation(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               AGENT_PULL_MAX_PER_DAY="8", AGENT_PULL_PER_WINDOW="2",
               AGENT_PULL_WINDOW_SEC="7200")
    assert (app.puller.per_window, app.puller.window_sec) == (2, 7200)
    import pathlib

    from agent_factory import telegram as tg
    installer = (pathlib.Path(tg.__file__).parent.parent.parent
                 / "install.sh").read_text()
    for knob in ("AGENT_PULL_PER_WINDOW", "AGENT_PULL_WINDOW_SEC"):
        assert knob in installer, knob


def test_the_queue_page_knows_the_pull_and_the_work_root(monkeypatch):
    """Шапка страницы собирается из двух источников: тяга даёт обратный отсчёт
    и потолки, рабочий каталог — сессии и токены. Не проброшены — страница
    открывается, но молчит о главном."""
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s",
               SIGNING_TOKEN="tok", AGENT_PULL_MAX_PER_DAY="8",
               AGENT_WORK_ROOT="/tmp/работа")
    page = app.webhook.pages["/queue"]
    assert page.puller is app.puller and page.work_root == "/tmp/работа"
    assert "/queue/limits" in app.webhook.pages


def test_who_may_open_the_queue_comes_from_the_installation(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s",
               SIGNING_TOKEN="tok", SIGNING_ALLOWED_USER_IDS="1000000001, 1000000002")
    page = app.webhook.pages["/queue"]
    assert page.allowed_ids == {"1000000001", "1000000002"}
    # Подпись проверяется ключом бота — другого ключа у Telegram нет.
    assert page.bot_token == app.token
    import pathlib

    from agent_factory import telegram as tg
    installer = (pathlib.Path(tg.__file__).parent.parent.parent
                 / "install.sh").read_text()
    assert "SIGNING_ALLOWED_USER_IDS" in installer


# ── уборка закрытых задач в архив ────────────────────────────────────────

def test_the_archiver_is_wired_into_the_feed(monkeypatch):
    """Уборку зовёт лента — там уже разобрано «задача закрылась». Не дойди
    архиватор до ленты, потолок тарифа так и считал бы каждую задачу, а
    заметно это стало бы в день, когда трекер перестал их заводить."""
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100",
               AGENT_QUEUE_KEY="agent:queue:x", AGENT_LINEAR_USER_ID="a1")
    assert app.feed.archiver is app.archiver
    assert app.archiver.after_days == 0
    # Очередь и агент нужны проверке «по задаче идёт сессия»: без них
    # уборка убрала бы задачу из-под работающей сессии.
    assert app.archiver.queue_key == "agent:queue:x"
    assert app.archiver.agent_user_id == "a1"


def test_the_installation_can_put_off_the_archiving(monkeypatch):
    # Там, где доской пользуются живые люди, мгновенная уборка меняет
    # привычную картину — площадка ставит себе отсрочку.
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100",
               AGENT_ARCHIVE_AFTER_DAYS="7")
    assert app.archiver.after_days == 7


def test_a_broken_delay_does_not_break_the_start(monkeypatch):
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100",
               AGENT_ARCHIVE_AFTER_DAYS="неделя")
    assert app.archiver.after_days == 0


def test_the_startup_line_says_what_the_archiving_does(monkeypatch):
    """Уборка молчит, когда не настроена, а замечают это по упёршемуся
    потолку тарифа — то есть в день, когда трекер перестал заводить задачи."""
    import logging

    for env, expect in (({}, "выключен"),
                        ({"AGENT_TEAM_ID": "t1"}, "сразу"),
                        ({"AGENT_TEAM_ID": "t1",
                          "AGENT_ARCHIVE_AFTER_DAYS": "7"}, "через 7 дн.")):
        app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100", **env)
        said = []
        logging.getLogger("intake").info = lambda m, *a: said.append(m % a)
        app._say_started()
        assert "архив → " + expect in said[0], expect


def test_the_pull_runs_on_the_review_cap_alone(monkeypatch):
    """Раньше тяга включалась только дневной квотой. Выключив её в пользу
    потолка на подписи, теряли тягу целиком — вместе с отсчётом и плиткой."""
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               AGENT_PULL_MAX_PER_DAY="0", AGENT_PULL_MAX_OPEN="7")
    assert app.puller is not None
    assert app.puller.max_open == 7 and app.puller.max_per_day == 0


def test_no_caps_at_all_means_no_pull(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1")
    assert app.puller is None


# ── правка сообщения: 400 бывает безобидный, а бывает важный ──────────────


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, description):
        self._body = json.dumps(
            {"ok": False, "error_code": 400,
             "description": description}).encode()
        super().__init__("https://api", 400, "Bad Request", {}, None)

    def read(self):
        return self._body


def transport(monkeypatch, error):
    """Транспорт, у которого любой вызов к Telegram кончается этой ошибкой."""
    t = telegram.TelegramTransport("токен")

    def boom(req, timeout=0):
        raise error

    monkeypatch.setattr(telegram.urllib.request, "urlopen", boom)
    return t


def test_правка_тем_же_текстом_это_успех(monkeypatch, caplog):
    """Telegram отвечает 400 на правку, которая ничего не меняет.

    Это штатная перерисовка карточки, а не отказ: 14.09.2026 таких строк было
    42 за день, и настоящие 400 в них тонули.
    """
    t = transport(monkeypatch, FakeHTTPError(
        "Bad Request: message is not modified"))
    with caplog.at_level(logging.WARNING):
        out = t._call("editMessageText", {"chat_id": 1, "message_id": 2})
    assert out.get("ok") is True
    assert out.get("not_modified") is True
    assert "editMessageText" not in caplog.text


def test_настоящий_отказ_попадает_в_журнал_с_причиной(monkeypatch, caplog):
    """`description` — единственное место, где написано, что именно не так."""
    t = transport(monkeypatch, FakeHTTPError(
        "Bad Request: message to edit not found"))
    with caplog.at_level(logging.WARNING):
        out = t._call("editMessageText", {"chat_id": 1, "message_id": 2})
    assert out == {}
    assert "message to edit not found" in caplog.text


def test_не_http_ошибка_тоже_видна(monkeypatch, caplog):
    t = transport(monkeypatch, urllib.error.URLError("сеть отвалилась"))
    with caplog.at_level(logging.WARNING):
        assert t._call("sendMessage", {}) == {}
    assert "sendMessage" in caplog.text


# ── потолок тяги считает и «не проверено» — кодом, не настройкой ──────────

def test_the_pull_cap_counts_the_unverified_state_without_being_told(monkeypatch):
    """Список SIGNING_STATES, где вторую стопку забыли, копил бы поломки без
    потолка. Состояние «не проверено» добавляет код (DEV-96)."""
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               AGENT_PULL_MAX_OPEN="7")
    assert app.puller.open_states == ("In Review", "Not Verified")


def test_the_pull_cap_takes_the_installations_state_names(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               AGENT_PULL_MAX_OPEN="7", SIGNING_STATES="На приёмке,Не проверено",
               AGENT_UNVERIFIED_STATE_NAME="Не проверено",
               AGENT_VERIFYING_STATE_NAME="Проверяется")
    assert app.puller.open_states == ("На приёмке", "Не проверено")


def test_the_checking_state_is_dropped_from_the_cap_even_if_listed(monkeypatch):
    app = _env(monkeypatch, AGENT_LINEAR_USER_ID="a1", AGENT_TEAM_ID="t1",
               AGENT_PULL_MAX_OPEN="7", SIGNING_STATES="In Review,Verifying")
    assert app.puller.open_states == ("In Review", "Not Verified")


def test_the_merge_closes_tasks_through_the_installations_tracker(monkeypatch):
    # Мерж PR → Done идёт через трекер инсталляции и имя закрытого состояния
    # из окружения. Между настройкой и `Verification` не должно быть места,
    # где они теряются: без трекера мерж только пишет в журнал.
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100", AGENT_DONE_STATE_NAME="Готово")
    assert app.verification.tracker is app._tracker
    assert app.verification.done_state == "Готово"


def test_without_the_setting_the_done_state_is_done(monkeypatch):
    monkeypatch.delenv("AGENT_DONE_STATE_NAME", raising=False)
    app = _env(monkeypatch, AGENT_NOTIFY_CHAT_ID="-100")
    assert app.verification.done_state == "Done"


def test_the_hosting_and_the_actions_reach_the_signing_page(monkeypatch):
    # Кнопка «Смержить» держится на трёх передачах: хостинг из окружения,
    # проверка (Done после мержа), обновление ядра. Потеряй одну — кнопка
    # молча исчезает, и снаружи это «страница читает, как раньше».
    monkeypatch.setenv("AGENT_REPOS_ROOT", "/nowhere")
    app = _env(monkeypatch, TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s",
               AGENT_LINEAR_USER_ID="a1", AGENT_NOTIFY_CHAT_ID="-100",
               SIGNING_TOKEN="tok", AGENT_TEAM_ID="team",
               AGENT_HOSTING_SOURCE="agent_factory.hosting:GitHubHosting",
               AGENT_UNVERIFIED_STATE_NAME="Не проверено")
    page = app.webhook.pages["/queue"]
    assert type(app.hosting).__name__ == "GitHubHosting"
    assert page.hosting is app.hosting
    assert page.verification is app.verification
    assert page.risky == ("Не проверено",)


def test_without_a_hosting_source_the_page_reads_as_before(monkeypatch):
    monkeypatch.delenv("AGENT_HOSTING_SOURCE", raising=False)
    app = _env(monkeypatch, TRACKER_WEBHOOK_PORT="8787", TRACKER_WEBHOOK_SECRET="s",
               AGENT_LINEAR_USER_ID="a1", AGENT_NOTIFY_CHAT_ID="-100",
               SIGNING_TOKEN="tok", AGENT_TEAM_ID="team")
    assert app.hosting is None and app.webhook.pages["/queue"].hosting is None


# ── сверка пропущенных мержей ────────────────────────────────────────────

def test_the_sweep_watches_both_human_stacks(monkeypatch):
    # Стопок у человека две — на подписи и не прошедшие проверку, — и
    # вторая добавляется КОДОМ: список, где её забыли настройкой, копил бы
    # пропущенные мержи молча. То же правило, что у потолка тяги.
    app = _env(monkeypatch, AGENT_TEAM_ID="t-1", SIGNING_STATES="In Review",
               AGENT_UNVERIFIED_STATE_NAME="Not Verified",
               AGENT_VERIFYING_STATE_NAME="Verifying")
    assert app.reconciler.states == ("In Review", "Not Verified")


def test_the_state_under_check_is_never_swept(monkeypatch):
    # «Проверка идёт» вычитается, ДАЖЕ ЕСЛИ инсталляция вписала её в
    # SIGNING_STATES (страница очереди показывает то, что там перечислено).
    # Задача, которую проверяют прямо сейчас, ещё не в стопке человека:
    # закрыв её, сверка отняла бы у проверки предмет. То же правило, что у
    # потолка тяги (`puller.NEVER_COUNT`).
    app = _env(monkeypatch, AGENT_TEAM_ID="t-1",
               SIGNING_STATES="In Review,Verifying",
               AGENT_VERIFYING_STATE_NAME="Verifying")
    assert app.reconciler.states == ("In Review", "Not Verified")


def test_the_sweep_goes_the_same_road_as_the_event(monkeypatch):
    # Не вторая дорога в Done, а та же: иначе у мержа появятся две записи,
    # которые разойдутся в первую же неделю.
    app = _env(monkeypatch, AGENT_TEAM_ID="t-1")
    assert app.reconciler.verification is app.verification
    assert app.reconciler.repos is app.verification.repos
    assert app.reconciler.hosting is app.hosting


def test_without_a_team_the_sweep_stays_quiet(monkeypatch):
    # Вебхук подписывают на весь воркспейс, и сверка без команды ходила бы
    # по чужим доскам — то же правило, что у уборки архива.
    app = _env(monkeypatch, AGENT_TEAM_ID="")
    assert app.reconciler.able()


# ── сверка «в работе» ─────────────────────────────────────────────────────

def _stalled_env(monkeypatch, **kw):
    return _env(monkeypatch, **{"AGENT_TEAM_ID": "team-1",
                                "AGENT_LINEAR_USER_ID": "a1",
                                "AGENT_WORK_ROOT": "/tmp",
                                "SIGNING_WORKING_STATES": "In Progress",
                                "SIGNING_STATES": "In Review", **kw})


def test_the_stalled_reconciler_is_assembled_and_able(monkeypatch):
    """Собранная, но неспособная сверка молчит и ничего не делает — значит
    сборку надо проверять прямо: состояния работы те же, что у страницы
    очереди, а стопка и «Todo» названы обе."""
    app = _stalled_env(monkeypatch)
    assert app.stalled.able() == ""
    assert app.stalled.states == ("In Progress",)
    assert app.stalled.review_state == "In Review"
    assert app.stalled.todo_state == "Todo"
    assert app.stalled.agent_user_id == "a1"


def test_a_board_without_work_states_turns_the_stalled_check_off(monkeypatch):
    """Площадка, у которой состояния работы не заданы, не должна получить
    сверку, решающую по пустому списку."""
    app = _stalled_env(monkeypatch, SIGNING_WORKING_STATES="")
    assert "состояния работы" in app.stalled.able()


def test_the_queue_is_asked_in_all_three_places(monkeypatch):
    """Задача без сессии законна в трёх случаях, и все три лежат в redis:
    ждёт слот, отложена по лимиту Claude, ждёт проверок. Пропустить любой —
    отобрать задачу у собственного ожидания."""
    app = _stalled_env(monkeypatch)

    class Client:
        async def lrange(self, key, a, b):
            assert key == "agent:queue:x"
            return [json.dumps({"identifier": "DEV-1"}), "не json"]

        async def zrange(self, key, a, b):
            return {"agent:deferred": [json.dumps({"identifier": "DEV-2"}).encode()],
                    "agent:ci-wait": ["DEV-3|поколение"]}.get(key, [])

    app.store.r = Client()
    assert asyncio.run(app._queued_idents()) == {"DEV-1", "DEV-2", "DEV-3"}


def test_a_store_that_is_not_redis_is_not_an_empty_queue(monkeypatch):
    """Пустое множество тут читалось бы как «в очереди никого» и развязывало
    бы сверке руки. Не спросили — круг пропускается целиком."""
    import pytest as _pytest

    app = _stalled_env(monkeypatch)
    app.store.r = None
    with _pytest.raises(RuntimeError):
        asyncio.run(app._queued_idents())


def test_every_action_tail_the_page_builds_is_also_served():
    """Хвосты действий, которые страница СТРОИТ, сервер обязан ПРИНИМАТЬ.

    16.09.2026: кнопка «Смержить» вела на `/queue/merge`, а сервер знал только
    `/data`, `/limits`, `/config`, `/runs`, `/mark`, `/auth`. Нажатие уходило в
    незарегистрированный путь и отвечало «нет такой страницы»; со стороны это
    выглядело как «кнопка не работает». Два списка разъехались молча — здесь
    они сверяются по исходникам, без поднятия сервера.
    """
    import pathlib, re
    рядом = pathlib.Path(__file__).resolve().parent
    страница = (рядом / "signing.py").read_text(encoding="utf-8")
    сервер = (рядом / "telegram.py").read_text(encoding="utf-8")

    кусок = страница.split("def base_of", 1)[1].split("def ", 1)[0]
    строит = set(re.findall(r'"(/[a-z/]+)"', кусок))
    assert "/merge" in строит, "образец сломан: страница больше не строит /merge"

    регистрация = сервер.split("for tail in (", 1)[1].split(")", 1)[0]
    принимает = set(re.findall(r'"(/[a-z/]+)"', регистрация))

    потеряны = строит - принимает
    assert not потеряны, f"страница строит, сервер не принимает: {sorted(потеряны)}"


# ── 429: телеграм говорит, сколько ждать ─────────────────────────────────


class FakeTooMany(urllib.error.HTTPError):
    """Ответ телеграма на «слишком часто». Срок — в теле, `parameters`."""

    def __init__(self, retry_after=420, headers=None):
        body = {"ok": False, "error_code": 429,
                "description": "Too Many Requests: retry after 420"}
        if retry_after is not None:
            body["parameters"] = {"retry_after": retry_after}
        self._body = json.dumps(body).encode()
        super().__init__("https://api", 429, "Too Many Requests",
                         headers or {}, None)

    def read(self):
        return self._body


def test_телеграм_сказал_сколько_ждать_и_это_прочитано(monkeypatch, caplog):
    """17.09.2026 бота забанили дважды за сутки: 1101 вызов, 1103 отказа.
    Срок ожидания лежал в теле ответа, и его никто не читал."""
    t = transport(monkeypatch, FakeTooMany(420))
    with caplog.at_level(logging.WARNING):
        assert t._call("editMessageText", {"chat_id": 1, "message_id": 2}) == {}
    assert 419 < t.retry_after() <= 420
    assert "слишком часто" in caplog.text


def test_пока_дверь_закрыта_в_сеть_не_ходим(monkeypatch, caplog):
    """Попытка внутри паузы её же и продлевает: телеграм считает вызовы, а
    не удачи."""
    calls = []

    def boom(req, timeout=0):
        calls.append(req)
        raise FakeTooMany(420)

    t = telegram.TelegramTransport("токен")
    monkeypatch.setattr(telegram.urllib.request, "urlopen", boom)
    t._call("editMessageText", {})
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            assert t._call("editMessageText", {}) == {}
    assert len(calls) == 1, f"в закрытую дверь постучались {len(calls)} раз"
    assert "ждём ещё" in caplog.text


def test_отказ_без_срока_всё_равно_даёт_паузу(monkeypatch):
    """Ноль здесь значил бы «паузы не было», то есть ту же долбёжку."""
    t = transport(monkeypatch, FakeTooMany(retry_after=None))
    t._call("editMessageText", {})
    assert t.retry_after() > 0


def test_срок_берётся_и_из_заголовка(monkeypatch):
    """`Retry-After` может поставить прокси перед телеграмом."""
    t = transport(monkeypatch,
                  FakeTooMany(retry_after=None, headers={"Retry-After": "60"}))
    t._call("editMessageText", {})
    assert 59 < t.retry_after() <= 60


def test_обычный_400_паузы_не_ставит(monkeypatch):
    """Удалённое сообщение — не бан: остальные правки обязаны пройти."""
    t = transport(monkeypatch, FakeHTTPError("Bad Request: message to edit not found"))
    t._call("editMessageText", {})
    assert t.retry_after() == 0
