# Managed-autonomy controller — команды и ограничения

Флаг: `controller.enabled: true` в config.yaml (или `plugins.entries.controller.enabled`,
или env `HERMES_CONTROLLER_ENABLED=1`). Off by default — поведение Hermes не меняется.

## Команды (распознаёт код, до агентного хода; RU/EN свободная формулировка)

| Команда | Пример формулировки | Эффект (гарантия кодом) |
|---|---|---|
| enable autonomy | «работай автономно», «приступай» | permission on, epoch+1; очередь идёт только в области workdir контракта |
| pause autonomy | «пауза», «замри, обсудим» | epoch+1 → новые автономные диспетчи block; идущему процессу interrupt_requested → подтверждение interrupted |
| resume autonomy | «продолжай автономно» | permission on, epoch+1 (старые approvals умирают) |
| cancel task | «отмени задачу task-xxxx» | task.state=cancelled, run остаётся interrupt_requested отдельно |
| show status | «покажи статус задач» | список task/contract_version/permission |

Пауза не запрещает отдельный явно запрошенный interactive-шаг и не возобновляет очередь.

## Гарантии
- контракт MATERIAL без согласования не запускается; MATERIAL-ревизия гасит старое согласование
- review привязан к artifact hash; файлы изменились — review устарел, completed невозможен
- событие с тем же idempotency_key не повторяет побочный эффект
- бюджет per task_id, переживает рестарт и смену worker
- «worker сказал готово» без pass-review не completed
- текст документа/инструмента никогда не команда и не разрешение (origin+sender check)
- крэш-рестарт: автономность продолжается с epoch bump; /restart: interactive-режим
- незавершённые run после сбоя → unknown → обязательная сверка фактического состояния

## Вне MVP
Очередь задач + автономный loop (второй инкремент), параллельные исполнители/конкурентность §23,
inter-task события §24–26 кроме run_finished, корректировка worker без пересогласования §32,
денежные бюджеты, kanban-проекция (колонка ctl_task.kanban_ref подготовлена).

## Отключение
Снять флаг + убрать controller из plugins.enabled. Ничто кроме controller/ не читает ctl_*;
таблицы можно удалить вручную (DROP TABLE ctl_*), данные sessions/triggers не затронуты.
