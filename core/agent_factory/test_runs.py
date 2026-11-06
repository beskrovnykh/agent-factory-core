"""Журнал запусков: задача как единица учёта, сводка и карточка в трекере.

Проверяется не «функция вернула словарь», а те два свойства, ради которых
журнал и заводился: цена задачи не распадается по заходам, и учёт не плодит
комментарии. Всё остальное — обвязка вокруг этих двух.
"""
import asyncio
import json
import time

import pytest

from agent_factory import runs
from agent_factory.runs import (Journal, Ledger, by_task, card_body,
                                card_facts, digest, digest_text, needs_human)

HOUR = 3600


def run(name, *, kind="work", cost=0.0, started=0.0, touched=None, secs=0,
        outcome="READY_FOR_HUMAN", going=False, dropped=False, mem=0,
        tokens=(0, 0, 0), stop="", mode=""):
    """Один заход в том виде, в каком его отдаёт `sessions.walk`."""
    return {
        "name": name, "task": name.split("-2026")[0].split("-verify")[0],
        "род": kind, "стоимость": cost, "started": started,
        "touched": started if touched is None else touched, "секунд": secs,
        "исход": outcome, "идёт": going, "брошена": dropped, "память": mem,
        "вход": tokens[0], "выход": tokens[1], "кэш": tokens[2],
        "остановка": stop, "режим": mode,
    }


# ── цена задачи ─────────────────────────────────────────────────────────────

def test_two_entrances_of_one_task_are_one_row_with_the_whole_price():
    """Случай DEV-62 из HLD: $9,15 и $1,20 — это ОДНА задача за $10,35.

    Пока строка равна сессии, цена задачи не видна нигде: два числа лежат в
    разных строках, и сложить их некому.
    """
    rows = by_task([
        run("DEV-62-20260912-030000", cost=9.15, started=1000, secs=1500),
        run("DEV-62-20260912-050000", cost=1.20, started=9000, secs=300),
    ])
    assert len(rows) == 1
    assert rows[0]["задача"] == "DEV-62"
    assert rows[0]["стоимость"] == 10.35
    assert rows[0]["заходов"] == 2
    assert rows[0]["секунд"] == 1800


def test_a_second_work_entrance_is_a_rework():
    rows = by_task([run("DEV-4-20260913-211228", started=1),
                    run("DEV-4-20260913-214657", started=2)])
    assert rows[0]["переделка"] is True
    assert rows[0]["рабочих заходов"] == 2


def test_a_deploy_check_is_not_a_rework():
    """Проверка выката бывает у КАЖДОЙ сданной задачи. Считать её вторым
    заходом значило бы объявить переделкой всё, что прошло нормально."""
    rows = by_task([run("DEV-46-20260913-190000", started=1),
                    run("DEV-46-verify-20260913-193043", kind="verify",
                        started=2, outcome="VERIFIED")])
    assert rows[0]["заходов"] == 2
    assert rows[0]["рабочих заходов"] == 1
    assert rows[0]["переделка"] is False


def test_task_outcome_is_the_last_finished_entrance():
    rows = by_task([
        run("DEV-46-20260913-190000", started=1, outcome="READY_FOR_HUMAN"),
        run("DEV-46-verify-20260913-193043", kind="verify", started=2,
            outcome="VERIFIED"),
    ])
    assert rows[0]["исход"] == "VERIFIED"


def test_a_new_entrance_after_a_good_check_puts_the_task_back_to_work():
    """VERIFIED не «прилипает»: после него завели ещё заход — значит задача
    снова в работе, чем бы проверка ни кончилась."""
    rows = by_task([
        run("DEV-46-verify-20260913-193043", kind="verify", started=2,
            outcome="VERIFIED"),
        run("DEV-46-20260913-220000", started=3, outcome="FAILED"),
    ])
    assert rows[0]["исход"] == "FAILED"


def test_an_entrance_without_a_stamp_does_not_erase_the_previous_outcome():
    """Раннер прежней версии исхода не писал. Пустая отметка — «неизвестно»,
    и подменять ею известный исход нельзя."""
    rows = by_task([run("DEV-9-20260901-100000", started=1, outcome="VERIFIED"),
                    run("DEV-9-20260901-120000", started=2, outcome="")])
    assert rows[0]["исход"] == "VERIFIED"


def test_calendar_span_covers_the_wait_between_entrances():
    rows = by_task([run("DEV-62-20260912-030000", started=0, touched=600, secs=600),
                    run("DEV-62-20260912-050000", started=10 * HOUR, touched=10 * HOUR + 300,
                        secs=300)])
    assert rows[0]["секунд"] == 900
    assert rows[0]["срок"] == 10 * HOUR + 300


def test_the_latest_mark_of_any_entrance_becomes_the_mark_of_the_task():
    rows = by_task(
        [run("DEV-7-20260912-010000", started=1), run("DEV-7-20260912-020000", started=2)],
        {"DEV-7-20260912-010000": {"оценка": "ошибки"}, "DEV-7-20260912-020000": {"оценка": "ок"}})
    assert rows[0]["оценка"] == "ок"


def test_an_intake_bundle_is_not_a_task():
    """У приёмной пачки свой номер (`INTAKE-<хэш>`) и никакой задачи в
    трекере. Считать её задачей значит объявить начатыми задачи, которых
    нет, а класть ей карточку — гарантированно спрашивать трекер о том,
    чего у него нет."""
    rows = by_task([run("INTAKE-6a827913368a-intake-20260911-062037",
                        kind="intake", cost=0.53, started=1)])
    assert rows[0]["постановка"] is True
    assert rows[0]["рабочих заходов"] == 0


def test_a_task_is_not_mistaken_for_an_intake_bundle():
    rows = by_task([run("DEV-1-20260913-100000", started=1)])
    assert rows[0]["постановка"] is False


def test_intake_is_counted_apart_but_its_money_is_not_hidden():
    now = time.time()
    rows = by_task([
        run("DEV-1-20260913-100000", started=now - HOUR, touched=now - HOUR,
            cost=2.0, outcome="VERIFIED"),
        run("INTAKE-6a82-intake-20260913-110000", kind="intake",
            started=now - HOUR, touched=now - HOUR, cost=0.53),
    ])
    d = window(rows)
    assert d["задач"] == 1 and d["постановок"] == 1
    assert d["стоимость"] == 2.53
    text = digest_text(d)
    assert "1 задач(и)" in text and "1 приём" in text


def test_a_window_of_nothing_but_intake_is_not_called_empty():
    """В нём потрачены деньги. «Запусков не было» тут было бы неправдой."""
    now = time.time()
    rows = by_task([run("INTAKE-6a82-intake-20260913-110000", kind="intake",
                        started=now - HOUR, touched=now - HOUR, cost=0.53)])
    text = digest_text(window(rows))
    assert "запусков не было" not in text
    assert "$0.53" in text


def test_an_intake_bundle_gets_no_card():
    tracker = FakeTracker()
    rows = by_task([run("INTAKE-6a82-intake-20260913-110000", kind="intake",
                        started=1)])
    assert Ledger(tracker, runs.log).write(rows) == 0
    assert tracker.comments == {}


def test_a_tracker_that_cannot_edit_says_so_once_not_per_task(caplog):
    """Инсталляция со своим трекером, у которого метода ещё нет. Строка на
    каждую задачу завалила бы журнал, и настоящие отказы в нём было бы не
    разглядеть."""
    class Old:
        def comment(self, issue_id, body):
            pass

    rows = by_task([run("DEV-1-20260913-100000", started=1),
                    run("DEV-2-20260913-110000", started=2)])
    with caplog.at_level("WARNING"):
        assert Ledger(Old(), runs.log).write(rows) == 0
    assert len([r for r in caplog.records if "не умеет править" in r.message]) == 1


# ── кому потребовался человек ───────────────────────────────────────────────

@pytest.mark.parametrize("row, expected", [
    ({"переделка": True}, True),
    ({"брошена": True}, True),
    ({"исход": "FAILED"}, True),
    ({"исход": "VERIFIED"}, False),
    ({"исход": "READY_FOR_HUMAN"}, False),
])
def test_human_is_counted_by_traces_not_by_asking(row, expected):
    assert needs_human(row) is expected


# ── сводка ──────────────────────────────────────────────────────────────────

def window(rows, span=runs.DAY):
    now = time.time()
    return digest(rows, now - span, now)


def test_digest_counts_what_the_statement_asked_for():
    now = time.time()
    rows = by_task([
        run("DEV-1-20260913-100000", started=now - HOUR, touched=now - HOUR, secs=600,
            outcome="VERIFIED", cost=1.0),
        run("DEV-2-20260913-110000", started=now - 2 * HOUR, touched=now - 2 * HOUR,
            secs=1200, outcome="READY_FOR_HUMAN", cost=2.0),
        run("DEV-3-20260913-120000", started=now - 3 * HOUR, touched=now - 3 * HOUR,
            secs=60, outcome="FAILED", cost=0.5),
        run("DEV-4-20260913-130000", started=now - 4 * HOUR, touched=now - 4 * HOUR,
            secs=30, outcome="WAITING_LIMIT", cost=0.1),
    ])
    d = window(rows)
    assert d["задач"] == 4
    assert d["выкат подтверждён"] == 1
    assert d["дошло до человека"] == 1
    assert d["по исходам"]["FAILED"] == 1
    assert d["ждёт"] == 1
    assert d["с человеком"] == 1
    assert d["стоимость"] == 3.6


def test_a_task_outside_the_window_is_not_counted():
    now = time.time()
    rows = by_task([run("DEV-1-20260913-100000", started=now - 40 * HOUR,
                        touched=now - 40 * HOUR)])
    assert window(rows)["задач"] == 0
    assert window(rows, 7 * runs.DAY)["задач"] == 1


def test_averages_ignore_a_task_that_is_still_running():
    """У идущей задачи срок ещё растёт. Подмешав её, среднее занижаем тем
    сильнее, чем больше задач прямо сейчас в работе."""
    now = time.time()
    rows = by_task([
        run("DEV-1-20260913-100000", started=now - HOUR, touched=now - HOUR, secs=600),
        run("DEV-2-20260913-110000", started=now - 60, touched=now - 1, secs=59, going=True),
    ])
    d = window(rows)
    assert d["идёт"] == 1
    assert d["среднее активное"] == 600


def test_an_empty_window_says_so_instead_of_printing_zeros():
    """«Начато 0, принято 0, $0» на телефоне читается как сломанный счётчик,
    и разбираться человек идёт не туда."""
    text = digest_text(window([]))
    assert "запусков не было" in text
    assert "$0" not in text


def test_the_digest_admits_which_outcomes_nobody_writes_yet():
    now = time.time()
    rows = by_task([run("DEV-1-20260913-100000", started=now - HOUR, touched=now - HOUR,
                        outcome="VERIFIED")])
    text = digest_text(window(rows))
    assert "некому записать" in text
    for outcome in runs.NOT_WRITTEN_BY_RUNNER:
        assert outcome in text


def test_a_written_outcome_disappears_from_the_nobody_writes_note():
    now = time.time()
    rows = by_task([run("DEV-1-20260913-100000", started=now - HOUR, touched=now - HOUR,
                        outcome="CANCELLED")])
    text = digest_text(window(rows))
    assert "CANCELLED" not in text.split("некому записать")[-1]


def test_a_day_window_stays_a_day_when_the_caller_leaves_the_end_open():
    """`until` по умолчанию берётся вторым вызовом `time.time()` — на
    микросекунду позже `since`. Без округления окно оказывается шире суток, и
    сводка уходит по недельной ветке: «Принято по дням: 0, 0» вместо одного
    числа. Ширину поэтому считает сама сводка и в целых секундах."""
    assert digest([], time.time() - runs.DAY)["окно"] == runs.DAY


def test_a_day_asked_for_by_the_command_is_not_read_as_a_week(tmp_path):
    """Окно берётся от ОДНОЙ отметки времени. Иначе `until` вычисляется на
    микросекунду позже `since`, окно оказывается шире суток, и дневная сводка
    уходит по недельной ветке — «Принято по дням: 0, 0» вместо одного числа.
    Юнит-тесты этого не видели: там оба конца окна приходят из одного `now`.
    """
    work_root(tmp_path, "DEV-77-20260913-100000")
    transport = FakeTransport()
    asyncio.run(Journal(str(tmp_path), transport=transport).command("42", ""))
    text = transport.sent[0][1]
    assert "Принято за сутки" in text
    assert "Принято по дням" not in text


def test_entrances_without_a_stamp_are_named_not_counted_as_nothing():
    """Первая сводка после выката: раннер ещё не перезапущен, исхода ни у кого
    нет. Нули во всех исходах без объяснения читаются как «фабрика ничего не
    сделала», и человек идёт разбираться не туда."""
    now = time.time()
    rows = by_task([run("DEV-1-20260913-100000", started=now - HOUR,
                        touched=now - HOUR, outcome="")])
    d = window(rows)
    assert d["без отметки"] == 1
    assert "Без отметки исхода: 1" in digest_text(d)


def test_a_stamped_window_says_nothing_about_missing_stamps():
    now = time.time()
    rows = by_task([run("DEV-1-20260913-100000", started=now - HOUR,
                        touched=now - HOUR, outcome="VERIFIED")])
    assert "Без отметки" not in digest_text(window(rows))


def test_a_day_is_read_against_the_pace_from_the_statement():
    now = time.time()
    rows = by_task([run(f"DEV-{i}-20260913-100000", started=now - HOUR, touched=now - HOUR,
                        outcome="VERIFIED") for i in range(5)])
    text = digest_text(window(rows), "За сутки")
    assert "в ориентире" in text and "4–8" in text


def test_a_slow_day_is_named_slow():
    now = time.time()
    rows = by_task([run("DEV-1-20260913-100000", started=now - HOUR, touched=now - HOUR,
                        outcome="VERIFIED")])
    assert "ниже ориентира" in digest_text(window(rows))


def test_only_a_confirmed_deploy_counts_as_accepted():
    """Открытый запрос на слияние задачей не является — в «принято» он не
    идёт, иначе показатель меряет активность, а не результат."""
    now = time.time()
    rows = by_task([run("DEV-1-20260913-100000", started=now - HOUR, touched=now - HOUR,
                        outcome="READY_FOR_HUMAN")])
    assert window(rows)["по дням"][-1]["принято"] == 0


def test_a_week_shows_a_number_per_day():
    now = time.time()
    rows = by_task([run("DEV-1-20260913-100000", started=now - HOUR, touched=now - HOUR,
                        outcome="VERIFIED")])
    d = window(rows, 7 * runs.DAY)
    assert len(d["по дням"]) >= 7
    assert "Принято по дням" in digest_text(d, "За неделю")


# ── этапы задачи ────────────────────────────────────────────────────────────

def test_the_stages_are_the_entrances_in_order():
    """«Переходы по этапам» из постановки — это заходы: род каждому ставит
    раннер, когда поднимает сессию. Событие, а не отметка в трекере."""
    rows = by_task([run("DEV-9-20260913-100000", kind="работа", started=1,
                        cost=2.0, secs=600),
                    run("DEV-9-verify-20260913-120000", kind="проверка",
                        started=2, cost=0.5, outcome="VERIFIED")])
    stages = rows[0]["этапы"]
    assert [st["род"] for st in stages] == ["работа", "проверка"]
    assert stages[0]["usd"] == 2.0 and stages[0]["секунд"] == 600
    assert stages[1]["исход"] == "VERIFIED"
    assert runs.stages_line(rows[0]) == "работа → проверка"


def test_a_second_work_entrance_reads_as_a_rework_in_the_stages():
    rows = by_task([run("DEV-9-20260913-100000", started=1),
                    run("DEV-9-20260913-140000", started=2),
                    run("DEV-9-verify-20260913-150000", kind="verify",
                        started=3)])
    assert runs.stages_line(rows[0]) == "работа → доработка → проверка"


def test_an_abnormal_stop_is_named_next_to_its_stage():
    """Без этого «работа → доработка» читается как «делали дважды», хотя на
    деле первый заход упёрся в лимит ходов — и переделывать было нечего."""
    rows = by_task([run("DEV-9-20260913-100000", started=1,
                        stop="error_max_turns"),
                    run("DEV-9-20260913-140000", started=2)])
    assert runs.stages_line(rows[0]) == "работа (error_max_turns) → доработка"


def test_the_card_carries_the_stages_for_man_and_machine():
    rows = by_task([run("DEV-9-20260913-100000", started=1),
                    run("DEV-9-verify-20260913-120000", kind="verify",
                        started=2)])
    body = card_body(rows[0])
    assert "Этапы: работа → проверка." in body
    # В машинный блок род едет КАК ЕСТЬ, без перевода: его пишет раннер, и
    # читателю блока нужен исходный признак, а не наше слово для человека.
    assert [st["род"] for st in card_facts(rows[0])["этапы"]] == \
        ["work", "verify"]


# ── карточка учёта ──────────────────────────────────────────────────────────

def test_the_card_carries_the_whole_price_and_a_machine_block():
    rows = by_task([run("DEV-62-20260912-030000", cost=9.15, started=1, secs=1500),
                    run("DEV-62-20260912-050000", cost=1.20, started=2, secs=300)])
    body = card_body(rows[0])
    assert "$10.35" in body
    assert body.startswith(runs.card_marker("DEV-62"))
    block = json.loads(body.split("```json")[1].split("```")[0])
    assert block["учёт"] == 1 and block["usd"] == 10.35
    assert block["заходов"] == 2 and block["минут"] == 30


def test_card_facts_stamp_the_time_in_utc():
    facts = card_facts(by_task([run("DEV-1-20260913-100000", started=0, touched=0)])[0])
    assert facts["начато"] == "" and facts["закончено"] == ""
    facts = card_facts(by_task([run("DEV-1-20260913-100000", started=1, touched=1)])[0])
    assert facts["начато"].endswith("Z")


class FakeTracker:
    """Трекер с памятью на комментарии — ровно столько, сколько нужно учёту."""

    def __init__(self, fail_on=()):
        self.comments: dict[str, list[str]] = {}
        self.asked: list[tuple[str, str]] = []
        self.fail_on = set(fail_on)

    def upsert_comment(self, key, marker, body):
        if key in self.fail_on:
            raise RuntimeError("задача не найдена")
        self.asked.append((key, marker))
        got = self.comments.setdefault(key, [])
        for i, existing in enumerate(got):
            # Правило протокола: своя карточка — та, что НАЧИНАЕТСЯ меткой.
            # Не «метка встречается в теле»: по вхождению писатель принимал за
            # свою карточку чужой комментарий, цитирующий её формат (DEV-18).
            # Настоящий отбор проверяется на самом адаптере, в test_tracking.
            first = next((ln.strip() for ln in existing.splitlines()
                          if ln.strip()), "")
            if first == marker:
                got[i] = body
                return "поправлен"
        got.append(body)
        return "создан"


def test_the_ledger_updates_its_card_instead_of_breeding_a_second():
    """Задача с тремя заходами не должна обрастать тремя карточками: человек
    прочитает историю пересчётов вместо итога."""
    tracker = FakeTracker()
    ledger = Ledger(tracker, runs.log)
    rows = by_task([run("DEV-62-20260912-030000", cost=9.15, started=1)])
    assert ledger.write(rows) == 1
    rows = by_task([run("DEV-62-20260912-030000", cost=9.15, started=1),
                    run("DEV-62-20260912-050000", cost=1.20, started=2)])
    assert ledger.write(rows) == 1
    assert len(tracker.comments["DEV-62"]) == 1
    assert "$10.35" in tracker.comments["DEV-62"][0]


def test_the_ledger_marks_its_card_by_the_line_it_starts_with():
    """Метка, по которой трекер ищет свою карточку, обязана быть первой
    строкой ЭТОГО тела и нести номер задачи. Если она разъедется с телом,
    писатель перестанет находить свою карточку и начнёт плодить вторую; если
    в ней окажется ключ машиночитаемого блока — найдёт чужую (DEV-18)."""
    tracker = FakeTracker()
    rows = by_task([run("DEV-62-20260912-030000", cost=9.15, started=1)])
    Ledger(tracker, runs.log).write(rows)
    (task, marker), = tracker.asked
    assert task == "DEV-62"
    assert marker == "### 🧾 Учёт: DEV-62"
    assert tracker.comments["DEV-62"][0].startswith(marker)
    assert '"учёт": 1' not in marker


def test_a_running_task_gets_no_card_yet():
    tracker = FakeTracker()
    rows = by_task([run("DEV-1-20260913-100000", going=True)])
    assert Ledger(tracker, runs.log).write(rows) == 0
    assert tracker.comments == {}


def test_one_unreachable_task_does_not_lose_the_rest(caplog):
    tracker = FakeTracker(fail_on={"DEV-1"})
    rows = by_task([run("DEV-1-20260913-100000", started=1), run("DEV-2-20260913-110000", started=2)])
    assert Ledger(tracker, runs.log).write(rows) == 1
    assert "DEV-2" in tracker.comments


# ── команда ─────────────────────────────────────────────────────────────────

class FakeTransport:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, **kw):
        self.sent.append((str(chat_id), text))


def work_root(tmp_path, name, *, cost=1.0, outcome="VERIFIED", age=HOUR):
    d = tmp_path / name
    d.mkdir()
    (d / "transcript.jsonl").write_text(json.dumps({
        "type": "result", "total_cost_usd": cost, "num_turns": 3,
        "duration_ms": 60000,
        "usage": {"input_tokens": 1, "output_tokens": 2,
                  "cache_read_input_tokens": 3}}) + "\n")
    (d / "stats.json").write_text(json.dumps(
        {"задача": name.split("-2026")[0], "род": "work", "слот": 1,
         "секунд": 60, "пик_мб": 400, "код": 0, "исход": outcome},
        ensure_ascii=False))
    was = time.time() - age
    import os
    os.utime(d / "transcript.jsonl", (was, was))
    return d


def test_the_command_answers_with_a_digest_and_saves_the_cards(tmp_path):
    work_root(tmp_path, "DEV-77-20260913-100000")
    transport, tracker = FakeTransport(), FakeTracker()
    journal = Journal(str(tmp_path), tracker=tracker, transport=transport)
    assert asyncio.run(journal.command("42", "")) == "ok"
    text = transport.sent[0][1]
    assert "За сутки" in text
    assert "Карточек учёта в трекере обновлено: 1" in text
    assert "DEV-77" in tracker.comments


def test_the_week_window_is_asked_for_by_word(tmp_path):
    work_root(tmp_path, "DEV-77-20260913-100000", age=3 * runs.DAY)
    transport = FakeTransport()
    journal = Journal(str(tmp_path), transport=transport)
    assert asyncio.run(journal.command("42", "")) == "ok"
    assert "запусков не было" in transport.sent[0][1]
    transport.sent.clear()
    assert asyncio.run(journal.command("42", "неделя")) == "ok"
    assert "За неделю" in transport.sent[0][1]


def test_an_unknown_period_is_refused_instead_of_silently_meaning_a_day(tmp_path):
    """Человек спрашивал не то, что получит, и узнает об этом, только сверив
    числа. Дешевле сказать сразу."""
    transport = FakeTransport()
    journal = Journal(str(tmp_path), transport=transport)
    assert asyncio.run(journal.command("42", "месяц")) == "unknown-window"
    assert "Не понял период" in transport.sent[0][1]


def test_without_a_work_root_the_command_says_so_instead_of_showing_zeros():
    transport = FakeTransport()
    journal = Journal("", transport=transport)
    assert asyncio.run(journal.command("42", "")) == "no-work-root"
    assert "не настроен" in transport.sent[0][1]


def test_the_card_keeps_the_whole_task_even_when_the_window_is_a_day(monkeypatch):
    """Ключевое свойство: дневная сводка НЕ пересчитывает карточку по одним
    сегодняшним заходам. Иначе вчерашние $9,15 исчезли бы, а осталось $1,20 —
    ровно та потеря цены, ради которой единицей учёта стала задача.

    Отсечку подменяем в `walk`, а не строим каталогами: он отбирает по времени
    СОЗДАНИЯ каталога, а созданный в тесте каталог всегда сегодняшний —
    сколько ему ни правь mtime. На таком стенде окно не отсекает ничего, и
    проверка проходила бы и со сломанным журналом.
    """
    now = time.time()
    every = [run("DEV-62-20260910-100000", cost=9.15, started=now - 3 * runs.DAY,
                 touched=now - 3 * runs.DAY),
             run("DEV-62-20260913-100000", cost=1.20, started=now - HOUR,
                 touched=now - HOUR)]
    monkeypatch.setattr(runs.sess, "walk",
                        lambda root, since=0.0: [s for s in every
                                                 if s["started"] >= since])
    tracker = FakeTracker()
    journal = Journal("/work", tracker=tracker, transport=FakeTransport())
    asyncio.run(journal.command("42", ""))
    assert "$10.35" in tracker.comments["DEV-62"][0]


def test_the_digest_goes_to_the_notification_chat_and_the_asker_is_told(tmp_path):
    """Сводка — общий факт фабрики. Но спросивший в личке обязан получить
    ответ туда, где спросил: молчание он прочитает как поломку бота."""
    work_root(tmp_path, "DEV-77-20260913-100000")
    transport = FakeTransport()
    journal = Journal(str(tmp_path), transport=transport, notify_chat="-100")
    asyncio.run(journal.command("42", ""))
    where = dict((chat, text) for chat, text in transport.sent)
    assert "За сутки" in where["-100"]
    assert "общий чат" in where["42"]


def test_without_a_tracker_the_digest_is_still_built(tmp_path):
    work_root(tmp_path, "DEV-77-20260913-100000")
    transport = FakeTransport()
    journal = Journal(str(tmp_path), transport=transport)
    assert asyncio.run(journal.command("42", "")) == "ok"
    assert "Карточек учёта" not in transport.sent[0][1]


def test_a_broken_tracker_does_not_take_the_digest_down_with_it(tmp_path, caplog):
    work_root(tmp_path, "DEV-77-20260913-100000")

    class Broken:
        def upsert_comment(self, *a):
            raise RuntimeError("трекер не отвечает")

    transport = FakeTransport()
    journal = Journal(str(tmp_path), tracker=Broken(), transport=transport)
    assert asyncio.run(journal.command("42", "")) == "ok"
    assert "За сутки" in transport.sent[0][1]


# ── круги по следам на диске (DEV-86) ────────────────────────────────────────

def _session_dir(root, name, cost=1.0):
    d = root / name
    d.mkdir()
    (d / "transcript.jsonl").write_text(json.dumps({"type": "result", "total_cost_usd": cost}))
    return d


def test_work_rounds_count_only_the_work_entrances(tmp_path):
    for name in ("DEV-4-20260913-211228", "DEV-4-20260913-214657", "DEV-4-20260914-003841",
                 "DEV-4-verify-20260913-214614", "DEV-4-verify-20260914-021516",
                 "DEV-40-20260913-000000", "INTAKE-abc-intake-20260911-062037"):
        _session_dir(tmp_path, name)
    assert runs.work_rounds(str(tmp_path), "DEV-4") == 3
    assert runs.work_rounds(str(tmp_path), "DEV-40") == 1
    assert runs.work_rounds(str(tmp_path), "DEV-5") == 0
    assert runs.work_rounds("", "DEV-4") == 0


def test_rounds_exhausted_is_the_count_at_or_over_the_cap(tmp_path):
    for name in ("DEV-4-20260913-211228", "DEV-4-20260913-214657", "DEV-4-20260914-003841"):
        _session_dir(tmp_path, name)
    assert runs.rounds_exhausted(str(tmp_path), "DEV-4", limit=3) == 3
    assert runs.rounds_exhausted(str(tmp_path), "DEV-4", limit=4) == 0
    # Потолок 0 — потолка нет.
    assert runs.rounds_exhausted(str(tmp_path), "DEV-4", limit=0) == 0


def test_the_default_cap_is_three_rounds():
    assert runs.MAX_WORK_ROUNDS == 3


def test_consequences_of_a_task_are_summed_by_reason():
    """Журнал DEV-18 отвечает на два вопроса раздельно: сколько работа
    вскрыла и сколько внесла сама. Это противоположные оценки, и одно число
    на оба ответа не годится."""
    sessions = [
        {"name": "DEV-56-1", "task": "DEV-56", "started": 10, "touched": 20,
         "род": "работа", "исход": "READY_FOR_HUMAN",
         "шлюз": {"вердикт": "тяжёлая", "признаки": ["прод"],
                  "последствия": {"найдено": 2, "внесено": 1, "всего": 3}}},
        {"name": "DEV-56-2", "task": "DEV-56", "started": 30, "touched": 40,
         "род": "работа", "исход": "READY_FOR_HUMAN",
         "шлюз": {"вердикт": "лёгкая", "признаки": [],
                  "последствия": {"найдено": 0, "внесено": 1, "всего": 1}}},
    ]
    (row,) = runs.by_task(sessions)
    assert row["последствий найдено"] == 2 and row["последствий внесено"] == 2
    assert row["тяжёлая"] is True
    d = runs.digest([row], since=0, until=100)
    assert d["тяжёлых"] == 1
    assert d["последствий найдено"] == 2 and d["последствий внесено"] == 2
    assert "Последствий найдено: 2, внесено самой правкой: 2" in runs.digest_text(d)


def test_a_task_without_the_gate_is_not_counted_as_light():
    """Заход раннера прежней версии шлюза не писал. Такая задача не тяжёлая
    и не лёгкая — про неё просто нечего сказать, и строка шлюза в сводке не
    печатается вовсе, чтобы нули не приучали её пролистывать."""
    sessions = [{"name": "DEV-1-1", "task": "DEV-1", "started": 10, "touched": 20,
                 "род": "работа", "исход": "READY_FOR_HUMAN"}]
    (row,) = runs.by_task(sessions)
    assert row["тяжёлая"] is False and row["последствий найдено"] == 0
    d = runs.digest([row], since=0, until=100)
    assert "Последствий найдено" not in runs.digest_text(d)


# ── заходы задачи для показа: значок, исход, цена ────────────────────────────

def test_each_entrance_has_an_icon_a_word_and_its_own_price():
    """Карточка задачи (DEV-100) обязана отвечать про КАЖДЫЙ заход: чем он
    был, чем кончился и сколько стоил. Одного значка мало, одного слова мало:
    смотрящий с телефона решает по строке целиком."""
    rows = by_task([run("DEV-9-20260915-100000", cost=2.0, started=1, secs=600),
                    run("DEV-9-verify-20260915-110000", kind="verify", cost=0.5,
                        started=2, secs=60, outcome="VERIFIED", mode="deploy"),
                    run("DEV-9-20260915-120000", cost=1.0, started=3, secs=300,
                        outcome="FAILED", stop="упёрлась в ходы")])
    got = runs.stages(rows[0])
    assert [s["этап"] for s in got] == ["работа", "проверка", "доработка"]
    assert [s["значок"] for s in got] == ["🛠", "🔬", "♻️"]
    assert [s["usd"] for s in got] == [2.0, 0.5, 1.0]
    assert got[1]["чем кончился"] == "выкат проверен"
    assert got[2]["остановка"] == "упёрлась в ходы"


def test_every_known_stage_has_an_icon_of_its_own():
    """Значок «•» означает «род неизвестен», и знакомому роду он врёт. Слова
    и значки заводят в разных задачах (`merge` пришёл на DEV-109, значки — на
    DEV-100), и без этой сверки новый род тихо получает чужой смысл."""
    known = sorted(set(runs.STAGE_WORDS.values()))
    without = [w for w in known if w not in runs.STAGE_ICONS]
    assert not without, f"этапы без своего значка: {without}"
    assert len(set(runs.STAGE_ICONS[w] for w in known)) == len(known), (
        "два этапа с одним значком — значок перестаёт отличать")


def test_a_checked_branch_is_not_called_a_checked_deployment():
    """`VERIFIED` на проверке ВЕТКИ — не «выкат проверен»: работа не
    смержена и до прода не доезжала (DEV-115).

    Карточка учёта живёт в трекере месяцами, и подпись в ней — то, по чему
    задачу вспоминают. «Выкат проверен» на непомерженной ветке приучает
    считать проверенным то, чего не было.
    """
    rows = by_task([run("DEV-9-verify-20260916-100000", kind="verify",
                        started=1, outcome="VERIFIED", mode="branch")])
    got = runs.stages(rows[0])
    assert got[0]["чем кончился"] == "ветка проверена"
    body = runs.card_body(rows[0])
    assert "Исход: ветка проверена" in body and "выкат проверен" not in body


def test_a_checked_deployment_still_says_so():
    """Вторая точка не пострадала: после мержа выкат состоялся, и слово о нём
    честное. Без этой половины правка сводилась бы к «убрать слово»."""
    rows = by_task([run("DEV-9-verify-20260916-100000", kind="verify",
                        started=1, outcome="VERIFIED", mode="deploy")])
    assert runs.stages(rows[0])[0]["чем кончился"] == "выкат проверен"
    assert "выкат проверен" in runs.card_body(rows[0])


def test_without_a_point_the_card_says_only_that_the_check_passed():
    """Заход прежнего раннера точки не писал. «Проверка прошла» — всё, что
    про него известно; «выкат проверен» было бы догадкой."""
    rows = by_task([run("DEV-9-verify-20260916-100000", kind="verify",
                        started=1, outcome="VERIFIED")])
    assert runs.stages(rows[0])[0]["чем кончился"] == "проверка прошла"


def test_an_entrance_of_an_unknown_kind_is_not_called_work():
    """Незнакомый род и работа — разные факты. Значок работы на неизвестном
    заходе врёт ровно там, где человек не сможет это заметить."""
    rows = by_task([run("DEV-9-20260915-100000", kind="перепись", started=1)])
    got = runs.stages(rows[0])
    assert got[0]["этап"] == "перепись" and got[0]["значок"] == runs.UNKNOWN_ICON


def test_an_unmarked_outcome_says_so_instead_of_staying_empty():
    rows = by_task([run("DEV-9-20260915-100000", outcome="", started=1)])
    assert runs.stages(rows[0])[0]["чем кончился"] == "исход не размечен"


def test_the_stage_line_and_the_stage_list_tell_the_same_story():
    """Строка карточки трекера и столбик карточки страницы считаются одним
    местом: разойдутся — два отчёта об одной задаче назовут разные этапы."""
    rows = by_task([run("DEV-9-20260915-100000", started=1),
                    run("DEV-9-20260915-110000", started=2, stop="лимит ходов")])
    line = runs.stages_line(rows[0])
    assert line == "работа → доработка (лимит ходов)"
    assert [s["этап"] for s in runs.stages(rows[0])] == ["работа", "доработка"]


# ── дополнительные пункты: в той же карточке, а не рядом ─────────────────────

def test_the_extra_points_name_what_is_known_beyond_the_entrances():
    first = run("DEV-9-20260915-100000", started=1000, touched=1600, secs=600)
    first["шлюз"] = {"вердикт": "тяжёлая", "последствия": {"найдено": 3}}
    rows = by_task([first,
                    run("DEV-9-20260915-110000", started=5000, touched=5300, secs=300)])
    got = runs.extras(rows[0])
    assert "переделка: рабочих заходов 2" in got
    assert "шлюз: тяжёлая" in got
    assert "последствий найдено 3" in got
    assert any(x.startswith("срок ") for x in got)


def test_a_point_nobody_counted_is_not_shown_as_zero():
    """«Последствий найдено 0» и «последствий никто не считал» с виду
    одинаковы, а это разные факты — второе не имеет права выглядеть первым."""
    rows = by_task([run("DEV-9-20260915-100000", started=1, touched=1)])
    assert runs.extras(rows[0]) == []


# ── куда кладётся оценка задачи ─────────────────────────────────────────────

def test_a_mark_lands_on_the_entrance_that_already_holds_it():
    """Оценку хранит заход, а ставят её на карточке задачи. Если положить
    новую поверх СВЕЖЕГО захода, снятие оценки не сработает: старая останется
    лежать на прежнем заходе и читаться дальше."""
    rows = by_task([run("DEV-9-20260915-100000", started=1),
                    run("DEV-9-20260915-110000", started=2)],
                   {"DEV-9-20260915-100000": {"оценка": "ок"}})
    assert rows[0]["оценка"] == "ок"
    assert rows[0]["оценить"] == "DEV-9-20260915-100000"


def test_without_a_mark_it_lands_on_the_latest_entrance():
    rows = by_task([run("DEV-9-20260915-100000", started=1),
                    run("DEV-9-20260915-110000", started=2)])
    assert rows[0]["оценить"] == "DEV-9-20260915-110000"


def test_two_entrances_of_one_second_are_ordered_by_name():
    """`sessions.walk` берёт время старта из ctime каталога, и у двух заходов
    одной секунды оно совпадает. Тогда порядок решала бы файловая система — а
    от него зависит, какой заход последний, то есть куда ляжет оценка задачи.
    То же правило, что у `last_verify` и `evidence.last_checks`."""
    rows = by_task([run("DEV-9-20260915-200000", started=5, touched=5),
                    run("DEV-9-20260915-100000", started=5, touched=5)])
    assert rows[0]["оценить"] == "DEV-9-20260915-200000"
    rows = by_task([run("DEV-9-20260915-100000", started=5, touched=5),
                    run("DEV-9-20260915-200000", started=5, touched=5)])
    assert rows[0]["оценить"] == "DEV-9-20260915-200000"


def test_a_running_entrance_is_not_called_an_unmarked_outcome():
    """«Некому было записать исход» и «заход ещё идёт» — разные факты. Первый
    требует объяснения, второй нет, и путать их на карточке нельзя."""
    rows = by_task([run("DEV-9-20260915-100000", outcome="", going=True, started=1)])
    assert runs.stages(rows[0])[0]["чем кончился"] == "идёт сейчас"
    rows = by_task([run("DEV-9-20260915-100000", outcome="", dropped=True, started=1)])
    assert runs.stages(rows[0])[0]["чем кончился"] == "брошена"
