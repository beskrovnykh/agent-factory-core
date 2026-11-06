"""Обновление инсталляции до новой версии ядра — по кнопке.

Общее у всех инсталляций: знать свою версию, заметить, что появилась новая,
сказать об этом человеку и применить по нажатию. Разное — КАК применить:
у одного клиента версия записана пином сабмодуля и катится через CI, у
другого ядро лежит клоном на хосте и ставится установщиком.

Поэтому здесь протокол `Source` с тремя методами и вся обвязка вокруг него,
а способ применения приносит инсталляция. Раньше это лежало целиком в
репозитории первого клиента, и второй инсталляции не досталось ничего —
она обновлялась руками и молча отставала.
"""
from __future__ import annotations

import asyncio
import inspect
import importlib
import logging
import os
from typing import Protocol

from .intake import Button

log = logging.getLogger("intake")


# Как часто инсталляция сама проверяет, не вышла ли новая версия. 0 — не
# проверять: у клиента это делает его CI по расписанию, а не приём.
CHECK_INTERVAL_SEC = int(os.environ.get("CORE_CHECK_INTERVAL_SEC", "0"))


def source_from_env(var: str = "CORE_UPDATE_SOURCE"):
    """Собрать реализацию протокола инсталляции по имени из окружения:
    `модуль:Класс`. По умолчанию — источник версий; тем же способом
    подключается и `deploy.Deployment` (`AGENT_DEPLOYMENT_SOURCE`).

    Реализацию приносит инсталляция — продукт не знает ни про её пины, ни про
    её установщик. Переменная не задана — этой способности просто нет.
    """
    spec = os.environ.get(var, "").strip()
    if not spec:
        return None
    if ":" not in spec:
        log.error("%s=%r — нужно «модуль:Класс», выключено", var, spec)
        return None
    mod, _, cls = spec.partition(":")
    try:
        return getattr(importlib.import_module(mod), cls)()
    except Exception as e:
        # Громко, но не падаем: приём важнее обновлений, и ронять его из-за
        # ненастроенного апдейтера нельзя.
        log.error("%s: %s не поднялся: %s", var, spec, e)
        return None


class Source(Protocol):
    """Откуда инсталляция знает про версии и как их применяет.

    Все три метода синхронные: их зовут из потока, чтобы не держать цикл.
    """

    def current(self) -> str:
        """Версия, которая сейчас работает. Пусто — узнать не вышло."""
        ...

    def available(self) -> str:
        """Версия, до которой можно обновиться. Пусто — узнать не вышло."""
        ...

    def apply(self, version: str) -> str:
        """Применить версию. Возвращает строку исхода для человека.

        Кидает исключение на отказе — вызывающий покажет его в чате, а не
        проглотит: обновление, которое молча не случилось, хуже отказа.

        ⚠️ НЕ ПЕРЕЗАПУСКАЙ ЗДЕСЬ ПРОЦЕСС, КОТОРЫЙ ТЕБЯ ВЫЗВАЛ. Обновление
        зовут из процесса приёма, и исход человеку пишется ПОСЛЕ возврата из
        этого метода. Перезапустив приём внутри `apply`, инсталляция убивает
        себя до отчёта: версия встаёт, а нажатие выглядит как «ничего не
        произошло». 12.09.2026 так прошло четыре обновления подряд, и каждое
        считали неудачей. Перезапускать приём надо отложенно и отдельным
        процессом (у systemd — `systemd-run --on-active`), остальное можно
        сразу.
        """
        ...

    # ── необязательное: чтобы сообщение читалось человеком ───────────────
    #
    # Версия для машины — это sha. Человеку в чате она не говорит ничего:
    # «10cd8c2b → 544a66e2» невозможно ни оценить, ни вспомнить. Источник,
    # который умеет назвать версию по-людски и перечислить, что в ней, —
    # называет. Не умеет — остаётся sha, и это рабочий исход.

    def describe(self, version: str) -> str:
        """Имя версии для человека: «0.6.14». Пусто — не знаем."""
        ...

    def changes(self, current: str, available: str) -> list[str]:
        """Что произошло между версиями — по строке на изменение, свежие
        первыми. Пусто — не знаем."""
        ...


def short(version: str) -> str:
    """Версию показываем короткой: 40 hex-символов sha в сообщении — шум."""
    v = (version or "").strip()
    return v[:8] if len(v) > 12 and all(c in "0123456789abcdef" for c in v) else v


# Сколько изменений показываем. Больше — это уже не сообщение, а лог: его
# пролистывают, не читая, и решение принимают всё равно вслепую.
MAX_CHANGES = 8

# Длина строки изменения. Заголовки коммитов бывают в абзац.
CHANGE_WIDTH = 90


def name_of(source, version: str) -> str:
    """Как назвать версию в сообщении. Источник не умеет — короткий sha."""
    ask = getattr(source, "describe", None)
    if ask:
        try:
            named = (ask(version) or "").strip()
            if named:
                return named
        except Exception as e:
            log.warning("имя версии %s не узнать: %s", short(version), e)
    return short(version)


def tell_apart(source, a: str, b: str) -> tuple[str, str]:
    """Имена двух версий так, чтобы их можно было различить.

    Номер версии больше не уникален на коммит: поднимаем его, когда есть что
    назвать, а не на каждую правку. Значит два разных коммита часто зовутся
    одинаково, и «0.6.17 старее установленной (0.6.17)» читается как
    бессмыслица. Совпали имена — дописываем sha, он различает всегда.
    """
    na, nb = name_of(source, a), name_of(source, b)
    if na != nb:
        return na, nb
    return f"{na} ({short(a)})", f"{nb} ({short(b)})"


def changes_between(source, current: str, available: str) -> list[str]:
    """Что вошло в новую версию. Источник не умеет — пустой список."""
    ask = getattr(source, "changes", None)
    if not ask:
        return []
    try:
        lines = ask(current, available) or []
    except Exception as e:
        log.warning("изменения между версиями не узнать: %s", e)
        return []
    out = []
    for line in lines[:MAX_CHANGES]:
        line = " ".join(str(line or "").split())
        if not line:
            continue
        out.append(line[:CHANGE_WIDTH].rstrip() + "…"
                   if len(line) > CHANGE_WIDTH else line)
    return out


def drift_text(current: str, available: str, source=None) -> str:
    """Сообщение о новой версии. `source` — чтобы назвать версии по-людски и
    перечислить, что в них; без него остаётся вид с sha.

    Заголовок говорит о НОВОМ, а не об отставании: человек читает его, чтобы
    решить, ставить ли, — и «отстал» в этом решении ничего не добавляет, зато
    звучит как упрёк.
    """
    was, now = tell_apart(source, current, available)
    head = f"Новая версия agent-factory: {now} (сейчас {was})"
    lines = changes_between(source, current, available)
    if not lines:
        return head + ". Обновить?"
    body = "\n".join(f"• {line}" for line in lines)
    return f"{head}\n\n{body}\n\nОбновить?"


def update_button(version: str, source=None) -> list[Button]:
    """Кнопка несёт ВЕРСИЮ, а не «обнови до последней».

    Человек согласился на то, что увидел в этом сообщении. Пока он думает,
    в ядро может уехать ещё что-нибудь, и «последняя» окажется не тем, на
    что он смотрел. В подписи — человеческое имя версии, в нагрузке по-прежнему sha.
    """
    return [Button(f"⬆️ Обновить до {name_of(source, version)}",
                   f"update:apply:{version}")]


class Updater:
    def __init__(self, source: Source, transport, notify_chat: str = "",
                 may_apply=None):
        """`may_apply(user_id) -> bool` — кому разрешено нажимать. Может быть
        и асинхронной: у инсталляции право нажать выясняется походом в
        хранилище (кто СЕЙЧАС принимает работу — владелец или делегат), и
        заставлять её кешировать этот ответ ради синхронной сигнатуры значит
        принимать решение по протухшим данным.

        Пусто — разрешено всем, кто вообще дошёл до кнопки. Так оставлять
        нельзя там, где кнопка живёт в общем чате: нажатие выкатывает ядро
        на прод вместе с контрактом всех сессий.
        """
        self.source = source
        self.transport = transport
        self.notify_chat = str(notify_chat)
        self.may_apply = may_apply

    async def _stale(self, version: str) -> str:
        """Версия кнопки уже не нужна? Возвращает текст для человека, иначе
        пусто.

        «Уже стоит» узнаём сравнением с установленной. «Позади установленной»
        — через те же изменения, что показываем в сообщении: если от этой
        версии до установленной что-то есть, значит она старее.
        """
        try:
            current = await asyncio.to_thread(self.source.current)
        except Exception as e:
            # Не смогли спросить — не мешаем: отказ обновления хуже лишнего.
            log.warning("не спросить установленную версию: %s", e)
            return ""
        if not current:
            return ""
        if version == current:
            return (f"✅ {name_of(self.source, version)} уже стоит — "
                    f"обновлять нечего.")
        if changes_between(self.source, version, current):
            asked, installed = tell_apart(self.source, version, current)
            return (f"↩️ {asked} старее установленной ({installed}) — кнопка "
                    f"из старого сообщения. Обновление не откатываю.")
        return ""

    async def _say(self, chat_id, message_id: str, text: str, buttons) -> None:
        """Сказать человеку исход. Не прошла правка — новым сообщением: иначе
        снаружи это «нажал, и ничего»."""
        if not await self.transport.edit(chat_id, message_id, text, buttons):
            await self.transport.send(chat_id, text, buttons)

    async def _allowed(self, user_id) -> bool:
        """Право нажать. Отказ проверки — это ОТКАЗ, а не разрешение: кнопка
        выкатывает ядро, и «не смогли спросить» не повод пускать."""
        try:
            verdict = self.may_apply(user_id)
            if inspect.isawaitable(verdict):
                verdict = await verdict
            return bool(verdict)
        except Exception as e:
            log.warning("не спросить право на обновление (%s) — не пускаю", e)
            return False

    async def check(self, chat_id=None) -> str:
        """Сравнить версии и, если разошлись, прислать кнопку.

        Возвращает доступную версию, если отстали, иначе пустую строку.
        """
        chat = str(chat_id or self.notify_chat)
        try:
            current = await asyncio.to_thread(self.source.current)
            available = await asyncio.to_thread(self.source.available)
        except Exception as e:
            log.warning("версии не сравнить: %s", e)
            return ""
        if not current or not available or current == available:
            return ""
        if chat:
            await self.transport.send(
                chat, drift_text(current, available, self.source),
                update_button(available, self.source))
        return available

    async def perform(self, version: str, user_id=None) -> tuple[str, str]:
        """Само обновление — без чата. Возвращает (исход, текст для человека).

        Отдельно от `apply`, потому что нажать теперь можно из двух мест:
        кнопкой в чате и со страницы подписи (`signing.SigningPage`). Право,
        проверка «уже стоит / старее» и вызов источника у обоих одни; разное
        только то, куда написать исход.
        """
        if self.may_apply and not await self._allowed(user_id):
            return "not-allowed", "Обновлять ядро может только тот, кого пускает приём."
        if not version:
            return "no-version", "Версия не названа — обновлять нечего."
        stale = await self._stale(version)
        if stale:
            # Кнопка живёт в чате вечно, а версия — нет. Без этой проверки
            # нажатие на старое сообщение ОТКАТЫВАЕТ инсталляцию: источник
            # честно ставит ту версию, которую у него попросили, вместе с её
            # установщиком. 12.09.2026 так трижды подряд откатывали площадку
            # на сломанный установщик, и каждый раз это выглядело как «жму —
            # ошибка».
            return "stale-button", stale
        try:
            outcome = await asyncio.to_thread(self.source.apply, version)
        except Exception as e:
            log.warning("обновление до %s не прошло: %s", short(version), e)
            return "failed", (f"⚠️ Обновить до {name_of(self.source, version)} "
                              f"не вышло: {e}")
        log.info("ядро обновлено до %s (нажал %s): %s", short(version),
                 user_id or "?", outcome)
        return "applied", f"⬆️ Ядро → {name_of(self.source, version)}: {outcome}"

    async def apply(self, version: str, chat_id, message_id: str,
                    user_id=None) -> str:
        """Нажатие кнопки в чате. Исход пишем в то же сообщение и кнопку
        снимаем: она адресует конкретную версию, и второй раз по ней жать
        нечего."""
        code, text = await self.perform(version, user_id)
        if code in ("not-allowed", "no-version"):
            return code
        if code == "stale-button":
            await self._say(chat_id, message_id, text, None)
        elif code == "failed":
            # Кнопку оставляем: причина показана, человек может нажать ещё раз.
            # Правка может не пройти: мессенджер отказывается менять
            # сообщение на идентичное, а второе нажатие даёт тот же текст.
            await self._say(chat_id, message_id, text,
                            update_button(version, self.source))
        elif not await self.transport.edit(chat_id, message_id, text, None):
            # Сообщение могли удалить. Обновление случилось, и человек обязан
            # об этом узнать.
            await self.transport.send(chat_id, text)
        return code

    async def drift(self) -> tuple[str, str]:
        """(установленная, доступная) — для страницы, без сообщений в чат.
        Пусто в любой из двух — узнать не вышло."""
        try:
            current = await asyncio.to_thread(self.source.current)
            available = await asyncio.to_thread(self.source.available)
        except Exception as e:
            log.warning("версии не сравнить: %s", e)
            return "", ""
        return str(current or ""), str(available or "")

    async def watch(self, interval_sec: int = 0) -> None:
        """Проверять раз в interval_sec и присылать кнопку при расхождении.

        Ноль — не проверять вовсе: у инсталляции с собственным CI это делает
        он, и второй сторож только дублировал бы сообщения.
        """
        every = interval_sec or CHECK_INTERVAL_SEC
        if every <= 0:
            return
        log.info("слежу за версией ядра: раз в %dс", every)
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("проверка версии не удалась: %s", e)
            await asyncio.sleep(every)
