"""Прозрачная сессия: находится по имени, читается по своим записям."""
import json
import os

import transparent


def _state(tmp_path, pid, name, sid, bridge="", cwd="/repo"):
    d = tmp_path / "sessions"
    d.mkdir(exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"name": name, "sessionId": sid, "bridgeSessionId": bridge,
         "jobId": "abc123", "cwd": cwd}), encoding="utf-8")
    return str(d)


def _transcript(tmp_path, sid, events):
    d = tmp_path / "projects" / "-repo"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
        encoding="utf-8")
    return str(tmp_path / "projects")


def test_a_session_is_found_by_the_name_a_human_sees(tmp_path, monkeypatch):
    monkeypatch.setattr(transparent, "SESSIONS_DIR",
                        _state(tmp_path, "111", "DEV-1 · работа", "sid-1"))
    monkeypatch.setattr(transparent, "PROJECTS_DIR", _transcript(tmp_path, "sid-1", []))
    found = transparent.find("DEV-1 · работа")
    assert found and found.session_id == "sid-1"
    assert transparent.find("чужое имя") is None


def test_the_newest_wins_when_names_repeat(tmp_path, monkeypatch):
    d = _state(tmp_path, "111", "DEV-1 · работа", "старая")
    _state(tmp_path, "222", "DEV-1 · работа", "новая")
    os.utime(os.path.join(d, "222.json"), (2 ** 31, 2 ** 31))
    monkeypatch.setattr(transparent, "SESSIONS_DIR", d)
    monkeypatch.setattr(transparent, "PROJECTS_DIR", str(tmp_path / "projects"))
    assert transparent.find("DEV-1 · работа").session_id == "новая"


def test_the_transcript_is_found_by_id_not_by_path(tmp_path, monkeypatch):
    """17.09: искали по каталогу рабочей копии и не нашли живую сессию —
    путь меняется, когда она уходит в свой worktree, а идентификатор нет."""
    monkeypatch.setattr(transparent, "PROJECTS_DIR",
                        _transcript(tmp_path, "sid-7", [{"type": "assistant"}]))
    assert transparent.transcript_of("sid-7").endswith("sid-7.jsonl")
    assert transparent.transcript_of("чужой") == ""


def test_money_and_pr_are_read_from_the_sessions_own_words(tmp_path, monkeypatch):
    """У прозрачной сессии нет события итога — деньги и PR лежат в её
    собственных записях, другими словами, чем у `claude -p`."""
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "делаю"}]}},
        {"type": "cost-state", "totalCostUSD": 7.68, "totalLinesAdded": 87},
        {"type": "pr-link", "prNumber": 65, "prUrl": "https://example/pull/65"},
        {"type": "bridge-session", "bridgeSessionId": "cse_ABC"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "готово"}]}},
    ]
    monkeypatch.setattr(transparent, "PROJECTS_DIR", _transcript(tmp_path, "s", events))
    s = transparent.Session(name="n", session_id="s")
    p = transparent.progress(s)
    assert p.turns == 2 and p.said == "готово"
    assert p.cost_usd == 7.68 and p.lines_added == 87
    assert p.pr_url.endswith("/65")
    assert s.link == "https://claude.ai/code/session_ABC"


def test_a_session_without_a_bridge_has_no_link_yet():
    """Пока регистрации в облаке нет, ссылки нет — и врать её нельзя."""
    assert transparent.Session(name="n").link == ""


def test_the_shim_streams_the_transcript_and_adds_the_ending(tmp_path, monkeypatch):
    """Обёртка отдаёт строки транскрипта как поток и дописывает итог.

    У прозрачной сессии события итога не бывает — она не заканчивается, а
    встаёт ждать. Без дописанного итога раннер счёл бы её оборванной.
    """
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "делаю"}]}},
        {"type": "cost-state", "totalCostUSD": 2.5, "totalLinesAdded": 10},
    ]
    monkeypatch.setattr(transparent, "PROJECTS_DIR", _transcript(tmp_path, "s", events))
    monkeypatch.setattr(transparent, "POLL_SEC", 0.01)
    s = transparent.Session(name="n", session_id="s", pid="0")  # pid 0 → не жива
    shim = transparent.Attached(s)
    out = [json.loads(x) for x in shim.stdout]
    assert [e["type"] for e in out] == ["assistant", "cost-state", "result"]
    end = out[-1]
    assert end["subtype"] == "success" and end["is_error"] is False
    assert end["total_cost_usd"] == 2.5 and end["num_turns"] == 1
    assert end["почему"] == "ушла"
    assert shim.returncode == 0


def test_silence_longer_than_the_threshold_counts_as_an_ending(tmp_path, monkeypatch):
    """Сессия, задавшая вопрос, жива — но больше ничего не сделает, пока
    человек не ответит. Для раннера это конец захода, а не обрыв."""
    import os as _os
    root = _transcript(tmp_path, "s", [{"type": "assistant", "message": {"content": []}}])
    monkeypatch.setattr(transparent, "PROJECTS_DIR", root)
    monkeypatch.setattr(transparent, "POLL_SEC", 0.01)
    path = transparent.transcript_of("s")
    _os.utime(path, (1, 1))  # молчит с 1970 года
    s = transparent.Session(name="n", session_id="s", pid=str(_os.getpid()))  # жива
    shim = transparent.Attached(s, idle_sec=60)
    out = [json.loads(x) for x in shim.stdout]
    assert out[-1]["почему"] == "ждёт"


def test_the_shim_offers_only_what_the_runner_asks_of_a_process():
    """Раннеру от процесса нужны ровно pid, stdout, wait и kill."""
    s = transparent.Session(name="n", session_id="s")
    shim = transparent.Attached(s)
    for attr in ("pid", "stdout", "wait", "kill"):
        assert hasattr(shim, attr), attr


def test_a_transparent_session_is_never_killed_by_process_group(monkeypatch):
    """Группа у прозрачной общая с демоном и соседями: убивать по ней нельзя.

    Без этой развилки снятие одной сессии уносило бы чужие — и это не теория,
    а то, как устроен демон фоновых сессий.
    """
    import runner
    убито = {}
    monkeypatch.setattr(runner.os, "killpg",
                        lambda *a: убито.setdefault("группа", True))
    s = transparent.Session(name="n", session_id="s", job_id="j")
    shim = transparent.Attached(s)
    monkeypatch.setattr(transparent, "stop", lambda *a, **k: убито.setdefault("своя", True))
    runner._kill_session(shim, pgid=4242)
    assert убито == {"своя": True}, "прозрачную сняли по группе процессов"


def test_the_persona_key_and_the_ceilings_reach_the_transparent_session():
    """Флаги обычной сессии обязаны доехать до прозрачной.

    Без `--mcp-config` сессия подписывается учёткой хоста — так уже ломалось
    в DEV-66, и комментарии приходили от имени владельца. Без потолков ходов
    и денег у сессии нет ни конца, ни цены: одна такая стоила $7.68.
    """
    import runner
    cmd = ["/bin/claude", "-p", "текст задачи",
           "--mcp-config", "/tmp/mcp.json",
           "--resume", "старая",
           "--model", "opus",
           "--dangerously-skip-permissions",
           "--max-turns", "150",
           "--max-budget-usd", "12",
           "--output-format", "stream-json", "--verbose"]
    got = runner._transparent_extra(cmd)
    assert "--mcp-config" in got and "/tmp/mcp.json" in got
    assert "--dangerously-skip-permissions" in got
    assert "--max-turns" in got and "150" in got
    assert "--max-budget-usd" in got and "12" in got
    # своё у прозрачной — промпт, поток и возобновление
    for gone in ("-p", "текст задачи", "--output-format", "stream-json",
                 "--verbose", "--resume", "старая", "--model", "opus",
                 "/bin/claude"):
        assert gone not in got, gone


def test_an_unknown_pid_is_never_zero():
    """Ноль для getpgid и счётчика памяти — это ТЕКУЩИЙ процесс, то есть сам
    раннер. Неизвестный pid должен быть заведомо ничейным."""
    assert transparent.Attached(transparent.Session(name="n")).pid == -1


def test_waiting_ends_on_silence_even_if_nobody_reads_the_stream(tmp_path, monkeypatch):
    """Сессия, задавшая вопрос, жива — и если читатель потока встал, ждать её
    до самого срока нельзя."""
    import os as _os
    monkeypatch.setattr(transparent, "PROJECTS_DIR",
                        _transcript(tmp_path, "s", [{"type": "assistant"}]))
    monkeypatch.setattr(transparent, "POLL_SEC", 0.01)
    _os.utime(transparent.transcript_of("s"), (1, 1))
    s = transparent.Session(name="n", session_id="s", pid=str(_os.getpid()))
    assert transparent.Attached(s, idle_sec=60).wait(timeout=5) == 0


def test_a_session_that_failed_to_start_is_never_silent():
    """Отказ запуска обязан доехать до человека.

    17.09 DEV-127 исчезла с доски молча: сессия не поднялась, раннер написал
    строку в журнал и вышел. Задача осталась на агенте, у которого нет
    входящих, — и про неё никто бы не узнал.
    """
    import inspect
    import runner
    src = inspect.getsource(runner.run_session)
    head = src[src.index("if proc is None:"):]
    head = head[:head.index("return")]
    assert "linear_comment" in head, "отказ запуска молчит в задачу"
    assert "linear_assign_to_reviewer" in head, "задача остаётся на агенте"


def test_a_session_that_stands_waiting_calls_the_human():
    """Сессия, вставшая за словом человека, обязана его позвать.

    17.09: она жива, помнит всё и продолжит с того же места — но через
    несколько минут её снимало подметание, и человек не узнавал, что его
    ждали. Молчание тут дороже всего: сессия умирает, работа пропадает,
    на доске ничего.
    """
    import inspect
    import runner
    src = inspect.getsource(runner.run_session)
    i = src.index('== "ждёт"')
    block = src[i:i + 900]
    assert "linear_comment" in block, "вставшая сессия молчит в задачу"
    assert "linear_assign_to_reviewer" in block, "задача не уходит человеку"
