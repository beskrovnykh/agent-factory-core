"""Подпись работы из чата: «Слить» и «Отменить» под сообщением задачи.

Зачем. Решение по сделанной работе принимается на странице подписи
(`signing.SigningPage`) — там материал для решения. Но у принятой работы
решение уже принято, и остаётся одно действие: смержить. Ради него человек
открывает страницу, ждёт обхода хостинга по семи клонам и жмёт через два
подтверждения. Владелец 16.09.2026: «мерж должен быть одним нажатием в боте,
иначе труба дело».

Поэтому под сообщением задачи, пришедшей к человеку со сдачей, стоят две
кнопки. «Слить» мержит открытый PR и переводит задачу в `Done` — той же
дорогой, что кнопка на странице и что событие хостинга (`Verification.
accept_merged`), другой дороги в `Done` в продукте нет и быть не должно.
«Отменить» PR не трогает вовсе: закрыть чужой PR — отдельное решение, и
принимать его за человека нельзя; задача уезжает в `Canceled` с
комментарием, по которому через месяц видно, что это было решение человека,
а не исход сессии.

Часть ПРОДУКТА: клавиатура и разбор нажатия одинаковы у любой инсталляции.
За инсталляцией — хостинг (`Hosting`) и перевод задачи (`Verification`), то
есть ровно то, что и так за ней.

Три вещи, которые здесь решены и которые легко забыть (все три стоили
первому клиенту по инциденту):

  • «часики» на кнопке гасятся ДО походов в сеть. Мерж — это `gh` по
    нескольким репозиториям, секунды; всё это время кнопка выглядит
    незажатой, и человек жмёт её снова. Гасит фронт (`telegram._on_click`),
    до вызова канала, — и он же показывает словом, что началось;

  • повторное нажатие не врёт. Открытого PR больше нет — это не «мержить
    нечего»: чаще всего его смержило первое нажатие. Поэтому спрашиваем
    хостинг о СМЕРЖЕННОМ и говорим «уже было смержено», называя номер;

  • осечка трекера видна в чате. Мерж прошёл, а статус не сменился — это
    две разные новости, и вторая без первой означает «работа в main, а
    задача висит в ревью». В журнале её видит только тот, кто туда смотрит.

Сработавшее нажатие снимает кнопки само. Закрытие задачи снимает их и
через ленту (`issue_feed.show` → `repaint(buttons=[])` по закрытой задаче),
но это событие трекера: доставку могли не настроить, служба могла лежать, а
кнопка «Слить» под уже смерженной задачей обещает сделать мерж ещё раз.
Снимаем ЧЕРЕЗ ЛЕНТУ, а не правкой сообщения мимо неё: кнопки лежат в её
памяти вместе с текстом, и снятая мимо памяти клавиатура вернулась бы на
первой же перерисовке по смене статуса. Не сработавшее нажатие (нет PR,
хостинг молчит, мерж не прошёл) кнопки оставляет: единственный способ
повторить не отбирают вместе с кнопкой.
"""
from __future__ import annotations

import asyncio
import logging
import re

from .intake import Button

log = logging.getLogger("intake")

# Хвосты нажатий. Своя приставка, а не `intake:`: у приёма она про пачку и
# карточки, а это решение по уже сделанной работе.
MERGE = "sign:merge:"
CANCEL = "sign:cancel:"

# Идентификатор задачи — тот же вид, что у страницы подписи. Нажатие
# приходит снаружи, и подставить в него что угодно ничего не стоит: в
# хостинг и в трекер уходит только то, что похоже на задачу.
IDENT = re.compile(r"[A-Z]{2,6}-\d+")

# Сколько держится захват «по этой задаче действие уже идёт». Кнопка в
# мессенджере жмётся дважды на раз — вторым нажатием, пока первое ходит в
# `gh`. Срок короткий: захват не решение, а защёлка на время похода, и
# висеть после осечки ему незачем — с осечки он снимается сам.
BUSY_TTL_SEC = 60

# Что человек читает вместо машинного слова исхода перевода задачи. Те же
# слова, что на странице подписи (`signing`): один исход — одна фраза, иначе
# чат и страница объясняют одно и то же по-разному.
MOVED = {
    "done": "Задача переведена в Done.",
    "canceled": "Задача переведена в Canceled.",
    "already-closed": "Задача уже была закрыта.",
    "no-issue": "Задачи в трекере нет — переводить нечего.",
    "no-tracker": "Трекер не подключён — переведи задачу руками.",
    "tracker-failed": "⚠️ Трекер не ответил — переведи задачу руками.",
    "no-verification": "⚠️ Перевести задачу нечем — переведи в Done руками.",
}

# Исходы перевода, после которых кнопки снимать НЕЛЬЗЯ: задача осталась
# открытой не потому, что так решили, а потому что перевод не прошёл. Второе
# нажатие такой перевод доводит до конца (`_already_merged`) — сними кнопку,
# и единственный способ починить это из чата отобран вместе с ней.
KEEP_BUTTONS = ("tracker-failed", "no-tracker", "no-verification")


def who_pressed(clicker: str = "", clicker_id=None) -> str:
    """Кто нажал — для комментария в задаче и журнала. Имя И идентификатор:
    по имени человек узнаёт себя и коллегу, по идентификатору нажатие
    сходится с журналом приёма, где имён нет. Одно из двух пусто —
    остаётся второе; оба пусты — пусто, и подпись просто не называет никого.
    """
    name = str(clicker or "").strip()
    uid = str(clicker_id if clicker_id is not None else "").strip()
    if name and uid:
        return f"{name} ({uid})"
    return name or uid


def handles(data: str) -> bool:
    """Нажатие наше? Спрашивает роутер канала, чтобы не знать про хвосты."""
    return str(data or "").startswith((MERGE, CANCEL))


def keyboard(identifier: str, has_request: bool) -> list[Button]:
    """Кнопки под сдачей. `has_request` — есть ли открытый PR по задаче.

    Кнопка стоит только там, где она применима (то же правило, что у
    карточки на странице подписи, DEV-120): мержить нечего — «Слить» нет
    вовсе, иначе единственное, что она может ответить, это «нечего».

    «Отменить» стоит всегда: она про задачу, а не про PR, и задаче без
    ветки отмена нужна ровно так же.
    """
    ident = str(identifier or "").strip()
    if not ident:
        return []
    out = []
    if has_request:
        out.append(Button("✅ Слить", MERGE + ident))
    out.append(Button("🚫 Отменить", CANCEL + ident))
    return out


class SignOff:
    """Клавиатура сдачи (`issue_feed.Handoff`) и разбор её нажатий.

    `hosting` — протокол `hosting.Hosting`: есть ли открытый PR, смержить,
    был ли смерженный. Нет хостинга — кнопки «Слить» нет ни у одной задачи:
    мержить нечем, и обещать это кнопкой хуже, чем не показать её.

    `verification` — `deploy.Verification`: дорога задачи в `Done` и в
    `Canceled`. Нет — нажатие скажет, что переводить некому, и это лучше
    молчания.

    `may_act(user_id) -> bool` — кому вообще можно нажать. Отдельно от
    допуска в приём: приём это «положить кусок в пачку», а здесь мерж в
    основную ветку. Не передан — решает вызывающий (фронт уже проверил
    допуск).

    `store` — захват на время действия, чтобы второе нажатие не пошло во
    второй `gh merge`. Нет стора — работаем без него: пропущенный захват
    хуже неработающей кнопки только в одном случае, и он не наступает —
    второе нажатие увидит, что открытого PR больше нет.

    `feed` (`issue_feed.IssueFeed`) и `lookup` (`ident -> задача трекера`) —
    чем снять кнопки после сработавшего нажатия. Порознь бесполезны: лента
    правит сообщения по id задачи, а нажатие приносит её идентификатор. Нет
    того или другого — кнопки снимет событие трекера о закрытии, и это
    сказано в журнале в момент нажатия, а не при старте.
    """

    def __init__(self, hosting=None, verification=None, transport=None,
                 store=None, may_act=None, busy_ttl_sec: int = BUSY_TTL_SEC,
                 feed=None, lookup=None):
        self.hosting = hosting
        self.verification = verification
        self.transport = transport
        self.store = store
        self.may_act = may_act
        self.busy_ttl_sec = busy_ttl_sec
        self.feed = feed
        self.lookup = lookup

    # ── клавиатура под сдачей ────────────────────────────────────────────

    async def on_handoff(self, payload: dict, text: str) -> tuple[str, list[Button]]:
        """Протокол `issue_feed.Handoff`: что показать под сдачей.

        Зовётся из разбора события трекера, то есть в чужом такте: поход в
        хостинг уводим в поток, иначе он держит доставку события.
        """
        data = (payload or {}).get("data") or {}
        ident = str(data.get("identifier") or "").strip()
        if not ident or not IDENT.fullmatch(ident):
            return text, []
        req = await self._open_request(ident)
        return self._with_request(text, req), keyboard(ident, req is not None)

    def _with_request(self, text: str, req: dict | None) -> str:
        """Строка про PR рядом с кнопкой: человек жмёт «Слить», зная, ЧТО
        сольётся. Без неё нажатие — ставка: в сообщении задачи номера PR
        нет, а открытых по задаче бывает два."""
        if not req or not req.get("number"):
            # Номера нет — хостинг молчал (`_open_request`). Кнопку мы
            # показываем, а строку про PR сочинять не из чего.
            return text
        m = getattr(self.transport, "markup", None)
        where = f"PR #{req.get('number') or '?'} в {req.get('repo') or '?'}"
        refs = f"{req.get('source_ref') or '?'} → {req.get('target_ref') or '?'}"
        if m is None:
            return f"{text}\n🔀 {where} ({refs})"
        # Текст ссылки экранируется так же, как остальное: имя репозитория
        # и ветки приходят от хостинга, и `_` в них (`dok_engine`,
        # `agent/DEV-1_fix`) в legacy-Markdown открывает курсив. Непарный
        # символ — это 400 «can't parse entities», то есть молча потерянное
        # сообщение о сдаче вместе с кнопками.
        safe = m.safe(where)
        head = m.link(safe, req["url"]) if req.get("url") else safe
        return f"{text}\n🔀 {head} ({m.safe(refs)})"

    async def _open_request(self, ident: str) -> dict | None:
        """Открытый PR по задаче. Молчащий хостинг — НЕ «PR нет».

        Разница видна на кнопке: «нет PR» прячет «Слить» насовсем, а
        «хостинг не ответил» прячет действие, которое на самом деле есть.
        Поэтому при осечке считаем, что PR есть: нажатие спросит хостинг
        заново и скажет прямо, что случилось. Спрятать кнопку молча — это
        «мерж из чата иногда не работает, и почему, не видно».
        """
        if self.hosting is None:
            return None
        try:
            return await asyncio.to_thread(self.hosting.open_request, ident)
        except Exception as e:
            log.warning("подпись из чата %s: хостинг не ответил (%s) — "
                        "показываю «Слить», нажатие спросит заново", ident, e)
            return {"number": "", "repo": "", "url": "", "source_ref": "",
                    "target_ref": "", "title": "", "mergeable": None}

    # ── нажатие ──────────────────────────────────────────────────────────

    async def on_click(self, data: str, chat_id, message_id: str = "",
                       clicker: str = "", clicker_id=None) -> str:
        """Нажатие «Слить» или «Отменить». Возвращает исход — для журнала и
        тестов. Спиннер гасит вызывающий ДО этого вызова."""
        raw = str(data or "")
        action = "merge" if raw.startswith(MERGE) else "cancel"
        ident = raw[len(MERGE if action == "merge" else CANCEL):].strip()
        if not IDENT.fullmatch(ident):
            log.warning("подпись из чата: не понял задачу в %r", raw)
            await self._say(chat_id, "Не понял, по какой задаче нажато.")
            return "bad-ident"
        if self.may_act is not None and not self.may_act(clicker_id):
            log.warning("подпись из чата %s: %s (%s) не вправе %s",
                        ident, clicker or "?", clicker_id, action)
            await self._say(chat_id, "⚠️ Решение по работе принимает тот, кто "
                                     "её подписывает — нажатие не выполнено.")
            return "not-allowed"
        if not await self._claim(action, ident):
            log.info("подпись из чата %s: %s уже идёт — второе нажатие пропускаю",
                     ident, action)
            await self._say(chat_id, f"{ident}: уже работаю над этим, секунду.")
            return "busy"
        who = who_pressed(clicker, clicker_id)
        try:
            if action == "merge":
                return await self._merge(ident, chat_id, who)
            return await self._cancel(ident, chat_id, who)
        finally:
            # Захват — защёлка на время похода, а не память о сделанном:
            # держать его после осечки значит запереть повтор на минуту.
            await self._release(action, ident)

    async def _merge(self, ident: str, chat_id, who: str) -> str:
        if self.hosting is None:
            log.warning("подпись из чата %s: мержить нечем — хостинг не подключён",
                        ident)
            await self._say(chat_id, f"{ident}: хостинг не подключён — мержить "
                                     f"нечем, смержи в хостинге руками.")
            return "no-hosting"
        try:
            req = await asyncio.to_thread(self.hosting.open_request, ident)
        except Exception as e:
            log.warning("подпись из чата %s: хостинг не ответил: %s", ident, e)
            await self._say(chat_id, f"{ident}: хостинг не ответил ({e}) — "
                                     f"не мержу. Нажми ещё раз.")
            return "hosting-failed"
        if not req or not req.get("number"):
            return await self._already_merged(ident, chat_id, who)

        log.warning("подпись из чата: %s мержит %s — %s#%s (%s → %s)", who or "?",
                    ident, req.get("repo"), req.get("number"),
                    req.get("source_ref"), req.get("target_ref"))
        try:
            outcome = await asyncio.to_thread(self.hosting.merge, req)
        except Exception as e:
            log.error("подпись из чата %s: мерж не прошёл: %s", ident, e)
            await self._say(chat_id, f"{ident}: мерж не прошёл — {e}")
            return "merge-failed"
        log.info("подпись из чата %s: %s", ident, outcome)
        moved = await self._accept_merged(ident, req, who)
        if moved not in KEEP_BUTTONS:
            await self._drop_buttons(ident)
        await self._say(chat_id, f"✅ {ident}: {outcome}\n{MOVED.get(moved, moved)}")
        return "merged"

    async def _already_merged(self, ident: str, chat_id, who: str) -> str:
        """Открытого PR нет. Врать «смержено» тут нельзя, и говорить
        «мержить нечего» — тоже: чаще всего это ВТОРОЕ нажатие по тому, что
        уже смержено первым, и человеку нужен ответ про это, а не про
        отсутствие ветки.

        Заодно доводим дело до конца: смерженный PR есть, а задача открыта —
        значит первое нажатие смержило, а перевод статуса не прошёл.
        """
        merged = None
        ask = getattr(self.hosting, "merged_request", None)
        if callable(ask):
            try:
                merged = await asyncio.to_thread(ask, ident)
            except Exception as e:
                log.warning("подпись из чата %s: смерженные PR не спросить: %s",
                            ident, e)
        if not merged:
            log.info("подпись из чата %s: открытого PR нет и смерженного нет", ident)
            await self._say(chat_id, f"{ident}: открытого PR нет — мержить нечего. "
                                     f"Если работа без кода, жми «Отменить» или "
                                     f"закрой задачу в трекере.")
            return "nothing-to-merge"
        where = f"{merged.get('project_path') or '?'}#{merged.get('mr_iid') or '?'}"
        log.info("подпись из чата %s: PR %s уже был смержен (%s)", ident, where,
                 merged.get("merged_at") or "когда — хостинг не сказал")
        moved = await self._accept_merged(
            ident, {"number": merged.get("mr_iid") or "",
                    "repo": merged.get("project_path") or "",
                    "url": merged.get("url") or "",
                    "source_ref": merged.get("source_ref") or "",
                    "target_ref": merged.get("ref") or ""}, who)
        if moved not in KEEP_BUTTONS:
            await self._drop_buttons(ident)
        when = merged.get("merged_at") or ""
        # Ветку называем: `merged_request` ищет по имени ветки задачи, а их у
        # задачи бывает две (`agent/DEV-66` и `agent/DEV-66-v2`). Если
        # хостинг не перечислил открытые PR какого-то репозитория (он
        # отвечает на это пустым списком, а не осечкой), сюда попадёт старый
        # мерж — и человек должен видеть, ЧТО именно считается смерженным.
        branch = merged.get("source_ref") or ""
        await self._say(chat_id, f"{ident}: PR {where} уже был смержен"
                                 f"{' ' + when if when else ''}"
                                 f"{' (ветка ' + branch + ')' if branch else ''}"
                                 f", второй раз не мержу.\n{MOVED.get(moved, moved)}")
        return "already-merged"

    async def _accept_merged(self, ident: str, req: dict, who: str) -> str:
        """Дорога в `Done` — та же, что у кнопки на странице и у события
        хостинга. Возвращает ИСХОД (`MOVED`), а не фразу: по нему решается
        не только что сказать, но и снимать ли кнопки. Осечка трекера обязана
        доехать до чата, иначе работа в main, а задача висит в ревью, и видно
        это только в журнале."""
        accept = getattr(self.verification, "accept_merged", None) if self.verification else None
        if not callable(accept):
            log.warning("подпись из чата %s: перевести задачу некому", ident)
            return "no-verification"
        got = await accept(ident, {
            "identifier": ident, "mr_iid": req.get("number") or "",
            "ref": req.get("target_ref") or "", "source_ref": req.get("source_ref") or "",
            "project_path": req.get("repo") or "", "url": req.get("url") or "",
            "merged_by": who}, by=who)
        return str(got)

    async def _cancel(self, ident: str, chat_id, who: str) -> str:
        """Отмена. Хостинг не спрашиваем вовсе и PR не трогаем: закрыть чужой
        PR — отдельное решение, а нажатие сказано про задачу."""
        accept = getattr(self.verification, "accept_canceled", None) if self.verification else None
        if not callable(accept):
            log.warning("подпись из чата %s: отменить некем — приём не подключён",
                        ident)
            await self._say(chat_id, f"{ident}: перевести задачу нечем — переведи "
                                     f"в Canceled руками.")
            return "no-tracker"
        log.warning("подпись из чата: %s отменяет %s (PR не трогаю)", who or "?", ident)
        got = await accept(ident, by=who)
        if got in ("canceled", "already-closed"):
            await self._drop_buttons(ident)
        await self._say(chat_id, f"🚫 {ident}: {MOVED.get(got, str(got))}\n"
                                 f"Открытый PR, если он есть, оставлен как был — "
                                 f"закрыть его отдельное решение.")
        return "canceled" if got in ("canceled", "already-closed") else str(got)

    # ── мелочь ───────────────────────────────────────────────────────────

    async def _drop_buttons(self, ident: str) -> None:
        """Снять кнопки под сообщением задачи: нажатие состоялось.

        Через ленту, а не через транспорт: кнопки лежат в её памяти вместе с
        текстом, и правка статуса возвращает их оттуда (`buttons=KEEP`).
        Сними клавиатуру мимо памяти — следующая же смена статуса вернула бы
        «Слить» под смерженной задачей.

        Второй слой, а не единственный: закрытие задачи снимает кнопки и
        само, событием трекера (`issue_feed.on_event`). Но событие приходит,
        когда доставку настроили и служба на ногах, а кнопка обязана гаснуть
        от собственного нажатия.

        Осечка сюда не поднимается: мерж уже прошёл, и падать после него
        значит показать человеку ошибку на состоявшемся действии.
        """
        if self.feed is None or not callable(self.lookup):
            log.warning("подпись из чата %s: нажатие сработало, а снять кнопки "
                        "нечем — %s", ident,
                        "ленты нет" if self.feed is None else "задачу не найти")
            return
        try:
            issue = await asyncio.to_thread(self.lookup, ident)
            issue_id = str((issue or {}).get("id") or "")
            if not issue_id:
                log.warning("подпись из чата %s: задачи в трекере нет — кнопки "
                            "снимать не под чем", ident)
                return
            await self.feed.repaint(issue_id, buttons=[])
        except Exception as e:
            log.warning("подпись из чата %s: кнопки не сняты: %s", ident, e)

    async def _say(self, chat_id, text: str) -> None:
        if self.transport is None or chat_id in (None, ""):
            log.warning("подпись из чата: ответить некуда — %s", text)
            return
        try:
            await self.transport.send(chat_id, text)
        except Exception as e:
            log.warning("подпись из чата: ответ не ушёл (%s): %s", e, text)

    def _busy_key(self, action: str, ident: str) -> str:
        return f"sign:busy:{action}:{ident}"

    async def _claim(self, action: str, ident: str) -> bool:
        if self.store is None:
            return True
        try:
            return bool(await self.store.claim(self._busy_key(action, ident),
                                               self.busy_ttl_sec))
        except Exception as e:
            # Лежащий стор не повод не мержить: хуже двух мержей только
            # неработающая кнопка, а второй мерж хостинг отвергнет сам.
            log.warning("подпись из чата %s: захват пропущен (%s)", ident, e)
            return True

    async def _release(self, action: str, ident: str) -> None:
        if self.store is None:
            return
        try:
            await self.store.drop(self._busy_key(action, ident))
        except Exception as e:
            log.warning("подпись из чата %s: захват не снят (%s) — повтор "
                        "возможен через %dс", ident, e, self.busy_ttl_sec)
