"""Кто принимает работу агента: владелец или тот, кому он передал приём.

Часть ПРОДУКТА. До 12.09.2026 делегирование жило у первого клиента, а в
продукте была только половина контракта — раннер читал ключ `agent:reviewer`
и умел разбирать запись. Писать её было нечем: у второй инсталляции «я
недоступен на неделю» означало править файл окружения и перезапускать юнит.

Владелец задан окружением и меняется только с рестартом. Этого мало: «меня
нет» — состояние на день, а не на релиз. Поэтому поверх окружения лежит
запись в хранилище инсталляции, которую владелец ставит командой `/reviewer`
в личке бота. Окружение остаётся значением по умолчанию: нет записи, лежит
хранилище, протух срок — работу принимает владелец. Отказ всегда в сторону
владельца, потому что потерявшаяся задача хуже лишнего пинга.

Запись читают двое — приём (лента, эта команда) и раннер (`current_reviewer`
в runner.py). Пакеты деплоятся раздельно и импортировать друг друга не могут,
поэтому разбор записи продублирован, а совпадение закреплено контрактным
тестом в core/test_reviewer.py: ключ, имена полей и правило «срок вышел —
записи нет» обязаны совпасть до буквы.

Срок проверяется НА ЧТЕНИИ, а не по таймеру: обе стороны и так спрашивают
принимающего на каждом событии, и «само вернулось к владельцу» не требует ни
планировщика, ни живого процесса в момент истечения.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .people import People

log = logging.getLogger("intake")

# Умолчания — те же, что у раннера (AGENT_REVIEWER_KEY / AGENT_REVIEWER_TZ):
# одно имя, одно значение, два потребителя. Инсталляции, живущие в одном
# redis, разводят ключ суффиксом — как очередь.
DEFAULT_KEY = "agent:reviewer"
DEFAULT_TZ = "Europe/Moscow"

# Единицы срока. «м» отдельно нет намеренно: «на 2 м» это и минуты, и месяцы,
# и угадывать тут нельзя — лучше честно не понять.
_DURATION_UNITS = (
    (("мин", "минут", "минуты", "минуту"), 60),
    (("ч", "час", "часа", "часов"), 3600),
    (("д", "дн", "день", "дня", "дней", "сут", "сутки", "суток"), 86400),
    (("нед", "неделя", "неделю", "недели", "недель"), 7 * 86400),
)

# «до завтра» без времени. Любое значение тут — догадка, поэтому бот всегда
# печатает в ответ разобранный момент целиком, и ошибку видно сразу.
_DEFAULT_DEADLINE_HOUR = 10

_RESET_WORDS = ("сброс", "reset", "я", "себе", "me", "-")

# Ответы в личку уходят без разметки (Transport.send по умолчанию), поэтому
# тексты ниже простые: звёздочка тут была бы видна как звёздочка.
HELP = (
    "Кто принимает работу агента:\n"
    "/reviewer — кто сейчас\n"
    "/reviewer Иван — передать бессрочно\n"
    "/reviewer Иван на неделю — на срок\n"
    "/reviewer Иван до завтра 10:00 — до момента\n"
    "/reviewer сброс — вернуть себе\n\n"
    "Срок: «на 3 часа» / «на 2 дня» / «на неделю» / «до 18:00» / "
    "«до завтра 10:00» / «до 2026-09-05 12:00». Без времени «до завтра» "
    "значит 10:00."
)


# ── срок ─────────────────────────────────────────────────────────────────

def local_tz(name: str):
    """Пояс, в котором человек называет время. Нет tzdata на хосте — UTC:
    промахнуться на три часа лучше, чем уронить команду исключением."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        log.warning("нет пояса %s, срок считаю в UTC", name)
        return timezone.utc


def parse_deadline(text: str, now: datetime, tz=timezone.utc):
    """Срок делегирования → (момент в UTC | None, ошибка | None).

    None без ошибки — бессрочно (пока не вернут вручную). Чистая функция: `now`
    и пояс приходят аргументами, поэтому проверяется без часов и без сети.
    """
    t = (text or "").strip().lower().replace("ё", "е")
    if not t or t in ("навсегда", "бессрочно", "без срока", "насовсем"):
        return None, None

    m = re.fullmatch(r"(?:на\s+)?(\d{1,4})\s*([а-я]+)\.?", t)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        for names, seconds in _DURATION_UNITS:
            if unit in names:
                if amount < 1:
                    return None, "срок должен быть больше нуля"
                return now + timedelta(seconds=amount * seconds), None
        return None, f"не понял единицу времени «{unit}»"
    # «на неделю» / «на день» без числа.
    m = re.fullmatch(r"на\s+([а-я]+)", t)
    if m:
        for names, seconds in _DURATION_UNITS:
            if m.group(1) in names:
                return now + timedelta(seconds=seconds), None
        return None, f"не понял срок «{t}»"

    m = re.fullmatch(r"до\s+(.+)", t)
    if not m:
        return None, f"не понял срок «{t}»"
    rest = m.group(1).strip()
    local = now.astimezone(tz)

    md = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[\sT]+(\d{1,2}):(\d{2}))?", rest)
    if md:
        y, mo, d = int(md.group(1)), int(md.group(2)), int(md.group(3))
        hh = int(md.group(4)) if md.group(4) else _DEFAULT_DEADLINE_HOUR
        mm = int(md.group(5)) if md.group(5) else 0
        try:
            when = datetime(y, mo, d, hh, mm, tzinfo=tz)
        except ValueError:
            return None, f"такой даты нет: «{rest}»"
        return _in_future(when, now)

    mw = re.fullmatch(r"(?:(завтра|сегодня)\s*)?(?:(\d{1,2})[:.](\d{2}))?", rest)
    if not mw or not (mw.group(1) or mw.group(2)):
        return None, f"не понял срок «{t}»"
    day = local.date() + (timedelta(days=1) if mw.group(1) == "завтра" else timedelta())
    hh = int(mw.group(2)) if mw.group(2) else _DEFAULT_DEADLINE_HOUR
    mm = int(mw.group(3)) if mw.group(3) else 0
    if hh > 23 or mm > 59:
        return None, f"такого времени нет: «{rest}»"
    when = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
    if mw.group(1) is None and when <= now:
        # «до 9:00», сказанное в полдень, — это завтрашние 9:00, а не отказ.
        when += timedelta(days=1)
    return _in_future(when, now)


def _in_future(when: datetime, now: datetime):
    if when <= now:
        return None, "этот момент уже прошёл"
    return when.astimezone(timezone.utc), None


def fmt_moment(iso_utc: str, tz_name: str) -> str:
    """ISO в UTC → «02.09 10:00 Europe/Moscow (07:00 UTC)». Оба времени
    намеренно: срок называют по-местному, а хранится и сравнивается он в UTC."""
    try:
        when = datetime.fromisoformat(iso_utc)
    except ValueError:
        return iso_utc
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(local_tz(tz_name))
    return (f"{local:%d.%m %H:%M} {tz_name} "
            f"({when.astimezone(timezone.utc):%H:%M} UTC)")


# ── запись ───────────────────────────────────────────────────────────────

def record_active(raw: str | None, now: datetime) -> dict | None:
    """Запись из хранилища → делегат, либо None (нет / битая / просрочена).

    Тот же разбор, что `reviewer_from_record` в runner.py, — см. шапку модуля
    о том, почему он продублирован. Битую запись трактуем как её отсутствие:
    делегирование, которое нельзя разобрать, не повод оставить задачу ни на
    ком.
    """
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(rec, dict) or not rec.get("linear_user_id"):
        return None
    until = rec.get("until") or ""
    if until:
        try:
            deadline = datetime.fromisoformat(until)
        except ValueError:
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= now:
            return None
    return rec


def format_status(rec: dict | None, owner_name: str, tz_name: str) -> str:
    """Строка «кто принимает» для ответа бота."""
    owner = owner_name or "владелец"
    if not rec:
        return f"👤 Работу принимает {owner} (владелец)."
    who = rec.get("name") or "делегат"
    if rec.get("until"):
        return (f"👤 Работу принимает {who} — до {fmt_moment(rec['until'], tz_name)}, "
                f"дальше снова {owner}.")
    return (f"👤 Работу принимает {who} — бессрочно. "
            f"Вернуть себе: /reviewer сброс")


# ── поиск человека ───────────────────────────────────────────────────────

def _norm(text) -> str:
    return (text or "").strip().lstrip("@").lower().replace("ё", "е")


def pick_user(users: list[dict], text: str) -> tuple[dict | None, list[dict]]:
    """«Иван» → человек в трекере. Возвращает (найденный | None, кандидаты).

    Сначала точное совпадение с именем или отображаемым именем, потом по
    началу любого слова: «Пав» найдёт Ивана, но «Па» при Иване и Паше даст
    обоих — и тогда лучше переспросить, чем угадать. Деактивированных не
    предлагаем: передать приём тому, кто ушёл, — потерянная работа.
    """
    q = _norm(text)
    if not q:
        return None, []
    alive = [u for u in users if u.get("active", True)]
    exact = [u for u in alive
             if q in {_norm(u.get("name")), _norm(u.get("displayName"))}]
    if len(exact) == 1:
        return exact[0], []
    if exact:
        return None, exact
    partial = [u for u in alive if any(
        w.startswith(q)
        for w in (_norm(u.get("name")).split() + [_norm(u.get("displayName"))]))]
    if len(partial) == 1:
        return partial[0], []
    return None, partial


# ── принимающий ──────────────────────────────────────────────────────────

class Reviewer:
    """Кто принимает работу прямо сейчас. Реализует `issue_feed.Reviewer`.

    Владелец — из карты людей (`People`): его id в трекере и его личка
    берутся оттуда же, откуда лента берёт адресатов. Отдельной переменной
    под личку владельца здесь нет: ещё один источник правды о тех же людях
    расходился бы с картой молча.
    """

    def __init__(self, store, people: People, transport=None, tracker=None,
                 key: str = DEFAULT_KEY, tz_name: str = DEFAULT_TZ,
                 owner_name: str = "", now=None):
        self.store = store
        self.people = people
        # Транспорт и трекер нужны только команде: ответить владельцу, найти
        # делегата по имени, предупредить его в личке. Ленте хватает
        # хранилища и карты.
        self.transport = transport
        self.tracker = tracker
        self.key = key or DEFAULT_KEY
        self.tz_name = tz_name or DEFAULT_TZ
        self.owner_name = owner_name or ""
        # Часы подменяемы: срок — это сравнение с «сейчас», и проверять его
        # надо без ожидания и без реальных дат.
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ── чтение ───────────────────────────────────────────────────────────

    async def record(self) -> dict | None:
        """Действующее делегирование или None. Просроченное заодно убираем —
        иначе `/reviewer` показывал бы делегата, к которому работа уже не
        едет, а раннер и приём расходились бы в том, кто принимает."""
        try:
            raw = await self.store.get(self.key)
        except Exception as e:
            log.warning("не спросить делегирование: %s", e)
            return None
        rec = record_active(raw, self._now())
        if rec is None and raw:
            try:
                await self.store.drop(self.key)
            except Exception as e:
                log.warning("не убрать просроченное делегирование: %s", e)
        return rec

    async def user_id(self) -> str:
        """Id принимающего в трекере. Пусто — владельца не задали."""
        rec = await self.record()
        if rec and rec.get("linear_user_id"):
            return str(rec["linear_user_id"])
        return self.people.owner_user_id

    async def chat_id(self) -> str:
        """Личка принимающего. Пусто — не знаем, куда писать."""
        rec = await self.record()
        if rec and rec.get("telegram_id"):
            return str(rec["telegram_id"])
        return self.people.chat_of(self.people.owner_user_id)

    # ── запись ───────────────────────────────────────────────────────────

    async def handover(self, user_id: str, name: str, chat_id, until: datetime | None,
                       set_by="") -> dict:
        """Передать приём. Возвращает записанное — оно же уезжает раннеру.

        Имена полей — контракт с раннером (`reviewer_from_record`), менять их
        порознь нельзя. `telegram_id` так и зовётся: раннер читает его под
        этим именем, а личка в другом мессенджере кладётся туда же.
        """
        now = self._now()
        record = {
            "linear_user_id": str(user_id),
            "name": name or "",
            "telegram_id": str(chat_id),
            "until": until.astimezone(timezone.utc).isoformat() if until else "",
            "set_by": str(set_by or ""),
            "set_at": now.isoformat(),
        }
        # Без срока жизни ключа: срок живёт полем `until` и проверяется обеими
        # сторонами на чтении. TTL давал бы второй, независимый источник
        # правды о том же — и расхождение между ними никто бы не заметил.
        await self.store.put(self.key, json.dumps(record, ensure_ascii=False), 0)
        return record

    async def reset(self) -> None:
        await self.store.drop(self.key)

    # ── команда ──────────────────────────────────────────────────────────

    async def _say(self, chat_id, text: str) -> None:
        if self.transport is None:
            log.warning("ответить некуда: у принимающего нет транспорта")
            return
        await self.transport.send(chat_id, text)

    def _status(self, rec: dict | None) -> str:
        return format_status(rec, self.owner_name, self.tz_name)

    async def command(self, chat_id, args: str) -> str:
        """`/reviewer [кто [срок]]`. Возвращает исход — для журнала и тестов.

        Что это владелец, проверяет роутер команд ДО вызова: правило одно на
        все команды инсталляции, и держать его здесь второй копией значило бы
        иметь два места, где оно может разойтись.
        """
        args = (args or "").strip()
        rec = await self.record()
        if not args:
            await self._say(chat_id, self._status(rec) + "\n\n" + HELP)
            return "show"

        if args.lower() in _RESET_WORDS:
            try:
                await self.reset()
            except Exception as e:
                await self._say(chat_id, f"⚠️ Не смог убрать делегирование: {e}.")
                return "store"
            await self._say(chat_id, f"✅ Работу снова принимает "
                                     f"{self.owner_name or 'владелец'}.")
            return "reset"

        # «Иван на неделю» → кто = «Иван», срок = «на неделю». Разрез по
        # первому «на»/«до» с пробелом после: срок без них не задаётся, а имя
        # с них может начинаться («Настя»), но пробела внутри не имеет. Срок
        # без имени («на неделю») даёт пустого «кого» — и честный отказ, а не
        # поиск человека по имени «на неделю».
        m = re.match(r"^(.*?)\s*(?:((?:на|до)\s+.*))?$", args, re.S)
        who_text = (m.group(1) or "").strip()
        deadline_text = (m.group(2) or "").strip()
        if not who_text:
            await self._say(chat_id, "⚠️ Не понял, кому передать. " + HELP)
            return "no-name"

        now = self._now()
        until, err = parse_deadline(deadline_text, now, local_tz(self.tz_name))
        if err:
            await self._say(chat_id, f"⚠️ {err}.\n\n{HELP}")
            return "deadline"

        ask = getattr(self.tracker, "users", None)
        if not ask:
            await self._say(chat_id, "⚠️ Трекер не настроен — некого искать.")
            return "no-tracker"
        try:
            users = await asyncio.to_thread(ask)
        except Exception as e:
            await self._say(chat_id, f"⚠️ Не смог спросить трекер: {e}.")
            return "tracker"
        user, candidates = pick_user(users, who_text)
        if user is None:
            if candidates:
                names = ", ".join(u.get("name") or "?" for u in candidates[:5])
                await self._say(chat_id, f"⚠️ Их несколько: {names}. Уточни.")
                return "ambiguous"
            await self._say(chat_id, f"⚠️ Не нашёл в трекере «{who_text}».")
            return "not-found"

        # Делегат ОБЯЗАН быть в карте людей. Иначе сдача до него не доедет
        # вовсе: лента не пишет незнакомым, и сообщения о сдаче не будет ни в
        # личке, ни в общем чате.
        chat = self.people.chat_of(user["id"])
        if not chat:
            await self._say(chat_id, (
                f"⚠️ {user.get('name') or ''} не заведён в карте людей "
                f"(AGENT_PEOPLE_MAP) — сдача до него не дойдёт. Сначала "
                f"добавь его туда."))
            return "not-in-map"

        name = user.get("name") or user.get("displayName") or who_text
        try:
            record = await self.handover(user["id"], name, chat, until, set_by=chat_id)
        except Exception as e:
            await self._say(chat_id, f"⚠️ Не смог записать делегирование: {e}.")
            return "store"

        await self._say(chat_id, self._status(record))
        # Делегату — в личку, иначе он узнает о новой обязанности только когда
        # получит первую сданную работу и не поймёт, почему.
        await self._say(chat, (
            f"👤 {self.owner_name or 'Владелец'} передал тебе приём работы агента"
            + (f" — до {fmt_moment(record['until'], self.tz_name)}."
               if record["until"] else " — бессрочно.")
            + "\nСданная агентом работа теперь приходит сюда."))
        return "set"
