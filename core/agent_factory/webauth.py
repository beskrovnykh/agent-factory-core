"""Кто открыл страницу — по подписи Telegram, а не по знанию ссылки.

Токен в ссылке — это «кто знает адрес, тот и внутри». Ссылку пересылают,
копируют в браузер на чужом компьютере, она остаётся в истории. Для страницы,
которая показывает постановки, сдачи и деньги, этого мало.

Telegram при открытии мини-приложения кладёт в адрес подписанные данные о
пользователе (`tgWebAppData`): кто он, когда открыл, и подпись ключом бота.
Подделать её, не зная токена бота, нельзя. Проверяем — и пускаем только тех,
кого назвала инсталляция.

Алгоритм — из документации Telegram, и в нём легко ошибиться местами ключа и
сообщения: секрет получается как HMAC от ТОКЕНА БОТА с ключом-константой
«WebAppData», а не наоборот.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse

log = logging.getLogger("intake")

# Данные старше этого не принимаем: подписанная строка, утекшая из истории
# браузера, не должна пускать вечно.
MAX_AGE_SEC = 24 * 3600


def check_string(fields: dict) -> str:
    """Строка, которую подписал Telegram: пары `ключ=значение` без `hash`,
    отсортированные по ключу и склеенные переводом строки.

    Убирается ТОЛЬКО `hash`. Поле `signature` (оно приезжает для сторонней
    проверки) Telegram в подпись включает, и выбросив его, мы считаем хеш от
    другой строки. 12.09.2026 это стоило полного отказа входа: в журнале
    «вход отклонён: неизвестно» при верной подписи и живом пользователе.
    """
    return "\n".join(f"{k}={v}" for k, v in sorted(fields.items())
                     if k != "hash")


def verify(init_data: str, bot_token: str,
           max_age_sec: int = MAX_AGE_SEC, now: float | None = None) -> dict | None:
    """Разобранный пользователь или None. None — значит не пускать."""
    if not (init_data and bot_token):
        return None
    fields = {k: v[0] for k, v in urllib.parse.parse_qs(init_data,
                                                        keep_blank_values=True).items()}
    got = fields.get("hash") or ""
    if not got:
        return None
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check_string(fields).encode(), hashlib.sha256).hexdigest()
    # Сравнение байтами: на строках `compare_digest` требует ASCII и на чужом
    # вводе кидает TypeError — ровно та ошибка, что стоила 500 вместо 401 на
    # вебхуке 12.09.2026.
    if not hmac.compare_digest(got.encode("latin-1", "replace"), want.encode()):
        return None
    try:
        age = float(now if now is not None else time.time()) - float(fields.get("auth_date") or 0)
    except Exception:
        return None
    if age > max_age_sec:
        log.info("вход: подпись верна, но данные старше %d ч", max_age_sec // 3600)
        return None
    try:
        user = json.loads(fields.get("user") or "{}")
    except Exception:
        return None
    return user if user.get("id") else None


def allowed(user: dict | None, ids: set) -> bool:
    """Пускаем только названных. Пустой список — никого: инсталляция, которая
    включила проверку и забыла перечислить людей, должна не пускать, а не
    пускать всех."""
    return bool(user) and str(user.get("id")) in {str(i) for i in ids}
