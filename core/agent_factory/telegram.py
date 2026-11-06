"""Приём постановок из Telegram: реализация протоколов на long polling.

Часть ПРОДУКТА. Модель, ключи и кнопки берутся из `intake.py` — здесь только
то, что специфично для транспорта: как поговорить с Telegram и как хранить
пачку в redis.

Почему long polling, а не вебхук: речь о МЕССЕНДЖЕРЕ, и `getUpdates`
работает откуда угодно — инсталляция не обязана заводить домен ради того,
чтобы принимать постановки. События ТРЕКЕРА приезжают иначе: их приносит
вебхук (`webhook.py`), он поднимается в этом же процессе.

Запускается как отдельный процесс рядом с раннером: у них разный жизненный
цикл. Раннер перезапускают при обновлении ядра, приём — нет; и наоборот,
залипший приём не должен ронять идущие сессии.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import redis.asyncio as aioredis
from redis.exceptions import TimeoutError as RedisTimeoutError

from .archive import Archiver, days_from
from .assignments import Assignments
from .canary import Canary, Stages
from .channel import Channel
from .commands import Unblock
from .deploy import (CANCEL_STATE_VAR, DEPLOY_SIGNATURE_HEADER, DONE_STATE_VAR,
                     DeployReceiver, Verification)
from .gitref import Repos
from .intake import Button, Item, is_allowed, seen_key
from .tracking import LinearTracker
from .signing import SigningPage
from . import runs
from .verdicts import Verdicts
from .issue_feed import IssueFeed
from .people import People
from .puller import Puller
from .reconcile import Reconciler, Stalled
from .reviewer import DEFAULT_KEY, DEFAULT_TZ, Reviewer
from . import signoff
from .updater import Updater, source_from_env
from .webhook import Receiver, Server

log = logging.getLogger("intake")

API = "https://api.telegram.org"


# ── транспорт ────────────────────────────────────────────────────────────

# Активные символы legacy-Markdown. Экранирование в нём ненадёжно, поэтому
# из чужого текста их вырезаем: непарная `*` или `[` в заголовке задачи — это
# 400 «can't parse entities» и молча потерянное сообщение.
MD_ACTIVE = re.compile(r"[_*`\[\]]")

# Приставка переменной с ключом личности. Та же, что читает раннер
# (`core/personas.py`): пакеты деплоятся раздельно и импортировать друг друга
# не могут, поэтому совпадение закреплено контрактным тестом.
PERSONA_KEY_PREFIX = "LINEAR_API_KEY_"


def _persona_keys_from_env(env=None) -> dict[str, str]:
    """Ключи личностей из окружения: `LINEAR_API_KEY_DUTY` → `duty`.

    Перебираем окружение, а не список личностей: список живёт в другом пакете,
    и копия здесь разъехалась бы с ним ровно так же, как разъезжались копии
    всего остального.

    Отсюда сторож видит БОЛЬШЕ, чем знает раннер: переменная с этой приставкой,
    которой нет в списке личностей, всё равно попадёт под присмотр. Это
    намеренно. Такая переменная — либо личность, о которой раннер не знает
    (расхождение, и его надо увидеть), либо опечатка в имени (её тоже надо
    увидеть). Молча игнорировать оба случая — значит воспроизвести ту самую
    поломку, ради которой сторож и заведён.
    """
    env = os.environ if env is None else env
    out = {}
    for name, value in env.items():
        if not name.startswith(PERSONA_KEY_PREFIX) or name == "LINEAR_API_KEY":
            continue
        persona = name[len(PERSONA_KEY_PREFIX):].lower()
        if persona and (value or "").strip():
            out[persona] = value.strip()
    return out


def dead_personas(keys: dict[str, str], whoami) -> list[str]:
    """Какие личности перестали отвечать. `whoami(key)` → имя или исключение.

    13.09.2026 четыре личности из пяти отвалились разом — приложениям отозвали
    авторизацию, — и фабрика СУТКИ писала комментарии в пустоту: ключ в
    окружении лежит, трекер отвечает 401, комментарий не создаётся. Со стороны
    неотличимо от «сессия ничего не сделала»; узнали случайно.

    Проверять надо ЗАПРОСОМ: отозванный токен выглядит ровно как живой, и
    «ключ непустой» здесь не проверка.

    Возвращаем список, а не бросаем: одна отвалившаяся личность не повод
    бросать проверку остальных — иначе сторож слепнет на первой же.
    """
    dead = []
    for persona in sorted(keys):
        try:
            if not whoami(keys[persona]):
                dead.append(persona)
        except (AttributeError, TypeError, NameError):
            # Это не «личность умерла», это сломан сам сторож. Записав такое в
            # мёртвые, он врёт: 14.09.2026 опечатка в имени поля объявила
            # мёртвыми ВСЕХ пятерых и подняла тревогу в чат за 4 мс — раньше,
            # чем успел бы уйти хоть один запрос. Пусть падает наверх: там
            # честное «сторож не отработал», а не ложная тревога.
            raise
        except Exception:
            dead.append(persona)
    return dead



class TelegramMarkup:
    """Разметка Telegram в её legacy-Markdown виде.

    Упоминание через `tg://user?id=` — единственный способ позвать человека,
    которого может не быть в этом чате по @имени.
    """

    def safe(self, text: str) -> str:
        return MD_ACTIVE.sub("", text or "")

    def bold(self, text: str) -> str:
        return f"*{text}*"

    def link(self, text: str, url: str) -> str:
        return f"[{text}]({url})"

    def mention(self, chat_id, name: str) -> str:
        return f"[{name}](tg://user?id={chat_id})"


class TelegramTransport:
    """Отправка, правка и ответ на нажатие.

    Синхронные вызовы уводятся в поток: сеть не должна блокировать цикл,
    который в это же время читает следующие обновления.
    """

    def __init__(self, token: str):
        self.token = token
        self.markup = TelegramMarkup()
        # До какого момента телеграм просил не стучаться. Монотонные часы:
        # системные могут прыгнуть, и тогда пауза либо исчезнет, либо
        # застынет навсегда.
        self._pause_until = 0.0

    # Телеграм отвечает 429, когда правок слишком много, и в теле пишет,
    # сколько ждать (`parameters.retry_after`). До 17.09.2026 это никто не
    # читал: раздутая карта сообщений задачи гнала тысячи правок подряд, бот
    # получал 429 и продолжал стучаться — за сутки 1101 вызов и 1103 отказа,
    # то есть не прошло НИЧЕГО, включая ответы на нажатия кнопок. Дважды за
    # сутки дверь закрывалась на минуты.
    TOO_MANY = 429

    # Сколько ждать, если телеграм отказал, но срока не назвал. Ноль здесь
    # значил бы «паузы не было», то есть ту же долбёжку.
    DEFAULT_RETRY_SEC = 30.0

    def retry_after(self) -> float:
        """Сколько секунд телеграм просил подождать. 0 — не просил.

        Читают двое: сам транспорт — чтобы не ходить в сеть, пока просили
        ждать, — и лента задач, чтобы остановить перерисовку, а не долбить
        дальше. Метод необязательный по протоколу: у мессенджера может не
        быть ни такого отказа, ни способа о нём сказать.
        """
        return max(0.0, self._pause_until - time.monotonic())

    @staticmethod
    def _retry_seconds(body: dict, headers) -> float:
        """Сколько просят ждать: сперва тело телеграма, потом заголовок.

        Тело точнее — `parameters.retry_after` кладёт сам телеграм; заголовок
        `Retry-After` может поставить прокси перед ним.
        """
        for value in (((body or {}).get("parameters") or {}).get("retry_after"),
                      (headers or {}).get("Retry-After") if headers else None):
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                return seconds
        return 0.0

    # Правка сообщения тем же текстом — не отказ. Telegram отвечает на неё
    # 400, и до 14.09.2026 это писалось в журнал как ошибка: 64 строки за
    # неделю, 42 из них за один день, и все — на штатной перерисовке
    # карточки, которая просто не изменилась. Настоящие 400 (сообщение
    # удалено, чат недоступен, разметка не разбирается) тонули в этом шуме.
    NOT_MODIFIED = "message is not modified"

    def _call(self, method: str, payload: dict) -> dict:
        left = self.retry_after()
        if left:
            # Просили подождать — ждём. Вызов, отправленный внутри паузы,
            # её же и продлевает: телеграм считает попытки, а не удачи.
            log.warning("telegram %s: ждём ещё %.0f с по просьбе телеграма — "
                        "не шлю", method, left)
            return {}
        req = urllib.request.Request(
            f"{API}/bot{self.token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # Тело ответа Telegram несёт `description` — ЕДИНСТВЕННОЕ место,
            # где написано, что именно не так. Без него в журнале оставался
            # голый «HTTP Error 400: Bad Request», по которому нельзя ни
            # починить, ни отличить безобидное от важного.
            body = {}
            try:
                body = json.loads(e.read()) or {}
            except Exception:
                pass
            why = body.get("description", "") if isinstance(body, dict) else ""
            if getattr(e, "code", 0) == self.TOO_MANY:
                wait = (self._retry_seconds(body, getattr(e, "headers", None))
                        or self.DEFAULT_RETRY_SEC)
                self._pause_until = time.monotonic() + wait
                log.warning("telegram %s: слишком часто, дверь закрыта на "
                            "%.0f с%s", method, wait, f" — {why}" if why else "")
                return {}
            if self.NOT_MODIFIED in why:
                # Текст на месте, значит правка достигнута. Возвращаем успех:
                # вызывающий считает правки, и «не изменилось» — это правка,
                # а не потерянное сообщение.
                return {"ok": True, "result": {}, "not_modified": True}
            log.warning("telegram %s: %s%s", method, e,
                        f" — {why}" if why else "")
            return {}
        except Exception as e:
            log.warning("telegram %s: %s", method, e)
            return {}

    @staticmethod
    def _markup(buttons: list[Button] | None) -> dict:
        # Пустой список, а не отсутствие поля: так клавиатура СНИМАЕТСЯ.
        # Без этого под разобранной пачкой остаётся живая кнопка «Собрать
        # задачу» — второй заход на те же куски.
        rows = [[{"text": b.text, "callback_data": b.payload}] for b in (buttons or [])]
        return {"inline_keyboard": rows}

    @staticmethod
    def _mode(rich: bool) -> dict:
        """Разметку включает вызывающий. По умолчанию её нет: текст пачки и
        карточки сочиняет модель, и разбирать его как Markdown значит менять
        доставку сообщения на курсив в половине случаев."""
        return {"parse_mode": "Markdown"} if rich else {}

    async def send(self, chat_id, text: str, buttons=None,
                   rich: bool = False) -> str:
        r = await asyncio.to_thread(self._call, "sendMessage", {
            "chat_id": chat_id, "text": text,
            "disable_web_page_preview": True,
            "reply_markup": self._markup(buttons), **self._mode(rich)})
        return str(((r or {}).get("result") or {}).get("message_id") or "")

    async def edit(self, chat_id, message_id: str, text: str, buttons=None,
                   rich: bool = False) -> bool:
        if not message_id:
            log.warning("правка без message_id — не шлю")
            return False
        r = await asyncio.to_thread(self._call, "editMessageText", {
            "chat_id": chat_id, "message_id": int(message_id), "text": text,
            "disable_web_page_preview": True,
            "reply_markup": self._markup(buttons), **self._mode(rich)})
        return bool((r or {}).get("ok"))

    async def delete(self, chat_id, message_id: str) -> bool:
        """Удалить сообщение. Возвращает, получилось ли.

        Ответ обязателен по протоколу, и молчаливый `None` стоил дорого: его
        `repaint` читал как «транспорт удалять не умеет» и шёл править ТОЛЬКО
        ЧТО УДАЛЁННОЕ сообщение. Правка отвечала 400, функция выходила раньше
        отправки — и счётчик пачки просто исчезал на втором же куске. Снаружи
        это выглядело как «прислал два сообщения, в ответ пустота» (12.09.2026).
        """
        r = await asyncio.to_thread(self._call, "deleteMessage",
                                    {"chat_id": chat_id, "message_id": int(message_id)})
        return bool((r or {}).get("ok"))

    async def answer(self, callback_id: str, notice: str = "") -> None:
        await asyncio.to_thread(self._call, "answerCallbackQuery", {
            "callback_query_id": callback_id, "text": notice})


# ── хранилище ────────────────────────────────────────────────────────────

def _text(raw) -> str:
    """Строка из redis, чем бы её ни отдал клиент: `decode_responses` включён
    не у всех вызывающих."""
    return raw.decode() if isinstance(raw, bytes) else str(raw)


class RedisStore:
    """Пачка и счётчик в redis той же инсталляции, что и очередь агента."""

    def __init__(self, url: str):
        self.r = aioredis.from_url(url, decode_responses=True)

    async def append(self, key, value): return await self.r.rpush(key, value)
    async def read_all(self, key): return await self.r.lrange(key, 0, -1)
    async def drop(self, key): await self.r.delete(key)
    async def get(self, key): return await self.r.get(key)
    # `ex=0` redis отвергает; ноль здесь значит «без срока», см. Store.put.
    async def put(self, key, value, ttl): await self.r.set(key, value, ex=ttl or None)
    async def enqueue(self, queue, element): await self.r.lpush(queue, element)

    async def expire(self, key, ttl): await self.r.expire(key, ttl)

    async def position(self, queue, element):
        return await self.r.lpos(queue, element)

    async def pop_blocking(self, key, timeout):
        """Ждать элемент, а не крутить опрос: результат сборки приезжает
        через минуты, и опрос секундами — это просто лишний трафик.

        Протухший на блокирующей команде сокет — это пустой круг, а не сбой:
        отдаём None и заходим снова. Иначе разбор результатов пишет
        трейсбек в лог каждые полминуты и тонет в собственном шуме.
        """
        try:
            got = await self.r.blpop(key, timeout=timeout)
        except (RedisTimeoutError, asyncio.TimeoutError):
            return None
        return got[1] if got else None

    async def claim(self, key, ttl):
        """SET NX: захват и проверка одной операцией. Два обновления,
        пришедшие одновременно, иначе оба решат, что счётчика ещё нет."""
        return bool(await self.r.set(key, "1", ex=ttl, nx=True))


# ── разбор входящего ─────────────────────────────────────────────────────

def item_from_message(msg: dict) -> Item | None:
    """Сообщение Telegram → кусок пачки. None — брать нечего.

    Пересылку узнаём и по новому `forward_origin`, и по старым полям: в
    чатах живут сообщения, пересланные разными версиями клиента.

    `unsupported` — не молчаливое игнорирование: кусок возвращается с
    причиной, и человек получает ответ, почему он не взят.
    """
    frm = ""
    origin = msg.get("forward_origin") or {}
    if origin:
        u = origin.get("sender_user") or {}
        frm = (origin.get("sender_user_name")
               or " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
               or (origin.get("chat") or {}).get("title") or "")
    else:
        u = msg.get("forward_from") or {}
        frm = (" ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
               or msg.get("forward_sender_name")
               or (msg.get("forward_from_chat") or {}).get("title") or "")

    mid = str(msg.get("message_id") or "")
    # На что отвечают. У Telegram это готовое поле; у пересланного сообщения
    # его нет, и это правильно: пересылка — новая фактура, а не ответ.
    reply_to = str((msg.get("reply_to_message") or {}).get("message_id") or "")
    base = dict(source="telegram", frm=frm, message_id=mid, reply_to=reply_to,
                date=msg.get("date") or 0)
    caption = (msg.get("caption") or "").strip()

    photos = msg.get("photo") or []
    if photos:
        # Последний размер — самый крупный: мелкие превью читать бесполезно,
        # текст скриншота виден только на полном.
        big = photos[-1]
        return Item(type="photo", text=caption, file_id=big.get("file_id", ""),
                    file_name=f"photo-{mid}.jpg", mime="image/jpeg",
                    file_size=big.get("file_size") or 0, **base)

    doc = msg.get("document")
    if doc:
        return Item(type="document", text=caption,
                    file_id=doc.get("file_id", ""),
                    file_name=doc.get("file_name") or f"file-{mid}",
                    mime=doc.get("mime_type") or "",
                    file_size=doc.get("file_size") or 0, **base)

    for field in ("voice", "audio", "video_note"):
        media = msg.get(field)
        if media:
            return Item(type="voice", text=caption,
                        file_id=media.get("file_id", ""),
                        file_name=f"{field}-{mid}",
                        mime=media.get("mime_type") or "",
                        file_size=media.get("file_size") or 0, **base)

    if msg.get("video") or msg.get("animation"):
        return Item(type="unsupported", text=caption, **base,
                    reason="видео вне скоупа — опиши текстом или пришли скриншот")

    text = (msg.get("text") or caption).strip()
    if text:
        return Item(type="text", text=text, **base)
    return None


# ── цикл ─────────────────────────────────────────────────────────────────

class TelegramIntake:
    """Приём в Telegram на длинных опросах.

    Тонкая обёртка над `Channel`: транспорт, чтение обновлений и допуск. Всё,
    что происходит после «пришёл кусок» и «нажали кнопку», лежит в канале —
    и там же его берёт первый клиент, который читает события вебхуком.
    """

    def __init__(self, token: str, redis_url: str, queue_key: str,
                 allowed: set[str], notify_chat: str = "",
                 tracker=None, agent_user_id: str = "", transcribe=None,
                 update_source=None, poll_assignments_sec: int = 0,
                 incident_label: str = "", webhook_port: int = 0,
                 webhook_bind: str = "127.0.0.1",
                 webhook_path: str = "/linear", webhook_secret: str = "",
                 people: People | None = None, team_id: str = "",
                 project_id: str = "", deploy_secret: str = "",
                 deploy_path: str = "/deploy", pull_opts: dict | None = None,
                 signing_token: str = "", signing_path: str = "/queue",
                 signing_states: tuple[str, ...] = ("In Review",),
                 signing_working: tuple[str, ...] = ("In Progress",),
                 signing_checking: tuple[str, ...] = ("Verifying",),
                 persona_keys: dict[str, str] | None = None,
                 reviewer=None, handoff=None, reviewer_key: str = DEFAULT_KEY,
                 reviewer_tz: str = DEFAULT_TZ, owner_name: str = "",
                 deployment=None, archive_after_days: int = 0, hosting=None,
                 canary_opts: dict | None = None):
        self.token = token
        # Общий чат: туда уходит то, что касается работы вообще, а не чьей-то
        # пачки, — в том числе «вебхук не поднялся».
        self.notify_chat = notify_chat
        self.store = RedisStore(redis_url)
        self.transport = TelegramTransport(token)
        # Распознавание голоса — за инсталляцией: у продукта своего нет, а
        # молча проглоченное голосовое это постановка без половины фактуры.
        self.transcribe = transcribe
        # Обновление ядра — по кнопке в том же чате. Кто может нажать: те же,
        # кого пускает приём; открывать это шире нельзя — нажатие
        # перезапускает раннер.
        self.updater = (Updater(update_source, self.transport, notify_chat,
                                may_apply=lambda uid: is_allowed(uid, allowed))
                        if update_source else None)
        # Карта людей нужна ленте, памяти об авторах приёмных задач и командам
        # в личке (кто владелец). Пустая — рабочее состояние: площадка без
        # команды не адресует лично, а команды не работают ни у кого.
        self.people = people or People(store=self.store)
        if self.people.store is None:
            self.people.store = self.store
        # Кто принимает работу: владелец из карты или делегат из хранилища.
        # Один объект на двух потребителей — ленту и команду `/reviewer`, —
        # чтобы «кому пишем» и «кого показываем» не разошлись.
        self.reviewer = reviewer or Reviewer(
            self.store, self.people, self.transport, tracker,
            key=reviewer_key, tz_name=reviewer_tz, owner_name=owner_name)
        # Назначение задачи агенту начинает работу без кнопки. Обычный путь —
        # вебхук трекера: инсталляция даёт адрес и секрет, код общий.
        # Уборка закрытых задач в архив. Заводится всегда, когда есть трекер:
        # потолок бесплатного тарифа считает неархивные задачи, и без уборки
        # он приходит за месяц. Площадка может отложить её днями, трекер без
        # архива её просто не заметит.
        self.archiver = (Archiver(self.store, tracker, queue_key=queue_key,
                                  agent_user_id=agent_user_id,
                                  after_days=archive_after_days,
                                  team_id=team_id)
                         if tracker else None)
        # Лента задачи в чате. Без адресата (ни карты, ни общего чата) её
        # заводить незачем: писать всё равно некуда.
        self.feed = (IssueFeed(self.store, self.transport, tracker, self.people,
                               notify_chat=notify_chat,
                               agent_user_id=agent_user_id, team_id=team_id,
                               reviewer=self.reviewer, handoff=handoff,
                               archiver=self.archiver)
                     if tracker and (self.people.chat_by_user or notify_chat)
                     else None)
        # Проверка выката. Откуда узнать, что работа выкатилась, — за
        # инсталляцией (`deploy.Deployment`); нет реализации — ничего не
        # ставится, но вердикты из моста раннера всё равно разбираются.
        # Рабочий каталог раннера — источник статистики по сессиям и токенам.
        # Тот же, что у раннера: его кладёт установщик из одной конфигурации.
        self.work_root = os.environ.get("AGENT_WORK_ROOT", "")
        self.queue_key = queue_key
        # Адрес задачи в трекере: из него собирается ссылка «↗» на странице
        # сессий. У каждого трекера свой, поэтому настройка.
        self.issue_base = os.environ.get("TRACKER_ISSUE_URL", "")
        # Кто может открыть страницу. Пусто — проверка выключена, доступ по
        # токену в ссылке. Задано — нужна подпись Telegram: ссылку можно
        # переслать, подпись — нет.
        self.signing_who = tuple(
            x.strip() for x in os.environ.get("SIGNING_ALLOWED_USER_IDS", "").split(",")
            if x.strip())
        self.signing_token = str(signing_token or "")
        self.signing_path = (signing_path if signing_path.startswith("/")
                             else "/" + signing_path)
        self.signing_states = tuple(signing_states)
        # Хостинг для действий со страницы подписи («Смержить»). Нет — кнопок
        # нет, страница читает, как прежде.
        self.hosting = hosting
        # Состояние «не проверено»: там кнопка мержа тоже есть, но с
        # предупреждением и подтверждением — то же имя, что читает раннер.
        self.unverified_state = (os.environ.get("AGENT_UNVERIFIED_STATE_NAME", "").strip()
                                 or "Not Verified")
        self.signing_working = tuple(signing_working)
        self.signing_checking = tuple(signing_checking)
        # Ключи личностей — только чтобы за ними присматривать. Подписывается
        # ими раннер; приём сюда не пишет.
        self.persona_keys = dict(persona_keys or {})
        # Трекер приходит параметром и раскладывается по подобъектам (лента,
        # тяга, архив), а в `self` не попадал. Сторож звал `self.tracker` и
        # падал с AttributeError МГНОВЕННО — а широкий `except` записывал всех
        # личностей в мёртвые и слал тревогу в чат. Ложная тревога 14.09.2026
        # ровно отсюда.
        self._tracker = tracker
        self.signing_team = str(team_id or "")
        # То же значение, но своим именем: гейт второго входа проверки — не
        # про страницу подписи, и читать его из `signing_team` значит однажды
        # поменять одно, сломав другое.
        self.team_id = str(team_id or "")
        # Состояние, перевод в которое поднимает проверку. Пусто — второго
        # входа нет, и всё работает как до него: проверяется только то, что
        # выкатилось. У клиентских задач выката не бывает вовсе, и без этой
        # настройки они не проверяются никогда.
        self.verify_state = os.environ.get("AGENT_VERIFYING_STATE_NAME", "").strip()
        self.deploy_secret = str(deploy_secret or "")
        self.deploy_path = deploy_path if deploy_path.startswith("/") else "/" + deploy_path
        # Ворота перед постановкой (DEV-86): задача в трекере, ветка и коммит
        # на площадке, круги — по рабочему каталогу. Чего у инсталляции нет,
        # того ворота не сверяют и говорят об этом в записи запуска.
        repos_root = os.environ.get("AGENT_REPOS_ROOT", "")
        self.verification = Verification(
            self.store, queue_key, deployment, agent_user_id=agent_user_id,
            feed=self.feed, transport=self.transport, notify_chat=notify_chat,
            lookup=getattr(tracker, "issue_by_identifier", None) if tracker else None,
            repos=Repos(repos_root) if repos_root else None,
            work_root=self.work_root,
            # Мерж PR в основную → Done: трекер целиком (три метода) и имя
            # закрытого состояния — той же переменной, что читает раннер.
            tracker=tracker,
            done_state=os.environ.get(DONE_STATE_VAR, ""),
            cancel_state=os.environ.get(CANCEL_STATE_VAR, ""),
            # Хостинг воротам — только ради одного вопроса: вливается ли
            # ветка (DEV-109). Нет его — ворота отвечают «не сверено».
            hosting=self.hosting)
        # Подпись работы из чата: «Слить» и «Отменить» под сообщением задачи
        # (DEV-123). Собирается ЗДЕСЬ, а не вместе с лентой, потому что ей
        # нужна `Verification` — дорога задачи в закрытое состояние, — а та
        # строится ниже ленты. Поэтому лента получает её довеском; передал
        # инсталляция свою — её и оставляем, продукт не спорит.
        #
        # Кому можно нажать: тем, кто подписывает работу
        # (`SIGNING_ALLOWED_USER_IDS`), а не всем, кого пускает приём. Приём
        # — это «положить кусок в пачку», а здесь мерж в основную ветку.
        # Список не задан — решает допуск приёма, как у кнопки обновления.
        self.signoff = signoff.SignOff(
            hosting=self.hosting, verification=self.verification,
            transport=self.transport, store=self.store,
            may_act=lambda uid: is_allowed(uid, self.signing_who or allowed),
            # Чем снять кнопки, когда нажатие сработало: лента правит
            # сообщения по id задачи, а нажатие приносит идентификатор —
            # нужны оба.
            feed=self.feed,
            lookup=getattr(tracker, "issue_by_identifier", None) if tracker else None)
        if self.feed is not None and self.feed.handoff is None:
            self.feed.handoff = self.signoff
        # Потолок кругов — общий для всех путей постановки рабочей сессии.
        self.rounds = ((lambda ident: runs.rounds_exhausted(self.work_root, ident))
                       if self.work_root and runs.MAX_WORK_ROUNDS > 0 else None)
        # Тяга: агент сам берёт из бэклога, пока есть слот и дневной лимит не
        # выбран. Без трекера, агента или команды брать неоткуда и некому; с
        # нулевым дневным потолком тяга выключена — площадка, где работу
        # начинает только человек, остаётся как была.
        self.puller = (Puller(tracker, self.store, agent_user_id, team_id,
                              incident_label=incident_label,
                              **(pull_opts or {}))
                       if tracker and agent_user_id and team_id
                       # Включена, если задан ХОТЬ ОДИН потолок. Раньше
                       # смотрели только на дневной, и выключив его в пользу
                       # очереди на подписи, тяга исчезала целиком — вместе с
                       # обратным отсчётом и плиткой в шапке (13.09.2026).
                       and ((pull_opts or {}).get("max_per_day")
                            or (pull_opts or {}).get("max_open")) else None)
        # Стопки человека — те же две, что считает тяга: на подписи
        # (SIGNING_STATES) и не прошедшие проверку. Состояние «проверка идёт»
        # вычитается всегда: оно рассасывается само, и сверять там нечего.
        # Список строится здесь, а не берётся у тяги: тяги может не быть
        # вовсе (все потолки нулевые), а стопки у человека есть всегда.
        self.human_states = tuple(
            s for s in dict.fromkeys(self.signing_states + (self.unverified_state,))
            if s and s != self.verify_state)
        # Сверка пропущенных мержей: раз в час пройти стопки человека и
        # подобрать то, о чём события хостинга не было. Клоны площадки — те
        # же, что у ворот; дорога в Done — та же, что у события и у кнопки.
        # Тот же обход пересматривает и сливаемость: протухшую задачу он
        # уводит из стопки в «не проверено» на агента и ставит ему сессию
        # разрешения конфликта (DEV-109). Очередь и агент — те же, что у
        # вебхука; состояние — то же, куда кладёт не прошедшую работу
        # маршрут вердикта.
        self.reconciler = Reconciler(
            tracker, self.verification, team_id, states=self.human_states,
            repos=self.verification.repos, hosting=self.hosting,
            store=self.store, queue_key=queue_key, agent_id=agent_user_id,
            stale_state=self.unverified_state)
        # Мерж в основную ветку — единственный момент, когда чужие ветки
        # становятся несливаемыми. Пересмотр по часам узнал бы об этом на
        # час позже; здесь он узнаёт сразу.
        self.verification.after_merge = self.reconciler.wake
        # Сверка «в работе»: статус говорит «агент работает», а сессии нет.
        # Состояния — те же, что показывает страница очереди как работу
        # (SIGNING_WORKING_STATES): два списка разошлись бы в первую же
        # неделю. Куда уводить: упавшую — в первую стопку человека (туда же,
        # куда сдаётся работа), не начатую — в «Todo», откуда её можно начать
        # заново. Очередь спрашиваем сами: без неё сверка отбирала бы задачи
        # у тех, кто ждёт слот.
        self.stalled = Stalled(
            tracker, self.work_root, team_id, states=self.signing_working,
            review_state=self.signing_states[0] if self.signing_states else "",
            todo_state=os.environ.get("AGENT_TODO_STATE_NAME", "").strip() or "Todo",
            agent_user_id=agent_user_id, reviewer_id=self.reviewer.user_id,
            queued=self._queued_idents)
        # Канарейка маршрута: раз в сутки фабрика проезжает собственный путь
        # задачей-пустышкой. Имена состояний — те же, что у страницы подписи и
        # у раннера: второй список разошёлся бы с первым в первую же неделю.
        self.canary = Canary(
            tracker, self.store, self.transport, team_id, agent_user_id,
            notify_chat=notify_chat, project_id=project_id,
            hosting=self.hosting,
            stages=Stages(progress=signing_working[0] if signing_working else "In Progress",
                          verifying=signing_checking[0] if signing_checking else "Verifying",
                          review=signing_states[0] if signing_states else "In Review",
                          unverified=self.unverified_state or "Not Verified",
                          done=os.environ.get(DONE_STATE_VAR, "") or "Done"),
            **(canary_opts or {}))
        # Вебхук собираем ПОСЛЕ проверки выката (её приёмник едет вторым путём
        # на том же порту) и ДО канала: от того, есть ли вебхук, зависит,
        # ставит ли задачу в очередь сама кнопка «Отдать агенту». Есть вебхук —
        # назначение поймает он, и постановка кнопкой будет ВТОРОЙ: раннер
        # возьмёт задачу дважды, откроет две ветки и дважды отчитается. 11.09
        # так вышло у первого клиента (TASK-285, TASK-288), 12.09 — здесь, на
        # DEV-62. Решает продукт, а не настройка: он единственный знает про
        # оба пути.
        self.webhook = self._webhook(
            webhook_port, webhook_bind, webhook_path, webhook_secret,
            queue_key, agent_user_id, tracker, incident_label)
        # Журнал запусков: считает по рабочим каталогам раннера и кладёт
        # карточку учёта в задачу. Без рабочего каталога его нет вовсе —
        # команда, отвечающая «не настроено», лучше команды, отвечающей нулями.
        self.journal = (runs.Journal(self.work_root, tracker=tracker,
                                     verdicts=Verdicts(self.store),
                                     transport=self.transport,
                                     notify_chat=notify_chat)
                        if self.work_root else None)
        self.channel = Channel(
            "telegram", self.store, self.transport, tracker, queue_key,
            allowed=allowed, agent_user_id=agent_user_id,
            notify_chat=notify_chat, updater=self.updater,
            incident_label=incident_label, people=self.people,
            reviewer=self.reviewer, project_id=project_id,
            journal=self.journal,
            # `/unblock DEV-66 <что снято>` — снять сторожа «наблюдать негде»
            # (DEV-105). Вход постановки тот же, что у перевода статуса:
            # команда только кладёт факт, решают ворота.
            unblocker=Unblock(self.work_root, self.transport,
                              raise_check=self.verification.on_unblocked),
            # Нажатия «Слить» и «Отменить» — тот же объект, что вешает
            # кнопки: клавиатура и её разбор обязаны жить вместе, иначе
            # хвост нажатия в одном месте разойдётся с хвостом в другом.
            signoff=self.signoff,
            # Не предсказание по трём настройкам, а ФАКТ: условий, при которых
            # вебхук не поднимается, больше одного, и они меняются. Разойдись
            # предсказание с `_webhook` — и задача назначена, а работать нечем:
            # вебхука нет, кнопка выключена, в журнале ни слова.
            enqueue_on_assign=self.webhook is None)
        self.allowed = allowed
        self.results = self.channel.results

        # Опрос — запасной путь: для площадки, до которой снаружи не
        # достучаться, и как страховка от недоехавшей доставки. Ключ захвата
        # у обоих путей один, так что вместе они не дают второй сессии.
        self.assignments = (
            Assignments(tracker, self.store, queue_key, agent_user_id,
                        interval_sec=poll_assignments_sec,
                        incident_label=incident_label, rounds=self.rounds,
                        reviewer_id=self.people.owner_user_id)
            if poll_assignments_sec and tracker and agent_user_id else None)

    def _webhook(self, port, bind, path, secret, queue_key, agent_user_id,
                 tracker, incident_label):
        """Приёмник событий трекера, если инсталляция его завела.

        Без секрета не поднимаем вовсе: открытый порт, заводящий сессии по
        чужому телу, хуже отсутствия вебхука. Слушающий и всё отвергающий
        порт был бы хуже обоих — снаружи он выглядит как работающий.
        """
        if not port:
            return None
        if not secret:
            log.error("вебхук задан портом %s, но без секрета — не поднимаю", port)
            return None
        if not agent_user_id:
            log.error("вебхук задан, но некому назначать (нет AGENT_LINEAR_USER_ID)"
                      " — не поднимаю")
            return None
        # Второй путь на том же порту — событие выката. Наружу каждый новый
        # порт надо выставлять прокси и открывать в брандмауэре, а это
        # разговор с админом инсталляции на каждое событие.
        routes = {}
        if self.deploy_secret and self.verification.deployment is not None:
            routes[self.deploy_path] = (
                DeployReceiver(self.verification, secret=self.deploy_secret),
                DEPLOY_SIGNATURE_HEADER)
        elif self.deploy_secret:
            log.warning("секрет вебхука выката задан, но разбирать событие "
                        "нечем (нет AGENT_DEPLOYMENT_SOURCE) — путь не поднят")
        srv = Server(
            Receiver(self.store, queue_key, agent_user_id, secret=secret,
                     tracker=tracker, incident_label=incident_label,
                     feed=self.feed, verification=self.verification,
                     verify_state=self.verify_state, team_id=self.team_id,
                     rounds=self.rounds, reviewer_id=self.reviewer.user_id),
            bind=bind, port=port, path=path, routes=routes)
        # Очередь подписи — страница на том же порту. Без токена не поднимаем:
        # адрес, отдающий постановки и обсуждение любому встречному, хуже
        # отсутствия страницы.
        if self.signing_token and tracker and self.signing_team:
            srv.pages[self.signing_path] = SigningPage(
                tracker, self.signing_team, self.signing_token,
                states=self.signing_states, working_states=self.signing_working,
                checking_states=self.signing_checking,
                puller=self.puller, work_root=self.work_root,
                verdicts=Verdicts(self.store), issue_base=self.issue_base,
                bot_token=self.token, allowed_ids=self.signing_who,
                hosting=self.hosting, verification=self.verification,
                updater=self.updater, risky_states=(self.unverified_state,))
            # Хвосты действий обязаны совпадать со списком в `SigningPage.base_of`:
            # страница строит адрес кнопки сама, и незарегистрированный хвост даёт
            # 404 при нажатии. 16.09.2026 так и было: кнопка «Смержить» постила на
            # `/merge`, а сервер знал только шесть путей ниже — нажатие уходило в
            # никуда, и выглядело это как «не тот адрес».
            for tail in ("/merge/confirm", "/done/confirm", "/update/confirm",
                         "/merge", "/done", "/update",
                         "/data", "/limits", "/config", "/runs", "/mark", "/auth"):
                srv.pages[self.signing_path + tail] = srv.pages[self.signing_path]
            log.info("очередь подписи на %s (статусы: %s)", self.signing_path,
                     ", ".join(self.signing_states))
        elif self.signing_token:
            log.warning("токен очереди подписи задан, но нет трекера или "
                        "команды — страница не поднята")
        return srv

    def _get_updates(self, offset: int) -> list[dict]:
        url = f"{API}/bot{self.token}/getUpdates?" + urllib.parse.urlencode({
            "offset": offset, "timeout": 25, "limit": 50,
            "allowed_updates": json.dumps(["message", "callback_query"])})
        try:
            with urllib.request.urlopen(url, timeout=40) as r:
                return json.loads(r.read()).get("result") or []
        except Exception as e:
            log.warning("getUpdates: %s", e)
            return []

    async def _seen(self, update: dict) -> bool:
        """Одно обновление обрабатываем один раз.

        Telegram не ретраит так, как MAX, но offset может не сохраниться при
        перезапуске посреди пачки — и тогда те же сообщения приедут снова.
        """
        uid = str(update.get("update_id"))
        key = seen_key("telegram", "update", "", uid)
        return not await self.store.claim(key, 3600)

    async def handle(self, update: dict) -> None:
        if await self._seen(update):
            return

        cq = update.get("callback_query")
        if cq:
            await self._on_click(cq)
            return

        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        # Приём — только в личке. В группе бот пишет уведомления, и складывать
        # туда же пачку значит превратить общий чат в чужую переписку.
        if chat.get("type") != "private":
            return
        user_id = (msg.get("from") or {}).get("id")
        if not is_allowed(user_id, self.allowed):
            log.warning("приём: не пускаю user_id=%s", user_id)
            return

        item = item_from_message(msg)
        if not item:
            return
        if item.type == "voice":
            item = await self._voice(chat.get("id"), item)
            if item is None:
                return
        await self.channel.on_item(chat.get("id"), item)

    # Что показать на кнопке, пока идёт работа. Спиннер гасится ДО похода в
    # сеть: поход в трекер это секунда, и всё это время кнопка выглядела бы
    # незажатой.
    _NOTICES = {"intake:reset": "Очистил", "intake:collect": "Собираю"}
    _PREFIX_NOTICES = (("intake:retry:", "Повторяю"), ("update:apply:", "Обновляю"),
                       (signoff.MERGE, "Мержу"), (signoff.CANCEL, "Отменяю"))

    async def _on_click(self, cq: dict) -> None:
        data = cq.get("data") or ""
        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
        cb_id = cq.get("id") or ""
        if not is_allowed((cq.get("from") or {}).get("id"), self.allowed):
            await self.transport.answer(cb_id, "Недоступно")
            return
        notice = self._NOTICES.get(data) or next(
            (n for pfx, n in self._PREFIX_NOTICES if data.startswith(pfx)), "Секунду")
        await self.transport.answer(cb_id, notice)
        await self.channel.on_click(
            data, chat_id, str((cq.get("message") or {}).get("message_id") or ""),
            cb_id, (cq.get("from") or {}).get("first_name") or "?",
            (cq.get("from") or {}).get("id"))

    async def _voice(self, chat_id, item: Item) -> Item | None:
        """Голосовое → текст, если инсталляция умеет. Не умеет — говорим об
        этом: сессия голос не прочитает, а класть его в пачку значит обещать
        фактуру, которой не будет."""
        text = None
        if self.transcribe:
            try:
                text = await self.transcribe(item)
            except Exception as e:
                log.warning("распознавание не отработало: %s", e)
        if not text:
            await self.transport.send(
                chat_id, "🎙 Голос пока не поддержан — пришли текстом.")
            return None
        return Item(type="text", source=item.source, text=text, frm=item.frm,
                    message_id=item.message_id, date=item.date)

    async def _queued_idents(self) -> set[str]:
        """Задачи, которые фабрика ещё собирается взять. Спрашивает сверка
        «в работе»: у такой задачи сессии нет ЗАКОННО.

        Три места, и все три в том же redis, что и очередь:

          • сама очередь — ждёт свободный слот;
          • отложенные (`AGENT_DEFERRED_KEY`) — сессия упёрлась в лимит
            Claude, продолжение вернётся после сброса;
          • ожидание проверок (`AGENT_CI_WAIT_KEY`) — раннер досматривает
            пайплайн и сам поднимет продолжение на красном.

        Ключи — те же переменные, что читает раннер: инсталляция задаёт их
        один раз на оба процесса. Осечка redis наверх и уходит: круг сверки
        без ответа про очередь не имеет права ничего трогать.

        Форма у записей разная, и разбирается каждая по-своему: в очереди и в
        отложенных лежит задача целиком (JSON), в расписании проверок —
        строка «DEV-11|поколение». Мусор пропускаем молча: в очереди рядом
        может лежать чужая запись, и ронять из-за неё круг сверки незачем —
        но и считать её именем задачи нельзя.
        """
        client = getattr(self.store, "r", None)
        if client is None:
            # Пустое множество тут читалось бы как «в очереди никого», то
            # есть развязывало бы сверке руки. Молчание — не «нет»: пусть
            # круг пропустится целиком.
            raise RuntimeError("хранилище не redis — очередь не спросить")
        deferred = os.environ.get("AGENT_DEFERRED_KEY", "").strip() or "agent:deferred"
        ci_wait = os.environ.get("AGENT_CI_WAIT_KEY", "").strip() or "agent:ci-wait"
        out = set()
        tasks = list(await client.lrange(self.queue_key, 0, -1) or [])
        tasks += list(await client.zrange(deferred, 0, -1) or [])
        for raw in tasks:
            try:
                out.add(str(json.loads(_text(raw)).get("identifier") or ""))
            except (ValueError, AttributeError):
                continue
        for raw in (await client.zrange(ci_wait, 0, -1) or []):
            out.add(_text(raw).split("|")[0])
        return {x for x in out if x}

    async def run(self) -> None:
        # Разбор результатов — рядом, в том же процессе: обе половины приёма
        # говорят одному человеку в один чат, и разводить их по юнитам значит
        # дважды держать один и тот же токен и одно и то же соединение.
        asyncio.create_task(self.results.run())
        asyncio.create_task(self.verification.run())
        # Инцидент без исполнителя напоминает о себе сам: карточка отправлена,
        # но пока на задаче никого нет, работа не идёт.
        if self.channel.duty:
            asyncio.create_task(self.channel.duty.run())
        if self.updater:
            asyncio.create_task(self.updater.watch())
        if self.webhook:
            await self._serve_webhook()
        if self.assignments:
            asyncio.create_task(self.assignments.run())
        if self.puller:
            asyncio.create_task(self.puller.run())
        # Сверка мержей сама решает, есть ли ей чем работать, и говорит об
        # этом строкой: выключенная молчанием сверка неотличима от сверки,
        # которая ничего не нашла, — а разница между ними и есть вся задача.
        asyncio.create_task(self.reconciler.run())
        # Сверка «в работе» — там же и по тому же правилу: сама решает, есть
        # ли ей чем работать, и говорит об этом строкой.
        asyncio.create_task(self.stalled.run())
        # Обход архива — только у площадки с отсрочкой. При нуле лента
        # убирает задачу прямо на закрытии, и обходу нечего искать: он сам
        # выходит, но и заводить его незачем.
        if self.archiver and self.archiver.after_days:
            asyncio.create_task(self.archiver.run())
        # Канарейка маршрута. Без репозитория-пустышки её нет вовсе: заводить
        # задачу, которую негде сделать, значит каждые сутки отдавать слот
        # работы под заведомо вставший прогон.
        if self.canary.able:
            asyncio.create_task(self.canary.run())
        # Нет трекера — спрашивать некого, и сторож только мешал бы ложными
        # тревогами.
        if self.persona_keys and self._tracker is not None:
            asyncio.create_task(self.watch_personas())
        await self.poll()

    async def watch_personas(self, every_sec: int = 3600) -> None:
        """Раз в час спрашивать у трекера, живы ли личности.

        13.09.2026 четыре из пяти отвалились разом — приложениям отозвали
        авторизацию, — и фабрика СУТКИ писала комментарии в пустоту: ключ в
        окружении лежит, трекер отвечает 401, комментарий не создаётся. Со
        стороны это неотличимо от «сессия ничего не сделала», и узнали мы
        случайно.

        Проверять надо ЗАПРОСОМ: отозванный токен выглядит ровно как живой.

        Говорим В КАНАЛ, а не только в журнал. Первая версия этого сторожа
        писала `log.error` — то есть ровно туда, куда в тот раз никто и не
        посмотрел. Сигнал был бы, а поведение системы осталось бы прежним.

        И говорим НА СМЕНУ СОСТОЯНИЯ, а не по таймеру: одна и та же строка раз
        в час превращается в шум, который перестают читать за сутки. Про
        выздоровление сообщаем тоже — иначе непонятно, закрыт вопрос или нет.
        """
        было_мёртвым: set[str] = set()
        while True:
            try:
                dead = set(await asyncio.to_thread(
                    dead_personas, self.persona_keys, self._whoami))
                if dead - было_мёртвым:
                    await self._say_personas(
                        f"⚠️ Личности не отвечают: {', '.join(sorted(dead))}.\n"
                        f"Комментарии от них НЕ ЗАПИШУТСЯ — вердикты и отчёты "
                        f"будут пропадать молча.\n"
                        f"Вернуть: `authorize.mjs <роль> <client_id> <secret>`",
                        level=logging.ERROR)
                elif было_мёртвым - dead:
                    await self._say_personas(
                        f"✅ Личности снова отвечают: "
                        f"{', '.join(sorted(было_мёртвым - dead))}",
                        level=logging.INFO)
                было_мёртвым = dead
            except Exception as e:
                log.warning("сторож личностей не отработал: %s", e)
            await asyncio.sleep(every_sec)

    async def _say_personas(self, text: str, level: int) -> None:
        """Сказать про личности человеку и в журнал. Канала нет — только журнал."""
        log.log(level, "%s", text.replace("\n", " "))
        if self.notify_chat:
            try:
                await self.transport.send(self.notify_chat, text)
            except Exception as e:
                log.warning("не сказать про личности в канал: %s", e)

    def _whoami(self, key: str) -> str:
        """Имя личности по её ключу. Пустое имя — она мертва."""
        return ((self._tracker.whoami(key) or {}).get("name") or "")

    async def _serve_webhook(self) -> None:
        """Поднять вебхук, а если порт не дался — вернуть постановку кнопке.

        Порт занимаем ДО того, как уйти в таску: иначе `OSError` («адрес уже
        используется» — остался прежний процесс, порт держит сосед) падал
        внутри таски, которую никто не ждёт. Процесс при этом жив, а постановки
        задачи нет НИ ОДНОЙ: вебхука нет, а кнопка уже отключена как раз
        потому, что «вебхук есть». Снаружи — «назначил и ничего».
        """
        try:
            await self.webhook.start()
        except Exception as e:
            log.error("вебхук не поднялся (%s) — постановку возвращаю кнопке", e)
            self.webhook = None
            self.channel.cards.enqueue_on_assign = True
            if self.notify_chat:
                await self.transport.send(
                    self.notify_chat,
                    f"⚠️ Вебхук не поднялся ({e}). Назначение в трекере работу "
                    f"НЕ начнёт — ставь кнопкой «Отдать агенту».")
            return
        asyncio.create_task(self.webhook.run())

    def _say_started(self) -> None:
        # Какими путями задача попадает в работу — видно сразу, а не по
        # отсутствию сессии через час.
        # Кнопка — путь не всегда: при живом вебхуке она только назначает,
        # чтобы постановка не случилась дважды.
        ways = ["кнопка" if self.channel.cards.enqueue_on_assign
                else "кнопка (только назначает)"]
        if self.feed:
            ways.append(f"лента ({len(self.people.chat_by_user)} в карте)")
        if self.webhook:
            ways.append(f"вебхук :{self.webhook.port}{self.webhook.path}")
        if self.assignments:
            ways.append(f"опрос раз в {self.assignments.interval_sec}с")
        if self.channel.duty:
            ways.append("присмотр за инцидентами")
        if self.puller:
            ways.append(f"тяга ({self.puller.max_in_flight} сразу, "
                        f"{self.puller.max_per_day} в сутки)")
        dep = self.verification.deployment
        ways.append("выкат → " + (type(dep).__name__ if dep else "не следим"))
        # Уборка молчит, когда не настроена, а замечают это по упёршемуся
        # потолку тарифа — то есть в день, когда трекер перестал заводить
        # задачи. Поэтому её состояние видно при старте, как и всё остальное.
        ways.append("архив → " + (
            "нет у трекера" if not (self.archiver and self.archiver.able)
            else "выключен (нет AGENT_TEAM_ID)" if not self.archiver.team_id
            else f"через {self.archiver.after_days} дн." if self.archiver.after_days
            else "сразу при закрытии"))
        ways.append(self.canary.line())
        log.info("приём запущен: очередь %s, допущено %d, назначение → %s",
                 self.channel.intake.queue_key, len(self.allowed), ", ".join(ways))

    async def poll(self) -> None:
        self._say_started()
        offset = 0
        while True:
            ups = await asyncio.to_thread(self._get_updates, offset)
            for u in ups:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                try:
                    await self.handle(u)
                except Exception:
                    # Одно кривое обновление не должно останавливать приём:
                    # человек продолжит слать, а бот замолчит навсегда.
                    log.exception("обновление %s не обработалось", u.get("update_id"))
            if not ups:
                await asyncio.sleep(1)


def app_from_env() -> "TelegramIntake | None":
    """Приём, собранный из окружения инсталляции. None — окружение не годится.

    Отдельно от `main`, чтобы сборку можно было проверить тестом: она
    состоит из одних передач значений, и ошибка в ней не видна ни при старте,
    ни в логе — только по тому, что через неделю что-то не сработало.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.error("нет TELEGRAM_BOT_TOKEN — приём не поднять")
        return None
    allowed = {x.strip() for x in os.environ.get("INTAKE_ALLOWED_USER_IDS", "").split(",") if x.strip()}
    if not allowed:
        log.error("нет INTAKE_ALLOWED_USER_IDS — пустой список не пускает никого, "
                  "и это правильно, но тогда приём бессмыслен")
        return None
    # Лейбл дежурного читаем ОДИН раз на два потребителя: приём вешает его на
    # создаваемую задачу, раннер по нему же поднимает дежурную сессию. Пока
    # трекер собирался без него, инцидент из приёма уезжал без лейбла — то
    # есть обычной работой, и увидеть это можно было только по промпту сессии.
    incident_label = os.environ.get("AGENT_INCIDENT_LABEL", "дежурный")
    return TelegramIntake(
        token=token,
        redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        queue_key=os.environ.get("AGENT_QUEUE_KEY", "agent:queue"),
        allowed=allowed,
        notify_chat=os.environ.get("AGENT_NOTIFY_CHAT_ID", ""),
        # Личность приёмщика: под её именем появляются заведённая задача и
        # разбор постановки. Имя переменной — то же, что читает раннер
        # (`core/personas.py`, `LINEAR_API_KEY_<ЛИЧНОСТЬ>`); пакеты
        # деплоятся раздельно и импортировать друг друга не могут, поэтому
        # совпадение закреплено контрактным тестом `core/test_personas.py`.
        # Пусто — приём пишет ключом инсталляции, как до личностей.
        tracker=LinearTracker(os.environ.get("LINEAR_API_KEY", ""),
                              incident_label=incident_label,
                              author_key=os.environ.get(
                                  "LINEAR_API_KEY_INTAKE", "").strip()),
        agent_user_id=os.environ.get("AGENT_LINEAR_USER_ID", ""),
        # Те же переменные, что читает раннер. Приём ими не пишет — он за ними
        # присматривает: отвалившаяся личность молча съедает комментарии.
        persona_keys=_persona_keys_from_env(),
        update_source=source_from_env(),
        poll_assignments_sec=int(os.environ.get("AGENT_POLL_ASSIGNMENTS_SEC", "0")),
        incident_label=incident_label,
        # Вся настройка вебхука — адрес и секрет. Порт слушаем на петле:
        # наружу его выставляет обратный прокси, он же держит TLS.
        webhook_port=int(os.environ.get("TRACKER_WEBHOOK_PORT", "0")),
        webhook_bind=os.environ.get("TRACKER_WEBHOOK_BIND", "127.0.0.1"),
        webhook_path=os.environ.get("TRACKER_WEBHOOK_PATH", "/linear"),
        webhook_secret=os.environ.get("TRACKER_WEBHOOK_SECRET", ""),
        # Карта «человек в трекере → чат в мессенджере» и владелец. Без карты
        # лента пишет в общий чат, с картой — лично тому, чья задача.
        people=People.from_json(
            os.environ.get("AGENT_PEOPLE_MAP", ""),
            owner_user_id=os.environ.get("AGENT_REVIEWER_LINEAR_USER_ID", "")),
        # Писать только про задачи этой команды. Пусто — про все.
        team_id=os.environ.get("AGENT_TEAM_ID", ""),
        # Передача приёма. Ключ — тот же, что читает раннер (у него та же
        # переменная); пояс — в котором владелец называет срок. Имя владельца
        # — тоже переменная раннера: одно имя, одно значение, два потребителя.
        reviewer_key=os.environ.get("AGENT_REVIEWER_KEY", "") or DEFAULT_KEY,
        reviewer_tz=os.environ.get("AGENT_REVIEWER_TZ", "") or DEFAULT_TZ,
        owner_name=os.environ.get("AGENT_REVIEWER_NAME", ""),
        # Через сколько дней после закрытия убирать задачу из активных. 0 —
        # сразу: потолок бесплатного тарифа считает неархивные задачи, и без
        # уборки он приходит за месяц. Площадка, где доской пользуются живые
        # люди, ставит себе отсрочку — тогда убирает суточный обход, а не лента.
        archive_after_days=days_from(os.environ.get("AGENT_ARCHIVE_AFTER_DAYS")),
        # Как понять, что работа выкатилась, знает только инсталляция —
        # тем же способом, что источник версий: «модуль:Класс».
        deployment=source_from_env("AGENT_DEPLOYMENT_SOURCE"),
        # Хостинг для кнопки «Смержить» — тем же способом.
        hosting=source_from_env("AGENT_HOSTING_SOURCE"),
        # В какой проект трекера складывать заведённое приёмом. Без него
        # задачу не отфильтровать: в доске она есть, в списке проекта — нет.
        project_id=os.environ.get("AGENT_PROJECT_ID", ""),
        # Событие выката: свой секрет и свой путь на том же порту.
        deploy_secret=os.environ.get("DEPLOY_WEBHOOK_SECRET", ""),
        deploy_path=os.environ.get("DEPLOY_WEBHOOK_PATH", "") or "/deploy",
        # Тяга. Ноль в дневном потолке (и умолчание) выключает её совсем.
        # Очередь подписи: страница на том же порту, доступ по токену в ссылке.
        signing_token=os.environ.get("SIGNING_TOKEN", ""),
        signing_path=os.environ.get("SIGNING_PATH", "") or "/queue",
        signing_working=tuple(
            s.strip() for s in
            os.environ.get("SIGNING_WORKING_STATES", "In Progress").split(",")
            if s.strip()),
        signing_states=tuple(
            s.strip() for s in
            os.environ.get("SIGNING_STATES", "In Review").split(",")
            if s.strip()),
        # Канарейка маршрута: репозиторий-пустышка и файл в нём, час прогона
        # по UTC. Без репозитория выключена — см. `canary.Canary.able`.
        canary_opts={
            "repo": os.environ.get("AGENT_CANARY_REPO", ""),
            "path": os.environ.get("AGENT_CANARY_FILE", "") or "canary/heartbeat.md",
            "hour_utc": int(os.environ.get("AGENT_CANARY_HOUR_UTC", "2")),
            "rounds_norm": int(os.environ.get("AGENT_CANARY_ROUNDS_NORM", "1")),
            "tick_sec": int(os.environ.get("AGENT_CANARY_EVERY_SEC", "120")),
        },
        pull_opts={
            "max_in_flight": int(os.environ.get("AGENT_PULL_MAX_IN_FLIGHT", "2")),
            "max_per_day": int(os.environ.get("AGENT_PULL_MAX_PER_DAY", "0")),
            # Потолок незакрытых: упор в приёмку, а не в производство.
            "max_open": int(os.environ.get("AGENT_PULL_MAX_OPEN", "0")),
            # Что считать «у человека» — те же имена, что показывает
            # страница очереди (два списка разошлись бы в первую же неделю),
            # ПЛЮС состояние «не проверено»: с DEV-96 туда уезжает провал в
            # любом режиме, и это вторая стопка человека — он её разбирает.
            # Добавляется кодом, а не настройкой: список, где её забыли,
            # копил бы поломки без потолка. Состояние «проверка идёт» не
            # считается никогда — оно рассасывается само.
            "open_states": tuple(dict.fromkeys(
                [x.strip() for x in
                 os.environ.get("SIGNING_STATES", "In Review").split(",")
                 if x.strip()]
                + [os.environ.get("AGENT_UNVERIFIED_STATE_NAME", "").strip()
                   or "Not Verified"])),
            "never_count": (os.environ.get("AGENT_VERIFYING_STATE_NAME", "").strip()
                            or "Verifying",),
            # Ровный темп: порция за окно. Ноль — брать сразу, как освободится
            # слот (так было до 12.09.2026).
            "per_window": int(os.environ.get("AGENT_PULL_PER_WINDOW", "0")),
            "window_sec": int(os.environ.get("AGENT_PULL_WINDOW_SEC", "7200")),
            "interval_sec": int(os.environ.get("AGENT_PULL_EVERY_SEC", "60")),
            "min_age_sec": int(os.environ.get("AGENT_PULL_MIN_AGE_SEC", "600")),
            "skip_labels": tuple(
                s.strip() for s in
                os.environ.get("AGENT_PULL_SKIP_LABELS", "").split(",")
                if s.strip()),
        })


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = app_from_env()
    if app is None:
        return 1
    asyncio.run(app.run())
    return 0


# Запускается как `python -m agent_factory.telegram`: путь к файлу
# от того, куда его положила инсталляция, а имя модуля — нет.
if __name__ == "__main__":
    raise SystemExit(main())
