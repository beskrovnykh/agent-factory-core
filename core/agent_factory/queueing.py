"""Правила постановки задачи в очередь агента.

Что кладётся в очередь и чем обычная работа отличается от инцидента. Ни
одного похода в трекер: на входе — словарь задачи, каким его отдал любой
адаптер, на выходе — элемент очереди. Поэтому модуль ни от кого в пакете не
зависит, и импортируют его все — включая сам адаптер (`tracking.py` берёт
отсюда `label_names`).

Правила читают два пути постановки — вебхук (`webhook.py`) и опрос
назначений (`assignments.py`), — и ни один из них не про трекер. Развилка
«дежурный или работа» одна на все пути, включая кнопку «Отдать агенту»
(`cards.py`): без этого инцидент, пришедший не тем путём, тихо уезжал
обычной работой.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("intake")


# Потолок на симптом: дежурному нужна одна фраза, а не пересказ описания.
SYMPTOM_MAX = 600


def has_incident_label(names, label: str) -> bool:
    """Есть ли среди лейблов дежурный. Сравниваем по имени без учёта регистра
    и краевых пробелов: лейбл ставит человек руками, и «Дежурный» — это тот же
    лейбл, что и «дежурный»."""
    target = (label or "").strip().casefold()
    if not target:
        return False
    return any(str(n or "").strip().casefold() == target for n in (names or []))


def symptom_of(issue: dict) -> str:
    """Симптом для дежурной сессии: первый содержательный абзац описания,
    иначе заголовок. Markdown-заголовки пропускаем — «## Контекст» симптома
    не описывает."""
    for block in (issue.get("description") or "").split("\n\n"):
        # Строку-заголовок выбрасываем, а не весь абзац: «## Симптом» и текст
        # под ним часто идут одним абзацем, и вместе с разметкой выпала бы
        # ровно та фраза, ради которой симптом и читают.
        block = " ".join(line for line in block.splitlines()
                         if not line.lstrip().startswith("#"))
        block = " ".join(block.split())
        if not block:
            continue
        return block[:SYMPTOM_MAX] + "…" if len(block) > SYMPTOM_MAX else block
    return (issue.get("title") or "").strip()


def label_names(issue: dict) -> list[str]:
    """Имена лейблов из ответа трекера. Форма вложенная, и разбирать её на
    каждой стороне значит держать два знания об одном."""
    nodes = ((issue.get("labels") or {}).get("nodes")
             if isinstance(issue.get("labels"), dict) else issue.get("labels"))
    return [str((n or {}).get("name") or "") if isinstance(n, dict) else str(n)
            for n in (nodes or [])]


async def hand_over(tracker, issue_id: str, ident: str, rounds: int,
                    reviewer_id: str) -> str:
    """Круги исчерпаны — задача уходит принимающему, а не на новый заход.

    Единственное место, где потолок кругов превращается в действие в трекере;
    зовут его все пути постановки рабочей сессии (вебхук, опрос). Исполнителя
    меняем и оставляем комментарий: без него человек увидит задачу на себе и
    не поймёт, почему агент её не взял. Осечка трекера — в журнал, не наружу:
    сессию мы всё равно не ставим, это решение уже принято.
    """
    # Шапка человеческим языком, как у всех машинных комментариев (DEV-108).
    # Собрана здесь руками, а не через `headline.render`: пакет приёма живёт
    # отдельным процессом и модулей раннера не импортирует (см. `evidence.py`).
    # Форма та же — два-три предложения прозой, без подписей к полям и без
    # машинных слов; разъедется — это увидят `test_webhook.py` и
    # `test_assignments.py`: оба читают первую строку этого комментария.
    body = (f"⛔ Агент отработал по {ident} положенное число заходов ({rounds}) "
            f"и следующий сам не возьмёт — дальше решаешь ты. Нужен ещё один "
            f"круг — назначь агента сам, от имени принимающего: такое "
            f"назначение потолок не считает. Не нужен — задача уже у тебя, "
            f"закрывай или переформулируй; сама собой она дальше не поедет. "
            f"Что было на прошлых заходах, видно лентой в этой же задаче.")
    done = []
    if reviewer_id and hasattr(tracker, "assign"):
        try:
            await asyncio.to_thread(tracker.assign, issue_id, reviewer_id)
            done.append("на принимающего")
        except Exception as e:
            log.warning("%s: круги исчерпаны, а переназначить не вышло: %s", ident, e)
    if hasattr(tracker, "comment"):
        try:
            await asyncio.to_thread(tracker.comment, issue_id, body)
            done.append("комментарий")
        except Exception as e:
            log.warning("%s: круги исчерпаны, а комментарий не записан: %s", ident, e)
    log.warning("%s: круги исчерпаны (%d рабочих заходов) — %s", ident, rounds,
                ", ".join(done) or "трекеру не сказано")
    return ", ".join(done)


def work_task(issue: dict, requested_by_name: str,
              incident_label: str = "", requested_by: str = "") -> dict:
    """Элемент очереди для сессии. Тело задачи раннер дочитывает из трекера
    сам — здесь только то, по чему он её находит и пишет логи.

    Здесь же единственная развилка «дежурный или обычная работа»: лейбл на
    задаче → `kind=incident`, и раннер поднимает сессию по дежурному
    контракту вместо рабочего. Развилка одна на все пути постановки —
    вебхук, кнопку и опрос назначений, — поэтому и живёт в одном месте: без
    этого инцидент, пришедший не тем путём, тихо уезжал обычной работой.

    `requested_by` — id постановщика, если путь постановки его знает. Знает
    только вебхук: в событии есть тот, кто назначил, а в самой задаче трекер
    хранит лишь автора. Пусто не пишем — иначе поле присутствует всегда и
    ничего не значит.
    """
    task = {
        "issue_id": issue.get("id") or "",
        "identifier": issue.get("identifier") or "",
        "title": issue.get("title") or "",
        "url": issue.get("url") or "",
        "requested_by_name": requested_by_name,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    if requested_by:
        task["requested_by"] = requested_by
    if has_incident_label(label_names(issue), incident_label):
        task["kind"] = "incident"
        task["symptom"] = symptom_of(issue)
        # Откуда узнали об инциденте. Для дежурного это чужая фактура, и
        # честный ответ — «пришло задачей в трекере», а не выдуманный алерт.
        task["source"] = (f"{task['identifier'] or '?'} — лейбл "
                          f"«{incident_label}»")
    return task


# Род захода для сессии разрешения конфликта (DEV-109). Слово читают двое:
# раннер — какой контракт собирать (`prompt.build_merge_prompt`), и журнал
# запусков — считать ли это рабочим кругом (`runs.WORK_KINDS`, а его там
# нет). Задача решена и проверена; конфликт ей достался от чужого мержа, и
# отнимать за него круг по `AGENT_MAX_WORK_ROUNDS` не за что.
MERGE_KIND = "merge"


def merge_task(issue: dict, why: str, request: dict | None = None,
               branch: str = "") -> dict:
    """Элемент очереди для сессии разрешения конфликта.

    Отличается от рабочего родом и тремя фактами, которые сессии иначе
    пришлось бы выяснять самой за деньги: почему её подняли, какая ветка не
    вливается и где открытый PR. Ставит его пересмотр стопки
    (`reconcile.Reconciler.restack`) — единственный, кто знает, что задача
    протухла.
    """
    task = work_task(issue, "пересмотр стопки: ветка не вливается")
    # Род перебиваем последним: у протухшей задачи мог остаться лейбл
    # дежурного, и уехать она должна разрешением конфликта, а не инцидентом.
    task["kind"] = MERGE_KIND
    task["conflict"] = str(why or "ветка не вливается в основную")
    if branch:
        task["ref"] = branch
    number = str((request or {}).get("number") or "")
    if number:
        task["mr"] = str((request or {}).get("url") or "") or number
        task["mr_iid"] = number
    target = str((request or {}).get("target_ref") or "")
    if target:
        task["target_ref"] = target
    return task
