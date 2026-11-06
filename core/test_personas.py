"""Личности фабрики в трекере: кто подписывается под комментарием.

Проверяется три вещи, и все три — про молчаливые отказы.

1. **Откат на общий ключ.** Личность не настроена — фабрика обязана писать
   как раньше, а не перестать писать вовсе. Инсталляция, у которой ещё нет
   приложений в трекере, — нормальное состояние продукта.
2. **Совпадение имён переменных у двух сторон.** Раннер и приём деплоятся
   раздельно и импортировать друг друга не могут: приём читает
   `LINEAR_API_KEY_INTAKE` строкой у себя, раннер собирает то же имя из
   `personas.key_var`. Разъедутся — приём тихо уйдёт на ключ инсталляции, и
   заметить это можно будет только по автору задач в трекере.
3. **Авторство отдельно от прав.** Личность подписывает комментарий и
   создание задачи; назначение, удаление и запросы идут ключом инсталляции.
   У приложения трекера с `actor=app` скоупы узкие, и «отдать агенту» не
   должно зависеть от того, заведена личность или нет.
"""
import os

import pytest

import personas
import runner
from agent_factory.results import LinearTracker


# ── соответствие вида задачи и личности ────────────────────────────────────

@pytest.mark.parametrize("kind,persona", [
    ("work", "engineer"),
    ("incident", "duty"),
    ("intake", "intake"),
    ("verify", "qa"),
])
def test_kind_picks_its_persona(kind, persona):
    assert personas.persona_of(kind) == persona


def test_checking_behaviour_is_not_the_same_voice_as_reviewing_code():
    """Ревью отвечает «правильно ли написан код», QA — «получает ли человек
    ожидаемое». Пока оба говорили голосом Reviewer, на доске нельзя было
    отличить прочитанный дифф от постучавшейся двери, а доверие к этим двум
    видам вердиктов калибруется порознь."""
    assert personas.persona_of("verify") != personas.REVIEWER


@pytest.mark.parametrize("kind", ["", None, "что-то новое"])
def test_unknown_kind_is_the_engineer(kind):
    """Как и везде в раннере: незнакомый вид — обычная работа. Очередь
    переживает деплой и может содержать задачи от старого кода."""
    assert personas.persona_of(kind) == personas.DEFAULT_PERSONA


def test_review_subsession_has_a_persona_without_a_kind():
    """Ревьюер — личность, но не вид задачи: его зовут изнутри работы."""
    assert personas.REVIEWER in personas.PERSONAS
    assert "reviewer" not in personas.PERSONA_BY_KIND


# ── ключи из окружения ─────────────────────────────────────────────────────

def test_the_run_sees_no_live_token_store():
    """Сторож изоляции: прогон обязан идти без живого хранилища токенов.

    `keys_from_env` подмешивает ключи С ДИСКА, и на машине, где фабрика
    работает, файл есть. Тогда четыре проверки ниже падают на чужих настоящих
    ключах, а причина в них не названа — 17.09.2026 набор был красным весь день
    именно так. Пусто здесь — пусть падает ОДИН тест и говорит, что́ сломалось:
    `core/conftest.py` перестал уводить `AGENT_LINEAR_TOKENS` в никуда.
    """
    assert personas.keys_from_store() == {}, (
        "прогон читает настоящее хранилище токенов — см. core/conftest.py")


def test_keys_are_read_by_persona_name():
    keys = personas.keys_from_env({"LINEAR_API_KEY_ENGINEER": "eng",
                                   "LINEAR_API_KEY_QA": "qa"})
    assert keys == {"engineer": "eng", "qa": "qa"}


def test_qa_without_its_own_key_speaks_as_the_reviewer():
    """Инсталляция, где приложения QA ещё нет, иначе писала бы проверки общим
    ключом — то есть от имени человека, и вердикт выглядел бы как его
    собственный. Прежний голос честнее безымянного."""
    keys = personas.keys_from_env({"LINEAR_API_KEY_REVIEWER": "rev"})
    assert keys["qa"] == "rev"


def test_qa_with_its_own_key_does_not_borrow_the_reviewers():
    keys = personas.keys_from_env({"LINEAR_API_KEY_REVIEWER": "rev",
                                   "LINEAR_API_KEY_QA": "own"})
    assert keys["qa"] == "own"


def test_empty_value_is_no_persona_at_all():
    """Пустая строка — это «не настроено», а не «ключ есть, но пустой».
    Иначе `persona in keys` врёт, а запись в трекер уходит с пустым
    заголовком — то есть отказом."""
    assert personas.keys_from_env({"LINEAR_API_KEY_DUTY": "   "}) == {}


def test_intake_side_reads_the_same_variable(monkeypatch):
    """Контракт между раннером и приёмом: одно имя переменной, две стороны.

    Приём собирает трекер в `telegram.app_from_env`, и имя переменной там
    записано строкой — импортировать `personas` он не может.
    """
    from agent_factory import telegram

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("INTAKE_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("LINEAR_API_KEY", "общий")
    monkeypatch.setenv(personas.key_var("intake"), "личность-приёмщика")

    app = telegram.app_from_env()
    assert app is not None
    assert app.channel.results.tracker._author_key == "личность-приёмщика"
    assert app.channel.results.tracker._key == "общий"


def test_intake_falls_back_to_the_installation_key(monkeypatch):
    from agent_factory import telegram

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("INTAKE_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("LINEAR_API_KEY", "общий")
    monkeypatch.delenv(personas.key_var("intake"), raising=False)

    app = telegram.app_from_env()
    assert app is not None
    assert app.channel.results.tracker._author_key == ""


# ── раннер: чьим ключом уходит комментарий ─────────────────────────────────

@pytest.fixture
def trackers(monkeypatch):
    """Адаптеры-пустышки вместо сети: важен ключ, с которым их собрали."""
    made = []

    class FakeAdapter:
        def __init__(self, key, url=""):
            self.key = key
            self.comments = []
            made.append(self)

        def post_comment(self, issue_id, body):
            self.comments.append((issue_id, body))

    monkeypatch.setattr(runner, "LinearAdapter", FakeAdapter)
    monkeypatch.setattr(runner, "_tracker", FakeAdapter("общий"))
    monkeypatch.setattr(runner, "_persona_trackers", {})
    return made


def test_comment_goes_out_under_the_persona_key(trackers, monkeypatch):
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ-ревьюера"})
    runner.linear_comment("issue-1", "разбор", "reviewer")
    assert runner._persona_trackers["reviewer"].key == "ключ-ревьюера"
    assert runner._persona_trackers["reviewer"].comments == [("issue-1", "разбор")]
    assert runner._tracker.comments == []


def test_comment_without_a_persona_key_goes_out_as_before(trackers, monkeypatch):
    monkeypatch.setattr(runner, "PERSONA_KEYS", {})
    runner.linear_comment("issue-1", "разбор", "reviewer")
    assert runner._tracker.comments == [("issue-1", "разбор")]
    assert runner._persona_trackers == {}


def test_persona_adapter_is_built_once(trackers, monkeypatch):
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"engineer": "ключ"})
    runner.linear_comment("issue-1", "раз", "engineer")
    runner.linear_comment("issue-1", "два", "engineer")
    assert len(runner._persona_trackers["engineer"].comments) == 2
    assert len([t for t in trackers if t.key == "ключ"]) == 1


# ── MCP-конфиг сессии ──────────────────────────────────────────────────────

def test_session_gets_its_own_tracker_config(tmp_path, monkeypatch):
    """Иначе сессия ходит в трекер конфигом хоста — общим ключом, и её
    собственные комментарии подписаны владельцем ключа."""
    import json
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"engineer": "ключ-инженера"})
    path = runner.write_mcp_config(tmp_path, "engineer")
    assert path == tmp_path / "mcp.json"
    body = json.loads(path.read_text())
    assert body["mcpServers"]["linear"]["headers"]["Authorization"] == \
        "Bearer ключ-инженера"
    assert body["mcpServers"]["linear"]["url"] == runner.LINEAR_MCP_URL


def test_no_persona_key_means_no_config_and_no_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    assert runner.write_mcp_config(tmp_path, "engineer") is None
    assert not (tmp_path / "mcp.json").exists()


def test_the_key_is_not_readable_by_the_whole_host(tmp_path, monkeypatch):
    """Ключ лежит файлом именно для этого: в аргументах процесса его видел бы
    любой через `ps`."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"duty": "ключ"})
    path = runner.write_mcp_config(tmp_path, "duty")
    assert path.stat().st_mode & 0o077 == 0


# ── молчаливый откат на общий ключ слышен ──────────────────────────────────

def test_a_session_without_its_persona_key_says_so(tmp_path, monkeypatch, caplog):
    """Сессия пойдёт общим ключом, и её комментарии подпишет владелец — по
    задаче это неотличимо от работы человека. Чаще всего причина не в
    замысле, а в том, что ключ дописали в окружение после старта юнита."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    with caplog.at_level("WARNING"):
        assert runner.write_mcp_config(tmp_path, "engineer") is None
    assert "LINEAR_API_KEY_ENGINEER" in caplog.text
    assert "общим ключом" in caplog.text


def test_a_missing_reviewer_key_is_not_silent(tmp_path, monkeypatch, caplog):
    """Ревью было, а комментария нет — и без записи в журнале это выглядит
    ровно как «ревью не запускали»."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {})
    (tmp_path / "review.md").write_text("Находок нет.")
    with caplog.at_level("WARNING"):
        assert runner.publish_review("issue-1", tmp_path, "DEV-1") is False
    assert "LINEAR_API_KEY_REVIEWER" in caplog.text


def test_the_startup_report_names_the_nameless(monkeypatch, caplog):
    """Настроенных мало — важнее назвать тех, кто подпишется владельцем."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"qa": "ключ"})
    with caplog.at_level("INFO"):
        runner._report_personas()
    assert "личности трекера настроены: qa" in caplog.text
    nameless = [r.getMessage() for r in caplog.records
                if r.levelname == "WARNING"]
    assert nameless and "engineer" in nameless[0] and "reviewer" in nameless[0]
    assert "qa" not in nameless[0]


def test_the_startup_report_is_not_lost_before_logging_is_set_up(tmp_path):
    """Отчёт зовётся из `main()`, а не с импорта: до `logging.basicConfig` у
    корневого логгера нет обработчиков, INFO отбрасывается вовсе, а WARNING
    уходит сырым в stderr. Прежняя строка про личности так и не попала в
    журнал инсталляции ни разу — проверено по journalctl."""
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LINEAR_API_KEY")}
    env["AGENT_WORK_ROOT"] = str(tmp_path)
    out = subprocess.run([sys.executable, "-c", "import runner"],
                         cwd=os.path.dirname(os.path.abspath(__file__)),
                         capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "личност" not in out.stderr


# ── ревью-субсессия отдельным комментарием ─────────────────────────────────

@pytest.fixture
def commented(monkeypatch):
    said = []
    monkeypatch.setattr(runner, "linear_comment",
                        lambda issue, body, persona="": said.append((issue, body, persona)))
    return said


@pytest.fixture
def with_diff(monkeypatch):
    """Сессия оставила дифф — ветку задачи с коммитом (DEV-121).

    Пиньуем явно, а не полагаемся на хост: `code_trace` читает настоящий
    `AGENT_REPOS_ROOT`, и на машине разработчика он есть, а в CI его нет —
    тест на отказ без этой фикстуры был бы зелёным локально и красным в CI
    (или наоборот) по причине, к самому отказу не относящейся.
    """
    monkeypatch.setattr(runner.codetrace, "code_trace",
                        lambda ident, root=None: "agent-factory: ветка "
                                                 f"agent/{ident}, коммитов поверх "
                                                 "origin/main — 2")


def test_review_goes_out_under_the_reviewer(tmp_path, commented, monkeypatch):
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    (tmp_path / "review.md").write_text("Находок 2, обе починил.")
    assert runner.publish_review("issue-1", tmp_path, "DEV-1") is True
    issue, body, persona = commented[0]
    assert issue == "issue-1" and persona == personas.REVIEWER
    assert "Находок 2" in body


OPEN_ISSUE = {"state": {"name": "Verifying", "type": "started"}}
CLOSED_ISSUE = {"state": {"name": "Done", "type": "completed"}}


def test_no_review_file_is_a_refusal_not_a_silence(tmp_path, commented, with_diff,
                                                   monkeypatch):
    """Отсутствие `review.md` — отказ, а не «ревью не было» (DEV-90): дифф
    уходит без ревью, и человек обязан это прочитать в задаче. Отказ — от
    раннера (личность сессии), не от ревьюера: ревьюер ничего не писал."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    assert runner.publish_review("issue-1", tmp_path, "DEV-1", OPEN_ISSUE, "engineer") is False
    issue, body, persona = commented[0]
    assert issue == "issue-1" and persona == "engineer"
    assert "Ревью не состоялось" in body and "не оставила `review.md`" in body
    (tmp_path / "review.md").write_text("   \n")
    assert runner.publish_review("issue-1", tmp_path, "DEV-1", OPEN_ISSUE) is False
    assert "`review.md` пуст" in commented[1][1]
    assert not any(p == personas.REVIEWER for _, _, p in commented)


def test_the_refusal_goes_out_even_without_a_reviewer_key(tmp_path, commented,
                                                          with_diff, monkeypatch):
    """Отказ — не копия текста ревью, дублировать ему нечего."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {})
    assert runner.publish_review("issue-1", tmp_path, "DEV-1", OPEN_ISSUE) is False
    assert len(commented) == 1 and "Ревью не состоялось" in commented[0][1]


def test_a_closed_task_without_code_needs_no_review(tmp_path, commented, with_diff,
                                                    monkeypatch):
    """Исходы 2 и 3 контракта — справка, отказ: ревьюить нечего.

    Закрытый статус остаётся причиной молчать даже тогда, когда ветка с
    коммитами всё-таки нашлась (`with_diff`): работа с кодом кончается не
    закрытием задачи, а передачей на проверку, и закрывший её сам сказал, что
    сдавать нечего. Без `with_diff` тест был бы зелёным из-за отсутствия
    диффа, а не из-за статуса, — то есть не проверял бы ничего.
    """
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    assert runner.publish_review("issue-1", tmp_path, "DEV-1", CLOSED_ISSUE) is False
    assert commented == []


def test_a_session_without_a_diff_is_not_nagged_about_review(tmp_path, commented,
                                                             monkeypatch, caplog):
    """Исход 4 контракта — стратегия на согласование: задача НЕ закрыта (она
    ушла человеку в статус приёмки), кода сессия не писала, ветки с коммитами
    нет. Ревью диффа, которого нет, — не пропущенный шаг, а бессмысленная
    операция, и человеку про неё писать нечего (DEV-121: семь сообщений, из
    них DEV-21 и DEV-119 — ровно этот случай)."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    monkeypatch.setattr(runner.codetrace, "code_trace", lambda ident, root=None: "")
    with caplog.at_level("INFO"):
        assert runner.publish_review("issue-1", tmp_path, "DEV-21", OPEN_ISSUE) is False
    assert commented == []
    assert "ревьюить нечего" in caplog.text


def test_a_session_with_a_diff_and_no_review_is_still_caught(tmp_path, commented,
                                                             monkeypatch):
    """Обратная сторона: ветка с коммитом есть, `review.md` нет — это
    настоящий пропуск, и он по-прежнему уезжает в задачу. Молчание здесь
    было бы хуже ложной тревоги: непроверенный дифф выдан за проверенный."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    monkeypatch.setattr(runner.codetrace, "code_trace",
                        lambda ident, root=None: "agent-factory: ветка agent/DEV-1, "
                                                 "коммитов поверх origin/main — 2")
    assert runner.publish_review("issue-1", tmp_path, "DEV-1", OPEN_ISSUE) is False
    (body,) = [b for _, b, _ in commented]
    assert "Ревью не состоялось" in body
    # Дифф назван в самом сообщении: человек проверяет его той же командой,
    # какой его нашёл раннер, а не верит на слово.
    assert "agent/DEV-1" in body


def test_an_unreadable_diff_keeps_the_old_loud_behaviour(tmp_path, commented,
                                                         monkeypatch):
    """Ответа про дифф нет вовсе (каталог репозиториев не прочитан, имя задачи
    не разобрано) — молчать нельзя: неизвестность трактуется как «дифф был»."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    monkeypatch.setattr(runner.codetrace, "code_trace", lambda ident, root=None: None)
    assert runner.publish_review("issue-1", tmp_path, "DEV-1", OPEN_ISSUE) is False
    assert len(commented) == 1 and "Ревью не состоялось" in commented[0][1]


def test_no_snapshot_means_no_refusal_either(tmp_path, commented, with_diff,
                                             monkeypatch):
    """Трекер не ответил — `ensure_handoff` уже сказал об этом в журнал, и
    комментарий туда же не доедет."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    assert runner.publish_review("issue-1", tmp_path, "DEV-1", {}) is False
    assert runner.publish_review("issue-1", tmp_path, "DEV-1") is False
    assert commented == []


def test_review_of_is_the_schema_behind_the_refusal(tmp_path):
    assert runner.review_of(tmp_path) == ("", "сессия не оставила `review.md`")
    (tmp_path / "review.md").write_text(" \n\t")
    assert runner.review_of(tmp_path) == ("", "`review.md` пуст")
    (tmp_path / "review.md").write_text("Находок нет.\n")
    assert runner.review_of(tmp_path) == ("Находок нет.", "")


def test_without_the_reviewer_persona_nothing_is_duplicated(tmp_path, commented,
                                                            monkeypatch):
    """Тот же текст за подписью владельца ключа мультиагентности не
    показывает — он просто второй раз лежит в задаче."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {})
    (tmp_path / "review.md").write_text("Находок нет.")
    assert runner.publish_review("issue-1", tmp_path, "DEV-1") is False
    assert commented == []


def test_a_long_review_is_trimmed_not_dropped(tmp_path, commented, monkeypatch):
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"reviewer": "ключ"})
    (tmp_path / "review.md").write_text("щ" * (runner.REVIEW_COMMENT_MAX + 5000))
    assert runner.publish_review("issue-1", tmp_path, "DEV-1") is True
    body = commented[0][1]
    assert len(body) < runner.REVIEW_COMMENT_MAX + 500
    assert "обрезано" in body


# ── приём: личность подписывает авторство, но не правит задачу ─────────────

class RecordingTracker(LinearTracker):
    """Настоящий `LinearTracker` с подменённым походом в сеть: проверяем,
    каким ключом ушёл каждый запрос."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []
        self.reply = {}

    def _gql(self, query, variables, key=""):
        self.calls.append((query, key or self._key))
        return self.reply


def test_issue_and_comment_are_authored_by_the_persona():
    tr = RecordingTracker("общий", author_key="личность")
    tr.reply = {"issueCreate": {"success": True,
                                "issue": {"id": "i1", "identifier": "DEV-1",
                                          "url": "u"}},
                "commentCreate": {"success": True}}
    tr.create_issue("team-1", "заголовок", "тело")
    tr.comment("i1", "разбор")
    authored = [key for query, key in tr.calls
                if "issueCreate" in query or "commentCreate" in query]
    assert authored == ["личность", "личность"]


def test_state_lookup_and_assignment_keep_the_installation_key():
    """Скоупы приложения узкие: чтение статусов команды и `issueUpdate` под
    ним могут не пройти вовсе. Отдать задачу агенту обязано получаться и у
    инсталляции с личностями, и у инсталляции без них."""
    tr = RecordingTracker("общий", author_key="личность")
    tr.reply = {"issueCreate": {"success": True,
                                "issue": {"id": "i1", "identifier": "DEV-1",
                                          "url": "u"}},
                "issue": {"id": "i1", "identifier": "DEV-1", "url": "u",
                          "assignee": {"id": "кто-то"}},
                "issueUpdate": {"success": True,
                                "issue": {"id": "i1", "identifier": "DEV-1",
                                          "url": "u"}}}
    tr.create_issue("team-1", "заголовок", "тело")
    tr.assign("i1", "агент")
    common = [key for query, key in tr.calls
              if "issueCreate" not in query and "commentCreate" not in query]
    assert common and set(common) == {"общий"}


def test_without_a_persona_everything_goes_out_as_before():
    tr = RecordingTracker("общий")
    tr.reply = {"commentCreate": {"success": True}}
    tr.comment("i1", "разбор")
    assert tr.calls == [(tr.calls[0][0], "общий")]


def test_a_stale_config_from_a_previous_run_is_locked_down_again(tmp_path,
                                                                 monkeypatch):
    """Сессия, продолженная после лимита, идёт в ТОТ ЖЕ каталог: файл там уже
    лежит, и `O_CREAT` режима ему не меняет."""
    monkeypatch.setattr(runner, "PERSONA_KEYS", {"engineer": "ключ"})
    stale = tmp_path / "mcp.json"
    stale.write_text("{}")
    stale.chmod(0o644)
    path = runner.write_mcp_config(tmp_path, "engineer")
    assert path.stat().st_mode & 0o077 == 0


def test_the_startup_report_is_actually_wired_into_main(tmp_path, monkeypatch):
    """Отчёт о личностях зовётся из `main()`, а не просто существует.

    Мутация «убрать вызов из `main()`» не роняла ни одного теста: функция была
    покрыта прямым вызовом, а её присутствие в пути запуска — ничем. Это ровно
    тот промах, от которого весь DEV-66: молчаливое отсутствие признака.

    Порядок относительно `logging.basicConfig` этот тест НЕ стережёт: под
    pytest у корневого логгера обработчики стоят всегда, и проверка «они уже
    на месте» не умеет ответить «нет» — мутация «перенести вызов выше
    basicConfig» её не роняет. Порядок стережёт
    `test_the_startup_report_is_not_lost_before_logging_is_set_up`: он
    отдельным процессом и потому видит настоящее состояние логгера.
    """
    seen = {}

    def spy():
        seen["звали"] = True

    monkeypatch.setattr(runner, "_report_personas", spy)
    monkeypatch.setattr(runner, "WORK_ROOT", tmp_path / "работа")

    class DeadRedis:
        def ping(self):
            raise runner.redis.RedisError("некуда")

    monkeypatch.setattr(runner.redis, "from_url", lambda *a, **k: DeadRedis())
    # Redis недоступен — `main()` выходит с 1 сразу после отчёта. Дальше по
    # коду потоки и сигналы, поднимать их ради этой проверки незачем.
    assert runner.main() == 1
    assert seen.get("звали"), "main() не позвал _report_personas"


def test_a_borrowed_voice_is_not_reported_as_a_persona_of_its_own(monkeypatch,
                                                                  caplog):
    """QA без своего приложения занимает ключ Reviewer (`FALLBACK_PERSONA`), и
    в карте ключей лежит настроенной.

    Назвать её тогда «настроенной» — сказать неправду ровно о том, ради чего
    отчёт и пишется: приложения QA у инсталляции нет, вердикты проверки
    подпишет Reviewer, и по авторам в задачах это выглядит как ревью.
    """
    monkeypatch.setattr(runner, "PERSONA_KEYS",
                        {"reviewer": "ключ-ревьюера", "qa": "ключ-ревьюера"})
    with caplog.at_level("INFO"):
        runner._report_personas()
    assert "настроены: reviewer" in caplog.text
    assert "настроены: qa" not in caplog.text
    assert "личность qa без своего ключа" in caplog.text
    assert "подпишет reviewer" in caplog.text


def test_a_persona_with_its_own_key_is_not_called_borrowed(monkeypatch, caplog):
    """Обратная сторона: у QA появилось своё приложение — и она снова своя.

    Без этого различение свелось бы к «qa всегда чужая», и заведённый ключ
    остался бы незамеченным в журнале.
    """
    monkeypatch.setattr(runner, "PERSONA_KEYS",
                        {"reviewer": "ключ-ревьюера", "qa": "ключ-qa"})
    with caplog.at_level("INFO"):
        runner._report_personas()
    assert "настроены: qa, reviewer" in caplog.text
    assert "без своего ключа" not in caplog.text
