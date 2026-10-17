# fork_explore — design (PR-1)

Fork-reasoning на развилке внутри одной сессии: контролируемые dry-роллауты
траекторий (куда повернёт работа), а не ответов. Единица форка — ТРАЕКТОРИЯ.

## Pipeline

1. **Fan-out**: `fork_explore(paths=[A,B,...])` строит побайтово одинаковый
   shared prefix (`tools/fork_explore_prefix.py`) + разный branch suffix.
   `prefix_hash` журналируется. k ≤ k_max (4), near-duplicate стратегий
   дедуплицируются до запуска.
2. **Branch rollout** (`run_branch`): 1..depth модельных шагов вперёд, формат
   строго JSON (kind/content/intents/reads/risks/infeasible). Read-only по
   построению: у rollout-кода нет write-путей вообще; единственная «запись» —
   `read_file` из `read_roots` (`_safe_read`, realpath, `..` не выходит).
   Желания записывать — только `intents`; необратимые → `needs_approval`.
   Ранняя остановка: `infeasible`, `deadline`, `stop`.
3. **Жёсткие гейты** (`hard_gates`, детерминированные, до судьи): schema_ok,
   feasible (не infeasible/deadline), intents_ok (нет запрещённых инструментов,
   имя инструмента — безопасный токен).
4. **Слепой сравнительный судья** (`judge_pair`): swap-pairwise — каждая пара
   судится дважды с переставленными позициями; несогласованный вердикт = tie
   (позиционный biasNeutralized). Счёт = wins/denominator; победитель обязан
   дать margin ≥ `min_margin_delta` (0.34), иначе `select_with_caveat`.
   Судья деградирует → `select_with_caveat` + open_issue, не падение.
5. **Синтез пути В** (`maybe_synthesize`): оркестратор видит траектории и
   слабые места A/Б, может предложить новый путь; В проходит те же гейты и
   тот же судейский матч против текущего лидера, прежде чем стать победителем
   (`strategy="synthesize"`).
6. **JoinPacket ≤ `join_max_tokens` (1500)** в главную контекст:
   направление + key_findings/risks каждой ветки + open_issues + intents
   (дедуп winner-first, idempotency_key sha256). Санайtiles
   (`fork_explore_guard.py`): template-токены вырезаны, императивы
   «забудь правила» neutralized, chain-of-thought не попадает. Полная трасса —
   только в журнале.
7. **Fork journal** (`fork_explore_journal.py`): sqlite
   `get_hermes_home()/forks/fork_journal.db` (profile-aware, HX001).
   Пишется: branches full-traces, margin, score, judge_degraded, plus shadow
   метрики `fork_rate` / `win_over_default` / `wasted_tokens` (PR-3 боёвка
   калибруется по ним; бой — только после калибровки). Retention 200 прогонов.
   `session_tokens_used()` обслуживает бюджет `session_token_budget` (24h):
   вилка, пересекающая бюджет, отвергается до первого чата.

## Путь к cache-fork (SGLang RadixAttention)

- Layout уже идеален для prefix-cache: общий префикс байт-в-байт одинаков во
  всех ветках (стабильность — тест `test_prefix_byte_identical_across_branches_and_calls`),
  ветки различаются только последним user-сообщением (суффиксом). При общем
  сервере первый запрос греет RadixTree, остальные k-1 попаданий — prefix hit.
- Phase-B (не в PR-1): `s.fork(k)`/n-параметр SGLang (:8001/:8012 — SGLang) —
  серверный форк KV-пула вместо k полных префиллов; `cache_fork_mode` в конфиге
  зарезервирован. Поведение cancel (дедлайн ветки = обрыв клиента, сервер
  освобождает батч-слот) проверить load-тестом против :8012 перед включением.
- Пересечение с ctl/task-b78849c10d: rollout-ветки — приостанавливаемые тёрны;
  PR-1 держит их как обычные side-task чаты (task="fork_explore"), интеграция
  с pause-механикой — PR-2+.

## Конфиг

DEFAULT_CONFIG `fork_explore:` (единственный источник дефолтов): enabled,
k_max=4, depth_default=2/depth_max=3, tokens_per_step_max=1000, temperature=0.7,
fork_deadline_s=90, session_token_budget=120000, min_margin_delta=0.34,
join_max_tokens=1500, synthesize=true, forbidden_tools=[terminal, write_file, patch],
read_roots=[], branch_endpoint{}, judge{} (флот пинит :8012; ключ FORK_JUDGE_API_KEY
в OPTIONAL_ENV_VARS).

## Tool surface

Имя `fork_explore`, toolset `sessions`, registration в `tools/fork_explore_tool.py`
(check_fn = enabled-флаг). Принимает paths как строки ИЛИ объекты
`{"ref","strategy"}` — LLM часто сериализует массивы объектов в строки.

## Маппинг на механизмы репо (что переиспользуем, что нового)

| Механизм | Для чего | В fork-explore |
|---|---|---|
| session `/fork`+`/join` (PR#6) | ветвление КОНТЕКСТА/диалога из точки, возврат ветки | НЕ трогаем; fork-explore — ветвление РАССУЖДЕНИЯ внутри одного тёрна, без новых сессий |
| `delegate_task` | разделение РАБОТЫ: тяжёлые субагенты, все результаты нужны | остаётся как есть; единица fork-explore — разделение СОМНЕНИЯ (tournament траекторий, живёт победитель) |
| `agent/interrupt_control.py` | мягкие паузы/прерывания тёрнов | переиспользуем для приостанавливаемости rollout-веток (сшивка с task-b78849c10d) |
| judge-эндпоинт `:8012` (glm-5.3-flash-libert, SGLang) | слепой судья | готов; другой модель против веток — само-preference риск снижен сменой модели |
| controller (ctl_*) | бюджеты, контракты, журналы | fork journal параллелен ctl-журналам; shadow-метрики читаются тем же read-путём |

## Не берём из SPEC.md (вердикт донора)

- SGLang frontend DSL-оркестрация (`s.fork(k)` как управляющий слой) — фаза-B, сейчас только OpenAI-совместимые вызовы;
- race/hedge роутер между эндпоинтами — нет необходимости при одном пуле :8001/:8012;
- production JoinGuard-транзакт (LOCK/VERSION/CONFLICT) — JoinPacket-lite с sanitize достаточно для dry-роллаутов, ветки не пишут;
- model-fork по гетерогенным бэкендам на старте — ninfer :8093 зажат max_model_len=8192, не тянет большой общий контекст (constraint, не блок);
- regression-калибровка весов судьи (self-preference) — после появления журнала.

## Модель продолжения: пошаговая пауза (директива Архитектора 17.10)

Фиксированная глубина — не семантика, а потолок. Ветка шагает: выполнила
шаг → стоит на границе → ждёт указание «продолжай следующий шаг» или
«прервись». Следствия:

- меж-шаговый гейт видит промежуточную траекторию: бесперспективные ветки
  отсекаются ДО траты следующего prefill+decode, перспективные углубляются
  избирательно (глубина становится динамической, `depth_max` — только потолок);
- реализация НЕ зависит от внешнего преэмпта: пауза живёт внутри `run_branch`
  (следующий запрос не отправляется; состояние ветки — checkpoint в fork
  journal: trace, бюджет, step_index), возобновление — per-branch flag.
  Внешний преэмпт (task-b78849c10d) позже ложится сверху как control-socket
  verb «не планировать следующий шаг» — сходится с тем же примитивом;
- в PR-1 `run_branch` крутил всю глубину циклом (непрерывный роллаут) —
  перенос на пошаговую паузу = PR-2, контракт v4: `tools/fork_explore_rollout.py`
  (`BranchRoller` + `inter_step_gate` + `ContinueFlag`). Реализовано:
  * граница шага = точка, в которой следующий chat-запрос ЕЩЁ не отправлен;
    `inter_step_gate` (детерминированный, дешёвый: bad_json/infeasible/
    forbidden_intent/fact_conflict/no_progress/deadline/token_cap/depth_cap)
    плюс план-vs-потолок: сверх `depth_default` ветка углубляется только при
    новизне последнего шага (≥0.5), иначе `selective_halt`;
  * `depth` в аргументе инструмента — потолок этого форка (зажимается к
    `depth_max`), ветка вправе встать раньше на любой границе;
  * каждая граница пишет checkpoint (trace/бюджет/step_index/cont_state) в
    `fork_journal.db`, таблица `fork_checkpoint` — ветка, отсечённая до join,
    остаётся в журнале полностью; журнал ведётся и в shadow-режиме.

### Сшивка с внешним преэмтом (task-b78849c10d) — предложение

Единый примитив — per-branch `ContinueFlag` (уже живой в `run_branch`),
внешний контроль — только thin verb поверх него, второго механизма нет:

1. `fork_explore_tool._ACTIVE_FORK_FLAGS[join_id][ref]` — реестр флагов
   живых форков процесса; `request_stop(join_id, ref="")` — единственная
   точка входа преэмпта (возвращает число реально переведённых in-stop).
2. Control-socket verb `fork.pause <join_id> [ref]` (и `fork.resume`) —
   диспетчер гейтвея дергает `request_stop`/`flag.resume()`; ветка реагирует
   на СВОЕЙ следующей границе (stop_reason=`paused_by_request`), не посреди
   decode — инвариант «cancel = обрыв клиента» для уже идущего запроса
   сохранён (load-тест PASS: слот освобождается за ~0.5 с).
3. Возобновление после паузы — по чекпоинту из `fork_checkpoint`
   (trace/step_index/бюджет): перестроить `messages` = prefix + suffix +
   сохранённые шаги, продолжить цикл с флагом resume. Кэш-фазу SGLang это
   только выигрывает: граница шага есть готовая точка переиспользования
   префикса.

## Тени и боёвка

Shadow-метрики за ОТДЕЛЬНЫМ `fork_explore.shadow_only: true` (дефолт):
гейт журналит решение, боевого fan-out'а нет. Боёвка — только после
калибровки по журналу (fork_rate / win_over_default / wasted_tokens).

## Статус / dogfood (PR-2, 17.10)

Scoring-конвейер PR-2 активен: hard gates (schema/feasible/intents_ok/facts_ok)
→ кластеризация траекторий (rep судится один раз, размер кластера = consensus)
→ слепой swap-pairwise судья (:8012) → агрегат S (judge 0.70 / consensus 0.25 /
cost 0.05, logprob L_i=0 — OpenAI-совместимый surface его не отдаёт) → margin
`min_margin_delta`; synthesize-путь В проходит те же гейты и тот же журнал.
Разбивка балла пишется в `fork_branch.score_components_json`;
`shadow_stats()` считает fork_rate / win_over_default / wasted_tokens /
gate_reject_rate (окно — аргумент).

Live-контракт SGLang :8012 (проверено вживую):
- cancel = обрыв клиента: load-тест `scripts/fork_cancel_loadtest.py` PASS,
  батч-слоты освободились за 526 мс при k=6, очереди дренируются;
- thinking-модели списывают reasoning_tokens ВНУТРЬ completion-бюджета и
  обрезают JSON шага (зафиксировано: finish=length, 58/70). Все fork-вызовы
  идут с `chat_template_kwargs.enable_thinking=false` (extra_body), relay'и без
  поддержки параметра — авто-fallback с мемо на процесс
  (`disable_thinking` в конфиге);
- branches при этом штатно проходят schema_ok и укладываются в бюджет шага.

Gates против фактов — позиционная эвристика: отрицание засчитывается только в
пределах ±2 токенов от опорных слов факта (эхо факта с далёким «без/не» — не
конфликт; ложноположительный случай найден live-догодом и закрыт тестом).

Unit: 43/43 зелёные (`scripts/run_tests.sh tests/tools/test_fork_explore.py`).
Live (`FORK_DOGFOOD_LIVE=1 test_fork_explore_live.py`): роллауты и судья
проходят целиком, select c margin подтверждён; наблюдалась модель-индуцированная
аномалия packet (внутренний select-JSON, эхо в summary при внешнем reject) —
контрактный тест её поймал; разбор отложен до следующего шага.
Боёвка не включена: ждём калибровку по shadow-журналу (SPEC 7.4/13.4).

### Что закрыто этим шагом (17.10, вечер)

- Live-dogfood против `:8012/glm-5.3-flash` через `fork_explore_handler`
  (`FORK_DOGFOOD_LIVE=1`, opt-in): межшаговый гейт режет ДО следующего prefill
  на живых данных — ветки с `fact_conflict`/`forbidden_intent` остановлены на
  199–254 tok одним запросом; собраны и `select` с judge-матчами, и честный
  `reject`.
- Контрактный дефект #1 (нашёл только live): `_note` о тримминге, клеившийся
  ПОСЛЕ enforce_size, пересекал потолок (1508 > 1500) — заметка теперь
  снимается, если сама лезет за кап; тест-парния в TestHandlerE2E.
- Контрактный дефект #2: при `reject` `provenance.clusters` был `[]` —
  теперь reject журналует структуру форка (кластеры по всем приземлившимся
  трассам; судья видит только прошедшие гейты). После фикса — 3/3 зелёных
  live-прогона.
- Thinking/429--hardening живёт в самом live-тесте (не в production-path):
  судейский HTTP 429 — environment, ретраится с backoff; `thinking` —
  отдельный config-key (см. «Тени и боёвка» выше про enable_thinking).
- Seam внешних пауз: `fork.pause`/`fork.resume` — тонкая обёртка над
  `fork_explore_tool.request_stop()` / `ContinueFlag.resume()` (флаг ветки
  читается на ЕЁ следующей границе; decode уже идущего запроса не рвём —
  инвариант cancel из load-теста сохранён). Шерсть контроллера
  (`/att` → continue/pause/resume, scoped targets) — в plugins/controller,
  от этого PR намеренно отделена; consumer-wiring остаётся на шаге b.
- Калибровка K (внешний deadline): живые замеры —
  `scripts/fork_k_deadline_calibration.py` (одиночные latency p95; k=4/k=8
  wall-time; ceiling = max wall-time; budget = 3×ceiling; env
  FORK_K_LIVE/FORK_K_LIVE_ENDPOINT). Модельная сетка + разбор «deadline
  cross-path ≠ deadline budget» — в тексте скрипта.
- Итог проверок: 45 unit зелёные (`scripts/run_tests.sh
  tests/tools/test_fork_explore.py`) + live-файл (1 тест, вне дефолта) зелёный
  на `:8012`, `scripts/check` — 12 checks ok.
