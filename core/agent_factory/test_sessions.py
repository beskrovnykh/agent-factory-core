"""Что фабрика делает сейчас и чего это стоило — по следам раннера.

Источник — рабочие каталоги сессий: `transcript.jsonl`, последняя запись
которого приходит от Claude Code со стоимостью и токенами. Это не оценка, а
то, что выставил счётчик, поэтому и читаем его, а не считаем сами.
"""
import json
import os
import time

from agent_factory.sessions import human_left, human_tokens, summary, walk

RESULT = {"type": "result", "total_cost_usd": 0.54, "num_turns": 12,
          "duration_ms": 104176,
          "usage": {"input_tokens": 16, "output_tokens": 6167,
                    "cache_read_input_tokens": 216539,
                    "cache_creation_input_tokens": 27931}}


def session(root, name, *, done=True, old=False, lines=3, result=None):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "transcript.jsonl"), "w") as f:
        for i in range(lines):
            f.write(json.dumps({"type": "assistant", "n": i}) + "\n")
        if done:
            f.write(json.dumps({**RESULT, **(result or {})}) + "\n")
    if old:
        was = time.time() - 5 * 3600
        os.utime(os.path.join(d, "transcript.jsonl"), (was, was))
    return d


def test_a_finished_session_reports_what_it_cost(tmp_path):
    session(str(tmp_path), "DEV-20-20260912-150816")
    got = walk(str(tmp_path))
    assert len(got) == 1
    s = got[0]
    assert s["task"] == "DEV-20" and s["идёт"] is False
    assert s["стоимость"] == 0.54 and s["ходов"] == 12 and s["секунд"] == 104
    assert s["вход"] == 16 and s["выход"] == 6167
    assert s["кэш"] == 216539 + 27931


def test_a_running_session_has_no_result_yet(tmp_path):
    session(str(tmp_path), "DEV-21-20260912-160000", done=False)
    s = walk(str(tmp_path))[0]
    assert s["идёт"] is True and s["брошена"] is False and s["стоимость"] == 0


def test_an_abandoned_session_is_not_counted_as_running(tmp_path):
    """Раннер снял её по таймауту, файл остался. Показывать такую как идущую
    значит вечно держать в шапке единицу, которой нет."""
    session(str(tmp_path), "DEV-22-20260912-010000", done=False, old=True)
    s = walk(str(tmp_path))[0]
    assert s["идёт"] is False and s["брошена"] is True


def test_the_summary_keeps_cache_apart(tmp_path):
    """Кэш и есть основная масса токенов: смешав его с входом, получаем число,
    по которому ничего не видно."""
    session(str(tmp_path), "DEV-20-20260912-150816")
    session(str(tmp_path), "DEV-23-20260912-160000")
    out = summary(walk(str(tmp_path)))
    assert out["закончено"] == 2 and out["идёт"] == 0
    assert out["вход"] == 32 and out["выход"] == 12334
    assert out["кэш"] == 2 * (216539 + 27931)
    assert out["стоимость"] == 1.08


def test_only_sessions_since_the_boundary(tmp_path):
    d = session(str(tmp_path), "DEV-24-20260901-000000")
    was = time.time() - 48 * 3600
    os.utime(d, (was, was))
    # ctime каталога подменить нельзя, поэтому проверяем обратное: граница в
    # будущем не пропускает ничего.
    assert walk(str(tmp_path), since=time.time() + 100) == []


def test_a_missing_work_root_is_not_a_crash(tmp_path):
    assert walk(str(tmp_path / "нет-такого")) == []


def test_a_truncated_transcript_is_not_a_crash(tmp_path):
    d = os.path.join(str(tmp_path), "DEV-25-20260912-170000")
    os.makedirs(d)
    with open(os.path.join(d, "transcript.jsonl"), "w") as f:
        f.write('{"type": "result", "total_cost')
    s = walk(str(tmp_path))[0]
    assert s["стоимость"] == 0 and s["идёт"] is True


def test_a_huge_transcript_is_read_from_the_end(tmp_path):
    # Файл — сотни килобайт, а нужна последняя строка.
    d = os.path.join(str(tmp_path), "DEV-26-20260912-180000")
    os.makedirs(d)
    with open(os.path.join(d, "transcript.jsonl"), "w") as f:
        f.write(json.dumps({"type": "assistant", "text": "х" * 500_000}) + "\n")
        f.write(json.dumps(RESULT) + "\n")
    assert walk(str(tmp_path))[0]["стоимость"] == 0.54


def test_human_numbers_are_readable():
    assert human_tokens(216539) == "216 тыс." and human_tokens(16) == "16"
    assert human_tokens(2_400_000) == "2.4 млн"
    assert human_left(0) == "можно сейчас" and human_left(30) == "меньше минуты"
    assert human_left(420) == "7 мин" and human_left(4800) == "1 ч 20 мин"


def test_resources_come_from_the_runners_file(tmp_path):
    """Память и слот пишет раннер: в итоге от Claude Code их нет — он про свою
    сторону, а память и слот про нашу."""
    import json as j
    d = session(str(tmp_path), "DEV-20-20260912-150816")
    with open(os.path.join(d, "stats.json"), "w") as f:
        f.write(j.dumps({"задача": "DEV-20", "род": "работа", "слот": 2,
                         "секунд": 104, "пик_мб": 482, "код": 0}))
    s = walk(str(tmp_path))[0]
    assert s["память"] == 482 and s["слот"] == 2 and s["род"] == "работа"
    assert summary([s])["пик_мб"] == 482


def test_a_session_without_the_file_is_still_counted(tmp_path):
    """Файл появился 12.09.2026 — у сессий до него его нет, и это не повод
    прятать их из статистики."""
    session(str(tmp_path), "DEV-19-20260911-100000")
    s = walk(str(tmp_path))[0]
    assert s["память"] == 0 and s["род"] == "работа" and s["стоимость"] == 0.54


def test_the_kind_is_guessed_from_the_name_when_there_is_no_file(tmp_path):
    session(str(tmp_path), "DEV-62-verify-20260912-030351")
    session(str(tmp_path), "INTAKE-abc-intake-20260912-020000")
    kinds = {s["task"]: s["род"] for s in walk(str(tmp_path))}
    assert kinds["DEV-62"] == "проверка"
    assert kinds["INTAKE-abc"] == "приём"


# ── причина остановки: словами поставщика, а не нашим выводом по rc ──────────

def test_a_normal_end_leaves_no_stop_reason(tmp_path):
    """Штатный конец не пишем: строка «end_turn» в карточке учёта ничего не
    добавляет, а глазу мешает отличить её от настоящей аварии."""
    session(str(tmp_path), "DEV-20-20260912-150816",
            result={"subtype": "success", "stop_reason": "end_turn"})
    assert walk(str(tmp_path))[0]["остановка"] == ""


def test_a_session_that_ran_out_of_turns_says_so(tmp_path):
    """`rc` про это молчит: упёршаяся в ходы сессия возвращает тот же ноль,
    что и дошедшая до конца. Разницу знает только поставщик."""
    session(str(tmp_path), "DEV-21-20260912-150816",
            result={"subtype": "error_max_turns", "stop_reason": "end_turn"})
    assert walk(str(tmp_path))[0]["остановка"] == "error_max_turns"


def test_an_unusual_stop_reason_of_the_last_answer_is_kept(tmp_path):
    session(str(tmp_path), "DEV-22-20260912-150816",
            result={"subtype": "success", "stop_reason": "max_tokens"})
    assert walk(str(tmp_path))[0]["остановка"] == "max_tokens"


def test_a_running_session_has_no_stop_reason(tmp_path):
    session(str(tmp_path), "DEV-23-20260912-160000", done=False)
    assert walk(str(tmp_path))[0]["остановка"] == ""
