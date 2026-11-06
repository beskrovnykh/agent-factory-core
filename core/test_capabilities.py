"""Список выключенных способностей (DEV-118).

Предмет проверки — не «печатается ли что-нибудь», а четыре обещания, ради
которых список и заводили:

1. выключенная способность НАЗВАНА (кнопки мержа не было месяц, потому что
   никто про неё не сказал);
2. названа СЛЕДСТВИЕМ, а не именем переменной;
3. выключенное не считается ошибкой — код возврата не меняется;
4. переменная, которую список называет, — та самая, которую читает продукт
   (иначе список протухнет на первом переименовании и будет врать уверенно).
"""
import re
from pathlib import Path

import pytest

import capabilities
import knowledge
import runner
from agent_factory import deploy

CORE = Path(__file__).resolve().parent


# ── 1. способность названа ──────────────────────────────────────────────────

def test_a_missing_hosting_source_is_named():
    """Ровно тот случай, с которого началась задача."""
    off = capabilities.disabled({})
    merge = [c for c in off if c["переменная"] == "AGENT_HOSTING_SOURCE"]
    assert merge, "мерж со страницы очереди выключен, а в списке его нет"
    assert merge[0]["причина"] == "не задан AGENT_HOSTING_SOURCE"


def test_a_given_hosting_source_leaves_the_list():
    """Вторая половина критерия: вернули переменную — строка исчезла."""
    env = {"AGENT_HOSTING_SOURCE": "my_installation.hosting:GitHub"}
    assert not [c for c in capabilities.disabled(env)
                if c["переменная"] == "AGENT_HOSTING_SOURCE"]


def test_a_source_without_a_class_is_disabled_too():
    """`source_from_env` на строке без «:» пишет в журнал и возвращает None —
    для человека это ровно то же «способности нет»."""
    env = {"AGENT_HOSTING_SOURCE": "my_installation.hosting"}
    why = [c["причина"] for c in capabilities.disabled(env)
           if c["переменная"] == "AGENT_HOSTING_SOURCE"]
    assert why and "модуль:Класс" in why[0]


def test_a_playbook_pointing_at_nothing_is_disabled(tmp_path):
    missing = tmp_path / "нет.md"
    why = capabilities.reason(
        {"переменная": "AGENT_SYMPTOM_PLAYBOOK_FILE", "вид": capabilities.FILE},
        {"AGENT_SYMPTOM_PLAYBOOK_FILE": str(missing)})
    assert "файла нет" in why


def test_an_empty_playbook_is_disabled(tmp_path):
    """Раннер отдаёт пустой файл как незаданный — значит и здесь выключено:
    иначе у человека стоит галочка, а способности нет."""
    empty = tmp_path / "playbook.md"
    empty.write_text("   \n\n", encoding="utf-8")
    why = capabilities.reason(
        {"переменная": "AGENT_SYMPTOM_PLAYBOOK_FILE", "вид": capabilities.FILE},
        {"AGENT_SYMPTOM_PLAYBOOK_FILE": str(empty)})
    assert "пуст" in why


def test_a_filled_playbook_is_enabled(tmp_path):
    full = tmp_path / "playbook.md"
    full.write_text("кэш чистится командой X", encoding="utf-8")
    assert capabilities.reason(
        {"переменная": "AGENT_SYMPTOM_PLAYBOOK_FILE", "вид": capabilities.FILE},
        {"AGENT_SYMPTOM_PLAYBOOK_FILE": str(full)}) == ""


@pytest.mark.parametrize("raw,off", [(None, False), ("", False), ("1", False),
                                     ("0", True), ("off", True), ("FALSE", True)])
def test_a_switch_is_on_unless_it_was_turned_off(raw, off):
    env = {} if raw is None else {"AGENT_VERIFY_AFTER_MERGE": raw}
    named = [c for c in capabilities.disabled(env)
             if c["переменная"] == "AGENT_VERIFY_AFTER_MERGE"]
    assert bool(named) is off


def test_an_interval_of_zero_is_watching_turned_off():
    assert [c for c in capabilities.disabled({"CORE_CHECK_INTERVAL_SEC": "0"})
            if c["переменная"] == "CORE_CHECK_INTERVAL_SEC"]
    assert not [c for c in capabilities.disabled({"CORE_CHECK_INTERVAL_SEC": "3600"})
                if c["переменная"] == "CORE_CHECK_INTERVAL_SEC"]


def test_a_negative_interval_is_watching_turned_off():
    """`updater.watch` выключается на `every <= 0`, а не только на нуле.
    Назвать минус включённым — соврать в ту самую сторону."""
    why = capabilities.reason(
        {"переменная": "CORE_CHECK_INTERVAL_SEC", "вид": capabilities.INTERVAL},
        {"CORE_CHECK_INTERVAL_SEC": "-1"})
    assert why == "CORE_CHECK_INTERVAL_SEC=-1"


def test_a_person_layer_that_points_at_nothing_is_disabled(tmp_path):
    """Каталога нет — `memory.layer_entries` отдаёт пустой слой молча: ровно
    тот случай, ради которого список и заводили."""
    why = capabilities.reason(
        {"переменная": "AGENT_PERSON_MEMORY", "вид": capabilities.DIR},
        {"AGENT_PERSON_MEMORY": str(tmp_path / "нет")})
    assert "каталога нет" in why


def test_an_empty_person_layer_is_disabled(tmp_path):
    (tmp_path / "readme.txt").write_text("не запись", encoding="utf-8")
    why = capabilities.reason(
        {"переменная": "AGENT_PERSON_MEMORY", "вид": capabilities.DIR},
        {"AGENT_PERSON_MEMORY": str(tmp_path)})
    assert "нет ни одной записи" in why


def test_a_person_layer_with_records_is_enabled(tmp_path):
    (tmp_path / "привычки.md").write_text("отчёт прозой", encoding="utf-8")
    assert capabilities.reason(
        {"переменная": "AGENT_PERSON_MEMORY", "вид": capabilities.DIR},
        {"AGENT_PERSON_MEMORY": str(tmp_path)}) == ""


def test_garbage_in_an_interval_is_not_called_merely_disabled():
    """Не число роняет приём на импорте. Назвать это «выключено» — успокоить
    человека там, где надо встревожить."""
    why = capabilities.reason(
        {"переменная": "CORE_CHECK_INTERVAL_SEC", "вид": capabilities.INTERVAL},
        {"CORE_CHECK_INTERVAL_SEC": "час"})
    assert "не число" in why


# ── 2. следствие, а не переменная ───────────────────────────────────────────

def test_every_line_starts_with_the_consequence():
    """Слева — то, что человек узнает в своём рабочем дне; имя переменной
    стоит справа, в причине, потому что по нему включают, а не понимают."""
    off = capabilities.disabled({})
    for cap, line in zip(off, capabilities.lines(off)):
        head = line.split("—")[0].strip()
        assert cap["переменная"] not in head, f"слева имя переменной: {line}"
        assert re.search(r"[а-яё]{3}", head), f"слева не человеческая фраза: {line}"


def test_the_disabled_line_carries_its_variable():
    """Назвать способность мало: включают её переменной, и она обязана быть
    в строке — иначе человеку придётся идти читать код."""
    for cap in capabilities.disabled({}):
        assert cap["переменная"] in cap["причина"]


def test_every_capability_says_what_the_silence_costs():
    for cap in capabilities.CAPABILITIES:
        assert len(cap["цена"]) > 20, cap


# ── 3. выключенное — не ошибка ──────────────────────────────────────────────

def test_a_fully_disabled_installation_still_exits_zero(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("LINEAR_API_KEY=x\n", encoding="utf-8")
    assert capabilities.main(["--env-file", str(env_file)]) == 0
    out = capsys.readouterr().out
    assert "выключено:" in out
    assert "мерж и открытие PR со страницы очереди" in out


def test_the_command_reads_the_env_file_the_way_install_sh_does(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('# комментарий\n'
                        'AGENT_HOSTING_SOURCE="mod:Cls"\n'
                        "SIGNING_TOKEN='tok'\n"
                        "AGENT_CANARY_REPO=owner/repo\n"
                        "  CORE_CHECK_INTERVAL_SEC = 3600 \n"
                        "AGENT_ENV_CANON=прод — юнит `x`, порт=3001\n"
                        "AGENT_PERSON_MEMORY=\n"
                        'СЛОМАНО="хвост\n'
                        "без-равенства\n", encoding="utf-8")
    env = capabilities.read_env(env_file)
    assert env["AGENT_HOSTING_SOURCE"] == "mod:Cls"
    assert env["SIGNING_TOKEN"] == "tok"
    assert env["AGENT_ENV_CANON"] == "прод — юнит `x`, порт=3001"
    assert env["AGENT_PERSON_MEMORY"] == ""
    assert "без-равенства" not in env
    # Разбор обязан совпадать с тем, которым живёт аудит базы знаний: два
    # разных понимания одного файла окружения — это два разных ответа на один
    # вопрос, и человеку негде узнать, какой из них верный.
    assert env == knowledge.read_env(env_file)


def test_nothing_disabled_says_so_out_loud(tmp_path):
    """Пустой вывод неотличим от «список не отработал»."""
    full = {c["переменная"]: "mod:Cls" for c in capabilities.CAPABILITIES}
    full["CORE_CHECK_INTERVAL_SEC"] = "3600"
    playbook = tmp_path / "p.md"
    playbook.write_text("рецепт", encoding="utf-8")
    full["AGENT_SYMPTOM_PLAYBOOK_FILE"] = str(playbook)
    full["AGENT_INCIDENT_PLAYBOOK_FILE"] = str(playbook)
    layer = tmp_path / "person"
    layer.mkdir()
    (layer / "привычки.md").write_text("отчёт прозой", encoding="utf-8")
    full["AGENT_PERSON_MEMORY"] = str(layer)
    assert capabilities.disabled(full) == []
    assert capabilities.report(full) == ["выключенных способностей нет"]


# ── 4. список не протухает ──────────────────────────────────────────────────

def _product_sources() -> str:
    """Исходники продукта без тестов и без самого списка."""
    out = []
    for path in list(CORE.glob("*.py")) + list((CORE / "agent_factory").glob("*.py")):
        if path.name.startswith("test_") or path.name == "capabilities.py":
            continue
        out.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


def test_every_named_variable_is_really_read_by_the_product():
    """Переименовали переменную в продукте — список обязан упасть здесь, а не
    молча продолжить рассказывать про способность, которой управляют другим
    именем. Ищем имя В КАВЫЧКАХ: упоминание в комментарии переменной не
    читает."""
    src = _product_sources()
    for cap in capabilities.CAPABILITIES:
        var = cap["переменная"]
        assert f'"{var}"' in src or f"'{var}'" in src, \
            f"{var} в продукте не читается — способность названа мёртвой переменной"


def test_the_verbose_list_does_not_claim_more_than_it_checked():
    """По файлу окружения видно «переменная задана», а не «работает»."""
    out = "\n".join(capabilities.report({"AGENT_HOSTING_SOURCE": "mod:Cls"}, show_all=True))
    assert "AGENT_HOSTING_SOURCE задан" in out
    assert "поднялась ли реализация" in out


def test_the_off_words_are_the_same_ones_the_product_reads():
    """Импортировать `deploy` отсюда нельзя (список зовут из установщика, до
    того как ядро способно подняться), поэтому совпадение держит тест."""
    assert capabilities.SWITCH_OFF == deploy.SWITCH_OFF == runner.SWITCH_OFF


def test_a_switch_named_here_is_read_as_a_switch_there():
    """Мало совпадения слов: значение должно пониматься так же."""
    for raw in ("0", "1", "", "off", "нет"):
        here = not capabilities.reason(
            {"переменная": "AGENT_VERIFY_AFTER_MERGE", "вид": capabilities.SWITCH},
            {"AGENT_VERIFY_AFTER_MERGE": raw})
        assert here is deploy.switch_on(raw), raw
