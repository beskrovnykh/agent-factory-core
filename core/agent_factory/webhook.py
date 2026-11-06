"""Событие трекера приходит само: приёмник вебхука.

Назначил задачу агенту — сессия стартует через секунду, а не на следующем
круге опроса. Часть ПРОДУКТА: разбор события, проверка подписи, дедуп и
постановка в очередь написаны один раз; инсталляция задаёт адрес и секрет.

Два слоя, и это не украшение:

  • `Receiver` — что делать с телом события. Ни сокета, ни фреймворка.
    Его берёт инсталляция, у которой веб-сервер уже есть (у первого клиента
    это FastAPI): её эндпоинт отдаёт сюда тело и заголовок с подписью.
  • `Server` — маленький HTTP-вход для инсталляции, у которой веб-сервера
    нет. Слушает локальный порт, наружу его выставляет обратный прокси.

Опрос назначений (`assignments.py`) остаётся, но как запасной путь: для
площадки, до которой снаружи действительно не достучаться, и как страховка
от недоехавшей доставки. Основным ему быть незачем — это лишний запрос в
трекер каждую минуту и задержка до круга.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from .deploy import remember_task, same_digest
from .queueing import hand_over, work_task

log = logging.getLogger("intake")

# Заголовок с подписью тела. Имя трекерозависимое, поэтому — параметр.
SIGNATURE_HEADER = "linear-signature"

# Повтор ТОЙ ЖЕ доставки: у ретрая отметка события та же.
SEEN_TTL_SEC = 3600

# Окно, в котором вторая доставка того же действия уже не заводит сессию.
# Доставки одного действия приходят за секунды; осознанное переназначение
# (после упавшей сессии) случается через десятки минут, и его блокировать
# нельзя. 11.09 без этого окна по TASK-285 и TASK-288 стартовало по две сессии.
START_WINDOW_SEC = 180

# Больше этого тело не читаем: событие трекера — килобайты, а открытый порт
# не должен давать способ занять память процесса приёма.
MAX_BODY_BYTES = 1 << 20

READ_TIMEOUT_SEC = 20


# ── разбор события ───────────────────────────────────────────────────────

def verify(secret: str, body: bytes, signature: str) -> bool:
    """Подпись тела события трекера."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return same_digest(signature.strip(), expected)


def assignee_id(payload: dict) -> str | None:
    """id исполнителя, если это СОБЫТИЕ НАЗНАЧЕНИЯ, иначе None.

    Назначение — это create с исполнителем либо update, где исполнитель
    реально сменился: трекер кладёт прежнее значение в `updatedFrom`. Без
    этой проверки любая правка задачи, на которой висит агент (комментарий,
    смена статуса, правка описания), заводила бы новую сессию.
    """
    if payload.get("type") != "Issue":
        return None
    action = payload.get("action")
    if action not in ("create", "update"):
        return None
    data = payload.get("data") or {}
    who = data.get("assigneeId") or (data.get("assignee") or {}).get("id")
    if not who:
        return None                      # сняли исполнителя / его и не было
    if action == "update" and "assigneeId" not in (payload.get("updatedFrom") or {}):
        return None                      # апдейт не про исполнителя
    return str(who)


def payload_label_names(payload: dict) -> list[str] | None:
    """Имена лейблов из тела события, либо None — «событие их не несёт».

    Пустой список и None — разные ответы, и путать их нельзя. Пустой значит
    «лейблов на задаче нет» (так и есть у обычной работы), None — «трекер не
    положил их в это событие», и тогда лейблы надо дочитать через API: иначе
    инцидент молча уедет обычной задачей, а видно это станет только по
    промпту сессии.

    Трекер кладёт лейблы в `data.labels` (объекты с именем), а в некоторых
    событиях — только `data.labelIds`. По id имя не восстановить, поэтому
    один случай решается сразу: пустой `labelIds` — честное «пусто».
    """
    data = payload.get("data") or {}
    labels = data.get("labels")
    if isinstance(labels, list):
        if not labels:
            return []
        names = [str((it or {}).get("name") or "").strip() for it in labels
                 if isinstance(it, dict)]
        if len(names) == len(labels) and all(names):
            return names
        return None                      # лейблы есть, а имён в событии нет
    ids = data.get("labelIds")
    if isinstance(ids, list) and not ids:
        return []
    return None


def status_change(payload: dict) -> str | None:
    """Имя нового статуса, если событие — смена статуса, иначе None.

    Живёт здесь, а не в ленте, хотя лента её и придумала: разбор события
    трекера весь собран в этом модуле, и с появлением второго потребителя
    (второй вход проверки, DEV-17) обратный импорт стал бы циклом. Лента
    по-прежнему берёт её у себя по имени — ей ничего не надо менять.
    """
    if payload.get("type") != "Issue" or payload.get("action") != "update":
        return None
    if "stateId" not in (payload.get("updatedFrom") or {}):
        return None
    return ((payload.get("data") or {}).get("state") or {}).get("name") or None


def event_stamp(payload: dict) -> str:
    """Отметка события — вторая половина ключа дедупа. У ретрая она та же."""
    data = payload.get("data") or {}
    return str(payload.get("webhookTimestamp") or data.get("updatedAt") or "")


def actor_of(payload: dict) -> tuple[str, str]:
    """Кто назначил: имя и id. Постановщик нужен для отказа «делать нечего» —
    такая задача возвращается ему, а не тому, кто принимает работу. В самой
    задаче его не видно: трекер хранит только автора."""
    actor = payload.get("actor") or {}
    return str(actor.get("name") or ""), str(actor.get("id") or "")


# ── приёмник ─────────────────────────────────────────────────────────────

class Receiver:
    """Тело события → задача в очереди. Без сокета: его даёт вызывающий."""

    def __init__(self, store, queue_key: str, agent_user_id: str,
                 secret: str = "", tracker=None, incident_label: str = "",
                 start_window_sec: int = START_WINDOW_SEC, on_queued=None,
                 feed=None, verification=None, verify_state: str = "",
                 team_id: str = "", rounds=None, reviewer_id=None):
        """`on_queued` — что инсталляция делает с уже поставленной задачей
        сверх продукта. Отказ здесь не отменяет постановку: задача уже в
        очереди, и назвать это неудачей значило бы соврать про исход.
        Заначка для проверки выката сюда больше не относится — её кладёт
        продукт сам (`deploy.remember_task`), на всех путях постановки.

        `feed` — лента задачи в чате (`issue_feed.IssueFeed`). Одно и то же
        событие трекера нужно двум потребителям: очереди и человеку. Держать
        для этого два вебхука значит дважды настраивать адрес и секрет.

        `verification` и `verify_state` — второй вход проверки: перевод задачи
        в это состояние поднимает проверяльщика так же, как это делает вебхук
        выката. Нужен там, где выката нет вовсе (мобильный клиент, библиотека):
        до него такие задачи не проверялись никогда. Не задано хоть одно —
        входа просто нет, всё остальное работает как раньше.

        `team_id` — чьи задачи проверяем. Старый вход гейтом не нуждался: он
        требует назначения на конкретного пользователя-агента, а это осознанное
        действие. У второго гейт — одно лишь ИМЯ состояния, и одинаковые имена
        на соседних досках дело обычное: на маркетинговой доске тоже заводят
        «Verifying». Без этой границы перевод маркетинговой задачи поднимал бы
        сессию, которая пойдёт искать репозиторий и займёт слот.

        `rounds(identifier) -> int` — сколько рабочих заходов у задачи выбрано
        сверх потолка (0 — можно ставить; см. `runs.rounds_exhausted`).
        Назначение, сделанное самим принимающим (`reviewer_id`, строка или
        функция, может быть асинхронной), потолок не считает: человек решил
        дать ещё круг — его право. Всё остальное — возврат от проверки,
        ретрай, чужая рука — упирается в потолок и уходит принимающему."""
        self.store = store
        self.rounds = rounds
        self.reviewer_id = reviewer_id
        self.queue_key = queue_key
        self.agent_user_id = str(agent_user_id or "")
        self.secret = secret
        self.tracker = tracker
        self.incident_label = incident_label
        self.start_window_sec = start_window_sec
        self.on_queued = on_queued
        self.feed = feed
        self.verification = verification
        self.verify_state = str(verify_state or "").strip()
        self.team_id = str(team_id or "")

    async def handle(self, body: bytes, signature: str) -> tuple[int, dict]:
        """Код ответа и тело. Код смотрит трекер: на не-2xx он повторит
        доставку, и это ровно то, чего мы хотим, когда не смогли поставить."""
        if not self.secret:
            return 503, {"error": "секрет вебхука не задан"}
        if not verify(self.secret, body, signature):
            log.warning("вебхук: подпись не сошлась, тело %d байт", len(body))
            return 401, {"error": "подпись не сошлась"}
        try:
            payload = json.loads(body)
        except Exception:
            return 400, {"error": "тело не разбирается как json"}
        if not isinstance(payload, dict):
            return 400, {"error": "тело не объект"}
        return await self.dispatch(payload)

    async def dispatch(self, payload: dict) -> tuple[int, dict]:
        """Разобранное событие → исход. Отдельно от `handle`, чтобы
        инсталляция со своей проверкой подписи могла звать только это."""
        who = assignee_id(payload)
        if not who or not self.agent_user_id or who != self.agent_user_id:
            # Не назначение на агента — значит это новость для человека:
            # смена статуса, сдача, закрытие, назначение на коллегу.
            shown = await self.tell(payload)
            checked = await self.check(payload)
            if checked.get("verify") == "enqueue-failed":
                # Ровно как с постановкой: задача переведена в проверку, а
                # проверяльщика нет. Пусть трекер повторит — метку мы сняли.
                return 503, {"ok": False, **shown, **checked}
            return 200, {"ok": True, **shown, **checked}
        # Назначение на агента человеку не показываем: сессия сама отчитается
        # в задаче, а сообщение «назначено на агента» — шум о собственном
        # действии.
        #
        # А вот проверку смотрим и здесь. Трекер шлёт смену статуса и смену
        # исполнителя ОДНИМ событием (см. соседний комментарий про сдачу), и
        # перевод в состояние проверки с одновременным назначением на агента
        # иначе поднял бы рабочую сессию вместо проверяющей — без единого
        # следа о том, что проверку пропустили.
        checked = await self.check(payload)
        outcome = await self.enqueue(payload)
        if outcome == "enqueue-failed" or checked.get("verify") == "enqueue-failed":
            # Не 200: задача висит в трекере назначенной на агента, а сессии
            # нет. Пусть трекер повторит доставку — захваты мы сняли.
            return 503, {"ok": False, "agent": outcome, **checked}
        return 200, {"ok": True, "agent": outcome, **checked}

    async def tell(self, payload: dict) -> dict:
        """Показать событие человеку, если лента заведена.

        Осечка ленты не должна отражаться на коде ответа: трекер повторит
        доставку, а повтор нужен очереди, не чату — у него своя память о том,
        что уже показано.
        """
        if not self.feed:
            return {"ignored": "лента не заведена"}
        try:
            return await self.feed.on_event(payload)
        except Exception as e:
            log.exception("лента не отработала на событии: %s", e)
            return {"feed-failed": True}

    async def check(self, payload: dict) -> dict:
        """Перевод в состояние проверки → проверяльщик в очереди.

        Осечка проверки не должна съедать показ события человеку — поэтому
        исход возвращается словарём и подмешивается к ответу, а не подменяет
        его.
        """
        if not (self.verification and self.verify_state):
            return {}
        team = str((payload.get("data") or {}).get("teamId") or "")
        if self.team_id and team and team != self.team_id:
            return {}
        state = status_change(payload)
        # Сверяем без регистра и краёв: имя состояния приезжает из настройки
        # инсталляции, и лишний пробел в ней не должен тихо выключать проверку.
        if not state or state.strip().casefold() != self.verify_state.casefold():
            return {}
        try:
            return {"verify": await self.verification.on_state(payload)}
        except Exception as e:
            log.exception("проверка по статусу не отработала: %s", e)
            return {"verify": "enqueue-failed"}

    async def enqueue(self, payload: dict) -> str:
        issue = dict(payload.get("data") or {})
        issue_id = str(issue.get("id") or "")
        if not issue_id:
            log.warning("вебхук: назначение без id задачи — пропускаю")
            return "no-issue-id"
        stamp = event_stamp(payload)
        seen = f"agent:seen:{issue_id}:{stamp}"
        # Ключ тот же, что у опроса назначений: два пути постановки не должны
        # заводить вторую сессию друг за другом.
        started = f"agent:started:{issue_id}"
        try:
            if not await self.store.claim(seen, SEEN_TTL_SEC):
                return "deduped"
            if not await self.store.claim(started, self.start_window_sec):
                return "deduped"
        except Exception as e:
            log.error("вебхук (%s): дедуп не отработал (%s) — не ставлю", issue_id, e)
            return "enqueue-failed"
        name, actor_id = actor_of(payload)
        # Потолок кругов — ПОСЛЕ дедупа (ретраи не должны считаться заново) и
        # ДО постановки. Метку окна снимаем: следующее назначение — уже
        # решение человека, и оно должно пройти.
        spent = await self._rounds_spent(issue, actor_id)
        if spent:
            await hand_over(self.tracker, issue_id,
                            issue.get("identifier") or issue_id, spent,
                            await self._reviewer())
            try:
                await self.store.drop(started)
            except Exception:
                pass
            return "rounds-exhausted"
        try:
            # Лейблы дочитываем ПОСЛЕ дедупа: ретраи идут пачкой, и платить
            # за них лишним запросом в трекер незачем.
            issue["labels"] = await self.labels(payload, issue_id)
            task = work_task(issue, name or "назначено в трекере",
                             self.incident_label, requested_by=actor_id)
            await self.store.enqueue(self.queue_key,
                                     json.dumps(task, ensure_ascii=False))
        except Exception as e:
            log.error("вебхук (%s): постановка не удалась: %s", issue_id, e)
            # Захваты снимаем: иначе повтор доставки съест `seen`, а новая
            # доставка — окно по задаче, и задача не поедет вообще, хотя в
            # трекере она висит назначенной на агента.
            await self.release(seen, started, issue_id)
            return "enqueue-failed"
        # Заначка — после постановки: выкат найдёт задачу по имени ветки
        # через часы-дни, когда в событии хостинга нет ни id, ни постановщика.
        await remember_task(self.store, task)
        if self.on_queued:
            try:
                await self.on_queued(task)
            except Exception as e:
                log.warning("вебхук (%s): послесловие инсталляции не отработало: %s",
                            issue_id, e)
        log.info("вебхук: назначение %s → в очередь",
                 task.get("identifier") or issue_id)
        return "queued"

    async def _reviewer(self) -> str:
        who = self.reviewer_id
        if callable(who):
            who = who()
            if asyncio.iscoroutine(who) or asyncio.isfuture(who):
                who = await who
        return str(who or "")

    async def _rounds_spent(self, issue: dict, actor_id: str) -> int:
        """Сколько заходов выбрано сверх потолка, если потолок есть и
        назначил не принимающий. Осечка счёта — не отказ: задача ставится,
        как без потолка, а в журнале видно почему."""
        if not self.rounds:
            return 0
        reviewer = await self._reviewer()
        if reviewer and actor_id and actor_id == reviewer:
            return 0
        try:
            return int(await asyncio.to_thread(self.rounds, issue.get("identifier") or "") or 0)
        except Exception as e:
            log.warning("вебхук (%s): круги не сосчитаны (%s) — ставлю без потолка",
                        issue.get("identifier") or "?", e)
            return 0

    async def labels(self, payload: dict, issue_id: str) -> list[str]:
        """Лейблы задачи для развилки «дежурный или работа»: из события, а
        если их там нет — запросом в трекер.

        Неудача не проваливает постановку: очередь важнее развилки, без неё
        сессия хотя бы состоится. Но и молчать нельзя — задача с лейблом
        уедет обычной работой, и по её промпту этого не увидеть.
        """
        names = payload_label_names(payload)
        if names is not None:
            return names
        ask = getattr(self.tracker, "issue_label_names", None)
        if not ask:
            log.error("вебхук (%s): лейблов в событии нет, а спросить трекер "
                      "нечем — задача уедет обычной работой", issue_id)
            return []
        try:
            return await asyncio.to_thread(ask, issue_id)
        except Exception as e:
            log.error("вебхук (%s): лейблы не прочитаны (%s) — задача уедет "
                      "обычной работой, даже если на ней стоит «%s»",
                      issue_id, e, self.incident_label)
            return []

    async def release(self, seen: str, started: str, issue_id: str) -> None:
        try:
            await self.store.drop(seen)
            await self.store.drop(started)
        except Exception:
            log.warning("вебхук (%s): захваты не сняты — следующая доставка "
                        "будет отброшена как дубль", issue_id)


# ── HTTP-вход ────────────────────────────────────────────────────────────

REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
           404: "Not Found", 411: "Length Required", 413: "Payload Too Large",
           500: "Internal Server Error", 503: "Service Unavailable"}


class Server:
    """Локальный HTTP-вход для инсталляции без своего веб-сервера.

    Слушает петлю, наружу его выставляет обратный прокси — он же держит TLS.
    Своего HTTP-фреймворка продукт не тянет: одна ручка, одно тело, один
    заголовок, и зависимость ради этого была бы дороже кода.
    """

    def __init__(self, receiver: Receiver | None = None, bind: str = "127.0.0.1",
                 port: int = 8787, path: str = "/linear",
                 signature_header: str = SIGNATURE_HEADER,
                 max_body: int = MAX_BODY_BYTES,
                 routes: dict | None = None):
        """Один порт, несколько путей. `routes` — `{путь: (приёмник, заголовок
        подписи)}`.

        Почему не второй сервер на второй порт: наружу каждый порт надо
        выставить прокси и открыть в брандмауэре, а это разговор с админом
        инсталляции на каждое новое событие. Событий будет больше одного —
        трекер, хостинг репозиториев, что-то ещё, — и все они про одно и то
        же: «пришло тело, проверь подпись, разберись».

        Приёмник обязан иметь `handle(body: bytes, signature: str)`.
        """
        self.bind = bind
        self.port = port
        self.path = path if path.startswith("/") else "/" + path
        self.signature_header = signature_header.lower()
        self.max_body = max_body
        # Приёмник трекера остаётся названным: он главный, и инсталляции
        # удобно дотянуться до него, не зная про устройство маршрутов.
        self.receiver = receiver
        self.routes: dict[str, tuple] = {}
        if receiver is not None:
            self.routes[self.path] = (receiver, self.signature_header)
        for p, (h, hdr) in (routes or {}).items():
            at = p if p.startswith("/") else "/" + p
            if at in self.routes:
                # Пути задаются независимыми ручками (`TRACKER_WEBHOOK_PATH`,
                # `DEPLOY_WEBHOOK_PATH`), и совпадение молча затирало бы
                # приёмник трекера деплойным: порт жив, /health отвечает 200, а
                # все события трекера получают 401 по чужому секрету. Это
                # ошибка настройки — падать на старте честнее, чем работать
                # наполовину.
                raise ValueError(f"два приёмника на один путь {at} — "
                                 "разведи пути в настройках")
            self.routes[at] = (h, hdr.lower())
        # Страницы: GET-обработчики на том же порту. Отдельно от приёмников
        # событий, потому что отвечают не JSON'ом и подпись у них другая —
        # человек с телефона, а не хостинг с HMAC.
        self.pages: dict[str, object] = {}
        # Занятый порт: None — ещё не поднимались.
        self.srv = None

    async def start(self):
        """Занять порт — СЕЙЧАС, не в отложенной таске.

        Отдельно от `run`, чтобы инсталляция узнала про занятый порт до того,
        как объявит вебхук рабочим путём. В таске эта ошибка была не видна:
        процесс жил, в журнале стоял «вебхук слушает», а постановка задачи не
        работала ни вебхуком, ни кнопкой — кнопка к тому моменту уже отключена
        как раз из-за «вебхук есть».
        """
        self.srv = await asyncio.start_server(self.serve, self.bind, self.port)
        self.port = (self.srv.sockets[0].getsockname()[1]
                     if self.srv.sockets else self.port)
        log.info("вебхук слушает %s:%d — пути: %s", self.bind, self.port,
                 ", ".join(sorted(self.routes)) or "нет")
        return self.srv

    async def run(self) -> None:
        srv = self.srv or await self.start()
        async with srv:
            await srv.serve_forever()

    async def serve(self, reader, writer) -> None:
        try:
            code, body = await self.answer(reader)
        except Exception:
            # Одно кривое соединение не должно ронять приём.
            log.exception("вебхук: соединение не обработалось")
            code, body = 500, {"error": "внутренняя ошибка"}
        try:
            await self.reply(writer, code, body)
        except Exception:
            pass
        writer.close()

    async def answer(self, reader) -> tuple[int, dict]:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),
                                          timeout=READ_TIMEOUT_SEC)
        except Exception:
            return 400, {"error": "заголовки не прочитаны"}
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return 400, {"error": "битая строка запроса"}
        method, target = parts[0].upper(), parts[1]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if k.strip():
                headers[k.strip().lower()] = v.strip()

        path = target.partition("?")[0]
        # Живость проверяют и человек, и мониторинг — до всякой подписи.
        # Только у ПОДНЯТЫХ путей: иначе `GET /deploy/health` отвечал 200 и
        # там, где путь `/deploy` не поднят вовсе (секрет задан, разбирать
        # событие нечем), — то есть проверка живости не умела заметить ровно
        # ту поломку, под которую её и добавили.
        if method == "GET" and (path == "/health" or (
                path.endswith("/health") and path[:-len("/health")] in self.routes)):
            return 200, {"ok": True}
        page = self.pages.get(path)
        if page is not None and method in ("GET", "POST"):
            body = b""
            if method == "POST":
                try:
                    length = int(headers.get("content-length") or 0)
                except ValueError:
                    length = 0
                if 0 < length <= self.max_body:
                    body = await asyncio.wait_for(reader.readexactly(length),
                                                  timeout=READ_TIMEOUT_SEC)
            return await page.handle(method, target, headers, body)
        route = self.routes.get(path)
        if route is None:
            return 404, {"error": "не тот адрес"}
        if method != "POST":
            return 404, {"error": "только POST"}

        if "chunked" in headers.get("transfer-encoding", "").lower():
            # Кусками тело не приходит: трекер шлёт content-length, а прокси
            # его сохраняет. Отказ честнее, чем разобрать неправильно: по
            # неверно прочитанному телу не сойдётся подпись, и это выглядело
            # бы как чужой запрос.
            return 411, {"error": "нужен content-length"}
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            return 400, {"error": "битый content-length"}
        if length < 0:
            return 400, {"error": "битый content-length"}
        if length > self.max_body:
            return 413, {"error": "тело слишком велико"}
        body = b""
        if length:
            try:
                body = await asyncio.wait_for(reader.readexactly(length),
                                              timeout=READ_TIMEOUT_SEC)
            except Exception:
                return 400, {"error": "тело не прочитано целиком"}
        handler, sig_header = route
        return await handler.handle(body, headers.get(sig_header, ""))

    async def reply(self, writer, code: int, body) -> None:
        # Страница отдаёт `(тип, байты)`, приёмник события — словарь. Второй
        # тип ответа заводится здесь, а не отдельным сервером: порт один, и
        # выставлять наружу второй — снова разговор с админом инсталляции.
        extra = {}
        if isinstance(body, tuple) and len(body) in (2, 3):
            # Страница отдаёт (тип, тело) или (тип, тело, заголовки): третье
            # нужно ровно для куки после проверки подписи Telegram.
            ctype, data = body[0], body[1]
            if len(body) == 3:
                extra = body[2] or {}
            data = data if isinstance(data, bytes) else str(data).encode()
        else:
            ctype = "application/json; charset=utf-8"
            data = json.dumps(body, ensure_ascii=False).encode()
        more = "".join(f"{k}: {v}\r\n" for k, v in extra.items())
        head = (f"HTTP/1.1 {code} {REASONS.get(code, 'OK')}\r\n"
                f"Content-Type: {ctype}\r\n"
                f"Content-Length: {len(data)}\r\n"
                f"{more}"
                "Connection: close\r\n\r\n").encode("latin-1")
        writer.write(head + data)
        await writer.drain()
