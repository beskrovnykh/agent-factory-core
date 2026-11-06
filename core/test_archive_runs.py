"""Архиватор записей о сессиях: переносит и НЕ стирает.

Главная проверка здесь одна и та же с разных сторон: оригинал исчезает
ТОЛЬКО после того, как копия прочиталась обратно целиком. Всё остальное —
обвязка вокруг этого правила.
"""
import gzip
import json
import os
import time

import pytest

import archive_runs as ar


def make_run(root, name, *, age_days=10.0, transcript="x" * 500,
             stats=True, verdict=True, secret=None):
    """Рабочий каталог сессии с нужным возрастом."""
    d = root / name
    d.mkdir(parents=True)
    if transcript is not None:
        (d / "transcript.jsonl").write_text(transcript)
    if stats:
        (d / "stats.json").write_text('{"стоимость": 1.5}')
    if verdict:
        (d / "verdict.txt").write_text("OK\n")
    (d / "session.log").write_text("строка лога\n" * 20)
    if secret:
        # ensure_ascii=False — ровно так пишет `runner.write_mcp_config`.
        (d / "mcp.json").write_text(json.dumps({"mcpServers": {"linear": {
            "type": "http", "url": "https://mcp.linear.app/mcp",
            "headers": {"Authorization": f"Bearer {secret}"}}}},
            ensure_ascii=False))
    past = time.time() - age_days * ar.DAY_SEC
    for child in list(d.iterdir()) + [d]:
        os.utime(child, (past, past))
    return d


@pytest.fixture()
def never_busy(monkeypatch):
    """Признак занятости смотрит в /proc; в тесте его подменяем.

    Без этого тест зависел бы от того, что происходит на машине, где его
    запустили, — а падать он должен только от своего кода.
    """
    monkeypatch.setattr(ar, "is_busy", lambda *_a, **_k: False)
    monkeypatch.setattr(ar, "busy_paths", lambda *_a, **_k: set())
    monkeypatch.setattr(ar, "procfs_available", lambda *_a, **_k: True)


def test_переносит_и_убирает_оригинал(tmp_path, never_busy):
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-1-20260901-010101", transcript="a" * 10000)
    assert ar.main(["--root", str(work), "--archive", str(arch), "--apply"]) == 0

    # Оригинал крупного ушёл, запись о нём — на месте и читается.
    assert not (run / "transcript.jsonl").exists()
    kept = arch / run.name / "transcript.jsonl.gz"
    with gzip.open(kept, "rb") as f:
        assert f.read() == b"a" * 10000
    # Мелкое лежит как есть — ради `zcat` его прятать незачем.
    assert (arch / run.name / "stats.json").read_text() == '{"стоимость": 1.5}'
    assert (arch / run.name / "verdict.txt").read_text() == "OK\n"
    # Сам каталог остаётся с отметкой, куда всё уехало.
    assert run.is_dir()
    assert str(arch / run.name) in (run / ar.MARKER).read_text()


def test_сухой_прогон_ничего_не_трогает(tmp_path, never_busy):
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-2-20260901-010101")
    assert ar.main(["--root", str(work), "--archive", str(arch)]) == 0
    assert (run / "transcript.jsonl").exists()
    assert not arch.exists()


def test_молодой_каталог_не_трогает(tmp_path, never_busy):
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-3-20260901-010101", age_days=0.5)
    assert ar.main(["--root", str(work), "--archive", str(arch),
                    "--older-than", "3", "--apply"]) == 0
    assert (run / "transcript.jsonl").exists()


def test_занятый_каталог_не_трогает(tmp_path, monkeypatch):
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-4-20260901-010101")
    monkeypatch.setattr(ar, "procfs_available", lambda *_a, **_k: True)
    monkeypatch.setattr(ar, "is_busy", lambda *_a, **_k: True)
    monkeypatch.setattr(ar, "busy_paths", lambda *_a, **_k: set())
    assert ar.main(["--root", str(work), "--archive", str(arch), "--apply"]) == 0
    assert (run / "transcript.jsonl").exists()
    assert not (run / ar.MARKER).exists()


def test_возраст_по_самому_свежему_файлу(tmp_path, never_busy):
    """Каталог старый, а транскрипт в нём дописан только что — не трогаем.

    mtime каталога не меняется от дописывания файла, поэтому считать возраст
    по нему значит уносить записи законченной минуту назад сессии.
    """
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-5-20260901-010101", age_days=30)
    now = time.time()
    os.utime(run / "transcript.jsonl", (now, now))
    assert ar.main(["--root", str(work), "--archive", str(arch),
                    "--older-than", "3", "--apply"]) == 0
    assert (run / "transcript.jsonl").exists()


def test_битая_копия_оставляет_оригинал(tmp_path, never_busy, monkeypatch):
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-6-20260901-010101")

    def broken(src, dst):
        dst.write_bytes(b"")
        raise OSError("диск кончился")

    monkeypatch.setattr(ar, "gzip_to", broken)
    assert ar.main(["--root", str(work), "--archive", str(arch), "--apply"]) == 0
    # Не перенеслось — значит и не убрано.
    assert (run / "transcript.jsonl").exists()
    assert (run / "session.log").exists()


def test_снимает_ключ_личности(tmp_path, never_busy):
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-7-20260901-010101", secret="lin_api_ЖИВОЙ")
    assert ar.main(["--root", str(work), "--archive", str(arch), "--apply"]) == 0
    body = (run / "mcp.json").read_text()
    assert "lin_api_ЖИВОЙ" not in body
    assert ar.MASK in body
    # Ключ никуда не копируется — в архиве его быть не должно.
    assert not (arch / run.name / "mcp.json").exists()


def test_ключ_не_уносится_в_архив_даже_без_записей(tmp_path, never_busy):
    """Каталог без транскрипта тоже разоружаем: ключ в нём живой."""
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = work / "DEV-8-ручной"
    run.mkdir(parents=True)
    (run / "mcp.json").write_text(json.dumps({"mcpServers": {"linear": {
        "headers": {"Authorization": "Bearer lin_api_ЖИВОЙ"}}}}))
    past = time.time() - 30 * ar.DAY_SEC
    os.utime(run / "mcp.json", (past, past))
    os.utime(run, (past, past))
    assert ar.main(["--root", str(work), "--archive", str(arch), "--apply"]) == 0
    assert "lin_api_ЖИВОЙ" not in (run / "mcp.json").read_text()


def test_повторный_прогон_не_дублирует(tmp_path, never_busy):
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-9-20260901-010101")
    args = ["--root", str(work), "--archive", str(arch), "--apply"]
    assert ar.main(args) == 0
    marker = (run / ar.MARKER).read_text()
    assert ar.main(args) == 0
    assert (run / ar.MARKER).read_text() == marker


def test_архив_внутри_источника_запрещён(tmp_path, never_busy):
    """Архив под рабочим корнем — не архив: следующая уборка унесёт и его."""
    work = tmp_path / "work"
    make_run(work, "DEV-10-20260901-010101")
    assert ar.main(["--root", str(work), "--archive", str(work / "arch"),
                    "--apply"]) == 1


def test_без_procfs_apply_отказывает(tmp_path, monkeypatch):
    work, arch = tmp_path / "work", tmp_path / "arch"
    make_run(work, "DEV-11-20260901-010101")
    monkeypatch.setattr(ar, "procfs_available", lambda *_a, **_k: False)
    assert ar.main(["--root", str(work), "--archive", str(arch), "--apply"]) == 1


def test_нет_корня(tmp_path):
    assert ar.main(["--root", str(tmp_path / "нет"), "--apply"]) == 1


def test_mask_only_снимает_ключи_и_не_переносит(tmp_path, never_busy):
    """Разовая уборка по уже накопленному: 63 каталога с ключом на хосте.

    Возраст тут ни при чём — ключ в каталоге, доработавшем минуту назад,
    лежит на диске ровно так же.
    """
    work, arch = tmp_path / "work", tmp_path / "arch"
    свежий = make_run(work, "DEV-20-свежий", age_days=0.01,
                      secret="lin_api_ЖИВОЙ")
    старый = make_run(work, "DEV-21-старый", age_days=30, secret="lin_api_ЖИВОЙ")
    assert ar.main(["--root", str(work), "--archive", str(arch),
                    "--mask-only", "--apply"]) == 0
    for run in (свежий, старый):
        assert "lin_api_ЖИВОЙ" not in (run / "mcp.json").read_text()
        # Ничего не перенесено: записи остались на месте.
        assert (run / "transcript.jsonl").exists()
        assert not (run / ar.MARKER).exists()
    assert not arch.exists()


def test_mask_only_сухой_прогон_не_трогает_ключ(tmp_path, never_busy):
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-22", secret="lin_api_ЖИВОЙ")
    assert ar.main(["--root", str(work), "--archive", str(arch),
                    "--mask-only"]) == 0
    assert "lin_api_ЖИВОЙ" in (run / "mcp.json").read_text()


def test_занятость_спрашивается_у_proc_один_раз(tmp_path, monkeypatch):
    """243 каталога × сотни процессов — обход обязан быть один на прогон.

    Пока `is_busy` сам лазил в /proc, сухой прогон на client-a не укладывался
    в две минуты.
    """
    work, arch = tmp_path / "work", tmp_path / "arch"
    for i in range(5):
        make_run(work, f"DEV-3{i}-20260901-010101")
    обходов = []
    monkeypatch.setattr(ar, "procfs_available", lambda *_a, **_k: True)
    monkeypatch.setattr(ar, "busy_paths",
                        lambda *_a, **_k: обходов.append(1) or set())
    assert ar.main(["--root", str(work), "--archive", str(arch), "--apply"]) == 0
    assert обходов == [1]


def test_не_спросили_proc_значит_занят(tmp_path):
    """`busy_paths` вернул None — это «считаем занятыми всех», не «свободны»."""
    assert ar.is_busy(tmp_path / "что-угодно", None) is True
    assert ar.is_busy(tmp_path / "что-угодно", set()) is False


def test_the_reference_file_travels_with_the_verdict(tmp_path, never_busy):
    """`expected.md` — эталон, по которому вердикт читается; без него архив
    хранит ответ без вопроса (DEV-87)."""
    work, arch = tmp_path / "work", tmp_path / "arch"
    run = make_run(work, "DEV-1-verify-20260901-010101")
    ref = run / "expected.md"
    ref.write_text("Источник критерия: задача\n")
    past = time.time() - 10 * ar.DAY_SEC
    os.utime(ref, (past, past))
    os.utime(run, (past, past))
    assert ar.main(["--root", str(work), "--archive", str(arch), "--apply"]) == 0
    assert (arch / run.name / "expected.md").read_text() == "Источник критерия: задача\n"
    assert not ref.exists()
