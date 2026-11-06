"""Очередь на подпись.

Страница отдаёт постановки, обсуждение и ссылки — то есть всё, что у команды
есть по задаче. Поэтому половина тестов здесь про доступ: адрес живёт в
открытом интернете, за ним Caddy, и ошибиться тут дороже, чем сломать вёрстку.
"""
import asyncio
import json

from agent_factory.signing import SigningPage, links_in, short, waiting_first

TOKEN = "t0ken-32"
TEAM = "team-uuid"


def issue(ident="DEV-1", state="In Review", when="2026-09-12T10:00:00Z",
          comments=(("агент",
                     "Что сделано: сдано. MR https://github.com/o/r/pull/7"),)):
    return {"id": f"i-{ident}", "identifier": ident, "title": f"{ident} — дело",
            "url": f"https://linear.app/x/issue/{ident}", "description": "что сделать",
            "priorityLabel": "High", "updatedAt": when, "state": {"name": state},
            "assignee": {"name": "Андрей"},
            "comments": {"nodes": [{"body": b, "createdAt": when,
                                    "user": {"name": who}} for who, b in comments]}}


class Tracker:
    def __init__(self, issues=(), boom=False):
        self.issues, self.boom, self.asked = list(issues), boom, []

    def issues_awaiting(self, team_id, states):
        self.asked.append((team_id, states))
        if self.boom:
            raise RuntimeError("трекер молчит")
        return list(self.issues)


def page(issues=(), token=TOKEN, **kw):
    return SigningPage(Tracker(issues), TEAM, token, **kw)


def get(p, target="/queue?t=" + TOKEN, headers=None, method="GET"):
    return asyncio.run(p.handle(method, target, headers or {}, b""))


# ── доступ ───────────────────────────────────────────────────────────────

def test_without_a_token_in_the_link_there_is_no_page():
    code, (ctype, body) = get(page([issue()]), target="/queue")
    assert code == 404 and "DEV-1" not in str(body)


def test_a_wrong_token_looks_like_a_missing_page():
    """404, а не 401: чужая ссылка не должна даже подтверждать, что страница
    существует."""
    code, _ = get(page([issue()]), target="/queue?t=не-тот")
    assert code == 404


def test_a_non_ascii_token_is_refused_not_crashed():
    # Тот же класс, что стоил 500 вместо 401 на подписи вебхука.
    code, _ = get(page([issue()]), target="/queue?t=подделка")
    assert code == 404


def test_the_cookie_works_after_the_link():
    code, _ = get(page([issue()]), target="/queue",
                  headers={"cookie": f"signing={TOKEN}"})
    assert code == 200


def test_an_installation_without_a_token_serves_nothing():
    code, _ = get(page([issue()], token=""), target="/queue?t=")
    assert code == 404


# ── что показывает ───────────────────────────────────────────────────────

def test_all_three_tabs_are_on_one_page():
    """Три вкладки — три вопроса: чем фабрика занята, что она проверяет и что
    держит человека. Один запрос в трекер на все три: лишний поход с телефона
    это секунда ожидания и повод не открыть страницу вовсе."""
    p = page([issue("DEV-1", state="In Progress"),
              issue("DEV-2", state="In Review"),
              issue("DEV-3", state="Verifying")])
    code, (_, body) = get(p)
    assert code == 200
    assert "В работе" in body and "На проверке" in body and "На подпись" in body
    assert "DEV-1" in body and "DEV-2" in body and "DEV-3" in body
    # Один запрос, все три набора статусов сразу.
    assert p.tracker.asked == [(TEAM, ("In Progress", "Verifying", "In Review"))]


def test_a_task_being_checked_does_not_vanish_from_the_page():
    """С новым порядком (DEV-78) работа уходит не к человеку, а на проверку.
    Пока страница знала два состояния, задача на проверке пропадала с неё
    целиком: не в работе, не на подписи — нигде. Человек видел, что дел стало
    меньше, хотя их стало столько же."""
    p = page([issue("DEV-9", state="Verifying")])
    code, (_, body) = get(p)
    assert "DEV-9" in body
    assert "Ничего не проверяется." not in body


def test_the_page_needs_no_javascript():
    # Открывается с телефона, часто по плохой связи, и внутри Telegram как
    # мини-приложение: каждый скрипт — ещё один способ показать белый экран.
    code, (_, body) = get(page([issue()]))
    assert "<script" not in body.lower()


def test_the_data_route_gives_both_lists():
    code, (ctype, body) = get(page([issue("DEV-1", state="In Progress"),
                                    issue("DEV-2")]),
                              target="/queue/data?t=" + TOKEN)
    got = json.loads(body)
    assert [t["identifier"] for t in got["в работе"]] == ["DEV-1"]
    assert [t["identifier"] for t in got["на подпись"]] == ["DEV-2"]


def test_the_working_states_are_a_parameter(monkeypatch):
    p = page([issue()], working_states=("Делается",))
    get(p)
    assert p.tracker.asked[-1][1] == ("Делается", "Verifying", "In Review")


def test_the_checking_states_are_a_parameter():
    """Имя состояния проверки у каждой доски своё — зашивать его нельзя."""
    p = page([issue()], checking_states=("Проверяется",))
    get(p)
    assert p.tracker.asked[-1][1] == ("In Progress", "Проверяется", "In Review")


HANDOFF = """**Что сделано.** Убрано наследование в двух местах.
- правило вынесено в одну функцию

**Проверено.** 408 тестов зелёные.

Риск: правка на горячем пути, откатывается мержем.

Не сделал: гашение симптома. MR https://github.com/o/r/pull/7"""


def test_the_card_shows_what_the_decision_needs():
    code, (ctype, body) = get(page([issue(comments=(("агент", HANDOFF),))]))
    assert code == 200 and "text/html" in ctype
    assert "DEV-1" in body
    for name in ("Что сделано", "Риск", "Проверено", "Осталось"):
        assert name in body, name
    assert "правило вынесено в одну функцию" in body


def test_the_card_does_not_copy_the_tracker():
    """Постановку и обсуждение не тащим: они уже есть в трекере, а копия
    делает из карточки простыню, которую пролистывают."""
    code, (_, body) = get(page([issue(comments=(("агент", HANDOFF),
                                                ("Андрей", "второй коммент")))]))
    assert "что сделать" not in body        # описание задачи
    assert "второй коммент" not in body     # чужая реплика
    assert "в трекере" in body              # вместо копии — ссылка
    assert "Убрано наследование" in body    # разбор агента, а не чужая реплика


def test_markdown_is_rendered_not_dumped():
    # Сырая разметка на экране — это и есть «выглядит плохо».
    code, (_, body) = get(page([issue(comments=(("агент", HANDOFF),))]))
    assert "**" not in body and "<b>" not in body.split("<style>")[0]
    assert "<li>" in body and "<ul>" in body
    assert "<pre>" not in body


def test_an_empty_queue_says_so():
    code, (_, body) = get(page([]))
    assert code == 200
    assert "Ничего не ждёт подписи" in body
    assert "Сейчас никто ничего не делает" in body


def test_the_data_route_answers_json():
    code, (ctype, body) = get(page([issue()]), target="/queue/data?t=" + TOKEN)
    assert "application/json" in ctype
    got = json.loads(body)["на подпись"]
    assert got[0]["identifier"] == "DEV-1" and got[0]["links"][0]["kind"] == "MR"


def test_the_data_route_shows_what_is_being_checked():
    """У JSON тот же долг, что у страницы: не показать проверяемое — значит
    соврать, что работы стало меньше. Этим ответом пользуются снаружи."""
    code, (_, body) = get(page([issue("DEV-9", state="Verifying")]),
                          target="/queue/data?t=" + TOKEN)
    got = json.loads(body)
    assert [t["identifier"] for t in got["на проверке"]] == ["DEV-9"]


def test_a_handoff_without_sections_says_so_instead_of_dumping_it():
    code, (_, body) = get(page([issue(comments=(("агент", "просто текст"),))]))
    assert "не оставила разбора" in body


def test_a_long_section_is_cut_with_a_link_to_the_rest():
    from agent_factory.signing import markdown
    out = markdown("Что сделано: " + "слово " * 400, limit=200)
    assert len(out) < 400 and out.endswith("</p>")


def test_a_silent_tracker_shows_the_page_with_the_reason():
    p = SigningPage(Tracker([], boom=True), TEAM, TOKEN)
    code, (_, body) = get(p)
    assert code == 503 and "Трекер не ответил" in body


def test_the_states_are_a_parameter_of_the_installation():
    p = page([issue()], states=("На приёмке", "In Review"))
    get(p)
    assert p.tracker.asked[-1][1] == ("In Progress", "Verifying", "На приёмке", "In Review")


# ── порядок и мелочи ─────────────────────────────────────────────────────

def test_the_longest_waiting_card_is_on_top():
    """Не по приоритету: приоритет решал, что делать раньше, и своё отработал.
    Здесь важно, что дольше держит человека."""
    got = waiting_first([{"updated_at": "2026-09-12T10:00:00Z", "identifier": "B"},
                         {"updated_at": "2026-09-10T10:00:00Z", "identifier": "A"}])
    assert [t["identifier"] for t in got] == ["A", "B"]


def test_html_in_a_task_cannot_break_the_page():
    # Текст пишет модель, а показываем его мы: экранирование обязательно,
    # и оно идёт ПЕРВЫМ шагом, до всякой разметки.
    bad = issue(comments=(("агент", "Что сделано: <script>alert(1)</script>"),))
    code, (_, body) = get(page([bad]))
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_links_are_named_by_what_they_are():
    got = links_in("см. https://gitlab.com/g/p/-/merge_requests/9 и "
                   "https://linear.app/x/issue/DEV-1 и "
                   "https://github.com/o/r/actions/runs/5")
    assert [l["kind"] for l in got] == ["MR", "задача", "прогон"]


def test_a_long_summary_is_cut_not_wrapped():
    assert short("а" * 300, 50).endswith("…") and len(short("а" * 300, 50)) == 50


# ── шапка: чем занята фабрика и когда следующая порция ───────────────────

class FakePuller:
    def __init__(self, left=5400.0, marks=2):
        self.left, self.marks, self.tuned = left, marks, None
        self.max_in_flight, self.max_per_day = 2, 8
        self.per_window, self.window_sec = 2, 7200
        self.max_open, self.opened = 0, 0

    def limits(self):
        return {"max_in_flight": self.max_in_flight,
                "max_per_day": self.max_per_day, "max_open": self.max_open,
                "per_window": self.per_window, "window_sec": self.window_sec}

    async def open_now(self):
        return self.opened

    async def next_batch_in(self, now=None):
        return self.left

    async def _marks(self, now):
        return [now] * self.marks

    async def tune(self, values):
        self.tuned = values
        return self.limits()


def with_head(tmp_path, left=5400.0):
    import json as j
    import os
    d = os.path.join(str(tmp_path), "DEV-20-20260912-150816")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "transcript.jsonl"), "w") as f:
        f.write(j.dumps({"type": "result", "total_cost_usd": 0.54,
                         "num_turns": 9, "duration_ms": 100000,
                         "usage": {"input_tokens": 10, "output_tokens": 6000,
                                   "cache_read_input_tokens": 200000}}) + "\n")
    p = page([issue()])
    p.puller, p.work_root = FakePuller(left), str(tmp_path)
    return p


def test_the_header_says_when_the_next_task_comes(tmp_path):
    """Чтобы с разбором не медлить: видно, сколько времени есть до того, как
    приедет следующая задача."""
    code, (_, body) = get(with_head(tmp_path))
    assert "Следующая задача" in body and "1 ч 30 мин" in body


def test_the_limits_are_visible_without_opening_anything(tmp_path):
    """Их не видно — значит человек не знает, ждать ли и сколько."""
    code, (_, body) = get(with_head(tmp_path))
    assert "по 2 за 2 ч" in body                 # порция и окно
    assert "из 8" in body                        # дневной потолок
    assert "из 2</span>" in body                 # слоты


def test_the_tab_buttons_stay_in_one_row_above_the_cards(tmp_path):
    """Раньше вторая кнопка стояла после первой панели и уезжала вниз вместе с
    её карточками — «на подпись» плясала то вверху, то под списком."""
    p = page([issue("DEV-1", state="In Progress"), issue("DEV-2")])
    code, (_, body) = get(p)
    tabs, pane = body.index('<nav class="tabs">'), body.index('<section class="pane')
    assert tabs < pane
    assert body.count('<nav class="tabs">') == 1


def test_the_configuration_speaks_human(tmp_path):
    """«Окно, секунд» человек не настраивает: он решает, как часто агент берёт
    работу. Числа те же, слова другие."""
    p = with_head(tmp_path)
    code, (_, body) = asyncio.run(
        p.handle("GET", "/queue/config?t=" + TOKEN, {}, b""))
    assert "Сколько задач вести одновременно" in body
    assert "Через сколько часов брать новые" in body and "Окно, сек" not in body
    assert "window_sec" not in body            # секунды наружу не торчат
    assert "Агент берёт до 2 задач, следующие — через 2 ч" in body


def test_there_are_three_fields_not_four(tmp_path):
    """«Сколько брать за раз» и «сколько вести одновременно» были про одно и
    то же: агент берёт ровно столько, сколько может вести."""
    p = with_head(tmp_path)
    code, (_, body) = asyncio.run(
        p.handle("GET", "/queue/config?t=" + TOKEN, {}, b""))
    assert body.count('class="row2"') == 4
    assert "per_window" not in body


def test_hours_are_saved_as_seconds_and_the_batch_follows_the_slots(tmp_path):
    p = with_head(tmp_path)
    asyncio.run(p.handle("POST", "/queue/limits?t=" + TOKEN, {},
                         b"window_hours=4&max_in_flight=3"))
    assert p.puller.tuned == {"max_in_flight": "3", "window_sec": 14400,
                              "per_window": 3}


def test_zero_hours_turns_the_pace_off(tmp_path):
    p = with_head(tmp_path)
    asyncio.run(p.handle("POST", "/queue/limits?t=" + TOKEN, {},
                         b"window_hours=0"))
    assert p.puller.tuned["per_window"] == 0


def test_a_broken_hours_field_does_not_break_the_rest(tmp_path):
    p = with_head(tmp_path)
    asyncio.run(p.handle("POST", "/queue/limits?t=" + TOKEN, {},
                         "window_hours=сколько&max_per_day=5".encode()))
    assert p.puller.tuned == {"max_per_day": "5"}   # часы отброшены, остальное цело


def test_the_pace_is_spelled_out_for_the_immediate_mode():
    from agent_factory.signing import pace_words
    words = pace_words({"per_window": 0, "window_sec": 7200,
                        "max_in_flight": 2, "max_per_day": 8})
    assert "сразу, как освободится место" in words


def test_the_configuration_is_its_own_page(tmp_path):
    p = with_head(tmp_path)
    code, (ctype, body) = asyncio.run(
        p.handle("GET", "/queue/config?t=" + TOKEN, {}, b""))
    assert code == 200 and "text/html" in ctype
    for key in ("max_in_flight", "window_hours", "max_open", "max_per_day"):
        assert key in body
    # На самой очереди формы больше нет — только ссылка в меню.
    code, (_, queue) = get(p)
    assert 'action="limits"' not in queue


def test_the_menu_links_are_absolute_and_carry_the_token(tmp_path):
    """Относительная ссылка `config` со страницы `/queue` уводит на `/config` —
    другого префикса и без токена, то есть в никуда."""
    code, (_, body) = get(with_head(tmp_path))
    assert f'href="/queue/config?t={TOKEN}"' in body
    assert f'href="/queue?t={TOKEN}"' in body


def test_the_menu_follows_the_installations_path(tmp_path):
    # Путь — параметр инсталляции; зашитый «/queue» увёл бы в никуда всех,
    # кто назвал его иначе.
    p = with_head(tmp_path)
    code, (_, body) = asyncio.run(
        p.handle("GET", "/подпись/config?t=" + TOKEN, {}, b""))
    assert f'href="/подпись?t={TOKEN}"' in body


def test_the_menu_is_a_sidebar(tmp_path):
    code, (_, body) = get(with_head(tmp_path))
    assert '<aside class="side">' in body and 'class="burger"' in body


def test_the_configuration_says_it_saved(tmp_path):
    p = with_head(tmp_path)
    code, (_, body) = asyncio.run(
        p.handle("POST", "/queue/limits?t=" + TOKEN, {}, b"max_per_day=4"))
    assert code == 200 and "Сохранено" in body and "max_per_day" in body
    assert f'action="/queue/limits?t={TOKEN}"' in body


def test_the_daily_quota_bar_does_not_collide_with_the_menu_bar(tmp_path):
    """Полоса квоты и верхняя полоса меню назывались одинаково: вложенный
    `div.bar` внутри плитки подхватывал стили липкой шапки и разъезжался синим
    блоком на пол-экрана."""
    code, (_, body) = get(with_head(tmp_path))
    assert '<div class="quota"><i style="width:25%">' in body
    assert '<div class="bar"' not in body          # только header.bar
    assert body.count('class="bar"') == 1


def test_the_header_counts_sessions_and_tokens(tmp_path):
    code, (_, body) = get(with_head(tmp_path))
    assert "Сессии" in body and "Токены за сутки" in body
    assert "206 тыс." in body          # 10 + 6000 + 200000
    assert "$0.54" in body


def test_the_header_is_optional(tmp_path):
    # Нет тяги и нет рабочего каталога — страница всё равно открывается.
    code, (_, body) = get(page([issue()]))
    assert code == 200 and "Следующая порция" not in body


def test_the_limits_can_be_changed_from_the_page(tmp_path):
    p = with_head(tmp_path)
    code, _ = asyncio.run(p.handle("POST", "/queue/limits?t=" + TOKEN, {},
                                   b"max_per_day=4&per_window=1"))
    assert code == 200
    assert p.puller.tuned == {"max_per_day": "4", "per_window": "1"}


def test_a_stranger_cannot_change_the_limits(tmp_path):
    p = with_head(tmp_path)
    code, _ = asyncio.run(p.handle("POST", "/queue/limits", {}, b"max_per_day=50"))
    assert code == 404 and p.puller.tuned is None


def test_the_data_route_carries_the_header(tmp_path):
    code, (ctype, body) = get(with_head(tmp_path),
                              target="/queue/data?t=" + TOKEN)
    got = json.loads(body)["сейчас"]
    assert got["тяга"]["следующая порция"] == "1 ч 30 мин"
    assert got["сессии"]["закончено"] == 1


def test_the_shell_puts_the_menu_button_in_its_own_bar(tmp_path):
    """Бургер жил поверх содержимого и налезал на первую плитку. Теперь у него
    своя полоса: она занимает место в потоке, а не висит над ним."""
    code, (_, body) = get(with_head(tmp_path))
    assert '<header class="bar">' in body
    assert body.index('class="bar"') < body.index('class="head"')
    assert "position:fixed; top:10px; left:10px" not in body   # прежний бургер


def test_the_content_does_not_start_below_a_hole(tmp_path):
    """Пустота под шапкой была отступом под фиксированный бургер: он висел над
    содержимым, и место под него держалось вручную. Полоса теперь в потоке —
    ручной отступ обязан был уйти вместе с ней."""
    code, (_, body) = get(with_head(tmp_path))
    assert "main { padding-top:44px" not in body
    assert body.index('class="head"') < body.index('<nav class="tabs">')


def test_the_counts_name_their_own_source(tmp_path):
    """Два числа рядом читались как противоречие: одно про тягу, другое про
    все сессии. Каждое подписано тем, откуда взялось."""
    code, (_, body) = get(with_head(tmp_path))
    assert "Сессии" in body and "идут сейчас · за сутки" in body


# ── сессии: на что ушли токены, время и память ───────────────────────────

def runs(tmp_path, n=2):
    import json as j
    import os
    import time as t
    for i in range(n):
        d = os.path.join(str(tmp_path), f"DEV-{50 + i}-20260912-1500{i}0")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "transcript.jsonl"), "w") as f:
            f.write(j.dumps({"type": "result", "total_cost_usd": 3.4,
                             "num_turns": 9, "duration_ms": 245000,
                             "usage": {"input_tokens": 10, "output_tokens": 6000,
                                       "cache_read_input_tokens": 200000}}) + "\n")
        with open(os.path.join(d, "stats.json"), "w") as f:
            f.write(j.dumps({"род": "работа", "слот": 1, "пик_мб": 372}))
    p = page([issue()])
    p.puller, p.work_root = FakePuller(), str(tmp_path)
    return p


def test_the_runs_page_shows_what_each_session_cost(tmp_path):
    p = runs(tmp_path)
    code, (ctype, body) = asyncio.run(
        p.handle("GET", "/queue/runs?t=" + TOKEN, {}, b""))
    assert code == 200 and "text/html" in ctype
    assert "DEV-50" in body and "DEV-51" in body
    assert "4 мин" in body and "372 МБ" in body and "$3.40" in body
    assert "206 тыс." in body                 # 10 + 6000 + 200000


def test_the_runs_page_explains_where_the_tokens_went(tmp_path):
    """Из 68 млн токенов 99 % — чтение кэша. Без этой строки число выглядит
    как «мы написали 68 миллионов», а это перечитывание контекста."""
    p = runs(tmp_path)
    code, (_, body) = asyncio.run(
        p.handle("GET", "/queue/runs?t=" + TOKEN, {}, b""))
    assert "перечитывание контекста" in body


def test_a_running_session_is_marked_and_has_no_numbers(tmp_path):
    import os
    d = os.path.join(str(tmp_path), "DEV-60-20260912-180000")
    os.makedirs(d)
    open(os.path.join(d, "transcript.jsonl"), "w").write("{}")
    p = runs(tmp_path)
    code, (_, body) = asyncio.run(
        p.handle("GET", "/queue/runs?t=" + TOKEN, {}, b""))
    assert "DEV-60" in body and "идёт" in body


def test_the_runs_page_is_in_the_menu(tmp_path):
    code, (_, body) = get(with_head(tmp_path))
    assert f'href="/queue/runs?t={TOKEN}"' in body


def test_a_stranger_does_not_see_the_runs(tmp_path):
    p = runs(tmp_path)
    code, _ = asyncio.run(p.handle("GET", "/queue/runs", {}, b""))
    assert code == 404


def test_relative_time_is_readable():
    from agent_factory.signing import when_words
    assert when_words(1000, now=1030) == "только что"
    assert when_words(1000, now=1000 + 900) == "15 мин назад"
    assert when_words(1000, now=1000 + 7200) == "2 ч назад"
    assert when_words(1000, now=1000 + 200000) == "2 д назад"


def test_a_session_can_be_marked_from_the_page(tmp_path):
    """Оценку ставит человек: три из четырёх исходов машина не знает."""
    from agent_factory.verdicts import Verdicts

    class St:
        kv = {}
        async def get(self, k): return self.kv.get(k)
        async def put(self, k, v, ttl): self.kv[k] = v

    p = runs(tmp_path)
    p.verdicts = Verdicts(St())
    code, (_, body) = asyncio.run(
        p.handle("POST", "/queue/mark?t=" + TOKEN, {},
                 b"session=DEV-50-20260912-150000&mark=%D0%BE%D0%BA"))
    assert code == 200 and "Сессии за сутки" in body     # вернулись на список
    assert asyncio.run(p.verdicts.all())["DEV-50-20260912-150000"]["оценка"] == "ок"
    assert "mk-on" in body                                # отметка видна


def test_the_runs_page_shows_the_value_summary(tmp_path):
    from agent_factory.verdicts import Verdicts

    class St:
        kv = {}
        async def get(self, k): return self.kv.get(k)
        async def put(self, k, v, ttl): self.kv[k] = v

    p = runs(tmp_path)
    p.verdicts = Verdicts(St())
    asyncio.run(p.verdicts.put("DEV-50-20260912-15000", "ок"))
    asyncio.run(p.verdicts.put("DEV-51-20260912-15010", "постановка"))
    code, (_, body) = asyncio.run(
        p.handle("GET", "/queue/runs?t=" + TOKEN, {}, b""))
    assert "толк у 100%" in body
    assert "50% работы ушло на то, что не стоило начинать" in body


def test_a_stranger_cannot_mark(tmp_path):
    p = runs(tmp_path)
    code, _ = asyncio.run(p.handle("POST", "/queue/mark", {},
                                   b"session=x&mark=ok"))
    assert code == 404


def test_the_runs_page_links_to_the_tracker(tmp_path):
    p = runs(tmp_path)
    p.issue_base = "https://linear.app/x/issue"
    code, (_, body) = asyncio.run(
        p.handle("GET", "/queue/runs?t=" + TOKEN, {}, b""))
    assert 'href="https://linear.app/x/issue/DEV-50"' in body


def test_an_intake_session_gets_no_tracker_link(tmp_path):
    """Приёмная сессия названа id ЧЕРНОВИКА: задачи в момент её старта ещё
    нет, она её и создаёт. Ссылка вела бы в никуда."""
    import os
    d = os.path.join(str(tmp_path), "INTAKE-7bc73da54fc1-intake-20260912-013622")
    os.makedirs(d)
    open(os.path.join(d, "transcript.jsonl"), "w").write("{}")
    p = runs(tmp_path, n=0)
    p.issue_base = "https://linear.app/x/issue"
    code, (_, body) = asyncio.run(
        p.handle("GET", "/queue/runs?t=" + TOKEN, {}, b""))
    assert "INTAKE-7bc73da54fc1" in body
    assert "issue/INTAKE" not in body


# ── доступ по подписи Telegram, а не по знанию ссылки ────────────────────

import hashlib as _hl
import hmac as _hm
import json as _js
import time as _t
import urllib.parse as _up

BOT = "123456:AAHf-secret"
ME = 1000000001


def tg_data(user_id=ME, token=BOT):
    user = _js.dumps({"id": user_id, "first_name": "Андрей"},
                     separators=(",", ":"), ensure_ascii=False)
    f = {"user": user, "auth_date": str(int(_t.time())), "query_id": "AAH"}
    line = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    secret = _hm.new(b"WebAppData", token.encode(), _hl.sha256).digest()
    f["hash"] = _hm.new(secret, line.encode(), _hl.sha256).hexdigest()
    return _up.urlencode(f)


def guarded(tmp_path=None):
    p = page([issue()])
    p.bot_token, p.allowed_ids = BOT, {str(ME)}
    return p


def test_the_token_alone_stops_being_enough():
    """Ссылку можно переслать, подпись — нет. Поэтому при включённой проверке
    знание адреса больше не пускает."""
    code, (_, body) = get(guarded())
    assert code == 200 and "DEV-1" not in body      # задач не видно
    assert "Проверяю, кто пришёл" in body


def test_a_signed_visit_gets_a_cookie_and_the_page():
    p = guarded()
    code, out = asyncio.run(p.handle("POST", "/queue/auth?t=" + TOKEN, {},
                                     ("data=" + _up.quote(tg_data())).encode()))
    assert code == 200 and len(out) == 3
    assert "signing_who=1000000001" in out[2]["Set-Cookie"]
    assert "HttpOnly" in out[2]["Set-Cookie"] and "Secure" in out[2]["Set-Cookie"]


def test_the_cookie_opens_the_page():
    p = guarded()
    code, (_, body) = get(p, headers={"cookie": f"signing_who={ME}"})
    assert code == 200 and "DEV-1" in body


def test_a_stranger_with_a_valid_signature_is_refused():
    p = guarded()
    code, out = asyncio.run(p.handle("POST", "/queue/auth?t=" + TOKEN, {},
                                     ("data=" + _up.quote(tg_data(user_id=777))).encode()))
    assert code == 403 and "Доступ закрыт" in out[1]


def test_a_forged_signature_is_refused():
    p = guarded()
    code, out = asyncio.run(p.handle(
        "POST", "/queue/auth?t=" + TOKEN, {},
        ("data=" + _up.quote(tg_data(token="999:OTHER"))).encode()))
    assert code == 403


def test_a_foreign_cookie_does_not_open_the_page():
    p = guarded()
    code, (_, body) = get(p, headers={"cookie": "signing_who=777"})
    assert "DEV-1" not in body and "Проверяю, кто пришёл" in body


def test_without_a_list_everything_works_as_before():
    # Инсталляция, которая проверку не включала, не должна ничего заметить.
    code, (_, body) = get(page([issue()]))
    assert "DEV-1" in body and "Проверяю, кто пришёл" not in body


def test_the_gate_says_what_to_do_outside_telegram():
    code, (_, body) = get(guarded())
    assert "<noscript>" in body and "кнопкой в боте" in body
    assert body.count("<script") == 1      # единственный скрипт на всём сайте


def test_the_header_shows_what_actually_stops_the_pull(tmp_path):
    """Плитка «взято за сутки» отвечала про производство. Тягу останавливает
    приёмка — её и показываем."""
    p = with_head(tmp_path)
    p.puller.max_open, p.puller.opened = 7, 9
    code, (_, body) = get(p)
    assert "На подписи" in body
    # Дробь зажата потолком: «9 из 7» читается как поломка, а не как «мест
    # нет». Настоящее число уходит в подпись.
    assert "7<span class=\"unit\"> из 7</span>" in body and "из 7</span>" in body
    assert "мест нет, на разборе 9" in body
    assert 'class="quota quota-full"' in body   # переполнено — цветом


def test_exactly_at_the_cap_says_no_room_without_a_number(tmp_path):
    p = with_head(tmp_path)
    p.puller.max_open, p.puller.opened = 7, 7
    code, (_, body) = get(p)
    assert "мест нет — новые не берём" in body
    assert "на разборе" not in body


def test_room_below_the_cap_is_spelled_out(tmp_path):
    p = with_head(tmp_path)
    p.puller.max_open, p.puller.opened = 7, 4
    code, (_, body) = get(p)
    assert "есть запас на 3" in body
    assert 'class="quota quota-full"' not in body


def test_the_next_task_is_called_a_task(tmp_path):
    code, (_, body) = get(with_head(tmp_path))
    assert "Следующая задача" in body and "Следующая порция" not in body


# ── свидетельства рядом с вердиктом, а не проза одна (DEV-94) ─────────────

def checked(tmp_path, gates=True, expected=True, verdict=True, session=True):
    """Задача на подписи и рабочий каталог с её последней проверкой."""
    from agent_factory.test_evidence import EXPECTED, GATES, VERDICT, verify_dir
    if session:
        stats = {"исход": "READY_FOR_HUMAN", "критерий по воротам": "задача"}
        if gates:
            stats["ворота сессии"] = {"итог": "не прошло",
                                      "почему": "упали ворота: logdiff (код 10)",
                                      "записи": GATES}
        verify_dir(tmp_path, stats=stats, gates=GATES if gates else None,
                   expected=EXPECTED if expected else None,
                   verdict=VERDICT if verdict else None)
    p = page([issue()])
    p.work_root = str(tmp_path)
    return p


def test_the_card_shows_commands_codes_and_the_line_by_line_check(tmp_path):
    """Критерий приёмки DEV-94: открыл карточку — видны команды, коды и
    построчная сверка, а не только рассказ сессии о себе."""
    p = checked(tmp_path)
    _, (_, body) = get(p)
    assert "python3 logdiff.py --since 10m api" in body     # команда
    assert '<td class="rc">10</td>' in body                   # код возврата logdiff
    assert "упали ворота: logdiff (код 10)" in body           # суд ворот
    assert "GET /health на dev-стенде" in body                # пункт эталона
    assert "ожидалось: 200" in body
    assert "без ответа" in body                               # пункт без строки
    assert "Источник критерия: задача · по воротам: задача" in body
    # Проза осталась — но не одна: свидетельства стоят раньше неё.
    assert body.index("Свидетельства") < body.index("Что сделано")


def test_a_verdict_without_gates_says_no_evidence_out_loud(tmp_path):
    """Второй критерий: вердикт без gates.json → явная строка «свидетельств
    нет». Молчание подписывающий прочитал бы как «всё в порядке»."""
    p = checked(tmp_path, gates=False)
    _, (_, body) = get(p)
    assert "Свидетельств нет: gates.json не записан" in body
    assert "свидетельств нет" in body                         # и в шапке карточки
    assert "ожидалось: 200" in body                           # эталон при этом виден


def test_a_verdict_without_a_reference_says_so(tmp_path):
    p = checked(tmp_path, expected=False)
    _, (_, body) = get(p)
    assert "Эталона нет: expected.md не записан" in body
    assert "ворота: не прошло" in body


def test_a_task_never_checked_says_so_instead_of_nothing(tmp_path):
    p = checked(tmp_path, session=False)
    _, (_, body) = get(p)
    assert "Свидетельств нет: состоявшейся проверки по задаче не было" in body


def test_without_a_work_root_the_card_says_why_there_is_nothing():
    """Инсталляция без рабочего каталога на этой машине — не «проверки не
    было», а «смотреть негде». Разные факты, разные слова."""
    p = page([issue()])
    _, (_, body) = get(p)
    assert "Свидетельств нет: рабочий каталог сессий на этой машине не задан" in body


def test_the_glance_badge_answers_from_the_gates_not_the_prose(tmp_path):
    """В шапке карточки — суд ворот, а не слово модели: вердикт говорит OK,
    ворота — «не прошло», и в шапке стоит второе."""
    p = checked(tmp_path)
    _, (_, body) = get(p)
    head = body[body.index('<summary>'):body.index('</summary>')]
    assert "ворота: не прошло" in head and "OK всё проверено" not in head


def test_evidence_in_a_verdict_cannot_break_the_page(tmp_path):
    from agent_factory.test_evidence import verify_dir
    verify_dir(tmp_path, stats={}, gates=[{"ворота": "<b>x</b>", "итог": "прошло",
                                           "команда": "echo <script>alert(1)</script>",
                                           "код": 0}],
               verdict="OK <img src=x onerror=alert(1)>\n")
    p = page([issue()])
    p.work_root = str(tmp_path)
    _, (_, body) = get(p)
    assert "<script>alert" not in body and "<img src=x" not in body
    assert "&lt;script&gt;" in body and "&lt;img src=x" in body


def test_the_data_route_carries_the_evidence(tmp_path):
    p = checked(tmp_path)
    _, (_, body) = get(p, "/queue/data?t=" + TOKEN)
    card = json.loads(body)["на подпись"][0]
    proof = card["свидетельства"]
    assert proof["ворота"]["записи"][0]["код"] == 10
    assert [r["ответ"] for r in proof["сверка"]] == ["сошлось", "не сошлось", "без ответа"]


def test_a_broken_gates_file_is_shown_as_a_harness_error(tmp_path):
    from agent_factory.test_evidence import verify_dir
    verify_dir(tmp_path, stats={"исход": "FAILED"}, gates="{broken",
               verdict="OK всё хорошо\n")
    p = page([issue()])
    p.work_root = str(tmp_path)
    _, (_, body) = get(p)
    assert "ошибка обвязки" in body and "не разобран" in body
    assert "Свидетельств нет: gates.json" not in body


# ── действия: «Смержить» и обновление ядра ───────────────────────────────
#
# Владелец: «мне каждый раз логиниться в GitHub». Кнопка мержа — на карточке,
# рядом со свидетельствами. Правила дороже вёрстки: только по подписи
# Telegram, всегда через подтверждение, на непрошедшем — предупреждение и
# галка, в журнал — кто и что нажал.

import logging as _logging

from agent_factory import signing as _signing


class Hosting:
    """`request` — один и тот же PR на любую задачу; `tasks` — по задачам,
    когда на странице их несколько и ответ у них разный."""

    def __init__(self, request=None, boom=False, merge_boom=False, tasks=None):
        self.request, self.boom, self.merge_boom = request, boom, merge_boom
        self.tasks, self.merged, self.asked = tasks, [], []

    def _of(self, ident):
        if self.tasks is not None:
            return self.tasks.get(ident)
        return self.request

    def open_request(self, ident):
        self.asked.append(ident)
        if self.boom:
            raise RuntimeError("gh: HTTP 502")
        req = self._of(ident)
        return dict(req) if req else None

    def open_requests(self, idents):
        self.asked.append(list(idents))
        if self.boom:
            raise RuntimeError("gh: HTTP 502")
        return {i: dict(self._of(i)) for i in idents if self._of(i)}


    def merge(self, req):
        if self.merge_boom:
            raise RuntimeError("Pull request is not mergeable")
        self.merged.append(req)
        return f"смержен PR #{req['number']}"


class OldHosting(Hosting):
    """Хостинг инсталляции, собранной до DEV-120: списка не знает."""
    open_requests = None


class Verification:
    def __init__(self, answer="done"):
        self.accepted, self.closed, self.answer = [], [], answer

    async def accept_merged(self, ident, facts, by=""):
        self.accepted.append((ident, facts, by)); return self.answer

    async def accept_done(self, ident, by=""):
        self.closed.append((ident, by)); return self.answer


REQ = {"number": "29", "repo": "o/dok-engine", "url": "https://gh/o/dok-engine/pull/29",
       "source_ref": "agent/DEV-1-preflight", "target_ref": "main",
       "title": "Ворота", "mergeable": True}

PASSED_PROOF = {"сессия": "DEV-1-verify-1", "когда": 0.0, "исход": "VERIFIED",
                "вердикт": "OK всё сошлось", "ворота": {"итог": "прошло",
                "почему": "прошли все ворота (2): тесты, линтер", "записи": []},
                "эталон": None, "сверка": [], "ответов": 0}
FAILED_PROOF = {**PASSED_PROOF, "исход": "FAILED", "вердикт": "FAIL ручка отдала 500",
                "ворота": {"итог": "не прошло", "почему": "упали ворота: тесты (код 1)",
                           "записи": []}}


def acting(issues=None, hosting=None, proof=PASSED_PROOF, monkeypatch=None, **kw):
    """Страница с хостингом, подписью Telegram и свидетельствами под рукой."""
    p = page(issues if issues is not None else [issue()],
             hosting=hosting if hosting is not None else Hosting(REQ),
             verification=Verification(), work_root="/proofs",
             risky_states=("Not Verified",), **kw)
    p.bot_token, p.allowed_ids = BOT, {str(ME)}
    if monkeypatch is not None:
        monkeypatch.setattr(_signing.ev, "collect",
                            lambda root, idents: {i: proof for i in idents})
    return p


SIGNED = {"cookie": f"signing_who={ME}"}


def post(p, tail, data, headers=SIGNED):
    import urllib.parse as u
    return asyncio.run(p.handle("POST", f"/queue{tail}?t={TOKEN}", headers,
                                u.urlencode(data).encode()))


def test_the_merge_button_stands_on_cards_awaiting_a_signature(monkeypatch):
    p = acting(monkeypatch=monkeypatch)
    code, (_, body) = get(p, headers=SIGNED)
    assert code == 200 and 'action="/queue/merge?t=' in body and "Смержить…" in body
    assert 'name="task" value="DEV-1"' in body


def test_a_task_in_not_verified_gets_the_button_with_a_risk_word(monkeypatch):
    p = acting([issue("DEV-2", state="Not Verified")], monkeypatch=monkeypatch,
               states=("In Review", "Not Verified"))
    code, (_, body) = get(p, headers=SIGNED)
    assert "Смержить с риском…" in body and "act-risk" in body


# ── мержить нечего: кнопка решается при отрисовке (DEV-120) ─────────────
#
# Андрей нажал «Смержить» на задаче-решении, подождал опроса хостинга и
# получил «открытого MR нет». Задача осталась в стопке навсегда: другой
# кнопки на карточке не было. Дословно: «если ветки нет, то тоже не надо эту
# кнопку делать».


def test_a_task_without_an_open_request_gets_closing_instead_of_merging(monkeypatch):
    p = acting(hosting=Hosting(None), monkeypatch=monkeypatch)
    code, (_, body) = get(p, headers=SIGNED)
    assert code == 200 and "Смержить" not in body
    assert "Закрыть как сделанную…" in body and 'action="/queue/done?t=' in body


def test_a_task_with_an_open_request_still_gets_the_merge_button(monkeypatch):
    p = acting(monkeypatch=monkeypatch)
    code, (_, body) = get(p, headers=SIGNED)
    assert "Смержить…" in body and "Закрыть как сделанную" not in body


def test_two_cards_get_different_buttons_by_their_own_answer(monkeypatch):
    """Ответ на «есть ли что мержить» — у каждой задачи свой; общего «на
    странице есть PR» не бывает."""
    p = acting([issue("DEV-29"), issue("DEV-102")], monkeypatch=monkeypatch,
               hosting=Hosting(tasks={"DEV-102": REQ}))
    code, (_, body) = get(p, headers=SIGNED)
    card29 = body.split("DEV-29", 1)[1].split("</details>", 1)[0]
    card102 = body.split("DEV-102", 1)[1].split("</details>", 1)[0]
    assert "Закрыть как сделанную…" in card29 and "Смержить" not in card29
    assert "Смержить…" in card102 and "Закрыть как сделанную" not in card102


def test_the_whole_page_asks_the_hosting_once(monkeypatch):
    """Вопрос при отрисовке стоит одного похода на страницу, а не похода на
    карточку: иначе ожидание переехало бы с нажатия на открытие страницы."""
    p = acting([issue("DEV-29"), issue("DEV-53"), issue("DEV-102")],
               monkeypatch=monkeypatch, hosting=Hosting(tasks={"DEV-102": REQ}))
    get(p, headers=SIGNED)
    assert p.hosting.asked == [["DEV-29", "DEV-53", "DEV-102"]]


def test_a_card_that_is_not_the_humans_to_decide_is_not_asked_about(monkeypatch):
    """Задачи в работе и на проверке хостингу не показываем: кнопок на них
    нет, а вопрос про них — лишнее ожидание на каждой отрисовке."""
    p = acting([issue("DEV-1"), issue("DEV-7", state="In Progress"),
                issue("DEV-8", state="Verifying")], monkeypatch=monkeypatch)
    get(p, headers=SIGNED)
    assert p.hosting.asked == [["DEV-1"]]


def test_an_old_hosting_without_the_list_is_asked_one_by_one(monkeypatch):
    """Инсталляция старой сборки списка не знает — страница всё равно обязана
    показать верную кнопку, а не свалиться."""
    p = acting([issue("DEV-29"), issue("DEV-102")], monkeypatch=monkeypatch,
               hosting=OldHosting(tasks={"DEV-102": REQ}))
    code, (_, body) = get(p, headers=SIGNED)
    assert code == 200 and p.hosting.asked == ["DEV-29", "DEV-102"]
    assert "Закрыть как сделанную…" in body and "Смержить…" in body


def test_a_silent_hosting_leaves_the_merge_button_where_it_was(monkeypatch, caplog):
    """Молчание хостинга — не «мержить нечего». Иначе человек закрыл бы как
    сделанную задачу, у которой PR есть."""
    p = acting(hosting=Hosting(REQ, boom=True), monkeypatch=monkeypatch)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        code, (_, body) = get(p, headers=SIGNED)
    assert code == 200 and "Смержить…" in body and "Закрыть как сделанную" not in body
    assert any("открытые PR не спросить" in r.getMessage() for r in caplog.records)


def test_a_hosting_answering_the_wrong_shape_costs_buttons_not_the_page(monkeypatch):
    """Хостинг пишет инсталляция. Ответ не той формы должен стоить кнопки, а
    не всей очереди: страница отдаёт и постановки, и свидетельства."""
    class Wrong(Hosting):
        def open_requests(self, idents):
            return ["DEV-1"]

    p = acting(hosting=Wrong(REQ), monkeypatch=monkeypatch)
    code, (_, body) = get(p, headers=SIGNED)
    assert code == 200 and "DEV-1" in body and "Смержить…" in body


def test_closing_as_done_goes_through_a_confirmation(monkeypatch):
    p = acting(hosting=Hosting(None), monkeypatch=monkeypatch)
    code, (_, body) = post(p, "/done", {"task": "DEV-1"})
    assert code == 200 and "Закрыть DEV-1 как сделанную?" in body
    assert 'action="/queue/done/confirm?t=' in body
    assert "мержить нечего" in body and p.verification.closed == []


def test_the_confirmation_closes_the_task_and_names_who(monkeypatch, caplog):
    p = acting(hosting=Hosting(None), monkeypatch=monkeypatch)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        code, (_, body) = post(p, "/done/confirm", {"task": "DEV-1"})
    assert code == 200 and p.verification.closed == [("DEV-1", str(ME))]
    assert "переведена в Done" in body
    assert any("закрытие без мержа" in r.getMessage().lower() and str(ME) in r.getMessage()
               for r in caplog.records)


def test_a_task_that_grew_a_pull_request_is_not_closed(monkeypatch, caplog):
    """Между отрисовкой и нажатием PR мог появиться — тогда работу надо
    мержить, а не хоронить."""
    p = acting(hosting=Hosting(REQ), monkeypatch=monkeypatch)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        code, (_, body) = post(p, "/done/confirm", {"task": "DEV-1"})
    assert code == 409 and p.verification.closed == []
    assert "#29" in body and "Смержить" in body


def test_a_silent_hosting_does_not_block_the_closing(monkeypatch):
    """Дверь не должна запираться ещё раз: хостинг молчит — закрываем по
    решению человека и говорим, что перепроверить не вышло."""
    p = acting(hosting=Hosting(None, boom=True), monkeypatch=monkeypatch)
    code, (_, body) = post(p, "/done/confirm", {"task": "DEV-1"})
    assert code == 200 and p.verification.closed == [("DEV-1", str(ME))]
    assert "Хостинг не ответил" in body


def test_closing_an_unverified_task_needs_the_checkbox(monkeypatch, caplog):
    p = acting([issue("DEV-2", state="Not Verified")], hosting=Hosting(None),
               proof=FAILED_PROOF, monkeypatch=monkeypatch,
               states=("In Review", "Not Verified"))
    with caplog.at_level(_logging.WARNING, logger="intake"):
        code, (_, body) = post(p, "/done/confirm", {"task": "DEV-2"})
    assert code == 400 and p.verification.closed == [] and "галк" in body
    code, _ = post(p, "/done/confirm", {"task": "DEV-2", "ack": "1"})
    assert code == 200 and p.verification.closed == [("DEV-2", str(ME))]


def test_the_confirmation_of_an_unverified_task_says_what_did_not_add_up(monkeypatch):
    p = acting([issue("DEV-2", state="Not Verified")], hosting=Hosting(None),
               proof=FAILED_PROOF, monkeypatch=monkeypatch,
               states=("In Review", "Not Verified"))
    code, (_, body) = post(p, "/done", {"task": "DEV-2"})
    assert code == 200 and "Действие с риском" in body and 'name="ack"' in body


def test_closing_without_a_signature_is_a_403():
    p = page([issue()], hosting=Hosting(None), verification=Verification())
    p.bot_token, p.allowed_ids = BOT, {str(ME)}
    assert post(p, "/done", {"task": "DEV-1"}, headers={})[0] == 403
    assert post(p, "/done/confirm", {"task": "DEV-1"}, headers={})[0] == 403


def test_a_nonsense_task_is_not_closed(monkeypatch):
    p = acting(hosting=Hosting(None), monkeypatch=monkeypatch)
    assert post(p, "/done", {"task": "../etc"})[0] == 400
    assert post(p, "/done/confirm", {"task": "x"})[0] == 400
    assert p.verification.closed == []


def test_without_a_reception_the_page_says_where_to_close_it_by_hand(monkeypatch):
    p = acting(hosting=Hosting(None), monkeypatch=monkeypatch)
    p.verification = None
    code, (_, body) = post(p, "/done/confirm", {"task": "DEV-1"})
    assert code == 200 and "руками" in body


def test_without_a_hosting_there_is_no_button():
    p = page([issue()])
    p.bot_token, p.allowed_ids = BOT, {str(ME)}
    code, (_, body) = get(p, headers=SIGNED)
    assert code == 200 and "Смержить" not in body


def test_a_task_being_worked_on_has_no_button(monkeypatch):
    p = acting([issue("DEV-3", state="In Progress")], monkeypatch=monkeypatch)
    code, (_, body) = get(p, headers=SIGNED)
    assert "DEV-3" in body and "Смержить" not in body


def test_an_action_without_the_telegram_signature_is_refused_and_logged(monkeypatch, caplog):
    """Токен в ссылке пересылается, подпись — нет. Действие по одному токену
    — это мерж по чужой пересланной ссылке."""
    p = acting(monkeypatch=monkeypatch)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        code, (_, body) = post(p, "/merge", {"task": "DEV-1"}, headers={"cookie": f"signing={TOKEN}"})
    assert code == 403 and "через Telegram" in body
    assert p.hosting.merged == [] and any("отклонено" in r.getMessage() for r in caplog.records)


def test_an_installation_without_the_allowed_list_never_acts(monkeypatch):
    # Без списка страница — по токену. Раздавать право мержа по ссылке нельзя.
    p = page([issue()], hosting=Hosting(REQ), verification=Verification())
    code, (_, body) = get(p)
    assert "Смержить" not in body
    assert post(p, "/merge", {"task": "DEV-1"}, headers={})[0] == 403


def test_the_preview_shows_the_request_the_verdict_and_the_gates(monkeypatch):
    p = acting(monkeypatch=monkeypatch)
    code, (_, body) = post(p, "/merge", {"task": "DEV-1"})
    assert code == 200
    assert "#29" in body and "o/dok-engine" in body
    assert "agent/DEV-1-preflight" in body and "main" in body
    assert "OK всё сошлось" in body and "прошли все ворота" in body
    assert "можно мержить" in body and 'name="ack"' not in body
    assert 'action="/queue/merge/confirm?t=' in body
    assert 'name="number" value="29"' in body and 'name="repo" value="o/dok-engine"' in body
    assert p.hosting.merged == []                    # показ — не действие


def test_a_failed_check_makes_the_preview_a_warning_with_a_checkbox(monkeypatch):
    """«Изменений не было, не прошло проверку, хотите ли вы это сделать?» —
    словами, что именно не сошлось, и галка, без которой форма не уйдёт."""
    p = acting([issue("DEV-1", state="Not Verified")], proof=FAILED_PROOF,
               monkeypatch=monkeypatch, states=("In Review", "Not Verified"))
    p.tracker.issue_by_identifier = lambda i: {"state": {"name": "Not Verified"}}
    code, (_, body) = post(p, "/merge", {"task": "DEV-1"})
    assert code == 200 and "Действие с риском" in body
    assert "Not Verified" in body and "проверку не прошла" in body
    assert "FAIL ручка отдала 500" in body and "упали ворота" in body
    assert 'name="ack" value="1" required' in body
    assert "Смержить с риском" in body


def test_no_check_at_all_is_a_risk_too(monkeypatch):
    p = acting(proof=None, monkeypatch=monkeypatch)
    code, (_, body) = post(p, "/merge", {"task": "DEV-1"})
    assert "свидетельств нет" in body and 'name="ack"' in body


def test_the_risk_is_read_from_the_state_and_the_evidence(monkeypatch):
    p = acting(monkeypatch=monkeypatch)
    assert p.risk_of("In Review", PASSED_PROOF) == []
    assert any("не прошла" in w for w in p.risk_of("Not Verified", PASSED_PROOF))
    assert any("FAIL" in w for w in p.risk_of("In Review", FAILED_PROOF))
    assert any("упали ворота" in w for w in p.risk_of("In Review", FAILED_PROOF))
    assert any("свидетельств нет" in w for w in p.risk_of("In Review", {}))
    assert any("gates.json" in w for w in p.risk_of("In Review", {**PASSED_PROOF, "ворота": None}))


def test_the_confirmation_merges_closes_the_task_and_names_who(monkeypatch, caplog):
    p = acting(monkeypatch=monkeypatch)
    with caplog.at_level(_logging.INFO, logger="intake"):
        code, (_, body) = post(p, "/merge/confirm",
                               {"task": "DEV-1", "repo": "o/dok-engine", "number": "29"})
    assert code == 200 and "смержен PR #29" in body and "переведена в Done" in body
    assert p.hosting.merged == [REQ]
    (ident, facts, by), = p.verification.accepted
    assert ident == "DEV-1" and by == str(ME)
    assert facts["mr_iid"] == "29" and facts["project_path"] == "o/dok-engine"
    assert facts["source_ref"] == "agent/DEV-1-preflight" and facts["ref"] == "main"
    said = [r.getMessage() for r in caplog.records if "мержит" in r.getMessage()]
    assert said and str(ME) in said[0] and "o/dok-engine#29" in said[0]


def test_a_risky_merge_without_the_checkbox_does_nothing(monkeypatch, caplog):
    p = acting(proof=FAILED_PROOF, monkeypatch=monkeypatch)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        code, (_, body) = post(p, "/merge/confirm",
                               {"task": "DEV-1", "repo": "o/dok-engine", "number": "29"})
    assert code == 400 and "Нужно подтверждение" in body and "FAIL" in body
    assert p.hosting.merged == [] and p.verification.accepted == []
    assert any("без галки" in r.getMessage() for r in caplog.records)


def test_a_risky_merge_with_the_checkbox_goes_through_and_is_loud(monkeypatch, caplog):
    p = acting(proof=FAILED_PROOF, monkeypatch=monkeypatch)
    with caplog.at_level(_logging.WARNING, logger="intake"):
        code, _ = post(p, "/merge/confirm", {"task": "DEV-1", "repo": "o/dok-engine",
                                             "number": "29", "ack": "1"})
    assert code == 200 and p.hosting.merged == [REQ]
    assert any("ПОВЕРХ РИСКА" in r.getMessage() and "FAIL" in r.getMessage()
               for r in caplog.records)


def test_a_request_that_changed_since_the_preview_is_not_merged(monkeypatch):
    """За минуту PR могли смержить, закрыть или открыть другой. Мержить
    «какой-нибудь» нельзя — только тот, что человек видел."""
    p = acting(monkeypatch=monkeypatch)
    code, (_, body) = post(p, "/merge/confirm", {"task": "DEV-1", "repo": "o/dok-engine",
                                                 "number": "28"})
    assert code == 409 and "изменился" in body and p.hosting.merged == []
    p.hosting.request = None
    code, _ = post(p, "/merge/confirm", {"task": "DEV-1", "repo": "o/dok-engine", "number": "29"})
    assert code == 409


def test_without_an_open_request_the_page_says_so(monkeypatch):
    p = acting(hosting=Hosting(None), monkeypatch=monkeypatch)
    code, (_, body) = post(p, "/merge", {"task": "DEV-1"})
    assert code == 200 and "открытого PR нет" in body


def test_a_hosting_that_does_not_answer_is_a_503_not_a_trace(monkeypatch):
    p = acting(hosting=Hosting(REQ, boom=True), monkeypatch=monkeypatch)
    code, (_, body) = post(p, "/merge", {"task": "DEV-1"})
    assert code == 503 and "HTTP 502" in body


def test_a_refused_merge_shows_the_reason_and_leaves_the_task(monkeypatch, caplog):
    p = acting(hosting=Hosting(REQ, merge_boom=True), monkeypatch=monkeypatch)
    with caplog.at_level(_logging.ERROR, logger="intake"):
        code, (_, body) = post(p, "/merge/confirm", {"task": "DEV-1", "repo": "o/dok-engine",
                                                     "number": "29"})
    assert code == 502 and "not mergeable" in body and p.verification.accepted == []


def test_a_tracker_that_could_not_close_is_said_on_the_page(monkeypatch):
    p = acting(monkeypatch=monkeypatch)
    p.verification = Verification(answer="tracker-failed")
    code, (_, body) = post(p, "/merge/confirm", {"task": "DEV-1", "repo": "o/dok-engine",
                                                 "number": "29"})
    assert code == 200 and "смержен" in body and "переведи задачу руками" in body


def test_a_garbled_task_name_is_refused(monkeypatch):
    p = acting(monkeypatch=monkeypatch)
    assert post(p, "/merge", {"task": "../etc"})[0] == 400
    assert post(p, "/merge/confirm", {"task": "x", "repo": "o/r", "number": "1"})[0] == 400


def test_the_action_paths_resolve_to_the_queue_base():
    assert SigningPage.base_of("/queue/merge") == "/queue"
    assert SigningPage.base_of("/queue/merge/confirm") == "/queue"
    assert SigningPage.base_of("/queue/update/confirm") == "/queue"
    assert SigningPage.base_of("/q/update") == "/q"


# ── обновление ядра со страницы конфигурации ─────────────────────────────

class Updater:
    def __init__(self, current="aaa111", available="bbb222"):
        self.current, self.available, self.performed = current, available, []

        class Source:
            def describe(s, v): return {"aaa111": "0.7.8", "bbb222": "0.7.9"}.get(v, "")
        self.source = Source()

    async def drift(self):
        return self.current, self.available

    async def perform(self, version, user_id=None):
        self.performed.append((version, user_id))
        return "applied", f"⬆️ Ядро → {version}: готово"


def test_the_configuration_offers_the_core_update_only_to_a_signed_viewer():
    p = acting(); p.updater = Updater()
    code, (_, body) = get(p, target="/queue/config?t=" + TOKEN, headers=SIGNED)
    assert code == 200 and "Стоит 0.7.8" in body and "доступна 0.7.9" in body
    assert 'action="/queue/update?t=' in body and 'name="version" value="bbb222"' in body
    code, (_, body) = get(p, target="/queue/config?t=" + TOKEN, headers={"cookie": f"signing={TOKEN}"})
    assert "Обновить до" not in body


def test_an_up_to_date_core_has_no_button():
    p = acting(); p.updater = Updater(current="bbb222")
    code, (_, body) = get(p, target="/queue/config?t=" + TOKEN, headers=SIGNED)
    assert "последняя версия" in body and 'action="/queue/update?t=' not in body


def test_the_core_update_goes_through_a_confirmation_and_names_who(caplog):
    p = acting(); p.updater = Updater()
    code, (_, body) = post(p, "/update", {"version": "bbb222"})
    assert code == 200 and "Обновить ядро до 0.7.9?" in body and 'action="/queue/update/confirm?t=' in body
    assert p.updater.performed == []
    with caplog.at_level(_logging.WARNING, logger="intake"):
        code, (_, body) = post(p, "/update/confirm", {"version": "bbb222"})
    assert code == 200 and "готово" in body
    assert p.updater.performed == [("bbb222", str(ME))]
    assert any(str(ME) in r.getMessage() and "обновляет" in r.getMessage() for r in caplog.records)


def test_the_core_update_needs_the_signature_too():
    p = acting(); p.updater = Updater()
    assert post(p, "/update/confirm", {"version": "bbb222"}, headers={})[0] == 403
    assert p.updater.performed == []


# ── контроль на карточке подписи (DEV-104) ────────────────────────────────

def test_the_card_tells_a_control_gate_apart_from_a_failure(tmp_path):
    """Критерий приёмки DEV-104: человек видит контроль отдельно. Проба,
    упавшая ПО ЗАМЫСЛУ, не должна читаться как поломка, а разобранная
    находка — как непрочитанный журнал."""
    from agent_factory.test_evidence import CONTROL, EXPECTED, VERDICT, verify_dir
    verify_dir(tmp_path, stats={"исход": "VERIFIED"}, gates=CONTROL,
               expected=EXPECTED, verdict=VERDICT)
    p = page([issue()])
    p.work_root = str(tmp_path)
    _, (_, body) = get(p)
    assert "контроль: ожидался код 3" in body
    assert "находка разобрана сессией — в исход не идёт" in body
    # И суд по такому журналу зелёный: красный код контроля его не понижает.
    assert "прошли все ворота (3)" in body


# ── одна задача — одна карточка (DEV-100) ────────────────────────────────

def entrance(root, name, *, kind="работа", cost=1.0, outcome="READY_FOR_HUMAN",
             secs=60, mem=100):
    """Заход на диске в том виде, в каком его читает `sessions.walk`."""
    import json as j
    import os
    d = os.path.join(str(root), name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "transcript.jsonl"), "w") as f:
        f.write(j.dumps({"type": "result", "total_cost_usd": cost, "num_turns": 3,
                         "duration_ms": secs * 1000, "usage": {}}) + "\n")
    with open(os.path.join(d, "stats.json"), "w") as f:
        j.dump({"род": kind, "слот": 1, "пик_мб": mem, "исход": outcome}, f,
               ensure_ascii=False)
    return d


def five_entrances(tmp_path):
    """Одна задача, пять заходов: работа, проверка, доработка, проверка, работа."""
    for i, kind in enumerate(("работа", "verify", "работа", "verify", "работа")):
        entrance(tmp_path, f"DEV-70{'-verify' if kind == 'verify' else ''}"
                           f"-20260915-1{i}0000", kind=kind, cost=1.0)
    p = page([issue()])
    p.puller, p.work_root = FakePuller(), str(tmp_path)
    return p


def runs_page(p):
    return asyncio.run(p.handle("GET", "/queue/runs?t=" + TOKEN, {}, b""))[1][1]


def test_five_entrances_of_one_task_are_one_card(tmp_path):
    """Было: сколько заходов, столько карточек, и задачу человек собирал
    глазами. Стало: одна карточка, внутри пять значков."""
    body = runs_page(five_entrances(tmp_path))
    assert body.count('<div class="run">') == 1
    assert body.count("<li>") == 5
    assert body.count("DEV-70</b>") == 1


def test_the_card_costs_the_whole_task_not_one_entrance(tmp_path):
    """Цена задачи — то единственное число, которое сравнимо с ценой
    человеко-часа. Пять заходов по доллару — это $5.00, а не $1.00."""
    body = runs_page(five_entrances(tmp_path))
    assert "$5.00" in body
    assert '<span>$1.00</span>' not in body


def test_the_freshest_entrance_is_on_top_of_the_card(tmp_path):
    """Человек смотрит на итог, а не на историю: последнее, что случилось с
    задачей, не должно требовать прокрутки."""
    p = five_entrances(tmp_path)
    entrance(tmp_path, "DEV-70-20260915-190000", kind="incident", cost=1.0)
    body = runs_page(p)
    stages = body[body.index('<ol class="stages">'):body.index("</ol>")]
    assert stages.index("дежурный") < stages.index("доработка")
    assert stages.index("доработка") < stages.index(">работа<")


def test_each_entrance_says_what_it_was_and_how_it_ended(tmp_path):
    body = runs_page(five_entrances(tmp_path))
    assert "проверка" in body and "доработка" in body
    assert "сдано, ждёт человека" in body
    assert "🔬" in body and "♻️" in body


def test_the_extra_points_are_in_the_same_card_not_beside_it(tmp_path):
    """Вторая плитка на ту же задачу — это ровно тот длинный список, из-за
    которого задачу приходилось собирать глазами."""
    body = runs_page(five_entrances(tmp_path))
    assert "переделка: рабочих заходов 3" in body
    assert body.count('<div class="run">') == 1


def test_the_evidence_on_the_card_is_from_the_last_completed_check(tmp_path):
    """Свидетельства DEV-94 остаются, но от ПОСЛЕДНЕЙ состоявшейся проверки:
    иначе карточка показывает ворота первой проверки рядом с ценой задачи,
    в которой была вторая."""
    from .test_evidence import verify_dir
    verify_dir(tmp_path, ident="DEV-71", stamp="20260915-100000",
               stats={"исход": "FAILED"},
               gates=[{"ворота": "тесты", "итог": "не прошло", "команда": "pytest",
                       "код": 1, "заметка": "", "источник": "сессия", "когда": 1.0}],
               expected="Источник критерия: задача\n\n- **место** — страница\n",
               verdict="FAIL: не сошлось\n- страница — разошлось\n")
    verify_dir(tmp_path, ident="DEV-71", stamp="20260915-200000",
               stats={"исход": "VERIFIED"},
               gates=[{"ворота": "тесты", "итог": "прошло", "команда": "pytest",
                       "код": 0, "заметка": "", "источник": "сессия", "когда": 9.0}],
               expected="Источник критерия: задача\n\n- **место** — страница\n",
               verdict="OK: всё сошлось\n- страница — сошлось\n")
    p = page([issue()])
    p.puller, p.work_root = FakePuller(), str(tmp_path)
    body = runs_page(p)
    line = body[body.index('<div class="run-ev">'):]
    line = line[:line.index("</div>")]
    assert "DEV-71-verify-20260915-200000" in line
    assert "DEV-71-verify-20260915-100000" not in line
    assert "ворота: прошло" in line and "сверка 1/1" in line
    assert "не прошло" not in line


def test_a_task_without_a_check_says_so_instead_of_staying_silent(tmp_path):
    """Пропуск подписывающий читает как «всё в порядке» — как раз там, где
    ничего не проверяли."""
    entrance(tmp_path, "DEV-72-20260915-100000")
    p = page([issue()])
    p.puller, p.work_root = FakePuller(), str(tmp_path)
    body = runs_page(p)
    assert "свидетельств нет" in body
    assert "состоявшейся проверки по задаче не было" in body


def test_the_mark_of_a_task_can_be_taken_back(tmp_path):
    """Оценка задачи кладётся на тот заход, который её уже держит: иначе
    повторное нажатие положит пустоту новому заходу, а старая оценка
    останется лежать и читаться дальше."""
    from agent_factory.verdicts import Verdicts

    class St:
        def __init__(self): self.kv = {}
        async def get(self, k): return self.kv.get(k)
        async def put(self, k, v, ttl): self.kv[k] = v

    p = five_entrances(tmp_path)
    p.verdicts = Verdicts(St())
    asyncio.run(p.verdicts.put("DEV-70-20260915-100000", "ок"))
    body = runs_page(p)
    assert 'name="session" value="DEV-70-20260915-100000"' in body
    assert 'value="" class="mk mk-on"' in body       # то же нажатие снимает


def test_the_runs_page_survives_a_task_without_a_work_root():
    from agent_factory.signing import runs_html
    body = runs_html("Ф", [], {}, token=TOKEN)
    assert "За сутки сессий не было." in body


def test_an_intake_batch_is_not_accused_of_having_no_evidence(tmp_path):
    """У приёмной пачки задачи в трекере ещё нет, и проверки выката у неё не
    бывает по построению. «Свидетельств нет» на ней — шум, от которого та же
    строка на настоящей задаче теряет вес."""
    entrance(tmp_path, "INTAKE-abc123-20260915-100000", kind="приём")
    p = page([issue()])
    p.puller, p.work_root = FakePuller(), str(tmp_path)
    body = runs_page(p)
    assert "приёмная пачка" in body
    assert "свидетельств нет" not in body
