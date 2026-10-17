# Preempt — дизайн пути step-1 (контракт task-b78849c10d)

Аудит HEAD `2934ca168f` (`preempt`, без коммитов поверх базы на момент аудита).
Каждое утверждение про механизм снабжено цитатой файл:строка из этого HEAD.

## 0. Что уже есть в HEAD — вердикты аудита

### 0.1 `tools/fork_explore_verbs.py` — soft pause/resume/close ✅

API-слой **без хендлеров**, подтверждено:

- глаголы: `pause_branch()` — `tools/fork_explore_verbs.py:93`,
  `resume_branch()` — `:137`, `close_branch()` — `:189`;
- персистентное состояние — таблица `fork_gate` в sqlite
  (`tools/fork_explore_journal.py:78` «PR-2b … persistent per-branch continuation gate»,
  путь `get_hermes_home()/forks/fork_journal.db` — `fork_explore_journal.py:107-108`);
- CAS-переходы: `cas_gate(join_id, ref, _PAUSABLE_STATES, "paused", …)` — `verbs.py:114`;
  групповой sweep одним UPDATE — `verbs.py:123-125`
  (`UPDATE fork_gate SET state='paused' … WHERE join_id=? AND state IN ('running','resumed_pending')`);
- идемпотентность различением, не схлопыванием: `already_paused`/`already_closed`/
  `unknown_branch` — машинные префиксы ошибок (`verbs.py:95-104`, `:202`);
- hard-вариант **вырождается в soft по дизайну** (§VERDICT-2):
  «from outside the owning process an in-flight HTTP step is reachable only through
  the step client's own timeout» — `verbs.py:97-99`; ответ всегда несёт `"variant": "soft"` (`:132`);
- lazy re-drive: resume только ставит очередь, work подхватывает драйвер при следующем
  вызове `fork_explore` — `queued_for_redrive`, `disposition: deferred` (`verbs.py:138-140`);
  механика — `tools/fork_explore_rollout.py:118,168` и `tools/fork_explore_tool.py:181,457`.

⚠ **Опровергнуто/уточнено**: в задании фигурировал путь `agent/control_socket.py` —
фактически это `gateway/control_socket.py`. Существенной разницы нет, но ссылки ниже — на фактический путь.

### 0.2 `/att` — человек-половина мягкого преэмпа тёрна (PR#40) ✅

- регистрация: `hermes_cli/commands.py:130-132` — `CommandDef("att", …, busy_policy="interrupt_then_dispatch", busy_handler="att")`;
  `gateway_only=True` (проверяет `tests/gateway/test_att_command.py::TestAttCommandDef`);
- busy-хендлер: `gateway/run_busy.py:1031` `_busy_att_command` — «human soft pause: freeze the
  live turn at its next safe boundary… NOT a hard cancel», дергает `running.interrupt(note or None)`
  (`run_busy.py:1044`) — мягко, без `hard_cancel`; idle-путь отвечает «нечего морозить» (`:992`);
- контракт зафиксирован тестом: `calls == [("wrong repo", {})]` — «soft: no hard_cancel kwargs»
  (`tests/gateway/test_att_command.py::test_soft_interrupts_running_agent_and_keeps_session`).

### 0.3 Control-socket шлюза: транспорт, права, аутентификация

Файл `gateway/control_socket.py`:

- протокол v1, одна JSON-строка на соединение; кэпы `_MAX_REQUEST_BYTES = 64 KiB`,
  `_MAX_RESPONSE_BYTES = 512 KiB` (`control_socket.py:20-21`);
- реестр глаголов — dict в конструкторе: `self._handlers = {"identify": …, "status": …,
  **(verb_handlers or {})}` (`:144-145`); диспетчеризация в `handle_request_line`
  (`:236+`): неизвестный глагол → ошибка + `supported_verbs`; хендлер с `params` получает dict параметров;
- **аутентификации нет — только файловая система**: сокет биндится с umask `0o177` и
  `chmod 0o600` (`:155-162`, комментарий «Restrictive umask so the socket is never world-connectable»);
  Windows — именованный pipe per-HERMES_HOME (`windows_pipe_name`, `:30-32`);
  клиентский API — `query_gateway_control(home, verb, params=…)` (`:216+`).

Вывод для дока: право слать = локальный доступ на запись в HERMES_HOME того же UID.
Отправитель **не проверяется** — в запросе `{id, verb, params}` поля отправителя нет вообще.
Это ключевой факт для §2 (атрибуция).

Сейчас зарегистрированы только профильные/апдейтные глаголы:
`gateway/run.py:5603-5612` — `pause-for-update`, `rescan-profiles`, `unserve-profile`,
`serve-profile`, `migrate-profile-identity`, `purge-profile-identity`, `reload-plugins`.
**Ни fork-\*, ни сессионных глаголов в HEAD не подключено** — grep `fork_explore_verbs|pause_branch`
по `gateway/`, `hermes_cli/` пуст. Заготовка точки подключения: комментарий `verbs.py:4`
«The gateway wires them into ``verb_handlers=``».

### 0.4 Флаг-механика прерывания: `agent/interrupt_control.py`

- `agent.interrupt(message=None, *, hard_cancel=False, tool_reason=None,
  require_generation=None) -> bool` — `interrupt_control.py:128-131`; публикация состояния:
  `_publish_interrupt_state` ставит `_interrupt_requested/_interrupt_message/_tool_interrupt_reason` (`:157-160`);
- per-tid сигнал: `_set_interrupt(True, self._execution_thread_id, reason=…)` (`:210`),
  fan-out на concurrent-tool worker'ы `_ic_signal_tool_workers` → `_set_interrupt(active, tid)`
  (`:106-122`) и children (`:217-219`);
- сброс: `clear_interrupt(preserve_redirect=False, hard_cancel=False)` (`:238-258`);
- **сессионные живые инъекции уже есть внутри процесса**: `steer(text)`
  («Queue user text for delivery as its own user row after the current tool batch finishes», `:267-269`)
  и `redirect(text)` (`:280`). Дренаж — `turn_iteration_prep.py:179-183` (инъекция после последнего
  tool result как user-строка, с комментарием о сохранении prompt-cache) и
  `agent/turn_finalizer.py:747`.

### 0.5 Где тёрн проверяет флаги (safe boundaries)

- между шагами: `agent/turn_iteration_prep.py:382` — `if agent._interrupt_requested:` →
  `_verdict("break")` c exit-reason `interrupted_by_user` / `interrupted_by_system(<issuer>)` (`:383-389`);
  тот же чек в начале итерации — `:54`;
- причина атрибутируется: `USER_INTERRUPT_REASONS` (hard stop / new message / user interrupt) —
  `interrupt_control.py:19-23`; всё остальное — системный производитель:
  `interrupt_issuer(agent)` возвращает slug или `None` для человек-стопа (`:27-33`);
  exit-reason формируется как `interrupted_during_api_call(<issuer>)` (`:48-51`);
- посреди tool-call паузы не происходит: soft-интерапт останавливает тёрн **после** текущей
  партии тулов на следующей итерации цикла (чек `:382` выполняется между итерациями);
  единственное воздействие на идущий HTTP-step — через собственный таймаут клиента (§0.1, §VERDICT-2).

### 0.6 Scheduler / ctl_* (controller)

Модель — `controller/model.py`:

- события уже несут источник: `emit(conn, *, kind, origin, task_id=None, payload=None,
  idempotency_key=None)` — origin пишется в `ctl_event` (`model.py:200-216`);
- статусы run: `{starting, running, interrupt_requested, interrupted, finished, failed,
  unknown}` (`:232-233`) — `paused`/`resumed_pending` для ctl_run ещё нет;
- шаги: insert со статусом `'pending'` (`add_step`, `:263-267`), перевод `finish_step(status)`
  (`:273-275`). Статусы paused/resumed у ctl_step отсутствуют — их добавляет PR-B (§4).

### 0.7 Лazy re-drive как паттерн для сессий

Паттерн «resume ставит строку в очереди, драйвер подхватывает на следующем естественном
проходе» реализован для форков (`verbs.py:137-140` → `tool.py:181` `drive_paused_forks`
интент, intake пар `(join_id, branch_ref)` — `fork_explore_journal.py:386`). Для сессий
аналог — re-drive входящего сообщения/очереди шлюза, а не продолжение тёрна в моменте.

---

## 1. PR-A: глаголы на сокете

Точка монтирования одна: dict `verb_handlers=` в `GatewayControlServer(...)` (`gateway/run.py:5603`).
Wire-формат fork-глаголов задан STITCH-DESIGN-8012 §3.2/§3.3 (`verbs.py:4`):

```
→ {"id":"c1","verb":"fork.pause","params":{"join_id":"…","ref":"","variant":"soft"}}
← {"ok":true,"result":{"join_id","ref","stopped_inflight","marked_paused","variant":"soft"}}
→ {"verb":"fork.resume","params":{"join_id","ref"}}      ← resumed_inflight, queued_for_redrive, disposition:"deferred"
→ {"verb":"fork.close","params":{"join_id","ref","reason"}}
ошибки: "unknown_branch: …", "already_paused: …", "already_closed: …"  (verbs.py:95-104,202)
```

Реализация — тонкие обёртки: handler вызывает `pause_branch/resume_branch/close_branch`
как есть; никакой второй семантики не заводить (инвариант `tool.py:229`: «the future
control-socket verb … is just request_stop() below — no second mechanism»).

### 1.1 Сессионные глаголы — проект wire-формата

```
→ {"verb":"interrupt-session","params":{"session_key":"<quick_key>","note":"","origin":"scheduler|agent:<name>"}}
← {"ok":true,"result":{"accepted":true,"mode":"grace_turn","boundary":"next_safe_boundary"}}

→ {"verb":"steer-session","params":{"session_key":"…","text":"…","origin":"…"}}
← {"ok":true,"result":{"accepted":true,"delivery":"active_turn_steer"| "queued_next_turn"}}
ошибки: "no_running_agent: …" (= семантика /att idle-ответа), "empty_text: …"
```

Семантика `interrupt-session` **= /att дословно**: soft-interrupt живого агента
(`run_busy.py:1031-1051`), история и pending-steer выживают, сессия остаётся resumable,
решение continue-or-stop — за следующим сообщением оператора. Реализация: переиспользовать
`_busy_att_command` как общий код, machine-sender передаёт note и origin вместо event-платформы.

Семантика `steer-session` = `/steer` busy-путь (`run_busy.py:1096+` → `agent.steer()`,
дренаж `turn_iteration_prep.py:179`). Это **живая инъекция между tool-call итерациями,
механика её позволяет** — она уже существует и кэш-безопасна (user-строка после последнего
tool result, append-only персист). Ограничение, которое док фиксирует честно: инъекция
возможна только в процессе-владельце тёрна. Поэтому и `interrupt-session`, и `steer-session`
реализуются **внутри гейтвея-владельца сессии** (тот же процесс держит control socket и
session state); если глагол приходит мультиплексору на чужом профиле — ответ
`{"ok":false,"error":"not_owner: <profile>"}`, без проксирования. Кросс-процессный
re-drive идёт через очередь сообщений платформы (lazy-паттерн §0.7), не через socket.

Оба глагола должны появиться в `supported_verbs` автоматически (dict `:145`) — отдельной
регистрации схемы нет, валидация — брошенные `ValueError`/`_err`-ответы хендлеров.

## 2. Системная атрибуция

Проблема: wire-запрос не несёт отправителя (`§0.3`), а журнал тёрна отличает человека от
системы только через `USER_INTERRUPT_REASONS` (`interrupt_control.py:19-33`).

Требования к полю `origin`:

1. **Ctl-журнал уже готов**: `ctl_event.origin` (`controller/model.py:200-216`) — писать туда
   `human:<platform>:<user>` vs `scheduler` vs `agent:<name>` vs `socket:<uid>`. Ничего нового
   в схеме не нужно.
2. **Fork journal**: добавить колонку `origin` в запись перехода `fork_gate` (вместе с
   `reason='human'` сегодня — `verbs.py:115,123`); дефолт `'human'` сохраняет совместимость
   с существующими строками.
3. **Interrupt-причина**: системные senders обязаны передавать `tool_reason=<slug>` в
   `interrupt()` (механика есть, `:128-131`); тогда exit-reason тёрна автоматически становится
   `interrupted_by_system(<issuer>)` (`turn_iteration_prep.py:386-388`). Для
   `interrupt-session` slug — `preempt_socket(origin=…)`.
4. Wire-уровень: поле `params.origin` advisory (сокет не аутентифицирует — см. §0.3),
   доверять ему можно как журнальному комментарию, но не как authorization-факту.

## 3. PR-B: контур контроллера (только граница)

Проект границы, без реализации:

- Контроллер **знает**: статус шага `paused`/`resumed_pending` в ctl_step (новые значения,
  модель `finish_step` `model.py:273-275` остаётся единственной точкой записи); события
  `step.paused`/`step.resumed` через `emit` с origin (§2); handoff-заметку — свободный текст,
  который контроллер кладёт в payload события и показывает следующему исполнителю.
- Контроллер **не знает**: внутреннего состояния тёрна (нет чтения `_interrupt_requested`,
  prompt-cache, очереди steer); он видит только факты границ шага, уже записанные в ctl_*.
  Никаких управляющих решений контроллер в моменте тёрна не принимает — resume-решение
  выражается новым запуском шага, что повторяет lazy re-drive, а не противоречит ему.
- Инвариант смешения: ctl_step.paused может сосуществовать с ctl_run.interrupted
  (статусы отдельных сущностей раздельные по построению — header `controller/model.py:7`).

## 4. Границы безопасности

- Чужой процесс не может hard-abort чужие HTTP-клиенты: подтверждено — hard-вариант
  degrades to soft, «an in-flight HTTP step is reachable only through the step client's
  own timeout» (`verbs.py:97-99`, §VERDICT-2). Хуже, чем мягкая просьба, внешний мир сделать не может.
- Кто валидирует глаголы: сервер гейтвея — единственная точка; `_handlers.get(verb)`
  (`control_socket.py:239`) + try/except вокруг хендлера (`handle_request_line`),
  т.е. кривая нагрузка не роняет gateway, а отвечает ошибкой. Валидация аргументов
  (`join_id` существование, variant ∈ {soft,hard} — `verbs.py:100-104`) — в хендлерах, там же,
  где уже живут машинные error-коды.
- Доступ к сокету = доступ на уровне OS (`0600`, §0.3). Следствие для дока: глаголы
  предваряются проверкой «этот сокет обслуживает этот профиль» (`resolve_client_socket_path(home)`
  привязан к HERMES_HOME, `control_socket.py:62+`), кросс-профильные команды через чужой сокет невозможны.
- Межпроцессное влияние остаётся в пределах DB-CAS (fork_gate) и очереди сообщений
  платформы — оба уже audit-able, в отличие от shared-memory флагов.

## 5. Dogfood-план (живой гейтвей того же хоста)

Предусловие: ветка `preempt`, свой HERMES_HOME, гейтвей запущен (`hermes gateway start` либо штатный запуск).

1. Форк-глаголы:
   ```bash
   # уронить/поднять конкретную ветку живого форка (join_id из последнего журнала)
   JOIN=$(sqlite3 ~/.hermes/forks/fork_journal.db "SELECT join_id FROM fork_run ORDER BY started_at DESC LIMIT 1")
   echo '{"id":"df1","verb":"fork.pause","params":{"join_id":"'$JOIN'"}}' \
     | socat - UNIX-CONNECT:"$(ls ~/.hermes/gateway.sock 2>/dev/null || python -c \
       'from gateway.control_socket import resolve_client_socket_path; from pathlib import Path; import sys; print(resolve_client_socket_path(Path.home()/".hermes"))')"
   # подтверждение: marked_paused>0 в ответе; вторая строка в sqlite:
   sqlite3 ~/.hermes/forks/fork_journal.db "SELECT ref,state,updated_at FROM fork_gate WHERE join_id='$JOIN'"
   ```
   Затем `fork.resume` (ожидаем `queued_for_redrive`, `disposition:"deferred"`) и убедиться,
   что следующий вызов `fork_explore` дол recuperацию веток (grep лога гейтвея на lazy re-drive).
2. Сессионный graceful-pause (после PR-A): отправить боту обычное сообщение, пока тёрн бежит;
   из другого терминала послать `interrupt-session` c `note="dogfood-$(date +%s)"`; проверить:
   - ответ глагола `accepted:true`;
   - следующее сообщение бота начинается с продолжения, а не с новой задачи (история цела);
   - в логе гейтвея строка `ATT for session …` / exit-reason `interrupted_by_system(preempt_socket…)`.
3. Steer-инъекция: во время долгого tool-batch послать `steer-session`; подтвердить
   появление текста как user-строки ПОСЛЕ последнего tool result (читать лог тёрна),
   и отсутствие прерывания выполнения.
4. Регрессия на кэш: убедиться, что после обоих глаголов следующий запрос собрался без
   mutation прошлых сообщений (prompt-cache hits на сервере :8012 в метриках/логе).

## 6. Открытые вопросы Архитектору

1. Является ли `interrupt-session` одним глаголом или парами `pause-session`/`resume-session`?
   В текущей семантике /att resume не явный — его делает следующее сообщение оператора.
2. Право на steer-инъекцию межпроцессных агентов: считать ли `origin=agent:*` полномочием
   равным человеку, или он ходит по той же очереди, что и текст? (Влияет на §2.)
3. Нужно ли controller'у жить в том же процессе, что и control socket, чтобы читать
   session-глаголы, или контроллер общается с гейтвеем только через ctl_event? (PR-B.)
4. Политика retention `fork_gate`-строк после `close` (терминальные абсорбируют всё —
   `verbs.py:34-35`) — сколько хранить paused/crashed-артефакты?
5. Должен ли `steer-session` быть ограничен rps/размером на уровне сокета отдельно от
   upstream-лимитов платформ?

## Приложение A. Grep-подтверждённые цитаты

| Утверждение | Цитата |
|---|---|
| Fork verbs сигнатуры | `tools/fork_explore_verbs.py:93,137,189` |
| CAS/group UPDATE при паузе | `tools/fork_explore_verbs.py:114,123-125` |
| Hard→soft degradация | `tools/fork_explore_verbs.py:97-99` |
| Lazy re-drive заготовка | `tools/fork_explore_verbs.py:138-140`; `tools/fork_explore_tool.py:181,457`; `tools/fork_explore_rollout.py:118,168` |
| /att контракт | `hermes_cli/commands.py:130-132`; `gateway/run_busy.py:1031-1051` |
| Interrupt ABI + tool_reason | `agent/interrupt_control.py:128-131,157-160` |
| Per-tid флаг + worker fan-out | `agent/interrupt_control.py:106-122,210` |
| steer/redirect внутри процесса | `agent/interrupt_control.py:267-269,280`; `agent/turn_iteration_prep.py:179-183` |
| Границы тёрна чекают флаг | `agent/turn_iteration_prep.py:54,382-389`; `agent/turn_finalizer.py:747,752-753` |
| Human-vs-system атрибуция | `agent/interrupt_control.py:19-33,48-51` |
| ctl_event.origin | `controller/model.py:200-216` |
| RUN_STATUSES / ctl_step pending | `controller/model.py:232-233,263-275` |
| Control socket transport/security | `gateway/control_socket.py:20-21,30-32,144-145,181-186,236+` |
| Текущий список глаголов (без fork./session.) | `gateway/run.py:5603-5612` |
| fork_gate persistent | `tools/fork_explore_journal.py:78,107-108,272` |
| ContinueFlag единый примитив | `tools/fork_explore_rollout.py:38-59`; `tools/fork_explore_tool.py:225-247` |

## Приложение B. Не удалось проверить

1. Записи `ctl_event` с origin кроме одной найденной пары `kind="agent_block"/origin="agent"`
   (одиночный вызов в head других emit-callsites выводится переменными, а не литералами) —
   реальный разброс значений origin в проде не измерялся.
2. Наличие отдельного ledger записи для `/att`: найден только `logger.info("ATT for session %s")`
   (`run_busy.py:1049`) и exit-reason в тёрне; перманентного journal-роу именно для att не обнаружено.
3. Поведение Windows named-pipe транспорта под нагрузкой (код `# pragma: no cover - wine2e lane`,
   `control_socket.py`); dogfood §5 выполнен только для POSIX-сокета.
4. Формулировки STITCH-DESIGN-8012 §3.2/§3.3/§5 целиком: документ-источник вне репо
   (есть только ссылки в коде: `verbs.py:4`, `rollout.py:118`, `journal.py:78,272,386`);
   соответствие wire-формата сверено с тестами `tests/tools/test_fork_explore_verbs.py`.

---

## 7. Реализация PR-A + PR-B (итог сшивки)

История исполнения (важна для чтения веток): контракт исполнялся ПАРАЛЛЕЛЬНО двумя
руками — PoC-рука дала wire-канон (root-origin, fork-обёртки), pump-рука (у неё
task ушёл в `running`, pump сам выдал шаги) дала более глубокую механику глаголов
(handoff-снимок активности, sidecar-стейджинг, fan-out детям) + PR-B интеграцию
контроллера. Сшивка: база — pump-механика, поверх — канон root-origin. Файлы PoC
(`gateway/session_verbs.py`, `gateway/run_control_socket.py`) в итоге не живут.

Что в дереве:

1. **Сессионные глаголы** — `gateway/run_session_verbs.py`
   (`interrupt_session_verb`/`steer_session_verb`), монтирование через
   `preempt_verb_handlers(runner)` (`gateway/run_control_handlers.py`) в
   `GatewayControlServer(verb_handlers=..., origin_required_verbs=...)`.
   - `interrupt-session {session, reason?, message?, hard?}` → soft по умолчанию,
     handoff-заметка строится из activity-снимка (`_last_activity_desc`,
     `_current_tool`) и СТЕЙДИТСЯ в pending sidecar-notice следующего хода;
     idle → `no-running-turn`, нечёткий substring → `ambiguous_session`.
     Явного resume нет — resume делает следующее сообщение оператора.
   - `steer-session {session, message}` → `agent.steer()`, ТОТ ЖЕ pending-steer
     inbox что у человека; машинный ход помечается видимой меткой
     `[from <origin>]`. Нет живого хода → `accepted:false, reason:"no-running-turn"`
     (не ошибка) — scheduler сам решает очередь.
2. **Origin на wire-корне (канон Архитектора).** Глаголы из
   `origin_required_verbs` обязаны нести непустой root-`origin`
   (`cli | human:<platform> | agent:<name>`); пустой/кривой — `missing_origin` /
   `unknown_origin` ДО любого хендлера; дефолтного sender'а нет; params-level
   origin root не переопределяется и не удовлетворяет требованию. Валидированный
   origin инжектится в `params`; `query_gateway_control(..., origin=...)` шлёт его
   в корне. Остальные глаголы (identify/pause-for-update/…) — исторический wire без origin.
3. **Атрибуция.** Дефолтный `tool_reason` глагола именует отправителя
   (`preempted_by_agent_scheduler`); эксплицитный reason и in-process вызовы без
   origin держат legacy-дефолт `controller_preemption`. machine-путь никогда не
   попадает в `USER_INTERRUPT_REASONS` (инвариант #112647).
4. **Fork-глаголы** — `gateway/fork_control_verbs.py`: тонкие обёртки над
   `tools/fork_explore_verbs` (hard вырожден в soft, §VERDICT-2), тоже root-origin.
   Попутно починен NULL-баг `_known_branch` (PR#42): `NULL + 1` в SQL
   лишал gate-only форк распознавания — теперь COALESCE на обеих ветках.
5. **PR-B (контроллер).** `controller/model.py`: CAS `pause_step`/`resume_step`
   (in_progress↔paused, машинные коды) + `request_preempt` (ctl_event only — прямого
   linking процессов controller↔gateway нет, граница обратимая); cooperative worker
   cut + RESUME re-anchoring в briefs. Хендлеры глаголов controller НЕ импортируют.
6. **Fallback старого гейтвея.** До-PR-A гейтвей отвечает `unknown verb` + список;
   `query_gateway_control` сворачивает `ok:false` в `None` — вызывающий держится
   чат-пути (контракт `pause-for-update`).
7. **Чего нет (честно):** dogfood на живом гейтвее (план §5) и Windows-полоса
   named-pipe под нагрузкой — следующий шаг после мержа.

Тесты: `tests/gateway/test_control_socket_session_verbs.py` (хендлер-механика:
атрибуция preempt≠user-stop, handoff staging, fan-out, real-socket round-trip,
fallback-контракты), `tests/gateway/test_control_socket_preempt_verbs.py`
(wire-канон: root-origin валидация до хендлеров, `[from <origin>]` метка,
default tool_reason по origin, supported_verbs из пяти, fork pause/resume через
CAS-хаб), `tests/plugins/test_controller_step_pause.py` + `test_controller_preempt.py`
(CAS round-trip, worker cut, ctl_event-атрибуция).
