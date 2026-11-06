"""Тесты сравнения журнала до/после выката (logdiff.py).

Главный риск этого инструмента — не «не найдёт», а «найдёт лишнего»: шаг,
который на каждом выкате показывает пугающие строки из обычного фона, человек
перестаёт читать через неделю. Поэтому большая часть тестов ниже — про то, что
фон НЕ попадает в находки, и про честность отчёта там, где сравнивать не с чем.

Docker и journalctl не запускаются: читалки подменяются, время задаётся
`--now`. Проверяется разбор, сведение строк к видам, классификация и отчёт.
"""
from collections import Counter
from datetime import datetime, timezone

import pytest

import logdiff

AT = "2026-08-31T10:00:00Z"
NOW = "2026-08-31T10:10:00Z"


# ── разбор момента выката ─────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "2026-08-31T10:00:00Z",
    "2026-08-31T10:00:00+00:00",
    "2026-08-31 10:00:00 UTC",
    "2026-08-31T10:00:00",
])
def test_deploy_moment_is_read_in_every_format_gitlab_sends(text):
    # Вебхук GitLab отдаёт то ISO-8601, то старый «... UTC», а docker inspect —
    # свой третий вид. Не разобрав время, инструмент молча съезжает окнами.
    assert logdiff.parse_ts(text) == datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def test_a_moment_with_an_offset_is_brought_to_utc():
    assert logdiff.parse_ts("2026-08-31T13:00:00+03:00") == datetime(
        2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def test_an_unparsable_moment_is_an_error_not_a_guess():
    # Тихо подставить «час назад» нельзя: окна разъедутся, а отчёт будет
    # выглядеть настоящим.
    with pytest.raises(ValueError):
        logdiff.parse_ts("вчера вечером")


# ── сведение строки к «виду» ──────────────────────────────────────────────

def test_the_same_message_with_different_numbers_is_one_kind():
    # Без этого повтор одной ошибки выглядит сотней разных «новых» видов, и
    # находка тонет в собственном выводе.
    a = logdiff.normalize("2026-08-31 10:00:01,123 ERROR request 8123 failed in 15ms")
    b = logdiff.normalize("2026-08-31 10:00:02,900 ERROR request 8124 failed in 31ms")
    assert a == b


def test_the_leading_timestamp_is_cut_in_docker_and_journald_shapes():
    docker = logdiff.normalize("2026-08-31T10:00:01.123456Z ERROR upstream is down")
    journal = logdiff.normalize(
        "Aug 31 10:00:01 ip-172-31-5-176 python3[1111571]: ERROR upstream is down")
    assert docker == journal == "ERROR upstream is down"


def test_ids_that_change_every_run_do_not_split_a_kind():
    a = logdiff.normalize("job 3f2a1b9c8d7e failed for 550e8400-e29b-41d4-a716-446655440000")
    b = logdiff.normalize("job a1b2c3d4e5f6 failed for 550e8400-e29b-41d4-a716-446655441111")
    assert a == b and "<uuid>" in a and "<hex>" in a


def test_different_messages_stay_different_kinds():
    # Обратная сторона: если сводить слишком грубо, новая ошибка совпадёт с
    # фоновой и проверка промолчит.
    assert (logdiff.normalize("ERROR redis timeout")
            != logdiff.normalize("ERROR postgres timeout"))


def test_a_long_line_is_truncated_but_still_comparable():
    line = "ERROR " + "x" * 500
    assert len(logdiff.normalize(line)) <= 180


# ── фильтр «похоже на ошибку» ─────────────────────────────────────────────

def test_ordinary_traffic_is_not_a_signal():
    assert not logdiff.interesting('GET /api/v2/health 200 OK 3ms')


@pytest.mark.parametrize("line", [
    "ERROR something",
    "Traceback (most recent call last):",
    "WARNING deprecated",
    "Timeout reading from redis:6379",
    "connection refused",
    "не удалось прочитать конфиг",
])
def test_error_shaped_lines_are_signals(line):
    assert logdiff.interesting(line)


def test_all_mode_counts_everything(monkeypatch):
    lines = ["GET /health 200", "GET /health 200", "ERROR boom"]
    counts, _ = logdiff.tally(lines, use_all=False)
    assert sum(counts.values()) == 1
    counts_all, _ = logdiff.tally(lines, use_all=True)
    assert sum(counts_all.values()) == 3


# ── классификация ─────────────────────────────────────────────────────────

def test_background_present_before_the_deploy_is_not_a_finding():
    before = Counter({"ERROR upstream flaky": 40})
    after = Counter({"ERROR upstream flaky": 10})
    new, surged, background = logdiff.classify(before, after, 40.0, 10.0)
    assert new == [] and surged == []
    assert background == [(10, "ERROR upstream flaky")]


def test_a_line_that_did_not_exist_before_is_new():
    new, surged, _ = logdiff.classify(
        Counter({"ERROR upstream flaky": 40}),
        Counter({"ERROR upstream flaky": 10, "Timeout reading from redis:<n>": 60}),
        40.0, 10.0)
    assert new == [(60, "Timeout reading from redis:<n>")]
    assert surged == []


def test_a_rate_that_jumped_is_a_finding_too():
    # Дефект умеет не создавать новых строк, а разгонять старые: было раз в
    # полчаса, стало каждые десять секунд.
    _, surged, _ = logdiff.classify(
        Counter({"WARN retrying": 1}), Counter({"WARN retrying": 60}), 30.0, 10.0)
    assert surged and surged[0][0] == 60


def test_a_couple_of_extra_lines_is_not_a_surge():
    # На коротком окне любая единичная строка даёт кратный «рост» — без порога
    # по количеству шаг стал бы шуметь на ровном месте.
    _, surged, _ = logdiff.classify(
        Counter({"WARN retrying": 1}), Counter({"WARN retrying": 2}), 30.0, 10.0)
    assert surged == []


# ── отчёт целиком ─────────────────────────────────────────────────────────

@pytest.fixture
def fake_logs(monkeypatch):
    """Подменить читалки журнала: (окно после, окно до) → строки."""
    def install(after, before, cut_after=False, cut_before=False):
        state = {}

        def read(name, since, until):
            # Окна различаем по границе: `until` окна «до» равен моменту выката.
            if state.get("first") is None:
                state["first"] = True
                return list(after), cut_after
            return list(before), cut_before

        monkeypatch.setattr(logdiff, "read_docker", read)
        monkeypatch.setattr(logdiff, "read_unit", read)
        monkeypatch.setattr(logdiff, "_inspect", lambda name: "RestartCount=0 Health=healthy")
        monkeypatch.setattr(logdiff, "_unit_state", lambda name: "NRestarts=0")
        monkeypatch.setattr(logdiff, "container_created",
                            lambda name: datetime(2026, 8, 1, tzinfo=timezone.utc))
    return install


def run(argv):
    return logdiff.main(argv)


def test_clean_deploy_reports_nothing_and_exits_zero(fake_logs, capsys):
    fake_logs(after=["INFO started", "ERROR upstream flaky"],
              before=["ERROR upstream flaky"] * 5)
    rc = run(["--at", AT, "--now", NOW, "--docker", "svc"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "НОВОЕ: нет" in out
    assert "нового в журнале нет" in out


def test_the_redis_timeout_case_is_caught(fake_logs, capsys):
    # Тот самый дефект 31.08.2026: файлы доехали байт в байт, а сервис начал
    # писать одну и ту же строку каждые 10 секунд. Ради него всё и заведено.
    fake_logs(after=["2026-08-31 10:00:%02d,000 ERROR Timeout reading from redis:6379"
                     % (i % 60) for i in range(60)] + ["INFO ok"],
              before=["INFO ok"] * 30)
    rc = run(["--at", AT, "--now", NOW, "--docker", "infrastructure-events-api-1"])
    out = capsys.readouterr().out
    assert rc == 10
    assert "Timeout reading from redis:<n>" in out
    assert "60×" in out
    assert "пример: 2026-08-31 10:00:" in out


def test_a_recreated_container_is_reported_as_having_no_background(fake_logs,
                                                                   monkeypatch, capsys):
    # `docker compose up -d` пересоздаёт контейнер, и журнал прежнего уезжает
    # вместе с ним. Тогда «новым» выглядит вообще всё, включая обычный старт, —
    # и отчёт обязан сказать это вслух, иначе проверка врёт с уверенным видом.
    fake_logs(after=["ERROR deprecation warning"], before=[])
    monkeypatch.setattr(logdiff, "container_created",
                        lambda name: datetime(2026, 8, 31, 9, 59, tzinfo=timezone.utc))
    rc = run(["--at", AT, "--now", NOW, "--docker", "svc"])
    out = capsys.readouterr().out
    assert rc == 10
    assert "пересоздан выкатом" in out
    assert "нет ни строки" in out


def test_a_container_younger_than_the_window_is_not_called_recreated(fake_logs,
                                                                    monkeypatch, capsys):
    # Контейнер, поднятый час назад сам по себе, окном «до» покрыт частично —
    # но выкат его не пересоздавал, и фон у него есть. Объявить тут «журнала
    # предшественника нет» значило бы гнать сессию в PARTIAL на ровном месте.
    fake_logs(after=["ERROR boom"], before=["ERROR boom"] * 4)
    monkeypatch.setattr(logdiff, "container_created",
                        lambda name: datetime(2026, 8, 31, 9, 45, tzinfo=timezone.utc))
    rc = run(["--at", AT, "--now", NOW, "--docker", "svc"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "пересоздан" not in out
    assert "фон известен только за 15.0 мин" in out


def test_a_command_that_failed_is_not_mistaken_for_log_lines():
    # docker пишет «No such container» в stderr, а он слит со stdout. Пока
    # такая строка считалась журналом, она попадала в оба окна, сходила за фон
    # — и опечатка в имени контейнера давала бодрое «нового в журнале нет».
    with pytest.raises(RuntimeError) as e:
        logdiff._run(["sh", "-c", "echo 'No such container: svc' >&2; exit 1"])
    assert "No such container" in str(e.value)


def test_an_empty_after_window_is_not_a_clean_journal(fake_logs, capsys):
    # journalctl на несуществующий юнит выходит с нулём и печатает пусто — по
    # коду возврата это не поймать, а «нового нет» тут читается как проверено.
    fake_logs(after=[], before=[])
    rc = run(["--at", AT, "--now", NOW, "--unit", "no-such-unit"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "источник не тот" in out
    assert "проверять нечего" in out
    assert "нового в журнале нет" not in out


def test_a_truncated_before_window_is_admitted(fake_logs, capsys):
    # Обрезанное окно «до» завышает число новых видов. Молчать про обрезку —
    # то же самое, что подделать сравнение.
    fake_logs(after=["ERROR boom"], before=["INFO ok"], cut_before=True)
    run(["--at", AT, "--now", NOW, "--docker", "svc"])
    assert "окно ДО обрезано" in capsys.readouterr().out


def test_a_too_fresh_deploy_is_flagged(fake_logs, capsys):
    # Проверка стартует через секунды после деплоя: то, что сыпется раз в
    # минуту, в окно ещё не попало, и «тихо» тут ничего не доказывает.
    fake_logs(after=[], before=["INFO ok"])
    run(["--at", AT, "--now", "2026-08-31T10:00:20Z", "--docker", "svc"])
    assert "окно после выката всего" in capsys.readouterr().out


def test_the_before_window_is_wider_than_the_after_one(fake_logs, capsys):
    # Фон надо ловить на длинном окне: редкая строка, попавшая в короткое
    # окно «после», иначе объявляется новой на каждом втором выкате.
    fake_logs(after=[], before=[])
    run(["--at", AT, "--now", NOW, "--docker", "svc"])
    out = capsys.readouterr().out
    assert "(40.0 мин" in out  # 4 × десятиминутного окна «после»
    assert "(10.0 мин" in out


def test_the_before_window_never_shrinks_below_half_an_hour(fake_logs, capsys):
    fake_logs(after=[], before=[])
    run(["--at", AT, "--now", "2026-08-31T10:01:00Z", "--docker", "svc"])
    assert "(30.0 мин" in capsys.readouterr().out


def test_the_number_of_hidden_kinds_is_printed(fake_logs, capsys):
    # Молча обрезанный список читается как «это всё, что нашлось».
    # Виды должны быть разными по СЛОВАМ: номера сводятся к `<n>` и склеили бы
    # всё в один вид.
    fake_logs(after=["ERROR %s failed" % ("x" * (i + 1)) for i in range(logdiff.TOP + 5)],
              before=[])
    run(["--at", AT, "--now", NOW, "--docker", "svc"])
    assert "и ещё 5 видов, не показаны" in capsys.readouterr().out


def test_two_sources_at_once_is_a_usage_error(capsys):
    assert run(["--at", AT, "--docker", "a", "--unit", "b"]) == 2


def test_no_source_at_all_is_a_usage_error(capsys):
    assert run(["--at", AT]) == 2


def test_a_deploy_moment_in_the_future_is_refused(capsys):
    # Съехавшее время — единственная поломка, которая делает отчёт бессмысленным
    # молча: окно «после» пустое, и всё выглядит благополучно.
    assert run(["--at", "2026-08-31T11:00:00Z", "--now", NOW, "--docker", "svc"]) == 2


def test_an_unreadable_journal_is_not_a_clean_verdict(monkeypatch, capsys):
    # «Контейнера нет» не должно превращаться в «нового в журнале нет».
    def boom(name, since, until):
        raise RuntimeError("No such container: svc")
    monkeypatch.setattr(logdiff, "read_docker", boom)
    assert run(["--at", AT, "--now", NOW, "--docker", "svc"]) == 2


# ── свой код — в журнал ворот сессии (DEV-88) ─────────────────────────────

def test_a_finding_is_recorded_in_the_gates_file_as_a_failed_gate(fake_logs, tmp_path, monkeypatch):
    """Код 10 ложится в `gates.json` самим скриптом: в трубе сессии он
    терялся (`| tail; echo $?` печатает код `tail`) — в 20 проверках из 20."""
    import gatelog
    path = tmp_path / "gates.json"
    monkeypatch.setenv(gatelog.ENV_FILE, str(path))
    fake_logs(after=["ERROR db down"] * 3, before=[])
    assert run(["--at", AT, "--now", NOW, "--docker", "svc"]) == 10
    (gate,) = gatelog.read(path)
    assert gate.name == "logdiff" and gate.rc == 10 and gate.status == gatelog.FAILED
    assert "--docker svc" in gate.command


def test_a_clean_journal_is_recorded_as_a_passed_gate(fake_logs, tmp_path, monkeypatch):
    import gatelog
    path = tmp_path / "gates.json"
    monkeypatch.setenv(gatelog.ENV_FILE, str(path))
    fake_logs(after=["INFO ok"], before=["INFO ok"])
    assert run(["--at", AT, "--now", NOW, "--docker", "svc"]) == 0
    assert gatelog.read(path)[0].status == gatelog.PASSED


def test_an_unreadable_journal_is_recorded_as_a_harness_error(tmp_path, monkeypatch):
    """Третье состояние: «прочитать не вышло» — не «журнал чист» и не
    «logdiff не звали»."""
    import gatelog
    path = tmp_path / "gates.json"
    monkeypatch.setenv(gatelog.ENV_FILE, str(path))

    def boom(name, since, until):
        raise RuntimeError("No such container: svc")
    monkeypatch.setattr(logdiff, "read_docker", boom)
    assert run(["--at", AT, "--now", NOW, "--docker", "svc"]) == 2
    (gate,) = gatelog.read(path)
    assert gate.status == gatelog.ERROR and gate.rc == 2


def test_bad_arguments_are_recorded_too(tmp_path, monkeypatch, capsys):
    # argparse выходит кодом 2 мимо return — без записи сорванный вызов
    # выглядел бы как «не звали».
    import gatelog
    path = tmp_path / "gates.json"
    monkeypatch.setenv(gatelog.ENV_FILE, str(path))
    assert run(["--docker", "svc"]) == 2       # нет обязательного --at
    assert gatelog.read(path)[0].status == gatelog.ERROR


def test_without_the_environment_nothing_is_written(fake_logs, tmp_path, monkeypatch):
    # Вне сессии скрипт зовут руками — журнала нет и писать некуда.
    import gatelog
    monkeypatch.delenv(gatelog.ENV_FILE, raising=False)
    monkeypatch.chdir(tmp_path)
    fake_logs(after=["ERROR db down"] * 3, before=[])
    assert run(["--at", AT, "--now", NOW, "--docker", "svc"]) == 10
    assert not (tmp_path / "gates.json").exists()
