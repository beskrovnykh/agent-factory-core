"""Транспорт MAX.

Второй канал приёма у первого клиента. Отличий от телеграмного больше, чем
кажется, и каждое стоило живой отладки:

- клавиатура — не отдельное поле, а вложение;
- у ПЕРЕСЛАННОГО сообщения тело пустое, содержимое лежит в `link.message` —
  и текст, и вложения. Прямая калька телеграмного разбора возвращала ноль
  вложений МОЛЧА, и человек видел «файлов не было»;
- в событии нажатия нет самого сообщения, а значит нет и chat_id: он лежит на
  уровне update;
- доставка ретраится до десяти раз, поэтому без отпечатка события одно
  нажатие «Собрать задачу» заводило несколько сессий;
- удаления сообщений в API нет вовсе — счётчик здесь правится на месте
  (см. Intake.repaint).
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .intake import Button, Item

log = logging.getLogger("intake")

API_URL = "https://platform-api.max.ru"


class PlainMarkup:
    """Разметки нет: имя остаётся именем, ссылка — текстом со ссылкой рядом.

    Это рабочий исход, а не заглушка. В MAX нет записи «позвать человека
    по id», и притворяться, что есть, значит слать в чат сырой синтаксис
    чужого мессенджера.
    """

    def safe(self, text: str) -> str:
        return text or ""

    def bold(self, text: str) -> str:
        return text

    def link(self, text: str, url: str) -> str:
        return f"{text} {url}".strip()

    def mention(self, chat_id, name: str) -> str:
        return name


class MaxTransport:
    def __init__(self, token: str, api_url: str = API_URL):
        self.token = token
        self.api_url = api_url
        self.markup = PlainMarkup()

    def _call(self, method: str, path: str, params: dict | None = None,
              body: dict | None = None) -> dict:
        if not self.token:
            raise RuntimeError("MAX_BOT_TOKEN не задан")
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            # Тело ошибки MAX содержит причину — без него в журнале остаётся
            # голый код, по которому не отличить «не тот токен» от «нет прав».
            raise RuntimeError(
                f"MAX {method} {path}: {e.code} {e.read()[:200]!r}") from e
        return json.loads(raw) if raw else {}

    @staticmethod
    def _keyboard(buttons: list[Button] | None) -> dict | None:
        """Клавиатура MAX — вложение, а не поле."""
        if not buttons:
            return None
        return {"type": "inline_keyboard", "payload": {"buttons": [[
            {"type": "callback", "text": b.text, "payload": b.payload}
            for b in buttons]]}}

    async def send(self, chat_id, text: str,
                   buttons: list[Button] | None = None,
                   rich: bool = False) -> str:
        body: dict = {"text": text}
        kb = self._keyboard(buttons)
        if kb:
            body["attachments"] = [kb]
        resp = await asyncio.to_thread(self._call, "POST", "/messages",
                                       {"chat_id": chat_id}, body)
        return (((resp or {}).get("message") or {}).get("body") or {}).get("mid", "")

    async def edit(self, chat_id, message_id: str, text: str,
                   buttons: list[Button] | None = None,
                   rich: bool = False) -> bool:
        if not message_id:
            # Пустой mid — не «правка не удалась», а ошибка выше по стеку:
            # поле взяли не оттуда. Молчаливый 400 от MAX об этом не сообщал.
            log.warning("max: пустой message_id, правку не шлю")
            return False
        kb = self._keyboard(buttons)
        # Пустой список СНИМАЕТ клавиатуру — именно это нужно после нажатия,
        # иначе под разобранной пачкой остаётся живая кнопка.
        body = {"text": text, "attachments": [kb] if kb else []}
        try:
            await asyncio.to_thread(self._call, "PUT", "/messages",
                                    {"message_id": message_id}, body)
            return True
        except Exception as e:
            log.warning("max: правка не прошла: %s", e)
            return False

    async def delete(self, chat_id, message_id: str) -> bool:
        """В MAX удаления сообщений нет. Не ошибка — развилка: приём поправит
        счётчик на месте вместо того, чтобы пересоздать его внизу ленты."""
        return False

    async def answer(self, callback_id: str, notice: str = "") -> None:
        body = {"notification": notice} if notice else {}
        try:
            await asyncio.to_thread(self._call, "POST", "/answers",
                                    {"callback_id": callback_id}, body)
        except Exception as e:
            log.warning("max: ответ на нажатие не прошёл: %s", e)


def item_from_message(message: dict) -> Item | None:
    """Сообщение MAX → кусок пачки.

    У пересланного тело пустое, а содержимое — в `link.message`. Отправителя
    берём из `link.sender`: в теле его нет.
    """
    body = message.get("body") or {}
    link = message.get("link") or {}
    src = link.get("message") if link.get("type") == "forward" else None
    holder = src or body
    # У MAX и пересылка, и ответ приезжают одним и тем же `link` — разводит их
    # только `type`. Перепутать нельзя: у пересылки содержимое в `link.message`
    # и это НОВАЯ фактура, у ответа в `link.message` лежит то, НА ЧТО отвечают.
    reply_to = (str((link.get("message") or {}).get("mid") or "")
                if link.get("type") == "reply" else "")
    text = (holder.get("text") or "").strip()
    frm = ""
    if src:
        snd = link.get("sender") or {}
        frm = " ".join(x for x in (snd.get("first_name"), snd.get("last_name"))
                       if x).strip()
    for a in holder.get("attachments") or []:
        payload = a.get("payload") or {}
        url = payload.get("url")
        if not url:
            continue
        kind = a.get("type")
        if kind == "image":
            # MAX отдаёт webp, а не jpeg, и имени файла у картинки нет.
            name = a.get("filename") or f"image-{payload.get('photo_id') or 'x'}.webp"
            return Item(type="photo", source="max", frm=frm, text=text,
                        reply_to=reply_to,
                        file_url=url, file_name=name, file_size=a.get("size") or 0)
        if kind == "file":
            # mime в событии нет вообще — тип выводится по расширению уже в
            # раннере, здесь не выдумываем.
            return Item(type="document", source="max", frm=frm, text=text,
                        reply_to=reply_to,
                        file_url=url, file_name=a.get("filename") or "файл",
                        file_size=a.get("size") or 0)
    if text:
        return Item(type="text", source="max", frm=frm, text=text,
                    reply_to=reply_to)
    return None


def update_fingerprint(update: dict) -> tuple[str, str, str]:
    """Отпечаток события: тип, время, идентификатор — как советует
    документация MAX. Доставка ретраится до десяти раз, и без него одно
    нажатие «Собрать задачу» заводило несколько сессий.
    """
    body = (update.get("message") or {}).get("body") or {}
    cb = update.get("callback") or {}
    return (str(update.get("update_type") or ""),
            str(update.get("timestamp") or ""),
            str(cb.get("callback_id") or body.get("mid") or ""))


def chat_id_of(update: dict) -> str:
    """Чат события. В нажатии сообщения нет вовсе — chat_id лежит на уровне
    update, и искать его внутри callback значит молча выйти, найдя пустую
    пачку несуществующего чата."""
    msg = update.get("message") or (update.get("callback") or {}).get("message") or {}
    return str(update.get("chat_id")
               or (msg.get("recipient") or {}).get("chat_id")
               or (update.get("recipient") or {}).get("chat_id") or "")


def message_id_of(update: dict) -> str:
    """mid сообщения, под которым нажали кнопку.

    Лежит не в одном месте: у части событий в `body`, у части — прямо на
    объекте сообщения. Пустой mid уходил в правку и возвращался «Missing
    required parameter» — правка молча не применялась, и счётчик застревал.
    """
    msg = update.get("message") or (update.get("callback") or {}).get("message") or {}
    return str((msg.get("body") or {}).get("mid") or msg.get("mid")
               or (msg.get("message") or {}).get("mid") or "")


def user_id_of(update: dict) -> str:
    """Кто это сделал. У сообщения — sender, у нажатия — user."""
    msg = update.get("message") or {}
    # В message_callback объекта `user` может не быть вовсе, и человек тогда
    # виден только как отправитель исходного сообщения. 08.09 на этом застрял
    # первый живой прогон: владелец жал кнопку восемь раз, проверка каждый раз
    # получала None и отвечала «Недоступно».
    return str(((update.get("callback") or {}).get("user") or {}).get("user_id")
               or (msg.get("sender") or {}).get("user_id") or "")
