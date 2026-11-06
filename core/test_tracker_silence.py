"""Отказ трекера обязан быть слышен.

Комментарий в задачу — это вердикт проверки или отчёт сессии о работе. Его
пропажа неотличима от «сессия ничего не делала»: на доске задача выглядит
непроверенной, в журнале ни строки, и никто не идёт разбираться.

Молчание здесь было двойным, и обе половины ловятся тестами ниже:

* Linear отвечает на ПРИКЛАДНОЙ отказ кодом 200 и полем `errors` — нет прав,
  чужая задача, неверный id, узкий скоуп у личности. Сетевая ошибка ловилась,
  эта — нет;
* сама мутация возвращает `success`, и он не проверялся вовсе.

Найдено вердиктом проверки по DEV-66: «отказ трекера в post_comment
по-прежнему молчит (проверено пробой)».
"""
import json
import logging

import pytest

from tracker import LinearAdapter

def adapter(reply, monkeypatch):
    a = LinearAdapter("ключ", "https://example.invalid")
    monkeypatch.setattr(a, "_graphql", lambda q, v: reply)
    return a

def test_a_refused_mutation_is_not_reported_as_written(monkeypatch):
    """`success: false` — отказ уровня мутации. Раньше он терялся целиком."""
    a = adapter({"data": {"commentCreate": {"success": False}}}, monkeypatch)
    assert a.post_comment("issue-1", "вердикт") is False

def test_a_written_comment_says_so(monkeypatch):
    a = adapter({"data": {"commentCreate": {"success": True}}}, monkeypatch)
    assert a.post_comment("issue-1", "вердикт") is True

def test_an_application_level_refusal_is_not_silence(monkeypatch):
    """Код 200 и поле `errors` — самый частый отказ у трекера, и самый тихий:
    сетевого сбоя нет, исключения нет, ответ формально успешен."""
    a = adapter({"errors": [{"message": "Access denied"}]}, monkeypatch)
    assert a.post_comment("issue-1", "вердикт") is False

def test_the_lost_text_is_named_in_the_log(monkeypatch, caplog):
    """В журнале должно быть видно, ЧТО потерялось, а не только что «не
    вышло»: по номеру задачи вердикт восстанавливают руками."""
    a = adapter({"data": {"commentCreate": {"success": False}}}, monkeypatch)
    with caplog.at_level(logging.ERROR):
        a.post_comment("issue-42", "вердикт на сто знаков")
    assert "issue-42" in caplog.text
    assert "НЕ записан" in caplog.text

def test_errors_from_the_tracker_reach_the_log(monkeypatch, caplog):
    """Текст отказа трекера нужен целиком: «нет прав» и «нет такой задачи»
    чинятся по-разному, а без сообщения они неразличимы."""
    calls = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"errors": [{"message": "Entity not found: Issue"}]}).encode()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Resp())
    a = LinearAdapter("ключ", "https://example.invalid")
    with caplog.at_level(logging.ERROR):
        ok = a.post_comment("issue-нет", "вердикт")
    assert ok is False
    assert "Entity not found" in caplog.text
