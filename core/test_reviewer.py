"""Кто принимает работу агента: делегирование поверх переменных окружения.

Смысл тестов в том, что раннер никогда не остаётся без принимающего. Любая
осечка — пустой ключ, битая запись, вышедший срок, лежачий redis — обязана
кончаться владельцем, а не «задача висит ни на ком».

Redis подменён: это тонкая обёртка над сетью, а разбор записи — чистая
функция, и проверяется он без неё.
"""
from datetime import datetime, timedelta, timezone

import pytest

import runner

OWNER = {"id": "owner-uuid", "name": "Андрей", "telegram_id": "111"}
DELEGATE = "pavel-uuid"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class FakeRedis:
    def __init__(self, value=None, broken=False):
        self.value = value
        self.broken = broken
        self.deleted = 0

    def get(self, key):
        if self.broken:
            raise RuntimeError("redis лежит")
        return self.value

    def delete(self, key):
        self.deleted += 1
        self.value = None


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(runner, "AGENT_OWNER_ID", OWNER["id"])
    monkeypatch.setattr(runner, "AGENT_OWNER_NAME", OWNER["name"])
    monkeypatch.setattr(runner, "AGENT_OWNER_TELEGRAM_ID", OWNER["telegram_id"])


def record(**over):
    rec = {"linear_user_id": DELEGATE, "name": "Иван", "telegram_id": "222",
           "until": ""}
    rec.update(over)
    import json
    return json.dumps(rec, ensure_ascii=False)


# ── разбор записи ──────────────────────────────────────────────────────────

def test_indefinite_delegation_is_active():
    rev = runner.reviewer_from_record(record(), NOW)
    assert rev["id"] == DELEGATE
    assert rev["delegated"] is True


def test_deadline_in_the_future_is_active():
    until = (NOW + timedelta(hours=2)).isoformat()
    assert runner.reviewer_from_record(record(until=until), NOW)["id"] == DELEGATE


def test_deadline_in_the_past_returns_none():
    # Срок проверяется НА ЧТЕНИИ — в этом весь смысл «само вернётся к
    # владельцу»: планировщика нет, и в момент истечения может не быть даже
    # живого процесса.
    until = (NOW - timedelta(minutes=1)).isoformat()
    assert runner.reviewer_from_record(record(until=until), NOW) is None


def test_deadline_exactly_now_is_over():
    assert runner.reviewer_from_record(record(until=NOW.isoformat()), NOW) is None


def test_naive_deadline_is_read_as_utc():
    # events-api пишет срок с зоной, но запись мог положить и человек руками:
    # naive-время без зоны не должно ни падать, ни трактоваться как локальное.
    until = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert runner.reviewer_from_record(record(until=until), NOW)["id"] == DELEGATE


def test_broken_json_is_not_a_reviewer():
    assert runner.reviewer_from_record("{не json", NOW) is None


def test_record_without_user_id_is_not_a_reviewer():
    assert runner.reviewer_from_record('{"name": "Иван"}', NOW) is None


def test_unparsable_deadline_falls_back_to_owner():
    assert runner.reviewer_from_record(record(until="когда-нибудь"), NOW) is None


def test_empty_value_is_not_a_reviewer():
    assert runner.reviewer_from_record("", NOW) is None


# ── current_reviewer ───────────────────────────────────────────────────────

def test_no_record_means_owner(owner, monkeypatch):
    monkeypatch.setattr(runner, "_redis_conn", lambda: FakeRedis(None))
    rev = runner.current_reviewer()
    assert (rev["id"], rev["name"], rev["delegated"]) == (OWNER["id"], "Андрей", False)


def test_record_means_delegate(owner, monkeypatch):
    monkeypatch.setattr(runner, "_redis_conn", lambda: FakeRedis(record()))
    rev = runner.current_reviewer()
    assert (rev["id"], rev["name"], rev["telegram_id"]) == (DELEGATE, "Иван", "222")


def test_expired_record_is_removed_and_owner_returned(owner, monkeypatch):
    fake = FakeRedis(record(until=(datetime.now(timezone.utc)
                                   - timedelta(hours=1)).isoformat()))
    monkeypatch.setattr(runner, "_redis_conn", lambda: fake)
    assert runner.current_reviewer()["id"] == OWNER["id"]
    # Убираем протухшее, иначе `/reviewer` в боте показывал бы делегата, к
    # которому работа уже не едет.
    assert fake.deleted == 1


def test_dead_redis_means_owner_not_a_crash(owner, monkeypatch):
    monkeypatch.setattr(runner, "_redis_conn", lambda: FakeRedis(broken=True))
    assert runner.current_reviewer()["id"] == OWNER["id"]


# ── куда уходит задача ─────────────────────────────────────────────────────

def test_assign_goes_to_the_delegate(owner, monkeypatch):
    monkeypatch.setattr(runner, "_redis_conn", lambda: FakeRedis(record()))
    calls = []
    monkeypatch.setattr(runner._tracker, "assign",
                        lambda task_id, user_id: calls.append((task_id, user_id)))
    runner.linear_assign_to_reviewer("issue-1")
    assert calls == [("issue-1", DELEGATE)]


def test_assign_uses_the_reviewer_passed_in(owner, monkeypatch):
    # Сессия идёт час: делегирование могли снять у неё под руками. Промпт уже
    # назвал человека — задача обязана уехать тому же.
    monkeypatch.setattr(runner, "_redis_conn", lambda: FakeRedis(None))
    calls = []
    monkeypatch.setattr(runner._tracker, "assign",
                        lambda task_id, user_id: calls.append((task_id, user_id)))
    runner.linear_assign_to_reviewer("issue-1", {"id": DELEGATE, "name": "Иван",
                                                 "telegram_id": "", "until": "",
                                                 "delegated": True})
    assert calls == [("issue-1", DELEGATE)]


def test_assign_without_anyone_leaves_a_warning_not_a_write(monkeypatch):
    monkeypatch.setattr(runner, "AGENT_OWNER_ID", "")
    monkeypatch.setattr(runner, "_redis_conn", lambda: FakeRedis(None))
    calls = []
    monkeypatch.setattr(runner._tracker, "assign",
                        lambda task_id, user_id: calls.append((task_id, user_id)))
    runner.linear_assign_to_reviewer("issue-1")
    assert calls == []


# ── тревожный вердикт зовёт человека лично, и ровно один раз ───────────────

def test_fail_verdict_is_sent_once_not_twice(owner, monkeypatch, tmp_path):
    # Раньше тревожный вердикт уходил дважды: строкой в общий чат и отдельным
    # сообщением в личку. Теперь notify по умолчанию адресован владельцу, и
    # второй раз тому же человеку слать нечего.
    monkeypatch.setattr(runner, "_redis_conn", lambda: FakeRedis(record()))
    sent = []
    monkeypatch.setattr(runner, "notify",
                        lambda text, chat_id="": sent.append((chat_id, text)))

    class Boom:
        def lpush(self, *a):
            raise RuntimeError("нет моста")

    monkeypatch.setattr(runner.redis, "from_url", lambda *a, **k: Boom())
    runner.report_verdict({"issue_id": "i-1", "identifier": "TASK-1", "url": "u"},
                          "FAIL: контейнер не поднялся", tmp_path / "s.log")
    assert len(sent) == 1, "вердикт продублировался"
    assert "контейнер не поднялся" in sent[0][1]


def test_ok_verdict_does_not_ping_anyone_personally(owner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_redis_conn", lambda: FakeRedis(record()))
    sent = []
    monkeypatch.setattr(runner, "notify",
                        lambda text, chat_id="": sent.append((chat_id, text)))

    class Ok:
        def lpush(self, *a):
            return 1

    monkeypatch.setattr(runner.redis, "from_url", lambda *a, **k: Ok())
    runner.report_verdict({"issue_id": "i-1", "identifier": "TASK-1", "url": "u"},
                          "OK: выкат подтверждён", tmp_path / "s.log")
    assert sent == []


# ── ошибки уходят владельцу в личку, а не в общий чат ──────────────────────

def _capture_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(runner, "_send_telegram",
                        lambda chat, text: sent.append((chat, text)) or True)
    return sent


def test_errors_go_to_the_owner_dm_not_the_group(monkeypatch):
    # Андрей 01.09.2026: «владелец процесса я, я отвечаю за результат, и эти
    # сообщения нужны именно мне». В общем чате остаётся состояние задачи.
    monkeypatch.setattr(runner, "AGENT_OWNER_TELEGRAM_ID", "1000000001")
    monkeypatch.setattr(runner, "TELEGRAM_CHAT_ID", "-100500")
    sent = _capture_sends(monkeypatch)
    runner.notify("⚠️ TASK-1: сессия упала")
    assert [c for c, _ in sent] == ["1000000001"], "ушло не в личку владельца"


def test_group_is_the_fallback_when_the_dm_refuses(monkeypatch):
    # Диалог с ботом не начат или бот заблокирован. Молчание хуже шума:
    # непрочитанное падение сессии — потерянная задача.
    monkeypatch.setattr(runner, "AGENT_OWNER_TELEGRAM_ID", "1000000001")
    monkeypatch.setattr(runner, "TELEGRAM_CHAT_ID", "-100500")
    tried = []
    monkeypatch.setattr(runner, "_send_telegram",
                        lambda chat, text: tried.append(chat) or chat == "-100500")
    runner.notify("⚠️ TASK-1: сессия упала")
    assert tried == ["1000000001", "-100500"], "не откатились в общий чат"


def test_group_is_used_when_no_owner_dm_configured(monkeypatch):
    monkeypatch.setattr(runner, "AGENT_OWNER_TELEGRAM_ID", "")
    monkeypatch.setattr(runner, "TELEGRAM_CHAT_ID", "-100500")
    sent = _capture_sends(monkeypatch)
    runner.notify("⚠️ TASK-1: сессия упала")
    assert [c for c, _ in sent] == ["-100500"]


def test_explicit_chat_id_is_not_second_guessed(monkeypatch):
    # Адресата выбрал вызывающий — подменять его общим чатом нельзя.
    monkeypatch.setattr(runner, "AGENT_OWNER_TELEGRAM_ID", "1000000001")
    monkeypatch.setattr(runner, "TELEGRAM_CHAT_ID", "-100500")
    tried = []
    monkeypatch.setattr(runner, "_send_telegram",
                        lambda chat, text: tried.append(chat) or False)
    runner.notify("текст", chat_id="777")
    assert tried == ["777"], "отправка ушла мимо явного адресата"


# ── контракт умеет оба хостинга (DEV-42, 09.09.2026) ──────────────────────
#
# Код Дока лежит на GitHub, а фабрика писалась под GitLab. Без этого демо на
# собственном продукте не замыкается: единственный проект, который владельцу
# разрешено показывать, оказался тем, с которым система не работает.

def _verify_contract():
    from prompt import build_verify_prompt
    from pathlib import Path
    return build_verify_prompt(
        {"identifier": "DEV-1", "mr_iid": "42", "project_path": "org/repo",
         "commit_sha": "abc1234", "ref": "main", "pipeline_id": "999",
         "deploy_jobs": ["deploy"]},
        Path("/tmp/w"), Path("/tmp/r"), "Андрей")


def test_verify_contract_knows_both_hostings():
    """Проверяльщик обязан уметь и MR, и PR: репозитории бывают и там, и там."""
    c = _verify_contract()
    assert "glab mr view" in c, "GitLab не должен пропасть"
    assert "gh pr view" in c, "GitHub обязан появиться"


def test_verify_contract_does_not_assume_gitlab():
    """Прямое указание проверить хостинг, а не считать GitLab по умолчанию —
    ровно та ошибка, на которой демо на Доке не замкнулось бы."""
    c = _verify_contract()
    assert "remote get-url origin" in c


def test_github_rerun_only_failed_jobs():
    """У Actions нет отдельной джобы в смысле GitLab: без --failed
    перезапустится весь прогон вместе с зелёными шагами, и «стало зелёным»
    перестанет что-либо значить."""
    c = _verify_contract()
    # Проверяем САМУ КОМАНДУ, а не упоминание флага в пояснении рядом:
    # мутация «убрать --failed из команды» иначе проходит незамеченной.
    rerun = [l for l in c.splitlines() if "gh run rerun" in l]
    assert rerun, "команды перезапуска нет"
    assert all("--failed" in l for l in rerun), rerun
    assert "gh run watch" in c, "чем-то надо дожидаться конца"


# ── контракт с приёмом ────────────────────────────────────────────────────
#
# Запись пишет приём (agent_factory/reviewer.py), читает раннер. Пакеты
# деплоятся раздельно и импортировать друг друга не могут, поэтому разбор
# продублирован — и совпадение ключа, полей и правила «срок вышел — записи
# нет» держится только этими тестами.

def test_the_intake_writes_under_the_key_the_runner_reads():
    import os

    from agent_factory import reviewer as intake_side
    assert runner.REVIEWER_KEY == os.environ.get("AGENT_REVIEWER_KEY",
                                                 intake_side.DEFAULT_KEY)


def test_a_handover_written_by_the_intake_is_read_by_the_runner():
    import asyncio

    from agent_factory.people import People
    from agent_factory.reviewer import Reviewer, record_active

    class Store:
        def __init__(self): self.kv = {}
        async def get(self, key): return self.kv.get(key)
        async def put(self, key, value, ttl): self.kv[key] = value
        async def drop(self, key): self.kv.pop(key, None)

    st = Store()
    rev = Reviewer(st, People({OWNER["id"]: OWNER["telegram_id"]},
                              owner_user_id=OWNER["id"]),
                   key=runner.REVIEWER_KEY, now=lambda: NOW)
    asyncio.run(rev.handover(DELEGATE, "Иван", "222", NOW + timedelta(hours=1)))
    raw = st.kv[runner.REVIEWER_KEY]

    got = runner.reviewer_from_record(raw, NOW)
    assert got["id"] == DELEGATE and got["telegram_id"] == "222"
    assert got["delegated"] is True
    # Обе стороны считают срок одинаково: вышел — принимает владелец.
    later = NOW + timedelta(hours=2)
    assert runner.reviewer_from_record(raw, later) is None
    assert record_active(raw, later) is None
