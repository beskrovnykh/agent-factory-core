"""Кто есть кто: карта «человек в трекере ↔ чат в мессенджере».

Одна карта на всё: и кнопки под сообщением задачи, и приём, и команды
владельца опираются на неё. Второй источник правды об одних и тех же людях
завести проще всего и дороже всего — он расходится с первым молча.

Часть ПРОДУКТА. У инсталляции своя карта и свой владелец, но правила
одинаковые: незнакомому не пишем и кнопок не даём, владелец ровно один, а
обратный перевод «чат → человек» берётся из той же карты, а не из второй.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("intake")

# Сколько помним, кто принёс задачу через личку. Дольше, чем живёт сообщение
# в чате: возврат закрытой задачи автору случается через недели после того,
# как её завели.
AUTHOR_TTL_SEC = 90 * 24 * 3600


class People:
    """Карта инсталляции. Пустая — это рабочее состояние, а не поломка:
    площадка без команды просто не адресует никому лично."""

    def __init__(self, chat_by_user: dict | None = None, owner_user_id: str = "",
                 store=None):
        # Ключи и значения приводим к строкам сразу: в трекере id строковые, в
        # Telegram числовые, и сравнение «123 == '123'» молча ложно.
        self.chat_by_user = {str(k): str(v) for k, v in (chat_by_user or {}).items()}
        self.owner_user_id = str(owner_user_id or "")
        # Хранилище нужно только памяти об авторах приёмных задач. Без него
        # класс остаётся полезным — просто автор будет браться из трекера.
        self.store = store

    @classmethod
    def from_json(cls, raw: str, owner_user_id: str = "", store=None) -> "People":
        """Карта из строки JSON. Битая строка — не повод падать при старте:
        приём и сессии от неё не зависят, а сообщения просто не найдут адресата.
        """
        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            log.warning("карта людей: не разбирается как JSON — считаю пустой")
            parsed = {}
        if not isinstance(parsed, dict):
            log.warning("карта людей: ожидался объект — считаю пустой")
            parsed = {}
        return cls(parsed, owner_user_id, store)

    # ── перевод в обе стороны ────────────────────────────────────────────

    def chat_of(self, user_id) -> str:
        """Личный чат человека. Пусто — его нет в карте."""
        return self.chat_by_user.get(str(user_id or ""), "")

    def user_of(self, chat_id) -> str:
        """Человек по чату — обратная сторона той же карты."""
        target = str(chat_id or "")
        for user_id, chat in self.chat_by_user.items():
            if chat == target:
                return user_id
        return ""

    # ── доступ ───────────────────────────────────────────────────────────

    def knows(self, chat_id) -> bool:
        """Свой ли это человек. Кнопки под сообщением задачи пишут в трекер под
        общим ключом инсталляции, поэтому жать их может только своя команда:
        иначе любой участник группового чата закрывает любую задачу.

        Строго исполнителем не ограничиваем намеренно: владелец закрывает
        задачи за других, и это нормальный ход работы.
        """
        return str(chat_id or "") in set(self.chat_by_user.values())

    def is_owner(self, chat_id) -> bool:
        """Владелец системы. Сверяем по его чату из той же карты — другой
        здесь нет, и заводить вторую значило бы иметь два источника правды.

        Карта не заполнена → владельца нет и команды не работают ни у кого.
        Это безопаснее, чем «работают у всех».
        """
        owner_chat = self.chat_of(self.owner_user_id)
        return bool(owner_chat) and str(chat_id or "") == owner_chat

    # ── кто принёс задачу ────────────────────────────────────────────────
    #
    # Приёмная задача заводится под общим ключом инсталляции, поэтому автор у
    # всех таких задач один — владелец ключа, а не тот, кто её принёс. Автора
    # запоминаем сами, в момент создания: без этого возврат закрытой задачи
    # автору не работал бы ровно для тех, ради кого он и сделан.

    @staticmethod
    def author_key(issue_id: str) -> str:
        return f"intake:author:{issue_id}"

    async def remember_author(self, issue_id: str, chat_id) -> str:
        """Запомнить, кто принёс задачу. Возвращает id автора в трекере, либо
        пусто: незнакомый человек до приёма всё равно не доходит, но карту
        могли и почистить между приёмом и созданием."""
        author = self.user_of(chat_id)
        if not (issue_id and author and self.store):
            return ""
        try:
            await self.store.put(self.author_key(issue_id), author, AUTHOR_TTL_SEC)
        except Exception as e:
            log.warning("не запомнить автора задачи %s: %s", issue_id, e)
            return ""
        return author

    async def author_of(self, issue_id: str) -> str:
        """Автор приёмной задачи, если она пришла через мессенджер. Иначе
        пусто — и автором считается тот, кого назвал трекер."""
        if not (issue_id and self.store):
            return ""
        try:
            return str(await self.store.get(self.author_key(issue_id)) or "")
        except Exception as e:
            log.warning("не спросить автора задачи %s: %s", issue_id, e)
            return ""


def creator_id(issue: dict) -> str:
    """id автора задачи из тела события. Трекер кладёт его штатно — поход в
    API нужен, только если поля не оказалось."""
    return str(issue.get("creatorId") or (issue.get("creator") or {}).get("id") or "")
