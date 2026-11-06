"""Три слоя памяти: владение, переопределение, потолок индекса (DEV-43)."""
from pathlib import Path

import pytest

import memory


def write(path: Path, name: str, desc: str = "", body: str = "тело", extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\n"
    if desc:
        front += f"description: {desc}\n"
    front += extra + "---\n"
    path.write_text(front + body + "\n", encoding="utf-8")
    return path


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "base"
    write(root / "review.md", "ревью перед мержем", "дифф смотрят до мержа")
    write(root / "tests.md", "тесты в CI", "зелёный CI — условие мержа")
    return root


# ── переопределение вместо правки ───────────────────────────────────────────

def test_client_file_replaces_the_base_one_whole(base, tmp_path):
    """Клиент кладёт файл с тем же именем — читается ЕГО версия, а базовая не
    склеивается с ней ни по разделам, ни построчно. Иначе переопределение
    пережило бы обновление продукта только до первого переименования заголовка
    в базе, и молча."""
    over = tmp_path / "over"
    write(over / "review.md", "ревью у нас", "двое глазами, мержит тимлид")
    entries = {e.name: e for e in memory.methodology_entries(base, over)}
    assert set(entries) == {"ревью у нас", "тесты в CI"}
    assert entries["ревью у нас"].path == over / "review.md"
    assert entries["тесты в CI"].path == base / "tests.md"


def test_an_overridden_entry_is_marked_for_the_session(base, tmp_path):
    """Сессия обязана видеть, что читает версию клиента, а не продукта: без
    метки расхождение с базой выглядит как ошибка продукта."""
    over = tmp_path / "over"
    write(over / "review.md", "ревью у нас", "мержит тимлид")
    entries = {e.name: e for e in memory.methodology_entries(base, over)}
    assert entries["ревью у нас"].overridden
    assert "переопределено клиентом" in entries["ревью у нас"].line()
    assert not entries["тесты в CI"].overridden
    assert "переопределено" not in entries["тесты в CI"].line()


def test_the_base_file_is_never_edited_by_an_override(base, tmp_path):
    """Переопределение не касается базы физически: обновление продукта
    перезаписывает её целиком, и правка исчезла бы молча."""
    before = (base / "review.md").read_text(encoding="utf-8")
    over = tmp_path / "over"
    write(over / "review.md", "ревью у нас")
    memory.methodology_entries(base, over)
    assert (base / "review.md").read_text(encoding="utf-8") == before


def test_drop_removes_a_base_entry_and_itself(base, tmp_path):
    """Выключение пункта — тоже переопределение, а не удаление базового файла:
    удалять его нельзя, релиз вернёт его назад."""
    over = tmp_path / "over"
    write(over / "tests.md", "не про нас", extra="drop: true\n")
    names = [e.name for e in memory.methodology_entries(base, over)]
    assert names == ["ревью перед мержем"]


def test_an_empty_override_does_not_switch_a_rule_off(base, tmp_path):
    """Пустой файл — оборванная запись, а не «правило не применяется». Если бы
    значил, недописанное переопределение выключало бы методологию молча."""
    over = tmp_path / "over"
    (over).mkdir()
    (over / "tests.md").write_text("", encoding="utf-8")
    names = [e.name for e in memory.methodology_entries(base, over)]
    assert len(names) == 2
    assert "tests" in names             # имя по файлу: frontmatter пустой


def test_a_new_name_is_an_addition_not_an_override(base, tmp_path):
    over = tmp_path / "over"
    write(over / "oncall.md", "дежурство", "у нас своё")
    entries = {e.name: e for e in memory.methodology_entries(base, over)}
    assert set(entries) == {"ревью перед мержем", "тесты в CI", "дежурство"}
    assert not entries["дежурство"].overridden


def test_override_matches_by_file_name_not_by_frontmatter(base, tmp_path):
    """Совпадение по имени ФАЙЛА: имя видно, не читая файл, и переопределяющий
    не сочинит его случайно, повторив `name` в новой записи."""
    over = tmp_path / "over"
    write(over / "svoe.md", "ревью перед мержем", "то же имя, другой файл")
    paths = {e.path.name for e in memory.methodology_entries(base, over)}
    assert paths == {"review.md", "tests.md", "svoe.md"}


def test_unclosed_frontmatter_does_not_leak_body_into_the_index(tmp_path):
    """Без закрывающего --- frontmatter не читается вовсе: строка «name:» из
    прозы иначе подменила бы имя записи."""
    p = tmp_path / "z.md"
    p.write_text("---\nname: nastoyashchee\n\nтело, где написано name: poddelka\n",
                 encoding="utf-8")
    assert memory.entry_of(p).name == "z"


def test_readme_of_the_override_directory_is_not_an_entry(tmp_path):
    """Пояснительную записку установщика сессия видеть не должна — иначе в
    индексе методологии появляется запись «как переопределять методологию»."""
    over = tmp_path / "over"
    write(over / "README.md", "как переопределять")
    write(over / "svoe.md", "своё правило")
    names = [e.name for e in memory.layer_entries(over)]
    assert names == ["своё правило"]


def test_an_unreadable_layer_does_not_break_the_session(tmp_path):
    """Слой — не условие работы: нет каталога, нет прав — сессия идёт дальше."""
    assert memory.layer_entries(None) == []
    assert memory.layer_entries(tmp_path / "net-takogo") == []


# ── потолок индекса ────────────────────────────────────────────────────────

def big_layer(root: Path, n: int) -> Path:
    for i in range(n):
        write(root / f"z{i:03}.md", f"запись {i}", "описание " + "д" * 80)
    return root


def test_index_lines_stay_within_the_budget(tmp_path):
    """Потолок считает строки индекса всех слоёв вместе: иначе три индекса
    упираются в потолок чтения быстрее одного (DEV-29), и это незаметно."""
    base = big_layer(tmp_path / "base", 40)
    person = big_layer(tmp_path / "person", 40)
    budget = 8000
    block = memory.layers_block(methodology_base=base, person_root=person,
                                budget=budget)
    lines = [l for l in block.splitlines()
             if l.startswith("- ") and not l.startswith("- …")]
    assert lines                                   # что-то влезло
    assert sum(len(l.encode()) + 1 for l in lines) <= budget


def test_the_cut_tail_is_named_not_dropped_silently(tmp_path):
    """Обрезанный хвост виден: сколько записей не влезло и где их искать.
    Молча урезанный индекс — это слой, о существовании которого сессия не
    узнает."""
    base = big_layer(tmp_path / "base", 40)
    block = memory.layers_block(methodology_base=base, budget=1500)
    tail = [l for l in block.splitlines() if l.startswith("- …")]
    shown = [l for l in block.splitlines()
             if l.startswith("- ") and not l.startswith("- …")]
    assert len(tail) == 1
    assert str(base) in tail[0]
    assert f"ещё {40 - len(shown)} записей" in tail[0]


def test_a_fat_project_index_shrinks_the_lists_but_not_to_nothing(tmp_path):
    """Проектный слой пишет сама сессия, урезать его записи мы не вправе — он
    берёт из потолка столько, сколько занимает. Но методология под нож не идёт:
    у неё своя броня."""
    base = big_layer(tmp_path / "base", 40)
    role = tmp_path / "agent-work" / "MEMORY.md"
    role.parent.mkdir()
    role.write_text("x" * 1800, encoding="utf-8")
    block = memory.layers_block(role_index=role, methodology_base=base,
                                budget=2000)
    entries = [l for l in block.splitlines()
               if l.startswith("- ") and not l.startswith("- …")]
    assert entries, "методология исчезла из-за размера чужого слоя"
    assert [l for l in block.splitlines() if l.startswith("- …")]   # хвост назван


def test_methodology_survives_an_index_that_ate_the_whole_ceiling(tmp_path):
    """Потолок исчерпан проектным индексом — методология всё равно печатается.

    До 14.09.2026 было наоборот: списки не печатались вовсе, и роль проверки на
    client-b четверо суток не видела `definition-of-done`, потому что её
    проектный индекс дорос до 16 КБ. Правило продукта не может зависеть от того,
    сколько заметок успела написать сессия."""
    base = big_layer(tmp_path / "base", 5)
    role = tmp_path / "agent-work" / "MEMORY.md"
    role.parent.mkdir()
    role.write_text("x" * 3000, encoding="utf-8")
    block = memory.layers_block(role_index=role, methodology_base=base,
                                budget=2000)
    assert "Сожми индекс" in block
    assert [l for l in block.splitlines() if l.startswith("- ")], \
        "методология не напечатана при переполненном проектном индексе"


def test_a_growing_project_index_is_warned_about_before_it_crowds_others(tmp_path):
    """Предупреждение приходит ДО того, как индекс вытеснил методологию:
    сообщение об исчерпанном потолке в момент исчерпания — это сообщение
    о том, что сессия уже работала без методологии."""
    base = big_layer(tmp_path / "base", 3)
    role = tmp_path / "agent-work" / "MEMORY.md"
    role.parent.mkdir()
    role.write_text("x" * 1200, encoding="utf-8")
    block = memory.layers_block(role_index=role, methodology_base=base,
                                budget=2000)
    assert "сожми его" in block
    assert [l for l in block.splitlines() if l.startswith("- ")]   # ещё влезли


def test_a_missing_project_index_is_not_an_error(tmp_path):
    """Первая сессия роли: индекса нет. Слой при этом обязан считаться пустым,
    а не сорвать сборку блока."""
    block = memory.layers_block(role_index=tmp_path / "net" / "MEMORY.md",
                                methodology_base=big_layer(tmp_path / "b", 2))
    assert "запись 0" in block


# ── владение: что сессия узнаёт из блока ───────────────────────────────────

def test_the_block_says_only_the_project_layer_is_writable(base):
    """Без этого методология расползается копиями по проектной памяти каждой
    инсталляции, и обновление продукта перестаёт что-либо значить."""
    block = memory.layers_block(methodology_base=base)
    assert "пишешь ты только в него" in block
    assert "чужой слой не переписывают" in block.lower()


def test_an_absent_person_layer_is_stated_in_words(base):
    """«Слоя нет» — тоже знание: иначе сессия ищет то, чего не заводили."""
    block = memory.layers_block(methodology_base=base, person_root=None)
    assert "слоя у этой инсталляции нет" in block


def test_the_person_layer_is_named_after_its_owner(base, tmp_path):
    person = tmp_path / "person"
    write(person / "otchet.md", "отчёт с цифрами", "ждёт числа, а не «стало лучше»")
    block = memory.layers_block(methodology_base=base, person_root=person,
                                person_owner="Андрей")
    assert "**О человеке (Андрей)" in block
    assert "отчёт с цифрами" in block


def test_entry_lines_carry_an_absolute_path(base):
    """Слой лежит вне каталога памяти сессии: по имени файла она его не
    откроет, а индекс без пути — список того, что прочитать нельзя."""
    for e in memory.methodology_entries(base, None):
        assert str(e.path) in e.line()
        assert e.path.is_absolute()


def test_the_shipped_methodology_covers_the_owners_list():
    """База методологии — часть продукта, а не пример: инсталляция без своих
    переопределений обязана получить содержательный слой. Список владельца
    (DEV-43): ревью перед мержем, тесты в CI, настройка CI, ветки, определение
    готовности, зоны ответственности ролей."""
    entries = memory.methodology_entries()
    names = " ".join(e.name for e in entries)
    for word in ("ревью", "тесты в CI", "настройка CI", "ветки",
                 "готовности", "зоны ответственности"):
        assert word in names, word
    for e in entries:
        assert e.description, e.path      # без описания строка индекса бесполезна
        assert len(e.path.read_text(encoding="utf-8")) > 500, e.path


# ── контракт сессии ────────────────────────────────────────────────────────

def test_every_role_prompt_carries_the_layers(base):
    """Механизм без инструкции бесполезен — как и сама память до 06.09."""
    import json

    from prompt import (build_incident_prompt, build_intake_prompt, build_prompt,
                        build_verify_prompt)
    from test_intake import TASK as INTAKE_TASK
    task = {"identifier": "X-1", "title": "t", "url": "u"}
    layers = memory.layers_block(methodology_base=base)
    # У проверки два режима — до мержа (ветка на стенде) и после (выкат): они
    # собирают разные начало, факты и исходы. Берём ОБА: одного мало, потому
    # что режим, оставшийся без блока, молчал бы — промпт собрался, слоёв нет.
    prompts = [
        build_prompt(task, Path("/w"), Path("/r"), "Рев", memory_layers=layers),
        build_verify_prompt(task, Path("/w"), Path("/r"), "Рев", memory_layers=layers),
        build_verify_prompt(dict(task, trigger="branch"), Path("/w"), Path("/r"),
                            "Рев", memory_layers=layers),
        build_intake_prompt(json.loads(json.dumps(INTAKE_TASK)), Path("/w"),
                            Path("/r"), memory_layers=layers),
        build_incident_prompt(task, "Рев", memory_layers=layers),
    ]
    for text in prompts:
        assert "## Память роли" in text          # прежний слой на месте
        assert "Ещё два слоя" in text
        assert "ревью перед мержем" in text


def test_without_layers_the_contract_is_what_it_was():
    """Инсталляция старого ядра не должна получить пустых заголовков вместо
    слоёв: пусто — прежний блок, буква в букву."""
    from prompt import MEMORY_BLOCK, memory_block
    assert memory_block("") == MEMORY_BLOCK
    assert memory_block("   \n ") == MEMORY_BLOCK


def test_the_runner_keeps_methodology_even_without_a_store(monkeypatch):
    """Методология приезжает с продуктом, а не из стора инсталляции: стор не
    задан (механизм памяти выключен) — слой методологии всё равно есть."""
    import runner
    monkeypatch.setattr(runner, "AGENT_MEMORY_STORE", "")
    monkeypatch.setattr(runner, "AGENT_PERSON_MEMORY", "")
    block = runner.memory_layers("work")
    assert "ревью перед мержем" in block


def test_the_runner_reads_the_override_and_index_of_the_role(tmp_path, monkeypatch):
    """Переопределения клиента лежат в сторе инсталляции, индекс — в каталоге
    РОЛИ: перепутанная роль дала бы сессии чужой потолок и чужие записи.

    Переопределяется страница без адреса (зоны ответственности): адресная
    (`roles: [work]`) дежурному не показывается, и это проверяется отдельно."""
    import runner
    store = tmp_path / "store"
    write(store / "methodology" / "role-boundaries.md", "зоны у нас",
          "дежурный ещё и катит")
    (store / "agent-incident").mkdir(parents=True)
    (store / "agent-incident" / "MEMORY.md").write_text("- [a](a.md) — раз\n",
                                                        encoding="utf-8")
    monkeypatch.setattr(runner, "AGENT_MEMORY_STORE", str(store))
    monkeypatch.setattr(runner, "AGENT_PERSON_MEMORY", "")
    block = runner.memory_layers("incident")
    assert "зоны у нас" in block
    assert "переопределено клиентом" in block
    assert "зоны ответственности ролей" not in block   # базовая версия вытеснена


def test_a_broken_layer_does_not_stop_the_session(monkeypatch):
    """Память — не обязательное условие работы: блок не собрался, сессия идёт."""
    import runner

    def explode(**kwargs):
        raise OSError("диск")

    monkeypatch.setattr(memory, "layers_block", explode)
    assert runner.memory_layers("work") == ""


def test_a_typo_in_the_ceiling_does_not_stop_the_runner(monkeypatch):
    """Потолок правят руками в файле окружения. `int("12к")` на импорте уронил
    бы раннер целиком — то есть память, которая «не обязательное условие
    работы», остановила бы все сессии. Умолчание, и вслух в журнал."""
    import runner
    for bad in ("12к", "", "0", "-1", "  "):
        monkeypatch.setenv("AGENT_MEMORY_INDEX_MAX", bad)
        assert runner._index_budget() == memory.DEFAULT_INDEX_MAX
    monkeypatch.setenv("AGENT_MEMORY_INDEX_MAX", "  4000 ")
    assert runner._index_budget() == 4000


def test_a_broken_ceiling_is_reported_to_the_journal(monkeypatch, caplog):
    import runner
    monkeypatch.setenv("AGENT_MEMORY_INDEX_MAX", "12к")
    with caplog.at_level("WARNING"):
        runner._index_budget()
    assert "AGENT_MEMORY_INDEX_MAX" in caplog.text


def test_the_cut_tail_names_both_methodology_directories(tmp_path):
    """Урезанный хвост методологии мог остаться и в базе, и в переопределениях:
    один названный каталог отправил бы сессию искать запись не туда."""
    base = big_layer(tmp_path / "base", 40)
    over = tmp_path / "over"
    write(over / "svoe.md", "своё правило", "описание " + "д" * 80)
    block = memory.layers_block(methodology_base=base, methodology_override=over,
                                budget=1200)
    tail = [l for l in block.splitlines() if l.startswith("- …")][0]
    assert str(base) in tail
    assert str(over) in tail


def test_the_verify_role_sees_the_definition_of_done(tmp_path):
    """Случай, который довёл до находки: роль проверки сверяет работу с
    определением готовности, а её проектный индекс раздулся до 16 КБ — и
    определения она не видела с 12.09.2026, четверо суток проверяя по тому,
    что придумала сама."""
    base = tmp_path / "methodology"
    base.mkdir()
    (base / "definition-of-done.md").write_text(
        "---\nname: определение готовности\n"
        "description: Сделано = причина установлена, изменение проверено\n---\n",
        encoding="utf-8")
    role = tmp_path / "agent-verify" / "MEMORY.md"
    role.parent.mkdir()
    role.write_text("x" * 16290, encoding="utf-8")     # как было на живом сервере

    block = memory.layers_block(role_index=role, methodology_base=base,
                                budget=12000)          # как на живом сервере
    assert "определение готовности" in block


# ── вид записи: жёсткая полоса ограничений (DEV-85) ─────────────────────────

def test_a_constraint_outranks_the_alphabet(tmp_path):
    """Порядок обрезки был алфавитным, и правило выживало по первой букве.

    `definition-of-done.md` уцелел на client-b потому, что «d» третья из шести,
    а не потому, что без определения готовности роль проверки не работает.
    Типо-слепое сжатие сохраняет 53% ограничений после одного прохода — лечится
    признаком, по которому ограничение можно не резать."""
    root = tmp_path / "base"
    for n in "abcdefgh":
        write(root / f"{n}.md", f"справка {n}", "x" * 200)
    write(root / "zzz.md", "правило", "нельзя выкатывать в пятницу",
          extra="kind: ограничение\n")
    block = memory.layers_block(methodology_base=root, budget=1200)
    lines = [l for l in block.splitlines() if l.startswith("- ") and not l.startswith("- …")]
    assert "правило" in lines[0], "ограничение не встало первым"


def test_a_constraint_is_not_cut_by_a_ceiling_that_the_rest_ate(tmp_path):
    """Ограничение бесполезно в урезанном виде: «не делай X, кроме» без
    продолжения хуже, чем ничего. Полоса забирает своё раньше остальных."""
    root = tmp_path / "base"
    for n in range(40):
        write(root / f"a{n:02d}.md", f"справка {n}", "x" * 300)
    write(root / "zzz.md", "правило", "нельзя выкатывать в пятницу",
          extra="kind: ограничение\n")
    block = memory.layers_block(methodology_base=root, budget=900)
    assert "правило" in block
    assert [l for l in block.splitlines() if l.startswith("- …")]


def test_constraints_overflowing_their_own_band_are_named_apart(tmp_path):
    """Полоса ограничена, а не бесконечна: инсталляция, пометившая ограничением
    сто записей, не должна тихо съесть весь контекст. Переполнение полосы — не
    обычная обрезка, а промах раскладки, и сказано о нём отдельными словами."""
    root = tmp_path / "base"
    for n in range(60):
        write(root / f"c{n:02d}.md", f"правило {n}", "x" * 200,
              extra="kind: ограничение\n")
    block = memory.layers_block(methodology_base=root, budget=12000)
    tail = [l for l in block.splitlines() if "ОГРАНИЧЕНИЙ не поместились" in l]
    assert tail, "переполнение полосы ограничений прошло молча"


# ── drop действует во всех слоях (DEV-85) ───────────────────────────────────

def test_a_switched_off_note_leaves_the_person_layer_too(tmp_path):
    """`drop: true` читался тремя разборщиками и действовал в одном.

    Пометка, работающая в одном месте из трёх, хуже отсутствующей: человек
    считает запись убранной, а сессия продолжает её читать."""
    person = tmp_path / "person"
    write(person / "zhivaya.md", "живая", "одно")
    write(person / "ubrannaya.md", "убранная", "два", extra="drop: true\n")
    names = {e.name for e in memory.layer_entries(person)}
    assert names == {"живая"}


def test_the_client_can_still_drop_a_base_page(tmp_path, base):
    """Тот же `drop: true` в каталоге переопределений значит другое — «убрать
    базовую страницу», — и это обязано продолжать работать."""
    over = tmp_path / "over"
    write(over / "review.md", "неважно", extra="drop: true\n")
    names = {e.name for e in memory.methodology_entries(base, over)}
    assert names == {"тесты в CI"}


# ── дисциплина: методология принимает команды, которые не пишут код ─────────

def test_a_team_that_writes_no_code_sees_only_what_applies_to_it(tmp_path):
    """Схема обязана принимать команду маркетинга, а не переделываться под неё.

    Базовая методология состояла из шести страниц про ветки, CI и ревью —
    маркетинговому агенту в ней не было ничего. Дисциплина стала полем записи:
    инсталляция называет свою, и видит её страницы плюс всё, что помечено
    `track: общее`."""
    root = tmp_path / "base"
    write(root / "branching.md", "ветки и мерж", "MR в основную",
          extra="track: разработка\n")
    write(root / "handoff.md", "передача работы", "отчёт в задачу",
          extra="track: общее\n")
    write(root / "posting.md", "график публикаций", "сначала план, потом пост",
          extra="track: маркетинг\n")

    dev = {e.name for e in memory.methodology_entries(root, tracks={"разработка"})}
    assert dev == {"ветки и мерж", "передача работы"}

    mkt = {e.name for e in memory.methodology_entries(root, tracks={"маркетинг"})}
    assert mkt == {"график публикаций", "передача работы"}


def test_without_a_declared_track_everything_is_shown(tmp_path):
    """Пока владелец не назвал дисциплину, показать лишнюю страницу дешевле,
    чем спрятать нужную: промах видно глазами, а не по отсутствию."""
    root = tmp_path / "base"
    write(root / "branching.md", "ветки и мерж", "MR", extra="track: разработка\n")
    write(root / "posting.md", "публикации", "план", extra="track: маркетинг\n")
    assert len(memory.methodology_entries(root, tracks=None)) == 2


def test_a_page_with_no_track_applies_to_every_team(tmp_path):
    """Страница без пометки — правило, не зависящее от дисциплины. Сегодняшняя
    база вся такая, и молча спрятать её от новой инсталляции нельзя."""
    root = tmp_path / "base"
    write(root / "old.md", "старое правило", "без пометки")
    assert len(memory.methodology_entries(root, tracks={"контент"})) == 1


def test_the_overflow_line_names_the_band_it_actually_had(tmp_path):
    """Сообщение об урезанном ограничении называет фактическую полосу.

    Раздел мог достаться урезанным слоями выше, и тогда полоса меньше
    `CONSTRAINT_BAND`, а то и ноль. «Не влезло в 3000 Б», когда места было 0,
    посылает читателя искать несуществующую причину — та же болезнь, ради
    которой переписана и прошлая формулировка про слой о человеке."""
    root = tmp_path / "base"
    for n in range(10):
        write(root / f"c{n}.md", f"правило {n}", "x" * 120, extra="kind: ограничение\n")
    lines, left, left_limits, band = memory._fit(memory.layer_entries(root), 200)
    assert left_limits and band == 200, "полоса не может быть больше самого раздела"


def test_the_budget_does_not_bound_the_project_index_itself(tmp_path):
    """Потолок ограничивает только то, что печатаем МЫ, — а не весь контекст.

    Ловушка, стоящая объяснения: проектный индекс в этот блок не попадает
    никогда. Его грузит сам claude из каталога памяти, на который раннер
    поставил симлинк, и `AGENT_MEMORY_INDEX_MAX` ему не указ — он участвует
    только в арифметике «сколько осталось методологии». Значит уменьшение
    потолка НЕ уменьшает контекст сессии: оно уменьшает долю методологии в нём.

    Единственный рычаг на размер проектного индекса — из чего он собран:
    число записей и `runner.INDEX_DESC_MAX`."""
    role = tmp_path / "agent-work" / "MEMORY.md"
    role.parent.mkdir()
    role.write_text("- [заметка](z.md) — строка проектного индекса\n" * 50,
                    encoding="utf-8")
    base = tmp_path / "methodology"
    write(base / "dod.md", "определение готовности", "сделано — это состояние")

    tight = memory.layers_block(role_index=role, methodology_base=base, budget=1000)
    loose = memory.layers_block(role_index=role, methodology_base=base, budget=99000)

    assert "строка проектного индекса" not in tight
    assert "строка проектного индекса" not in loose
    assert len(tight) < len(loose) or "определение готовности" in tight


# ── адресация по ролям (DEV-92) ─────────────────────────────────────────────

ROLES = ("work", "verify", "intake", "incident")


def test_a_page_addressed_to_roles_is_shown_only_to_them(tmp_path):
    """Методология была одна на все роли: `kind` выбирал путь к индексу, а база
    и переопределения шли всем одинаково. Дежурному по инциденту ехали правила
    про ветки и MR, приёмщику — про тесты в CI, и место в потолке чтения они
    занимали такое же, как нужные."""
    root = tmp_path / "base"
    write(root / "branching.md", "ветки и мерж", "MR", extra="roles: [work]\n")
    write(root / "dod.md", "готовность", "сделано =", extra="roles: [work, verify]\n")
    write(root / "handoff.md", "передача", "отчёт в задачу")          # без адреса
    by_role = {r: {e.name for e in memory.methodology_entries(root, role=r)}
               for r in ROLES}
    assert by_role["work"] == {"ветки и мерж", "готовность", "передача"}
    assert by_role["verify"] == {"готовность", "передача"}
    assert by_role["intake"] == {"передача"}
    assert by_role["incident"] == {"передача"}


def test_roles_are_read_in_every_form_the_field_is_written(tmp_path):
    """Мигратор пишет `roles: [work]`; человек напишет и без скобок, и блочным
    списком YAML. Форма, которую разбор не понял, молча адресовала бы страницу
    всем — то есть вернула бы прежнее поведение без единой ошибки."""
    root = tmp_path / "base"
    write(root / "a.md", "скобки", extra="roles: [Work, verify]\n")
    write(root / "b.md", "без скобок", extra="roles: work, verify\n")
    write(root / "c.md", "блок", extra="roles:\n  - work\n  - verify\nkind: факт\n")
    for f in "abc":
        e = memory.entry_of(root / f"{f}.md")
        assert e.roles == ("work", "verify"), f
    assert memory.entry_of(root / "c.md").kind == "факт"   # ключ после блока не потерян


def test_without_a_role_everything_is_shown(tmp_path):
    """Сборка без роли — прежнее поведение: как у дисциплин, спрятать страницу
    от того, кто роль не назвал, хуже, чем показать лишнюю."""
    root = tmp_path / "base"
    write(root / "a.md", "адресная", extra="roles: [verify]\n")
    write(root / "b.md", "общая")
    assert len(memory.methodology_entries(root)) == 2
    assert len(memory.methodology_entries(root, role="")) == 2


def test_intake_and_work_get_different_sets_from_the_shipped_base():
    """Критерий приёмки: блок для `intake` и для `work` — наборы различаются.
    Одинаковые — поле `roles` не читается."""
    sets = {r: {e.path.name for e in memory.methodology_entries(role=r)} for r in ROLES}
    assert sets["intake"] != sets["work"]
    assert sets["intake"] < sets["work"]            # приёмщику — подмножество
    assert sets["verify"] < sets["work"]
    assert "definition-of-done.md" in sets["verify"]  # то, ради чего заведена броня
    assert "definition-of-done.md" not in sets["intake"]


def test_the_incident_role_gets_no_branch_or_review_pages():
    """Критерий приёмки: в блоке роли `incident` страниц про ветки и ревью нет.
    Есть — адресация не работает."""
    block = memory.layers_block(role="incident")
    for page in ("branching.md", "review-before-merge.md", "tests-in-ci.md",
                 "ci-setup.md"):
        assert page not in block, page
    for word in ("ветки и мерж", "ревью перед мержем", "тесты в CI"):
        assert word not in block, word
    # Общее при этом на месте: дежурный не работает вслепую.
    for page in ("role-boundaries.md", "evidence-before-claim.md", "handoff.md",
                 "memory-expansion.md"):
        assert page in block, page


def test_the_block_says_its_list_is_addressed(tmp_path):
    """Сессия обязана знать, что список неполный по замыслу: иначе страницу,
    которую называет контракт другой роли, она пойдёт искать как пропавшую."""
    root = tmp_path / "base"
    write(root / "a.md", "общая")
    assert "адресованные твоей роли (incident)" in memory.layers_block(
        methodology_base=root, role="incident")
    assert "адресованные твоей роли" not in memory.layers_block(methodology_base=root)


def test_the_runner_passes_the_role_into_the_block(tmp_path, monkeypatch):
    """`runner.memory_layers(kind)` использовал `kind` только для пути к индексу
    (`core/runner.py`, DEV-92). Теперь роль идёт и в сборку блока."""
    import runner
    store = tmp_path / "store"
    for r in ROLES:
        (store / f"agent-{r}").mkdir(parents=True)
    monkeypatch.setattr(runner, "AGENT_MEMORY_STORE", str(store))
    monkeypatch.setattr(runner, "AGENT_PERSON_MEMORY", "")
    incident, work = runner.memory_layers("incident"), runner.memory_layers("work")
    assert incident != work
    assert "branching.md" in work
    assert "branching.md" not in incident
    assert "role-boundaries.md" in incident


def test_an_override_without_an_address_keeps_the_base_address(base, tmp_path):
    """Переопределение вытесняет базовую страницу целиком — кроме адреса. Файл
    клиента, написанный до появления адресации, иначе молча вернул бы правила
    про ветки дежурному: ровно то, от чего адресация заведена."""
    write(base / "review.md", "ревью перед мержем", "дифф до мержа",
          extra="roles: [work]\n")
    over = tmp_path / "over"
    write(over / "review.md", "ревью у нас", "мержит тимлид")
    names = {e.name for e in memory.methodology_entries(base, over, role="incident")}
    assert "ревью у нас" not in names
    names = {e.name for e in memory.methodology_entries(base, over, role="work")}
    assert "ревью у нас" in names


def test_an_override_with_its_own_address_wins(base, tmp_path):
    """Свой адрес клиент пишет явно, и тогда он главнее базового: расширить
    правило на дежурного — его право."""
    write(base / "review.md", "ревью перед мержем", "дифф до мержа",
          extra="roles: [work]\n")
    over = tmp_path / "over"
    write(over / "review.md", "ревью у нас", "и дежурный тоже",
          extra="roles: [work, incident]\n")
    names = {e.name for e in memory.methodology_entries(base, over, role="incident")}
    assert "ревью у нас" in names


def test_drop_works_whatever_the_role(base, tmp_path):
    """`drop: true` — инструкция убрать базовую страницу, и роль на неё не
    влияет: выключенное не должно всплыть у роли, которой оно адресовано."""
    write(base / "review.md", "ревью перед мержем", "дифф", extra="roles: [work]\n")
    over = tmp_path / "over"
    write(over / "review.md", "неважно", extra="drop: true\n")
    names = {e.name for e in memory.methodology_entries(base, over, role="work")}
    assert names == {"тесты в CI"}


def test_the_person_layer_is_addressed_too(tmp_path):
    """Конверт один на все слои: запись о человеке с адресом видна только
    названным ролям, без адреса — всем."""
    person = tmp_path / "person"
    write(person / "otchet.md", "отчёт с цифрами", extra="roles: [verify]\n")
    write(person / "ton.md", "без канцелярита")
    names = {e.name for e in memory.layer_entries(person, role="intake")}
    assert names == {"без канцелярита"}
    names = {e.name for e in memory.layer_entries(person, role="verify")}
    assert names == {"отчёт с цифрами", "без канцелярита"}


def test_every_shipped_page_names_only_known_roles():
    """Опечатка в адресе (`roles: [wrok]`) молча спрятала бы страницу от всех:
    ни одной роли она не адресована, а без адреса не считается."""
    for e in memory.methodology_entries():
        for r in e.roles:
            assert r in ROLES, (e.path.name, r)


def test_the_shipped_methodology_has_the_contract_editing_rule():
    """Правило правки контрактов (DEV-92, из инцидента DEV-59): смену предмета
    сессии врезкой не делают; противоречие внутри одного текста выигрывает
    фрагмент, который стоит первым и которого больше."""
    pages = {e.path.name: e for e in memory.methodology_entries()}
    assert "contract-edits.md" in pages
    page = pages["contract-edits.md"]
    text = page.path.read_text(encoding="utf-8")
    for word in ("врезкой не делают", "стоит первым", "которого больше", "DEV-59"):
        assert word in text, word
    assert "work" in page.roles          # правят контракты в рабочей сессии
    assert "incident" not in page.roles


# ── контракт не пересказывает методологию (DEV-92) ─────────────────────────

def _step(text: str, n: int) -> str:
    """Текст шага n «Порядка работы» контракта: от `\\nn. ` до `\\nn+1. `."""
    body = text.split("Порядок работы:", 1)[1]
    start = body.index(f"\n{n}. ")
    end = body.index(f"\n{n + 1}. ", start)
    return body[start:end]


def test_the_work_contract_reads_methodology_at_named_steps():
    """Критерий приёмки: ссылка обязательна, а не «см. также» — названа точка
    чтения. «При необходимости» читать не будут."""
    from prompt import TEMPLATE
    for n, page in ((3, "branching.md"), (7, "tests-in-ci.md"),
                    (9, "review-before-merge.md")):
        step = _step(TEMPLATE, n)
        assert f"`{page}`" in step, (n, page)
        assert "ПРОЧИТАЙ" in step, n
        for soft in ("при необходимости", "см. также", "если нужно", "по желанию"):
            assert soft not in step.lower(), (n, soft)
    # Страница про тесты действует и на шаге 11 — контракт это называет, а
    # не пересказывает правило второй раз.
    assert "тесты в CI" in _step(TEMPLATE, 7)


def test_the_work_contract_no_longer_repeats_the_pages():
    """Дубли: `branching.md` ↔ шаг 3, `review-before-merge.md` ↔ шаг 9,
    `tests-in-ci.md` ↔ шаг 7. Копия в контракте расходится с
    оригиналом при первой правке, и сессия получает две версии правила."""
    from prompt import TEMPLATE
    for copied in ("Ребейз переписал бы уже опубликованные коммиты",
                   "Без неё worktree ответвится от HEAD",
                   "Изоляция, ради которой это могло бы понадобиться",
                   "Находка — гипотеза, а не приговор",
                   "«Отработает в CI» — это предположение",
                   "«отработает в CI-джобе» — это",
                   "Всё выше про «не ждать» относится"):
        assert copied not in TEMPLATE, copied


def test_the_work_contract_stayed_slim():
    """Критерий приёмки: контракт похудел. До DEV-92 `TEMPLATE` весил 34 496 Б
    (354 строки); после — 31 006. Потолок с запасом на правки соседних шагов:
    вернуть дубли молча нельзя.

    Поднят один раз, до 40 000: DEV-56 добавил шлюз тяжёлой задачи (шаг 7) —
    целый шаг, а не копию соседнего, 7,4 КБ вместе с признаками и схемой
    плана. Потолок держит ровно то, ради чего заведён: подросший на новый
    шаг, он по-прежнему не пускает возврат вычищенных дублей (их ловит
    `test_the_work_contract_no_longer_repeats_the_pages`)."""
    from prompt import TEMPLATE
    assert len(TEMPLATE.encode("utf-8")) <= 40000
