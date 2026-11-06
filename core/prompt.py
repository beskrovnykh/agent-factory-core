"""Вход в контракты сессий: сами контракты лежат по ролям в `roles/`.

Раньше все четыре контракта жили здесь одним файлом на две тысячи строк.
Разложены по модулям на DEV-13: роль — зона ответственности, и правка
контракта одной роли не должна открывать файл, в котором лежат остальные три.
Что считается ролью, что навыком, а что скилом, написано в `roles/__init__.py`
и в `CLAUDE.md`.

Этот файл остался входом, и не из вежливости к истории: имена отсюда
импортируют раннер (`from prompt import ...`) и тесты, а раннер у каждой
инсталляции обновляется целиком и отдельно от приёма. Перенести точку входа
значило бы уронить сессии на импорте у всех, кто ещё не обновился.

Состав экспорта закреплён `core/test_roles.py`: он сверяет `__all__` с тем,
что правда импортируют `runner.py` и тесты, и в обе стороны — имя в `__all__`,
которого фасад не отдаёт, ломает `import *` молча.
"""
from roles import (DEFAULT_DONE_STATE, DEFAULT_ENV_CANON,
                   DEFAULT_HANDOFF_ATTEMPTS, DEFAULT_INCIDENT_PLAYBOOK,
                   DEFAULT_KNOWN_REPOS, DEFAULT_PROGRESS_STATE,
                   DEFAULT_REVIEW_STATE, DEFAULT_SYMPTOM_PLAYBOOK,
                   DEFAULT_UNVERIFIABLE_LABEL, DEFAULT_UNVERIFIED_STATE,
                   DEFAULT_VERIFYING_STATE, FACTS_BRANCH, FACTS_DEPLOY,
                   GATE_PATH, GOAL_BRANCH, GOAL_DEPLOY, HANDOFF_SCHEMA,
                   INCIDENT_TEMPLATE, INCIDENT_VERDICT_HEADS,
                   INTAKE_TEMPLATE, LOGDIFF_PATH, MERGE_TEMPLATE,
                   MEMORY_BLOCK, NOTHING_DEPLOYED_NOTE, OUTCOMES_BRANCH,
                   OUTCOMES_DEPLOY, OUTCOMES_FAILURE, REPAIR_TEMPLATE,
                   RETRY_STEP_BRANCH, RETRY_STEP_DEPLOY, STATE_TRIGGERED_NOTE,
                   TEMPLATE, VERIFY_TEMPLATE, _intake_item_line,
                   build_incident_prompt, build_intake_prompt,
                   build_merge_prompt, build_prompt,
                   build_repair_prompt, build_verify_prompt, factory_repo_path,
                   memory_block)

__all__ = [
    "DEFAULT_DONE_STATE", "DEFAULT_ENV_CANON", "DEFAULT_HANDOFF_ATTEMPTS",
    "DEFAULT_INCIDENT_PLAYBOOK", "DEFAULT_KNOWN_REPOS",
    "DEFAULT_PROGRESS_STATE", "DEFAULT_REVIEW_STATE",
    "DEFAULT_SYMPTOM_PLAYBOOK", "DEFAULT_UNVERIFIABLE_LABEL",
    "DEFAULT_UNVERIFIED_STATE", "DEFAULT_VERIFYING_STATE", "FACTS_BRANCH",
    "FACTS_DEPLOY", "GATE_PATH", "GOAL_BRANCH", "GOAL_DEPLOY",
    "HANDOFF_SCHEMA", "INCIDENT_TEMPLATE", "INCIDENT_VERDICT_HEADS",
    "INTAKE_TEMPLATE", "LOGDIFF_PATH", "MERGE_TEMPLATE",
    "MEMORY_BLOCK", "NOTHING_DEPLOYED_NOTE", "OUTCOMES_BRANCH",
    "OUTCOMES_DEPLOY", "OUTCOMES_FAILURE", "REPAIR_TEMPLATE",
    "RETRY_STEP_BRANCH", "RETRY_STEP_DEPLOY", "STATE_TRIGGERED_NOTE",
    "TEMPLATE", "VERIFY_TEMPLATE", "_intake_item_line",
    "build_incident_prompt", "build_intake_prompt", "build_merge_prompt",
    "build_prompt",
    "build_repair_prompt", "build_verify_prompt", "factory_repo_path",
    "memory_block",
]
