"""Шапка машинного комментария (DEV-108): ответ раньше доказательства.

Проверяется не «есть ли нужные слова» — их тут нет вовсе, шапка пишется
прозой, — а четыре признака бланка, которые владелец назвал сам, возвращая
работу: подписи к полям, машинное слово выше черты, длина абзаца и
отсутствие в нём ответа на «а мне-то что делать».

Каждый тест устроен так, чтобы уметь провалиться: рядом с «хорошим» текстом
стоит ровно одна испорченная деталь, и проверяется, что промах назвали
поимённо, а не «что-то не так».
"""
import headline

# Пример владельца из возврата 16.09, дословно. Нарочно он, а не сочинённый:
# если завтра линт начнёт возвращать этот текст, значит линт разошёлся с тем,
# что заказано, — и увидеть это надо тестом, а не на живой задаче.
GOOD_LEAD = (
    "Сделал проверку строже — теперь ловит две вещи, которые раньше "
    "проскакивали. Посмотреть можно командой `knowledge.py audit`, должна "
    "сказать «не хватает: 0». Всё сошлось, дальше нужен твой мерж."
)
PROOF = "## Цель\nдлинный разбор\n\n## Границы\nничего не отложено\n"
GOOD = f"{GOOD_LEAD}\n\n---\n\n{PROOF}"

# Шапка, собранная раннером: только по именам — порядок кусков уже менялся.
PARTS = dict(done="Сделал проверку строже",
             use="Посмотреть можно командой `knowledge.py audit`",
             act="Всё сошлось, дальше нужен твой мерж")


def test_the_owners_own_example_passes():
    assert headline.lint_text(GOOD, "handoff.md") == []


def test_a_refusal_written_like_a_human_passes():
    """Второй пример владельца — когда взяться не вышло."""
    text = ("Не могу взять в работу: задача слишком крупная. Это шесть "
            "отдельных механизмов, а ветка делает один из них. Нужно "
            "разрезать на части, тогда возьму.\n\n---\n\n" + PROOF)
    assert headline.lint_text(text, "handoff.md") == []


def test_a_form_with_field_labels_is_sent_back():
    """Главный промах, ради которого работу вернули: бланк с подписями.

    Владелец дословно: «Не хочу трафареты. Нормальное общение, как люди
    общаются». Возврат обязан назвать именно это, а не «шапка не по схеме»:
    иначе сессия перепишет подписи другими словами и сдаст тот же бланк."""
    text = ("**Что требуется от человека** — мержить\n"
            "**Что сделано** — проверка стала строже\n"
            "**Итог** — прошло\n\n---\n\n" + PROOF)
    misses = headline.lint_text(text, "handoff.md")
    assert len(misses) == 1
    assert "бланком" in misses[0]


def test_a_list_above_the_rule_is_sent_back():
    """Список — тот же бланк без жирного шрифта."""
    text = ("* сделал проверку строже\n"
            "* посмотреть можно командой `knowledge.py audit`\n"
            "* дальше нужен твой мерж\n\n---\n\n" + PROOF)
    misses = headline.lint_text(text, "handoff.md")
    assert [m for m in misses if "списком" in m]


def test_a_headline_that_starts_with_a_command_is_sent_back():
    text = "```\nknowledge.py audit\n```\n\nдальше нужен твой мерж\n\n---\n\n" + PROOF
    assert [m for m in headline.lint_text(text, "handoff.md") if "блоком кода" in m]


def test_a_paragraph_instead_of_an_answer_is_sent_back():
    """Длина — то единственное, чем код отличает ответ от начала спора.

    Без потолка в шапку обратно переезжает ровно то, от чего она заведена:
    позиции подстрок и суммы, только теперь сверху."""
    long = GOOD_LEAD + " И ещё " + "подробность " * 60
    misses = headline.lint_text(f"{long}\n\n---\n\n{PROOF}", "handoff.md")
    assert len(misses) == 1
    assert "длиннее" in misses[0] and str(headline.MAX_CHARS) in misses[0]


def test_a_headline_that_never_says_what_to_do_is_sent_back():
    """Ради этой строки всё и затевалось: человек читает абзац и знает, идти
    ему что-то делать или нет."""
    text = ("Сделал проверку строже — теперь ловит две вещи, которые раньше "
            "проскакивали. Посмотреть можно командой `knowledge.py audit`.\n"
            "\n---\n\n" + PROOF)
    misses = headline.lint_text(text, "handoff.md")
    assert len(misses) == 1
    assert "что делать человеку" in misses[0]


def test_saying_that_nothing_is_needed_is_also_an_answer():
    """«От тебя ничего не нужно» — законный ответ, а не пропуск строки.

    Ни одного глагола в адрес человека тут нарочно нет: иначе тест проходил бы
    через `_ACTION` и про прямое «ничего не нужно» ничего не говорил."""
    text = ("Прочитал свой дифф целиком: пять находок, все правки уже в ветке. "
            "Разбор по каждой — ниже. От тебя ничего не нужно, дифф поедет "
            "дальше сам.\n\n---\n\n" + PROOF)
    assert headline.lint_text(text, "review.md") == []
    # Ворота самой проверки: без прямой фразы этот же текст возвращается.
    assert headline.lint_text(text.replace("От тебя ничего не нужно, дифф",
                                           "Дифф"), "review.md")


def test_a_machine_code_above_the_rule_is_sent_back():
    """Шапка — поверхность человека, и `FAIL/specification` на ней это шум.

    Ниже черты код остаётся: по нему считают и по нему читает следующая
    сессия. Возврат называет фразу, которой код заменить."""
    text = ("Проверил на стенде, не сошлось: `FAIL/specification`. Нужно "
            "разрезать задачу.\n\n---\n\n" + PROOF)
    misses = headline.lint_text(text, "комментарий")
    assert len(misses) == 1
    assert "FAIL/specification" in misses[0]
    assert "задача слишком крупная" in misses[0]


def test_a_bare_code_without_backticks_is_sent_back_too():
    """Так его и пишут в скобках: «по вердикту от 16.09 (FAIL/implementation)».

    Реестр без кавычек `FAIL` не трогает нарочно — в прозе это слово, а не
    вердикт, — но выше черты оговорки нет: человек читает одинаковый шум с
    кавычками и без. Найдено на живой DEV-13, где код проехал в шапку ленты.
    """
    text = ("Доделал по ответу проверки от 16.09 (FAIL/implementation) и по "
            "возврату владельца. Посмотреть — ветка сливается. Можно "
            "мержить.\n\n---\n\n" + PROOF)
    misses = headline.lint_text(text, "лента")
    assert len(misses) == 1
    assert "FAIL/implementation" in misses[0]


def test_the_same_code_below_the_rule_is_not_a_miss():
    """Ниже черты код стоит среди доказательства, где он машине и нужен."""
    text = GOOD.replace("длинный разбор", "разбор, класс `FAIL/specification`")
    assert headline.lint_text(text, "handoff.md") == []


def test_a_machine_code_in_the_title_is_sent_back_too():
    """Титул — это первое, что человек видит, и запрет действует и на него.

    Без этого `## Проверка DEV-41 — FAIL/implementation` проезжал бы целиком:
    шапка ниже него чистая, а машинное слово всё равно на первом экране."""
    text = "## Проверка DEV-41 — `FAIL/implementation`\n\n" + GOOD
    assert [m for m in headline.lint_text(text, "комментарий")
            if "FAIL/implementation" in m]


def test_a_human_title_is_allowed():
    """Заголовок сам по себе не запрещён — запрещена машинерия в нём."""
    assert headline.lint_text("## Проверка DEV-41 — сошлось\n\n" + GOOD,
                              "комментарий") == []


def test_service_words_of_the_product_are_sent_back():
    """«Ворота», «вердикт», «исход» — внутренняя кухня, человеку она не нужна.

    Владелец назвал их поимённо. Проверяются по одному, чтобы возврат называл
    то слово, которое правда стоит в тексте."""
    for word, said in (("вердикт", "Вердикт по ветке: сошлось, нужен мерж"),
                       ("ворота", "Ворота прошли, дальше нужен твой мерж"),
                       ("исход", "Исход захода такой: сошлось, нужен мерж")):
        misses = headline.lint_text(f"{said}.\n\n---\n\n{PROOF}", "handoff.md")
        assert [m for m in misses if word in m], word


def test_a_latin_word_that_merely_contains_a_code_is_not_a_code():
    """Без границ слова `OK` находится внутри `TOKEN`, а `FAIL` — внутри
    `FAILOVER`: сессию возвращали бы за слово, которого в шапке нет."""
    text = ("Положил TOKEN в окружение юнита и проверил FAILOVER вручную. "
            "Посмотреть можно в журнале. Дальше нужен твой мерж.\n\n---\n\n"
            + PROOF)
    assert headline.lint_text(text, "handoff.md") == []


def test_an_ordinary_word_is_not_a_service_word():
    """`исходный` и `исходя` — обычные слова, и ловить их значило бы учить
    сессию писать канцелярски, лишь бы пройти линт."""
    text = ("Вернул исходный файл на место, исходя из того, что правка была "
            "лишней. Посмотреть можно в диффе. Дальше нужен твой "
            "мерж.\n\n---\n\n" + PROOF)
    assert headline.lint_text(text, "handoff.md") == []


def test_proof_first_is_named_as_a_missing_headline():
    """Отчёт, открывающийся `## Цель`, — это «шапки нет», ОДИН промах.

    Не «три промаха на одно действие»: сессия чинит одно дело, и возврат
    должен назвать одно."""
    misses = headline.lint_text(PROOF, "handoff.md")
    assert len(misses) == 1
    assert "нет шапки" in misses[0]


def test_a_missing_file_is_not_a_headline_miss():
    """Нет файла — это промах схемы сдачи, и называет его она."""
    assert headline.lint_file("/no/such/dir", "handoff.md") == []


def test_a_file_by_the_schema_passes_from_disk(tmp_path):
    (tmp_path / "handoff.md").write_text(GOOD)
    assert headline.lint_file(tmp_path, "handoff.md") == []


# ── сборка ───────────────────────────────────────────────────────────────────

def test_what_the_runner_renders_would_pass_its_own_lint():
    """Раннер не должен уметь положить в шапку то, за что вернул бы сессию."""
    built = headline.render(**PARTS)
    assert headline.lint_text(built, "лента") == []
    long = headline.render(**{**PARTS, "done": "и " * headline.MAX_PHRASE_CHARS})
    assert headline.lint_text(long, "лента") == []
    assert "…" in long


def test_three_long_parts_still_fit_the_paragraph():
    """Потолок у АБЗАЦА, а не у куска.

    Пока каждый кусок резался по своему потолку, три длинных складывались в
    шапку длиннее той, за которую раннер вернул бы сессию, — и лента печатала
    бы её сама."""
    long = "слово " * 200
    built = headline.render(done=long, use=long, act="Дальше нужен твой мерж, а ещё " + long)
    assert headline.lint_text(built, "лента") == []
    assert len(built) <= headline.MAX_CHARS


def test_the_action_is_not_the_part_that_gets_cut():
    """Режутся «что сделано» и «где посмотреть»: ради действия абзац и пишется."""
    built = headline.render(done="слово " * 200, use="слово " * 200,
                            act="Дальше нужен твой мерж, сама задача не поедет")
    assert built.endswith("Дальше нужен твой мерж, сама задача не поедет.")


def test_a_quote_with_machine_words_is_softened_before_it_reaches_the_human():
    """Раннер вставляет в шапку ЧУЖИЕ фразы — цель работы, раздел «чем
    наблюдать». Переписать их он не может, а печатать то, за что вернул бы
    сессию, не должен. Обе живые фразы взяты с DEV-13 и DEV-109.
    """
    built = headline.render(
        done="Доделать DEV-13 по вердикту проверки от 16.09 (FAIL/implementation)",
        use="Ожидаемое: ворота строже",
        act="Можно мержить")
    assert headline.lint_text(built, "лента") == []
    # Падеж сохранён, а не заменён словарной формой.
    assert "по ответу от 16.09" in built
    assert "проверки строже" in built


def test_the_render_is_a_paragraph_and_not_a_form():
    """Собранная шапка — сплошной текст: ни переносов строк, ни подписей."""
    built = headline.render(**PARTS)
    assert "\n" not in built
    assert "**" not in built
    assert built.count(". ") >= 2


def test_a_gap_in_the_parts_does_not_leave_a_dangling_dot():
    """Пустой кусок пропадает целиком, а не оставляет точку посреди абзаца."""
    built = headline.render(done="", use="", act="От тебя ничего не нужно")
    assert built == "От тебя ничего не нужно."


def test_the_title_stands_above_the_paragraph():
    built = headline.render(**PARTS, title="## Лента")
    assert built.startswith("## Лента\n\n")


def test_the_proof_goes_below_the_rule_and_stays_whole():
    details = "## Цель\n" + "подробность\n" * 50
    body = headline.with_details(headline.render(**PARTS), details)
    assert body.index(headline.RULE) < body.index("## Цель")
    # Подробности не сокращаются — ровно то, что задача запретила заодно.
    assert body.count("подробность") == 50


def test_without_proof_there_is_no_dangling_rule():
    assert headline.RULE not in headline.with_details(headline.render(**PARTS), "")


# ── старый бланк остаётся читаемым ───────────────────────────────────────────

OLD_FORM = (
    "**Что требуется от человека** — мержить\n"
    "**Что сделано** — проверка стала строже на две пропущенные вещи\n"
    "**Как посмотреть самому** — `python3 knowledge.py audit`\n"
    "**Итог** — прошло\n"
    "**Чего ждать дальше** — ждёт тебя\n\n---\n\n" + PROOF
)


def test_the_old_form_is_still_read():
    """Комментарии, написанные бланком, уже лежат в живых задачах.

    Перестать их разбирать значило бы ослепить ленту на всей истории — ровно
    то, от чего предостерегало дополнение владельца про связь между агентами.
    """
    got = headline.fields_of(OLD_FORM)
    assert got["что сделано"] == "проверка стала строже на две пропущенные вещи"
    assert got["как посмотреть самому"] == "python3 knowledge.py audit"


def test_the_older_name_of_the_line_is_read_too():
    """«Как воспользоваться» — как строка звалась до первого переименования."""
    got = headline.fields_of("**Как воспользоваться** — стенд на 3002\n")
    assert got["как посмотреть самому"] == "стенд на 3002"


def test_prose_has_no_fields():
    assert headline.fields_of(GOOD) == {}
