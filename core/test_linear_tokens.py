"""Обновление токенов личностей.

Токен Linear живёт ровно сутки. До 15.09.2026 `refresh_token` выбрасывался
при авторизации, и каждые сутки все пять личностей умирали молча: вердикты
начинали подписываться общим ключом, то есть именем человека, и это было
видно только глазами.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import linear_tokens as lt
import personas


def _rec(hours_left=None, refresh="rt-1"):
    rec = {"access_token": "at-1", "scope": "read"}
    if refresh:
        rec["refresh_token"] = refresh
    if hours_left is not None:
        rec["expires_at"] = (datetime.now(timezone.utc)
                             + timedelta(hours=hours_left)).isoformat()
    return rec


class TestDue:
    def test_скоро_умрёт_значит_пора(self):
        assert lt.due({"qa": _rec(hours_left=2)}) == ["qa"]

    def test_запаса_хватает_не_трогаем(self):
        assert lt.due({"qa": _rec(hours_left=20)}) == []

    def test_срок_неизвестен_тоже_пора(self):
        """Лишний запрос дешевле мёртвой личности."""
        assert lt.due({"qa": {"access_token": "at", "refresh_token": "rt"}}) == ["qa"]

    def test_истёкший_пора(self):
        assert lt.due({"qa": _rec(hours_left=-1)}) == ["qa"]


class TestExpiry:
    def test_срок_считается_от_obtained_и_expires_in(self):
        """`expires_at` может не прийти — тогда считаем сами."""
        got = datetime.now(timezone.utc).isoformat()
        rec = {"obtained": got, "expires_in": 86399}
        exp = lt._expires_at(rec)
        assert exp and abs(exp - (time.time() + 86399)) < 5

    def test_битая_дата_не_роняет(self):
        assert lt._expires_at({"expires_at": "вчера"}) is None


class TestRefreshOne:
    def test_без_refresh_token_говорит_что_делать(self):
        rec, why = lt.refresh_one({"access_token": "at"}, "cid", "secret")
        assert rec is None
        assert "переавторизация" in why

    def test_новый_refresh_вытесняет_старый(self, monkeypatch):
        """Linear может вернуть новый refresh — старый после этого мёртв, и
        сохранить надо именно новый."""
        def fake(req, timeout=0):
            class R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s): return json.dumps(
                    {"access_token": "at-2", "refresh_token": "rt-2",
                     "expires_in": 86399}).encode()
            return R()
        monkeypatch.setattr(lt.urllib.request, "urlopen", fake)
        rec, why = lt.refresh_one(_rec(hours_left=1), "cid", "secret")
        assert why == ""
        assert rec["access_token"] == "at-2" and rec["refresh_token"] == "rt-2"
        assert rec["expires_at"] > datetime.now(timezone.utc).isoformat()

    def test_ответ_без_токена_не_портит_запись(self, monkeypatch):
        def fake(req, timeout=0):
            class R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s): return b'{"error":"invalid_grant"}'
            return R()
        monkeypatch.setattr(lt.urllib.request, "urlopen", fake)
        rec, why = lt.refresh_one(_rec(hours_left=1), "cid", "secret")
        assert rec is None and "без токена" in why


class TestStore:
    def test_запись_атомарна(self, tmp_path):
        """Обрыв посреди записи не должен оставить покалеченный файл."""
        p = tmp_path / "t.json"
        lt.save({"qa": _rec()}, p)
        assert json.loads(p.read_text())["qa"]["access_token"] == "at-1"
        assert not list(tmp_path.glob("*.tmp"))

    def test_битое_хранилище_читается_как_пустое(self, tmp_path):
        p = tmp_path / "t.json"
        p.write_text("{не json")
        assert lt.load(p) == {}


class TestPersonasReadStore:
    def test_хранилище_старше_окружения(self, tmp_path, monkeypatch):
        """В окружении ключ заморожен при старте раннера, в хранилище — тот,
        что обновил фоновый прогон. Иначе раннер сутками ходит мёртвым."""
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"qa": {"access_token": "свежий"}}))
        monkeypatch.setenv("AGENT_LINEAR_TOKENS", str(p))
        got = personas.keys_from_env({"LINEAR_API_KEY_QA": "старый"})
        assert got["qa"] == "свежий"

    def test_без_хранилища_работает_окружение(self, tmp_path, monkeypatch):
        """Инсталляция без обновления токенов должна работать как раньше."""
        monkeypatch.setenv("AGENT_LINEAR_TOKENS", str(tmp_path / "нет.json"))
        got = personas.keys_from_env({"LINEAR_API_KEY_QA": "из-окружения"})
        assert got["qa"] == "из-окружения"

    def test_пустой_токен_в_хранилище_не_вытесняет_живой(self, tmp_path, monkeypatch):
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"qa": {"access_token": "  "}}))
        monkeypatch.setenv("AGENT_LINEAR_TOKENS", str(p))
        got = personas.keys_from_env({"LINEAR_API_KEY_QA": "живой"})
        assert got["qa"] == "живой"


class TestClientCredentials:
    """Путь без браузера: токен на 30 дней по id и секрету приложения.

    Владелец 15.09.2026: «меня заебало это делать». Согласие в браузере — это
    ровно то, от чего избавляет `grant_type=client_credentials`: actor у него
    всё равно `app`, а срок втрое-тридцатикратно длиннее.
    """

    def _ответ(self, monkeypatch, payload):
        def fake(req, timeout=0):
            class R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def read(s): return json.dumps(payload).encode()
            return R()
        monkeypatch.setattr(lt.urllib.request, "urlopen", fake)

    def test_выпускает_без_согласия(self, monkeypatch):
        self._ответ(monkeypatch, {"access_token": "at-30", "expires_in": 2592000})
        rec, why = lt.mint("cid", "secret")
        assert why == "" and rec["access_token"] == "at-30"
        assert rec["выдан"] == "client_credentials"

    def test_срок_тридцать_дней_записан(self, monkeypatch):
        self._ответ(monkeypatch, {"access_token": "at", "expires_in": 2592000})
        rec, _ = lt.mint("cid", "secret")
        left = lt._expires_at(rec) - time.time()
        assert 29 * 86400 < left <= 30 * 86400

    def test_отказ_не_выдаёт_пустую_запись(self, monkeypatch):
        self._ответ(monkeypatch, {"error": "invalid_client"})
        rec, why = lt.mint("cid", "плохой")
        assert rec is None and "без токена" in why

    def test_неполная_пара_не_считается_клиентом(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({
            "qa": {"client_id": "id", "client_secret": "s"},
            "duty": {"client_id": "id"},
            "intake": {},
        }))
        assert sorted(lt.load_clients(p)) == ["qa"]

    def test_нет_файла_работаем_по_старому(self, tmp_path):
        assert lt.load_clients(tmp_path / "нет.json") == {}



class TestПрогонБезСрочных:
    """Прогон, которому нечего делать, обязан сказать об этом вслух.

    Молчащий таймер неотличим от несработавшего, а именно на этом личности
    и умирали незаметно.
    """

    def test_прогон_без_срочных_не_молчит(self, tmp_path, monkeypatch, capsys):
        годен_до = (datetime.now(timezone.utc) + timedelta(days=29)).isoformat()
        хранилище = tmp_path / "tokens.json"
        хранилище.write_text(json.dumps(
            {"qa": {"access_token": "at", "expires_at": годен_до}}))
        клиенты = tmp_path / "clients.json"
        клиенты.write_text(json.dumps(
            {"qa": {"client_id": "id", "client_secret": "secret"}}))
        # Путь к клиентам замораживается в константу при импорте,
        # поэтому окружением его уже не подменить.
        monkeypatch.setattr(lt, "CREDS", клиенты)
        monkeypatch.setattr("sys.argv",
                            ["linear_tokens.py", "--refresh", "--store", str(хранилище)])

        assert lt.main() == 0
        сказано = capsys.readouterr().out
        assert "обновлять некого" in сказано, "прогон промолчал"
