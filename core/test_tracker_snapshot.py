"""Снимок задачи смотрит на СВЕЖИЕ комментарии, а не на самые старые.

Linear отдаёт связи новыми вперёд, и `comments(last:5)` — это пять самых
старых. Проба по живому API 15.09.2026, DEV-4 с 18 комментариями:

    last:5   → 2026-09-07T21:34 … 2026-09-13T21:16
    first:5  → 2026-09-14T02:19 … 2026-09-13T23:59

Досдача (`ensure_handoff`) считала отчётом сессии комментарий новее её
старта — и на любой задаче, набравшей больше пяти комментариев, не видела
его никогда: в ночь 14.09 семь сессий подряд «не оставили комментария» ровно
на задачах со вторым-третьим заходом, хотя в трассах комментарий записан и
трекер ответил `success`. Восемь из восемнадцати досдач client-b за 06–14.09
— этот баг, а не сессии (DEV-95).
"""
import re

from tracker import LinearAdapter


def snapshot_query():
    a = LinearAdapter("ключ", "https://example.invalid")
    seen = []
    a._graphql = lambda q, v: (seen.append(q), {})[1]
    a.fetch_issue_snapshot("issue-1")
    return seen[0]


def test_the_snapshot_asks_for_the_newest_comments():
    q = snapshot_query()
    assert re.search(r"comments\(first:\d+\)", q), q
    assert "comments(last:" not in q


def test_the_window_is_wide_enough_for_one_session_and_its_neighbours():
    """Своих комментариев у сессии до трёх (взялся, критерий, сдача), плюс
    ревью, плюс вердикты проверки того же вечера. Пять — впритык, десять
    — с запасом; больше — лишний трафик на каждую сдачу."""
    assert 6 <= LinearAdapter.SNAPSHOT_COMMENTS <= 20
