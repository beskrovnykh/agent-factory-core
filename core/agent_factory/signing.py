"""Очередь на подпись: карточки сделанного, с телефона, одним экраном.

Чат для решения не годится. Кнопка под сообщением требует выбора, а материала
для выбора в сообщении нет: что менялось, чем рискуем, где агент решил за
человека. Нажатие превращается в ставку — при этом отвечает за неё человек.
Развернуть весь разбор в чат тоже нельзя: на телефоне это простыня, которую
пролистывают, то есть та же ставка, только длиннее.

Поэтому роли разведены: чат остаётся звонком в дверь («сдал, вот ссылка»),
трекер остаётся хранилищем для агентов, а решение принимается здесь — в одном
месте, где у каждой карточки есть и короткий ответ на вопрос «можно не глядя?»,
и всё остальное на один тап глубже.

Часть ПРОДУКТА: что показывать и в каком порядке, одинаково у любой
инсталляции. За инсталляцией — трекер (`tracking.Tracker`) и адрес.

С 15.09.2026 страница не только читает. Владелец: «мне каждый раз логиниться
в GitHub» — единственный способ смержить принятую работу был уйти в хостинг
руками. Поэтому на карточке рядом со свидетельствами стоит «Смержить», а на
странице конфигурации — обновление ядра. Правила действий, и они дороже
вёрстки:

  • действие — только по подписи Telegram (`SIGNING_ALLOWED_USER_IDS`).
    Токен в ссылке пересылается, подпись — нет. Без списка кнопок нет вовсе:
    раздавать право мержа по ссылке из чата нельзя;
  • всегда через страницу подтверждения: PR, ветки, вердикт, итог ворот.
    Одним тапом ничего в чужом репозитории не меняется;
  • на «не проверено» и на всём, что не прошло, — предупреждение словами
    (что не сошлось) и обязательная галка;
  • кто и что нажал — в журнал, до и после действия.
"""
from __future__ import annotations

import html
import hmac
import json
import logging
import re
import time
import urllib.parse

from . import evidence as ev
from . import runs as rn
from . import sessions as sess
from . import webauth
from .verdicts import MARKS, Verdicts, counted

log = logging.getLogger("intake")

# Статусы, в которых задача ждёт человека. Имена — параметр инсталляции:
# у Linear они настраиваются, «In Review» есть не у всех.
WAITING_STATES = ("In Review",)
# Статусы, в которых задача прямо сейчас у агента.
WORKING_STATES = ("In Progress",)
# Статусы, в которых задачу прямо сейчас ПРОВЕРЯЮТ.
#
# Третья корзина появилась с новым порядком (DEV-78): работа уходит не к
# человеку, а на проверку, и между «у агента» и «ждёт подписи» встало
# состояние, которого страница не знала. Задачи на проверке пропадали с неё
# целиком: не в работе, не на подписи — нигде. Человек видел, что дел стало
# меньше, хотя их стало столько же.
CHECKING_STATES = ("Verifying",)


STYLE = """:root { --bg:#0f1115; --card:#171a21; --line:#272b34; --txt:#e6e8ec;
         --dim:#8b93a1; --acc:#7aa2f7; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--txt); font:15px/1.45
        -apple-system,system-ui,"Segoe UI",sans-serif; padding:10px 12px 36px; }
.tab-in { position:absolute; opacity:0; pointer-events:none; }
.tab { display:inline-block; padding:7px 14px;
        border:1px solid var(--line); border-radius:20px; color:var(--dim);
        font-size:14px; cursor:pointer; user-select:none; }
.tab b { color:var(--txt); font-weight:600; }
#t-work:checked ~ .tab[for="t-work"],
#t-check:checked ~ .tab[for="t-check"],
#t-sign:checked ~ .tab[for="t-sign"] { background:var(--acc); border-color:var(--acc);
        color:#0f1115; }
#t-work:checked ~ .tab[for="t-work"] b,
#t-sign:checked ~ .tab[for="t-sign"] b { color:#0f1115; }
.pane { display:none; }
#t-work:checked ~ .pane-work, #t-check:checked ~ .pane-check,
#t-sign:checked ~ .pane-sign { display:block; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px;
         margin-bottom:10px; padding:13px 15px; }
summary { list-style:none; cursor:pointer; }
summary::-webkit-details-marker { display:none; }
.row { display:flex; gap:6px; align-items:center; margin-bottom:5px;
        flex-wrap:wrap; }
.id { font-weight:600; color:var(--acc); }
.st, .pr, .who { font-size:11px; color:var(--dim); border:1px solid var(--line);
            border-radius:20px; padding:1px 8px; }
.ttl { font-weight:600; margin-bottom:5px; line-height:1.3; }
.sum { color:var(--dim); font-size:13px; }
.body { margin-top:12px; border-top:1px solid var(--line); padding-top:6px; }
.sec { margin-bottom:10px; }
.sec p { margin:4px 0; font-size:14px; }
.sec ul { margin:4px 0; padding-left:18px; }
.sec li { margin:3px 0; font-size:14px; }
.sec code { background:#0f1115; border:1px solid var(--line); border-radius:5px;
             padding:0 4px; font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace; }
h4 { margin:10px 0 2px; font-size:11px; color:var(--dim);
      text-transform:uppercase; letter-spacing:.06em; }
a { color:var(--acc); }
.links { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
.lnk { color:var(--acc); text-decoration:none; border:1px solid var(--line);
        border-radius:8px; padding:4px 11px; font-size:13px; }
.dim { color:var(--dim); font-size:13px; }
.head { display:grid; grid-template-columns:repeat(2,1fr); gap:7px;
         margin-bottom:12px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:12px;
         padding:9px 11px 10px; }
.tl { font-size:11px; color:var(--dim); text-transform:uppercase;
       letter-spacing:.05em; }
.tv { font-size:21px; font-weight:600; margin:4px 0 3px; letter-spacing:-.01em;
       font-variant-numeric:tabular-nums; }
.unit { font-size:13px; font-weight:400; color:var(--dim); }
.quota { height:4px; background:var(--line); border-radius:3px; margin-top:7px;
        overflow:hidden; }
.quota i { display:block; height:100%; background:var(--acc); }
.quota-full i { background:#f7768e; }
.tn { font-size:11px; color:var(--dim); line-height:1.35; }
.limits { margin-bottom:12px; color:var(--dim); font-size:13px; }
.limits summary { display:inline-block; border:1px solid var(--line);
        border-radius:20px; padding:5px 13px; }
.limits form { display:flex; gap:8px; flex-wrap:wrap; align-items:flex-end;
        margin-top:10px; }
.f { display:flex; flex-direction:column; gap:3px; font-size:11px; }
.f input { width:84px; background:var(--card); color:var(--txt);
        border:1px solid var(--line); border-radius:8px; padding:6px 8px;
        font-size:14px; }
.limits button { background:var(--acc); color:#0f1115; border:0;
        border-radius:8px; padding:8px 14px; font-size:14px; font-weight:600; }
.empty, .err { color:var(--dim); padding:20px 4px; }
.err { color:#f7768e; }
.menu-in { position:absolute; opacity:0; pointer-events:none; }
.bar { position:sticky; top:0; z-index:3; display:flex; align-items:center;
        gap:10px; height:46px; margin:-10px -12px 10px; padding:0 10px;
        background:var(--bg); border-bottom:1px solid var(--line); }
.burger { width:34px; height:34px; display:flex; align-items:center;
        justify-content:center; font-size:17px; color:var(--txt);
        background:var(--card); border:1px solid var(--line); border-radius:9px;
        cursor:pointer; flex:none; }
.brand { font-weight:600; font-size:15px; }
.side-brand { display:none; }
.side { position:fixed; top:0; left:0; bottom:0; width:214px; z-index:4;
        background:var(--card); border-right:1px solid var(--line);
        padding:14px 10px; display:flex; flex-direction:column; gap:3px;
        transform:translateX(-100%); transition:transform .18s ease; }
.menu-in:checked ~ .side { transform:none; }
.veil { position:fixed; inset:0; z-index:2; background:#0009; display:none; }
.menu-in:checked ~ .veil { display:block; }
.nav { color:var(--dim); text-decoration:none; font-size:15px; padding:9px 11px;
        border-radius:9px; }
.nav.on { color:var(--txt); background:var(--bg); }
h1 { font-size:18px; margin:0 0 12px; }
@media (min-width:760px) {
  .bar, .veil { display:none; }
  .side { transform:none; }
  .side-brand { display:block; padding:6px 11px 12px; font-size:16px; }
  body { padding-left:238px; padding-top:14px; }
}
.tabs { display:flex; gap:6px; margin-bottom:10px; }
.row2 { display:grid; grid-template-columns:1fr 96px; gap:4px 10px;
         align-items:center; background:var(--card); border:1px solid var(--line);
         border-radius:12px; padding:11px 13px; margin-bottom:8px; }
.cl { font-weight:600; font-size:14px; }
.ch { grid-column:1 / -1; color:var(--dim); font-size:12px; line-height:1.4; }
.ci { display:flex; align-items:center; gap:6px; justify-content:flex-end; }
.cu { color:var(--dim); font-size:12px; }
.row2 input { width:70px; background:var(--bg); color:var(--txt);
        border:1px solid var(--line); border-radius:8px; padding:7px 9px;
        font-size:15px; text-align:right; font-variant-numeric:tabular-nums; }
.run-head { display:grid; grid-template-columns:1fr repeat(4,minmax(52px,auto));
        gap:8px; padding:0 13px 6px; font-size:10px; color:var(--dim);
        text-transform:uppercase; letter-spacing:.05em; }
.run-head span:not(:first-child) { text-align:right; }
.run { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:10px 13px; margin-bottom:7px; }
.run-h { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap;
        margin-bottom:6px; }
.run-h b { font-size:15px; }
.run-n { display:grid; grid-template-columns:1fr repeat(3,minmax(52px,auto));
        gap:8px; font-size:13px; font-variant-numeric:tabular-nums; }
.run-n span:not(:first-child) { text-align:right; color:var(--dim); }
.run-on { color:var(--acc); font-size:11px; border:1px solid var(--acc);
        border-radius:20px; padding:1px 8px; }
.run-bad { color:#f7768e; font-size:11px; border:1px solid #f7768e;
        border-radius:20px; padding:1px 8px; }
.marks { display:flex; gap:5px; flex-wrap:wrap; margin-top:9px;
        padding-top:9px; border-top:1px solid var(--line); }
.mk { background:none; color:var(--dim); border:1px solid var(--line);
        border-radius:20px; padding:4px 10px; font-size:12px; width:auto;
        font-weight:400; cursor:pointer; }
.mk-on { color:#0f1115; background:var(--acc); border-color:var(--acc);
        font-weight:600; }
.run-link { color:var(--acc); text-decoration:none; font-size:13px; }
.stages { list-style:none; margin:8px 0; padding:0; }
.stages li { display:flex; gap:7px; align-items:baseline; font-size:13px;
        padding:4px 0; border-top:1px solid var(--line); }
.stages li:first-child { border-top:0; }
.ic { flex:none; width:19px; text-align:center; font-size:14px; }
.sw { font-weight:600; white-space:nowrap; }
.so { color:var(--dim); flex:1; }
.sc { color:var(--dim); white-space:nowrap;
        font-variant-numeric:tabular-nums; }
.ex { color:var(--dim); font-size:12px; margin-top:7px; line-height:1.45; }
.run-ev { display:flex; gap:6px; flex-wrap:wrap; align-items:center;
        margin-top:8px; font-size:12px; color:var(--dim); }
.run-ev code { background:var(--bg); border:1px solid var(--line);
        border-radius:5px; padding:0 4px;
        font:11px ui-monospace,SFMono-Regular,Menlo,monospace; }
.lead { color:var(--txt); font-size:14px; background:var(--card);
        border:1px solid var(--line); border-radius:12px; padding:11px 13px;
        margin:0 0 12px; line-height:1.45; }
.ok { color:#9ece6a; font-size:13px; margin-bottom:10px; }
button { background:var(--acc); color:#0f1115; border:0; border-radius:10px;
        padding:11px 18px; font-size:15px; font-weight:600; width:100%; }
.ev { margin:10px 0 12px; padding:9px 11px 6px; background:var(--bg);
        border:1px solid var(--line); border-radius:10px; }
.ev-t { font-size:11px; color:var(--dim); text-transform:uppercase;
        letter-spacing:.06em; margin-bottom:6px; }
.ev-h { color:var(--dim); font-size:12px; margin-bottom:6px; }
.ev code { background:var(--card); border:1px solid var(--line); border-radius:5px;
        padding:0 4px; font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace;
        word-break:break-all; }
.vd { font-size:14px; margin-bottom:6px; line-height:1.4; }
.ev-none { color:#f7768e; font-size:13px; margin:4px 0 8px; }
.ev-j { font-size:13px; color:var(--dim); margin:4px 0 6px; }
.g { display:inline-block; font-size:11px; border:1px solid var(--line);
        border-radius:20px; padding:0 7px; white-space:nowrap; color:var(--dim); }
.g-ok { color:#9ece6a; border-color:#9ece6a; }
.g-bad { color:#f7768e; border-color:#f7768e; }
.g-err { color:#e0af68; border-color:#e0af68; }
.scroll { overflow-x:auto; margin-bottom:8px; }
.ev table { border-collapse:collapse; width:100%; font-size:13px; }
.ev td { padding:4px 6px; border-top:1px solid var(--line); vertical-align:top; }
.ev td.rc { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.ev .exp { color:var(--dim); font-size:12px; }
.act { margin-top:10px; }
.act button { background:none; color:var(--acc); border:1px solid var(--acc);
        border-radius:8px; padding:6px 14px; font-size:13px; width:auto; }
.act-risk button { color:#e0af68; border-color:#e0af68; }
.warn { background:#2a2118; border:1px solid #e0af68; color:#e0af68;
        border-radius:12px; padding:11px 13px; margin:0 0 12px; font-size:14px;
        line-height:1.45; }
.chk { display:flex; gap:9px; align-items:flex-start; margin:12px 0;
        font-size:14px; line-height:1.4; }
.chk input { width:20px; height:20px; margin-top:1px; flex:none; }
.facts { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:11px 13px; margin:0 0 12px; font-size:14px; line-height:1.5; }
.facts b { color:var(--acc); }
.back { display:inline-block; margin-top:14px; }
.danger { background:#f7768e; color:#0f1115; }
"""


def short(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def waiting_first(tasks: list[dict]) -> list[dict]:
    """Порядок карточек: кто дольше ждёт подписи — тот выше.

    Не по приоритету: приоритет решал, что делать раньше, и своё уже отработал.
    Здесь другой вопрос — что дольше держит человека, потому что невзятая
    подпись блокирует выкат, а блокированный выкат копит следующие.
    """
    return sorted(tasks, key=lambda t: str(t.get("updated_at") or ""))


# Заголовки, по которым сессия сама структурирует сдачу. Ищем их, а не
# пересказываем весь текст: карточка отвечает на вопрос «подписывать?», а не
# заменяет задачу в трекере.
SECTIONS = (
    ("что", ("что сделано", "что сделал", "сделано", "что изменилось")),
    ("проверено", ("проверено", "проверка", "тесты")),
    ("риск", ("риск", "необратимо", "внимание", "осторожно")),
    ("осталось", ("не сделал", "осталось", "открытые вопросы", "вопросы",
                  "что дальше", "дальше от тебя")),
)


def _headline(line: str) -> tuple[str, str]:
    """Строка → (ключ куска, остаток строки). Не тот заголовок — ключ пустой.

    Сессии пишут заголовки тремя способами сразу: `## Что сделано`,
    `**Проверено.** 408 тестов`, `Риск: переписывает 6 млн строк`. Остаток
    ОБЯЗАН возвращаться: у второго способа весь текст живёт на той же строке,
    и отбросив его, карточка теряет ровно то, ради чего собиралась.
    """
    t = line.strip().lstrip("#").strip()
    bare = t.replace("*", "").replace("_", "").strip().casefold()
    for key, names in SECTIONS:
        for name in names:
            if not bare.startswith(name):
                continue
            rest = t.replace("*", "").replace("_", "").strip()[len(name):]
            return key, rest.lstrip(" .:—-–").strip()
    return "", ""


def sections(text: str) -> dict[str, str]:
    """Сдача → куски по смыслу. Чего сессия не написала — того и нет."""
    out: dict[str, list[str]] = {}
    current = ""
    for line in (text or "").splitlines():
        key, rest = _headline(line)
        if key:
            current = key
            out.setdefault(current, [])
            if rest:
                out[current].append(rest)
            continue
        if current and line.strip():
            out[current].append(line.rstrip())
        elif current and not line.strip() and out.get(current):
            current = ""            # пустая строка закрывает кусок
    return {k: "\n".join(v).strip() for k, v in out.items() if "".join(v).strip()}


def markdown(text: str, limit: int = 600) -> str:
    """Крошечный markdown → html: жирное, моноширинное, ссылки, списки.

    Свой, а не библиотека: в карточку едут четыре вида разметки, а зависимость
    в продукте, который ставится на чужой хост, стоит дороже сорока строк.
    Экранирование ПЕРВЫМ шагом — текст пишет модель, показываем его мы.
    """
    import re
    cut = text or ""
    if len(cut) > limit:
        cut = cut[:limit].rsplit(" ", 1)[0] + "…"
    out, in_list = [], False
    for raw in cut.splitlines():
        line = html.escape(raw.rstrip())
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                      r'<a href="\2" target="_blank">\1</a>', line)
        line = re.sub(r"(?<![\"=>])(https?://[^\s<]+)",
                      r'<a href="\1" target="_blank">ссылка</a>', line)
        stripped = line.strip()
        item = stripped.startswith(("- ", "* ", "• "))
        if item and not in_list:
            out.append("<ul>"); in_list = True
        elif not item and in_list:
            out.append("</ul>"); in_list = False
        if item:
            out.append("<li>" + stripped[2:].strip() + "</li>")
        elif stripped:
            out.append("<p>" + stripped + "</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


class SigningPage:
    """Страница очереди. Токен — в ссылке, дальше в куке.

    Ни одного действия, меняющего мир, здесь пока нет: страница читает. Кнопка
    подписи приедет отдельно и с подтверждением — раздавать право мержа по
    ссылке из чата нельзя, пока не решено, как её отзывать.
    """

    def __init__(self, tracker, team_id: str, token: str,
                 states: tuple[str, ...] = WAITING_STATES,
                 working_states: tuple[str, ...] = WORKING_STATES,
                 checking_states: tuple[str, ...] = CHECKING_STATES,
                 title: str = "Фабрика агентов", puller=None, work_root: str = "",
                 verdicts=None, issue_base: str = "",
                 bot_token: str = "", allowed_ids=(), hosting=None,
                 verification=None, updater=None, risky_states=()):
        """`hosting` (`hosting.Hosting`) — открытый PR по задаче и мерж; нет —
        кнопки «Смержить» нет. `verification` (`deploy.Verification`) —
        после мержа переводит задачу в Done тем же путём, что событие о
        мерже. `updater` (`updater.Updater`) — обновление ядра со страницы
        конфигурации. `risky_states` — состояния, где мерж — действие с
        риском («не проверено»): кнопка есть, но с предупреждением."""
        self.tracker = tracker
        self.team_id = team_id
        self.token = str(token or "")
        self.states = tuple(states)
        self.working = tuple(working_states)
        self.checking = tuple(checking_states)
        self.title = title
        # Тяга — источник обратного отсчёта и потолков; рабочий каталог —
        # источник правды про сессии и токены. Нет их — шапка просто короче.
        self.puller = puller
        self.work_root = str(work_root or "")
        # Оценки сессий: ставит человек, хранит инсталляция.
        self.verdicts = verdicts
        # Откуда собирать ссылку на задачу: «…/issue/» + идентификатор. У
        # каждого трекера свой адрес, поэтому параметр, а не литерал.
        self.issue_base = str(issue_base or "")
        # Кто именно может открыть. Пусто — проверка выключена, доступ по
        # токену в ссылке, как было. Задано — токена мало: нужна подпись
        # Telegram, и id должен быть в списке.
        self.bot_token = str(bot_token or "")
        self.allowed_ids = {str(i).strip() for i in allowed_ids if str(i).strip()}
        self.hosting = hosting
        self.verification = verification
        self.updater = updater
        self.risky = tuple(risky_states)

    def known(self, headers: dict) -> bool:
        """Пускали ли уже этого человека — по куке, поставленной после
        проверки подписи."""
        for part in (headers.get("cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "signing_who" and v in self.allowed_ids:
                return True
        return False

    def who(self, headers: dict) -> str:
        """Кто пришёл — id из куки, поставленной после проверки подписи
        Telegram. Пусто — неизвестно (или не из списка)."""
        for part in (headers.get("cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "signing_who" and v in self.allowed_ids:
                return v
        return ""

    def may_act(self, headers: dict) -> bool:
        """Действие, меняющее мир, — только тому, кого узнали по подписи.
        Инсталляция без списка не действует вовсе: там доступ по токену в
        ссылке, а ссылку пересылают."""
        return bool(self.allowed_ids) and bool(self.who(headers))

    def sign_in(self, body: bytes, base: str) -> tuple[int, tuple]:
        """Подписанные данные от Telegram → кука или отказ."""
        values = {k: v[0] for k, v in urllib.parse.parse_qs(
            body.decode("utf-8", "replace")).items()}
        user = webauth.verify(values.get("data", ""), self.bot_token)
        if not webauth.allowed(user, self.allowed_ids):
            who = (user or {}).get("id", "неизвестно")
            log.warning("вход отклонён: %s", who)
            return 403, ("text/html; charset=utf-8",
                         page_shell(self.title, "Доступ закрыт",
                                    "Эта страница открыта не для вас."))
        log.info("вход: %s (%s)", user.get("id"), user.get("first_name", ""))
        # Кука на месяц: мини-приложение открывают ежедневно, и спрашивать
        # Telegram каждый раз незачем. HttpOnly — чтобы её не вычитал скрипт.
        cookie = (f"signing_who={user['id']}; Path=/; Max-Age=2592000; "
                  "HttpOnly; Secure; SameSite=Lax")
        return 200, ("text/html; charset=utf-8",
                     page_shell(self.title, "Готово",
                                "Открываю…",
                                refresh=base + f"?t={urllib.parse.quote(self.token)}"),
                     {"Set-Cookie": cookie})

    def issue_url(self, task: str) -> str:
        """Адрес задачи в трекере. Пусто — ссылки не будет, и это нормально:
        приёмная сессия называется id черновика, а не задачи."""
        # Идентификатор задачи — «БУКВЫ-ЧИСЛО». Приёмная сессия называется id
        # ЧЕРНОВИКА (задачи в момент её старта ещё нет, она её и создаёт), и
        # ссылка по такому имени вела бы в никуда.
        if not (self.issue_base and re.fullmatch(r"[A-Z]{2,6}-\d+", task or "")):
            return ""
        return self.issue_base.rstrip("/") + "/" + task

    @staticmethod
    def base_of(path: str) -> str:
        """Адрес самой очереди по адресу любой её страницы.

        Путь — параметр инсталляции (`SIGNING_PATH`), и зашивать «/queue» в
        ссылки значит увести человека в никуда у всех, кто назвал его иначе.
        """
        for tail in ("/merge/confirm", "/done/confirm", "/update/confirm",
                     "/merge", "/done", "/update",
                     "/data", "/limits", "/config", "/runs", "/mark", "/auth"):
            if path.endswith(tail):
                return path[:-len(tail)] or "/"
        return path or "/"

    # ── доступ ───────────────────────────────────────────────────────────
    def allowed(self, target: str, headers: dict) -> bool:
        if not self.token:
            return False
        given = ""
        query = urllib.parse.urlparse(target).query
        got = urllib.parse.parse_qs(query).get("t")
        if got:
            given = got[0]
        if not given:
            for part in (headers.get("cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "signing":
                    given = v
        return bool(given) and hmac.compare_digest(
            given.encode("latin-1", "replace"), self.token.encode())

    async def handle(self, method: str, target: str, headers: dict,
                     body: bytes) -> tuple[int, tuple]:
        path = urllib.parse.urlparse(target).path
        if not self.allowed(target, headers):
            # 404, а не 401: страница по чужой ссылке не должна даже
            # подтверждать, что она существует.
            return 404, ("text/plain; charset=utf-8", "нет такой страницы")
        if path.endswith("/auth") and method == "POST":
            return self.sign_in(body, self.base_of(path))
        if method == "POST" and (path.endswith("/merge") or path.endswith("/merge/confirm")
                                 or path.endswith("/done") or path.endswith("/done/confirm")
                                 or path.endswith("/update") or path.endswith("/update/confirm")):
            # Действия — ДО прихожей: право на них проверяет сам `act`, и
            # попытка без подписи должна кончаться 403 и строкой в журнале,
            # а не тихой страницей «проверяю, кто пришёл».
            return await self.act(path, headers, body)
        if self.allowed_ids and not self.known(headers):
            # Токен есть, а кто пришёл — неизвестно. Спрашиваем у Telegram:
            # страница-прихожая читает подписанные данные из адреса и шлёт их
            # сюда. Если открыто не из Telegram, спрашивать некого — так и
            # скажем, вместо того чтобы показать пустоту.
            return 200, ("text/html; charset=utf-8",
                         gate_html(self.title, self.base_of(path), self.token))
        if path.endswith("/limits") and method == "POST":
            return await self.retune(body, path)
        if path.endswith("/mark") and method == "POST":
            if self.verdicts is None:
                return 404, ("text/plain; charset=utf-8", "оценки не включены")
            values = {k: v[0] for k, v in urllib.parse.parse_qs(
                body.decode("utf-8", "replace")).items()}
            await self.verdicts.put(values.get("session", ""),
                                    values.get("mark", ""))
            path = self.base_of(path) + "/runs"
        if path.endswith("/runs"):
            import asyncio
            got = (await asyncio.to_thread(sess.walk, self.work_root,
                                           time.time() - 86400)
                   if self.work_root else [])
            marks = await self.verdicts.all() if self.verdicts else {}
            # Свидетельства ищутся по ВСЕМУ каталогу, а не по суточному окну:
            # проверка задачи могла быть вчера, а сегодняшняя доработка —
            # заход без свидетельств. Окно бы показало «свидетельств нет» на
            # задаче, у которой они есть, и это худший вид пустоты.
            proofs = (await asyncio.to_thread(
                ev.collect, self.work_root,
                sorted({str(x.get("task") or "") for x in got}))
                if self.work_root else {})
            return 200, ("text/html; charset=utf-8",
                         runs_html(self.title, got, await self.head(),
                                   base=self.base_of(path), token=self.token,
                                   verdicts=marks, issue_url=self.issue_url,
                                   proofs=proofs))
        if path.endswith("/config"):
            return 200, ("text/html; charset=utf-8",
                         config_html(self.title, await self.head(),
                                     saved="saved" in target,
                                     base=self.base_of(path),
                                     token=self.token,
                                     core=await self.core(headers)))
        try:
            working, checking, waiting = await self.tasks()
        except Exception as e:
            log.warning("очередь подписи не собралась: %s", e)
            return 503, ("text/html; charset=utf-8",
                         page_html(self.title, [], [], [], error=str(e)))
        head = await self.head()
        base = self.base_of(path)
        if path.endswith("/data"):
            return 200, ("application/json; charset=utf-8",
                         json.dumps({"сейчас": head, "в работе": working,
                                     "на проверке": checking,
                                     "на подпись": waiting}, ensure_ascii=False))
        return 200, ("text/html; charset=utf-8",
                     page_html(self.title, working, checking, waiting, head=head,
                               base=base, token=self.token))

    async def retune(self, body: bytes, path: str = "/queue/limits") -> tuple[int, tuple]:
        """Правка потолков со страницы. Возвращаемся на неё же, а не отвечаем
        json'ом: форма без JavaScript, и после отправки человек должен увидеть
        страницу, а не голый текст."""
        if self.puller is None:
            return 404, ("text/plain; charset=utf-8", "нечего настраивать")
        values = {k: v[0] for k, v in urllib.parse.parse_qs(
            body.decode("utf-8", "replace")).items()}
        hours = values.pop("window_hours", None)
        if hours is not None:
            try:
                hours = int(hours)
            except Exception:
                log.warning("через сколько часов брать: %r — не число", hours)
                hours = None
        if hours is not None:
            # Размер пачки не спрашиваем: он равен числу одновременных. Ноль
            # часов — темпа нет вовсе, берём сразу, как освободится место.
            values["window_sec"] = max(1, hours) * 3600
            at_once = values.get("max_in_flight") or self.puller.max_in_flight
            values["per_window"] = int(at_once) if hours else 0
        await self.puller.tune(values)
        # Возвращаем саму страницу конфигурации, а не редирект: заголовка
        # Location у нашего ответа нет, а человек должен увидеть, что
        # применилось, — не пустой экран.
        return 200, ("text/html; charset=utf-8",
                     config_html(self.title, await self.head(), saved=True,
                                 base=self.base_of(path),
                                 token=self.token))

    # ── действия ─────────────────────────────────────────────────────────

    async def act(self, path: str, headers: dict, body: bytes) -> tuple[int, tuple]:
        """Развилка действий. Первое — право: без подписи Telegram 403, и это
        в журнал: попытка действия по чужой ссылке — событие, а не шум."""
        base = self.base_of(path)
        if not self.may_act(headers):
            log.warning("действие %s отклонено: нет подписи Telegram (список: %s)",
                        path, "задан" if self.allowed_ids else "пуст")
            return 403, ("text/html; charset=utf-8",
                         note_html(self.title, "Действие закрыто",
                                   ["Действия доступны только по входу через Telegram. "
                                    "Открой страницу из мини-приложения."],
                                   base, self.token))
        values = {k: v[0] for k, v in urllib.parse.parse_qs(
            body.decode("utf-8", "replace")).items()}
        who = self.who(headers)
        if path.endswith("/merge/confirm"):
            return await self.merge_confirm(values, who, base)
        if path.endswith("/merge"):
            return await self.merge_preview(values, who, base)
        if path.endswith("/done/confirm"):
            return await self.done_confirm(values, who, base)
        if path.endswith("/done"):
            return await self.done_preview(values, who, base)
        if path.endswith("/update/confirm"):
            return await self.update_confirm(values, who, base)
        return await self.update_preview(values, who, base)

    async def _request(self, ident: str) -> dict | None:
        import asyncio
        return await asyncio.to_thread(self.hosting.open_request, ident)

    async def _requests(self, idents: list[str]) -> dict[str, dict] | None:
        """Открытые PR по всем карточкам, где решение за человеком, — одним
        походом в хостинг при ОТРИСОВКЕ страницы (DEV-120).

        Зачем при отрисовке: у задачи-решения (DEV-29) ветки нет никогда, и
        хостинг на ней отвечает пусто. Раньше это выяснялось только после
        нажатия «Смержить» — человек ждал опроса, который заведомо ничего не
        найдёт, и получал сообщение об ошибке вместо действия.

        Три разных ответа, и путать их нельзя:
          • словарь с задачей — PR есть, кнопка «Смержить»;
          • словарь без задачи — PR нет, кнопка «Закрыть как сделанную»;
          • `None` — хостинг не ответил, мы НЕ ЗНАЕМ. Тогда страница ведёт
            себя как до правки: кнопка мержа с проверкой при нажатии. Считать
            молчание хостинга за «мержить нечего» нельзя: человек закрыл бы
            задачу, у которой PR есть.
        """
        import asyncio
        if self.hosting is None or not idents:
            return {}
        bulk = getattr(self.hosting, "open_requests", None)
        try:
            if callable(bulk):
                got = await asyncio.to_thread(bulk, list(idents))
            else:
                # Хостинг старой сборки списка не знает — спрашиваем по одной.
                got = {}
                for ident in idents:
                    one = await asyncio.to_thread(self.hosting.open_request, ident)
                    if one:
                        got[ident] = one
            # Разбор ответа — тоже под `try`: хостинг пишет инсталляция, и
            # ответ не той формы должен стоить кнопок, а не всей страницы.
            return {k: v for k, v in (got or {}).items() if v}
        except Exception as e:
            log.warning("очередь подписи: открытые PR не спросить (%s) — "
                        "кнопки остаются как были", e)
            return None

    async def _state_and_proof(self, ident: str) -> tuple[str, dict]:
        """Состояние задачи и свидетельства последней проверки — то, что
        человек должен видеть ДО подтверждения."""
        import asyncio
        state = ""
        look = getattr(self.tracker, "issue_by_identifier", None)
        if callable(look):
            try:
                issue = await asyncio.to_thread(look, ident)
                state = ((issue or {}).get("state") or {}).get("name") or ""
            except Exception as e:
                log.warning("мерж по кнопке %s: состояние не прочитано: %s", ident, e)
        proof = {}
        if self.work_root:
            got = await asyncio.to_thread(ev.collect, self.work_root, [ident])
            proof = got.get(ident) or {}
        return state, proof

    def risk_of(self, state: str, proof: dict) -> list[str]:
        """Почему мерж — действие с риском. Пусто — риска не видно: проверка
        прошла, ворота прошли, состояние не «не проверено». Слова — для
        предупреждения; человек должен видеть, ЧТО не сошлось, а не только
        цвет кнопки."""
        why = []
        if state and state in self.risky:
            why.append(f"задача в «{state}» — проверку не прошла")
        head = (proof or {}).get("вердикт") or ""
        word = head.split()[0].strip(":;,.—-").upper() if head else ""
        if not proof:
            why.append("состоявшейся проверки по задаче не было — свидетельств нет")
        elif word not in ("OK", "FIXED"):
            why.append(f"вердикт проверки: {short(head, 200) or 'пустой'}")
        gates = (proof or {}).get("ворота")
        if proof and not gates:
            why.append("ворота: свидетельств нет (gates.json не записан)")
        elif gates and gates.get("итог") != ev.PASSED:
            why.append(f"ворота: {gates.get('итог')} — {short(gates.get('почему') or '', 200)}")
        return why

    async def merge_preview(self, values: dict, who: str, base: str) -> tuple[int, tuple]:
        """Страница подтверждения: PR, ветки, вердикт, ворота, предупреждение."""
        ident = str(values.get("task") or "").strip()
        if self.hosting is None:
            return 404, ("text/plain; charset=utf-8", "хостинг не подключён")
        if not re.fullmatch(r"[A-Z]{2,6}-\d+", ident):
            return 400, ("text/html; charset=utf-8",
                         note_html(self.title, "Не понял задачу",
                                   [f"«{ident}» не похоже на идентификатор задачи."],
                                   base, self.token))
        log.info("мерж по кнопке: %s открыл подтверждение по %s", who, ident)
        try:
            req = await self._request(ident)
        except Exception as e:
            log.warning("мерж по кнопке %s: хостинг не ответил: %s", ident, e)
            return 503, ("text/html; charset=utf-8",
                         note_html(self.title, "Хостинг не ответил", [str(e)],
                                   base, self.token))
        if not req:
            return 200, ("text/html; charset=utf-8",
                         note_html(self.title, f"{ident}: открытого PR нет",
                                   ["По ветке agent/" + ident + " открытого PR не найдено — "
                                    "либо уже смержен, либо ветки ещё нет."],
                                   base, self.token))
        state, proof = await self._state_and_proof(ident)
        risk = self.risk_of(state, proof)
        return 200, ("text/html; charset=utf-8",
                     confirm_merge_html(self.title, ident, req, state, proof, risk,
                                        base, self.token))

    async def merge_confirm(self, values: dict, who: str, base: str) -> tuple[int, tuple]:
        """Само действие. PR сверяется заново с тем, что человек видел на
        подтверждении: за минуту его могли смержить, закрыть или открыть
        другой; мержить «какой-нибудь» нельзя."""
        import asyncio
        ident = str(values.get("task") or "").strip()
        if self.hosting is None or not re.fullmatch(r"[A-Z]{2,6}-\d+", ident):
            return 400, ("text/plain; charset=utf-8", "не понял задачу")
        try:
            req = await self._request(ident)
        except Exception as e:
            return 503, ("text/html; charset=utf-8",
                         note_html(self.title, "Хостинг не ответил", [str(e)],
                                   base, self.token))
        seen = (str(values.get("repo") or ""), str(values.get("number") or ""))
        if not req or (req["repo"], req["number"]) != seen:
            log.warning("мерж по кнопке %s: PR изменился (видели %s#%s, сейчас %s) — не мержу",
                        ident, seen[0], seen[1],
                        f"{req['repo']}#{req['number']}" if req else "нет")
            return 409, ("text/html; charset=utf-8",
                         note_html(self.title, "PR изменился",
                                   ["Пока ты смотрел, открытый PR по задаче сменился или "
                                    "исчез. Открой подтверждение заново."],
                                   base, self.token))
        state, proof = await self._state_and_proof(ident)
        risk = self.risk_of(state, proof)
        if risk and values.get("ack") != "1":
            log.warning("мерж по кнопке %s: %s подтвердил без галки при риске (%s) — не мержу",
                        ident, who, "; ".join(risk))
            return 400, ("text/html; charset=utf-8",
                         note_html(self.title, "Нужно подтверждение",
                                   ["Мерж непроверенной работы — действие с риском: "
                                    "поставь галку на странице подтверждения."]
                                   + risk, base, self.token))
        log.warning("мерж по кнопке: %s мержит %s — %s#%s (%s → %s)%s", who, ident,
                    req["repo"], req["number"], req["source_ref"], req["target_ref"],
                    (" ПОВЕРХ РИСКА: " + "; ".join(risk)) if risk else "")
        try:
            outcome = await asyncio.to_thread(self.hosting.merge, req)
        except Exception as e:
            log.error("мерж по кнопке %s: не вышло: %s", ident, e)
            return 502, ("text/html; charset=utf-8",
                         note_html(self.title, f"{ident}: мерж не прошёл", [str(e)],
                                   base, self.token))
        log.info("мерж по кнопке %s: %s", ident, outcome)
        lines = [outcome]
        if self.verification is not None:
            done = await self.verification.accept_merged(ident, {
                "identifier": ident, "mr_iid": req["number"], "ref": req["target_ref"],
                "source_ref": req["source_ref"], "project_path": req["repo"],
                "url": req.get("url") or "", "merged_by": who}, by=who)
            lines.append({"done": "Задача переведена в Done.",
                          "already-closed": "Задача уже была закрыта.",
                          "no-issue": "Задачи в трекере нет — переводить нечего.",
                          "no-tracker": "Трекер не подключён — переведи задачу руками.",
                          "tracker-failed": "Трекер не ответил — переведи задачу руками."}
                         .get(done, done))
        else:
            lines.append("Проверка не подключена — задачу в трекере переведи руками.")
        return 200, ("text/html; charset=utf-8",
                     note_html(self.title, f"{ident}: смержено", lines, base, self.token))

    async def done_preview(self, values: dict, who: str, base: str) -> tuple[int, tuple]:
        """Подтверждение закрытия БЕЗ мержа (DEV-120). Хостинг тут не
        спрашиваем: ответ на «есть ли PR» уже получен при отрисовке
        карточки, и второй поход — это ровно то ожидание, из-за которого
        задача и заведена. Сверка с хостингом — при самом действии."""
        ident = str(values.get("task") or "").strip()
        if not re.fullmatch(r"[A-Z]{2,6}-\d+", ident):
            return 400, ("text/html; charset=utf-8",
                         note_html(self.title, "Не понял задачу",
                                   [f"«{ident}» не похоже на идентификатор задачи."],
                                   base, self.token))
        log.info("закрытие без мержа: %s открыл подтверждение по %s", who, ident)
        state, proof = await self._state_and_proof(ident)
        risk = self.risk_of(state, proof)
        return 200, ("text/html; charset=utf-8",
                     confirm_done_html(self.title, ident, state, proof, risk,
                                       base, self.token))

    async def done_confirm(self, values: dict, who: str, base: str) -> tuple[int, tuple]:
        """Само закрытие. Перед ним — последний вопрос хостингу: если
        открытый PR всё-таки есть (появился, пока человек смотрел, или
        карточка отрисовалась на молчащем хостинге), закрывать нельзя —
        работу надо мержить, а не хоронить."""
        ident = str(values.get("task") or "").strip()
        if not re.fullmatch(r"[A-Z]{2,6}-\d+", ident):
            return 400, ("text/plain; charset=utf-8", "не понял задачу")
        lines = []
        if self.hosting is not None:
            try:
                req = await self._request(ident)
            except Exception as e:
                # Хостинг молчит — но кнопка «Закрыть как сделанную» стоит
                # только там, где при отрисовке ответили «PR нет». Закрываем
                # и говорим прямо, что перепроверить не вышло.
                log.warning("закрытие без мержа %s: хостинг не ответил (%s) — "
                            "закрываю по решению человека", ident, e)
                req = None
                lines.append("Хостинг не ответил — открытый PR перед закрытием "
                             "перепроверить не вышло.")
            if req:
                log.warning("закрытие без мержа %s: открытый PR %s#%s — не закрываю",
                            ident, req.get("repo"), req.get("number"))
                return 409, ("text/html; charset=utf-8",
                             note_html(self.title, f"{ident}: мержить есть что",
                                       ["По задаче открыт PR #" + str(req.get("number") or "?")
                                        + " в " + str(req.get("repo") or "?")
                                        + " — закрывать её как сделанную без мержа нельзя. "
                                        "Обнови очередь: на карточке будет «Смержить»."],
                                       base, self.token))
        state, proof = await self._state_and_proof(ident)
        risk = self.risk_of(state, proof)
        if risk and values.get("ack") != "1":
            log.warning("закрытие без мержа %s: %s подтвердил без галки при риске (%s) — "
                        "не закрываю", ident, who, "; ".join(risk))
            return 400, ("text/html; charset=utf-8",
                         note_html(self.title, "Нужно подтверждение",
                                   ["Закрыть непроверенную работу — действие с риском: "
                                    "поставь галку на странице подтверждения."]
                                   + risk, base, self.token))
        close = getattr(self.verification, "accept_done", None) if self.verification else None
        if not callable(close):
            log.warning("закрытие без мержа %s: приём не подключён — переводить некому", ident)
            return 200, ("text/html; charset=utf-8",
                         note_html(self.title, f"{ident}: закрыть отсюда нечем",
                                   lines + ["Приём не подключён — переведи задачу в Done "
                                            "руками, в трекере."], base, self.token))
        log.warning("закрытие без мержа: %s закрывает %s (открытого PR нет)%s", who, ident,
                    (" ПОВЕРХ РИСКА: " + "; ".join(risk)) if risk else "")
        got = await close(ident, by=who)
        lines.append({"done": "Задача переведена в Done и ушла из очереди.",
                      "already-closed": "Задача уже была закрыта.",
                      "no-issue": "Задачи в трекере нет — переводить нечего.",
                      "no-tracker": "Трекер не подключён — переведи задачу руками.",
                      "tracker-failed": "Трекер не ответил — переведи задачу руками."}
                     .get(got, got))
        return 200, ("text/html; charset=utf-8",
                     note_html(self.title, f"{ident}: закрыто без мержа", lines,
                               base, self.token))

    async def core(self, headers: dict) -> dict:
        """Ядро для страницы конфигурации: установленная и доступная версии.
        Пусто — обновления нет или его не спросить."""
        if self.updater is None or not self.may_act(headers):
            return {}
        current, available = await self.updater.drift()
        if not current or not available:
            return {}
        from .updater import name_of
        return {"current": current, "available": available,
                "current_name": name_of(self.updater.source, current),
                "available_name": name_of(self.updater.source, available),
                "same": current == available}

    async def update_preview(self, values: dict, who: str, base: str) -> tuple[int, tuple]:
        if self.updater is None:
            return 404, ("text/plain; charset=utf-8", "обновление не подключено")
        version = str(values.get("version") or "").strip()
        from .updater import name_of
        log.info("обновление ядра со страницы: %s открыл подтверждение до %s", who, version[:12])
        return 200, ("text/html; charset=utf-8",
                     confirm_update_html(self.title, version,
                                         name_of(self.updater.source, version),
                                         base, self.token))

    async def update_confirm(self, values: dict, who: str, base: str) -> tuple[int, tuple]:
        if self.updater is None:
            return 404, ("text/plain; charset=utf-8", "обновление не подключено")
        version = str(values.get("version") or "").strip()
        log.warning("обновление ядра со страницы: %s обновляет до %s", who, version[:12])
        code, text = await self.updater.perform(version, user_id=who)
        status = 200 if code == "applied" else 409
        return status, ("text/html; charset=utf-8",
                        note_html(self.title, "Ядро" if code == "applied" else "Не обновлено",
                                  [text], base, self.token))

    async def head(self) -> dict:
        """Шапка: чем занята фабрика, когда следующая порция, чего это стоит.

        Собирается из двух источников, и оба могут молчать: тяги может не быть
        вовсе, рабочего каталога может не быть на этой машине. Тогда в шапке
        просто меньше строк — но страница открывается.
        """
        out: dict = {}
        if self.work_root:
            import asyncio
            today = time.time() - 24 * 3600
            got = await asyncio.to_thread(sess.walk, self.work_root, today)
            out["сессии"] = sess.summary(got)
            out["последние"] = [
                {"задача": s["task"], "идёт": s["идёт"],
                 "стоимость": round(s["стоимость"], 2), "секунд": s["секунд"]}
                for s in got[:5]]
        if self.puller is not None and getattr(self.puller, "max_open", 0):
            got = await self.puller.open_now()
            if got is not None:
                out["на подписи"] = got
        if self.puller is not None:
            left = await self.puller.next_batch_in()
            marks = await self.puller._marks(time.time())
            out["тяга"] = {
                "следующая порция": sess.human_left(left),
                "секунд до порции": int(left),
                "взято за сутки": len(marks),
                **self.puller.limits(),
            }
        return out

    async def tasks(self) -> tuple[list[dict], list[dict], list[dict]]:
        """Три вкладки — три вопроса. «В работе» отвечает, чем занята фабрика;
        «На проверке» — что она уже сделала и сейчас проверяет; «На подпись» —
        что держит человека.

        Один запрос на все три: лишний поход с телефона это секунда ожидания и
        повод не открыть страницу вовсе.
        """
        import asyncio
        got = await asyncio.to_thread(
            self.tracker.issues_awaiting,
            self.team_id, self.working + self.checking + self.states) or []
        # Свидетельства последней проверки — из рабочего каталога, одним
        # обходом на все карточки (DEV-94). Нет каталога на этой машине —
        # карточка скажет об этом словами, а не промолчит.
        proofs = (await asyncio.to_thread(
            ev.collect, self.work_root, [i.get("identifier") for i in got])
            if self.work_root else {})
        # Открытые PR — тоже при отрисовке, одним походом (DEV-120): карточка
        # обязана знать, есть ли что мержить, ДО того как человек нажал.
        reqs = await self._requests([i.get("identifier") or "" for i in got
                                     if self.decides(i)])
        cards = [self.card(i, self.proof(i, proofs), reqs) for i in got]
        working = [c for c in cards if c["state"] in self.working]
        checking = [c for c in cards if c["state"] in self.checking]
        waiting = [c for c in cards if c["state"] in self.states]
        # «В работе» и «на проверке» — сначала взятое позже: оно и есть «сейчас».
        working.sort(key=lambda c: str(c.get("updated_at") or ""), reverse=True)
        checking.sort(key=lambda c: str(c.get("updated_at") or ""), reverse=True)
        return working, checking, waiting_first(waiting)

    def decides(self, issue: dict) -> bool:
        """Карточка, на которой действие за человеком: хостинг подключён, вход
        по подписи Telegram, и задача в состоянии, где ждут его решения —
        подпись или разбор непрошедшего."""
        if self.hosting is None or not self.allowed_ids:
            return False
        return (issue.get("state") or {}).get("name") in self.states + self.risky

    def proof(self, issue: dict, proofs: dict) -> dict:
        """Свидетельства для карточки: разбор проверки либо причина, почему
        его нет. Причина — всегда словами: «нет каталога» и «проверки не
        было» — разные факты, и оба читаются иначе, чем пустое место."""
        if not self.work_root:
            return {"нет": "рабочий каталог сессий на этой машине не задан"}
        got = proofs.get(issue.get("identifier") or "")
        return got if got else {"нет": "состоявшейся проверки по задаче не было"}

    def card(self, issue: dict, proof: dict | None = None,
             requests: dict | None = None) -> dict:
        """Карточка отвечает на один вопрос: подписывать или смотреть глубже.

        Постановку и обсуждение сюда НЕ тащим: они уже есть в трекере, и
        копия делает из карточки простыню, которую пролистывают.

        Свидетельства (`proof`, DEV-94) — единственное в карточке, что
        написала не модель: ворота с кодами, эталон до заявки, построчная
        сверка. Рядом с прозой, а не вместо неё.
        """
        ident = issue.get("identifier") or ""
        # Что можно сделать прямо отсюда. Мержить — только когда есть что
        # мержить: `requests` собран при отрисовке, и задача без открытого PR
        # получает «Закрыть как сделанную» вместо «Смержить» (DEV-120).
        # `requests is None` — хостинг промолчал; тогда как раньше, мержем.
        if not self.decides(issue):
            actions = []
        elif requests is None or (requests or {}).get(ident):
            actions = ["merge"]
        else:
            actions = ["done"]
        comments = (issue.get("comments") or {}).get("nodes", [])
        # Сдача — ПОСЛЕДНИЙ разбор, а не последняя реплика: человек, ответивший
        # после агента, иначе становился бы содержимым карточки.
        parts, last = {}, ""
        for c in reversed(comments):
            got = sections(c.get("body") or "")
            if got:
                parts, last = got, c.get("body") or ""
                break
        return {
            "identifier": ident,
            "title": issue.get("title") or "",
            "url": issue.get("url") or "",
            "state": (issue.get("state") or {}).get("name") or "",
            "priority": issue.get("priorityLabel") or "",
            "updated_at": issue.get("updatedAt") or "",
            "assignee": (issue.get("assignee") or {}).get("name") or "",
            "summary": short(parts.get("что") or parts.get("риск") or "", 150),
            "sections": parts,
            "links": links_in(last),
            "talk": len(comments),
            "свидетельства": proof or {"нет": "не собирались"},
            "действия": actions,
            "риск": (issue.get("state") or {}).get("name") in self.risky,
        }


def links_in(text: str) -> list[dict]:
    """Ссылки из текста сдачи: MR, прогон, лог. Их агент и так пишет."""
    out, seen = [], set()
    for word in (text or "").replace("(", " ").replace(")", " ").split():
        w = word.strip(".,;:«»\"'<>[]")
        if not w.startswith("http") or w in seen:
            continue
        seen.add(w)
        host = urllib.parse.urlparse(w).netloc
        kind = ("MR" if "/merge_requests/" in w or "/pull/" in w else
                "прогон" if "/pipelines/" in w or "/actions/" in w else
                "задача" if "linear.app" in w else host)
        out.append({"kind": kind, "url": w})
    return out


def page_shell(title: str, head: str, text: str, refresh: str = "") -> str:
    """Короткая страница на один экран: «готово», «закрыто», «не оттуда»."""
    e = html.escape
    meta = (f'<meta http-equiv="refresh" content="0; url={e(refresh)}">'
            if refresh else "")
    return f'''<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{meta}<title>{e(title)}</title><style>{STYLE}</style></head><body>
<main><h1>{e(head)}</h1><p class="lead">{e(text)}</p></main></body></html>'''


def gate_html(title: str, base: str, token: str) -> str:
    """Прихожая: берёт подписанные Telegram данные из адреса и отдаёт их нам.

    Двенадцать строк скрипта — единственные на всём сайте, и без них никак:
    Telegram кладёт подпись в ФРАГМЕНТ адреса (после `#`), а фрагмент браузер
    на сервер не отправляет вовсе. Прочитать его может только страница.

    Ничего не грузим со стороны: `telegram-web-app.js` для этого не нужен,
    данные лежат в адресе открытым текстом.
    """
    e = html.escape
    action = f"{base}/auth?t={urllib.parse.quote(token)}"
    return f'''<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{STYLE}</style></head><body>
<main><h1>{e(title)}</h1>
<p class="lead" id="say">Проверяю, кто пришёл…</p>
<form id="f" method="post" action="{e(action)}">
  <input type="hidden" name="data" id="d"></form>
<noscript><p class="lead">Открой страницу кнопкой в боте.</p></noscript>
<script>
(function () {{
  var h = location.hash.slice(1), got = "";
  h.split("&").forEach(function (p) {{
    var i = p.indexOf("=");
    if (i > 0 && p.slice(0, i) === "tgWebAppData") got = decodeURIComponent(p.slice(i + 1));
  }});
  if (!got) {{
    document.getElementById("say").textContent =
      "Эту страницу нужно открывать кнопкой в боте — так Telegram подтверждает, кто вы.";
    return;
  }}
  document.getElementById("d").value = got;
  document.getElementById("f").submit();
}})();
</script>
</main></body></html>'''


# Цвет отметки по слову: прошло/сошлось — зелёный, упало/разошлось/без
# ответа — красный, обвязка/не проверял/лишняя строка — жёлтый. Жёлтое —
# не «почти зелёное»: это «неизвестно», и читать его как порядок нельзя.
_MARK = {ev.PASSED: "g-ok", ev.MATCHED: "g-ok",
         ev.FAILED: "g-bad", ev.MISMATCHED: "g-bad", ev.NO_ANSWER: "g-bad",
         ev.ERROR: "g-err", ev.UNCHECKED: "g-err", ev.NO_ITEM: "g-err",
         ev.NO_EVIDENCE: "g-err"}


def _mark(word: str) -> str:
    return f'<span class="g {_MARK.get(word, "")}">{html.escape(word)}</span>'


def glance_html(p: dict) -> str:
    """Отметка в шапке карточки — ответ на «можно не глядя?» одним словом:
    суд ворот, либо «свидетельств нет». Проза в шапку не попадает."""
    if not p or p.get("нет"):
        return _mark("свидетельств нет")
    g = p.get("ворота")
    if not g:
        return _mark("свидетельств нет")
    return f'<span class="g {_MARK.get(g["итог"], "")}">ворота: {html.escape(g["итог"])}</span>'


def evidence_html(p: dict) -> str:
    """Свидетельства проверки на карточке (DEV-94): что запускалось и с каким
    кодом, эталон до заявки, построчная сверка. Пустое называется своими
    словами — «Свидетельств нет: …» — и никогда не пропускается: пропуск
    подписывающий читает как «всё в порядке».
    """
    e = html.escape
    if not p or p.get("нет"):
        why = (p or {}).get("нет") or "проверки не было"
        return (f'<div class="ev"><div class="ev-t">Свидетельства</div>'
                f'<div class="ev-none">Свидетельств нет: {e(why)}.</div></div>')
    parts = ['<div class="ev-t">Свидетельства</div>']
    meta = f'проверка <code>{e(p["сессия"])}</code>'
    if p.get("когда"):
        meta += f' · {e(when_words(p["когда"]))}'
    if p.get("исход"):
        meta += f' · исход {e(p["исход"])}'
    parts.append(f'<div class="ev-h">{meta}</div>')
    if p.get("вердикт"):
        parts.append(f'<div class="vd">{e(p["вердикт"])}</div>')
    else:
        parts.append('<div class="ev-none">Вердикта нет: verdict.txt не записан.</div>')

    # Ворота: имя, команда, код, итог. Команда и код — как записала обвязка.
    parts.append("<h4>Ворота</h4>")
    g = p.get("ворота")
    if not g:
        parts.append('<div class="ev-none">Свидетельств нет: gates.json не записан — '
                     'что запускалось и с каким кодом, неизвестно.</div>')
    else:
        parts.append(f'<div class="ev-j">{_mark(g["итог"])} {e(g["почему"])}</div>')
        rows = []
        for r in g["записи"]:
            cmd = f'<code>{e(short(r["команда"], 140))}</code>' if r["команда"] else "—"
            rc = "—" if r["код"] is None else str(r["код"])
            note = f'<div class="exp">{e(short(r["заметка"], 160))}</div>' if r["заметка"] else ""
            if r.get("заменена"):
                # Попытку отменил перезапуск тех же ворот: исход она не
                # понижает, и красной её читать не надо (DEV-107).
                note += ('<div class="exp">попытка отменена перезапуском — '
                         'в исход не идёт</div>')
            if r.get("контроль"):
                # Контрольные ворота: красный код тут ЗАДУМАН, и человек
                # должен видеть это строкой, а не выяснять по команде
                # (DEV-104). Иначе карточка пугает там, где всё правильно.
                note += ('<div class="exp">контроль: ожидался код '
                         f'{e(str(r["ожидание"]))} — красный код здесь '
                         'доказывает, что проба ловит отсутствие правки</div>')
            elif r.get("ожидание") is not None:
                note += ('<div class="exp">ожидание кода '
                         f'{e(str(r["ожидание"]))} не зачтено: объявлено '
                         'задним числом</div>')
            if r.get("разобрана"):
                note += ('<div class="exp">находка разобрана сессией — '
                         'в исход не идёт</div>')
            rows.append(f'<tr><td>{e(r["ворота"])}</td><td>{cmd}</td>'
                        f'<td class="rc">{e(rc)}</td><td>{_mark(r["итог"])}{note}</td></tr>')
        parts.append(f'<div class="scroll"><table class="gates">{"".join(rows)}</table></div>')

    # Эталон и построчный ответ: пункт против строки вердикта, по порядку.
    parts.append("<h4>Эталон и ответ по пунктам</h4>")
    ref = p.get("эталон")
    if ref is None:
        parts.append('<div class="ev-none">Эталона нет: expected.md не записан — '
                     'сверять было не с чем.</div>')
    else:
        src = ref.get("источник") or "не назван"
        line = f"Источник критерия: {src}"
        if p.get("критерий по воротам"):
            line += f' · по воротам: {p["критерий по воротам"]}'
        parts.append(f'<div class="ev-j">{e(line)}</div>')
        if p.get("сверка"):
            rows = []
            for r in p["сверка"]:
                place = e(short(r["место"], 120)) or "—"
                want = (f'<div class="exp">ожидалось: {e(short(r["ожидаемое"], 160))}</div>'
                        if r["ожидаемое"] else "")
                seen = e(short(r["текст"], 220))
                rows.append(f'<tr><td>{place}{want}</td><td>{_mark(r["ответ"])}</td>'
                            f'<td>{seen}</td></tr>')
            parts.append(f'<div class="scroll"><table class="check">{"".join(rows)}</table></div>')
        elif ref.get("текст"):
            parts.append('<div class="ev-none">Пункты эталона не разобраны — файл как есть.</div>'
                         + markdown(ref["текст"], 1200))
        else:
            parts.append('<div class="ev-none">Эталон пуст: в expected.md ни одного пункта.</div>')
    return '<div class="ev">' + "".join(parts) + "</div>"


def action_html(t: dict, base: str, token: str) -> str:
    """Кнопка действия под карточкой — форма без JavaScript, ведёт на
    страницу подтверждения. Без токена страницы кнопки нет: ей некуда
    вести.

    Кнопка ровно одна, и какая — решено при отрисовке (DEV-120): есть
    открытый PR — «Смержить», нет — «Закрыть как сделанную». Кнопки мержа
    там, где мержить нечего, быть не должно: нажатие на неё кончалось
    сообщением об ошибке и задачей, застрявшей в стопке навсегда.
    """
    actions = t.get("действия") or []
    if not actions or not token:
        return ""
    e = html.escape
    risk = " act-risk" if t.get("риск") else ""
    if "done" in actions:
        tail, word = "/done", "Закрыть как сделанную…"
    else:
        tail = "/merge"
        word = "Смержить с риском…" if t.get("риск") else "Смержить…"
    action = f"{base}{tail}?t={urllib.parse.quote(token)}"
    return (f'<form class="act{risk}" method="post" action="{e(action)}">'
            f'<input type="hidden" name="task" value="{e(t["identifier"])}">'
            f'<button>{e(word)}</button></form>')


def card_html(t: dict, base: str = "/queue", token: str = "") -> str:
    e = html.escape
    links = "".join(
        f'<a class="lnk" href="{e(l["url"])}" target="_blank">{e(l["kind"])}</a>'
        for l in t["links"])
    if t["url"]:
        links += (f'<a class="lnk" href="{e(t["url"])}" target="_blank">'
                  f'в трекере{" · " + str(t["talk"]) if t["talk"] > 1 else ""}</a>')
    order = (("что", "Что сделано"), ("риск", "Риск"),
             ("проверено", "Проверено"), ("осталось", "Осталось"))
    blocks = "".join(
        f'<div class="sec"><h4>{e(name)}</h4>{markdown(t["sections"][key])}</div>'
        for key, name in order if t["sections"].get(key))
    if not blocks:
        blocks = '<div class="dim">Сессия не оставила разбора — смотри в трекере.</div>'
    who = f'<span class="who">{e(t["assignee"])}</span>' if t["assignee"] else ""
    proof = t.get("свидетельства") or {}
    # Свидетельства — ПЕРЕД прозой: рядом с вердиктом, а не под ним, чтобы
    # тот, кто размечает вердикт, смотрел на коды, а не на рассказ.
    return f'''
<details class="card">
  <summary>
    <div class="row"><span class="id">{e(t["identifier"])}</span>
      <span class="st">{e(t["state"])}</span>
      <span class="pr">{e(t["priority"])}</span>{who}{glance_html(proof)}</div>
    <div class="ttl">{e(t["title"])}</div>
    <div class="sum">{e(t["summary"])}</div>
  </summary>
  <div class="body">{evidence_html(proof)}{blocks}<div class="links">{links}</div>{action_html(t, base, token)}</div>
</details>'''


def _shell(title: str, here: str, base: str, token: str, body: str) -> str:
    e = html.escape
    return f'''<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{STYLE}</style></head><body>
{nav_html(here, base, token)}<main>{body}</main>
</body></html>'''


def note_html(title: str, heading: str, lines: list[str], base: str = "/queue",
              token: str = "") -> str:
    """Страница-ответ на действие: заголовок, строки, ссылка назад. Не
    редирект — человек должен увидеть, что случилось."""
    e = html.escape
    back = f"{base}?t={urllib.parse.quote(token)}" if token else base
    body = (f"<h1>{e(heading)}</h1>"
            + "".join(f'<p class="lead">{e(str(ln))}</p>' for ln in lines)
            + f'<a class="lnk back" href="{e(back)}">← к очереди</a>')
    return _shell(title, "queue", base, token, body)


def confirm_merge_html(title: str, ident: str, req: dict, state: str, proof: dict,
                       risk: list[str], base: str = "/queue", token: str = "") -> str:
    """Подтверждение мержа: что именно и поверх чего. Риск — словами и с
    обязательной галкой: браузер сам не отправит форму без неё."""
    e = html.escape
    action = f"{base}/merge/confirm?t={urllib.parse.quote(token)}"
    facts = (f'<div class="facts"><b>{e(ident)}</b>'
             + (f' · <span class="st">{e(state)}</span>' if state else "")
             + f'<br>PR <b>#{e(req["number"])}</b> в {e(req["repo"])}'
             + (f' — <a href="{e(req["url"])}" target="_blank">открыть</a>' if req.get("url") else "")
             + f'<br><code>{e(req["source_ref"])}</code> → <code>{e(req["target_ref"])}</code>'
             + (f'<br>{e(short(req["title"], 160))}' if req.get("title") else "")
             + ("<br><span class='err'>Хостинг сообщает конфликт — мерж не пройдёт.</span>"
                if req.get("mergeable") is False else "")
             + "</div>")
    if risk:
        warn = ('<div class="warn"><b>Действие с риском.</b> '
                + " ".join(e(w) + "." for w in risk)
                + " Изменений в проде от этого не будет, пока не выкатится, но ветка "
                  "уйдёт в основную как принятая. Смержить всё равно?</div>"
                '<label class="chk"><input type="checkbox" name="ack" value="1" required>'
                'Понимаю, что работа не прошла проверку, и принимаю её сознательно.</label>')
        button = '<button class="danger">Смержить с риском</button>'
    else:
        warn = '<p class="ok">Проверка прошла, ворота прошли — можно мержить.</p>'
        button = "<button>Смержить</button>"
    body = (f"<h1>Смержить {e(ident)}?</h1>{facts}{evidence_html(proof)}{warn}"
            f'<form method="post" action="{e(action)}">'
            f'<input type="hidden" name="task" value="{e(ident)}">'
            f'<input type="hidden" name="repo" value="{e(req["repo"])}">'
            f'<input type="hidden" name="number" value="{e(req["number"])}">'
            f"{button}</form>"
            f'<a class="lnk back" href="{e(base + "?t=" + urllib.parse.quote(token))}">← отмена</a>')
    return _shell(title, "queue", base, token, body)


def confirm_done_html(title: str, ident: str, state: str, proof: dict,
                      risk: list[str], base: str = "/queue", token: str = "") -> str:
    """Подтверждение закрытия без мержа: что закрываем и почему мержить
    нечего. Риск — теми же словами и с той же обязательной галкой, что и у
    мержа: «сделано» на непрошедшей проверке — тоже решение человека."""
    e = html.escape
    action = f"{base}/done/confirm?t={urllib.parse.quote(token)}"
    facts = (f'<div class="facts"><b>{e(ident)}</b>'
             + (f' · <span class="st">{e(state)}</span>' if state else "")
             + "<br>Открытого PR по задаче нет — мержить нечего."
             + "<br>Задача уйдёт в Done и пропадёт из очереди на подпись.</div>")
    if risk:
        warn = ('<div class="warn"><b>Действие с риском.</b> '
                + " ".join(e(w) + "." for w in risk)
                + " Кода это не меняет — меняется только статус задачи. "
                  "Закрыть как сделанную всё равно?</div>"
                '<label class="chk"><input type="checkbox" name="ack" value="1" required>'
                'Понимаю, что проверка не сошлась, и закрываю задачу сознательно.</label>')
        button = '<button class="danger">Закрыть как сделанную</button>'
    else:
        warn = '<p class="ok">Проверка прошла, ворота прошли — задача сделана.</p>'
        button = "<button>Закрыть как сделанную</button>"
    body = (f"<h1>Закрыть {e(ident)} как сделанную?</h1>{facts}{evidence_html(proof)}{warn}"
            f'<form method="post" action="{e(action)}">'
            f'<input type="hidden" name="task" value="{e(ident)}">'
            f"{button}</form>"
            f'<a class="lnk back" href="{e(base + "?t=" + urllib.parse.quote(token))}">← отмена</a>')
    return _shell(title, "queue", base, token, body)


def confirm_update_html(title: str, version: str, name: str, base: str = "/queue",
                        token: str = "") -> str:
    e = html.escape
    action = f"{base}/update/confirm?t={urllib.parse.quote(token)}"
    body = (f"<h1>Обновить ядро до {e(name)}?</h1>"
            '<div class="warn">Раннер и приём перезапустятся; идущие сессии доработают, '
            "новые пойдут на новом ядре.</div>"
            f'<form method="post" action="{e(action)}">'
            f'<input type="hidden" name="version" value="{e(version)}">'
            "<button>Обновить</button></form>"
            f'<a class="lnk back" href="{e(base + "/config?t=" + urllib.parse.quote(token))}">← отмена</a>')
    return _shell(title, "config", base, token, body)


def core_html(core: dict, base: str, token: str) -> str:
    """Плитка ядра на странице конфигурации: что стоит, что доступно, кнопка."""
    if not core:
        return ""
    e = html.escape
    if core.get("same"):
        return (f'<h4>Ядро</h4><p class="lead">Стоит {e(core["current_name"])} — '
                "это последняя версия.</p>")
    action = f"{base}/update?t={urllib.parse.quote(token)}"
    return (f'<h4>Ядро</h4><p class="lead">Стоит {e(core["current_name"])}, '
            f'доступна {e(core["available_name"])}.</p>'
            f'<form class="act" method="post" action="{e(action)}">'
            f'<input type="hidden" name="version" value="{e(core["available"])}">'
            f'<button>Обновить до {e(core["available_name"])}…</button></form>')


def head_html(head: dict) -> str:
    """Шапка: чем занята фабрика, сколько ей позволено и чего это стоило.

    Потолки — не в раскрывашке, а на виду: человек смотрит сюда, чтобы решить,
    ждать ли следующую порцию, и «2 за 2 ч» — это половина ответа. Форма
    правки остаётся свёрнутой, потому что правят раз в неделю, а смотрят
    каждый день.
    """
    if not head:
        return ""
    e = html.escape
    s = head.get("сессии") or {}
    pull = head.get("тяга") or {}
    tiles = []

    if s or pull:
        idut = s.get("идёт", 0)
        slots = pull.get("max_in_flight", 0)
        tiles.append((
            "Сессии",
            f'{idut}<span class="unit"> из {slots}</span>' if slots else str(idut),
            f"идут сейчас · за сутки {s.get('закончено', 0)}"
            + (f", брошено {s['брошено']}" if s.get("брошено") else "")))

    # На подписи — то, что упирает тягу: агенты сдают быстрее, чем человек
    # подписывает. Работу ограничивают слоты, а копится приёмка.
    waiting_now = head.get("на подписи")
    if waiting_now is not None and pull.get("max_open"):
        cap = pull["max_open"]
        full = waiting_now >= cap
        # Число в дроби ЗАЖАТО потолком: «12 из 7» читается как поломка, а не
        # как «мест нет». Настоящее значение уходит в подпись, когда оно выше.
        shown = min(waiting_now, cap)
        width = min(100, round(100 * waiting_now / cap))
        if not full:
            note = f"есть запас на {cap - waiting_now}"
        elif waiting_now > cap:
            note = f"мест нет, на разборе {waiting_now} — новые не берём"
        else:
            note = "мест нет — новые не берём"
        tiles.append((
            "На подписи",
            f'{shown}<span class="unit"> из {cap}</span>'
            f'<div class="quota{" quota-full" if full else ""}">'
            f'<i style="width:{width}%"></i></div>', note))
    elif pull:
        day, cap = pull.get("взято за сутки", 0), pull.get("max_per_day", 0)
        if cap:
            width = min(100, round(100 * day / cap))
            tiles.append((
                "Взято за сутки",
                f'{day}<span class="unit"> из {cap}</span>'
                f'<div class="quota"><i style="width:{width}%"></i></div>',
                "из бэклога"))

    if pull:
        window = pull.get("window_sec", 0) // 3600 or 1
        tiles.append((
            "Следующая задача",
            e(str(pull.get("следующая порция", ""))),
            f"по {pull.get('per_window', 0)} за {window} ч"
            if pull.get("per_window") else "берёт, как освободится место"))

    if s:
        total = s.get("вход", 0) + s.get("выход", 0) + s.get("кэш", 0)
        tiles.append((
            "Токены за сутки", sess.human_tokens(total),
            f"кэш {sess.human_tokens(s.get('кэш', 0))} · "
            f"выход {sess.human_tokens(s.get('выход', 0))} · "
            f"${s.get('стоимость', 0)}"))

    cells = "".join(
        f'<div class="tile"><div class="tl">{e(name)}</div>'
        f'<div class="tv">{value}</div><div class="tn">{note}</div></div>'
        for name, value, note in tiles)

    return f'<div class="head">{cells}</div>'


BRAND = "Фабрика агентов"


def pace_words(pull: dict) -> str:
    """Настройки одной фразой — тем же языком, каким про них думают."""
    hours = (pull.get("window_sec") or 0) // 3600
    at_once = pull.get("max_in_flight") or 0
    day = pull.get("max_per_day") or 0
    if pull.get("per_window") and hours:
        how = (f"берёт до {at_once} задач, следующие — через {hours} ч"
               if hours > 1 else
               f"берёт до {at_once} задач, следующие — через час")
    else:
        how = "берёт следующую задачу сразу, как освободится место"
    # Ноль — это «без суточного потолка», а не «ноль задач в сутки». Писать
    # «не больше 0 за сутки» под работающей тягой — прямая ложь на экране.
    if not day:
        return f"Агент {how}. Суточный потолок не задан."
    return f"Агент {how}, и не больше {day} за сутки."


# Поля конфигурации: как их называет человек, а не код. Их ТРИ, а не четыре:
# «сколько брать за раз» и «сколько вести одновременно» были про одно и то же —
# агент и берёт ровно столько, сколько может вести. Размер пачки поэтому не
# спрашивается, а равен числу одновременных.
FIELDS = (
    ("max_in_flight", "Сколько задач вести одновременно", 1, 8, "шт.",
     "Столько же он берёт за раз. Ограничение не про машину, а про тебя: "
     "каждая взятая задача кончается разбором и подписью."),
    ("window_hours", "Через сколько часов брать новые", 0, 24, "ч",
     "Ноль — брать следующую сразу, как освободится место. Два — работа "
     "приходит порциями раз в два часа."),
    ("max_open", "Не больше на подписи", 0, 50, "шт.",
     "Сколько сданных задач может ждать разбора. Упирается в тебя, а не в "
     "агентов: накопилось столько — новое не берут, разобрал — снова берут. "
     "Ноль — не смотреть."),
    ("max_per_day", "Не больше за сутки", 0, 50, "шт.",
     "Дневной потолок на всякий случай. Ноль — без него."),
)


def when_words(ts: float, now: float | None = None) -> str:
    """«14 мин назад», «3 ч назад» — время, о котором думают в относительных
    единицах. Абсолютное здесь бесполезно: смотрят «давно ли», а не «когда»."""
    now = time.time() if now is None else now
    d = int(max(0, now - ts))
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} д назад"


def nav_html(here: str, base: str = "/queue", token: str = "") -> str:
    """Боковое меню. Адреса АБСОЛЮТНЫЕ и с токеном.

    Относительная ссылка `config` со страницы `/queue` уводит на `/config` —
    другого префикса и без токена, то есть в никуда. Токен в ссылке
    обязателен по той же причине: куку мы не ставим, а без неё следующая
    страница снова чужая.
    """
    e = html.escape
    tail = f"?t={urllib.parse.quote(token)}" if token else ""
    items = (("queue", "Очередь", f"{base}{tail}"),
             ("runs", "Сессии", f"{base}/runs{tail}"),
             ("config", "Конфигурация", f"{base}/config{tail}"))
    links = "".join(
        f'<a class="nav{" on" if key == here else ""}" href="{e(href)}">{e(name)}</a>'
        for key, name, href in items)
    return ('<input type="checkbox" id="menu" class="menu-in">'
            '<header class="bar"><label for="menu" class="burger">☰</label>'
            f'<span class="brand">{e(BRAND)}</span></header>'
            f'<aside class="side"><span class="brand side-brand">{e(BRAND)}</span>'
            f'{links}</aside>'
            '<label for="menu" class="veil"></label>')


def config_html(title: str, head: dict, saved: bool = False,
                base: str = "/queue", token: str = "", core: dict | None = None) -> str:
    """Конфигурация отдельной страницей: её открывают раз в неделю, и в шапке
    очереди она занимала место, которое нужно цифрам."""
    e = html.escape
    pull = (head or {}).get("тяга") or {}
    if not pull:
        body = '<div class="empty">Тяга не включена — настраивать нечего.</div>'
    else:
        shown = dict(pull)
        shown["window_hours"] = ((pull.get("window_sec") or 0) // 3600
                                 if pull.get("per_window") else 0)
        rows = "".join(
            f'<label class="row2"><span class="cl">{e(name)}</span>'
            f'<span class="ci"><input name="{key}" type="number" '
            f'value="{shown.get(key, "")}" min="{low}" max="{high}">'
            f'<span class="cu">{e(unit)}</span></span>'
            f'<span class="ch">{e(hint)}</span></label>'
            for key, name, low, high, unit, hint in FIELDS)
        action = f"{base}/limits" + (f"?t={urllib.parse.quote(token)}"
                                     if token else "")
        body = (f'<p class="lead">{e(pace_words(pull))}</p>'
                f'<form method="post" action="{e(action)}">' + rows +
                '<button>Применить</button></form>')
    note = '<div class="ok">Сохранено.</div>' if saved else ""
    body += core_html(core or {}, base, token)
    return f'''<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} — конфигурация</title><style>{STYLE}</style></head><body>
{nav_html("config", base, token)}<main><h1>Конфигурация</h1>{note}{body}</main>
</body></html>'''


def check_line_html(p: dict | None) -> str:
    """Свидетельства последней СОСТОЯВШЕЙСЯ проверки — одной строкой.

    На карточке задачи (DEV-100) свидетельства из DEV-94 не пропадают, но и
    не разворачиваются целиком: здесь смотрят «сколько стоило и чем
    кончилось», а полный разбор — ворота с кодами, эталон, построчная
    сверка — лежит на карточке очереди. Поэтому строка называет все три
    свидетельства счётом и итогом и говорит, ОТ КАКОЙ проверки они: имя
    каталога видно, и подменить последнюю проверку первой незаметно нельзя.

    Пустое называется словами. Пропуск подписывающий читает как «всё в
    порядке», а это ровно тот случай, когда ничего не проверяли.
    """
    e = html.escape
    if not p or p.get("нет"):
        why = (p or {}).get("нет") or "состоявшейся проверки по задаче не было"
        return (f'<div class="run-ev">{_mark("свидетельств нет")}'
                f'<span>{e(why)}</span></div>')
    bits = [glance_html(p)]
    ref = p.get("эталон")
    if ref is None:
        bits.append('<span class="g g-err">эталона нет</span>')
    else:
        bits.append(f'<span>эталон: {e(ref.get("источник") or "не назван")}</span>')
    rows = p.get("сверка") or []
    if rows:
        ok = sum(1 for r in rows if r["ответ"] == ev.MATCHED)
        good = " g-ok" if ok == len(rows) else " g-bad"
        bits.append(f'<span class="g{good}">сверка {ok}/{len(rows)}</span>')
    else:
        bits.append('<span class="g g-err">сверки нет</span>')
    bits.append(f'<span>проверка <code>{e(p.get("сессия") or "")}</code></span>')
    if p.get("вердикт"):
        bits.append(f'<span>{e(short(p["вердикт"], 120))}</span>')
    return f'<div class="run-ev">{"".join(bits)}</div>'


def stages_html(row: dict) -> str:
    """Заходы задачи столбиком: значок, чем был, чем кончился, чего стоил.

    Свежее сверху — человек смотрит на итог, а не на историю: последнее, что
    случилось с задачей, не должно требовать прокрутки. Считает их
    `runs.stages` по порядку событий, поэтому список здесь переворачивается,
    а не собирается задом наперёд.
    """
    e = html.escape
    out = []
    for st in reversed(rn.stages(row)):
        what = st["чем кончился"]
        if st["остановка"]:
            what += f" · {st['остановка']}"
        cost = f"${st['usd']:.2f}" if st["usd"] else "—"
        dur = rn.human_dur(st["секунд"]) if st["секунд"] else "—"
        out.append(f'<li><span class="ic">{e(st["значок"])}</span>'
                   f'<span class="sw">{e(st["этап"])}</span>'
                   f'<span class="so">{e(what)}</span>'
                   f'<span class="sc">{e(dur)} · {e(cost)}</span></li>')
    return f'<ol class="stages">{"".join(out)}</ol>' if out else ""


def runs_html(title: str, sessions: list[dict], head: dict,
              base: str = "/queue", token: str = "",
              verdicts: dict | None = None, issue_url=None,
              proofs: dict | None = None) -> str:
    """Задачи за сутки: одна задача — одна карточка, заходы внутри неё.

    Пока карточка равнялась сессии, задача с пятью заходами давала пять
    карточек, и человек собирал её глазами: пять раз один номер, пять цен,
    ни одной суммы (DEV-100). Поэтому единица показа здесь — та же, что
    единица учёта в `runs.by_task`: ЗАДАЧА. Заходы никуда не деваются —
    они внутри, столбиком, каждый со своим значком, исходом и ценой.

    Столбцов ровно столько, сколько влезает на телефон и сколько нужно, чтобы
    понять «дорого ли и почему»: длительность, пик памяти, токены и деньги —
    и всё это ПО ЗАДАЧЕ ЦЕЛИКОМ, то есть сравнимо с ценой человеко-часа.
    Оценка — отдельной строкой под ними, потому что ставит её человек, и
    кнопки не должны мешаться с числами.
    """
    e = html.escape
    verdicts = verdicts or {}
    proofs = proofs or {}
    s = (head or {}).get("сессии") or {}
    tail = f"?t={urllib.parse.quote(token)}" if token else ""
    cards = []
    for row in rn.by_task(sessions, verdicts):
        task = row["задача"]
        cost = f"${row['стоимость']:.2f}" if row["стоимость"] else "—"
        mem = f"{row['пик_мб']} МБ" if row["пик_мб"] else "—"
        dur = rn.human_dur(row["секунд"]) if row["секунд"] else "—"
        tokens = sess.human_tokens(row["вход"] + row["выход"] + row["кэш"])
        state = ('<span class="run-on">идёт</span>' if row["идёт"] else
                 '<span class="run-bad">брошена</span>' if row["брошена"] else "")
        mine = row["оценка"]
        marks = "".join(
            f'<button name="mark" value="{"" if mine == key else key}" '
            f'class="mk{" mk-on" if mine == key else ""}" title="{e(hint)}">'
            f'{icon} {e(name)}</button>'
            for key, icon, name, hint in MARKS)
        # Оценка кладётся на ОДИН заход задачи (`оценить`), а читается по всем:
        # оценивают работу, а работа — это задача целиком.
        form = (f'<form class="marks" method="post" action="{e(base)}/mark{e(tail)}">'
                f'<input type="hidden" name="session" value="{e(row["оценить"])}">'
                f'{marks}</form>')
        link = ""
        url = issue_url(task) if issue_url else ""
        if url:
            link = f' <a class="run-link" href="{e(url)}" target="_blank">↗</a>'
        word = rn.outcome_word(row["исход"], str(row.get("режим") or ""))
        tries = f'{row["заходов"]} заход(а/ов)'
        extra = " · ".join(rn.extras(row))
        cards.append(
            f'<div class="run"><div class="run-h"><b>{e(task)}</b>{link}'
            f'<span class="dim">{e(tries)} · {e(word)}'
            f' · {e(when_words(row["закончено"]))}</span>{state}</div>'
            f'{stages_html(row)}'
            f'<div class="run-n"><span>{dur}</span><span>{mem}</span>'
            f'<span>{tokens}</span><span>{cost}</span></div>'
            + (f'<div class="ex">{e(extra)}</div>' if extra else "")
            # У приёмной пачки задачи в трекере ещё нет, и проверки выката у
            # неё не бывает по построению: «свидетельств нет» на ней — не
            # факт, а шум, от которого та же строка на настоящей задаче
            # теряет вес.
            + ("" if row["постановка"] else check_line_html(proofs.get(task)))
            + f'{form}</div>')
    body = "".join(cards) or '<div class="empty">За сутки сессий не было.</div>'

    lead = ""
    if s:
        v = counted(verdicts)
        value = ""
        if v["оценено"]:
            value = (f' Оценено {v["оценено"]}: толк у {v["толк"]}% того, за что '
                     f'отвечает агент')
            if v["впустую"]:
                value += (f'; {v["впустую"]}% работы ушло на то, что не стоило '
                          f'начинать')
            value += "."
        lead = (f'<p class="lead">За сутки {s.get("закончено", 0)} сессий, '
                f'{sess.human_tokens(s.get("вход", 0) + s.get("выход", 0) + s.get("кэш", 0))} '
                f'токенов, ${s.get("стоимость", 0)}. Из них кэш — '
                f'{sess.human_tokens(s.get("кэш", 0))}: это перечитывание '
                f'контекста, а не написанное. Карточка — задача целиком: '
                f'заходы, деньги и свидетельства собраны в одну.{value}</p>')
    return f'''<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} — сессии</title><style>{STYLE}</style></head><body>
{nav_html("runs", base, token)}<main><h1>Сессии за сутки</h1>{lead}
<div class="run-head"><span>задача</span><span>время</span><span>память</span>
<span>токены</span><span>деньги</span></div>{body}</main>
</body></html>'''


def page_html(title: str, working: list[dict], checking: list[dict],
              waiting: list[dict],
              error: str = "", head: dict | None = None,
              base: str = "/queue", token: str = "") -> str:
    """Две вкладки, без единого байта JavaScript: переключение на radio+CSS.

    Страница открывается с телефона, часто по плохой связи, и внутри Telegram
    как мини-приложение. Каждый скрипт здесь — ещё один способ показать белый
    экран вместо списка задач.
    """
    e = html.escape
    # Переключатели идут ОДНИМ рядом, до обеих панелей. Раньше вторая кнопка
    # стояла после первой панели и уезжала вниз вместе с её карточками —
    # «на подпись» плясала то вверху, то под списком.
    # Порядок вкладок — порядок жизни задачи: работа → проверка → подпись.
    # «На проверке» стоит между ними не для красоты: увидев задачу там, человек
    # понимает, что делать ничего не нужно, надо подождать. Без этой вкладки
    # она пропадала со страницы совсем, и ожидание выглядело как пропажа.
    pairs = (("work", "В работе", working, "Сейчас никто ничего не делает."),
             ("check", "На проверке", checking, "Ничего не проверяется."),
             ("sign", "На подпись", waiting, "Ничего не ждёт подписи."))
    tabs = [f'<input type="radio" name="tab" id="t-{key}" class="tab-in"'
            f'{" checked" if i == 0 else ""}>'
            for i, (key, *_rest) in enumerate(pairs)]
    tabs.append('<nav class="tabs">' + "".join(
        f'<label for="t-{key}" class="tab">{e(name)} <b>{len(items)}</b></label>'
        for key, name, items, _ in pairs) + '</nav>')
    for key, name, items, empty in pairs:
        body = ("".join(card_html(t, base, token) for t in items)
                or f'<div class="empty">{empty}</div>')
        tabs.append(f'<section class="pane pane-{key}">{body}</section>')
    err = f'<div class="err">Трекер не ответил: {e(error)}</div>' if error else ""
    top = nav_html("queue", base, token) + head_html(head or {})
    return f'''<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{STYLE}</style></head><body>
{top}<main>{err}{"".join(tabs)}</main>
</body></html>'''
