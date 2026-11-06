#!/usr/bin/env python3
"""Обновление токенов личностей Linear до того, как они умрут.

Токен доступа Linear живёт **ровно сутки** (`expires_in` = 86399). Вместе с
ним выдаётся `refresh_token` и работает `POST /oauth/token` с
`grant_type=refresh_token`. До 15.09.2026 скрипт авторизации сохранял только
сам токен и выбрасывал остальное — поэтому каждые сутки все пять личностей
молча умирали, вердикты начинали подписываться общим ключом (то есть именем
человека), и починка была одна: пройти браузерное согласие пять раз.

Здесь — хранилище с полным ответом и обновление по расписанию. Раннер читает
токены отсюда, а не только из окружения, поэтому обновление не требует его
перезапуска.

    python3 core/linear_tokens.py --check          # что живо, что скоро умрёт
    python3 core/linear_tokens.py --refresh        # обновить всё, чему пора
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Где лежат токены. Отдельный файл, а не переменные окружения: обновление
# должно доезжать до работающего раннера без перезапуска.
STORE = Path(os.environ.get("AGENT_LINEAR_TOKENS",
                            str(Path.home() / ".config" / "linear-agents" / "tokens.json")))
TOKEN_URL = "https://api.linear.app/oauth/token"
API_URL = "https://api.linear.app/graphql"

# Где лежат id и секрет приложения каждой личности. Это и есть способ
# обойтись без браузера совсем: `grant_type=client_credentials` выдаёт токен
# на 30 дней, actor у него автоматически `app` — ровно то, что нужно роли, —
# и человеческого согласия не требует вовсе.
#
# Формат: {"qa": {"client_id": "...", "client_secret": "..."}, ...}
CREDS = Path(os.environ.get("AGENT_LINEAR_CLIENTS",
                            str(Path.home() / ".config" / "linear-agents" / "clients.json")))
# Те же права, что были у выданных вручную токенов.
SCOPES = "read,issues:create,comments:create,app:assignable,app:mentionable"

# За сколько до смерти обновлять. Сутки жизни, обновляем за шесть часов: даже
# если обновление падало трижды подряд, запас ещё есть.
REFRESH_BEFORE_SEC = int(os.environ.get("AGENT_TOKEN_REFRESH_BEFORE_SEC", 6 * 3600))


def load(store: Path = None) -> dict:
    p = store or STORE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict, store: Path = None) -> None:
    """Пишем через временный файл: обрыв посреди записи не должен оставить
    покалеченное хранилище — токенов нет вообще хуже, чем старые."""
    p = store or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _expires_at(rec: dict) -> float | None:
    """Когда токен умрёт, секундами эпохи. Источник — `expires_at`, иначе
    считаем от `obtained` плюс `expires_in`, иначе не знаем."""
    raw = rec.get("expires_at")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    got, ttl = rec.get("obtained"), rec.get("expires_in")
    if got and ttl:
        try:
            return datetime.fromisoformat(str(got).replace("Z", "+00:00")).timestamp() + float(ttl)
        except ValueError:
            pass
    return None


def alive(token: str, timeout: float = 10.0) -> str | None:
    """Живой токен → имя владельца. Мёртвый или сеть легла → None.

    Проверка именно запросом: у Linear нет способа спросить «жив ли токен» без
    обращения, а срок в хранилище может врать после ручной переавторизации.
    """
    req = urllib.request.Request(
        API_URL, data=json.dumps({"query": "{viewer{name}}"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
        return ((body.get("data") or {}).get("viewer") or {}).get("name")
    except Exception:
        return None


def refresh_one(rec: dict, client_id: str, client_secret: str,
                timeout: float = 15.0) -> tuple[dict | None, str]:
    """Обновить одну запись. → (новая запись, причина отказа)."""
    rt = rec.get("refresh_token")
    if not rt:
        return None, ("refresh_token не сохранён — нужна разовая "
                      "переавторизация через authorize.mjs")
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": rt,
        "client_id": client_id, "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error") or e.reason
        except Exception:
            detail = e.reason
        return None, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if not body.get("access_token"):
        return None, f"ответ без токена: {sorted(body)}"
    out = dict(rec)
    out["access_token"] = body["access_token"]
    out["obtained"] = datetime.now(timezone.utc).isoformat()
    if body.get("refresh_token"):
        # Обновлённый refresh может прийти новый — старый после этого мёртв.
        out["refresh_token"] = body["refresh_token"]
    if body.get("expires_in"):
        out["expires_in"] = body["expires_in"]
        out["expires_at"] = datetime.fromtimestamp(
            time.time() + float(body["expires_in"]), timezone.utc).isoformat()
    return out, ""


def mint(client_id: str, client_secret: str, scopes: str = SCOPES,
         timeout: float = 15.0) -> tuple[dict | None, str]:
    """Выпустить токен по id и секрету приложения. Браузер не нужен.

    Токен живёт 30 дней против суток у выданного через согласие, и `actor` у
    него `app` — то есть комментарии и переводы подписаны личностью, а не
    человеком. Ограничение Linear: до 1000 живых токенов на приложение при
    одинаковых правах, так что перевыпускать можно свободно.
    """
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials", "scope": scopes,
        "client_id": client_id, "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error") or e.reason
        except Exception:
            detail = e.reason
        return None, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if not body.get("access_token"):
        return None, f"ответ без токена: {sorted(body)}"
    ttl = float(body.get("expires_in") or 0)
    out = {
        "access_token": body["access_token"],
        "scope": body.get("scope") or scopes,
        "obtained": datetime.now(timezone.utc).isoformat(),
        "выдан": "client_credentials",
    }
    if ttl:
        out["expires_in"] = body["expires_in"]
        out["expires_at"] = datetime.fromtimestamp(
            time.time() + ttl, timezone.utc).isoformat()
    return out, ""


def load_clients(path=None) -> dict:
    """id и секреты приложений. Нет файла — пусто, работаем по-старому."""
    p = path or CREDS
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("client_id") and v.get("client_secret")}


def due(data: dict, now: float | None = None,
        before: int = REFRESH_BEFORE_SEC) -> list[str]:
    """Кому пора обновляться. Срок неизвестен — тоже пора: лучше лишний
    запрос, чем мёртвая личность."""
    now = time.time() if now is None else now
    out = []
    for name, rec in data.items():
        exp = _expires_at(rec)
        if exp is None or exp - now <= before:
            out.append(name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="что живо и когда умрёт")
    ap.add_argument("--refresh", action="store_true", help="обновить, кому пора")
    ap.add_argument("--all", action="store_true", help="обновить всех, не только срочных")
    ap.add_argument("--store", default=str(STORE))
    a = ap.parse_args()
    store = Path(a.store)
    data = load(store)
    if not data:
        print(f"хранилище пусто или не читается: {store}")
        return 1

    if a.check or not a.refresh:
        now = time.time()
        for name, rec in sorted(data.items()):
            exp = _expires_at(rec)
            left = "срок неизвестен" if exp is None else (
                f"осталось {int((exp - now) / 60)} мин" if exp > now else "ИСТЁК")
            who = alive(rec.get("access_token", "")) or "МЁРТВ"
            rt = "есть" if rec.get("refresh_token") else "НЕТ"
            print(f"  {name:10s} {who:12s} {left:20s} refresh_token: {rt}")
        return 0

    clients = load_clients()
    names = sorted(set(data) | set(clients)) if a.all else sorted(
        set(due(data)) | (set(clients) - set(data)))
    if not names:
        # Молчаливый прогон неотличим от несработавшего таймера — именно так
        # личности и умирали незаметно. Прогон всегда говорит, что увидел.
        now = time.time()
        запасы = [(_expires_at(r) or now) - now for r in data.values()]
        дней = int(min(запасы) / 86400) if запасы else 0
        print(f"обновлять некого: ближайший срок через {дней} дн., "
              f"порог {int(REFRESH_BEFORE_SEC / 86400)} дн.")
        return 0

    if clients:
        # Путь без браузера: где есть id и секрет, просто выпускаем новый.
        bad = 0
        for name in names:
            c = clients.get(name)
            if not c:
                continue
            rec, why = mint(c["client_id"], c["client_secret"])
            if rec is None:
                print(f"  {name:10s} НЕ выпущен: {why}")
                bad += 1
                continue
            data[name] = rec
            print(f"  {name:10s} выпущен заново, годен до {rec.get('expires_at','?')[:19]}")
        save(data, store)
        оставшиеся = [n for n in names if n not in clients]
        if not оставшиеся:
            return 1 if bad else 0
        names = оставшиеся
        print(f"  без id/секрета: {', '.join(names)} — пробую обновлением")

    cid = os.environ.get("LINEAR_CLIENT_ID", "")
    secret = os.environ.get("LINEAR_CLIENT_SECRET", "")
    if not (cid and secret):
        print("нет ни clients.json, ни LINEAR_CLIENT_ID/SECRET — обновить нечем")
        return 1

    bad = 0
    for name in names:
        rec, why = refresh_one(data[name], cid, secret)
        if rec is None:
            print(f"  {name:10s} НЕ обновлён: {why}")
            bad += 1
            continue
        data[name] = rec
        print(f"  {name:10s} обновлён, годен до {rec.get('expires_at', '?')[:19]}")
    save(data, store)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
