"""TrackerAdapter: изоляция runner.py от конкретного трекера задач.

Сейчас единственная реализация — LinearAdapter, поведение перенесено 1:1 из
прежнего прямого использования Linear GraphQL внутри runner.py. Ядро
(ensure_handoff, обработка падений сессии) знает только про этот интерфейс,
не про Linear конкретно.

Не включает разбор входящих вебхуков (кто ставит задачу в очередь агента) —
это часть отдельного сервиса (events-api), не этого репозитория. Здесь только
то, что runner.py делает сам во время и после сессии: комментарий,
переназначение, чтение и правка состояния задачи для проверки сдачи.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Protocol

log = logging.getLogger("agent-runner")


class TrackerAdapter(Protocol):
    def post_comment(self, task_id: str, body: str) -> bool: ...

    def list_comments(self, task_id: str, limit: int = 50) -> list[dict]: ...

    def update_comment(self, comment_id: str, body: str) -> bool: ...

    def assign(self, task_id: str, user_id: str) -> None: ...

    def fetch_issue_snapshot(self, task_id: str) -> dict: ...

    def update_issue(self, task_id: str, fields: dict) -> None: ...

    def add_label(self, task_id: str, team_id: str, name: str) -> bool: ...


class LinearAdapter:
    """Реализация TrackerAdapter поверх Linear GraphQL API."""

    def __init__(self, api_key: str, api_url: str = "https://api.linear.app/graphql"):
        self._api_key = api_key
        self._api_url = api_url

    def _graphql(self, query: str, variables: dict) -> dict:
        """Пустой ключ → no-op, чтобы раннер не падал из-за него."""
        if not self._api_key:
            log.warning("LINEAR_API_KEY пуст — пропускаю запись в Linear")
            return {}
        req = urllib.request.Request(
            self._api_url,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json", "Authorization": self._api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.error("Linear API failed: %s", e)
            return {}
        # Прикладной отказ приезжает с кодом 200 и полем `errors`: нет прав,
        # чужая задача, неверный id, узкий скоуп у личности. Без этой проверки
        # запись молча не происходила при «успешном» запросе — вердикт
        # проверки исчезал бесследно, а на доске задача выглядела
        # непроверенной (найдено вердиктом по DEV-66).
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            log.error("Linear отказал (код 200, поле errors): %s",
                      "; ".join(str(e.get("message") or e) for e in errors)[:400])
        return data

    def post_comment(self, task_id: str, body: str) -> bool:
        """Записан ли комментарий. Возвращаем ответ, а не `None`.

        Молчащий отказ здесь дороже любого другого: комментарий — это вердикт
        проверки или отчёт о работе, и его пропажа неотличима от «сессия ничего
        не делала». Проверять надо ИМЕННО `success`: `errors` ловит отказ
        уровня запроса, а `success: false` — отказ уровня мутации, и это разные
        случаи.
        """
        data = self._graphql(
            "mutation($i:String!,$b:String!){commentCreate(input:{issueId:$i,body:$b}){success}}",
            {"i": task_id, "b": body},
        )
        ok = bool((((data or {}).get("data") or {}).get("commentCreate") or {}).get("success"))
        if not ok:
            log.error("комментарий в %s НЕ записан — текст потерян (%d знаков)",
                      task_id, len(body or ""))
        return ok

    # Окно ленты заходов (`timeline.py`). Шире, чем `SNAPSHOT_COMMENTS`: снимок
    # отвечает на «есть ли свежий комментарий», а лента — на «как шла работа
    # целиком», и обрезанная с начала она соврёт именно про первые заходы, то
    # есть про то, откуда взялось расхождение вердиктов.
    TIMELINE_COMMENTS = 50

    def list_comments(self, task_id: str, limit: int = TIMELINE_COMMENTS) -> list[dict]:
        """Комментарии задачи для ленты заходов: `id`, `createdAt`, `body`, автор.

        `first:N`, а не `last:N`: Linear отдаёт связи новыми вперёд, и `last`
        вернул бы САМЫЕ СТАРЫЕ (см. `SNAPSHOT_COMMENTS`). Ленте нужны все, но
        если задача переросла окно — терять новые хуже: по ним считается
        последний вердикт и что требуется от человека.

        Автор приезжает двумя полями — человек (`user`) и приложение
        (`botActor`): личности фабрики пишут приложениями, и без второго поля
        у половины заходов автора не было бы вовсе.
        """
        data = self._graphql(
            "query($id:String!,$n:Int!){issue(id:$id){comments(first:$n){nodes{"
            "id createdAt body user{name} botActor{name}}}}}",
            {"id": task_id, "n": int(limit)},
        )
        nodes = ((((data or {}).get("data") or {}).get("issue") or {})
                 .get("comments") or {}).get("nodes") or []
        out = []
        for n in nodes:
            who = (n.get("user") or n.get("botActor") or {}).get("name") or ""
            out.append({"id": n.get("id") or "", "createdAt": n.get("createdAt") or "",
                        "body": n.get("body") or "", "author": who})
        return out

    def update_comment(self, comment_id: str, body: str) -> bool:
        """Переписать существующий комментарий. Записан ли — как у создания.

        Нужно ровно ленте заходов: она ОДНА на задачу и правится на месте.
        Копить по комментарию на заход значило бы лечить полотна полотнами.
        """
        data = self._graphql(
            "mutation($i:String!,$b:String!){commentUpdate(id:$i,input:{body:$b}){success}}",
            {"i": comment_id, "b": body},
        )
        ok = bool((((data or {}).get("data") or {}).get("commentUpdate") or {}).get("success"))
        if not ok:
            log.error("комментарий %s НЕ переписан (%d знаков)", comment_id, len(body or ""))
        return ok

    def assign(self, task_id: str, user_id: str) -> None:
        """Решение о том, стоит ли вообще назначать (и что делать при пустом
        user_id), — за вызывающим; адаптер только исполняет."""
        self._graphql(
            "mutation($i:String!,$a:String!){issueUpdate(id:$i,input:{assigneeId:$a}){success}}",
            {"i": task_id, "a": user_id},
        )

    # Окно комментариев в снимке. Linear отдаёт связи НОВЫМИ ВПЕРЁД, и
    # `last:N` здесь — N самых СТАРЫХ: на DEV-4 (18 комментариев)
    # `comments(last:5)` вернул 07.09–13.09 21:16, `first:5` — 14.09 02:19 и
    # моложе (проба по живому API 15.09.2026). С `last:5` любая задача,
    # набравшая больше пяти комментариев, — то есть каждый второй заход —
    # выглядела «без отчёта», и досдача дописывала за сессию, которая всё
    # написала: восемь из восемнадцати досдач client-b за 06–14.09 (DEV-95).
    SNAPSHOT_COMMENTS = 10

    def fetch_issue_snapshot(self, task_id: str) -> dict:
        """Сырьё по задаче: статус, исполнитель, статусы команды (для
        разрешения имени статуса в id), самые свежие комментарии.

        Имя команды — ради ошибки «статуса нет»: по одному id команду в
        журнале не опознать."""
        data = self._graphql(
            "query($id:String!){issue(id:$id){id state{name type} assignee{id}"
            " team{id name states(first:60){nodes{id name}}}"
            " labels{nodes{id name}}"
            f" comments(first:{self.SNAPSHOT_COMMENTS}){{nodes{{createdAt}}}}}}}}",
            {"id": task_id},
        )
        return ((data or {}).get("data") or {}).get("issue") or {}

    def update_issue(self, task_id: str, fields: dict) -> None:
        """`fields` — плоский словарь полей issueUpdate, например
        `{"assigneeId": "...", "stateId": "..."}`. Пустой — не зовёт API."""
        if not fields:
            return
        sets = ",".join(k + ':"' + v + '"' for k, v in fields.items())
        self._graphql(
            "mutation($i:String!){issueUpdate(id:$i,input:{" + sets + "}){success}}",
            {"i": task_id},
        )

    def add_label(self, task_id: str, team_id: str, name: str) -> bool:
        """Повесить на задачу метку ПО ИМЕНИ. Нет такой в команде — завести.

        Метка нужна раннеру для исхода «проверять было нечего» (DEV-96): по
        ней считается доля работы, ушедшей к людям непроверенной. Искать по
        имени, а не держать id в настройке: у каждой инсталляции своя доска,
        и id метки — не то, что человек станет переписывать в окружение.
        Заводим сами, если нет: без метки долю не сосчитать, а «метки нет»
        не должно тихо превращаться в «метку не поставили».

        `issueAddLabel`, а не `issueUpdate(labelIds)`: второй ЗАМЕНЯЕТ набор
        меток, и правка задачи снесла бы всё, что на ней уже висело.
        """
        data = self._graphql(
            "query($n:String!){issueLabels(filter:{name:{eq:$n}},first:20)"
            "{nodes{id name team{id}}}}",
            {"n": name},
        )
        nodes = (((data or {}).get("data") or {}).get("issueLabels") or {}).get("nodes") or []
        # Своя метка команды важнее одноимённой общей: на доске видна именно
        # она. Общая (team=null) годится, если своей нет.
        mine = [n for n in nodes if (n.get("team") or {}).get("id") == team_id]
        shared = [n for n in nodes if not n.get("team")]
        label_id = ((mine or shared or nodes) or [{}])[0].get("id") or ""
        if not label_id:
            data = self._graphql(
                "mutation($n:String!,$t:String!){issueLabelCreate(input:{name:$n,teamId:$t})"
                "{success issueLabel{id}}}",
                {"n": name, "t": team_id},
            )
            created = (((data or {}).get("data") or {}).get("issueLabelCreate") or {})
            label_id = (created.get("issueLabel") or {}).get("id") or ""
            if not label_id:
                log.error("метка %r не заведена в команде %s — на %s не поставлена",
                          name, team_id, task_id)
                return False
            log.info("метка %r заведена в команде %s", name, team_id)
        data = self._graphql(
            "mutation($i:String!,$l:String!){issueAddLabel(id:$i,labelId:$l){success}}",
            {"i": task_id, "l": label_id},
        )
        ok = bool((((data or {}).get("data") or {}).get("issueAddLabel") or {}).get("success"))
        if not ok:
            log.error("метка %r на %s НЕ поставлена", name, task_id)
        return ok
