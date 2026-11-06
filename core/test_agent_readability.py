"""Новый вид комментария читается машиной так же, как старый (DEV-108).

Главный риск правки «сделать понятно человеку» — ослепить агента: комментарии
в Linear читают не только люди. Проверяющая сессия достаёт из заявки автора
коммит, ветку и прошлый вердикт; лента — роль и вердикт; страница подписи —
раздел «что сделано». Дополнение владельца требовало предъявить это прогоном,
а не рассуждением, — вот прогон.

Способ: один и тот же набор фактов, записанный ДВАЖДЫ — так, как отчёт
выглядел до этой задачи, и так, как он выглядит после. Каждый извлекатель
зовётся на обоих и обязан вернуть одно и то же. Тест поэтому падает не когда
формат поменялся, а когда он поменялся ТОЛЬКО для одного из двух читателей.
"""
import handoff
import headline
import timeline
from agent_factory import signing

COMMIT = "ae53d09"
BRANCH = "agent/DEV-108"
PR = "54"

# Как отчёт выглядел ДО этой задачи: сразу разделы, никакой шапки.
OLD_REPORT = f"""## Цель
Поставить ответ раньше доказательства.

## Формат выхода
Репозиторий agent-factory, ветка `{BRANCH}`, PR #{PR}, коммит `{COMMIT}`.

## Чем наблюдать
```bash
python3 -m pytest core/ -q
```
Ожидаемое: ноль падений.

## Границы
Ничего не отложено.
"""

# Как он выглядел ПОСЛЕ первого захода этой задачи: те же разделы, целиком, но
# под шапкой-бланком и чертой. Владелец бланк вернул — здесь эта форма
# осталась только затем, чтобы машина продолжала её читать: такие комментарии
# уже лежат в живых задачах.
FORM_REPORT = (
    "**Что требуется от человека** — мержить PR #54\n"
    "**Что сделано** — ответ встал раньше доказательства\n"
    "**Как посмотреть самому** — `python3 -m pytest core/ -q` в ветке, ждём ноль падений\n"
    "**Итог** — прошло\n"
    "**Чего ждать дальше** — ждёт тебя, сама задача дальше не поедет\n"
    "\n---\n\n" + OLD_REPORT
)

OLD_VERDICT = f"""## Проверка DEV-108 ДО МЕРЖА (коммит `{COMMIT}`, PR #{PR}) — `OK`

Построчно по пунктам `expected.md`: всё сошлось.
"""

# И как он выглядит СЕЙЧАС: два-три предложения обычного текста. Разделы под
# чертой те же самые — это и есть предмет проверки ниже.
PROSE_REPORT = (
    "Поставил ответ раньше доказательства: комментарий каждой машины теперь "
    "открывается парой обычных предложений, а весь разбор стоит под чертой. "
    "Посмотреть можно так: `python3 -m pytest core/ -q` в ветке, ждём ноль "
    "падений. Всё сошлось, дальше нужен твой мерж PR #54.\n"
    "\n---\n\n" + OLD_REPORT
)

PROSE_VERDICT = (
    f"## Проверка DEV-108 ДО МЕРЖА (коммит `{COMMIT}`, PR #{PR}) — сошлось\n\n"
    "Проверил поведение на ветке, заявленное увидел своими глазами. "
    "Посмотреть то же самое можно, прогнав набор в ветке. От тебя нужен "
    "мерж — сама задача дальше не поедет.\n"
    "\n---\n\n"
    "**Вердикт:** `OK`\n\n"
    "**Источник критерия: задача.**\n\n"
    "Построчно по пунктам `expected.md`: всё сошлось.\n"
)

NEW_VERDICT = (
    f"## Проверка DEV-108 ДО МЕРЖА (коммит `{COMMIT}`, PR #{PR}) — `OK`\n\n"
    "**Что требуется от человека** — мержить\n"
    "**Что сделано** — проверил поведение на ветке\n"
    "**Как посмотреть самому** — прогнать набор в ветке\n"
    "**Итог** — прошло\n"
    "**Чего ждать дальше** — ждёт тебя\n"
    "\n---\n\n"
    "Построчно по пунктам `expected.md`: всё сошлось.\n"
)


def _round(body: str, when: str = "2026-09-16T00:53:18.000Z") -> dict:
    return {"createdAt": when, "body": body, "author": ""}


def _facts(body: str) -> dict:
    """Всё, что машина достаёт из одного комментария."""
    (r,) = timeline.rounds([_round(body)])
    parts = handoff.sections_of(body)
    return {
        "роль": r["role"],
        "вердикт": r["verdict"],
        "коммит": r["commit"],
        "PR": r["pr"],
        "разделы сдачи": sorted(parts),
        "ветка в «Формате выхода»": BRANCH in parts.get(handoff.key_of("Формат выхода"), ""),
    }


def test_the_work_report_gives_a_machine_the_same_facts_in_all_three_shapes():
    """Заявка автора: коммит, ветка, PR, разделы сдачи — во всех трёх видах.

    Форм три, потому что их три и в жизни: отчёты до этой задачи, отчёты
    первого её захода (бланк) и сегодняшние прозой. Все три лежат в живых
    задачах одновременно, и машина обязана читать любой.
    """
    assert _facts(FORM_REPORT) == _facts(OLD_REPORT)
    assert _facts(PROSE_REPORT) == _facts(OLD_REPORT)


def test_the_page_of_signatures_gains_a_summary_it_did_not_have():
    """Единственное, что у машины изменилось, — в бо́льшую сторону.

    Страница подписи собирает карточку по разделам сдачи и ищет среди них
    «что сделано». В старом виде такого раздела не было вовсе (там «Цель»), и
    карточка шла без краткого пересказа; строка шапки даёт его сама. Это
    названо отдельным тестом, а не спрятано в равенстве выше: «стало лучше» —
    тоже изменение поведения, и следующий, кто будет читать эти тесты, должен
    видеть, что оно замечено, а не пропущено.
    """
    assert not signing.sections(OLD_REPORT).get("что")
    assert signing.sections(FORM_REPORT).get("что")


def test_the_verdict_gives_a_machine_the_same_facts_in_both_shapes():
    """Прошлый вердикт и его коммит — то, по чему следующий заход решает,
    проверялся ли уже этот код."""
    assert _facts(NEW_VERDICT) == _facts(OLD_VERDICT)
    assert _facts(NEW_VERDICT)["вердикт"] == "OK"
    assert _facts(NEW_VERDICT)["коммит"] == COMMIT


def test_a_verdict_written_in_prose_still_names_its_commit_and_pr():
    """Из титула вердикта ушёл машинный класс — но коммит, PR и роль остались.

    Класс расхождения ушёл с первого экрана, но не пропал: он стоит первой
    строкой ПОД чертой, помеченной («**Вердикт:** `OK`»), и `verdict_of`
    читает её так же уверенно, как читал титул. Ровно это дополнение
    владельца и требовало предъявить прогоном: человеческое сверху, машинное
    ниже, ничего не удалено.
    """
    got = _facts(PROSE_VERDICT)
    assert got["коммит"] == COMMIT
    assert got["PR"] == PR
    assert got["роль"] == timeline.ROLE_VERIFY
    assert got["вердикт"] == "OK"
    # И на первом экране этого слова нет: оно живёт ниже черты.
    assert "`OK`" not in PROSE_VERDICT[:PROSE_VERDICT.index(headline.RULE)]
    assert headline.lint_text(PROSE_VERDICT, "комментарий") == []


def test_nothing_of_the_old_shape_is_lost_line_by_line():
    """Ни один непустой знак старого вида не пропал в новом.

    Дополнение владельца: «ничего не удалять, меняется только место и
    порядок». Это и проверяется — построчно, а не на глаз.
    """
    for shape in (FORM_REPORT, PROSE_REPORT):
        missing = [l for l in OLD_REPORT.splitlines()
                   if l.strip() and l not in shape.splitlines()]
        assert missing == []


def test_the_headline_stands_above_the_rule_and_the_proof_below_it():
    assert PROSE_REPORT.index("нужен твой мерж") < PROSE_REPORT.index(headline.RULE)
    assert PROSE_REPORT.index(headline.RULE) < PROSE_REPORT.index("## Цель")
    assert headline.lint_text(PROSE_REPORT, "handoff.md") == []


def test_the_timeline_reads_both_shapes_as_one_history():
    """Лента по смешанной истории — старые заходы и новые в одной ленте.

    Задача живёт дольше формата: в ней уже лежат комментарии, написанные до
    этой правки, и лента обязана читать оба.
    """
    text = timeline.render([
        _round(OLD_REPORT, "2026-09-16T00:53:18.000Z"),
        _round(OLD_VERDICT, "2026-09-16T02:16:00.000Z"),
        _round(FORM_REPORT, "2026-09-16T11:00:00.000Z"),
        _round(NEW_VERDICT, "2026-09-16T12:00:00.000Z"),
        _round(PROSE_REPORT, "2026-09-16T13:00:00.000Z"),
        _round(PROSE_VERDICT, "2026-09-16T14:00:00.000Z"),
    ])
    assert "заходов 6, из них проверок 3" in text
    assert headline.lint_text(text, "лента") == []


def test_a_report_written_by_the_old_contract_still_answers():
    """Старый отчёт без шапки — лента отвечает его разделами, а не молчит."""
    done, use = timeline._answers_of(OLD_REPORT)
    assert "ответ раньше доказательства" in done
    assert "pytest" in use


def test_the_old_form_headline_still_wins_over_the_sections():
    """Слова сессии точнее пересказа: бланк есть — берётся он."""
    done, _ = timeline._answers_of(FORM_REPORT)
    assert done == "ответ встал раньше доказательства"


def test_a_prose_report_answers_from_its_sections():
    """У прозы полей нет, и лента отвечает по разделам сдачи — тем же путём,
    что и на отчётах, написанных до всей этой затеи."""
    assert headline.fields_of(PROSE_REPORT) == {}
    done, use = timeline._answers_of(PROSE_REPORT)
    assert "ответ раньше доказательства" in done
    assert "pytest" in use


def test_an_old_field_name_is_still_understood():
    """«Как воспользоваться» переименовано в «Как посмотреть самому», и
    старое имя обязано читаться: комментарии, написанные до переименования,
    уже лежат в задачах и переписываться не будут."""
    old = ("**Что сделано** — поправлено\n"
           "**Как воспользоваться** — `curl /export`\n"
           "**Итог** — прошло\n"
           "**Что требуется от человека** — мержить\n")
    got = headline.fields_of(old)
    assert got[handoff.key_of("Как посмотреть самому")] == "curl /export"


# ── разметка для человека не отнимает у машины ───────────────────────────────

def _long_verdict() -> str:
    """Вердикт, в шапке которого три машинных кода, а коммит назван ниже.

    Так и пишут: «первый заход `OK`, второй `FAIL/implementation`, третий
    `PARTIAL`» — и коммит проверяемой ветки стоит после этого перечня.
    """
    import glossary
    head = (
        "## Проверка DEV-108 ДО МЕРЖА — `FAIL/implementation`\n\n"
        "**Что требуется от человека** — ничего: дефект настоящий, чиню сам\n"
        "**Что сделано** — сверил построчно, вердикты прошлых заходов: `OK`, "
        "потом `PARTIAL`, потом `FAIL/implementation`\n"
        "**Как посмотреть самому** — " + "прогнать набор в ветке, " * 4 + "\n"
        "**Итог** — не прошло\n"
        "**Чего ждать дальше** — вернётся агенту\n\n---\n\n"
        f"Проверял коммит `{COMMIT}`, PR #{PR}. Источник критерия: задача.\n"
    )
    return glossary.decorate(head)


def test_the_human_markup_does_not_push_the_commit_out_of_the_window():
    """Разметка добавляет к каждому коду по полсотни знаков, а извлекатели
    читают первые 500. Три кода в шапке — и коммит уезжает за окно.

    Проверяется на ЖИВОЙ форме: сначала убеждаемся, что разметка правда
    сдвинула коммит за окно (иначе тест проходил бы вхолостую), потом — что
    машина всё равно его находит.
    """
    import glossary
    marked = _long_verdict()
    plain = glossary.undecorate(marked)
    # Оба факта без разметки — в окне, с разметкой — за ним. Обе проверки
    # обязательны: без них тест прошёл бы и на коде без `undecorate`.
    assert plain.index(COMMIT) < timeline._HEAD_WINDOW
    assert plain.index(f"PR #{PR}") < timeline._HEAD_WINDOW
    assert marked.index(COMMIT) > timeline._HEAD_WINDOW
    (r,) = timeline.rounds([_round(marked)])
    assert r["commit"] == COMMIT
    assert r["pr"] == PR
    assert r["verdict"] == "FAIL/implementation"


def test_undecorate_only_strips_what_the_registry_wrote():
    """Чужие скобки в тексте сессии не трогаются."""
    import glossary
    text = "вердикт `OK` (по моей же пробе, а не по чужой)"
    assert glossary.undecorate(text) == text
    assert glossary.undecorate(glossary.decorate("вердикт `OK`")) == "вердикт `OK`"
