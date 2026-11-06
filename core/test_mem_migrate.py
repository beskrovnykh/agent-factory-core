"""Конверт записи памяти: что мигратор вправе трогать и за что отвечает (DEV-85)."""
from datetime import date
from pathlib import Path

import pytest

import mem_migrate

TODAY = date(2026, 9, 14)

NOTE = """---
name: runner-contract-drift
description: расхождение видно одной строкой в журнале
metadata:
  type: reference
---

Установлено 13.09.2026 (DEV-16). Раннер досдаёт за сессию, и это видно.
"""


def write(path: Path, text: str = NOTE) -> Path:
    """Запись на диске. Каталог роли обязателен: роль мигратор берёт из него."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path):
    """Стор из одной записи в роли work."""
    write(tmp_path / "agent-memory-client-b" / "agent-work" / "drift.md")
    return tmp_path / "agent-memory-client-b"


def front_of(path: Path) -> dict[str, str]:
    """Поля верхнего уровня из frontmatter файла, как их прочитает сессия."""
    lines, _ = mem_migrate.split_front(path.read_text(encoding="utf-8"))
    out = {}
    for line in lines or []:
        key, sep, value = line.partition(":")
        if sep and not key.startswith(" "):
            out[key] = value.strip()
    return out


def migrated_of(path: Path) -> set[str]:
    """Имена полей, которые мигратор объявил своей догадкой."""
    raw = front_of(path).get("migrated", "").strip("[]")
    return {n.strip() for n in raw.split(",") if n.strip()}


def run(store: Path, *args: str) -> None:
    mem_migrate.main([str(store), *args])


# ── сохранность чужого ──────────────────────────────────────────────────────

def test_existing_fields_and_body_survive_the_migration(store):
    """Стор отдаётся клиенту и правится людьми. Мигратор дописывает конверт и
    не редактирует ничего чужого: потерянное описание или сдвинутая строка тела
    обнаружились бы месяцем позже и без возможности понять, что было."""
    note = store / "agent-work" / "drift.md"
    run(store, "--apply")
    after = note.read_text(encoding="utf-8")
    fields = front_of(note)
    assert fields["name"] == "runner-contract-drift"
    assert fields["description"] == "расхождение видно одной строкой в журнале"
    assert "metadata:\n  type: reference" in after
    assert after.endswith(NOTE.split("---\n", 2)[2])


def test_a_field_already_in_the_file_is_never_rewritten(tmp_path):
    """Дата, проставленная человеком, — единственное в конверте, чему можно
    верить без оговорок. Мигратор, который её «уточняет» по прозе, уничтожает
    ровно то, ради чего конверт заводился."""
    note = write(tmp_path / "agent-work" / "n.md", NOTE.replace(
        "metadata:", "verified: 2020-01-01\nmetadata:"))
    run(tmp_path, "--apply")
    assert front_of(note)["verified"] == "2020-01-01"


def test_an_empty_field_counts_as_filled_in_too(tmp_path):
    """Пустое поле — тоже чьё-то решение (например, «источник неизвестен»).
    Дозаполнить его догадкой значит выдать догадку за ответ человека."""
    note = write(tmp_path / "agent-work" / "n.md", NOTE.replace(
        "metadata:", "source:\nmetadata:"))
    run(tmp_path, "--apply")
    assert front_of(note)["source"] == ""


def test_a_second_run_adds_nothing_and_keeps_the_first_backup(tmp_path):
    """Мигратор запускают по стору не один раз (роль за ролью, после правок).
    Второй прогон, дописывающий те же поля второй раз, испортил бы frontmatter
    и подменил копию «до миграции» уже мигрированным файлом."""
    note = write(tmp_path / "agent-work" / "n.md")
    run(tmp_path, "--apply")
    after_first = note.read_text(encoding="utf-8")
    run(tmp_path, "--apply")
    assert note.read_text(encoding="utf-8") == after_first
    backups = sorted((tmp_path / "agent-work" / "_before-migrate").iterdir())
    assert [b.name for b in backups] == ["n.md"]
    assert backups[0].read_text(encoding="utf-8") == NOTE


# ── дата проверки ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("prose, expected", [
    ("Установлено 13.09.2026 на прогоне", date(2026, 9, 13)),
    ("Замерено 2026-09-13: индекс дорос", date(2026, 9, 13)),
    ("Ловилось с 12–13 сентября каждую ночь", date(2026, 9, 13)),
    ("Проверено 13.09, роль молчит", date(2026, 9, 13)),
])
def test_the_date_is_read_from_every_form_the_notes_use(prose, expected):
    """Живые записи датируют себя четырьмя способами сразу, и все четыре — проза.
    Поддержать три из четырёх значит объявить четверть стора недатированной."""
    assert mem_migrate.infer_verified(prose, TODAY) == expected


def test_the_latest_date_in_the_note_wins():
    """Записи правят дописыванием снизу. Первая дата говорит, когда запись
    завели, последняя — когда знание последний раз подтверждали; поле
    отвечает на второй вопрос."""
    text = "Установлено 07.09.2026. Подтверждено 13.09.2026 ещё раз."
    assert mem_migrate.infer_verified(text, TODAY) == date(2026, 9, 13)


def test_a_date_from_the_future_is_not_accepted():
    """`verified` отвечает «когда это проверяли». Проверить в будущем нельзя,
    значит такая дата в тексте всегда про другое — срок, план, пример."""
    assert mem_migrate.infer_verified("Дожить до 01.01.2099", TODAY) is None


def test_addresses_and_versions_are_not_read_as_dates():
    """В этих записях IP-адресов и номеров версий больше, чем дат. Регулярка
    без границ превращает `198.51.100.22` в дату и датирует ею запись — тихо
    и убедительно."""
    text = "RU mirror MySQL (198.51.100.22), python 3.12, версия 0.7.6"
    assert mem_migrate.infer_verified(text, TODAY) is None


def test_a_month_word_needs_a_day_to_become_a_date():
    """«К сентябрю верным не было» и «майской атаки» называют месяц, но не день.
    Достроить день до первого числа — выдумать точность, которой в тексте нет."""
    assert mem_migrate.infer_verified("к сентябрю это устарело", TODAY) is None
    assert mem_migrate.infer_verified("после 12 машин в очереди", TODAY) is None


# ── источник ────────────────────────────────────────────────────────────────

def test_the_task_the_note_was_written_in_becomes_its_source():
    """Номер задачи — единственный источник, который в этих записях
    машиночитаем: по нему видно, откуда знание и можно ли спросить у истории."""
    assert mem_migrate.infer_source("Установлено 13.09 (DEV-16), см. DEV-78") == "DEV-16"


def test_a_note_without_a_task_number_gets_no_source_at_all():
    """`наблюдение` по умолчанию отличить от слов владельца по тексту нельзя, а
    выдать второе за первое — потерять источник, который сессия не вправе
    оспаривать. Лучше пустое поле, чем правдоподобное."""
    assert mem_migrate.infer_source("Просто наблюдение без номера") is None
    assert mem_migrate.infer_source("кодировка UTF-8, формат ISO-8601") is None


# ── вид записи и срок пересмотра ────────────────────────────────────────────

def test_a_constraint_gets_no_review_date_whatsoever():
    """Срок пересмотра — признание, что знание протухает. Ограничение
    («НИКОГДА не удалять сессии») отменяет только владелец и только явно; срок
    на нём разрешил бы сессии счесть правило устаревшим по календарю."""
    assert mem_migrate.review_after("ограничение", date(2026, 9, 13)) is None
    assert mem_migrate.review_after("факт", date(2026, 9, 13)) == date(2026, 10, 13)
    assert mem_migrate.review_after("рецепт", date(2026, 9, 13)) == date(2026, 12, 12)


def test_a_constraint_in_the_store_comes_out_without_review_after(tmp_path):
    """То же самое целиком через файл: поля `review_after` в мигрированном
    ограничении нет вовсе, а не стоит далёкая дата."""
    note = write(tmp_path / "agent-work" / "ban.md", NOTE.replace(
        "Раннер досдаёт за сессию, и это видно.",
        "НИКОГДА не удаляй .jsonl сессии — бэкап делай всегда."))
    run(tmp_path, "--apply")
    fields = front_of(note)
    assert fields["kind"] == "ограничение"
    assert "review_after" not in fields


def test_review_after_is_counted_from_what_the_file_says_not_from_the_guess(tmp_path):
    """Человек назвал запись ограничением, а эвристика по тексту считает её
    фактом. Срок, посчитанный от догадки, выдал бы ограничению дату протухания
    — при том, что само поле `kind` осталось человеческим."""
    note = write(tmp_path / "agent-work" / "n.md", NOTE.replace(
        "metadata:", "kind: ограничение\nmetadata:"))
    run(tmp_path, "--apply")
    fields = front_of(note)
    assert fields["kind"] == "ограничение"
    assert "review_after" not in fields


def test_review_after_counts_from_the_human_date_when_there_is_one(tmp_path):
    """Дата человека старше выведенной, и отсчитывать срок надо от неё:
    иначе в файле окажутся две даты, между собой не связанные."""
    note = write(tmp_path / "agent-work" / "n.md", NOTE.replace(
        "metadata:", "verified: 2026-01-01\nmetadata:"))
    run(tmp_path, "--apply")
    fields = front_of(note)
    assert fields["kind"] == "справка"        # +180 дней
    assert fields["review_after"] == "2026-06-30"


def test_a_note_without_a_date_gets_no_review_date_either():
    """Срок, отсчитанный от неизвестной даты, — выдумка с точностью до дня."""
    assert mem_migrate.review_after("факт", None) is None


# ── честность: что программа решила сама ────────────────────────────────────

def test_every_guessed_field_is_named_in_migrated(store):
    """Без списка догадок проставленное регуляркой поле выглядит так же, как
    поставленное человеком, и следующая сессия принимает догадку за факт. Это
    ровно та болезнь, от которой конверт заводится, — с машинной печатью."""
    note = store / "agent-work" / "drift.md"
    run(store, "--apply")
    assert migrated_of(note) == {"kind", "source", "verified", "review_after"}


def test_what_was_read_from_the_file_is_not_called_a_guess(tmp_path):
    """Поле, которое человек проставил сам, мигратор не трогает — и в список
    своих догадок не записывает: иначе пометка обесценится, начав стоять на
    всём подряд."""
    note = write(tmp_path / "agent-work" / "n.md", NOTE.replace(
        "metadata:", "verified: 2020-01-01\nmetadata:"))
    run(tmp_path, "--apply")
    assert "verified" not in migrated_of(note)
    assert "kind" in migrated_of(note)


def test_kind_is_a_guess_even_when_the_old_type_field_agrees(tmp_path):
    """`metadata: type:` — лучший из сигналов, но это перевод старой
    классификации в новую, а не чтение нового поля. У эвристики нет способа
    отличить попадание от совпадения слов, поэтому `kind` помечается всегда."""
    note = write(tmp_path / "agent-work" / "n.md")
    run(tmp_path, "--apply")
    assert "kind" in migrated_of(note)


def test_scope_and_roles_are_taken_from_the_store_not_from_the_prose(store):
    """Эти два поля — не догадка: роль записи это каталог, в котором она лежит,
    а область применения даёт вызывающий. Пометить их догадкой значило бы
    уравнять факт файловой системы с чтением прозы регуляркой."""
    note = store / "agent-work" / "drift.md"
    run(store, "--apply", "--scope", "client-b")
    fields = front_of(note)
    assert fields["scope"] == "client-b"
    assert fields["roles"] == "[work]"
    assert migrated_of(note) & {"scope", "roles"} == set()


def test_supersedes_is_never_filled_by_the_migrator(store):
    """Что именно запись заменяет, знает только писавший. Догадка здесь
    выключила бы живую запись — молча и без следа."""
    run(store, "--apply")
    assert "supersedes" not in front_of(store / "agent-work" / "drift.md")


# ── dry-run и копии ─────────────────────────────────────────────────────────

def test_without_apply_nothing_reaches_the_disk(store, capsys):
    """Разовый прогон по чужому живому стору должно быть безопасно запустить
    просто чтобы посмотреть, что получится."""
    note = store / "agent-work" / "drift.md"
    run(store)
    assert note.read_text(encoding="utf-8") == NOTE
    assert not (store / "agent-work" / "_before-migrate").exists()
    assert "dry-run" in capsys.readouterr().out


def test_apply_puts_the_original_file_into_before_migrate(store):
    """Владелец не удаляет ничего: старое уводится в подкаталог. Копия «до»
    — единственный способ откатить неудачную догадку по всему стору разом."""
    run(store, "--apply")
    copy = store / "agent-work" / "_before-migrate" / "drift.md"
    assert copy.read_text(encoding="utf-8") == NOTE


def test_the_backup_directory_is_not_migrated_as_a_note(store):
    """Копии лежат внутри каталога роли. Принять их за записи — мигрировать
    собственный бэкап и потерять состояние «до»."""
    run(store, "--apply")
    run(store, "--apply")
    copy = store / "agent-work" / "_before-migrate" / "drift.md"
    assert copy.read_text(encoding="utf-8") == NOTE


# ── обход стора и отчёт ─────────────────────────────────────────────────────

def test_the_layer_index_is_not_a_note(store):
    """`MEMORY.md` — индекс слоя, его пишет сессия целиком. Конверт на нём не
    значит ничего, а строки во frontmatter индекс сломают."""
    index = write(store / "agent-work" / "MEMORY.md", "- drift — про раннер\n")
    run(store, "--apply")
    assert index.read_text(encoding="utf-8") == "- drift — про раннер\n"


def test_roles_come_from_their_own_directories(tmp_path):
    """Одна запись адресована проверке, другая работе — и перепутать их нельзя:
    роль читает свой каталог и получает чужое правило как своё."""
    write(tmp_path / "agent-verify" / "v.md")
    write(tmp_path / "agent-work" / "w.md")
    run(tmp_path, "--apply")
    assert front_of(tmp_path / "agent-verify" / "v.md")["roles"] == "[verify]"
    assert front_of(tmp_path / "agent-work" / "w.md")["roles"] == "[work]"


def test_a_note_without_frontmatter_gets_an_envelope_and_keeps_its_body(tmp_path):
    """Frontmatter есть не у всех записей. Пропустить такую — оставить в сторе
    запись без конверта; переписать тело — потерять её содержание."""
    note = write(tmp_path / "agent-work" / "bare.md", "Просто текст от 07.09.2026.\n")
    run(tmp_path, "--apply")
    text = note.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert text.endswith("Просто текст от 07.09.2026.\n")
    assert front_of(note)["verified"] == "2026-09-07"


def test_the_report_names_every_undated_note(store, capsys):
    """Записи без даты — работа, которую придётся доделать человеком. Число
    без списка не говорит, где именно её делать."""
    write(store / "agent-work" / "nodate.md", NOTE.replace("13.09.2026 (DEV-16)", "тогда-то"))
    run(store)
    out = capsys.readouterr().out
    assert "просмотрено записей: 2" in out
    assert "дата проверки выведена: 1 из 2" in out
    assert "nodate.md" in out and "drift.md" not in out
