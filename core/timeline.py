"""Лента заходов (DEV-108): переписка машин одной лентой, по строке на заход.

Три круга проверки одной задачи — это сегодня три полотна подряд. На DEV-41
их было восемь: 2530, 4882, 6987, 7846, 938, 5990, 2211, 7613 знака, и два
ПРОТИВОПОЛОЖНЫХ вердикта на одном и том же коммите (`OK` → `FAIL` → `OK`).
Расхождение — самое важное, что в этой переписке есть, и именно оно тонуло:
чтобы его увидеть, надо было прочитать всё и сравнить первые строки.

Лента отвечает на третий вопрос человека — «почему машины спорили и чем спор
кончился» — не заставляя открывать ни один из полотен: заход, время, роль,
вердикт, по строке. Расхождение названо словами в отдельной строке над лентой.

Собирает её раннер, а не сессия, и иначе нельзя: ни одна сессия не видит
заходов, кроме своего. Лента — ОДИН комментарий на задачу, который правится
на месте (тот же ход, что у `issue_feed` в приёме): копить по ленте на заход
значило бы чинить полотна полотнами.

Разбор чужих комментариев здесь — по форме, которую задают контракты
(`prompt.py`): шапка (`headline.py`), разделы сдачи (`handoff.py`), голова
вердикта (`OK`/`PARTIAL`/`FIXED`/`FAIL`). Форма разъедется — лента обеднеет
до «машина что-то написала», но не соврёт и не упадёт: роль по неопознанному
комментарию не выдумывается.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import glossary
import handoff
import headline

# По этой строке лента узнаёт СВОЙ комментарий среди остальных — и чтобы
# поправить его вместо второго, и чтобы не посчитать себя заходом. Видимая, а
# не HTML-комментарий: разметку трекер может вырезать, а текст — нет.
# Заголовком, а не жирным: титул перед шапкой разрешён только заголовком
# (`headline._lead`), и лента обязана проходить свой же линт шапки.
MARK = "## 🧵 Лента заходов"

ROLE_WORK = "работа"
ROLE_REVIEW = "ревью"
ROLE_VERIFY = "проверка"
ROLE_INTAKE = "приём"
ROLE_RUNNER = "раннер"
ROLE_UNKNOWN = "машина"

VERDICT_HEADS = ("OK", "PARTIAL", "FIXED", "FAIL")

_ORDINALS = ("первый", "второй", "третий", "четвёртый", "пятый",
             "шестой", "седьмой", "восьмой", "девятый", "десятый")

# Голова вердикта в обратных кавычках — так её пишут контракты и так она
# стоит в титуле проверки. Без кавычек тоже читается, но только в начале
# строки: иначе `FAIL` из фразы «ложный FAIL стоит одного круга» посчитался
# бы вердиктом.
_VERDICT_QUOTED = re.compile(r"`(" + "|".join(VERDICT_HEADS) + r")(/[A-Za-zА-Яа-яё]+)?`")
_VERDICT_BARE = re.compile(r"(?m)^[\s#>*_]*(" + "|".join(VERDICT_HEADS) + r")(/[A-Za-zА-Яа-яё]+)?\b")
# Коммит — только названный словом «коммит»: в тексте проверок хватает и
# сумм `md5`, и позиций подстрок, и семь шестнадцатеричных знаков среди них
# найдутся всегда.
# Класс вердикта под чертой: помеченная строка, которую пишет сессия проверки
# (её слова — в `headline.verdict_block`). Ищется по всему тексту, а не в окне
# заголовка: помета однозначна.
_VERDICT_MARKED = re.compile(
    r"(?mi)^\s{0,3}[*_]{0,2}Вердикт[*_]{0,2}\s*[:—-]\s*[*_`]{0,2}("
    + "|".join(VERDICT_HEADS) + r")(/[A-Za-zА-Яа-яё]+)?")

_COMMIT = re.compile(r"комми[а-яё]*\s*[`'\"]?([0-9a-f]{7,40})\b")
_PR = re.compile(r"\b(?:PR|MR)\s*[#!]?(\d+)")
_FINDINGS = re.compile(r"(\d+)\s+наход")
_HEAD_WINDOW = 500
# Ниже этой длины строка шапки не отвечает ни на что — набираем следующую
# фразу. Порог, а не «одно предложение»: см. `_first_phrase`.
_READABLE = 60
# Блок кода и ссылка внутри разделов сдачи. Оба надо разворачивать в строку:
# сплющенный тремя кавычками блок в шапке — это уже не ответ, а его обломок,
# а полный URL занимает всю строку и не говорит ничего.
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
# Комментарий приёма о мерже — единственный признак того, что человек работу
# ПРИНЯЛ, и по нему лента отвечает «от человека не требуется ничего». Поэтому
# узнаётся он по своему НАЧАЛУ, а не по фразе где-нибудь в тексте: отчёт
# работы вправе написать «задача закроется по мержу», и поймав это, лента
# сказала бы про открытый PR, что мержить уже нечего.
MERGED_MARK = r"^\s*🤖\s*\**Закрыто по мержу"
# Стартовый комментарий сессии («взялся») — заход, но не отчёт: ответ «что
# сделано» из него брать нечего.
_START_MARK = "взялся"
# Слова, по которым узнаётся роль. Своими именами, а не литералами внутри
# правил: те же слова стоят в контрактах (`prompt.py`), и связь между «что
# велено написать» и «по чему это потом узнают» должна быть видна.
_MARK_CRITERION = r"Источник критерия"       # проверка, шаг эталона
_MARK_VERIFY_TITLE = r"(?m)^\s{0,3}#{1,6}[^\n]*Проверка"   # титул вердикта
_MARK_HANDOFF = r"(?m)^\s{0,3}#{0,6}\s*\**Формат выхода"    # раздел сдачи
_MARK_REVIEW = r"код-ревью"


def _is_start(body: str) -> bool:
    return _START_MARK in (body or "")[:200].lower()


def _looks_like(body: str, pattern: str) -> bool:
    return re.search(pattern, body, re.IGNORECASE | re.MULTILINE) is not None


def role_of(body: str, author: str = "") -> str:
    """Роль автора комментария — по тексту, а не по подписи.

    Подпись личности (`personas.py`) появляется только у инсталляции, которая
    завела приложения в трекере; там, где их нет, ВСЁ подписано владельцем
    ключа, и лента по автору сложилась бы в один голос. Поэтому текст первый,
    а автор — только подсказка для того, чего в тексте нет.

    Порядок правил — не вкусовой. Роли узнаются по СВОИМ словам, но одни и те
    же слова встречаются у разных ролей внутри текста: отчёт работы обязан
    сказать «итог ревью» (контракт, шаг сдачи), а его раздел «Цель» после
    возврата начинается со слова «Проверка». Поэтому «код-ревью» ищется только
    в самом начале комментария, а вердикт проверки — вместе с титулом.
    """
    body = (body or "").strip()
    if not body:
        return ROLE_UNKNOWN
    if _looks_like(body, MERGED_MARK):
        return ROLE_INTAKE
    if _looks_like(body, r"^\s*🤖"):
        return ROLE_RUNNER
    if _looks_like(body, _MARK_CRITERION) or (
            _looks_like(body, _MARK_VERIFY_TITLE) and verdict_of(body)):
        return ROLE_VERIFY
    if _looks_like(body, _MARK_HANDOFF) or _is_start(body):
        return ROLE_WORK
    if _looks_like(body[:200], _MARK_REVIEW):
        return ROLE_REVIEW
    known = {ROLE_WORK: ("engineer",), ROLE_REVIEW: ("reviewer",),
             ROLE_VERIFY: ("qa",), ROLE_INTAKE: ("intake",)}
    low = (author or "").strip().lower()
    for role, names in known.items():
        if low in names:
            return role
    return ROLE_UNKNOWN


def verdict_of(body: str) -> str:
    """Вердикт из комментария проверки: `OK`, `FAIL/implementation`, … Пусто —
    вердикта в тексте нет.

    Два места, и оба нужны. Раньше класс стоял в титуле («## Проверка DEV-41 —
    `OK`»), и по титулу его читают все комментарии, уже лежащие в задачах.
    С DEV-108 первый экран принадлежит человеку, машинного слова там быть не
    должно, и класс переехал под черту отдельной помеченной строкой. Искать
    её можно по всему тексту: помета `**Вердикт:**` однозначна, в отличие от
    голого `FAIL`, который встречается и в прозе («ложный FAIL стоит круга»).
    """
    head = (body or "")[:_HEAD_WINDOW]
    m = (_VERDICT_QUOTED.search(head) or _VERDICT_BARE.search(head)
         or _VERDICT_MARKED.search(body or ""))
    return (m.group(1) + (m.group(2) or "")) if m else ""


def _cut(text: str, limit: int) -> str:
    """Обрезка по словам, не по знакам: обрубленное слово читается как сбой
    ленты, а не как «дальше в отчёте»."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    return (head[:head.rfind(" ")] if " " in head else head).rstrip() + "…"


def _plain(text: str) -> str:
    """Текст без блоков кода и без адресов ссылок — то, что можно положить в
    одну строку, не превратив её в мусор."""
    return _FENCE.sub(" ", _LINK.sub(r"\1", text or ""))


def _first_command(text: str) -> str:
    """Первая ЗНАЧАЩАЯ команда из блока кода раздела. Пусто — блока нет.

    Раздел «Чем наблюдать» обычно кончается блоком с командой, и это ровно то,
    что человек спрашивает в «как воспользоваться». Переход в каталог и
    подстановка окружения командой не считаются: первой строкой блока почти
    всегда стоит `cd`, и именно он уезжал в шапку вместо самой проверки.
    """
    m = _FENCE.search(text or "")
    if not m:
        return ""
    lines = [x.strip() for x in m.group(1).splitlines()]
    for i, line in enumerate(lines):
        if not line or line.startswith("#"):
            continue
        if re.match(r"(cd|export|set|source|\.)\b", line):
            continue
        # Перенос строки обратным слешем — одна команда, а не команда и мусор:
        # без склейки в шапку уезжало `… --repos-root /repos \`, где половина
        # аргументов осталась на следующей строке.
        while line.endswith("\\") and i + 1 < len(lines):
            i += 1
            line = line[:-1].rstrip() + " " + lines[i]
        return line
    return ""


def _first_phrase(text: str, limit: int = headline.MAX_PHRASE_CHARS) -> str:
    """Начало текста в одну строку — то, что влезет в строку шапки.

    Не «первое предложение»: разделы сдачи начинаются и списком, и жирным
    словом, и на DEV-41 «первым предложением» раздела «Чем наблюдать» выходило
    `**1.` — то есть ничего. Поэтому фразы набираются, пока не станет читаемо
    (`_READABLE`), но набор, не влезший в потолок, ОТКАТЫВАЕТСЯ к первой фразе,
    а не обрезается посередине второй: обрывок «…: Ожидаемое (наблюдал…» не
    отвечает ни на что и читается как сбой.
    """
    flat = " ".join(re.sub(r"[*#>]", " ", _plain(text)).split())
    flat = re.sub(r"^(?:[-+•]|\d{1,2}[.)])\s*", "", flat).strip()
    if not flat:
        return ""
    phrases = re.split(r"(?<=[.!?;])\s", flat)
    out = ""
    for part in phrases:
        grown = f"{out} {part}".strip() if out else part
        if len(grown) > limit:
            break
        out = grown
        if len(out) >= _READABLE:
            break
    if not out:
        out = _cut(phrases[0], limit)
    return out


def _when(raw: str) -> str:
    """Время захода человеческим видом: `15.09 20:24`, UTC.

    Именно UTC, а не пояс читателя: сами сессии пишут в комментариях время по
    `date -u` («предыдущая (23:37) тоже поставила OK»), и лента обязана
    сходиться с ними — сдвинутая на пояс, она называет один и тот же заход
    другим временем, чем его вердикт. Пояс поэтому назван в шапке ленты, а не
    угадан. Не разобралось — пусто: выдуманное время хуже отсутствующего.
    """
    try:
        dt = datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%d.%m %H:%M")


def rounds(comments: list[dict]) -> list[dict]:
    """Заходы по комментариям задачи, от старого к новому.

    Вход — что отдаёт трекер: `createdAt`, `body`, `author`. Своя же лента из
    заходов выкидывается: она не заход, а их отражение.
    """
    out: list[dict] = []
    for c in sorted(comments or [], key=lambda c: str(c.get("createdAt") or "")):
        body = str(c.get("body") or "")
        if body.lstrip().startswith(MARK):
            continue
        if not body.strip():
            continue
        # Машина читает текст БЕЗ расшифровок: они добавляют к каждому коду
        # по полсотни знаков и сдвигают за окно поиска то, что ниже, —
        # коммит, названный в четвёртой строке шапки, перестал бы находиться.
        plain = glossary.undecorate(body)
        role = role_of(plain, str(c.get("author") or ""))
        commit = _COMMIT.search(plain[:_HEAD_WINDOW])
        pr = _PR.search(plain[:_HEAD_WINDOW])
        out.append({
            "when": _when(str(c.get("createdAt") or "")),
            "role": role,
            "merged": _looks_like(body, MERGED_MARK),
            "verdict": verdict_of(plain) if role == ROLE_VERIFY else "",
            "commit": commit.group(1) if commit else "",
            "pr": pr.group(1) if pr else "",
            "body": body,
        })
    return out


def _ordinal(i: int) -> str:
    return _ORDINALS[i] if i < len(_ORDINALS) else f"{i + 1}-й"


def disagreement(rs: list[dict]) -> str:
    """Спор машин словами. Пусто — проверок не было вовсе.

    Называется прямо, а не выводится читателем из сравнения первых строк: на
    DEV-41 два противоположных вердикта стояли на одном коммите, и это было
    видно только тому, кто прочитал оба полотна целиком.
    """
    checks = [r for r in rs if r["role"] == ROLE_VERIFY]
    if not checks:
        return ""
    said = [r["verdict"] or "без вердикта" for r in checks]
    heads = {v.split("/", 1)[0] for v in said}
    listing = ", ".join(f"{_ordinal(i)} заход `{v}`" for i, v in enumerate(said))
    if len(heads) == 1:
        if len(checks) == 1:
            return f"**Проверка была одна:** `{said[0]}`."
        return f"**Вердикты не расходились:** заходов {len(checks)}, все `{said[0]}`."
    same = _same_commit_split(checks)
    return f"**Вердикты расходились:** {listing}." + (f" {same}" if same else "")


def _same_commit_split(checks: list[dict]) -> str:
    """Противоположные вердикты на ОДНОМ коммите — отдельной фразой.

    Это другой случай, чем «первый заход не прошёл, доработали, второй
    прошёл»: код между ними не менялся, значит разошлись сами проверки, и
    доверять обеим одинаково нельзя.
    """
    by_commit: dict[str, list[tuple[int, str]]] = {}
    for i, r in enumerate(checks):
        if r["commit"]:
            by_commit.setdefault(r["commit"], []).append((i, r["verdict"]))
    for commit, said in by_commit.items():
        if len({v.split("/", 1)[0] for _, v in said}) > 1:
            who = " и ".join(f"{_ordinal(i)}" for i, _ in said)
            return (f"Вердикты {who} — на ОДНОМ И ТОМ ЖЕ коммите `{commit}`: "
                    f"код между ними не менялся, разошлись сами проверки.")
    return ""


def _line(i: int, r: dict) -> str:
    """Одна строка ленты на один заход. Без имён файлов и без чисел, которые
    в отрыве ничего не значат, — за подробностями человек идёт в сам
    комментарий, и ради этого лента короткая."""
    what = {
        ROLE_VERIFY: (f"вердикт `{r['verdict']}`" if r["verdict"] else "вердикта не оставила"),
        ROLE_REVIEW: "код-ревью диффа",
        ROLE_WORK: ("взялся" if _is_start(r["body"]) else "отчёт о работе"),
    }.get(r["role"], _first_phrase(re.sub(r"[*`#>]", "", r["body"]), 80))
    if r["role"] == ROLE_REVIEW:
        m = _FINDINGS.search(r["body"][:_HEAD_WINDOW])
        if m:
            what += f", находок {m.group(1)}"
    tail = []
    if r["role"] == ROLE_VERIFY and r["commit"]:
        tail.append(f"коммит `{r['commit']}`")
    if r["role"] == ROLE_WORK and r["pr"]:
        tail.append(f"PR #{r['pr']}")
    return (f"{i + 1}. `{r['when'] or '—'}` · **{r['role']}** — {what}"
            + (f" ({', '.join(tail)})" if tail else ""))


def _work_answers(rs: list[dict]) -> tuple[str, str]:
    """(что сделано, как воспользоваться) — из последнего отчёта работы.

    Сначала из его шапки: сессия обязана её написать (контракт, шаг сдачи), и
    её слова точнее любого пересказа. Шапки нет — заход старый или сессия её
    не написала — берётся первая фраза разделов «Цель» и «Чем наблюдать»: они
    ровно для этого и заведены, и на задачах, написанных до шапки, лента
    отвечает по ним, а не молчит.
    """
    reports = [r for r in rs if r["role"] == ROLE_WORK and not _is_start(r["body"])]
    for r in reversed(reports):
        done, use = _answers_of(r["body"])
        if done:
            return done, use
    return "", ""


# Как раздел сдачи назывался у разных поколений контракта. Только для ЧТЕНИЯ:
# лента разбирает то, что уже лежит в задачах, а там сдача написана и по
# сегодняшней схеме, и по вчерашней.
_DONE_SECTIONS = ("Цель", "Что сделано", "Что сделал")
_WATCH_SECTIONS = ("Чем наблюдать", "Чем проверено", "Как посмотреть самому",
                   "Проверено")


def _section(parts: dict, *names: str) -> str:
    for name in names:
        got = parts.get(handoff.key_of(name), "")
        if got:
            return got
    return ""


def _answers_of(body: str) -> tuple[str, str]:
    """(что сделано, как посмотреть) из одного комментария. Пусто — не отсюда.

    Берётся ПОСЛЕДНИЙ отчёт, который вообще отвечает, а не просто последний
    комментарий роли: на DEV-66 последним заходом работы лежит уточнение к
    границе на два абзаца, и по нему лента отвечала «отчёта в задаче ещё нет»
    — при том, что двумя комментариями выше лежал целый отчёт.
    """
    fields = headline.fields_of(body)
    done = fields.get(handoff.key_of("Что сделано"), "")
    use = fields.get(handoff.key_of("Как посмотреть самому"), "")
    if not done or not use:
        parts = handoff.sections_of(body)
        done = done or _first_phrase(_section(parts, *_DONE_SECTIONS))
        watch = _section(parts, *_WATCH_SECTIONS)
        use = use or _with_command(_first_phrase(watch, _USE_PROSE), watch)
    return done, use


# Сколько строки «как воспользоваться» отдать прозе, чтобы хватило на
# команду: потолок строки шапки один, и делить его надо здесь, а не на обрезке.
_USE_PROSE = 110


def _with_command(prose: str, section: str) -> str:
    """Проза раздела плюс его команда, если она есть и влезает."""
    cmd = _first_command(section)
    if not cmd:
        return prose
    room = headline.MAX_PHRASE_CHARS - len(prose) - 4
    if room < 20:
        return prose
    if len(cmd) > room:
        cmd = cmd[:room - 1].rstrip() + "…"
    return (f"{prose} `{cmd}`" if prose else f"`{cmd}`").strip()


# Чем кончился последний заход проверки — куском фразы, а не кодом: этот
# кусок едет в шапку, а выше черты машинных слов нет вовсе.
_OUTCOME = {
    "OK": "проверка прошла",
    "FIXED": "проверка прошла со второго раза",
    "PARTIAL": "проверке было нечем посмотреть поведение",
    "FAIL": "проверка работу не подтвердила",
}

# Что теперь нужно от человека — законченным предложением, а не значением
# поля: из значений собирается бланк, а владелец его вернул. Здесь же ответ
# на «чего ждать»: придёт ли сюда ещё кто-нибудь сам. Два вопроса в одной
# фразе нарочно — по-человечески они и говорятся вместе.
_ACT = {
    "OK": "дальше нужен твой мерж — сама задача с места не сдвинется",
    "FIXED": "дальше нужен твой мерж — сама задача с места не сдвинется",
    "PARTIAL": "посмотреть придётся тебе — агент этого не починит",
    "FAIL": "надо разобрать, что именно разошлось",
}

# То же по КЛАССУ расхождения, а не по его голове. Владелец привёл ровно этот
# пример: на «задаче слишком крупной» ему нужно «иди дроби», а не «разобрать».
# Класс несёт действие, голова — только настроение.
_ACT_BY_CLASS = {
    "FAIL/specification": "задачу надо разрезать или уточнить — она слишком "
                          "крупная или поставлена неточно, и в коде чинить "
                          "нечего; агент за неё не возьмётся, пока так",
    "FAIL/implementation": "от тебя ничего не нужно: дефект настоящий, задача "
                           "вернулась агенту и работа идёт",
    "FAIL/unobservable": "нужен стенд, выкат или доступ — смотреть было негде, "
                         "ни код, ни постановка не виноваты, и агент этого не "
                         "починит",
    "PARTIAL/specification": "критерий надо поправить тебе: он есть, но не про "
                             "эту работу",
}

# Роли, чей заход означает, что работа продолжилась.
_ALIVE = (ROLE_WORK, ROLE_REVIEW, ROLE_VERIFY)


def _closed_by_merge(rs: list[dict]) -> bool:
    """Задача закрыта мержем — и после этого к ней не возвращались.

    Два условия, и второе стоило живой проверки. По одной только метке мержа
    лента объявляла закрытыми DEV-13, DEV-66 и DEV-102, у которых мерж был
    ПРОШЛЫМ кругом, а потом задачу переоткрыли: человек читал «от тебя ничего
    не требуется» про задачу, которая как раз его и ждала. Хуже голого кода:
    голый код непонятен, а это понятно и неправда.

    Считается по метке мержа, а не по роли автора: роль `приём` получает любой
    комментарий приёма — и «круги исчерпаны», и «ветка перестала сливаться», —
    то есть ровно те случаи, когда работа НЕ принята.
    """
    last = -1
    for i, r in enumerate(rs):
        if r.get("merged"):
            last = i
    if last < 0:
        return False
    return not any(r["role"] in _ALIVE for r in rs[last + 1:])


def render(comments: list[dict]) -> str:
    """Лента целиком: шапка прозой, черта, спор словами, по строке на заход.

    Заходов нет вовсе — пусто: комментарий про то, что комментариев нет,
    нужен не человеку, а ленте.
    """
    rs = rounds(comments)
    if not rs:
        return ""
    checks = [r for r in rs if r["role"] == ROLE_VERIFY and r["verdict"]]
    last = checks[-1]["verdict"].split("/", 1)[0].upper() if checks else ""
    merged = _closed_by_merge(rs)
    done, use = _work_answers(rs)
    # «Проверки ещё не было» и «проверка была, но ничего не сказала» — разные
    # новости: во втором случае заход потрачен, а ответа нет, и человек должен
    # знать, что ждать нечего.
    silent = any(r["role"] == ROLE_VERIFY and not r["verdict"] for r in rs)
    outcome = (_OUTCOME.get(last, "проверка сказала своё слово") if last
               else ("проверка была, но словами ничего не сказала"
                     if silent else "проверки ещё не было"))
    if merged:
        act = "от тебя ничего не нужно: работа принята, сюда больше никто не придёт"
        outcome = "задача уже закрыта мержем"
    else:
        full = checks[-1]["verdict"] if checks else ""
        act = _ACT_BY_CLASS.get(full) or _ACT.get(
            last, "от тебя пока ничего не нужно: следующей придёт проверка и "
                  "скажет, сошлось или нет")
    # Куски приезжают из чужих отчётов, и машинные слова в них заменяет
    # `headline.render` (`soften`): скобка с расшифровкой ставит код РЯДОМ, а
    # выше черты кода быть не должно вовсе — лента иначе печатала бы то, за
    # что раннер вернул бы сессию. Ниже черты, в строках заходов, разметка
    # расшифровкой остаётся.
    head = headline.render(
        done=done or
             "Отчёта работы в задаче пока нет — что сделано, видно только по "
             "ленте ниже",
        use=(f"Посмотреть самому — {use}" if use else
             "Посмотреть пока нечего: отчёт работы не назвал места"),
        act=f"{outcome[0].upper()}{outcome[1:]}, {act}",
        title=f"{MARK} — заходов {len(rs)}, из них проверок "
              f"{len([r for r in rs if r['role'] == ROLE_VERIFY])}. Время UTC.",
    )
    body = [d for d in (disagreement(rs),) if d] + [""] + [_line(i, r) for i, r in enumerate(rs)]
    # Лента — единственный машинный текст, который человек читает целиком, и
    # вердиктов в ней столько же, сколько заходов. Разметка расшифровкой здесь
    # не украшение: без неё лента отвечает на «чем кончился спор» кодами.
    return headline.with_details(head, glossary.decorate("\n".join(body).strip()))
