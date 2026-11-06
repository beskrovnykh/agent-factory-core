"""Приём постановок: модель и очередь. Часть ПРОДУКТА, не инсталляции.

Почему это здесь, а не у клиента. До 09.09.2026 весь приём — разбор входящего
сообщения, кнопки, ключи пачки, постановка задачи в очередь — жил в
`events-api` **инфраструктурного репозитория первого клиента**. В продукте его
не было вовсе.

Следствие проверялось владельцем прямо: «уберу кнопку в фабрике — уберётся ли
у Дока?» Ответ был «нет»: кнопок в фабрике нет. Вторая инсталляция стояла с
работающим раннером и без «лица» — принять задачу и показать кнопку ей было
нечем.

Слои разведены по зависимостям, чтобы продукт не тащил за собой ни FastAPI, ни
redis, ни конкретный мессенджер:

    модель      (этот файл) — чистые функции: разбор сообщения, клавиатуры,
                              ключи, допуск. Ни сети, ни хранилища.
    хранилище   (Store)     — протокол: пачка, счётчик, идемпотентность.
    транспорт   (Transport) — протокол: отправить, поправить, ответить.
    инсталляция              — реализации протоколов и вебхуки. Своя у каждой.

Транспортов уже два (Telegram и MAX), и именно их расхождение стоило владельцу
трёх одинаковых поломок за неделю: правку вносили в один путь и не вносили в
другой. Общая модель здесь — то, что делает такое расхождение невозможным, а
не «проверяемым».
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("intake")


# ── ключи ────────────────────────────────────────────────────────────────
#
# Ключи считает продукт, а не инсталляция: разные инсталляции живут в одном
# redis (проверено на bluefreedom, где рядом стоит Док), и совпадение ключей
# означало бы, что пачка одного клиента доедет до сессии другого.

def batch_key(source: str, chat_id) -> str:
    """Куски будущей постановки, накопленные в этом чате."""
    return f"intake:{source}:batch:{chat_id}"


def control_key(source: str, chat_id) -> str:
    """Сообщение со счётчиком и кнопками. После сборки забывается —
    иначе следующая пачка молча правит отработавшее сообщение выше по ленте,
    и человек решает, что бот не ответил (09.09, MAX)."""
    return f"intake:{source}:control:{chat_id}"


# Сколько живёт связь «карточка → задача». Отвечают почти всегда сразу, а
# карточек за месяц накапливаются сотни: вечная связь — это состояние, которое
# растёт и никогда не убирается. Неделя с запасом перекрывает «увидел утром,
# ответил вечером», и при этом истёкшая связь не теряет ответ — он ведёт себя
# как обычное сообщение (см. `Channel.on_item`).
ANSWER_TTL_SEC = 7 * 24 * 60 * 60


def answer_key(source: str, chat_id, message_id: str) -> str:
    """Ключ связи: ответ на ЭТО сообщение — комментарий в ТУ задачу.

    Ключ включает источник и чат, а не только идентификатор сообщения:
    нумерация у мессенджеров своя, и номер из MAX однажды совпадёт с номером
    из Telegram — тогда ответ уехал бы в чужую задачу.
    """
    return f"intake:answer:{source}:{chat_id}:{message_id}"


def seen_key(source: str, update_type: str, timestamp, ident: str) -> str:
    """Отпечаток обработанного события.

    MAX ретраит доставку до десяти раз: без идемпотентности одно нажатие
    «Собрать задачу» заводит несколько сессий — сожжённые слоты и дубли
    задач. У Telegram такой заботы нет, но ключ общий: транспорт, который
    не ретраит, просто никогда не встретит совпадения."""
    return f"intake:{source}:seen:{update_type}:{timestamp}:{ident}"


# ── элемент пачки ────────────────────────────────────────────────────────

@dataclass
class Item:
    """Один кусок будущей постановки: текст, картинка или файл.

    `source` едет вместе с куском, а не рядом: раннер возвращает пачку как
    есть, и по ней потом определяется, в какой мессенджер отвечать. Без
    этого карточка с заведённой задачей уходит в чужой чат — 09.09 так и
    случилось, `telegram alert failed: 400`.
    """
    type: str                      # text | photo | document | voice | unsupported
    source: str                    # telegram | max
    text: str = ""
    frm: str = ""                  # от кого переслано
    file_id: str = ""              # Telegram: getFile по нему
    file_url: str = ""             # MAX: прямая ссылка, getFile-аналога нет
    file_name: str = ""
    file_size: int = 0
    mime: str = ""
    # Отпечаток куска в мессенджере. Нужен на возврате фактуры: без него один
    # и тот же кусок, вернувшийся от раннера с пометкой об ошибке, считается
    # новым и ложится в пачку вторым (см. item_id).
    message_id: str = ""
    # На какое сообщение это ответ. Пусто — обычное сообщение. Нужно, чтобы
    # отличить ответ на карточку заведённой задачи от новой фактуры: без него
    # ответ человека на вопрос приёмщика молча ложится в пачку и становится
    # ВТОРОЙ задачей вместо комментария к первой.
    #
    # В `to_dict` НЕ едет намеренно: развилка срабатывает до пачки, и в самой
    # пачке это поле никто не читает. Класть его туда значит возить по очереди
    # и хранить в redis то, чем никто не пользуется.
    reply_to: str = ""
    date: int = 0
    # Почему кусок не взят. Заполнено только у type="unsupported": молча
    # съеденный скриншот хуже отказа — постановка соберётся без него, и понять
    # это можно будет только по итоговому черновику.
    reason: str = ""

    def to_dict(self) -> dict:
        d = {"type": self.type, "source": self.source}
        if self.message_id: d["message_id"] = self.message_id
        if self.date: d["date"] = self.date
        if self.text: d["text"] = self.text
        if self.frm: d["from"] = self.frm
        if self.file_id: d["file_id"] = self.file_id
        if self.file_url: d["file_url"] = self.file_url
        if self.file_name: d["file_name"] = self.file_name
        if self.file_size: d["file_size"] = self.file_size
        if self.mime: d["mime"] = self.mime
        return d


# ── допуск ───────────────────────────────────────────────────────────────

def is_allowed(user_id, allowed: set[str]) -> bool:
    """Пускать ли этого человека.

    Сверяем по идентификатору ЧЕЛОВЕКА, не по чату: чат может быть
    переиспользован, человек — нет.

    Пустой список — не пускаем никого. Это безопаснее, чем «работает у
    всех»: инсталляция, где допуск забыли настроить, молчит, а не открывает
    приём задач кому попало.
    """
    return bool(allowed) and str(user_id) in {str(x) for x in allowed}


# ── клавиатура ───────────────────────────────────────────────────────────

@dataclass
class Button:
    text: str
    payload: str


def collect_keyboard(chat_id=None) -> list[Button]:
    """Кнопки под пачкой. Общие для всех транспортов — в этом и смысл:
    убрал кнопку в продукте, она исчезла у всех инсталляций.

    В payload едет чат: кнопка адресует пачку СВОЕГО чата. Пересланное
    сообщение с живой кнопкой иначе трогало бы чужую пачку.
    """
    tail = f":{chat_id}" if chat_id is not None else ""
    return [Button("📝 Собрать задачу", f"intake:collect{tail}"),
            Button("🗑 Сбросить", f"intake:reset{tail}")]


def collect_head(items: list[dict]) -> str:
    """Шапка сообщения на время сборки. Здесь, а не в счётчике: она нужна и
    без него, а считается по тому же составу пачки."""
    return f"🧠 Собираю задачу из {len(items)} шт. ({summary(items)})…"


def control_text(items: list[dict]) -> str:
    """Счётчик пачки: сколько кусков и из чего они.

    Состав, а не только число: человек видит, что доехало, не пересматривая
    переписку. При переносе приёма в продукт состав и значки потерялись —
    счётчик стал сухим «Пачка: 6 шт.», и это первое, что было замечено.
    """
    return (f"📥 Пачка: {len(items)} шт. ({summary(items)}).\n"
            f"Когда всё перешлёшь — жми «Собрать задачу».")


RESULTS_KEY = "agent:intake:results"

# Потолок на пачку — защита от «переслал переписку за месяц»: сессия читает
# всё перечисленное, и полсотни вложений это её память и её таймаут.
MAX_ITEMS = 40
# Лимит платформы, не наш: Bot API отдаёт файлы не больше 20 МБ. Проверяем на
# приёме, чтобы человек узнал сразу и переслал иначе, а не через минуту от
# сессии, которой файл не доехал.
MAX_FILE_BYTES = 20 * 1024 * 1024
TTL_SEC = 60 * 60 * 24 * 7

# Потолок карточки в мессенджере. Не лимит транспорта (там 4096), а предел
# читаемости: карточку человек проглядывает, читает он задачу в трекере.
CARD_LIMIT = 900


def summary(items: list[dict]) -> str:
    """Из чего собралась пачка — одной строкой, чтобы человек видел, что
    доехало, не пересматривая переписку."""
    counts: dict[str, int] = {}
    for it in items:
        counts[it.get("type") or "?"] = counts.get(it.get("type") or "?", 0) + 1
    names = {"text": "текст", "photo": "картинки", "document": "файлы"}
    parts = [f"{names.get(k, k)}: {v}" for k, v in counts.items() if k in names]
    return ", ".join(parts) or "пусто"


def authors(items: list[dict]) -> list[str]:
    """Авторы пересланного, в порядке появления и без повторов. «Переслано от
    Ивана» — половина контекста постановки, и в тикете это единственное место,
    где видно, чей это был разговор."""
    names: list[str] = []
    for it in items:
        who = (it.get("from") or "").strip()
        if who and who not in names:
            names.append(who)
    return names


def source_line(items: list[dict]) -> str:
    """Строчка происхождения в конце тикета.

    Её пишем мы, а не сессия: состав пачки и авторы известны точно, а просьба
    пересказать источник заставляла модель придумывать то, что и так лежит в
    данных. Дата не нужна — её показывает сам трекер.
    """
    tail = summary(items)
    who = authors(items)
    if who:
        tail += "; от " + ", ".join(who[:5])
    return f"_Собрано из пересланного в личку бота: {tail}._"


def issue_description(draft: dict, items: list[dict]) -> str:
    """Тело тикета: постановка от сессии плюс то, что известно приёму самому.

    Похожие задачи кладём сюда, а не в карточку: ссылки нужны тому, кто будет
    решать, дубль это или нет, а решает он в трекере. Вопрос — туда же:
    человек отвечает на него правкой описания, а не сообщением в мессенджер.
    """
    parts = [(draft.get("description") or "").strip()]
    similar = draft.get("similar") or []
    if similar:
        lines = ["## Похожие открытые задачи", "",
                 "Найдены при сборке; дубль это или нет — решает человек.", ""]
        for s in similar[:5]:
            ident = s.get("identifier") or "?"
            url = s.get("url") or ""
            title = s.get("title") or ""
            lines.append(f"- [{ident}]({url}) — {title}" if url
                         else f"- {ident} — {title}")
        parts.append("\n".join(lines))
    question = (draft.get("question") or "").strip()
    if question:
        parts.append(f"## Вопрос\n\n{question}")
    parts.append(source_line(items))
    return "\n\n".join(p for p in parts if p)


def intake_notes(draft: dict) -> str:
    """Разбор приёма: что именно он решил про эту постановку.

    Решения приёма уезжают в задачу молча. Роль видна лейблом, приоритет —
    просто стоит, репозиторий лежит строкой в «Допущениях». Ни одно из них не
    подписано «это решил приёмщик», и поэтому ошибку приёма не поймать: задача
    выглядит одинаково и когда роль угадана, и когда она угадана неверно.

    Отдельным комментарием от личности приёмщика её видно сразу — и до того,
    как задачу отдали агенту, то есть пока правка ещё ничего не стоит.

    Чего здесь НЕТ и почему: срока, оценки и риска. Приём их не определяет, а
    напечатать поле, которого он не считал, — это выдать догадку модели за
    решение системы.
    """
    incident = str(draft.get("role") or "").strip().lower() == "incident"
    lines = ["🧭 **Разбор приёма.** Что решил приёмщик, собирая эту задачу:", ""]
    if incident:
        lines += [
            "- **Роль:** инцидент — что-то сломано прямо сейчас, работу "
            "ведёт дежурный, а не рабочая сессия.",
            "- **Приоритет:** Urgent — ставится инциденту автоматически: "
            "инцидент в общей куче ничем от неё не отличается.",
        ]
    else:
        lines += [
            "- **Роль:** обычная работа — задачу возьмёт рабочая сессия.",
            "- **Приоритет:** не ставил — его назначает человек.",
        ]
    repo = str(draft.get("repo") or "").strip()
    lines.append(f"- **Репозиторий:** {repo} (догадка по фактуре)" if repo
                 else "- **Репозиторий:** не определён — назови его до того, "
                      "как отдавать задачу агенту.")
    lines += [
        "",
        "Разобрано неверно — поправь **до** того, как нажмёшь «Отдать "
        "агенту»: роль и приоритет здесь же в трекере, репозиторий — строкой "
        "в «Допущениях». После старта сессия работает по тому, что написано.",
    ]
    return "\n".join(lines)


def essence(draft: dict) -> str:
    """Суть в двух-трёх строках для карточки.

    Сессия кладёт её отдельным полем `summary`. Поля нет — берём первые строки
    описания, пропуская заголовки разделов: карточка без сути хуже, чем
    карточка с началом текста.
    """
    text = (draft.get("summary") or "").strip()
    if text:
        return text
    lines = []
    for raw in (draft.get("description") or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
        if len(lines) == 3:
            break
    return " ".join(lines)


def card_text(issue: dict, draft: dict) -> str:
    """Карточка созданной задачи: номер, суть, пометка о похожих, ссылка.

    Без разметки намеренно. Текст сочинила модель, и одна непарная `*` или `[`
    в нём — это отказ транспорта и молча потерянная карточка на уже созданной
    задаче: человек не узнал бы ни номера, ни ссылки.
    """
    ident = issue.get("identifier") or "?"
    blocks = [f"✅ {ident} — {(draft.get('title') or 'Без названия').strip()}"]
    gist = essence(draft)
    if gist:
        blocks.append(gist)
    similar = draft.get("similar") or []
    if similar:
        idents = ", ".join(s.get("identifier") or "?" for s in similar[:5])
        blocks.append(f"🔎 Похоже на открытые: {idents} — ссылки в задаче.")
    question = (draft.get("question") or "").strip()
    if question:
        blocks.append(f"❓ {question}")
    text = "\n\n".join(blocks)
    if len(text) > CARD_LIMIT:
        text = text[:CARD_LIMIT].rstrip() + "…"
    # Ссылка приписывается ПОСЛЕ обрезки и в неё не входит. Она единственное,
    # что в карточке обязано доехать: без сути человек откроет задачу и
    # прочитает её целиком, а без ссылки он остаётся с номером, который надо
    # искать руками.
    return text + "\n\n" + (issue.get("url") or "")


def issue_keyboard(issue_id: str) -> list[Button]:
    """Кнопки под карточкой заведённой задачи.

    Их ровно две, и обе честные: задача уже в трекере, править её человек идёт
    туда же по ссылке. «Отдать агенту» — исполнитель, с которого начинается
    рабочая сессия; «Удалить» — единственный способ убрать то, что заводить не
    стоило.
    """
    return [Button("🤖 Отдать агенту", f"intake:agent:{issue_id}"),
            Button("🗑 Удалить", f"intake:delete:{issue_id}")]


def reject_reason(draft: dict) -> str:
    return (draft.get("reject") or "").strip()


def reject_text(draft: dict) -> str:
    """Исход «это не постановка»: задачу не создаём вовсе, а говорим, чего не
    хватает. Фактура при этом возвращается в пачку — дописанное человеком
    ляжет к ней, и «Собрать задачу» соберёт уже по полной."""
    return ("🚫 Из этого постановки не выходит.\n\n"
            + reject_reason(draft)
            + "\n\nФактура не потеряна — она снова в пачке. Перешли "
              "недостающее и собери задачу ещё раз.")


# ── постановка в очередь ─────────────────────────────────────────────────

def build_task(source: str, chat_id, items: list[Item], draft_id: str,
               queued_at: str) -> dict:
    """Элемент очереди агента.

    `identifier` нужен раннеру для имени рабочего каталога и журнала —
    задачи в трекере ещё нет, а называть сессию как-то надо.
    """
    return {
        "kind": "intake",
        "source": source,
        "identifier": f"INTAKE-{draft_id}",
        "title": "черновик постановки из личных сообщений",
        "chat_id": chat_id,
        "draft_id": draft_id,
        "items": [i.to_dict() if isinstance(i, Item) else i for i in items],
        "queued_at": queued_at,
    }


@dataclass
class Collected:
    """Итог нажатия «Собрать задачу».

    `element` — ровно та строка, что легла в очередь: по ней счётчик узнаёт,
    ждёт задача слота или её уже забрал раннер. Пересобирать её из `task`
    нельзя — совпадение до байта не гарантировано.
    """
    task: dict
    element: str
    control_mid: str
    items: list[dict]


def item_id(item: dict) -> str:
    """Чем один кусок пачки отличается от другого. Сравнивать словари целиком
    нельзя: тот же кусок возвращается от раннера то с пометкой об ошибке
    скачивания, то без неё."""
    key = f"{item.get('file_id') or ''}|{item.get('message_id') or ''}"
    return key if key != "|" else json.dumps(item, sort_keys=True,
                                             ensure_ascii=False)


def merge_items(incoming: list[dict], known: list[dict]) -> list[dict]:
    """Пачка от раннера + то, что появилось у нас, пока он работал.

    Нужно на отказе: пачку мы вернём человеку, чтобы он дописал недостающее и
    собрал ещё раз, не пересылая переписку заново. Пока заход шёл, он мог
    переслать что-то ещё — без объединения эхо затёрло бы эти куски, и
    ссылки на файлы пропали бы совсем.
    """
    seen = {item_id(it) for it in incoming}
    return list(incoming) + [it for it in known if item_id(it) not in seen]


@dataclass
class Added:
    """Исход добавления куска. `reason` — готовый текст человеку: почему не
    взяли. Молча не брать нельзя — он считает, что фактура доехала."""
    ok: bool
    count: int = 0
    reason: str = ""


def source_of(result: dict) -> str:
    """Откуда пришла постановка — по самой пачке.

    Раннер возвращает `items` как есть, поэтому отдельного поля в ответе не
    нужно. Куски без `source` положены кодом, который про него ещё не знал:
    очередь переживает деплой, и такие элементы отвечают в telegram, как
    раньше.
    """
    for it in (result.get("items") or []):
        src = (it or {}).get("source")
        if src:
            return src
    return "telegram"


# ── протоколы инсталляции ────────────────────────────────────────────────

class Store(Protocol):
    """Хранилище пачки. Реализация — за инсталляцией (redis, что угодно)."""

    async def append(self, key: str, value: str) -> int: ...
    async def read_all(self, key: str) -> list[str]: ...
    async def drop(self, key: str) -> None: ...
    async def get(self, key: str) -> str | None: ...

    async def put(self, key: str, value: str, ttl: int) -> None:
        """`ttl` 0 — без срока. Нужно передаче приёма: её срок живёт полем
        записи и проверяется на чтении, а срок на ключе был бы вторым
        источником правды о том же."""
        ...

    async def claim(self, key: str, ttl: int) -> bool:
        """Захватить право один раз. True — захватили мы."""
        ...
    async def enqueue(self, queue: str, element: str) -> None: ...

    async def expire(self, key: str, ttl: int) -> None:
        """Продлить жизнь пачки. Ставится на каждый кусок: пачка живёт от
        последнего сообщения, а не от первого."""
        ...

    async def position(self, queue: str, element: str) -> int | None:
        """Где элемент в очереди, если он ещё там. Нужен счётчику сборки:
        видно элемент — задача ждёт слота, пропал — её уже забрали."""
        ...

    async def pop_blocking(self, key: str, timeout: int) -> str | None:
        """Дождаться элемента списка. Нужен разбору результатов, который
        ждёт готовую постановку минутами."""
        ...


class Markup(Protocol):
    """Как этот мессенджер пишет жирное, ссылку и упоминание человека.

    Нужна ленте задач: там надо позвать исполнителя по имени так, чтобы ему
    прилетело уведомление. У каждого мессенджера это своя запись, а у
    некоторых её нет вовсе — тогда остаётся имя текстом, и это рабочий исход,
    а не ошибка.
    """

    def safe(self, text: str) -> str:
        """Обезвредить чужой текст: заголовок задачи пишет человек, и один
        активный символ в нём — это отказ транспорта и потерянное сообщение."""
        ...

    def bold(self, text: str) -> str: ...
    def link(self, text: str, url: str) -> str: ...
    def mention(self, chat_id, name: str) -> str: ...


class Transport(Protocol):
    """Мессенджер. Реализация — за инсталляцией."""

    # Как писать разметку. Лента задач берёт её отсюда, чтобы не знать про
    # конкретный мессенджер.
    markup: Markup

    async def send(self, chat_id, text: str,
                   buttons: list[Button] | None = None,
                   rich: bool = False) -> str:
        """`rich` — текст уже размечен через `markup` и его надо отдать
        мессенджеру как разметку. По умолчанию нет: постановку и суть
        карточки сочиняет модель, и одна непарная скобка в них — это отказ
        транспорта и молча потерянное сообщение."""
        ...

    async def edit(self, chat_id, message_id: str, text: str,
                   buttons: list[Button] | None = None,
                   rich: bool = False) -> bool: ...
    async def answer(self, callback_id: str, notice: str = "") -> None: ...

    async def delete(self, chat_id, message_id: str) -> bool:
        """Убрать сообщение. Нужно счётчику пачки: он не правится, а
        пересоздаётся внизу ленты (см. Intake.repaint).

        False — транспорт этого не умеет или не смог. Тогда счётчик правится
        на месте: кнопка останется выше по ленте, но дублей не будет.
        """
        ...

    def retry_after(self) -> float:
        """Сколько секунд мессенджер просил подождать после «слишком часто».
        0 — не просил.

        Метод НЕОБЯЗАТЕЛЬНЫЙ, как `merged_request` у хостинга: у мессенджера
        может не быть ни такого отказа, ни способа о нём сказать. Нет метода
        — читающий считает, что ждать не просили, и работает как раньше.

        Заведён 17.09.2026: телеграм дважды за сутки закрыл дверь боту, и
        единственное место, где сказано, сколько ждать, — тело его отказа.
        Продолжать стучаться в закрытую дверь дороже, чем промолчать: пауза
        продлевается на каждую попытку.
        """
        ...


class Intake:
    """Приём постановок поверх любого транспорта и любого хранилища."""

    def __init__(self, source: str, store: Store, transport: Transport,
                 queue_key: str, allowed: set[str] | None = None,
                 max_items: int = MAX_ITEMS,
                 max_file_bytes: int = MAX_FILE_BYTES, ttl_sec: int = TTL_SEC):
        self.source = source
        self.store = store
        self.transport = transport
        self.queue_key = queue_key
        self.allowed = allowed or set()
        self.max_items = max_items
        self.max_file_bytes = max_file_bytes
        self.ttl_sec = ttl_sec
        # Замок на чат: два сообщения подряд иначе оба пересоздают счётчик, и
        # в ленте остаётся осиротевшая кнопка над той же пачкой.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, chat_id) -> asyncio.Lock:
        return self._locks.setdefault(str(chat_id), asyncio.Lock())

    async def add(self, chat_id, item: Item) -> Added:
        """Кусок в пачку и перерисовка счётчика. Отказ — с объяснением."""
        if item.type == "unsupported":
            return Added(False, reason=f"⚠️ Не взял в пачку: {item.reason}.")
        if item.file_size and item.file_size > self.max_file_bytes:
            mb = item.file_size / (1024 * 1024)
            return Added(False, reason=(
                f"⚠️ «{item.file_name or 'файл'}» — {mb:.0f} МБ. Больше "
                f"{self.max_file_bytes // (1024 * 1024)} МБ бот скачать не "
                f"может: пришли скриншот нужного места или файл поменьше."))
        key = batch_key(self.source, chat_id)
        async with self._lock(chat_id):
            raw = await self.store.read_all(key)
            if len(raw) >= self.max_items:
                return Added(False, count=len(raw), reason=(
                    f"⚠️ В пачке уже {self.max_items} кусков — собери задачу "
                    f"или очисти."))
            await self.store.append(key, json.dumps(item.to_dict(),
                                                    ensure_ascii=False))
            await self.store.expire(key, self.ttl_sec)
            items = [json.loads(x) for x in raw] + [item.to_dict()]
            await self.repaint(chat_id, items)
        return Added(True, count=len(items))

    async def repaint(self, chat_id, items: list[dict]) -> None:
        """Пересобрать сообщение с кнопкой «Собрать задачу» внизу ленты.

        Именно пересобрать, а не отредактировать: мессенджер не поднимает
        отредактированное сообщение вниз, и после пяти пересланных подряд
        кнопка оказывалась бы вверху, куда надо доскроллить. Пачка при этом
        лежит в хранилище, а не в сообщении, — потерянная кнопка ничего не
        теряет.
        """
        key = control_key(self.source, chat_id)
        old = await self.store.get(key)
        text, btns = control_text(items), collect_keyboard(chat_id)
        log.info("счётчик %s: было %s, кусков %d", self.source, old or "нет",
                 len(items))
        if old and not await self.transport.delete(chat_id, old):
            # Не удалилось по двум разным причинам, и вести себя надо
            # по-разному.
            #
            # Транспорт удалять НЕ УМЕЕТ (в MAX такого метода нет) — правим на
            # месте. Кнопка останется выше по ленте, зато дублей не будет:
            # слать новое, не убрав старое, — ровно тот случай, из-за которого
            # человек получал три счётчика с разными числами.
            #
            # Сообщения УЖЕ НЕТ (удалили руками, оно старше суток — Telegram
            # такие удалять не даёт) — правка тоже не пройдёт, и выходить здесь
            # значит не показать счётчик вовсе. 12.09.2026 так и было: мёртвая
            # ссылка в памяти глушила ответ на КАЖДЫЙ следующий кусок, человек
            # прислал шесть сообщений и не увидел ничего.
            if await self.transport.edit(chat_id, old, text, btns):
                return
            # Ссылку не чистим: следующей строкой уходит новый счётчик и
            # перезапишет её. Отдельный `drop` тут был бы мёртвым кодом.
            log.info("счётчик %s: %s не удалить и не поправить — шлю новый",
                     self.source, old)
        new = await self.transport.send(chat_id, text, btns)
        if new:
            log.info("счётчик %s: новый %s", self.source, new)
            await self.store.put(key, new, self.ttl_sec)
        else:
            # Сообщение не ушло — забываем прежнее: править нечего, а держать
            # ссылку на удалённое значит удалять её второй раз на следующем
            # куске.
            await self.store.drop(key)

    async def restore(self, chat_id, items: list[dict]) -> int:
        """Вернуть фактуру в пачку — заход унёс её с собой, а задача не
        завелась.

        Куски, пришедшие пока шёл заход, уже лежат в пачке: их не затираем, а
        дописываем следом. Возвращает, сколько не влезло в потолок. Обычно
        ноль; когда не ноль — про это надо сказать вслух, иначе обещание
        «фактура не потеряна» оказывается враньём ровно в тот момент, когда
        её больше всего.
        """
        if not items:
            return 0
        key = batch_key(self.source, chat_id)
        async with self._lock(chat_id):
            raw = await self.store.read_all(key)
            merged = merge_items(items, [json.loads(x) for x in raw])
            # Режем хвост, а не голову: вернувшаяся фактура — предмет
            # разговора, а хвост это то, что человек дослал только что и
            # помнит.
            dropped = max(0, len(merged) - self.max_items)
            merged = merged[:self.max_items]
            await self.store.drop(key)
            for it in merged:
                await self.store.append(key, json.dumps(it, ensure_ascii=False))
            await self.store.expire(key, self.ttl_sec)
            await self.repaint(chat_id, merged)
        return dropped

    async def collect(self, chat_id, draft_id: str,
                      queued_at: str) -> Collected | None:
        """Пачка → задача в очередь. None — собирать было нечего."""
        key = batch_key(self.source, chat_id)
        async with self._lock(chat_id):
            raw = await self.store.read_all(key)
            if not raw:
                return None
            items = [json.loads(x) for x in raw]
            task = build_task(self.source, chat_id, items, draft_id, queued_at)
            element = json.dumps(task, ensure_ascii=False)
            # Пачку забираем ТОЛЬКО после успешной постановки: сбой очереди
            # иначе стоил бы человеку всей пересланной фактуры.
            await self.store.enqueue(self.queue_key, element)
            await self.store.drop(key)
            # Кнопки снимаем с того же сообщения, где их нажали, и отдаём его
            # вызывающему: счётчик сборки тикает в нём же, а не шлёт своё.
            mid = await self._close_control(chat_id, collect_head(items))
        return Collected(task=task, element=element, control_mid=mid,
                         items=items)

    async def reset(self, chat_id) -> None:
        async with self._lock(chat_id):
            await self.store.drop(batch_key(self.source, chat_id))
            await self._close_control(chat_id, "Пачка сброшена.")

    async def _close_control(self, chat_id, text: str) -> str:
        """Погасить сообщение-счётчик: текст об исходе и никаких кнопок.

        Забыть его мало. Забытое сообщение остаётся в ленте с живой кнопкой
        «Собрать задачу», и следующий тап по ней приходит на пачку, которой
        уже нет.
        """
        key = control_key(self.source, chat_id)
        mid = await self.store.get(key)
        await self.store.drop(key)
        if mid:
            await self.transport.edit(chat_id, mid, text, None)
        return mid or ""
