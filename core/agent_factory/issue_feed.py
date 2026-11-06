"""Жизнь задачи в чате: одно сообщение на задачу, от назначения до исхода.

Событие трекера приезжает — человек видит его в мессенджере. Назначили на
него — сообщение с упоминанием; сменился статус — правится то же сообщение,
а не приходит новое; агент сдал работу — под сообщением появляются кнопки;
задачу закрыли на приёмнике — она уезжает обратно автору.

Часть ПРОДУКТА, и это главное в модуле. До 11.09.2026 всё перечисленное жило
у первого клиента: у второй инсталляции при назначении в чате не появлялось
НИЧЕГО, а продукт умел слать только ошибки сессий. Одно и то же поведение
приходилось бы чинить дважды.

Что остаётся за инсталляцией — и только это:

  • карта людей (`people.py`): кому писать лично, кто владелец;
  • чат по умолчанию: куда писать, если человека в карте нет;
  • `Reviewer`: кто принимает работу прямо сейчас (владелец или делегат);
  • `Handoff`: что происходит, когда агент сдал работу. Это единственное
    место, где нужен хостинг репозиториев — подписать MR можно только зная,
    GitLab там или GitHub. Продукт про это не знает и знать не должен.

Почему одно сообщение, а не лента: на одну задачу приходит назначение, сдача
и исход проверки выката. Тремя сообщениями чат превращается в поток
неотличимых дублей с одним заголовком, и текущее состояние задачи в нём
приходится искать.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

from .intake import Button
from .people import People, creator_id
from .webhook import assignee_id, event_stamp, status_change

log = logging.getLogger("intake")

# Сколько помним сообщение задачи. Меньше — и правка статуса через месяц
# найдёт пустоту, а человек получит второе сообщение о той же задаче.
TTL_SEC = 30 * 24 * 3600

# Типы состояний, в которых задача уже закрыта. Тип, а не имя: имена у каждой
# команды свои, а типы у трекера общие.
CLOSED_TYPES = {"completed", "canceled"}

# Запасной признак — на случай, если состояние придёт без типа. Без него
# проверка молча выродилась бы в «ничего не отсеиваем», то есть в тот же баг.
CLOSED_NAMES = {"done", "canceled", "cancelled"}

# Потолок карты сообщений задачи: сколько сообщений перерисовка тронет за
# раз. Настоящих сообщений на задачу бывает одно-два (личка исполнителя,
# общий чат), десятки — уже поломка. Потолок стоит не вместо разбора
# поломки, а на случай, когда разобрать её не успели: 17.09.2026 у DEV-108 в
# карте лежало 4751 запись на два настоящих сообщения, и перерисовка на
# каждой смене статуса шла править их ВСЕ. Телеграм отвечал 429 и закрывал
# дверь боту на минуты — дважды за сутки.
#
# Режется ХВОСТ, то есть остаются самые свежие: карта дописывается с конца, и
# сообщение, про которое человеку сейчас говорят, лежит последним.
MAX_REFS = 40

# «Не трогать кнопки». Отдельное значение, а не None: None у транспорта
# означает «снять», и перепутать их — значит потерять «Мерж» под сдачей на
# первой же смене статуса.
KEEP = object()


# ── что произошло ────────────────────────────────────────────────────────

def issue_is_closed(issue: dict) -> bool:
    """Задача закрыта — `Done` или `Canceled`. Значит, подписывать под ней
    нечего: у не-кодовой задачи и у «не берусь» MR нет по определению."""
    state = (issue or {}).get("state") or {}
    if str(state.get("type") or "").strip().lower() in CLOSED_TYPES:
        return True
    return str(state.get("name") or "").strip().lower() in CLOSED_NAMES


def closes_issue(payload: dict, agent_user_id: str = "") -> bool:
    """Событие — ЗАКРЫТИЕ задачи, а не что угодно по уже закрытой.

    Разница существенная для обоих потребителей: и возврат автору, и уборка в
    архив должны случиться один раз, в момент закрытия. По «задача закрыта»
    правка описания спустя недели швыряла бы её автору и заново лезла бы в
    архив.

    Закрытие в две мутации (сначала статус, потом исполнитель) приезжает
    вторым событием уже без `stateId` — поэтому второе условие: работа ушла
    от агента, а задача при этом закрыта.
    """
    if payload.get("type") != "Issue" or payload.get("action") != "update":
        return False
    data = payload.get("data") or {}
    if not data.get("id") or not issue_is_closed(data):
        return False
    updated = payload.get("updatedFrom") or {}
    return bool("stateId" in updated or (
        agent_user_id
        and str(updated.get("assigneeId") or "") == str(agent_user_id)))


def is_agent_handoff(payload: dict, agent_user_id: str) -> bool:
    """Сдача: работа ушла ОТ агента к человеку. Только здесь у сообщения есть
    смысл в кнопках — работа готова и ждёт решения.

    Смотрим на ПРЕЖНЕГО исполнителя, а не на того, кто сделал событие. По
    автору события не выходит: агент ходит в трекер под ключом владельца, и
    автором сдачи приезжает владелец. У первого клиента условие по автору не
    выполнилось НИ РАЗУ — кнопок в проде не было ни на одной задаче.

    Прежний исполнитель врать не может: задача попала к агенту тем, что её на
    него назначили, и уходит от него, только когда он закончил. Тем же
    ловится и путь аварии — упавшая сессия тоже возвращает задачу человеку, и
    разводить эти два случая незачем: решение всё равно принимает человек.

    Одного признака мало: сдача бывает и без чего-либо на подпись — не-кодовую
    задачу агент закрывает сам. Это отсекается отдельно, по закрытости задачи.
    """
    if not agent_user_id:
        # Иначе снятие исполнителя на площадке без агента («было пусто»)
        # сравнялось бы с пустым именем агента и сошло за сдачу.
        return False
    # Сравнение заодно отсекает всё, где исполнитель не менялся: там прежнего
    # значения нет, и пустое с именем агента не совпадёт.
    return str((payload.get("updatedFrom") or {}).get("assigneeId") or "") == str(agent_user_id)



# ── что за инсталляцией ──────────────────────────────────────────────────

class Reviewer(Protocol):
    """Кто принимает работу агента прямо сейчас.

    Отдельно от владельца: приём можно передать на время, и тогда закрытая
    задача считается «дошедшей до приёмника» и у делегата тоже.
    """

    async def user_id(self) -> str: ...


class Handoff(Protocol):
    """Агент сдал работу — что с этим делает инсталляция.

    Единственное место продукта, где нужен хостинг репозиториев: подписать
    MR можно только зная, GitLab там или GitHub. Поэтому здесь протокол, а не
    реализация, — ровно как у обновления ядра с его `Source`.
    """

    async def on_handoff(self, payload: dict, text: str) -> tuple[str, list[Button]]:
        """Возвращает текст сообщения и кнопки под ним.

        Зовётся ТОЛЬКО на настоящей сдаче: что ею считать, решает продукт
        (`is_agent_handoff`), иначе правило пришлось бы держать в двух местах
        и оно бы разошлось. Инсталляции остаётся «что показать».
        """
        ...


# ── лента ────────────────────────────────────────────────────────────────

class IssueFeed:
    def __init__(self, store, transport, tracker, people: People,
                 notify_chat: str = "", agent_user_id: str = "",
                 team_id: str = "", reviewer: Reviewer | None = None,
                 handoff: Handoff | None = None, ttl_sec: int = TTL_SEC,
                 archiver=None):
        """`team_id` — писать только про задачи этой команды. Пусто — про все.

        Нужен там, где чат общий на весь отдел, а агент ведёт одну команду:
        чужие задачи в нём не сигнал, а шум.

        `archiver` (`archive.Archiver`) — уборка закрытой задачи из активных.
        Здесь потому, что здесь уже разобрано «задача закрылась»: свой обход
        был бы вторым источником правды о том же событии.
        """
        self.store = store
        self.transport = transport
        self.tracker = tracker
        self.people = people
        self.notify_chat = str(notify_chat or "")
        self.agent_user_id = str(agent_user_id or "")
        self.team_id = str(team_id or "")
        self.reviewer = reviewer
        self.handoff = handoff
        self.ttl_sec = ttl_sec
        self.archiver = archiver

    # ── ключи ────────────────────────────────────────────────────────────
    #
    # Сообщение адресуется парой «чат + id сообщения», а НЕ задачей: задачу
    # могут переназначить, тогда в чатах висит несколько сообщений про неё, и
    # ключ по задаче указывал бы на последнее — правка старого сообщения
    # меняла бы новое.

    @staticmethod
    def msg_key(chat_id, message_id) -> str:
        return f"feed:msg:{chat_id}:{message_id}"

    @staticmethod
    def index_key(issue_id) -> str:
        """Обратный индекс: задача → её сообщения. Без него смена статуса
        добавляла бы в чат ещё одно сообщение вместо правки висящего."""
        return f"feed:issue:{issue_id}"

    @staticmethod
    def status_key(issue_id) -> str:
        """Последний статус, с которым сообщение отрисовано.

        Нужен, когда сообщение правит не смена статуса, а исход проверки
        выката: статуса в том сигнале нет, а терять строку статуса при
        дописывании отметки нельзя.
        """
        return f"feed:status:{issue_id}"

    @staticmethod
    def mark_key(issue_id) -> str:
        """Отметка об исходе проверки выката. Живёт отдельно от шапки и от
        статуса: дописывается позже них и обязана пережить смены статуса."""
        return f"feed:mark:{issue_id}"

    # ── текст ────────────────────────────────────────────────────────────

    def render(self, base: str, status: str | None, mark: str | None) -> str:
        """Текст = неизменная шапка + статус + отметка о проверке.

        В памяти лежит только шапка: статус и отметка дописываются при каждой
        отрисовке. Иначе при повторной правке строки копились бы одна под
        другой.
        """
        m = self.transport.markup
        text = base if not status else f"{base}\n_{m.safe(status)}_"
        return text if not mark else f"{text}\n{m.safe(mark)}"

    def assignment_text(self, payload: dict) -> str | None:
        """Шапка сообщения о назначении, либо None — писать некому.

        None значит «человека нет в карте»: сообщение до него всё равно не
        доедет, и слать его в общий чат значит звать всю команду к чужой
        задаче.
        """
        who = assignee_id(payload)
        if not who:
            return None
        chat = self.people.chat_of(who)
        if not chat:
            return None
        m = self.transport.markup
        data = payload.get("data") or {}
        ident = data.get("identifier") or "?"
        name = m.safe((data.get("assignee") or {}).get("name") or "исполнитель")
        title = m.safe(data.get("title") or "")
        url = data.get("url") or ""
        # Текстом ссылки идёт идентификатор, а не заголовок: в нём нет
        # активных символов по определению, и разметка не сломается.
        tail = "\n" + m.link(ident, url) if url else ""
        return f"📋 {m.bold(ident)} → {m.mention(chat, name)}\n{title}{tail}"

    # ── память о сообщениях ──────────────────────────────────────────────

    async def remember(self, issue_id: str, chat_id, message_id: str,
                       text: str, buttons: list[Button]) -> None:
        """Запомнить отправленное сообщение задачи.

        Кнопки храним вместе с текстом: правка статуса обязана вернуть их на
        место, иначе «Мерж» под сдачей исчезает от первой же смены статуса, а
        она приезжает отдельным событием через секунду.
        """
        if not (issue_id and message_id):
            return
        record = json.dumps(
            {"text": text, "buttons": [[b.text, b.payload] for b in (buttons or [])]},
            ensure_ascii=False)
        try:
            await self.store.put(self.msg_key(chat_id, message_id), record,
                                 self.ttl_sec)
            await self._index(issue_id, f"{chat_id}:{message_id}")
        except Exception as e:
            log.warning("лента: сообщение %s не запомнено (%s) — следующая "
                        "правка придёт новым сообщением", issue_id, e)

    async def _index(self, issue_id: str, ref: str) -> None:
        """Положить ссылку на сообщение в карту задачи РОВНО ОДИН РАЗ.

        Перерисовка зовёт `remember` заново на каждое сообщение, которое
        поправила, — и до 17.09.2026 каждый такой вызов дописывал в карту ту
        же пару «чат:сообщение». У DEV-108 накопилось 4751 запись при двух
        настоящих сообщениях; дальше перерисовка честно шла править их все, и
        телеграм закрывал дверь боту на минуты.

        Уже накопленные дубли не просто пропускаются, а ВЫЧИЩАЮТСЯ: карта
        живёт тридцать дней, и починка одного лишь будущего оставила бы
        живые задачи ломать бота ещё месяц. Переписываем целиком и только
        когда есть что менять — лишняя запись в хранилище тут не бесплатна.
        """
        key = self.index_key(issue_id)
        stored = [str(r) for r in (await self.store.read_all(key) or [])]
        seen: set[str] = set()
        unique: list[str] = []
        for r in stored + [ref]:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        if unique != stored:
            # drop + append, а не «допишем недостающее»: дописать поверх
            # дублей нечем, а порядок должен остаться прежним.
            await self.store.drop(key)
            for r in unique:
                await self.store.append(key, r)
        await self.store.expire(key, self.ttl_sec)

    async def _refs(self, issue_id: str) -> list[str]:
        """Сообщения задачи: без повторов и не больше потолка.

        Дедуп стоит и на чтении, а не только на записи (`_index`): карту
        могла раздуть прежняя версия кода, и ждать, пока её перепишет
        следующая отправка, значит оставить ленту ломать бота до тех пор.
        """
        try:
            stored = [str(r) for r in
                      (await self.store.read_all(self.index_key(issue_id)) or [])]
        except Exception as e:
            log.warning("лента: индекс %s не прочитан: %s", issue_id, e)
            return []
        seen: set[str] = set()
        refs: list[str] = []
        for r in stored:
            if r not in seen:
                seen.add(r)
                refs.append(r)
        if len(refs) > MAX_REFS:
            log.warning("лента: у %s %d сообщений в карте — правлю последние "
                        "%d, остальное не трогаю", issue_id, len(refs), MAX_REFS)
            refs = refs[-MAX_REFS:]
        return refs

    def _asked_to_wait(self) -> float:
        """Сколько мессенджер просит подождать. 0 — не просит.

        Метод транспорта необязательный (см. `Transport.retry_after`): нет
        его — считаем, что ждать не просили.
        """
        ask = getattr(self.transport, "retry_after", None)
        if not callable(ask):
            return 0.0
        try:
            return max(0.0, float(ask() or 0))
        except Exception:
            return 0.0

    async def _record(self, chat_id, message_id: str) -> dict | None:
        try:
            raw = await self.store.get(self.msg_key(chat_id, message_id))
        except Exception:
            return None
        if not raw:
            return None
        try:
            rec = json.loads(raw)
        except Exception:
            return None
        return rec if isinstance(rec, dict) else None

    @staticmethod
    def _buttons_of(record: dict) -> list[Button]:
        return [Button(t, p) for t, p in (record.get("buttons") or [])
                if isinstance(t, str) and isinstance(p, str)]

    # ── правка ───────────────────────────────────────────────────────────

    async def repaint(self, issue_id: str, status: str | None = None,
                      mark: str | None = None, buttons=KEEP) -> int:
        """Перерисовать все сообщения задачи. Возвращает их число.

        Два источника правки — смена статуса и исход проверки выката, — и ни
        один не должен затирать чужой кусок. Поэтому оба лежат рядом с шапкой,
        а не только в приходящем сигнале.
        """
        if not issue_id:
            return 0
        wait = self._asked_to_wait()
        if wait:
            # Перерисовка правит только висящее: пропустить её — значит
            # показать человеку вчерашний статус, а не потерять сообщение.
            # Стучаться в закрытую дверь дороже: каждая попытка продлевает
            # паузу, и вместе с правками не проходят ответы на кнопки.
            log.warning("лента: телеграм просит подождать %.0f с — "
                        "перерисовку %s пропускаю", wait, issue_id)
            return 0
        status = await self._remember_part(self.status_key(issue_id), status)
        mark = await self._remember_part(self.mark_key(issue_id), mark)
        edited = 0
        refs = await self._refs(issue_id)
        for ref in refs:
            chat_id, _, message_id = ref.rpartition(":")
            if not (chat_id and message_id):
                continue
            rec = await self._record(chat_id, message_id)
            if not rec:
                continue          # сообщение старше памяти — правим что помним
            keep = self._buttons_of(rec) if buttons is KEEP else (buttons or [])
            ok = await self.transport.edit(
                chat_id, message_id,
                self.render(rec.get("text") or "", status, mark), keep, rich=True)
            if not ok:
                if self._stop_here(issue_id, edited, len(refs)):
                    break
                continue
            if buttons is not KEEP:
                await self.remember(issue_id, chat_id, message_id,
                                    rec.get("text") or "", keep)
            edited += 1
        return edited

    def _stop_here(self, issue_id: str, done: int, total: int) -> bool:
        """Правка не прошла — это «слишком часто»? Тогда перерисовка кончена.

        Отличать надо: одно удалённое сообщение — повод пропустить его и
        править дальше, а закрытая дверь — повод уйти. До 17.09.2026 разницы
        не было, и лента доедала отказами весь остаток карты.
        """
        wait = self._asked_to_wait()
        if not wait:
            return False
        log.warning("лента: телеграм просит подождать %.0f с — перерисовка %s "
                    "остановлена на %d из %d", wait, issue_id, done, total)
        return True

    async def _remember_part(self, key: str, value: str | None) -> str | None:
        """Кусок текста: пришёл — запомнить, не пришёл — достать прежний."""
        try:
            if value is not None:
                await self.store.put(key, value, self.ttl_sec)
                return value
            return await self.store.get(key)
        except Exception as e:
            log.warning("лента: %s не прочитан: %s", key, e)
            return value

    async def edit_existing(self, issue_id: str, text: str, status: str | None,
                            buttons: list[Button], only_chat: str = "") -> int:
        """Переписать висящее сообщение задачи под новое назначение.

        `only_chat` ограничивает правку одним чатом. Без него назначение
        правило бы ЛЮБОЕ сообщение задачи — в том числе оставшееся в общем
        чате от прежнего назначения, — и в личку оно не ушло бы вовсе, а
        кнопки оказались бы у всей команды.

        Ноль означает «править нечего, отправляй новое»: и задача в чате
        впервые, и сообщение старше памяти, и его удалили руками, и оно есть,
        но в другом чате. Во всех случаях промолчать нельзя — человек не
        узнает о назначении вовсе.
        """
        if not issue_id:
            return 0
        status = await self._remember_part(self.status_key(issue_id), status)
        mark = await self._remember_part(self.mark_key(issue_id), None)
        edited = 0
        refs = await self._refs(issue_id)
        for ref in refs:
            chat_id, _, message_id = ref.rpartition(":")
            if not (chat_id and message_id):
                continue
            if only_chat and str(chat_id) != str(only_chat):
                continue
            if not await self._record(chat_id, message_id):
                continue
            ok = await self.transport.edit(
                chat_id, message_id, self.render(text, status, mark),
                # Кнопки заменяем всегда: назначение без кнопок обязано снять
                # оставшиеся от прошлой сдачи, иначе клик уведёт подписывать
                # работу по задаче, которая уже уехала дальше.
                buttons or [], rich=True)
            if not ok:
                if self._stop_here(issue_id, edited, len(refs)):
                    break
                continue
            # Шапку перезаписываем: в ней имя исполнителя, а оно и меняется.
            await self.remember(issue_id, chat_id, message_id, text, buttons or [])
            edited += 1
        return edited

    # ── возврат закрытой задачи ──────────────────────────────────────────

    async def recipients(self) -> set[str]:
        """Кому «принадлежит» работа агента: владелец и действующий делегат.

        По этому признаку «закрыли работу агента» отличается от «двое людей
        ведут задачу между собой».
        """
        ids = {self.people.owner_user_id}
        if self.reviewer:
            try:
                ids.add(str(await self.reviewer.user_id() or ""))
            except Exception as e:
                log.warning("лента: не спросить принимающего: %s", e)
        return {i for i in ids if i}

    async def return_closed_to_creator(self, payload: dict) -> str:
        """Задача закрылась на приёмнике работы → вернуть её автору. Возвращает
        id автора, если переназначили, иначе пусто.

        Зациклиться некому: после переназначения исполнитель уже не приёмник,
        и на следующем событии условие не выполнится.
        """
        if not closes_issue(payload, self.agent_user_id):
            return ""
        data = payload.get("data") or {}
        issue_id = data.get("id") or ""
        assignee = str(data.get("assigneeId")
                       or (data.get("assignee") or {}).get("id") or "")
        if not assignee or assignee not in await self.recipients():
            return ""

        author = await self.people.author_of(issue_id) or creator_id(data)
        if not author:
            author = await self._ask_creator(issue_id)
        if not author or author == assignee:
            return ""
        # Автор — сам владелец: он и так принимает работу, возвращать нечего.
        if author == self.people.owner_user_id:
            return ""
        # Автор — агент: назначение на него ставит новую сессию, то есть
        # закрытая задача пошла бы на второй круг.
        if self.agent_user_id and author == self.agent_user_id:
            return ""
        # Автора нет в карте — сообщение до него не доедет, а задача уедет к
        # тому, кто её не увидит.
        if not self.people.chat_of(author):
            return ""

        # Дедуп по метке события: трекер ретраит доставку, а переназначение
        # на каждый ретрай ходило бы в API.
        mark = f"feed:returned:{issue_id}:{event_stamp(payload)}"
        claimed = False
        try:
            claimed = await self.store.claim(mark, 3600)
            if not claimed:
                return ""
        except Exception as e:
            log.warning("лента: дедуп возврата пропущен (%s)", e)

        try:
            await self._assign(issue_id, author)
        except Exception as e:
            # Возврат — удобство, а не обязательство: не вышло — задача
            # осталась на приёмнике. Отметку снимаем: ретрай доставки —
            # единственный шанс попробовать ещё раз.
            if claimed:
                try:
                    await self.store.drop(mark)
                except Exception:
                    log.warning("лента: отметка возврата %s не снята", mark)
            log.warning("лента: не вернуть закрытую %s автору: %s",
                        data.get("identifier") or issue_id, e)
            return ""
        log.info("лента: закрытая %s возвращена автору %s",
                 data.get("identifier") or issue_id, author)
        return author

    async def _ask_creator(self, issue_id: str) -> str:
        ask = getattr(self.tracker, "creator_of", None)
        if not ask:
            return ""
        try:
            import asyncio
            return str(await asyncio.to_thread(ask, issue_id) or "")
        except Exception as e:
            log.warning("лента: не спросить автора задачи %s: %s", issue_id, e)
            return ""

    async def _assign(self, issue_id: str, user_id: str) -> None:
        import asyncio
        await asyncio.to_thread(self.tracker.assign, issue_id, user_id)

    # ── главная ручка ────────────────────────────────────────────────────

    async def on_event(self, payload: dict) -> dict:
        """Событие трекера → что видно в чате. Возвращает исход для журнала.

        Уборка закрытой задачи стоит ПОСЛЕ всего остального и намеренно: она
        ходит в трекер, а до неё в этом же событии человеку уходит сообщение.
        Поменяй порядок — и сдача, приехавшая одним событием с закрытием,
        ждала бы в чате чужого HTTP-запроса.
        """
        out = await self.show(payload)
        if self.archiver and closes_issue(payload, self.agent_user_id):
            if await self.archiver.on_closed(payload.get("data") or {}):
                out["archived"] = True
        return out

    async def show(self, payload: dict) -> dict:
        """Всё, что видно человеку: сообщение, статус, кнопки, возврат автору."""
        data = payload.get("data") or {}
        issue_id = data.get("id") or ""
        out: dict = {}

        # Возврат автору стоит первым и не зависит ни от чата, ни от команды:
        # это чисто трекерное действие, в мессенджер оно не пишет.
        returned = await self.return_closed_to_creator(payload)
        if returned:
            out["returned"] = returned

        team = str(data.get("teamId") or "")
        if self.team_id and team and team != self.team_id:
            out["skipped"] = "чужая команда"
            return out

        status = status_change(payload)
        if status:
            # Задачу закрыли — кнопки под ней снимаем. Обычно их уже нет, но
            # статус меняется не всегда тем же событием, что исполнитель:
            # разбей агент сдачу на два запроса — и кнопки успевают появиться,
            # а снять их было бы уже некому.
            out["repainted"] = await self.repaint(
                issue_id, status=status,
                buttons=[] if issue_is_closed(data) else KEEP)

        text = self.assignment_text(payload)
        if text is None:
            return out

        # Дальше — назначение, и выходить рано нельзя: агент в конце сессии
        # меняет исполнителя и статус одним запросом, трекер шлёт это одним
        # событием. Счесть такое «только сменой статуса» значит не сказать
        # человеку, что задача теперь на нём.
        if not await self._first_delivery(payload, issue_id, data):
            out["deduped"] = True
            return out

        buttons: list[Button] = []
        if (self.handoff and is_agent_handoff(payload, self.agent_user_id)
                and not issue_is_closed(data)):
            try:
                text, buttons = await self.handoff.on_handoff(payload, text)
            except Exception as e:
                # Сдача без кнопок доедет; сдача, потерянная из-за осечки
                # подписи, — нет.
                log.error("лента: сдача %s не разобрана (%s) — шлю без кнопок",
                          data.get("identifier") or issue_id, e)
                buttons = []

        target = self.people.chat_of(assignee_id(payload)) or self.notify_chat
        if not target:
            out["sent"] = False
            return out

        # Сдача правит уже висящее сообщение, а не добавляет ещё одно: ревью
        # многостадийное — сдал, вернули, сдал снова, — и каждая стадия
        # постила бы неотличимый дубль.
        edited = await self.edit_existing(issue_id, text, status, buttons,
                                          only_chat=target)
        if edited:
            out["edited"] = edited
            return out
        rendered = self.render(
            text, status, await self._remember_part(self.mark_key(issue_id), None))
        mid = await self.transport.send(target, rendered, buttons, rich=True)
        if not mid and self.notify_chat and target != self.notify_chat:
            # Личка не приняла: человек не начинал разговор с ботом или
            # заблокировал его. Доставка важнее адресности — уходим в общий
            # чат, иначе о задаче не узнает никто.
            log.warning("лента: личка %s не приняла — шлю в общий чат", target)
            target = self.notify_chat
            mid = await self.transport.send(target, rendered, buttons, rich=True)
        if mid:
            await self.remember(issue_id, target, mid, text, buttons)
        out["sent"] = bool(mid)
        return out

    async def _first_delivery(self, payload: dict, issue_id: str,
                              data: dict) -> bool:
        """Трекер ретраит доставку — одно назначение показываем один раз."""
        key = (f"feed:assign:{issue_id}:{data.get('assigneeId')}:"
               f"{event_stamp(payload)}")
        try:
            return bool(await self.store.claim(key, 3600))
        except Exception as e:
            # Лежащий стор не повод молчать о назначении: лучше показать
            # дважды, чем не показать вовсе.
            log.warning("лента: дедуп назначения пропущен (%s)", e)
            return True
