"""Сборка базы знаний проекта (DEV-41).

Проверяем три вещи по отдельности, потому что ломаются они по-разному:
что выводится из репозиториев без вопросов (`scan`/`draft`), что аудит
обязан назвать нехваткой (`audit`), и что после этого остаётся спросить у
команды (`questions`).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import knowledge

ROOT = Path(__file__).resolve().parent.parent

GOOD_CLAUDE = ("# x\n\n## Как проверяют\n\n```bash\nnpm test\n```\n\n"
               "Стенд: `deploy.sh`\n\n"
               # Разделы про данные и сервисы — часть полного ответа: без них
               # опросник честно спросит про схему и про то, что запущено.
               "## Схема данных\n\nМиграции в `db/`, накатывает `npm run migrate`.\n\n"
               "## Сервисы\n\nЮнит `alpha.service` на проде.\n")
CANON_OK = ("прод — юнит `doc-engine@familydoctor`, проверить "
            "`systemctl is-enabled doc-engine@familydoctor`")


def make_repo(root: Path, name: str, *, origin="https://github.com/org/x.git",
              files=None, claude=None) -> Path:
    d = root / name
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {origin}\n')
    for rel, text in (files or {}).items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    if claude is not None:
        (d / "CLAUDE.md").write_text(claude)
    return d


def full_env(tmp_path: Path, names=("alpha",)) -> dict:
    """Окружение инсталляции, у которой всё на месте."""
    store = tmp_path / "store"
    for role in knowledge.ROLES:
        (store / f"agent-{role}").mkdir(parents=True, exist_ok=True)
    books = tmp_path / "playbooks"
    books.mkdir(exist_ok=True)
    env = {"AGENT_KNOWN_REPOS": "репозитории проекта: " + ", ".join(names),
           "AGENT_ENV_CANON": CANON_OK,
           "AGENT_MEMORY_STORE": str(store),
           "AGENT_PERSON_MEMORY": str(tmp_path / "person")}
    for pb in knowledge.PLAYBOOKS:
        f = books / (pb["переменная"].lower() + ".md")
        f.write_text("рецептов пока нет, инцидентов не было\n")
        env[pb["переменная"]] = str(f)
    return env


@pytest.fixture
def core_dir(tmp_path: Path) -> Path:
    """Каталог ядра с базой методологии рядом — как его раскатывает install.sh."""
    d = tmp_path / "core"
    (d / "methodology").mkdir(parents=True)
    (d / "methodology" / "branching.md").write_text("---\nname: ветки\n---\n")
    return d


def gaps(result, level=knowledge.MUST) -> str:
    return "\n".join(g["чего"] for g in result["нет"] if g["уровень"] == level)


# ── что собирается само ─────────────────────────────────────────────────────

class TestСбор:
    def test_язык_форж_и_команда_из_клона(self, tmp_path):
        make_repo(tmp_path, "alpha", files={"a.py": "", "b.py": "", "c.js": "",
                                            "pyproject.toml": "", "test_a.py": ""})
        r = knowledge.scan_repo(tmp_path / "alpha")
        assert r["языки"][0] == "Python"
        assert r["форж"] == "github"
        assert (r["проверка"], r["откуда"]) == ("python -m pytest", "pyproject.toml")

    def test_псевдоним_ssh_не_становится_форжем(self, tmp_path):
        make_repo(tmp_path, "alpha", origin="git@github-client-b:org/x.git")
        assert knowledge.scan_repo(tmp_path / "alpha")["форж"] == "github"

    def test_команда_проверки_из_package_json(self, tmp_path):
        make_repo(tmp_path, "alpha",
                  files={"package.json": json.dumps({"scripts": {"test": "jest"}})})
        assert knowledge.check_command(tmp_path / "alpha") == ("npm test", "package.json")

    def test_package_json_без_скрипта_командой_не_считается(self, tmp_path):
        make_repo(tmp_path, "alpha", files={"package.json": json.dumps({"name": "x"})})
        assert knowledge.check_command(tmp_path / "alpha") == ("", "")

    def test_битый_package_json_не_роняет_сбор(self, tmp_path):
        make_repo(tmp_path, "alpha", files={"package.json": "{не json"})
        assert knowledge.check_command(tmp_path / "alpha") == ("", "")

    def test_зависимости_не_считаются_языком_проекта(self, tmp_path):
        make_repo(tmp_path, "alpha", files={
            "main.py": "", **{f"node_modules/lib/{i}.js": "" for i in range(5)}})
        assert knowledge.languages(tmp_path / "alpha") == ["Python"]

    def test_тесты_зависимостей_не_считаются_тестами_проекта(self, tmp_path):
        make_repo(tmp_path, "alpha", files={"pyproject.toml": "",
                                            "node_modules/lib/test_x.py": ""})
        assert knowledge.check_command(tmp_path / "alpha") == ("", "")

    def test_pytest_настроенный_не_через_pyproject(self, tmp_path):
        """Маркер на входе и условие детектора обязаны совпадать: репозиторий
        с одним `pytest.ini` иначе выглядит непроверяемым."""
        make_repo(tmp_path, "alpha", files={"pytest.ini": "[pytest]\n", "test_a.py": ""})
        assert knowledge.check_command(tmp_path / "alpha") == ("python -m pytest", "pytest.ini")

    def test_каталог_без_git_не_репозиторий(self, tmp_path):
        make_repo(tmp_path, "alpha")
        (tmp_path / "выгрузка").mkdir()
        assert [r["репозиторий"] for r in knowledge.scan(tmp_path)] == ["alpha"]

    def test_рабочая_копия_читает_origin_из_общего_каталога(self, tmp_path):
        """У worktree `.git` — файл со ссылкой. Без разбора ссылки все рабочие
        копии сессий выглядели бы репозиториями без origin."""
        make_repo(tmp_path, "alpha", origin="https://gitlab.com/org/x.git")
        wt = tmp_path / "work"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {tmp_path / 'alpha' / '.git'}\n")
        assert knowledge.origin_url(wt).endswith("gitlab.com/org/x.git")
        assert knowledge.forge_of(knowledge.origin_url(wt)) == "gitlab"


class TestЧерновик:
    def test_черновик_одной_строкой(self, tmp_path):
        """Значение уезжает в файл окружения юнита, а systemd оборвёт его на
        первом переводе строки — многострочный черновик был бы негодным."""
        make_repo(tmp_path, "alpha",
                  files={"package.json": json.dumps({"scripts": {"test": "jest"}})})
        make_repo(tmp_path, "beta", files={"go.mod": "module x"})
        draft = knowledge.known_repos_draft(knowledge.scan(tmp_path), tmp_path)
        assert "\n" not in draft
        assert "alpha" in draft and "npm test" in draft
        assert "beta" in draft and "go test ./..." in draft

    def test_кавычки_не_рвут_присваивание(self, tmp_path):
        make_repo(tmp_path, 'a"lpha')
        assert '"' not in knowledge.known_repos_draft(knowledge.scan(tmp_path), tmp_path)


# ── чего не хватает ─────────────────────────────────────────────────────────

class TestАудит:
    def test_полная_база_достаточна(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        r = knowledge.audit(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert gaps(r) == ""
        assert r["код"] == 0

    def test_нет_клонов_нет_базы(self, tmp_path, core_dir):
        (tmp_path / "repos").mkdir()
        r = knowledge.audit(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert "нет ни одного клона" in gaps(r) and r["код"] == 1

    def test_репозиторий_без_claude_md(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha")
        r = knowledge.audit(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert "alpha: нет CLAUDE.md" in gaps(r) and r["код"] == 1

    def test_claude_md_без_раздела_о_проверке(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude="# alpha\n\nПро архитектуру.\n")
        r = knowledge.audit(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert "не сказано, чем проверять" in gaps(r) and r["код"] == 1

    def test_явно_названное_отсутствие_тестов_это_ответ(self, tmp_path, core_dir):
        """«Автотестов нет, проверка глазами» — ответ на вопрос базы знаний.
        Считать его дырой значило бы требовать выдумать тесты."""
        make_repo(tmp_path / "repos", "alpha",
                  claude="# alpha\n\nАвтотестов нет, проверка глазами. Выкат: `deploy.sh`\n")
        r = knowledge.audit(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert gaps(r) == "" and r["код"] == 0

    def test_склонирован_но_не_назван(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        make_repo(tmp_path / "repos", "beta", claude=GOOD_CLAUDE)
        r = knowledge.audit(tmp_path / "repos", full_env(tmp_path, ("alpha",)), core_dir)
        assert "beta склонирован, но не назван" in gaps(r) and r["код"] == 1

    def test_имя_внутри_чужого_имени_не_считается_упоминанием(self, tmp_path, core_dir):
        """`dok-engine` внутри `dok-engine-old` — другой репозиторий."""
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path), "AGENT_KNOWN_REPOS": "репозиторий alpha-old — старый"}
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert "alpha склонирован, но не назван" in gaps(r) and r["код"] == 1

    def test_пустой_список_репозиториев(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path), "AGENT_KNOWN_REPOS": "  "}
        assert "AGENT_KNOWN_REPOS пуст" in gaps(knowledge.audit(tmp_path / "repos", env, core_dir))

    def test_нет_каталога_памяти_роли(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = full_env(tmp_path)
        (Path(env["AGENT_MEMORY_STORE"]) / "agent-verify").rmdir()
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert "agent-verify" in gaps(r) and r["код"] == 1

    def test_нет_базы_методологии(self, tmp_path):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        пусто = tmp_path / "пусто"
        пусто.mkdir()
        r = knowledge.audit(tmp_path / "repos", full_env(tmp_path), пусто)
        assert "базы методологии" in gaps(r) and r["код"] == 1

    def test_слой_о_человеке_только_желательный(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path), "AGENT_PERSON_MEMORY": ""}
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert gaps(r) == "" and "AGENT_PERSON_MEMORY" in gaps(r, knowledge.NICE)
        assert r["код"] == 0


class TestКанон:
    def test_канон_без_команды_не_принимается(self, tmp_path, core_dir):
        """Правдоподобный текст без способа проверки — то, чем канон дважды
        оказывался неверным (TASK-184, TASK-235)."""
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path),
               "AGENT_ENV_CANON": "прод — этот сервер, дев — соседний"}
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert "ни одной строки с командой-доказательством" in gaps(r)
        assert r["код"] == 1

    def test_пустой_канон_называется_отдельно(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path), "AGENT_ENV_CANON": ""}
        assert "AGENT_ENV_CANON пуст" in gaps(knowledge.audit(tmp_path / "repos", env, core_dir))

    def test_строка_без_проверки_это_желательное(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path),
               "AGENT_ENV_CANON": CANON_OK + "\nдев — соседний сервер"}
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert gaps(r) == ""
        assert "дев — соседний сервер" in gaps(r, knowledge.NICE)
        assert r["код"] == 0

    def test_длинная_строка_канона_не_печатается_целиком(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path), "AGENT_ENV_CANON": CANON_OK + "\n" + "щ" * 300}
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert max(len(g["чего"]) for g in r["нет"]) < 200


# ── что остаётся спросить ───────────────────────────────────────────────────

class TestПлейбуки:
    """Две точки чтения, пустота которых выглядит безобиднее всех: `prompt.py`
    подставит заглушку, сессия соберётся и отработает — просто дежурный будет
    каждый инцидент разбирать с нуля. Аудит обязан называть это нехваткой."""

    def test_плейбук_не_задан_это_нехватка(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = full_env(tmp_path)
        env.pop("AGENT_SYMPTOM_PLAYBOOK_FILE")
        env.pop("AGENT_INCIDENT_PLAYBOOK_FILE")
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert "AGENT_SYMPTOM_PLAYBOOK_FILE не задан" in gaps(r)
        assert "AGENT_INCIDENT_PLAYBOOK_FILE не задан" in gaps(r)
        assert r["код"] == 1

    def test_полная_база_без_плейбука_не_достаточна(self, tmp_path, core_dir):
        """Различающая пара: единственное отличие — заданный плейбук.

        Без неё «база достаточна» ничего не значит: приговор обязан меняться
        от того, что база меняется."""
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        полная = full_env(tmp_path)
        assert knowledge.audit(tmp_path / "repos", полная, core_dir)["код"] == 0
        без = {**полная, "AGENT_INCIDENT_PLAYBOOK_FILE": ""}
        assert knowledge.audit(tmp_path / "repos", без, core_dir)["код"] == 1

    def test_путь_задан_а_файла_нет(self, tmp_path, core_dir):
        """Та же грабля, от которой предупреждает раннер при старте: путь в
        конфиге есть, файл не доехал — и промпт молча собирается с заглушкой."""
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path),
               "AGENT_SYMPTOM_PLAYBOOK_FILE": str(tmp_path / "нет.md")}
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert "задан, но файла нет" in gaps(r)
        assert r["код"] == 1

    def test_пустой_файл_плейбука_это_пустая_точка(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        пустой = tmp_path / "пусто.md"
        пустой.write_text("   \n\n")
        env = {**full_env(tmp_path), "AGENT_SYMPTOM_PLAYBOOK_FILE": str(пустой)}
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert "файл пуст" in gaps(r)
        assert r["код"] == 1

    def test_записанное_нет_это_ответ(self, tmp_path, core_dir):
        """«Способа гашения нет» — законный ответ, но он обязан быть записан:
        незаданная переменная и решённое «нет» иначе неотличимы."""
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        f = tmp_path / "symptom.md"
        f.write_text("Гасить симптом здесь нечем: кэша нет, всё считается на лету.\n")
        env = {**full_env(tmp_path), "AGENT_SYMPTOM_PLAYBOOK_FILE": str(f)}
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert "SYMPTOM" not in gaps(r)
        assert r["код"] == 0

    def test_про_незаполненный_плейбук_спрашивают_с_формой(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path), "AGENT_INCIDENT_PLAYBOOK_FILE": ""}
        qs = knowledge.questions(tmp_path / "repos", env, core_dir)
        про = [q for q in qs if q["куда"] == "AGENT_INCIDENT_PLAYBOOK_FILE"]
        assert len(про) == 1
        assert "инцидент" in про[0]["вопрос"].lower()
        assert "не было" in про[0]["форма"]

    def test_вопрос_ведёт_в_файл_а_не_в_переменную(self, tmp_path, core_dir):
        """Путь задан, а файла нет: ответ кладётся в этот файл, и назвать надо
        его — иначе команда впишет путь второй раз вместо плейбука."""
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        нет = tmp_path / "ещё-не-написан.md"
        env = {**full_env(tmp_path), "AGENT_SYMPTOM_PLAYBOOK_FILE": str(нет)}
        q = [q for q in knowledge.questions(tmp_path / "repos", env, core_dir)
             if q["вопрос"].startswith("Чем здесь гасят")]
        assert len(q) == 1 and q[0]["куда"] == str(нет)

    def test_заполненный_плейбук_не_спрашивают(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        qs = knowledge.questions(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert not [q for q in qs if "PLAYBOOK" in q["куда"]]


# ── схема данных и сервисы ──────────────────────────────────────────────────

class TestДанныеИСервисы:
    """Чекбокс задачи называет схему данных и список сервисов среди того, что
    вычитывается из проекта. Вычитываем адрес, а не содержимое: пересказ DDL
    в базе знаний — вторая копия правды, она разойдётся с первой миграцией."""

    def test_схема_и_сервисы_находятся_в_дереве(self, tmp_path):
        make_repo(tmp_path, "alpha", files={"migrations/001_init.sql": "",
                                            "docker-compose.yml": "services:\n"})
        d = knowledge.scan_repo(tmp_path / "alpha")["данные"]
        assert d["схема"] == ["migrations/"]
        assert d["сервисы"] == ["docker-compose.yml"]

    def test_юнит_systemd_в_подкаталоге_это_сервис(self, tmp_path):
        make_repo(tmp_path, "alpha", files={"deploy/app@.service": "[Unit]\n"})
        assert knowledge.scan_repo(tmp_path / "alpha")["данные"]["сервисы"] == \
            ["deploy/app@.service"]

    def test_найденное_попадает_в_черновик_списка_репозиториев(self, tmp_path):
        make_repo(tmp_path, "alpha", files={"prisma/schema.prisma": "",
                                            "Procfile": "web: run\n"})
        line = knowledge.repo_line(knowledge.scan_repo(tmp_path / "alpha"))
        assert "схема данных: prisma/" in line
        assert "сервисы: Procfile" in line

    def test_ничего_не_нашлось_значит_спрашиваем(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude="# x\n\nnpm test\n")
        env = full_env(tmp_path)
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        желательное = gaps(r, knowledge.NICE)
        assert "схема данных" in желательное and "сервис" in желательное
        # но не обязательное: «у проекта нет БД» неотличимо от «схема не в
        # репозитории», и объявлять правильную базу дырой из-за этого нельзя
        assert "схема данных" not in gaps(r) and r["код"] == 0
        куда = [q["куда"] for q in knowledge.questions(tmp_path / "repos", env, core_dir)]
        assert any("Схема данных" in k for k in куда)
        assert any("Сервисы" in k for k in куда)

    def test_раздел_в_claude_md_закрывает_вопрос(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = full_env(tmp_path)
        r = knowledge.audit(tmp_path / "repos", env, core_dir)
        assert "схема данных" not in gaps(r, knowledge.NICE)
        assert not [q for q in knowledge.questions(tmp_path / "repos", env, core_dir)
                    if "Схема данных" in q["куда"]]

    def test_заголовок_юнит_тестов_не_ответ_про_сервисы(self, tmp_path, core_dir):
        """«## Юнит-тесты» есть у половины репозиториев, и если он закрывает
        вопрос про сервисы, вопрос не задаётся уже никогда и нигде."""
        claude = "# x\n\nnpm test\n\n## Юнит-тесты\n\nГоняются в CI.\n"
        make_repo(tmp_path / "repos", "alpha", claude=claude)
        assert knowledge.scan_repo(tmp_path / "repos" / "alpha")["claude"]["сервисы"] is False
        r = knowledge.audit(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert "сервис" in gaps(r, knowledge.NICE)

    def test_каталог_charts_с_графиками_не_сервис(self, tmp_path):
        """Helm опознаётся по `Chart.yaml`, а не по имени каталога: объявить
        выручку в CSV сервисом проекта — положить в базу знаний неправду."""
        make_repo(tmp_path, "alpha", files={"charts/выручка.csv": "1,2\n"})
        assert knowledge.scan_repo(tmp_path / "alpha")["данные"]["сервисы"] == []
        make_repo(tmp_path, "beta", files={"helm/app/Chart.yaml": "name: app\n"})
        assert "helm/app/Chart.yaml" in knowledge.scan_repo(tmp_path / "beta")["данные"]["сервисы"]

    def test_упоминание_словом_ответом_не_считается(self, tmp_path, core_dir):
        """На живых репозиториях Доки поиск по словам «нашёл» схему данных в
        строке «Таблица отказов из постановки», а сервисы — в «Jetpack
        Compose». Ложное «отвечено» гасит настоящий вопрос молча."""
        claude = ("# x\n\nnpm test\n\n## Что это\n\n"
                  "Таблица отказов из постановки. UI на Jetpack Compose, "
                  "миграция людей на аккаунты — не переименование поля.\n")
        make_repo(tmp_path / "repos", "alpha", claude=claude)
        c = knowledge.scan_repo(tmp_path / "repos" / "alpha")["claude"]
        assert c["схема"] is False and c["сервисы"] is False
        желательное = gaps(knowledge.audit(tmp_path / "repos", full_env(tmp_path),
                                           core_dir), knowledge.NICE)
        assert "схема данных" in желательное and "сервис" in желательное


class TestОпросник:
    def test_черновик_подставлен_в_вопрос(self, tmp_path, core_dir):
        """Спрашивать «чем проверяется», когда в package.json написано, —
        перекладывать на команду работу, которую мы сделали сами."""
        make_repo(tmp_path / "repos", "alpha",
                  files={"package.json": json.dumps({"scripts": {"test": "jest"}})})
        qs = knowledge.questions(tmp_path / "repos", full_env(tmp_path), core_dir)
        про_alpha = [q for q in qs if q["вопрос"].startswith("alpha:")]
        assert len(про_alpha) == 1
        assert "npm test" in про_alpha[0]["черновик"]
        assert про_alpha[0]["куда"].endswith("alpha/CLAUDE.md")

    def test_не_спрашивает_про_готовое(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        qs = knowledge.questions(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert not [q for q in qs if q["вопрос"].startswith("alpha:")]

    def test_канон_спрашивается_с_формой(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path), "AGENT_ENV_CANON": "прод — этот сервер"}
        q = [q for q in knowledge.questions(tmp_path / "repos", env, core_dir)
             if q["куда"] == "AGENT_ENV_CANON"]
        assert len(q) == 1 and "обратных кавычках" in q[0]["форма"]

    def test_канон_с_проверкой_не_спрашивают(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        qs = knowledge.questions(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert not [q for q in qs if q["куда"] == "AGENT_ENV_CANON"]

    def test_вопросы_о_неписаном_задаются_всегда(self, tmp_path, core_dir):
        """Три вопроса, которых не закроет никакое чтение кода, — по одному
        на слой памяти: проект, опыт роли, привычки принимающего."""
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        qs = knowledge.questions(tmp_path / "repos", full_env(tmp_path), core_dir)
        assert [q["вопрос"] for q in qs] == [q["вопрос"] for q in knowledge.ALWAYS]
        assert len({q["куда"] for q in knowledge.ALWAYS}) == 3

    def test_черновик_списка_репозиториев_в_опроснике(self, tmp_path, core_dir):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        env = {**full_env(tmp_path), "AGENT_KNOWN_REPOS": ""}
        q = [q for q in knowledge.questions(tmp_path / "repos", env, core_dir)
             if q["куда"] == "AGENT_KNOWN_REPOS"]
        assert len(q) == 1 and "alpha" in q[0]["черновик"]


# ── командная строка и установщик ───────────────────────────────────────────

class TestКоманднаяСтрока:
    def env_file(self, tmp_path, **over) -> Path:
        env = {**full_env(tmp_path), **over}
        p = tmp_path / "env"
        p.write_text("".join(f'{k}="{v}"\n' for k, v in env.items()))
        return p

    def test_аудит_отвечает_кодом(self, tmp_path, capsys):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        ef = self.env_file(tmp_path)
        assert knowledge.main(["audit", "--repos-root", str(tmp_path / "repos"),
                               "--env-file", str(ef)]) == 0
        assert "база достаточна" in capsys.readouterr().out
        ef = self.env_file(tmp_path, AGENT_ENV_CANON="")
        assert knowledge.main(["audit", "--repos-root", str(tmp_path / "repos"),
                               "--env-file", str(ef)]) == 1

    def test_многострочный_канон_в_файле_окружения_не_теряется(self, tmp_path):
        """Файл окружения — одна строка на ключ; канон в нём лежит одной
        строкой с `\\n` внутри значения, и разбор обязан это пережить."""
        ef = self.env_file(tmp_path, AGENT_ENV_CANON=CANON_OK)
        assert knowledge.read_env(ef)["AGENT_ENV_CANON"] == CANON_OK

    def test_черновик_печатает_присваивание(self, tmp_path, capsys):
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        assert knowledge.main(["draft", "--repos-root", str(tmp_path / "repos")]) == 0
        out = capsys.readouterr().out
        assert 'AGENT_KNOWN_REPOS="' in out and "alpha" in out

    def test_без_корня_репозиториев_это_ошибка_запуска(self, capsys, monkeypatch):
        monkeypatch.delenv("AGENT_REPOS_ROOT", raising=False)
        assert knowledge.main(["audit"]) == 2

    def test_опросник_печатает_куда_класть_ответ(self, tmp_path, capsys):
        make_repo(tmp_path / "repos", "alpha")
        ef = self.env_file(tmp_path, AGENT_ENV_CANON="")
        assert knowledge.main(["questions", "--repos-root", str(tmp_path / "repos"),
                               "--env-file", str(ef)]) == 0
        out = capsys.readouterr().out
        assert "куда:" in out and "AGENT_ENV_CANON" in out

    def test_модуль_запускается_отдельным_процессом(self, tmp_path):
        """На хосте его зовёт install.sh, а не pytest: синтаксис и импорты
        обязаны быть целы без окружения тестов."""
        make_repo(tmp_path / "repos", "alpha", claude=GOOD_CLAUDE)
        p = subprocess.run([sys.executable, str(ROOT / "core" / "knowledge.py"),
                            "draft", "--repos-root", str(tmp_path / "repos")],
                           capture_output=True, text=True)
        assert p.returncode == 0 and "alpha" in p.stdout


class TestУстановщикЗовётАудит:
    """Аудит, который надо вспомнить и запустить руками, на второй установке
    не запустят. Поэтому его зовёт `install.sh` — последним шагом."""

    def test_установщик_зовёт_knowledge(self):
        sh = (ROOT / "install.sh").read_text()
        assert "knowledge.py" in sh and "audit" in sh

    def test_аудит_не_роняет_установку(self):
        """Код 1 у аудита — результат, а не сбой установки: под `set -e`
        непойманный ненулевой код оборвал бы установщик на последнем шаге."""
        sh = (ROOT / "install.sh").read_text()
        calls = [l for l in sh.splitlines() if 'knowledge.py" audit' in l]
        assert calls and all("|| true" in l for l in calls)
