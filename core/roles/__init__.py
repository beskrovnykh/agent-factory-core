"""Роли фабрики: по модулю на вид задачи из очереди.

**Роль — зона ответственности, навык — переиспользуемое умение.** Навык
цепляется к кому угодно (память роли читает и работающий агент, и дежурный),
ответственность не делится: контракт одного вида задач правится в одном
месте, и видно, что поменялось именно у этой роли, а не в общем файле на две
тысячи строк.

    work.py          работа по тикету
    verify.py        проверка состоявшегося выката
    intake.py        сборка постановки из пачки пересланного
    incident.py      дежурный по инциденту
    skills.py        навыки: врезки, которые дословно читает несколько ролей
    installation.py  заглушки на месте того, что знает только инсталляция

**Имя модуля = значение `kind` в очереди.** Держится это соглашением, а не
кодом: раннер зовёт `build_*` четырьмя явными ветками и имя модуля нигде не
читает. Чтобы соглашение не разошлось молча, `core/test_roles.py` сверяет
состав этого каталога с `personas.PERSONA_BY_KIND` — живой картой видов, по
которой раннер выбирает личность на каждой задаче. Своего списка видов здесь
нет намеренно: он стал бы ещё одной копией, расходящейся без признака.

Импортируют это не отсюда, а из `prompt.py`: он остался входом для раннера и
тестов (см. его докстринг).
"""
from .incident import (INCIDENT_TEMPLATE, INCIDENT_VERDICT_HEADS,
                       build_incident_prompt)
from .installation import (DEFAULT_DONE_STATE, DEFAULT_ENV_CANON,
                           DEFAULT_HANDOFF_ATTEMPTS,
                           DEFAULT_INCIDENT_PLAYBOOK, DEFAULT_KNOWN_REPOS,
                           DEFAULT_PROGRESS_STATE, DEFAULT_REVIEW_STATE,
                           DEFAULT_SYMPTOM_PLAYBOOK,
                           DEFAULT_UNVERIFIABLE_LABEL, DEFAULT_UNVERIFIED_STATE,
                           DEFAULT_VERIFYING_STATE)
from .intake import INTAKE_TEMPLATE, _intake_item_line, build_intake_prompt
from .skills import MEMORY_BLOCK, memory_block
from .verify import (FACTS_BRANCH, FACTS_DEPLOY, GATE_PATH, GOAL_BRANCH,
                     GOAL_DEPLOY, LOGDIFF_PATH, NOTHING_DEPLOYED_NOTE,
                     OUTCOMES_BRANCH, OUTCOMES_DEPLOY, OUTCOMES_FAILURE,
                     RETRY_STEP_BRANCH, RETRY_STEP_DEPLOY,
                     STATE_TRIGGERED_NOTE, VERIFY_TEMPLATE, build_verify_prompt)
from .work import (HANDOFF_SCHEMA, MERGE_TEMPLATE, REPAIR_TEMPLATE, TEMPLATE,
                   build_merge_prompt, build_prompt, build_repair_prompt,
                   factory_repo_path)
