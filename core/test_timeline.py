"""Лента заходов (DEV-108): переписка машин одной лентой.

Образец — DEV-41: три круга проверки, два противоположных вердикта на одном и
том же коммите. Проверяется то, ради чего лента заведена: что расхождение
названо СЛОВАМИ, что заход не теряется и не удваивается, и что роль узнаётся
по своим словам, а не по первому похожему слову в чужом тексте.
"""
import headline
import timeline

WORK_START = "**DEV-41 · сессия работы: взялся.** Репозиторий `agent-factory`, ветка `agent/DEV-41`."

# Отчёт работы по нынешней схеме: шапка, черта, четыре раздела. «Итог ревью»
# внутри него — требование контракта, а не признак того, что отчёт написало
# ревью.
WORK_REPORT = (
    "**Что требуется от человека** — мержить\n"
    "**Что сделано** — база знаний собирается процессом, а не руками владельца\n"
    "**Как посмотреть самому** — `python3 knowledge.py audit`, ждём «не хватает: 0»\n"
    "**Итог** — прошло\n"
    "**Чего ждать дальше** — ждёт тебя, сама задача дальше не поедет\n\n---\n\n"
    "## Цель\nПревратить создание базы знаний в процесс продукта.\n"
    # «Код-ревью» ВНУТРИ отчёта работы: контракт велит и назвать итог ревью, и
    # закоммитить правки по нему отдельным коммитом. Ровно из-за этих слов
    # отчёт работы уезжал в ленту как ревью.
    "## Формат выхода\nPR #46, ветка `agent/DEV-41`. Итог ревью: три находки,\n"
    "все починены вторым коммитом «правки по код-ревью».\n"
    "## Чем наблюдать\n```bash\ncd core\npython3 knowledge.py audit --repos-root /repos\n```\n"
    "## Границы\nничего не отложено\n"
)

# Тот же отчёт без шапки — так писали до DEV-108, и такие задачи в трекере
# остались.
OLD_REPORT = (
    "## Цель\nБаза знаний собирается процессом продукта, а не руками владельца.\n"
    "## Формат выхода\nPR #46\n"
    "## Чем наблюдать\nОжидаемое: ворота строже.\n```bash\ncd core\npython3 knowledge.py audit\n```\n"
    "## Границы\nничего\n"
)

REVIEW = "🔍 **Код-ревью по диффу ветки.**\n\n## Код-ревью ветки — 3 находки, все починены\n"


def check(n: str, verdict: str, commit: str = "") -> str:
    where = f", коммит `{commit}`" if commit else ""
    return (f"## Проверка DEV-41 ДО МЕРЖА ({n} заход{where}) — `{verdict}`\n\n"
            "**Источник критерия: задача.**\n")


def c(when: str, body: str, author: str = "", cid: str = "") -> dict:
    return {"id": cid or when, "createdAt": when, "body": body, "author": author}


# ── роль по тексту, а не по первому похожему слову ─────────────────────────

def test_a_work_report_that_mentions_the_review_is_still_work():
    """Контракт ВЕЛИТ отчёту работы назвать итог ревью.

    Пока «код-ревью» искалось в любом месте текста, отчёт работы уезжал в
    ленту как ревью — и на DEV-41 два захода работы из четырёх подписывались
    чужой ролью."""
    assert timeline.role_of(WORK_REPORT) == timeline.ROLE_WORK
    assert timeline.role_of(OLD_REPORT) == timeline.ROLE_WORK


def test_a_goal_that_starts_with_the_word_check_is_still_work():
    """Раздел «Цель» доработки начинается со слова «Проверка»: проверка
    вернула задачу. Роль по этому слову — тоже работа."""
    assert timeline.role_of(OLD_REPORT) == timeline.ROLE_WORK


def test_the_review_is_recognised_by_its_own_opening():
    assert timeline.role_of(REVIEW) == timeline.ROLE_REVIEW


def test_a_human_comment_that_mentions_the_review_in_passing_is_not_the_review():
    """Человек тоже пишет в задачу, и «код-ревью» в его абзаце — упоминание, а
    не подпись роли. Ревью узнаётся по СВОЕМУ началу (`🔍 Код-ревью по диффу`),
    а не по слову где-нибудь в тексте: иначе лента подписывает чужой абзац
    ролью, которой он не принадлежит, и человек читает спор машин, которого не
    было."""
    body = ("Посмотрел глазами, вопрос по стратегии. " + "Дальше рассуждение. " * 20
            + "И ещё раз прочитай код-ревью по этой ветке.")
    assert timeline.role_of(body) == timeline.ROLE_UNKNOWN


def test_a_verdict_needs_both_the_title_and_the_head():
    assert timeline.role_of(check("второй", "FAIL/implementation")) == timeline.ROLE_VERIFY
    assert timeline.verdict_of(check("второй", "FAIL/implementation")) == "FAIL/implementation"


def test_the_class_moved_below_the_rule_is_still_read():
    """Титул стал человеческим, и класс переехал под черту помеченной строкой.

    Без этого правка «сделать понятно человеку» ослепила бы ленту и раннер на
    всех новых вердиктах: роль проверки узнаётся титулом ВМЕСТЕ с классом.
    """
    body = ("## Проверка DEV-41 до мержа (коммит `0cdf0fd`, PR #54) — не сошлось\n\n"
            "Проверил на стенде, заявленного поведения не увидел. Вернул агенту, "
            "тебе делать ничего не нужно.\n\n---\n\n"
            "**Вердикт:** `FAIL/implementation`\n\n**Источник критерия: задача.**\n")
    assert timeline.verdict_of(body) == "FAIL/implementation"
    assert timeline.role_of(body) == timeline.ROLE_VERIFY
    (r,) = timeline.rounds([c("2026-09-15T22:54:00Z", body)])
    assert r["commit"] == "0cdf0fd" and r["pr"] == "54"
    # И выше черты класса нет — это и есть предмет правки.
    assert "FAIL" not in body[:body.index(headline.RULE)]


def test_the_word_fail_inside_prose_is_not_a_verdict():
    """«Ложный FAIL стоит одного круга» — не вердикт."""
    assert timeline.verdict_of("Заход работы. Ложный FAIL стоит одного лишнего круга.") == ""


def test_the_merge_note_is_the_only_sign_the_human_accepted():
    assert timeline.role_of("🤖 **Закрыто по мержу.** PR #46") == timeline.ROLE_INTAKE
    assert timeline.role_of("🤖 **Сдача не по схеме.** Смотри MR глазами.") == timeline.ROLE_RUNNER


def test_a_report_that_talks_about_the_merge_does_not_close_the_task():
    """«Закрыто по мержу» — начало комментария приёма, а не слово в чужом
    абзаце. Иначе лента скажет про открытый PR «от человека не требуется
    ничего», и работа останется лежать непринятой."""
    body = ("**Что требуется от человека** — мержить\n"
            "**Что сделано** — поправлено\n**Как посмотреть самому** — стенд\n"
            "**Итог** — прошло\n**Чего ждать дальше** — ждёт тебя\n\n---\n\n"
            "## Формат выхода\nPR #46. Задача будет Закрыто по мержу приёмом.\n")
    assert timeline.role_of(body) == timeline.ROLE_WORK
    ribbon = timeline.render([c("2026-09-15T20:24:00Z", body),
                              c("2026-09-15T20:41:00Z", check("первый", "OK"))])
    assert "закрыта мержем" not in ribbon
    assert "нужен твой мерж" in ribbon


# ── спор машин словами ────────────────────────────────────────────────────

def test_a_disagreement_is_said_in_words_not_left_to_be_compared():
    rs = timeline.rounds([
        c("2026-09-15T20:41:00Z", check("первый", "OK", "0cdf0fd")),
        c("2026-09-15T22:54:00Z", check("второй", "FAIL/implementation", "0cdf0fd")),
        c("2026-09-15T23:37:00Z", check("третий", "OK", "7804003")),
    ])
    said = timeline.disagreement(rs)
    assert "расходились" in said
    assert "первый заход `OK`" in said
    assert "второй заход `FAIL/implementation`" in said
    assert "третий заход `OK`" in said


def test_two_opposite_verdicts_on_one_commit_are_named_separately():
    """Это другой случай, чем «не прошло → доработали → прошло»: код между
    заходами не менялся, значит разошлись сами проверки. Без этой фразы
    человек читает ленту как нормальный ход работы."""
    rs = timeline.rounds([
        c("2026-09-15T20:41:00Z", check("первый", "OK", "0cdf0fd")),
        c("2026-09-15T22:54:00Z", check("второй", "FAIL/implementation", "0cdf0fd")),
    ])
    said = timeline.disagreement(rs)
    assert "ОДНОМ И ТОМ ЖЕ коммите `0cdf0fd`" in said
    assert "разошлись сами проверки" in said


def test_a_fix_between_two_verdicts_is_not_called_a_split_commit():
    rs = timeline.rounds([
        c("2026-09-15T20:41:00Z", check("первый", "FAIL/implementation", "0cdf0fd")),
        c("2026-09-15T23:37:00Z", check("второй", "OK", "7804003")),
    ])
    said = timeline.disagreement(rs)
    assert "расходились" in said
    assert "ОДНОМ И ТОМ ЖЕ" not in said


def test_verdicts_that_agree_say_so():
    rs = timeline.rounds([c("2026-09-15T20:41:00Z", check("первый", "OK", "7804003")),
                          c("2026-09-15T23:37:00Z", check("второй", "OK", "7804003"))])
    assert "не расходились" in timeline.disagreement(rs)


def test_one_check_is_not_a_dispute():
    rs = timeline.rounds([c("2026-09-15T20:41:00Z", check("первый", "OK"))])
    assert "Проверка была одна" in timeline.disagreement(rs)


def test_without_checks_there_is_nothing_to_say():
    assert timeline.disagreement(timeline.rounds([c("2026-09-15T20:10:00Z", WORK_START)])) == ""


# ── лента целиком ─────────────────────────────────────────────────────────

FULL = [
    c("2026-09-15T20:10:00Z", WORK_START, "Engineer"),
    c("2026-09-15T20:24:00Z", WORK_REPORT, "Engineer"),
    c("2026-09-15T20:41:00Z", check("первый", "OK", "0cdf0fd"), "QA"),
    c("2026-09-15T22:54:00Z", check("второй", "FAIL/implementation", "0cdf0fd"), "QA"),
    c("2026-09-15T23:15:00Z", REVIEW, "Reviewer"),
    c("2026-09-15T23:37:00Z", check("третий", "OK", "7804003"), "QA"),
]


def test_one_line_per_round_and_not_one_more():
    body = timeline.render(FULL)
    numbered = [ln for ln in body.splitlines() if ln[:1].isdigit() and ". `" in ln]
    assert len(numbered) == len(FULL)


def test_the_ribbon_opens_with_the_answer_and_passes_its_own_lint():
    body = timeline.render(FULL)
    assert headline.lint_text(body, "лента") == []
    # Ответ выше ленты, а не под ней.
    assert (body.index("база знаний собирается процессом")
            < body.index(headline.RULE) < body.index("1. `"))
    # И это абзац, а не бланк: жирных подписей выше черты нет ни одной.
    assert "**" not in body[:body.index(headline.RULE)].split("\n\n", 1)[1]


def test_the_answer_is_lifted_from_the_headline_of_the_work_report():
    body = timeline.render(FULL)
    assert "база знаний собирается процессом" in body
    assert "knowledge.py audit" in body


def test_an_old_report_without_a_headline_still_answers_from_its_sections():
    """Задачи, написанные до шапки, остались в трекере. Лента по ним отвечает
    разделами сдачи — они заведены ровно для этих двух вопросов, — а не
    молчит."""
    body = timeline.render([c("2026-09-15T20:10:00Z", WORK_START),
                            c("2026-09-15T20:24:00Z", OLD_REPORT),
                            c("2026-09-15T20:41:00Z", check("первый", "OK"))])
    assert "База знаний" in body or "база знаний" in body
    # Команда из блока — а не `cd`, который стоит в блоке первым.
    assert "knowledge.py audit" in body
    assert "`cd core`" not in body


def test_a_command_broken_over_lines_reaches_the_headline_whole():
    """Команда с переносом — одна команда. Без склейки в шапку уезжала её
    половина, а аргументы оставались на следующей строке."""
    report = ("## Цель\nЦель работы одной фразой, чтобы шапке было что сказать.\n"
              "## Формат выхода\nPR #46\n"
              "## Чем наблюдать\nСмотреть так.\n```bash\ncd core\n"
              "python3 knowledge.py audit \\\n  --repos-root /repos\n```\n"
              "## Границы\nнет\n")
    body = timeline.render([c("2026-09-15T20:24:00Z", report),
                            c("2026-09-15T20:41:00Z", check("первый", "OK"))])
    assert "knowledge.py audit --repos-root /repos" in body
    assert "\\`" not in body


def test_the_last_verdict_decides_what_the_human_must_do():
    ok = timeline.render(FULL)
    assert "нужен твой мерж" in ok
    fail = timeline.render(FULL[:-1])
    # `FAIL/implementation` — единственный класс, который чинит агент: от
    # человека тут не требуется ничего, и сказать ему «разобрать» значило бы
    # позвать его туда, где он не нужен.
    assert "задача вернулась агенту" in fail
    assert "работу не подтвердила" in fail


def test_a_merged_task_asks_nothing_of_the_human():
    body = timeline.render(FULL + [c("2026-09-15T23:51:00Z", "🤖 **Закрыто по мержу.** PR #46")])
    assert "закрыта мержем" in body


def test_the_times_match_what_the_comments_themselves_say():
    """Сессии пишут время по `date -u`; сдвинутая на пояс лента называла бы
    тот же заход другим временем, чем его вердикт."""
    body = timeline.render(FULL)
    assert "`15.09 20:41`" in body
    assert "Время UTC" in body


def test_the_ribbon_does_not_count_itself_as_a_round():
    """Иначе на каждом заходе лента росла бы на строку про саму себя."""
    first = timeline.render(FULL)
    again = timeline.render(FULL + [c("2026-09-15T23:40:00Z", first)])
    assert again == first


def test_a_ribbon_is_shorter_than_one_of_the_comments_it_folds():
    """Смысл ленты — полминуты на три вопроса. Лента длиннее любого из полотен
    не сворачивает ничего."""
    assert len(timeline.render(FULL)) < max(len(x["body"]) for x in FULL) * 3


def test_nothing_to_fold_is_no_ribbon():
    assert timeline.render([]) == ""


# ── лента в задаче: один комментарий, правится на месте ───────────────────

class FakeTracker:
    """Трекер на память: комментарии задачи, как их видит раннер."""

    def __init__(self, comments=()):
        self.comments = [dict(x) for x in comments]
        self.posted = 0
        self.updated = []

    def list_comments(self, task_id, limit=50):
        return [dict(x) for x in self.comments]

    def post_comment(self, task_id, body):
        self.posted += 1
        self.comments.append({"id": f"new-{self.posted}", "createdAt": "2026-09-16T00:00:00Z",
                              "body": body, "author": "Runner"})
        return True

    def update_comment(self, comment_id, body):
        self.updated.append(comment_id)
        for x in self.comments:
            if x["id"] == comment_id:
                x["body"] = body
        return True


def _runner(monkeypatch, fake):
    import runner
    runner.TIMELINE_ON = True  # лента выключена по умолчанию (16.09), тест проверяет саму подачу
    monkeypatch.setattr(runner, "_tracker", fake)
    return runner


def test_the_second_round_edits_the_ribbon_instead_of_posting_another(monkeypatch):
    """Копить по ленте на заход значило бы лечить полотна полотнами."""
    fake = FakeTracker(FULL)
    runner = _runner(monkeypatch, fake)
    assert runner.publish_timeline("issue-1", "DEV-41") is True
    assert fake.posted == 1
    first = fake.comments[-1]["id"]

    fake.comments.append(c("2026-09-16T00:10:00Z", check("четвёртый", "OK", "7804003"), "QA"))
    assert runner.publish_timeline("issue-1", "DEV-41") is True
    assert fake.posted == 1                     # второй ленты не появилось
    assert fake.updated == [first]
    assert "четвёртый заход" in fake.comments[[x["id"] for x in fake.comments].index(first)]["body"]


def test_the_ribbon_keeps_its_place_in_the_task(monkeypatch):
    """Правится САМАЯ СТАРАЯ лента: человек привыкает к её месту в задаче, а
    не ищет её заново после каждого захода."""
    ribbon = c("2026-09-15T20:30:00Z", timeline.render(FULL), cid="old-ribbon")
    late = c("2026-09-16T01:00:00Z", timeline.render(FULL), cid="late-ribbon")
    fake = FakeTracker([*FULL, late, ribbon])
    runner = _runner(monkeypatch, fake)
    runner.publish_timeline("issue-1", "DEV-41")
    assert fake.updated == ["old-ribbon"]


def test_one_round_is_not_worth_a_ribbon(monkeypatch):
    """На одном заходе лента повторяла бы сам комментарий."""
    fake = FakeTracker([c("2026-09-15T20:10:00Z", WORK_START)])
    runner = _runner(monkeypatch, fake)
    assert runner.publish_timeline("issue-1", "DEV-41") is False
    assert fake.posted == 0


def test_a_tracker_that_says_nothing_does_not_break_the_handover(monkeypatch):
    """Лента — подача, а не сдача: из-за неё нельзя терять сессию, которая
    работу сделала."""
    fake = FakeTracker()
    runner = _runner(monkeypatch, fake)
    assert runner.publish_timeline("issue-1", "DEV-41") is False
    assert runner.publish_timeline("", "DEV-41") is False


def test_a_tracker_that_answers_nonsense_does_not_cost_the_session(monkeypatch, caplog):
    """Лента идёт последним действием, после отчёта, ревью и перевода задачи.

    Исключение здесь обменяло бы сданную работу на её оформление — поэтому
    обёрнута вся сборка, а не только сеть."""
    class Broken:
        def list_comments(self, task_id, limit=50):
            raise RuntimeError("трекер ответил не тем")

    runner = _runner(monkeypatch, Broken())
    with caplog.at_level("WARNING"):
        assert runner.publish_timeline("issue-1", "DEV-41") is False
    # Пропажа ленты названа в журнале: молча не собравшаяся лента неотличима
    # от задачи, по которой нечего сворачивать.
    assert [r for r in caplog.records if "лента заходов не собралась" in r.getMessage()]


# ── закрыта мержем или переоткрыта (найдено глазами на живых задачах) ────────

MERGED = "🤖 Закрыто по мержу. PR 46 (agent/DEV-41 → main) — смержил beskrovnykh."
# Комментарий приёма, который мержа НЕ означает: работа как раз не принята.
INTAKE_NOT_MERGED = ("Ветка PR #54 перестала сливаться с `main`. Работа сделана "
                     "и проверена, переделывать не надо: влить main, развести "
                     "конфликты и сдать заново.")


def test_a_merge_mark_closes_the_task():
    ribbon = timeline.render([c("2026-09-15T20:24:00Z", WORK_REPORT),
                              c("2026-09-15T23:51:00Z", MERGED)])
    assert "закрыта мержем" in ribbon
    assert "сюда больше никто не придёт" in ribbon


def test_a_task_reopened_after_the_merge_is_not_closed():
    """Мерж ПРОШЛОГО круга не делает задачу закрытой.

    Нашлось глазами на живых DEV-13, DEV-66 и DEV-102: у всех трёх мерж был
    прошлым кругом, после него работу переоткрыли, а лента писала «от тебя
    ничего не требуется» — про задачу, которая как раз ждала человека. Хуже
    голого кода: голый код непонятен, а это понятно и неправда.
    """
    ribbon = timeline.render([
        c("2026-09-15T20:24:00Z", WORK_REPORT),
        c("2026-09-15T23:51:00Z", MERGED),
        c("2026-09-16T01:29:00Z", WORK_REPORT),
        c("2026-09-16T11:51:00Z", check("новый", "FAIL/implementation")),
    ])
    assert "закрыта мержем" not in ribbon
    assert "задача вернулась агенту" in ribbon


def test_an_intake_comment_that_is_not_a_merge_does_not_close_the_task():
    """Роль «приём» получает и «круги исчерпаны», и «ветка не сливается» —
    то есть ровно те случаи, когда работа НЕ принята. Закрытие считается по
    метке мержа, а не по роли автора."""
    rounds = timeline.rounds([c("2026-09-16T11:31:00Z", INTAKE_NOT_MERGED,
                                author="Intake")])
    assert rounds[0]["role"] == timeline.ROLE_INTAKE
    ribbon = timeline.render([c("2026-09-15T20:24:00Z", WORK_REPORT),
                              c("2026-09-16T11:31:00Z", INTAKE_NOT_MERGED,
                                author="Intake")])
    assert "закрыта мержем" not in ribbon


# ── отчёт, который отвечает, а не просто последний ──────────────────────────

def test_the_answers_come_from_the_last_report_that_answers():
    """На DEV-66 последним заходом работы лежало уточнение к границе на два
    абзаца, и по нему лента отвечала «отчёта в задаче ещё нет» — при том, что
    выше лежал целый отчёт."""
    # Роль тут даёт АВТОР, а не текст: в уточнении нет ни одного слова, по
    # которому роль узнаётся, — и без подписи `Engineer` заход не был бы
    # заходом работы, а тест проходил бы вхолостую. Так это и лежит на живой
    # DEV-66: уточнение к границе подписано работой и стоит последним.
    afterword = ("### Уточнение к границе: токены личностей живы\n\n"
                 "В отчёте выше я записал иначе — поправляю.\n")
    assert timeline.role_of(afterword, "Engineer") == timeline.ROLE_WORK
    assert timeline._answers_of(afterword) == ("", "")
    ribbon = timeline.render([c("2026-09-15T20:24:00Z", WORK_REPORT, "Engineer"),
                              c("2026-09-15T20:40:00Z", afterword, "Engineer"),
                              c("2026-09-15T20:41:00Z", check("первый", "OK"))])
    assert "база знаний собирается процессом" in ribbon
    assert "отчёта с шапкой в задаче ещё нет" not in ribbon


def test_an_old_section_name_still_answers():
    """Сдача прошлых поколений звала раздел «Что сделано», а не «Цель»."""
    old = ("## Что сделано\nКонтракты сессий вынесены в пакет `core/roles/`.\n"
           "## Чем проверено\n```bash\npython3 -m pytest core/ -q\n```\n")
    done, use = timeline._answers_of(old)
    assert "Контракты сессий вынесены" in done
    assert "pytest" in use


# ── расшифровка в самой ленте ───────────────────────────────────────────────

def test_the_ribbon_explains_the_codes_it_prints():
    """Лента отвечает на «чем кончился спор» вердиктами — значит, кодами.
    Голыми они и были тем, чего человек не понимал."""
    ribbon = timeline.render([c("2026-09-15T20:24:00Z", WORK_REPORT),
                              c("2026-09-15T20:41:00Z", check("первый", "OK")),
                              c("2026-09-15T22:54:00Z",
                                check("второй", "FAIL/implementation"))])
    assert "настоящий дефект в коде" in ribbon
    assert "`FAIL/implementation`" in ribbon     # код НЕ выкинут


def test_the_explained_ribbon_still_passes_its_own_headline_lint():
    """Разметка пришивается ДО потолка строки, иначе шапка ленты вылезала бы
    за то, за что раннер вернул бы сессию."""
    ribbon = timeline.render([c("2026-09-15T20:24:00Z", WORK_REPORT),
                              c("2026-09-15T22:54:00Z",
                                check("второй", "FAIL/implementation"))])
    assert headline.lint_text(ribbon, "лента") == []


def test_a_check_that_left_no_verdict_is_said_out_loud():
    """«Проверки ещё не было» и «проверка была, но вердикта нет» — разные
    новости: во втором случае заход потрачен, а ответа нет."""
    silent = ("## Проверка DEV-1 ДО МЕРЖА\n\n**Источник критерия: задача.**\n\n"
              "смотрел стенд, договорить не успел\n")
    ribbon = timeline.render([c("2026-09-15T20:24:00Z", WORK_REPORT),
                              c("2026-09-15T20:41:00Z", silent)])
    assert "словами ничего не сказала" in ribbon


# ── адрес машинной части в самой ленте ──────────────────────────────────────

def test_the_ribbon_names_where_the_machine_part_lies(monkeypatch, tmp_path):
    """«Linear — поверхность человека, машинное — по ссылке» (дополнение
    владельца). Удалять машинное нельзя: им спорят и его читает следующая
    сессия. Поэтому лента называет АДРЕС, а не вставляет содержимое."""
    (tmp_path / "expected.md").write_text("эталон на десять экранов")
    (tmp_path / "gates.json").write_text("[]")
    fake = FakeTracker(FULL)
    runner = _runner(monkeypatch, fake)
    assert runner.publish_timeline("issue-1", "DEV-41", tmp_path) is True
    body = fake.comments[-1]["body"]
    assert "Машинная часть" in body
    assert "expected.md" in body and str(tmp_path) in body
    assert "эталон на десять экранов" not in body   # адрес, а не копия


def test_a_ribbon_without_a_workdir_is_still_a_ribbon(monkeypatch):
    """Каталога нет (или его не передали) — блока просто не будет; ленту это
    не роняет."""
    fake = FakeTracker(FULL)
    runner = _runner(monkeypatch, fake)
    assert runner.publish_timeline("issue-1", "DEV-41") is True
    assert "Машинная часть" not in fake.comments[-1]["body"]


# ── класс расхождения несёт действие, голова вердикта — только настроение ────

def test_a_specification_failure_tells_the_human_to_split_the_task():
    """Пример владельца дословно: на `FAIL/specification` ему нужно «задача
    слишком крупная, иди дроби», а не «разобрать»."""
    ribbon = timeline.render([
        c("2026-09-15T20:24:00Z", WORK_REPORT),
        c("2026-09-16T04:38:00Z", check("первый", "FAIL/specification"))])
    assert "разрезать или уточнить" in ribbon
    # И ждать агента не надо: за такую постановку он не возьмётся.
    assert "агент за неё не возьмётся" in ribbon
    assert "задача вернулась агенту" not in ribbon


def test_an_unobservable_failure_asks_for_a_place_to_look():
    ribbon = timeline.render([
        c("2026-09-15T20:24:00Z", WORK_REPORT),
        c("2026-09-16T04:38:00Z", check("первый", "FAIL/unobservable"))])
    assert "нужен стенд, выкат или доступ" in ribbon
    assert "агент этого не починит" in ribbon


def test_a_real_defect_does_not_ask_the_human_for_anything():
    """`FAIL/implementation` — единственный класс, который чинит агент."""
    ribbon = timeline.render([
        c("2026-09-15T20:24:00Z", WORK_REPORT),
        c("2026-09-16T04:38:00Z", check("первый", "FAIL/implementation"))])
    assert "задача вернулась агенту" in ribbon
