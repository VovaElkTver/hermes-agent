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

## Статус / dogfood

Unit: 26/26 зелёные (`.venv/bin/python -m pytest tests/tools/test_fork_explore.py`).
Live (:8012, `test_fork_explore_live.py`, skipped by default): конвейер
пройден целиком — fan-out→гейты→судья→JoinPacket→журнал, budget enforced;
на живой модели ветки упёрлись в schema_ok (glm-5.3-flash не вернул строгий
JSON за 600 токенов — фолбэк сработал штатно: reject + open_issue + журнал).
Калибровка branch-промпта/температуры под живой судью — следующий шаг перед
shadow-боем.
