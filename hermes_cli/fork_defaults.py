"""Fork-explore config defaults — DEFAULT_CONFIG["fork_explore"] lives here so
config_defaults.py stays under its size ratchet (facade + siblings rule).

Consumers read the section through DEFAULT_CONFIG (deep-merged in
config_defaults.py at import); runtime reading goes through
``tools.fork_explore_config.load_fork_config``.
"""

from __future__ import annotations

DEFAULT_FORK_EXPLORE: dict = {
    # Master switch; also gates tool availability (check_fn). Off = tool hidden.
    "enabled": True,
    # Решение 17.10 (Архитектор): боёвка только после калибровки. Shadow-режим —
    # отдельный дефолт, НЕ тот же флаг, что enabled: роллаут исполняется и
    # решение журналируется (fork_run.status='shadow'), но боевого fan-out'а
    # нет — JoinPacket помечен advisory, автоматического ForkGate-форка нет.
    # Флот переключает на false по данным shadow_stats() (SPEC 7.4 / 13.4).
    "shadow_only": True,
    # Fan-out: paths per fork (clamped), adaptive k lives with ForkGate (PR-3).
    "k_max": 4,
    # Rollout depth: модельных шагов вперёд на ветку (default / hard ceiling).
    "depth_default": 2,
    "depth_max": 3,
    # Completion cap per branch step and per-branch sampling temperature.
    "tokens_per_step_max": 1000,
    "bad_json_retry": True,
    "temperature": 0.7,
    # SGLang/vLLM thinking models bill reasoning inside the completion cap and
    # truncate fork-step JSON (verified live :8012). Disable thinking for fork
    # calls; relays that reject chat_template_kwargs auto-fall-back once.
    "disable_thinking": True,
    # Wall-clock for the whole fan-out incl. judge; over-budget branches are cancelled
    # (cancel = client disconnect; server frees the batch slot — see docs/fork-explore-design.md).
    "fork_deadline_s": 90,
    # Sum of fork tokens per session over 24h; a fork that would cross it is refused.
    "session_token_budget": 120000,
    # Winner needs this score gap after the blind pairwise, else select_with_caveat.
    "min_margin_delta": 0.34,
    # Scoring pipeline (PR-2): порог кластеризации траекторий (Jaccard по шагам);
    # размер кластера = консенсус-компонента итогового балла.
    "cluster_similarity": 0.8,
    # Итоговый балл S = judge*J + consensus*C - cost*cost_norm (SPEC 7.2, без logprob).
    "score_weights": {"judge": 0.70, "consensus": 0.25, "cost": 0.05},
    # Рання остановка: как только ⌈k/2⌉+1 прошедших гейты веток сошлись на
    # эквивалентных траекториях, очереди на запуск отменяются (SPEC 5.4).
    "early_stop_consensus": True,
    # Hard cap of the JoinPacket entering the main context (tokens, rough estimate).
    "join_max_tokens": 1500,
    # Give birth to path В from the strengths/weaknesses of A and Б (must pass the same gates).
    "synthesize": True,
    # Intent policy lite: branches may propose intents for these tools; irreversible
    # intents always carry needs_approval. Empty list = any tool name allowed.
    "forbidden_tools": ["terminal", "write_file", "patch"],
    # Directories a branch may READ during rollout (empty = no file reads at all).
    "read_roots": [],
    # Branch endpoint: empty base_url = the session's main model (side-task 'auto' route);
    # set an OpenAI-compatible URL to pin a dedicated rollout model.
    "branch_endpoint": {"base_url": "", "model": "", "api_key_env": ""},
    # Blind comparative judge (another model than the branches, SPEC 7.3). Empty
    # base_url falls back to the branch endpoint; the fleet pins SGLang :8012 here.
    "judge": {"base_url": "", "model": "", "api_key_env": "FORK_JUDGE_API_KEY",
              "max_tokens": 4000, "disable_thinking": True},
}

# OPTIONAL_ENV_VARS contribution (same entry shape as _env() produces there).
OPTIONAL_FORK_ENV_VARS: dict = {
    "FORK_JUDGE_API_KEY": {
        "description": "Optional API key for the fork_explore judge endpoint "
                       "(fork_explore.judge.api_key_env); local SGLang/vLLM accept any value",
        "prompt": "fork_explore judge API key (leave empty for a local endpoint)",
        "password": True, "category": "tool", "advanced": True,
    },
}
