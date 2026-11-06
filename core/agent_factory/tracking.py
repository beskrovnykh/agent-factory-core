"""Адаптер трекера: что приёму нужно от трекера и реализация поверх Linear.

Точка роста продукта: каждый новый метод (`archive_issue`, `closed_issues`,
`unassigned_issues`, `issues_awaiting`) приезжает сюда. Поэтому здесь и
только здесь — ни правил постановки в очередь, ни кнопок, ни разбора
результатов: соседство с ними означало, что две несвязанные задачи правят
один файл и дерутся за него (DEV-66 против DEV-67, 12.09.2026).

Протокол отдельно от реализации, потому что трекер — за инсталляцией:
первый клиент подставляет свой адаптер, и `LinearTracker` для него просто
одна из возможных реализаций, а не обязательная.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

# Разбор формы лейблов в ответе трекера живёт с правилами постановки: его
# читает `work_task`, и он же нужен здесь. Импорт в эту сторону, а не
# обратно, — `queueing.py` не должен знать про адаптер.
from .queueing import label_names

log = logging.getLogger("intake")


class Tracker(Protocol):
    """То, что приёму нужно от трекера. Всё синхронное: вызывается из потока."""

    def whoami(self, key: str = "") -> dict: ...

    def issue_by_identifier(self, identifier: str) -> dict | None: ...

    def create_issue(self, team_id: str, title: str, description: str,
                     role: str = "", project_id: str = "") -> dict: ...

    def comment(self, issue_id: str, body: str) -> None:
        """Комментарий в задачу от имени приёмщика.

        Нужен разбору постановки: что именно приём решил про роль, приоритет
        и репозиторий, видно только по этому комментарию — в самой задаче
        решение растворено (приоритет просто стоит, роль видна лейблом).
        """
        ...

    def upsert_comment(self, key: str, marker: str, body: str) -> str:
        """Комментарий-карточку — завести или ПОПРАВИТЬ существующую.

        Нужен учёту (`runs.Ledger`): на задачу ведётся один комментарий,
        который обновляется по мере заходов. Без правки на месте задача с
        тремя заходами обрастала бы тремя карточками, и человек читал бы
        историю пересчётов вместо итога.

        `key` — id задачи ИЛИ её идентификатор («DEV-18»): учёт собирается из
        имён рабочих каталогов, где лежит второе, а id там нет вовсе.
        `marker` — ПЕРВАЯ СТРОКА нашего комментария целиком. Не подстрока
        где-то в теле: подстроку человек воспроизводит сам, когда цитирует
        формат в обсуждении той же задачи, и правка «своего» комментария
        затирает чужой текст (DEV-18: метка `"учёт": 1` совпала с блоком,
        приведённым примером в согласованном HLD).
        Возвращает «создан» или «поправлен» — для журнала и для теста.
        """
        ...

    def assign(self, issue_id: str, user_id: str) -> dict: ...

    def set_state(self, issue_id: str, state_name: str) -> dict:
        """Перевести задачу в состояние ПО ИМЕНИ. Возвращает `{from, to,
        already}`; нет такого имени в команде — исключение с перечнем.

        Нужен мержу (`deploy.Verification.on_merge`): PR влит в основную
        ветку — задача закрывается приёмом. Имя, а не id: id состояния у
        каждой команды свой, а имя — настройка инсталляции
        (`AGENT_DONE_STATE_NAME`). НЕОБЯЗАТЕЛЬНЫЙ, как архив: у площадки со
        своим трекером его может не быть — тогда мерж скажет об этом в журнал
        и статус не тронет.
        """
        ...

    def issue_assignee_id(self, issue_id: str) -> str: ...

    def delete(self, issue_id: str) -> str: ...

    def unassigned_issues(self, team_id: str) -> list[dict]: ...

    def issues_awaiting(self, team_id: str,
                        states: tuple[str, ...]) -> list[dict]: ...

    def open_count(self, team_id: str,
                   states: tuple[str, ...]) -> int: ...

    def assigned_issues(self, user_id: str, states: tuple[str, ...]) -> list[dict]:
        """Что висит на исполнителе в перечисленных состояниях.

        Нужен инсталляции без вебхука: там о назначении узнают опросом.
        """
        ...

    def issue_label_names(self, issue_id: str) -> list[str]:
        """Имена лейблов задачи. Нужен приёмнику вебхука: событие назначения
        не всегда несёт лейблы, а без них инцидент уедет обычной работой.
        """
        ...

    def creator_of(self, issue_id: str) -> str:
        """Кто завёл задачу. Нужен ленте: закрытую работу агента возвращают
        автору, а в событии закрытия автора может не оказаться.
        """
        ...

    def users(self) -> list[dict]:
        """Люди рабочего пространства: `{id, name, displayName, active}`.
        Нужен передаче приёма: владелец называет человека по имени, а id в
        трекере знает только трекер.
        """
        ...
        ...

    # ── архив ────────────────────────────────────────────────────────────
    #
    # Оба метода НЕОБЯЗАТЕЛЬНЫЕ: у площадки архива может не быть вовсе.
    # Поэтому зовут их через `getattr`, и отсутствие — не ошибка, а «эта
    # площадка не умеет». Объявлены здесь, чтобы у второй реализации было по
    # чему сверяться.

    def archive_issue(self, issue_id: str) -> bool:
        """Убрать закрытую задачу из активных. True — убрали.

        Зовёт лента, когда задача перешла в закрытый статус: у бесплатного
        Linear потолок в 250 задач считает только НЕархивные, и без этого он
        превращается в срок, к которому приём постановок встанет.
        """
        ...

    def closed_issues(self, team_id: str, before: str) -> list[dict]:
        """Закрытые до `before` (ISO-8601, UTC) и ещё не архивные задачи
        команды — с временем закрытия.

        Нужен только отложенному архиву (`AGENT_ARCHIVE_AFTER_DAYS > 0`): при
        нуле лента убирает задачу сразу и спрашивать нечего. Возраст отбирает
        трекер, а не вызывающий: закрытых свежее порога бывает больше
        страницы, и просеивание на своей стороне до старых не дошло бы.
        """
        ...



def _is_mine(comment: dict, marker: str, me: str) -> bool:
    """Наш ли это комментарий-карточка.

    Два условия, и ни одного «метка встречается в теле»:
    первая непустая строка равна метке — то есть комментарий НАЧИНАЕТСЯ
    карточкой, а не упоминает её; и автор либо мы, либо неизвестен (у
    комментария от приложения `user` пустой — тогда решает первая строка,
    иначе писатель заводил бы новую карточку каждый заход).
    """
    body = comment.get("body") or ""
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    if first != marker:
        return False
    author = (comment.get("user") or {}).get("id") or ""
    return not me or not author or author == me


class LinearTracker:
    API = "https://api.linear.app/graphql"

    def __init__(self, api_key: str, api_url: str = "",
                 incident_label: str = "", author_key: str = ""):
        """`author_key` — ключ ЛИЧНОСТИ приёмщика: под её именем появляются
        заведённая задача и разбор постановки. Пусто — всё идёт ключом
        инсталляции, как было до личностей.

        Почему две ручки, а не одна: у приложения трекера с `actor=app`
        скоупы узкие (`issues:create`, `comments:create`), правка задачи в них
        не входит. Назначение по кнопке «Отдать агенту», удаление и запросы
        обязаны работать независимо от того, заведена личность или нет, —
        поэтому личность отвечает только за авторство, а всё остальное
        остаётся на `api_key`. Третьего адреса ключа нет: оба приходят
        параметром, окружение читает сборка (`telegram.app_from_env`).
        """
        self._key = api_key
        self._author_key = author_key
        self._url = api_url or self.API
        # Пусто — развилки «дежурный или работа» у этой инсталляции нет, и
        # выдумывать её не надо: лейбл будет ставить человек руками.
        self._incident_label = incident_label

    def _gql(self, query: str, variables: dict, key: str = "") -> dict:
        """`key` — ключ, отличный от ключа инсталляции: авторские запросы
        (создание задачи, комментарий) уходят от имени личности."""
        import urllib.request
        req = urllib.request.Request(
            self._url,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": key or self._key})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        if resp.get("errors"):
            raise RuntimeError(str(resp["errors"]))
        return resp.get("data") or {}

    def whoami(self, key: str = "") -> dict:
        """Кем трекер считает предъявителя ключа. Пустой ответ — ключ мёртв.

        Нужен сторожу личностей: отозванный токен выглядит ровно как живой,
        и отличить их можно ТОЛЬКО сходив в трекер.
        """
        return (self._gql("query{viewer{id name}}", {}, key) or {}).get("viewer") or {}

    def issue_by_identifier(self, identifier: str) -> dict | None:
        """Задача по идентификатору («DEV-86»): id, заголовок, адрес, описание.
        None — задачи нет. Всё остальное (сеть, ключ, лимит) — исключение:
        «трекер не ответил» и «задачи нет» обязаны различаться, иначе ворота
        отсеют живую задачу из-за упавшей сети.

        Linear принимает идентификатор в `issue(id:)` наравне с uuid, а на
        отсутствующий отвечает ошибкой «Entity not found», не пустым полем.
        """
        try:
            got = self._gql(
                "query($k:String!){issue(id:$k){id identifier title url "
                "description state{name type}}}", {"k": identifier})
        except RuntimeError as e:
            if "not found" in str(e).lower():
                return None
            raise
        return got.get("issue") or None

    def create_issue(self, team_id: str, title: str, description: str,
                     role: str = "", project_id: str = "") -> dict:
        """Задача создаётся в Backlog и БЕЗ ИСПОЛНИТЕЛЯ — это и есть гейт
        качества приёма: пока на ней никого нет, ничего не выполняется, а
        человек правит её в трекере. Исполнителя ставит отдельная кнопка.

        Статус спрашиваем у команды, а не полагаемся на её дефолт: он
        настраивается, и «черновик приехал сразу в Todo» смешал бы
        непросмотренное с принятым в работу. Обязательным этот запрос не
        делаем: задача в дефолтном статусе лучше, чем несозданная.
        """
        issue_input: dict = {"teamId": team_id, "title": title[:255],
                             "description": description}
        # Без проекта задачу не отфильтровать: в доске она есть, а в списке
        # проекта — нет. 12.09.2026 так накопилось десять бесхозных задач, и
        # шесть из них завёл этот самый приём.
        if project_id:
            issue_input["projectId"] = project_id
        if role == "incident":
            # Срочный — сразу, а не «потом поднимем». Инцидент, лежащий в
            # общей куче с обычным приоритетом, ничем от неё не отличается:
            # 12.09.2026 TASK-302 так и пролежала 50 минут, пока человек не
            # заметил карточку. 1 — Urgent (0 нет, 2 high, 3 normal, 4 low).
            issue_input["priority"] = 1
        if role == "incident" and self._incident_label:
            # Лейбл вешаем сразу при создании, а не отдельным апдейтом:
            # назначение на агента может поймать вебхук, и лейбл должен быть
            # на задаче раньше, чем по ней придёт событие назначения.
            try:
                issue_input["labelIds"] = [self._incident_label_id(team_id)]
            except Exception as e:
                # Задача без лейбла лучше несозданной, но это именно потеря
                # маршрута: приём разобрал инцидент, а агент получит его
                # обычной работой. Громко в лог; человек увидит это и по самой
                # задаче — лейбла на карточке не будет.
                log.error("лейбл «%s» не поставлен на задачу команды %s (%s) — "
                          "инцидент уедет обычной работой",
                          self._incident_label, team_id, e)
        try:
            states = (((self._gql(
                "query($id:String!){team(id:$id){states(first:50)"
                "{nodes{id type}}}}", {"id": team_id})
                .get("team") or {}).get("states") or {}).get("nodes") or [])
            backlog = next((s for s in states if s.get("type") == "backlog"), None)
            if backlog:
                issue_input["stateId"] = backlog["id"]
        except Exception as e:
            log.warning("не спросил статусы команды %s (%s) — создаю в дефолтном",
                        team_id, e)
        # От имени личности — только само создание. Всё, что выше (статусы
        # команды, лейбл дежурного), спрашивается ключом инсталляции: это не
        # авторство, а обстановка, и узкие скоупы приложения на неё не
        # рассчитаны — лейбл, например, приходится заводить.
        created = self._gql(
            "mutation($input:IssueCreateInput!){issueCreate(input:$input)"
            "{success issue{id identifier url}}}",
            {"input": issue_input}, self._author_key).get("issueCreate") or {}
        if not created.get("success") or not created.get("issue"):
            raise RuntimeError(str(created))
        return created["issue"]

    def comment(self, issue_id: str, body: str) -> None:
        """Комментарий от имени приёмщика (или ключом инсталляции, если
        личности нет)."""
        done = self._gql(
            "mutation($i:String!,$b:String!){commentCreate(input:{issueId:$i,"
            "body:$b}){success}}",
            {"i": issue_id, "b": body}, self._author_key).get("commentCreate") or {}
        if not done.get("success"):
            raise RuntimeError(str(done))

    def upsert_comment(self, key: str, marker: str, body: str) -> str:
        """Один запрос на поиск, один на запись.

        Задача читается по `key`, который у Linear принимает и id, и
        идентификатор вида «DEV-18» — это и позволяет учёту не знать id.

        Ключом ИНСТАЛЛЯЦИИ, а не личности: у приложения-личности скоуп
        `comments:create` без правки, и карточка обновлялась бы только в
        первый раз, молча заводя вторую при каждом следующем заходе.

        Свой комментарий узнаётся по ПЕРВОЙ СТРОКЕ, а не по вхождению метки в
        тело: цитата метки внутри чужого текста — обычное дело, и по ней
        правка уедет в чужой комментарий (см. протокол выше).
        """
        # Сотни комментариев хватает с запасом: карточка ложится в задачу
        # после первого же прогона сводки, то есть оказывается в начале
        # списка, а не в конце. Перерасти эту границу может только задача, у
        # которой сто комментариев появились РАНЬШЕ первой карточки.
        #
        # `viewer` спрашивается тем же запросом: автор — вторая, независимая
        # проверка «своё ли». Сама по себе она не спасает (у этой инсталляции
        # ключ владельца, и обсуждение он пишет тем же лицом), но отсекает
        # карточку, процитированную из-под другого ключа — личности или
        # человека.
        got = self._gql(
            "query($k:String!){viewer{id} issue(id:$k){id comments(first:100)"
            "{nodes{id body user{id}}}}}", {"k": key})
        me = (got.get("viewer") or {}).get("id") or ""
        got = got.get("issue") or {}
        if not got.get("id"):
            raise RuntimeError(f"задача {key} не найдена")
        mine = next((c for c in (got.get("comments") or {}).get("nodes") or []
                     if _is_mine(c, marker, me)), None)
        if mine:
            done = self._gql(
                "mutation($i:String!,$b:String!){commentUpdate(id:$i,"
                "input:{body:$b}){success}}",
                {"i": mine["id"], "b": body}).get("commentUpdate") or {}
            if not done.get("success"):
                raise RuntimeError(str(done))
            return "поправлен"
        done = self._gql(
            "mutation($i:String!,$b:String!){commentCreate(input:{issueId:$i,"
            "body:$b}){success}}",
            {"i": got["id"], "b": body}).get("commentCreate") or {}
        if not done.get("success"):
            raise RuntimeError(str(done))
        return "создан"

    def _incident_label_id(self, team_id: str) -> str:
        """id лейбла дежурного в этой команде; заводит его, если такого нет.

        Ищем по имени среди всех лейблов воркспейса, а не только командных:
        лейбл может быть заведён на уровень воркспейса (тогда он у команды
        есть, но в `team.labels` его не видно), и попытка создать одноимённый
        упёрлась бы в отказ по дублю.

        Одноимённый лейбл ЧУЖОЙ команды не годится: создание задачи с ним
        откажет, и задача не создастся вовсе. Заводим свой — одноимённые в
        разных командах Linear разрешает.
        """
        nodes = ((self._gql(
            "query{issueLabels(first:250){nodes{id name team{id}}}}", {})
            .get("issueLabels") or {}).get("nodes") or [])
        target = self._incident_label.strip().casefold()
        same = [n for n in nodes
                if str((n or {}).get("name") or "").strip().casefold() == target]
        # Команда важнее воркспейса: есть оба — вешаем командный, это тот же
        # лейбл, который человек видит в фильтрах своей доски.
        mine = [n for n in same if str((n.get("team") or {}).get("id") or "") == str(team_id)]
        if mine:
            return mine[0]["id"]
        shared = [n for n in same if not (n.get("team") or {}).get("id")]
        if shared:
            return shared[0]["id"]
        created = self._gql(
            "mutation($input:IssueLabelCreateInput!){issueLabelCreate(input:$input)"
            "{success issueLabel{id}}}",
            {"input": {"name": self._incident_label, "teamId": team_id}}
        ).get("issueLabelCreate") or {}
        if not created.get("success") or not created.get("issueLabel"):
            raise RuntimeError(str(created))
        log.info("лейбл «%s» заведён в команде %s", self._incident_label, team_id)
        return created["issueLabel"]["id"]

    def issue_assignee_id(self, issue_id: str) -> str:
        """Кто исполнитель, или пусто. Нужен напоминанию про инцидент: пока на
        задаче никого нет, работа не идёт, сколько бы меток на ней ни было."""
        issue = (self._gql("query($id:String!){issue(id:$id){assignee{id}}}",
                           {"id": issue_id}).get("issue") or {})
        return str((issue.get("assignee") or {}).get("id") or "")

    def assign(self, issue_id: str, user_id: str) -> dict:
        """Исполнителя сначала читаем, и уже стоящего не переставляем
        (`already`): повторное назначение тем же значением — это второе
        событие трекера, мимо дедупа очереди, и две сессии на одну задачу."""
        issue = (self._gql(
            "query($id:String!){issue(id:$id){identifier url assignee{id}}}",
            {"id": issue_id}).get("issue") or {})
        if not issue:
            raise RuntimeError(f"задача {issue_id} не найдена")
        if str((issue.get("assignee") or {}).get("id") or "") == str(user_id):
            return {**issue, "already": True}
        updated = self._gql(
            "mutation($id:String!,$a:String!){issueUpdate(id:$id,"
            "input:{assigneeId:$a}){success issue{id identifier url}}}",
            {"id": issue_id, "a": user_id}).get("issueUpdate") or {}
        if not updated.get("success"):
            raise RuntimeError(str(updated))
        return {**(updated.get("issue") or issue), "already": False}

    def set_state(self, issue_id: str, state_name: str) -> dict:
        """Состояние сначала читаем, и уже стоящее не переставляем
        (`already`): повторный `issueUpdate` тем же значением — это второе
        событие трекера, второй проход ленты и уборки. Имя разрешается по
        состояниям команды задачи; нет такого — ошибка называет, какие есть:
        промахнуться мимо имени молча нельзя."""
        issue = (self._gql(
            "query($id:String!){issue(id:$id){id state{id name type} "
            "team{id name states(first:60){nodes{id name}}}}}",
            {"id": issue_id}).get("issue") or {})
        if not issue:
            raise RuntimeError(f"задача {issue_id} не найдена")
        cur = issue.get("state") or {}
        was = str(cur.get("name") or "")
        if was == state_name:
            return {"from": was, "to": state_name, "already": True}
        team = issue.get("team") or {}
        states = (team.get("states") or {}).get("nodes") or []
        state_id = next((st["id"] for st in states if st.get("name") == state_name), "")
        if not state_id:
            raise RuntimeError(
                f"в команде {team.get('name') or team.get('id') or '?'} нет статуса "
                f"{state_name!r}, есть: "
                + (", ".join(repr(st.get("name") or "") for st in states) or "ни одного"))
        updated = self._gql(
            "mutation($id:String!,$s:String!){issueUpdate(id:$id,"
            "input:{stateId:$s}){success}}",
            {"id": issue_id, "s": state_id}).get("issueUpdate") or {}
        if not updated.get("success"):
            raise RuntimeError(str(updated))
        return {"from": was, "to": state_name, "already": False}

    def assigned_issues(self, user_id: str, states: tuple[str, ...]) -> list[dict]:
        """Задачи исполнителя в состояниях нужного типа.

        Фильтруем типом состояния, а не именем: имена у каждой команды свои
        («Todo», «К работе»), типы у Linear общие.
        """
        data = self._gql(
            "query($u:ID!,$t:[String!]!){issues(filter:{assignee:{id:{eq:$u}},"
            "state:{type:{in:$t}}}, first:50){nodes{id identifier title url "
            "description state{name type} labels{nodes{name}}}}}",
            {"u": user_id, "t": list(states)})
        return ((data.get("issues") or {}).get("nodes") or [])

    def unassigned_issues(self, team_id: str) -> list[dict]:
        """Бэклог команды: что не начато и ни на ком не висит.

        Берёт тяга. Состояния — типами, как и у `assigned_issues`: имена у
        каждой команды свои. Отменённое и сделанное сюда не попадает по типу,
        а взятое кем-то — по фильтру исполнителя.
        """
        data = self._gql(
            "query($t:ID!){issues(filter:{team:{id:{eq:$t}},"
            "assignee:{null:true},state:{type:{in:[\"backlog\",\"unstarted\"]}}},"
            " first:50, orderBy:createdAt){nodes{id identifier title url "
            "priority createdAt state{name type} labels{nodes{name}}}}}",
            {"t": team_id})
        return ((data.get("issues") or {}).get("nodes") or [])

    def issues_awaiting(self, team_id: str,
                        states: tuple[str, ...]) -> list[dict]:
        """Задачи команды в перечисленных состояниях.

        Берёт очередь подписи — отсюда и имя: изначально спрашивали только
        стопки человека. Сверки спрашивают тем же запросом другие состояния
        (`Reconciler` — стопки, `Stalled` — работу), и это один и тот же
        вопрос «кто сейчас в этих состояниях».

        Тянем сразу и обсуждение: карточка должна отвечать на вопрос «можно
        не глядя?» без второго запроса — на телефоне каждый лишний поход это
        секунда ожидания и повод не открыть.

        `assignee` — с id, а не только именем: сверка «в работе» решает по
        нему, агенту принадлежит задача или человеку, и на имени эта развилка
        держаться не может (людей с одинаковым именем никто не запрещал).
        """
        data = self._gql(
            "query($t:ID!,$s:[String!]!){issues(filter:{team:{id:{eq:$t}},"
            "state:{name:{in:$s}}}, first:30, orderBy:updatedAt)"
            "{nodes{id identifier title url description priorityLabel updatedAt "
            "state{name} assignee{id name} "
            "comments(first:20){nodes{body createdAt user{name}}}}}}",
            {"t": team_id, "s": list(states)})
        return ((data.get("issues") or {}).get("nodes") or [])

    def open_count(self, team_id: str, states: tuple[str, ...]) -> int:
        """Сколько задач команды ЖДЁТ ПОДПИСИ прямо сейчас.

        По именам состояний, а не по типу: у Linear «In Progress» и «In
        Review» оба `started`, а копится только второе. Работу и так
        ограничивают слоты — узкое место в приёмке.
        """
        data = self._gql(
            "query($t:ID!,$s:[String!]!){issues(filter:{team:{id:{eq:$t}},"
            "state:{name:{in:$s}}}, first:250){nodes{id}}}",
            {"t": team_id, "s": list(states)})
        return len(((data.get("issues") or {}).get("nodes") or []))

    def issue_label_names(self, issue_id: str) -> list[str]:
        """Лейблы задачи по её id. Спрашивает приёмник вебхука, когда событие
        назначения их не принесло."""
        issue = self._gql("query($id:String!){issue(id:$id)"
                          "{labels{nodes{name}}}}",
                          {"id": issue_id}).get("issue") or {}
        return label_names(issue)

    def creator_of(self, issue_id: str) -> str:
        """id автора задачи. Спрашивает лента, когда возвращает закрытую
        работу тому, кто её принёс, а в событии автора не оказалось."""
        issue = self._gql("query($id:String!){issue(id:$id){creator{id}}}",
                          {"id": issue_id}).get("issue") or {}
        return str((issue.get("creator") or {}).get("id") or "")

    def users(self) -> list[dict]:
        """Люди рабочего пространства. Спрашивает `/reviewer Иван`: имя
        называет владелец, id знает трекер. `active` едет вместе — передать
        приём деактивированному значит потерять работу."""
        nodes = ((self._gql("query{users{nodes{id name displayName active}}}", {})
                  .get("users") or {}).get("nodes") or [])
        return [{"id": str(u.get("id") or ""), "name": u.get("name") or "",
                 "displayName": u.get("displayName") or "",
                 "active": bool(u.get("active", True))}
                for u in nodes if u.get("id")]

    def archive_issue(self, issue_id: str) -> bool:
        """`issueArchive` — задача уходит из активных, но остаётся целиком.

        Проверено роундтрипом 12.09.2026 (DEV-62, DEV-65): после архивации
        `issue(id:)` отдаёт задачу вместе со статусом, лейблами и ВСЕМИ
        комментариями, а счётчик неархивных уменьшается. Значит учёт, который
        живёт в комментариях, архивацию переживает, а потолок бесплатного
        тарифа перестаёт быть сроком.
        """
        done = self._gql("mutation($id:String!){issueArchive(id:$id){success}}",
                         {"id": issue_id}).get("issueArchive") or {}
        if not done.get("success"):
            raise RuntimeError(str(done))
        return True

    def closed_issues(self, team_id: str, before: str) -> list[dict]:
        """Закрытые до `before` и ещё не архивные задачи команды.

        Архивные сюда не попадают САМИ: без `includeArchived:true` Linear их
        не отдаёт (проверено 12.09.2026 — 65 задач против 72). Отдельного
        «и не архивные» в фильтре поэтому нет, и добавлять его нельзя: с
        `includeArchived:true` в список вернулись бы и удалённые — у Linear
        корзина это тот же `archivedAt`.

        Возраст отбирает трекер: `orderBy:updatedAt` отдаёт СВЕЖИЕ первыми, и
        сотня свежезакрытых вытеснила бы из страницы всё, что пора убирать.
        Условие через `or`, потому что у `completed` заполнен `completedAt`, у
        `canceled` — `canceledAt`, и одно другое не подменяет (проверено на
        живой доске: 20 задач до 12.09, ноль до 01.09).

        Сотня за круг: обход ходит раз в сутки, а закрывается в день единицы.
        Не поместившееся уедет следующим — теперь это правда, потому что в
        страницу попадают только те, кого уже пора убрать.
        """
        data = self._gql(
            "query($t:ID!,$d:DateTimeOrDuration!){issues(filter:{team:{id:{eq:$t}},"
            "or:[{completedAt:{lt:$d}},{canceledAt:{lt:$d}}]}, first:100,"
            " orderBy:updatedAt){nodes{id identifier teamId completedAt "
            "canceledAt updatedAt assignee{id}}}}",
            {"t": team_id, "d": before})
        return ((data.get("issues") or {}).get("nodes") or [])

    def delete(self, issue_id: str) -> str:
        """`issueDelete` — это корзина, а не стирание: задача восстановима из
        Trash. Для «выбросил случайно» этого достаточно, и поэтому кнопка не
        переспрашивает.

        Идентификатор читаем заранее: сказать, какую именно убрали, важнее
        одного лишнего запроса — карточек в личке несколько подряд.
        """
        ident = ((self._gql("query($id:String!){issue(id:$id){identifier}}",
                            {"id": issue_id}).get("issue") or {}).get("identifier") or "")
        done = self._gql("mutation($id:String!){issueDelete(id:$id){success}}",
                         {"id": issue_id}).get("issueDelete") or {}
        if not done.get("success"):
            raise RuntimeError(str(done))
        return ident
