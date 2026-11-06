"""Прозрачная сессия: та, которую человек видит в приложении и может войти.

Раннер запускает сессии `claude -p` и читает их поток `stream-json`: оттуда он
берёт ходы, деньги и событие итога. Такая сессия человеку не видна — проверено
17.09.2026: `-p` вместе с `--remote-control` регистрации в облаке не даёт,
`bridgeSessionId` остаётся пустым.

Видимость даёт `--bg --remote-control`. Но эта сессия устроена иначе:

  • отвязывается сразу — `Popen` возвращает управление, потока нет;
  • не заканчивается, а переходит в ожидание: события итога не бывает;
  • пишет СВОЙ транскрипт, и там есть всё то же, только другими словами —
    `bridge-session` (ссылка), `pr-link` (номер PR), `cost-state` (деньги и
    строки), `worktree-state` (своя рабочая копия).

Отсюда этот модуль: поднять такую сессию, найти её по имени и следить за ней
по её же записям. Имя — то, что человек увидит в списке приложения, поэтому
оно и есть сообщение: «DEV-115 · работа».

Связь с PR прозрачная сессия записывает сама (`pr-link`) — значит поднять её
обратно можно и по номеру PR, не только по идентификатору.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# Как часто спрашивать транскрипт. Реже — дешевле, но дольше замечаем конец;
# чаще — лишние чтения файла, который растёт до мегабайтов.
POLL_SEC = 5.0
# Сколько ждём сам запуск и сколько — пока сессия назовётся своим именем.
START_TIMEOUT_SEC = 120.0
NAME_WAIT_SEC = 60.0


@dataclass
class Session:
    """Живая прозрачная сессия — то, что о ней известно снаружи."""

    name: str
    job_id: str = ""
    pid: str = ""
    session_id: str = ""
    bridge: str = ""
    transcript: str = ""
    worktree: str = ""

    @property
    def link(self) -> str:
        """Адрес, по которому человек войдёт. Пусто — регистрации ещё нет."""
        if not self.bridge:
            return ""
        return "https://claude.ai/code/" + self.bridge.replace("cse_", "session_")

    @property
    def alive(self) -> bool:
        return bool(self.pid) and os.path.isdir(f"/proc/{self.pid}")


@dataclass
class Progress:
    """Что сессия уже сделала — из её собственных записей."""

    turns: int = 0
    cost_usd: float = 0.0
    lines_added: int = 0
    pr_url: str = ""
    said: str = ""
    idle_sec: int = 0
    events: dict = field(default_factory=dict)


def _state_files() -> list[str]:
    try:
        return [os.path.join(SESSIONS_DIR, f) for f in os.listdir(SESSIONS_DIR)
                if f.endswith(".json")]
    except OSError:
        return []


def find(name: str) -> Session | None:
    """Сессия по имени — свежайшая, если тёзки.

    Имя, а не идентификатор: его задаёт раннер при запуске, и по нему же
    человек находит сессию в приложении. Идентификатор становится известен
    только после старта.
    """
    best: tuple[float, Session] | None = None
    for path in _state_files():
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        if data.get("name") != name:
            continue
        stamp = os.path.getmtime(path)
        if best and stamp <= best[0]:
            continue
        s = Session(
            name=name,
            job_id=str(data.get("jobId") or ""),
            pid=os.path.basename(path)[:-5],
            session_id=str(data.get("sessionId") or ""),
            bridge=str(data.get("bridgeSessionId") or ""),
            worktree=str(data.get("cwd") or ""),
        )
        s.transcript = transcript_of(s.session_id)
        best = (stamp, s)
    return best[1] if best else None


def transcript_of(session_id: str) -> str:
    """Транскрипт ищем по идентификатору, а не по пути.

    Путь меняется, когда сессия уходит в свою рабочую копию, а идентификатор
    нет — и имя файла транскрипта это он и есть. 17.09 поиск по пути не нашёл
    транскрипт живой и работающей сессии.
    """
    if not session_id:
        return ""
    try:
        folders = os.listdir(PROJECTS_DIR)
    except OSError:
        return ""
    for folder in folders:
        path = os.path.join(PROJECTS_DIR, folder, f"{session_id}.jsonl")
        if os.path.exists(path):
            return path
    return ""


def progress(session: Session) -> Progress:
    """Прочитать транскрипт целиком и сказать, что сделано.

    Целиком, а не хвостом: строки в нём разной длины, а события, которые нас
    интересуют (деньги, PR, мост), пишутся по одному разу и могут лежать в
    начале. Файл читается раз в несколько секунд — дешевле, чем городить
    смещения и ошибиться на перезаписи.
    """
    out = Progress()
    path = session.transcript or transcript_of(session.session_id)
    if not path or not os.path.exists(path):
        return out
    session.transcript = path
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        kind = ev.get("type")
        out.events[kind] = out.events.get(kind, 0) + 1
        if kind == "assistant":
            out.turns += 1
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, list):
                said = " ".join(c.get("text", "") for c in content
                                if isinstance(c, dict) and c.get("type") == "text")
                if said.strip():
                    out.said = said.strip()
        elif kind == "cost-state":
            out.cost_usd = float(ev.get("totalCostUSD") or out.cost_usd)
            out.lines_added = int(ev.get("totalLinesAdded") or out.lines_added)
        elif kind == "pr-link":
            out.pr_url = str(ev.get("prUrl") or out.pr_url)
        elif kind == "bridge-session":
            session.bridge = str(ev.get("bridgeSessionId") or session.bridge)
    try:
        out.idle_sec = int(time.time() - os.path.getmtime(path))
    except OSError:
        pass
    return out


def start(name: str, prompt: str, cwd: str, claude_bin: str = "claude",
          model: str = "", env: dict | None = None,
          resume: str = "", extra: list[str] | None = None) -> Session | None:
    """Поднять прозрачную сессию и вернуть её, когда она назовётся.

    `extra` — флаги, которые раннер уже собрал для обычной сессии: ключ
    личности (`--mcp-config`), потолки ходов и денег, разрешения. Их нельзя
    собирать здесь заново: без ключа личности сессия подпишется учёткой хоста
    (так уже ломалось в DEV-66), а без потолков у неё нет ни конца, ни цены.

    `claude --bg` печатает короткий номер работы и возвращает управление —
    ждать его выхода нельзя, он и не собирается жить. Поэтому после запуска
    ждём, пока сессия объявится файлом состояния под своим именем.
    """
    cmd = [claude_bin, "--bg", "--remote-control", "-n", name]
    cmd += list(extra or ())
    if resume:
        cmd += ["--resume", resume]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=START_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        # Запуск не должен ронять заход: слот освободится, задача вернётся.
        return None
    if proc.returncode != 0:
        return None
    deadline = time.time() + NAME_WAIT_SEC
    while time.time() < deadline:
        found = find(name)
        if found:
            return found
        time.sleep(1.0)
    return None


def wait(session: Session, timeout_sec: float, idle_sec: float = 0.0,
         tick=None) -> tuple[str, Progress]:
    """Дождаться, пока сессия кончит или встанет ждать человека.

    Возвращает причину остановки и то, что она успела: `ушла` — процесса
    больше нет, `ждёт` — молчит дольше `idle_sec` (значит спрашивает человека
    и ответа нет), `срок` — вышло время.

    `ждёт` не провал: прозрачная сессия не заканчивается, а останавливается, и
    отличить «сделала и стоит» от «спрашивает» можно только по её словам.
    """
    deadline = time.time() + timeout_sec
    last = Progress()
    while time.time() < deadline:
        last = progress(session)
        if tick:
            tick(session, last)
        if not session.alive:
            return "ушла", last
        if idle_sec and last.idle_sec >= idle_sec:
            return "ждёт", last
        time.sleep(POLL_SEC)
    return "срок", last


def stop(session: Session, claude_bin: str = "claude") -> bool:
    """Потушить сессию. Транскрипт остаётся — её можно поднять обратно."""
    if not session.job_id:
        return False
    done = subprocess.run([claude_bin, "stop", session.job_id],
                          capture_output=True, text=True, timeout=60)
    return done.returncode == 0


class Attached:
    """Прозрачная сессия, притворяющаяся процессом с потоком.

    Раннеру от процесса нужно ровно четыре вещи: `pid`, `stdout`, `wait` и
    `kill`. Прозрачная сессия даёт первое и последнее сама, а поток мы
    собираем из её транскрипта — события `assistant` там той же формы, что у
    `claude -p`, так что переводить почти нечего.

    Чего у неё нет — события итога: она не заканчивается, а встаёт ждать.
    Поэтому итог дописываем сами, когда она ушла или замолчала, и берём числа
    из `cost-state`. Без этого раннер считал бы сессию оборванной.
    """

    def __init__(self, session: Session, idle_sec: float = 0.0,
                 claude_bin: str = "claude"):
        self.session = session
        self.idle_sec = idle_sec
        self.claude_bin = claude_bin
        self.returncode: int | None = None
        self._ended = ""

    @property
    def pid(self) -> int:
        """Ноль здесь опасен: для `getpgid` и счётчика памяти это ТЕКУЩИЙ
        процесс, то есть сам раннер. Неизвестный pid отдаём как -1 — на него
        оба просто ничего не найдут."""
        try:
            return int(self.session.pid)
        except (TypeError, ValueError):
            return -1

    @property
    def stdout(self):
        """Строки транскрипта по мере появления, в конце — событие итога."""
        return self._lines()

    def _lines(self):
        """Хвост транскрипта по мере роста.

        Дочитываем со смещения, а не читаем файл целиком каждый раз: за час
        работы транскрипт вырастает до мегабайтов (видел девятимегабайтный), и
        полное чтение раз в несколько секунд — квадрат от длины сессии.
        """
        path = self.session.transcript or transcript_of(self.session.session_id)
        offset = 0
        while True:
            if path and os.path.exists(path):
                self.session.transcript = path
                with open(path, encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    for line in fh:
                        if line.strip():
                            yield line
                    offset = fh.tell()
            else:
                path = transcript_of(self.session.session_id)

            if not self.session.alive:
                self._ended = "ушла"
                break
            if self.idle_sec and path and os.path.exists(path):
                quiet = time.time() - os.path.getmtime(path)
                if quiet >= self.idle_sec:
                    self._ended = "ждёт"
                    break
            time.sleep(POLL_SEC)

        yield json.dumps(self._result(), ensure_ascii=False) + "\n"
        self.returncode = 0

    def _result(self) -> dict:
        """Событие итога — то, которого у прозрачной сессии не бывает.

        Форма как у `claude -p`, чтобы разбор потока не знал о разнице;
        `почему` — наша добавка: «ушла» или «ждёт».
        """
        done = progress(self.session)
        return {"type": "result", "subtype": "success",
                "is_error": False, "num_turns": done.turns,
                "total_cost_usd": done.cost_usd,
                "session_id": self.session.session_id,
                "почему": self._ended or "ушла",
                "ссылка": self.session.link, "pr": done.pr_url}

    def wait(self, timeout: float | None = None) -> int:
        """Ждать конца так же, как ждут процесс.

        Конец — это уход процесса ИЛИ молчание дольше порога: прозрачная
        сессия, задавшая вопрос, живее всех живых, но делать больше ничего не
        будет, пока человек не ответит.
        """
        deadline = time.time() + (timeout if timeout is not None else 1e9)
        while time.time() < deadline:
            if self.returncode is not None:
                return self.returncode
            if not self.session.alive:
                self.returncode = 0
                return 0
            # Молчание распознаём ЗДЕСЬ, а не только в потоке: иначе сессия,
            # ждущая человека, провисит до самого срока, если читатель потока
            # почему-либо встал.
            if self.idle_sec and self._quiet_for() >= self.idle_sec:
                self.returncode = 0
                return 0
            time.sleep(min(POLL_SEC, 1.0))
        raise subprocess.TimeoutExpired(cmd="claude --bg", timeout=timeout or 0)

    def _quiet_for(self) -> float:
        """Сколько сессия молчит. Нет транскрипта — считаем, что не молчит."""
        path = self.session.transcript or transcript_of(self.session.session_id)
        try:
            return time.time() - os.path.getmtime(path)
        except (OSError, TypeError):
            return 0.0

    def kill(self) -> None:
        stop(self.session, self.claude_bin)


def attach(name: str, prompt: str, cwd: str, claude_bin: str = "claude",
           model: str = "", env: dict | None = None, resume: str = "",
           idle_sec: float = 0.0, extra: list[str] | None = None) -> Attached | None:
    """Поднять прозрачную сессию и отдать её как процесс с потоком."""
    session = start(name, prompt, cwd, claude_bin=claude_bin, model=model,
                    env=env, resume=resume, extra=extra)
    if session is None:
        return None
    return Attached(session, idle_sec=idle_sec, claude_bin=claude_bin)
