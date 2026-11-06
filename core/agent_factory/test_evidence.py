"""Свидетельства проверки для страницы подписи (DEV-94).

Половина тестов — про пустое: «свидетельств нет» обязано быть сказано
словами, а битый журнал — не то же, что пустой. Вторая половина — про
сверку по строкам: пункт без ответа виден, лишний ответ виден.
"""
import json
import os

import gatelog
from agent_factory import evidence as ev
from agent_factory import runs


# ── фикстуры ─────────────────────────────────────────────────────────────

def verify_dir(root, ident="DEV-1", stamp="20260914-233827", stats=None,
               gates=None, expected=None, verdict=None, running=False):
    d = os.path.join(str(root), f"{ident}-verify-{stamp}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "transcript.jsonl"), "w") as f:
        if not running:
            f.write(json.dumps({"type": "result", "total_cost_usd": 0.5,
                                "num_turns": 3, "duration_ms": 1000,
                                "usage": {}}) + "\n")
        else:
            f.write("{}\n")
    if stats is not None:
        with open(os.path.join(d, "stats.json"), "w") as f:
            json.dump({"род": "verify", **stats}, f, ensure_ascii=False)
    if gates is not None:
        with open(os.path.join(d, "gates.json"), "w") as f:
            f.write(gates if isinstance(gates, str)
                    else json.dumps(gates, ensure_ascii=False))
    if expected is not None:
        with open(os.path.join(d, "expected.md"), "w") as f:
            f.write(expected)
    if verdict is not None:
        with open(os.path.join(d, "verdict.txt"), "w") as f:
            f.write(verdict)
    return d


GATES = [
    {"ворота": "logdiff", "итог": "не прошло",
     "команда": "python3 logdiff.py --since 10m api", "код": 10,
     "заметка": "", "источник": "сессия", "когда": 2.0},
    {"ворота": "pytest", "итог": "прошло", "команда": "python3 -m pytest -q",
     "код": 0, "заметка": "", "источник": "сессия", "когда": 3.0},
]

EXPECTED = """Источник критерия: задача

1. **место** — `GET /health` на dev-стенде
   **команда** — `curl -fsS http://127.0.0.1:3002/health`
   **ожидаемое значение** — 200 и `{"status":"ok"}`
   **чем покраснеет** — 502 от Caddy

2. место: журнал юнита
   команда: python3 logdiff.py --since 10m
   ожидаемое значение: НОВОЕ 0 видов
   чем покраснеет: строка Traceback

3. место: stats.json сессии
   команда: cat stats.json
   ожидаемое значение: поле класс
   чем покраснеет: поля нет
"""

VERDICT = """OK всё проверено против критерия
- /health: видел 200 {"status":"ok"}, сошлось
- журнал: видел НОВОЕ 1 вид (Traceback), не сошлось
проверено поведение — ручка и журнал
"""


# ── ворота ───────────────────────────────────────────────────────────────

def test_the_words_are_the_gatelog_words():
    """Пакет приёма `gatelog` не импортирует — слова скопированы. Разошлись
    бы молча: страница показывала бы «прошло» жёлтым."""
    assert (ev.PASSED, ev.FAILED, ev.ERROR, ev.NO_EVIDENCE) == (
        gatelog.PASSED, gatelog.FAILED, gatelog.ERROR, gatelog.NO_EVIDENCE)
    assert ev.GATES_FILE == gatelog.GATES_FILE
    # Шкалы инструментов — тоже копия (DEV-104): разойдись они, карточка
    # звала бы находку провалом или наоборот.
    assert ev.CODES == gatelog.CODES and ev.FINDINGS == gatelog.FINDINGS


def test_the_judge_agrees_with_gatelog_on_every_shape():
    """Суд — копия `gatelog.judge`; на одном журнале оба обязаны сказать
    одно и то же, включая порядок недоверия."""
    shapes = (
        [], GATES, GATES[1:],
        GATES[1:] + [{"ворота": "health", "итог": "ошибка обвязки", "команда": "",
                      "код": None, "заметка": "команда не запустилась", "источник": "сессия"}],
        GATES + [{"ворота": "x", "итог": "ошибка обвязки", "команда": "", "код": None,
                  "заметка": "", "источник": "сессия"}],
        # Отменённая перезапуском попытка и её замена (DEV-107), и та же
        # отметка задним числом — суды обязаны совпасть и здесь.
        [{"ворота": "pytest", "итог": "не прошло", "код": 1, "начата": 10.0,
          "когда": 20.0, "отменена": 30.0},
         {"ворота": "pytest", "итог": "прошло", "код": 0, "начата": 30.0, "когда": 40.0}],
        [{"ворота": "pytest", "итог": "не прошло", "код": 1, "начата": 10.0,
          "когда": 20.0, "отменена": 100.0},
         {"ворота": "pytest", "итог": "прошло", "код": 0, "начата": 30.0, "когда": 40.0}],
        # Журнал прежней сессии: `начата` записи не знали вовсе, есть только
        # `когда`. Запасной ход обязан быть одинаковым в обеих копиях суда.
        [{"ворота": "pytest", "итог": "не прошло", "код": 1, "когда": 20.0,
          "отменена": 25.0},
         {"ворота": "pytest", "итог": "прошло", "код": 0, "когда": 40.0}],
        # Контрольные ворота: объявленное ожидание зачтено, не зачтено (код
        # не тот), объявлено задним числом — три разных приговора (DEV-104).
        [{"ворота": "proba-main", "итог": "прошло", "код": 3, "начата": 10.0,
          "когда": 11.0, "ожидание": 3}],
        [{"ворота": "proba-main", "итог": "не прошло", "код": 0, "начата": 10.0,
          "когда": 11.0, "ожидание": 3}],
        [{"ворота": "pytest", "итог": "не прошло", "код": 1, "когда": 11.0},
         {"ворота": "pytest", "итог": "прошло", "код": 1, "начата": 20.0,
          "когда": 21.0, "ожидание": 1}],
        [{"ворота": "pytest", "итог": "прошло", "код": 1, "ожидание": 1}],
        # Прежняя попытка кончилась ошибкой обвязки — кода не показала, и
        # права объявить ожидание не отнимает (правка по код-ревью DEV-104).
        [{"ворота": "proba", "итог": "ошибка обвязки", "код": None,
          "начата": 1.0, "когда": 2.0},
         {"ворота": "proba", "итог": "прошло", "код": 3, "начата": 10.0,
          "когда": 11.0, "ожидание": 3}],
        # Находка инструмента: без разбора, с разбором, с разбором до неё.
        [{"ворота": "logdiff", "итог": "не прошло", "код": 10, "когда": 20.0}],
        [{"ворота": "logdiff", "итог": "не прошло", "код": 10, "когда": 20.0},
         {"ворота": "razbor", "итог": "прошло", "заметка": "фон",
          "разбирает": "logdiff", "начата": 30.0, "когда": 31.0}],
        [{"ворота": "logdiff", "итог": "не прошло", "код": 10, "когда": 20.0},
         {"ворота": "razbor", "итог": "прошло", "заметка": "фон",
          "разбирает": "logdiff", "начата": 1.0, "когда": 2.0}],
        [{"ворота": "pytest", "итог": "не прошло", "код": 1, "когда": 20.0},
         {"ворота": "razbor", "итог": "прошло", "заметка": "да ну его",
          "разбирает": "pytest", "начата": 30.0, "когда": 31.0}],
    )
    for raw in shapes:
        theirs = gatelog.judge([gatelog.Gate.from_dict(r) for r in raw])
        ours = ev.judge([ev._gate(r) for r in raw])
        assert ours == theirs, raw


def test_the_runner_judgement_in_stats_wins_over_the_file(tmp_path):
    """По разбору раннера считался исход — показываем его, а не свою
    вторую правду. В нём и ворота постановки, которых в файле нет."""
    stats = {"ворота сессии": {"итог": "не прошло", "почему": "упали ворота: logdiff (код 10)",
                               "записи": [{"ворота": "ворота постановки", "итог": "прошло",
                                           "источник": "постановка"}] + GATES}}
    d = verify_dir(tmp_path, stats=stats, gates=GATES[1:])
    got = ev.gates_of(d, stats)
    assert got["итог"] == "не прошло"
    assert [g["ворота"] for g in got["записи"]] == ["ворота постановки", "logdiff", "pytest"]
    assert got["записи"][1]["код"] == 10


def test_without_the_runner_judgement_the_file_is_read(tmp_path):
    """Прод на раннере до DEV-88: поля в stats.json нет. Файл всё равно
    читается сам — если он есть."""
    d = verify_dir(tmp_path, stats={"исход": "FAILED"}, gates=GATES)
    got = ev.gates_of(d, {"исход": "FAILED"})
    assert got["итог"] == "не прошло" and "logdiff (код 10)" in got["почему"]
    assert got["записи"][0]["команда"] == "python3 logdiff.py --since 10m api"


def test_no_file_and_no_judgement_is_no_evidence(tmp_path):
    d = verify_dir(tmp_path, stats={"исход": "FAILED"})
    assert ev.gates_of(d, {"исход": "FAILED"}) is None


def test_an_empty_journal_is_no_evidence_too(tmp_path):
    """Пустой список записей — то же «нет свидетельств», что и отсутствие
    файла: разбор раннера так и говорит, и таблица с нулём строк молчала бы."""
    d = verify_dir(tmp_path, stats={}, gates=[])
    assert ev.gates_of(d, {}) is None
    stats = {"ворота сессии": {"итог": "нет свидетельств", "почему": "…", "записи": []}}
    assert ev.gates_of(d, stats) is None


def test_a_broken_journal_is_a_harness_error_not_silence(tmp_path):
    """Битый gates.json — ОДНА запись «ошибка обвязки». Слить его с «нет
    свидетельств» значит потерять задокументированный источник ложных OK."""
    d = verify_dir(tmp_path, stats={}, gates="{not json")
    got = ev.gates_of(d, {})
    assert got["итог"] == ev.ERROR
    assert len(got["записи"]) == 1 and "не разобран" in got["записи"][0]["заметка"]


def test_an_unknown_status_is_not_passed(tmp_path):
    got = ev._gate({"ворота": "x", "итог": "ok", "код": 0})
    assert got["итог"] == ev.ERROR and "не разобран" in got["заметка"]


# ── эталон и построчный ответ ────────────────────────────────────────────

def test_the_reference_is_split_into_items_with_four_fields():
    items = ev.expected_items(EXPECTED)
    assert len(items) == 3
    assert items[0]["место"] == "GET /health на dev-стенде"
    assert items[0]["команда"] == "curl -fsS http://127.0.0.1:3002/health"
    assert items[0]["ожидаемое"] == '200 и {"status":"ok"}'
    assert items[0]["покраснеет"] == "502 от Caddy"
    assert items[1]["место"] == "журнал юнита" and items[2]["покраснеет"] == "поля нет"


def test_a_reference_written_as_a_table_is_read_too():
    text = ("Источник критерия: запасной\n\n"
            "| место | команда | ожидаемое | чем покраснеет |\n"
            "| -- | -- | -- | -- |\n"
            "| /health | curl /health | 200 | 502 |\n"
            "| журнал | logdiff | 0 видов | Traceback |\n")
    items = ev.expected_items(text)
    assert [i["место"] for i in items] == ["/health", "журнал"]
    assert items[1]["ожидаемое"] == "0 видов"
    assert ev.criterion_source(text) == "запасной"


def test_the_criterion_source_reads_the_runner_forms():
    assert ev.criterion_source("Источник критерия: задача\n") == "задача"
    assert ev.criterion_source("- **Источник критерия:** нет\n") == "нет"
    assert ev.criterion_source("критерий взят из задачи\n") == ""


def test_verdict_rows_keep_only_the_answers():
    """Первая строка — вердикт; ответ — строка со словом контракта. Проза
    («проверено поведение — …») в сверку не идёт."""
    head, rows = ev.verdict_rows(VERDICT)
    assert head.startswith("OK всё проверено")
    assert [r["ответ"] for r in rows] == ["сошлось", "не сошлось"]
    assert rows[0]["текст"].startswith("/health: видел 200")


def test_the_answer_word_is_read_in_its_forms():
    assert ev.answer_of("x: видел 1, не сошлось") == ev.MISMATCHED
    assert ev.answer_of("x: видел 1, сошлось") == ev.MATCHED
    assert ev.answer_of("x: не проверял — стенд занят") == ev.UNCHECKED
    assert ev.answer_of("байты сошлись") == ev.MATCHED
    assert ev.answer_of("байты не сошлись") == ev.MISMATCHED
    assert ev.answer_of("проверено поведение — ручка") == ""


def test_an_item_without_an_answer_line_is_visible():
    """Ради этого построчно: пропущенный пункт виден строкой «без ответа»,
    а не прячется за абзацем."""
    items = ev.expected_items(EXPECTED)
    _, rows = ev.verdict_rows(VERDICT)
    got = ev.pair(items, rows)
    assert len(got) == 3
    assert [r["ответ"] for r in got] == ["сошлось", "не сошлось", "без ответа"]
    assert got[2]["место"] == "stats.json сессии" and got[2]["текст"] == ""


def test_an_answer_without_an_item_is_visible_too():
    items = ev.expected_items(EXPECTED)[:1]
    _, rows = ev.verdict_rows(VERDICT)
    got = ev.pair(items, rows)
    assert [r["ответ"] for r in got] == ["сошлось", ev.NO_ITEM]
    assert got[1]["место"] == "" and got[1]["текст"].startswith("журнал:")


# ── какая проверка и всё вместе ──────────────────────────────────────────

def test_last_checks_agree_with_runs_last_verify(tmp_path, monkeypatch):
    """Правило «последняя состоявшаяся» одно на двоих: `runs.last_verify`
    ходит по одной задаче, здесь — обход на все. Расхождение — две страницы
    показывают разные проверки одной задачи.

    Обход подменён: сортировка `sessions.walk` идёт по ctime каталога, а он
    у созданных в одну секунду одинаков (DEV-89 на хосте падал ровно так).
    Здесь проверяется правило отбора, а не сортировка."""
    from agent_factory import sessions
    fake = [                                          # свежие первыми
        {"name": "DEV-1-verify-20260914-120000", "task": "DEV-1", "род": "verify",
         "идёт": True, "исход": ""},
        {"name": "DEV-1-verify-20260914-110000", "task": "DEV-1", "род": "verify",
         "идёт": False, "исход": "CANCELLED"},
        {"name": "DEV-1-20260914-103000", "task": "DEV-1", "род": "работа",
         "идёт": False, "исход": "READY_FOR_HUMAN"},
        {"name": "DEV-1-verify-20260914-100000", "task": "DEV-1", "род": "verify",
         "идёт": False, "исход": "FAILED"},
        {"name": "DEV-1-verify-20260914-090000", "task": "DEV-1", "род": "verify",
         "идёт": False, "исход": "VERIFIED"},
    ]
    monkeypatch.setattr(sessions, "walk", lambda root, since=0.0: list(fake))
    theirs = runs.last_verify(str(tmp_path), "DEV-1")
    ours = ev.last_checks(str(tmp_path), ["DEV-1"])
    assert theirs["name"] == ours["DEV-1"]["name"] == "DEV-1-verify-20260914-100000"


def test_both_break_a_same_second_tie_by_the_stamp_in_the_name(tmp_path, monkeypatch):
    """Два каталога одной секунды: обход отдал их в обратном порядке. Оба
    места обязаны взять тот, у кого свежее штамп в имени, — иначе ворота
    держат блокировку по одной проверке, а карточка показывает другую."""
    from agent_factory import sessions
    fake = [
        {"name": "DEV-1-verify-20260914-090000", "task": "DEV-1", "род": "verify",
         "идёт": False, "исход": "FAILED", "started": 1700.0, "touched": 1700.0},
        {"name": "DEV-1-verify-20260914-100000", "task": "DEV-1", "род": "verify",
         "идёт": False, "исход": "VERIFIED", "started": 1700.0, "touched": 1700.0},
    ]
    monkeypatch.setattr(sessions, "walk", lambda root, since=0.0: list(fake))
    theirs = runs.last_verify(str(tmp_path), "DEV-1")
    ours = ev.last_checks(str(tmp_path), ["DEV-1"])
    assert theirs["name"] == ours["DEV-1"]["name"] == "DEV-1-verify-20260914-100000"


def test_collect_reads_everything_a_card_needs(tmp_path):
    verify_dir(tmp_path, stats={"исход": "READY_FOR_HUMAN", "критерий по воротам": "задача",
                                "ворота сессии": {"итог": "не прошло", "почему": "упали ворота: logdiff (код 10)",
                                                  "записи": GATES}},
               gates=GATES, expected=EXPECTED, verdict=VERDICT)
    got = ev.collect(str(tmp_path), ["DEV-1", "DEV-2"])
    assert got["DEV-2"] is None
    p = got["DEV-1"]
    assert p["сессия"] == "DEV-1-verify-20260914-233827"
    assert p["вердикт"].startswith("OK") and p["исход"] == "READY_FOR_HUMAN"
    assert p["ворота"]["итог"] == "не прошло"
    assert p["эталон"]["источник"] == "задача" and p["критерий по воротам"] == "задача"
    assert [r["ответ"] for r in p["сверка"]] == ["сошлось", "не сошлось", "без ответа"]
    json.dumps(p, ensure_ascii=False)              # едет в /data как есть


def test_a_check_without_any_file_is_all_empty_by_name(tmp_path):
    """Прод 15.09.2026: 66 проверок, ни одного gates.json и expected.md.
    Каждая пустота названа отдельно — карточка скажет, чего именно нет."""
    verify_dir(tmp_path, stats={"исход": "FAILED"}, verdict="FAIL одно\n")
    p = ev.collect(str(tmp_path), ["DEV-1"])["DEV-1"]
    assert p["ворота"] is None and p["эталон"] is None and p["сверка"] == []
    assert p["вердикт"] == "FAIL одно"


def test_a_reference_that_does_not_parse_is_kept_as_text(tmp_path):
    verify_dir(tmp_path, stats={}, expected="Источник критерия: нет\n\nпроверять нечего\n",
               verdict="PARTIAL НЕЧЕГО критерий не определён\n")
    p = ev.collect(str(tmp_path), ["DEV-1"])["DEV-1"]
    assert p["эталон"]["пункты"] == [] and "проверять нечего" in p["эталон"]["текст"]
    assert p["эталон"]["источник"] == "нет"


def test_no_work_root_means_nothing_not_a_crash():
    assert ev.collect("", ["DEV-1"]) == {"DEV-1": None}
    assert ev.last_checks("/nonexistent/path", ["DEV-1"]) == {}


# ── отменённая попытка на карточке: видна, но исход не понижает (DEV-107) ──

CANCELLED = [{"ворота": "pytest", "итог": "не прошло", "команда": "pytest core/",
              "код": 1, "начата": 10.0, "когда": 20.0, "отменена": 30.0},
             {"ворота": "pytest", "итог": "прошло", "команда": "pytest core/",
              "код": 0, "начата": 30.0, "когда": 40.0}]


def test_a_replaced_attempt_is_shown_but_does_not_redden_the_card(tmp_path):
    """Человек подписывает по карточке: красная строка обязана остаться
    видимой (перезапуск не прячут), но итог по ней не считается."""
    d = verify_dir(tmp_path, stats={}, gates=CANCELLED)
    got = ev.gates_of(d, {})
    assert got["итог"] == ev.PASSED and "отменённых попыток: 1" in got["почему"]
    assert [r["заменена"] for r in got["записи"]] == [True, False]


def test_a_cancellation_stamped_after_the_replacement_is_not_one(tmp_path):
    late = [{**CANCELLED[0], "отменена": 100.0}, CANCELLED[1]]
    d = verify_dir(tmp_path, stats={}, gates=late)
    got = ev.gates_of(d, {})
    assert got["итог"] == ev.FAILED
    assert [r["заменена"] for r in got["записи"]] == [False, False]


# ── контроль и находки на карточке (DEV-104) ─────────────────────────────

CONTROL = [
    {"ворота": "proba-main", "итог": "не прошло", "команда": "python3 proba.py",
     "код": 3, "начата": 10.0, "когда": 11.0, "ожидание": 3},
    {"ворота": "logdiff", "итог": "не прошло", "команда": "python3 logdiff.py",
     "код": 10, "когда": 20.0},
    {"ворота": "razbor-logdiff", "итог": "прошло", "заметка": "фон: чужая подсистема",
     "разбирает": "logdiff", "начата": 30.0, "когда": 31.0},
]


def test_a_control_gate_is_marked_apart_from_a_failure():
    """Человек обязан различать «ворота упали» и «контроль сработал как
    задумано»: без пометки карточка пугает красным там, где всё правильно."""
    records = ev._marked([ev._gate(r) for r in CONTROL])
    control, finding, resolution = records
    assert control["контроль"] and control["ожидание"] == 3
    assert not finding["контроль"] and finding["разобрана"]
    assert not resolution["разобрана"] and resolution["разбирает"] == "logdiff"
    assert ev.judge(records)[0] == ev.PASSED


def test_a_declaration_made_after_the_fact_is_not_marked_as_control(tmp_path):
    """Незачтённое ожидание контролем не помечается — иначе карточка
    успокаивала бы там, где исход понижен."""
    raw = [{"ворота": "pytest", "итог": "не прошло", "код": 1, "когда": 11.0},
           {"ворота": "pytest", "итог": "прошло", "код": 1, "начата": 20.0,
            "когда": 21.0, "ожидание": 1}]
    records = ev._marked([ev._gate(r) for r in raw])
    assert [r["контроль"] for r in records] == [False, False]
    assert ev.judge(records)[0] == ev.FAILED
