"""Контракт моста вердиктов между раннером и приёмом.

Раннер (`runner.py`) и приём (`agent_factory.deploy`) деплоятся раздельно
и импортировать друг друга не могут. Ключ списка, форма записи и правило
«тревожный вердикт раннер шлёт сам» держатся на совпадении — этот файл его
закрепляет, как `test_reviewer.py` закрепляет запись о принимающем.
"""
import json
from pathlib import Path

import pytest

import runner
from agent_factory import deploy


def test_both_sides_read_the_same_key():
    assert deploy.VERIFY_RESULTS_KEY == runner.VERIFY_RESULTS_KEY


@pytest.fixture
def bridge(monkeypatch):
    pushed, notes = [], []

    class FakeRedis:
        def lpush(self, key, value):
            pushed.append((key, json.loads(value))); return 1

    monkeypatch.setattr(runner.redis, "from_url", lambda url: FakeRedis())
    monkeypatch.setattr(runner, "notify", lambda text, chat_id="": notes.append(text))
    return pushed, notes


TASK = {"kind": "verify", "issue_id": "issue-uuid-1", "identifier": "TASK-200",
        "url": "https://linear.app/x/TASK-200"}


def test_the_record_carries_what_the_intake_reads(bridge):
    pushed, _ = bridge
    runner.report_verdict(TASK, "OK md5 сошёлся", Path("/tmp/x.log"))
    ((_, rec),) = pushed
    assert {"issue_id", "identifier", "icon", "verdict"} <= set(rec)
    assert deploy.Verification.mark_of(rec) == f"{rec['icon']} {rec['verdict']}"


def test_the_alarm_icon_is_the_one_the_runner_shouts_itself(bridge):
    # Приём на этот значок молчит, если сообщения задачи нет: раннер уже
    # отправил вердикт лично. Разойдись значки — тревога или дублировалась
    # бы, или терялась.
    pushed, notes = bridge
    runner.report_verdict(TASK, "FAIL md5 разошёлся", Path("/tmp/x.log"))
    ((_, rec),) = pushed
    assert rec["icon"] == deploy.LOUD_ICON and notes


def test_a_calm_verdict_is_not_shouted_by_the_runner(bridge):
    # Обратная сторона того же контракта: спокойный вердикт раннер сам не
    # шлёт, и показать его — дело приёма.
    pushed, notes = bridge
    runner.report_verdict(TASK, "OK всё сошлось", Path("/tmp/x.log"))
    assert pushed and notes == []


def test_the_record_carries_the_commit_for_the_gate(bridge):
    """Ворота перед следующей постановкой отсекают повтор того же коммита
    и называют прежний вердикт — оба факта едут по мосту от раннера."""
    pushed, _ = bridge
    runner.report_verdict({**TASK, "commit_sha": "abc123"}, "OK всё сошлось", Path("/tmp/x.log"))
    ((_, rec),) = pushed
    assert rec["commit_sha"] == "abc123" and rec["line"] == "OK всё сошлось"


# ── выключатели точек проверки (DEV-97) ──────────────────────────────────────
#
# Ставит проверку приём, раскладку на старте объявляет раннер — из одного
# файла окружения. Разойдись имена переменных или разбор значения, раннер
# говорил бы «после мержа выключена», а приём ставил бы сессии.

def test_both_sides_name_the_same_switches():
    assert deploy.VERIFY_BEFORE_MERGE_VAR == runner.VERIFY_BEFORE_MERGE_VAR
    assert deploy.VERIFY_AFTER_MERGE_VAR == runner.VERIFY_AFTER_MERGE_VAR
    assert deploy.SWITCH_OFF == runner.SWITCH_OFF


@pytest.mark.parametrize("raw", ["1", "0", "", None, " 0 ", "false", "off", "yes", "2"])
def test_both_sides_read_a_switch_the_same_way(raw):
    assert deploy.switch_on(raw) == runner._switch_on(raw)


# ── класс расхождения: одно слово у раннера и у ворот (DEV-89) ──────────────

from agent_factory import runs  # noqa: E402
from agent_factory.gitref import Located  # noqa: E402


def test_both_sides_name_the_blocking_class_the_same():
    assert deploy.CLASS_UNOBSERVABLE == runner.CLASS_UNOBSERVABLE
    assert deploy.CLASS_UNOBSERVABLE in runner.VERDICT_CLASSES


def test_the_class_the_runner_writes_is_the_one_the_gate_reads(tmp_path):
    """Раннер пишет `класс`/`режим` в stats.json, ворота читают их через
    `runs.last_verify` — поля и слова обязаны совпасть, иначе правило
    «повторное unobservable не ставится» молча не работает."""
    d = tmp_path / "DEV-1-verify-20260915-100000"
    d.mkdir()
    (d / "transcript.jsonl").write_text(json.dumps({"type": "result"}))
    (d / "verdict.txt").write_text("PARTIAL выката не было\n")
    runner._write_stats(d, slot=1, ident="DEV-1", kind="verify", took=1, peak_mb=1, rc=0,
                        outcome="READY_FOR_HUMAN",
                        discrepancy=runner.verdict_class("PARTIAL выката не было"),
                        mode="branch")
    got = runs.last_verify(str(tmp_path), "DEV-1")
    assert got["класс"] == "unobservable" and got["режим"] == "branch"
    gate = deploy.decide(entry="state", hint="branch", ident="DEV-1", ref="agent/DEV-1",
                         exists=True, seen=None,
                         located=Located(repo="/r", ref="agent/DEV-1", sha="a" * 40,
                                         ancestor=False, when=1),
                         prior_class=got["класс"], prior_mode=got["режим"],
                         prior_line=got["вердикт"])
    assert isinstance(gate, deploy.Skip) and "unobservable" in gate.reason


# ── закрытое состояние: одно имя у раннера и у приёма ───────────────────────
#
# Раннер закрывает задачу маршрутом по вердикту после мержа, приём — по мержу
# PR в основную ветку. Разойдись переменная или умолчание, один закрывал бы в
# «Done», другой искал бы «Готово» и писал в журнал «статуса нет».

def test_both_sides_name_the_same_done_state():
    assert deploy.DONE_STATE_VAR == runner.DONE_STATE_VAR
    assert deploy.DEFAULT_DONE_STATE == runner.DEFAULT_DONE_STATE


# ── точка проверки в подписи: одни и те же режимы у всех троих (DEV-115) ────
#
# Подпись «выкат проверен» — утверждение о том, что работа доехала до прода.
# Право его сказать даёт РЕЖИМ, который выбрали ворота приёма; раннер пишет
# подпись в чат, учёт — в карточку задачи и на страницу сессий. Разойдись
# разбор режима у любого из троих — и один назвал бы выкатом то, что другой
# считает веткой, молча.

def test_all_three_split_the_modes_into_the_same_two_points():
    """Слова режима у раннера — те же, по которым ворота считают точку."""
    assert deploy.point_of(runner.MODE_BRANCH) == deploy.POINT_BEFORE
    for mode in runner.MODES_AFTER_MERGE:
        assert deploy.point_of(mode) == deploy.POINT_AFTER
    # Все режимы, которые ворота вообще выдают (`mode_for`), у раннера
    # разобраны: незнакомый уехал бы в «точка неизвестна» и подписался бы
    # нейтрально там, где точка на самом деле известна.
    produced = {deploy.mode_for(entry=e, hint=h, ancestor=a)
                for e in ("state", "event")
                for h in ("", "branch", "build", "deploy")
                for a in (True, False, None)}
    assert produced <= {runner.MODE_BRANCH, *runner.MODES_AFTER_MERGE}


def test_the_chat_line_and_the_ledger_card_agree_on_what_was_checked():
    """Одна проверка — одно слово в чате и в карточке учёта."""
    for mode in (runner.MODE_BRANCH, *runner.MODES_AFTER_MERGE):
        assert runner._verdict_label("OK всё сошлось", mode) == \
            runs.outcome_word("VERIFIED", mode)
    # И там, и там точка бывает неизвестной — оба молчат про выкат.
    assert "выкат" not in runner._verdict_label("OK", "")
    assert "выкат" not in runs.outcome_word("VERIFIED", "")
