"""Журнал ворот проверки (gatelog.py, DEV-88).

Главное, что здесь держится: у ворот три состояния, а не два. «Ошибка
обвязки» никогда не читается как «прошло» и никогда не сливается с «нет
свидетельств» — иначе сломанная обвязка неотличима от сессии, которая ничего
не запускала, а это задокументированный источник ложных пропусков.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import gatelog
from gatelog import ERROR, FAILED, NO_EVIDENCE, PASSED, Gate

PY = sys.executable
GATELOG = str(Path(gatelog.__file__).resolve())


# ── код возврата → исход ──────────────────────────────────────────────────

@pytest.mark.parametrize("rc, status", [(0, PASSED), (10, FAILED), (2, ERROR), (1, ERROR)])
def test_logdiff_codes_are_read_by_their_meaning_not_by_zero(rc, status):
    # 10 у logdiff — находка, 2 — журнал не прочитан. Незнакомый код известного
    # инструмента — не провал, а «выполнилось не то».
    assert gatelog.status_for("logdiff", rc) == status


def test_an_unknown_tool_is_judged_by_zero_only():
    assert gatelog.status_for("pytest", 0) == PASSED
    assert gatelog.status_for("pytest", 1) == FAILED


def test_a_command_that_never_finished_is_a_harness_error():
    assert gatelog.status_for("pytest", None) == ERROR
    assert gatelog.status_for("logdiff", None) == ERROR


# ── журнал: запись и чтение ───────────────────────────────────────────────

def test_records_append_and_read_back_in_order(tmp_path):
    path = tmp_path / "gates.json"
    gatelog.record(path, Gate(name="health", status=PASSED, command="curl", rc=0))
    gatelog.record(path, Gate(name="logdiff", status=FAILED, command="logdiff", rc=10))
    got = gatelog.read(path)
    assert [(g.name, g.status, g.rc) for g in got] == [("health", PASSED, 0),
                                                        ("logdiff", FAILED, 10)]
    # Файл — обычный JSON-список, читается и без модуля.
    assert [r["ворота"] for r in json.loads(path.read_text())] == ["health", "logdiff"]


def test_a_missing_file_is_no_evidence(tmp_path):
    assert gatelog.read(tmp_path / "gates.json") == []
    assert gatelog.judge([])[0] == NO_EVIDENCE


def test_a_corrupt_file_is_a_harness_error_not_an_empty_journal(tmp_path):
    """Битый файл — одна запись «ошибка обвязки». Прочитать как пустой значило
    бы, что снятая посреди записи сессия выглядит как «ничего не запускала»."""
    path = tmp_path / "gates.json"
    path.write_text('[{"ворота": "health", "итог": "прошло"}, {"вор')
    (gate,) = gatelog.read(path)
    assert gate.status == ERROR and "не разобран" in gate.note
    assert gatelog.judge([gate])[0] == ERROR


def test_a_file_that_is_not_a_list_is_a_harness_error(tmp_path):
    path = tmp_path / "gates.json"
    path.write_text('{"ворота": "health"}')
    (gate,) = gatelog.read(path)
    assert gate.status == ERROR


def test_an_unknown_status_in_a_record_is_not_a_pass(tmp_path):
    path = tmp_path / "gates.json"
    path.write_text(json.dumps([{"ворота": "health", "итог": "ok"}]))
    (gate,) = gatelog.read(path)
    assert gate.status == ERROR


def test_recording_into_a_corrupt_journal_refuses_loudly(tmp_path):
    path = tmp_path / "gates.json"
    path.write_text("{{{")
    with pytest.raises(OSError):
        gatelog.record(path, Gate(name="x", status=PASSED))
    # Битый файл не затёрт: чтение по-прежнему отдаёт ошибку обвязки.
    assert gatelog.read(path)[0].status == ERROR


# ── суд ───────────────────────────────────────────────────────────────────

def test_one_failed_gate_outweighs_any_number_of_passed():
    passed = [Gate(name=f"g{i}", status=PASSED) for i in range(5)]
    state, why = gatelog.judge(passed + [Gate(name="logdiff", status=FAILED, rc=10)])
    assert state == FAILED and "logdiff (код 10)" in why


def test_a_harness_error_is_told_apart_from_failure_and_from_silence():
    broken = Gate(name="logdiff", status=ERROR, rc=2, note="журнал не прочитан")
    assert gatelog.judge([broken])[0] == ERROR
    assert gatelog.judge([Gate(name="h", status=PASSED), broken])[0] == ERROR
    assert {gatelog.judge([broken])[0], gatelog.judge([])[0],
            gatelog.judge([Gate(name="l", status=FAILED)])[0]} == {ERROR, NO_EVIDENCE, FAILED}


def test_all_passed_is_the_only_way_to_pass():
    state, why = gatelog.judge([Gate(name="health", status=PASSED),
                                Gate(name="md5", status=PASSED)])
    assert state == PASSED and "health" in why and "md5" in why


def test_summary_is_machine_readable():
    got = gatelog.summary([Gate(name="health", status=PASSED, command="curl", rc=0)])
    assert got["итог"] == PASSED
    assert got["записи"][0] == {"ворота": "health", "итог": PASSED, "команда": "curl",
                                "код": 0, "заметка": "", "источник": "сессия",
                                "когда": 0.0, "начата": 0.0, "отменена": 0.0,
                                "ожидание": None, "находка": False, "разбирает": ""}


# ── обёртка ───────────────────────────────────────────────────────────────

def test_the_wrapper_passes_the_exit_code_through_and_records_it(tmp_path):
    path = tmp_path / "gates.json"
    rc = gatelog.run("logdiff", [PY, "-c", "import sys; print('НОВОЕ'); sys.exit(10)"], path)
    assert rc == 10
    (gate,) = gatelog.read(path)
    assert gate.status == FAILED and gate.rc == 10 and "sys.exit(10)" in gate.command


def test_a_command_that_cannot_start_is_a_harness_error(tmp_path, capsys):
    path = tmp_path / "gates.json"
    rc = gatelog.run("health", ["/нет/такой/команды", "--x"], path)
    assert rc == gatelog.HARNESS_RC
    (gate,) = gatelog.read(path)
    assert gate.status == ERROR and gate.rc is None and "не запустилась" in gate.note
    # И не молча.
    assert "не запустилась" in capsys.readouterr().err


def test_a_command_killed_by_a_signal_is_a_harness_error(tmp_path):
    path = tmp_path / "gates.json"
    rc = gatelog.run("pytest", [PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"], path)
    assert rc == gatelog.HARNESS_RC
    (gate,) = gatelog.read(path)
    assert gate.status == ERROR and "сигналом" in gate.note


def test_the_wrapper_takes_the_file_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "w" / "gates.json"
    monkeypatch.setenv(gatelog.ENV_FILE, str(path))
    assert gatelog.run("health", [PY, "-c", "pass"]) == 0
    assert gatelog.read(path)[0].status == PASSED


def test_without_a_file_the_wrapper_still_runs_but_says_so(monkeypatch, capsys):
    monkeypatch.delenv(gatelog.ENV_FILE, raising=False)
    assert gatelog.run("health", [PY, "-c", "pass"]) == 0
    assert "записать некуда" in capsys.readouterr().err


def test_the_cli_runs_the_command_after_the_dashes(tmp_path):
    """Как её зовёт сессия: `python3 gatelog.py <имя> -- <команда>`; вывод
    команды идёт наружу как есть, код — её."""
    path = tmp_path / "gates.json"
    done = subprocess.run([PY, GATELOG, "--file", str(path), "logdiff", "--",
                           PY, "-c", "print('ИТОГ: новых видов 1'); raise SystemExit(10)"],
                          capture_output=True, text=True)
    assert done.returncode == 10
    assert "ИТОГ: новых видов 1" in done.stdout
    (gate,) = gatelog.read(path)
    assert gate.status == FAILED and gate.rc == 10


def test_the_pipe_after_the_wrapper_no_longer_hides_the_code(tmp_path):
    """Ровно та грабля с живых трасс: `… | tail; echo $?` печатает код tail.
    С обёрткой код записан ДО трубы."""
    path = tmp_path / "gates.json"
    shell = (f"{PY} {GATELOG} --file {path} logdiff -- {PY} -c "
             f"'import sys; sys.exit(10)' | tail -1; echo rc=$?")
    done = subprocess.run(["sh", "-c", shell], capture_output=True, text=True)
    assert "rc=0" in done.stdout            # сессия видит ноль…
    assert gatelog.read(path)[0].rc == 10   # …а в журнале настоящий код


def test_the_cli_refuses_without_a_command(tmp_path):
    done = subprocess.run([PY, GATELOG, "--file", str(tmp_path / "g.json"), "health"],
                          capture_output=True, text=True)
    assert done.returncode == gatelog.HARNESS_RC
    assert "нужна команда" in done.stderr


# ── отменённая попытка: перезапуск заменяет, задний ход — нет (DEV-107) ────
#
# Красной оставалась попытка, которую сессия сама же и отменила: разобралась,
# поправила вызов, перезапустила. Единственный способ выглядеть чисто был —
# не перезапускать вовсе, то есть прятать перезапуск. Отмена объявляется
# ДО запуска замены — тогда она ничего не знает о её результате.

def _rerun(path, name, code):
    """Прогон ворот через обёртку: 0 — чисто, иначе провал."""
    return gatelog.run(name, [PY, "-c", f"raise SystemExit({code})"], path)


def test_a_rerun_under_the_same_name_replaces_the_failed_attempt(tmp_path):
    path = tmp_path / "gates.json"
    _rerun(path, "pytest", 1)
    _rerun(path, "pytest", 0)
    first, second = gatelog.read(path)
    # Обе попытки в журнале: перезапуск не прячут.
    assert (first.status, second.status) == (FAILED, PASSED)
    assert first.cancelled > 0 and second.cancelled == 0
    # А судится одна — действующая.
    state, why = gatelog.judge(gatelog.read(path))
    assert state == PASSED and "отменённых попыток: 1" in why
    assert gatelog.effective(gatelog.read(path)) == [second]


def test_a_replaced_attempt_stays_visible_in_the_summary(tmp_path):
    path = tmp_path / "gates.json"
    _rerun(path, "pytest", 1)
    _rerun(path, "pytest", 0)
    got = gatelog.summary(gatelog.read(path))
    assert got["итог"] == PASSED
    assert [r["итог"] for r in got["записи"]] == [FAILED, PASSED]
    assert got["записи"][0]["отменена"] > 0


def test_a_cancellation_declared_after_the_replacement_finished_is_not_one(tmp_path):
    """Главная защита: отменить, УЖЕ ЗНАЯ результат замены, нельзя — иначе
    отмена становится способом прогнать дважды и оставить зелёное."""
    red = Gate(name="pytest", status=FAILED, rc=1, started=10.0, at=20.0,
               cancelled=100.0)                      # отметка поставлена позже
    green = Gate(name="pytest", status=PASSED, rc=0, started=30.0, at=40.0)
    state, why = gatelog.judge([red, green])
    assert state == FAILED and "pytest (код 1)" in why
    assert "отменённых попыток" not in why


def test_a_cancelled_attempt_without_a_replacement_still_counts(tmp_path):
    """Обёртка отметила отмену и не дожила до запуска замены (сессию сняли).
    Красная попытка остаётся действующей — в худшую для сессии сторону."""
    path = tmp_path / "gates.json"
    _rerun(path, "pytest", 1)
    assert gatelog.supersede(path, "pytest", 1e12) == 1
    assert gatelog.judge(gatelog.read(path))[0] == FAILED


def test_renaming_the_gate_is_not_a_replacement(tmp_path):
    """`pytest` → `pytest-ispravlen`: имя не договор, в журнале остаются обе."""
    path = tmp_path / "gates.json"
    _rerun(path, "pytest-vetka", 1)
    _rerun(path, "pytest-vetka-ispravlen", 0)
    state, why = gatelog.judge(gatelog.read(path))
    assert state == FAILED and "pytest-vetka (код 1)" in why


def test_a_rerun_touches_only_its_own_name(tmp_path):
    path = tmp_path / "gates.json"
    _rerun(path, "logdiff", 10)
    _rerun(path, "pytest", 1)
    _rerun(path, "pytest", 0)
    assert gatelog.judge(gatelog.read(path))[0] == FAILED, "находка logdiff не отменена"
    assert [g.name for g in gatelog.effective(gatelog.read(path))] == ["logdiff", "pytest"]


def test_the_wrapper_says_out_loud_that_it_replaced_an_attempt(tmp_path, capsys):
    path = tmp_path / "gates.json"
    _rerun(path, "pytest", 1)
    capsys.readouterr()
    _rerun(path, "pytest", 0)
    assert "прежних попыток отменено 1" in capsys.readouterr().err


def test_a_third_run_cancels_both_previous_attempts(tmp_path):
    path = tmp_path / "gates.json"
    _rerun(path, "pytest", 1)
    _rerun(path, "pytest", 2)
    _rerun(path, "pytest", 0)
    gates = gatelog.read(path)
    assert [g.cancelled > 0 for g in gates] == [True, True, False]
    assert gatelog.judge(gates) == (PASSED, "прошли все ворота (1): pytest; "
                                            "отменённых попыток: 2")


def test_the_logdiff_self_report_cancels_nothing(tmp_path, monkeypatch):
    """`logdiff` зовёт `note_result` уже с результатом в руках, и имя у всех
    его прогонов одно — второй прогон не имеет права стереть находку первого."""
    path = tmp_path / "gates.json"
    monkeypatch.setenv(gatelog.ENV_FILE, str(path))
    gatelog.note_result("logdiff", ["logdiff.py", "--unit", "a"], 10)
    gatelog.note_result("logdiff", ["logdiff.py", "--unit", "b"], 0)
    assert gatelog.judge(gatelog.read(path))[0] == FAILED


# ── контрольные ворота: красные по замыслу (DEV-104) ──────────────────────

def _control(path, name, code, expect):
    """Контрольные ворота через обёртку: ожидание объявлено в самой команде
    постановки, то есть до того, как код стал известен."""
    return gatelog.run(name, [PY, "-c", f"raise SystemExit({code})"], path,
                       expect=expect)


def test_a_control_gate_that_returns_the_declared_code_has_passed(tmp_path):
    """Тот самый случай DEV-38: проба против main обязана упасть с кодом 3, и
    её падение — доказательство, что проба ловит отсутствие правки."""
    path = tmp_path / "gates.json"
    assert _control(path, "proba-main", 3, 3) == 3, "код команды обёртка не меняет"
    (gate,) = gatelog.read(path)
    assert (gate.status, gate.rc, gate.expected) == (PASSED, 3, 3)
    state, why = gatelog.judge([gate])
    assert state == PASSED and "контрольных ворот: 1" in why


def test_a_control_gate_that_returns_another_code_has_not_passed(tmp_path):
    """Ожидание — не индульгенция: ноль тут ничем не лучше тройки. Проба,
    прошедшая против main, не доказала ничего и обязана быть красной."""
    path = tmp_path / "gates.json"
    _control(path, "proba-main", 0, 3)
    state, why = gatelog.judge(gatelog.read(path))
    assert state == FAILED and "proba-main (код 0, ждали 3)" in why


def test_without_a_declaration_nothing_changes(tmp_path):
    """Умолчание то же, что было: ожидание не объявлено — ждём ноль."""
    path = tmp_path / "gates.json"
    _rerun(path, "proba-main", 3)
    (gate,) = gatelog.read(path)
    assert gate.expected is None and gate.status == FAILED
    assert gatelog.judge([gate])[0] == FAILED


def test_a_declaration_on_a_record_nobody_launched_is_not_one(tmp_path, monkeypatch):
    """Объявляет ожидание ОБЁРТКА при запуске, и только она. У записи,
    поданной инструментом о себе самом (`note_result`) или дописанной
    отдельно, отметки запуска нет — суд считает код как без ожидания.

    Чего этот тест НЕ утверждает: что журнал нельзя подделать правкой файла.
    Кто правит `gates.json` руками, тому проще переписать сам `итог`, и так
    было до DEV-104 — журнал по контракту пишет обвязка. Защита здесь от
    ЛЕГАЛЬНОГО обхода: объявить ожидание командой после того, как код
    известен (соседний тест про перезапуск)."""
    path = tmp_path / "gates.json"
    monkeypatch.setenv(gatelog.ENV_FILE, str(path))
    gatelog.note_result("pytest", ["pytest", "-q"], 1)
    raw = json.loads(path.read_text())
    raw[0]["ожидание"], raw[0]["итог"] = 1, PASSED    # подрисовали и итог
    path.write_text(json.dumps(raw, ensure_ascii=False))
    state, why = gatelog.judge(gatelog.read(path))
    assert state == FAILED
    assert "ожидание 1 объявлено задним числом" in why


def test_a_rerun_cannot_declare_what_the_first_run_already_showed(tmp_path):
    """Второй обход того же: прогнать без объявления, увидеть код и
    перезапустить с `--expect <тот самый код>`. Формально объявление опять
    раньше своего запуска — по существу код уже известен."""
    path = tmp_path / "gates.json"
    _rerun(path, "pytest", 1)
    _control(path, "pytest", 1, 1)
    state, why = gatelog.judge(gatelog.read(path))
    assert state == FAILED and "объявлено задним числом" in why


def test_a_control_gate_may_be_rerun_under_the_same_declaration(tmp_path):
    """Честный перезапуск правило не задевает: ошиблась в команде, прогнала
    снова с тем же объявлением — засчитывается."""
    path = tmp_path / "gates.json"
    _control(path, "proba-main", 3, 3)
    _control(path, "proba-main", 3, 3)
    state, why = gatelog.judge(gatelog.read(path))
    assert state == PASSED and "отменённых попыток: 1" in why


def test_a_harness_error_stays_an_error_under_any_declaration(tmp_path):
    """Не отработавший запуск не доказательство ничего — даже если ждали
    именно его код. Иначе хватило бы объявить ожидание и не запускать."""
    path = tmp_path / "gates.json"
    rc = gatelog.run("proba-main", ["/nonexistent/binary"], path, expect=2)
    assert rc == gatelog.HARNESS_RC
    (gate,) = gatelog.read(path)
    assert gate.status == ERROR and gate.rc is None
    assert gatelog.judge([gate])[0] == ERROR
    # И код известного инструмента, означающий «выполнилось не то», тоже.
    assert gatelog.status_for("logdiff", 2, 2) == ERROR


def test_the_declaration_is_visible_in_the_record_and_in_the_summary(tmp_path):
    """Человек обязан отличать «ворота упали» от «контроль сработал»: без
    этого карточка подписи пугает красным там, где всё правильно."""
    path = tmp_path / "gates.json"
    _control(path, "proba-main", 3, 3)
    got = gatelog.summary(gatelog.read(path))
    assert got["записи"][0]["ожидание"] == 3
    assert "контрольных ворот: 1" in got["почему"]


# ── находка инструмента и её разбор (DEV-41) ──────────────────────────────

def test_the_tool_declares_its_own_scale_not_the_session(tmp_path, monkeypatch):
    """`logdiff` кодом 10 говорит «вот находки», и знает об этом он сам —
    сессии не надо помнить, что его надо обернуть особым образом."""
    path = tmp_path / "gates.json"
    monkeypatch.setenv(gatelog.ENV_FILE, str(path))
    gatelog.note_result("logdiff", ["logdiff.py", "--unit", "a"], 10)
    (gate,) = gatelog.read(path)
    assert gate.finding and gate.status == FAILED
    state, why = gatelog.judge([gate])
    assert state == FAILED and "находка не разобрана" in why


def test_an_old_record_is_read_as_a_finding_too(tmp_path):
    """В журналах, записанных до DEV-104, поля «находка» нет — узнаём по
    шкале инструмента, иначе разобрать прежнюю находку было бы нечем."""
    (gate,) = [Gate.from_dict({"ворота": "logdiff", "итог": FAILED, "код": 10})]
    assert gate.finding


def test_a_finding_explained_by_the_session_stops_lowering_the_outcome(tmp_path):
    """Случай DEV-41: находки разобраны, записаны отдельными воротами — и
    четырнадцать зелёных ворот больше не тонут из-за двух кодов 10."""
    path = tmp_path / "gates.json"
    _rerun(path, "logdiff", 10)
    assert gatelog.resolve("razbor", "logdiff", "фон: чужая подсистема", path) == 0
    state, why = gatelog.judge(gatelog.read(path))
    assert state == PASSED and "разобранных находок: 1" in why


def test_a_resolution_without_a_reason_is_refused(tmp_path):
    """Разбор без причины — кнопка «считать зелёным», а не разбор."""
    path = tmp_path / "gates.json"
    _rerun(path, "logdiff", 10)
    assert gatelog.resolve("razbor", "logdiff", "   ", path) == gatelog.HARNESS_RC
    assert len(gatelog.read(path)) == 1
    assert gatelog.judge(gatelog.read(path))[0] == FAILED


def test_only_a_finding_can_be_resolved(tmp_path):
    """Провал теста разбором не гасится: шкалу объявляет инструмент, и у
    `pytest` ненулевой код означает ровно провал."""
    path = tmp_path / "gates.json"
    _rerun(path, "pytest", 1)
    gatelog.resolve("razbor", "pytest", "да ну его", path)
    state, why = gatelog.judge(gatelog.read(path))
    assert state == FAILED and "pytest (код 1)" in why


def test_a_failed_resolution_resolves_nothing(tmp_path):
    """Разбор — такие же ворота: упавший разбор ничего не разобрал."""
    path = tmp_path / "gates.json"
    _rerun(path, "logdiff", 10)
    gatelog.run("razbor", [PY, "-c", "raise SystemExit(1)"], path, resolves="logdiff")
    assert gatelog.judge(gatelog.read(path))[0] == FAILED


def test_a_resolution_run_before_the_finding_resolves_nothing(tmp_path):
    """Разбор ложится ПОСЛЕ результата — судить находку, не посмотрев на
    неё, нельзя."""
    finding = Gate(name="logdiff", status=FAILED, rc=10, at=100.0, finding=True)
    early = Gate(name="razbor", status=PASSED, note="фон", resolves="logdiff",
                 started=10.0, at=20.0)
    assert gatelog.judge([finding, early])[0] == FAILED
    late = Gate(name="razbor", status=PASSED, note="фон", resolves="logdiff",
                started=110.0, at=120.0)
    assert gatelog.judge([finding, late])[0] == PASSED


# ── CLI ───────────────────────────────────────────────────────────────────

def test_the_cli_declares_the_expected_code(tmp_path):
    path = tmp_path / "gates.json"
    rc = subprocess.run([PY, GATELOG, "--file", str(path), "--expect", "3",
                         "proba-main", "--", PY, "-c", "raise SystemExit(3)"]).returncode
    assert rc == 3
    (gate,) = gatelog.read(path)
    assert gate.expected == 3 and gate.status == PASSED


def test_the_cli_resolves_a_finding_without_a_command(tmp_path):
    path = tmp_path / "gates.json"
    _rerun(path, "logdiff", 10)
    rc = subprocess.run([PY, GATELOG, "--file", str(path), "--resolves", "logdiff",
                         "--why", "фон: разовая пачка от сканера",
                         "razbor-logdiff"]).returncode
    assert rc == 0
    assert gatelog.judge(gatelog.read(path))[0] == PASSED


def test_a_flag_after_the_name_is_named_not_run(tmp_path):
    """`gatelog.py имя --expect 3 -- …` разобрался бы как «запусти программу
    --expect»: сказать это прямо дешевле, чем оставить в журнале «команда не
    запустилась»."""
    path = tmp_path / "gates.json"
    got = subprocess.run([PY, GATELOG, "--file", str(path), "proba", "--expect", "3",
                          "--", PY, "-c", "pass"], capture_output=True, text=True)
    assert got.returncode == gatelog.HARNESS_RC
    assert "флаги обёртки ставятся ПЕРЕД именем" in got.stderr
    assert gatelog.read(path) == []


def test_a_harness_error_does_not_take_away_the_right_to_declare(tmp_path, capsys):
    """Найдено код-ревью DEV-104: попытка, кончившаяся ошибкой обвязки, кода
    не показала — значит объявить ожидание после неё можно. Иначе опечатка в
    команде запирала бы контроль насовсем."""
    path = tmp_path / "gates.json"
    gatelog.run("proba-main", ["/nonexistent/binary"], path)      # опечатка
    _control(path, "proba-main", 3, 3)
    state, why = gatelog.judge(gatelog.read(path))
    assert state == PASSED and "контрольных ворот: 1" in why


def test_the_wrapper_says_at_once_that_a_declaration_will_not_count(tmp_path, capsys):
    """Тоже из ревью: узнать о незачтённом объявлении из вердикта через час —
    значит не успеть ничего исправить."""
    path = tmp_path / "gates.json"
    _rerun(path, "pytest", 1)
    capsys.readouterr()
    _control(path, "pytest", 1, 1)
    assert "ожидание 1 НЕ зачтено" in capsys.readouterr().err
    # А у честного контроля такой строки нет.
    _control(path, "proba-main", 3, 3)
    assert "НЕ зачтено" not in capsys.readouterr().err
