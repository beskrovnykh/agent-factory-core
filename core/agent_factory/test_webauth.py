"""Кто открыл страницу — по подписи Telegram, а не по знанию ссылки.

Токен в ссылке значит «кто знает адрес, тот внутри»: ссылку пересылают,
копируют в браузер на чужом компьютере, она остаётся в истории. Подпись
подделать нельзя, не зная ключа бота, — на этом и держится проверка.
"""
import hashlib
import hmac
import json
import time
import urllib.parse

from agent_factory.webauth import MAX_AGE_SEC, allowed, verify

BOT = "123456:AAHf-secret"
ME = 1000000001


def signed(user_id=ME, token=BOT, age=0, name="Андрей", **extra):
    user = json.dumps({"id": user_id, "first_name": name},
                      separators=(",", ":"), ensure_ascii=False)
    fields = {"user": user, "auth_date": str(int(time.time()) - age),
              "query_id": "AAH", **extra}
    line = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, line.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def test_a_real_signature_names_the_user():
    got = verify(signed(), BOT)
    assert got and got["id"] == ME and got["first_name"] == "Андрей"


def test_a_signature_from_another_bot_is_refused():
    """Ключ бота — единственное, чего нет у чужого. На нём всё и держится."""
    assert verify(signed(token="999:OTHER"), BOT) is None


def test_tampering_with_the_user_breaks_the_signature():
    data = signed()
    spoiled = data.replace(str(ME), "1")
    assert verify(spoiled, BOT) is None


def test_an_added_field_breaks_the_signature():
    assert verify(signed() + "&admin=1", BOT) is None


def test_data_without_a_hash_is_refused():
    fields = urllib.parse.parse_qs(signed())
    del fields["hash"]
    assert verify(urllib.parse.urlencode(fields, doseq=True), BOT) is None


def test_old_data_stops_working():
    """Подписанная строка, утекшая из истории браузера, не должна пускать
    вечно."""
    assert verify(signed(age=MAX_AGE_SEC + 60), BOT) is None
    assert verify(signed(age=60), BOT) is not None


def test_a_non_ascii_hash_is_refused_not_crashed():
    # Тот же класс, что стоил 500 вместо 401 на вебхуке 12.09.2026.
    data = urllib.parse.parse_qs(signed())
    data["hash"] = ["подделка"]
    assert verify(urllib.parse.urlencode(data, doseq=True), BOT) is None


def test_without_a_bot_token_nothing_is_verified():
    assert verify(signed(), "") is None


def test_empty_input_is_refused():
    assert verify("", BOT) is None and verify("не=данные", BOT) is None


# ── кого пускаем ─────────────────────────────────────────────────────────

def test_only_the_named_are_let_in():
    me = verify(signed(), BOT)
    assert allowed(me, {ME}) is True
    assert allowed(me, {"1000000001"}) is True      # id строкой — тот же человек
    assert allowed(me, {12345}) is False


def test_an_empty_list_lets_nobody_in():
    """Инсталляция, которая включила проверку и забыла перечислить людей,
    должна не пускать, а не пускать всех."""
    assert allowed(verify(signed(), BOT), set()) is False


def test_nobody_is_not_somebody():
    assert allowed(None, {ME}) is False


def test_the_signature_field_is_part_of_what_was_signed():
    """`signature` приезжает рядом с `hash` для сторонней проверки, но в
    подпись Telegram его ВКЛЮЧАЕТ. Выбросив его, считаем хеш от другой строки
    — и отказываем всем при верной подписи (12.09.2026, «вход отклонён:
    неизвестно»)."""
    data = signed(signature="deadbeef_ed25519")
    got = verify(data, BOT)
    assert got and got["id"] == ME


def test_only_the_hash_is_left_out_of_the_check_string():
    from agent_factory.webauth import check_string
    line = check_string({"b": "2", "hash": "x", "signature": "s", "a": "1"})
    assert line == "a=1\nb=2\nsignature=s"
