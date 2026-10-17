"""Live dogfood for fork_explore against the fleet judge (:8012 SGLang).

Skipped unless FORK_DOGFOOD_LIVE=1. Proves the whole engine end-to-end with a
real model: branch rollout -> hard gates -> blind swap-pairwise judge ->
JoinPacket under budget. NOT part of the default suite (network + GPU).
"""

import json
import os

import pytest

from agent.model_metadata import estimate_tokens_rough

pytestmark = pytest.mark.skipif(
    not (os.environ.get("FORK_DOGFOOD_LIVE") or os.path.exists("/tmp/fork-dogfood-home")),
    reason="set FORK_DOGFOOD_LIVE=1 or mkdir /tmp/fork-dogfood-home to run")

BASE_URL = os.environ.get("FORK_DOGFOOD_URL", "http://192.168.9.100:8012/v1")
MODEL = os.environ.get("FORK_DOGFOOD_MODEL", "glm-5.3-flash-libert")


def test_live_fork_pr2_vs_pr3(tmp_path, monkeypatch):
    from tools import fork_explore_config
    ep = {"base_url": BASE_URL, "model": MODEL, "api_key_env": ""}
    cfg = fork_explore_config.load_fork_config()
    cfg.update({"enabled": True, "k_max": 4, "depth_default": 2, "depth_max": 3,
                "tokens_per_step_max": 1000, "temperature": 0.3, "fork_deadline_s": 180,
                "session_token_budget": 200000, "min_margin_delta": 0.34,
                "join_max_tokens": 1500, "synthesize": True,
                "forbidden_tools": ["terminal", "write_file", "patch"],
                "read_roots": [str(tmp_path)],
                "branch_endpoint": dict(ep), "judge": {**ep, "max_tokens": 4000}})
    monkeypatch.setattr(fork_explore_config, "load_fork_config", lambda: cfg)

    from tools.fork_explore_tool import fork_explore
    args = {
        "task": ("Autonomy-линия Hermes: выбрать следующий PR развития форка рассуждений "
                 "после готового PR-1 fork_explore."),
        "shared_context": (
            "PR-1 реализован: read-only dry-роллауты, жёсткие гейты, слепой swap-pairwise "
            "судья, JoinPacket <=1500 токенов, журнал fork_rate/win_over_default/wasted_tokens "
            "в sqlite, 16 unit-тестов зелёные.\n"
            "Кандидат А (PR-2): version-check + re-score политика — вердикт протухает при "
            "смене версии контекста.\n"
            "Кандидат Б (PR-3): ForkGate shadow-метрики — боёвка только после калибровки по "
            "журналу.\n"
            "Ограничения: staged/обратимый rollout; prompt-cache основного цикла не трогать; "
            "судья :8012 max_tokens=4000; одна ветка = один PR."),
        "fork_point": "Что делать следующим: PR-2 (version-check/re-score) или PR-3 (shadow ForkGate)?",
        "acceptance_direction": [
            "не блокирует боёвку без данных калибровки",
            "обратимо / staged",
            "не рвёт prompt-cache основного цикла",
            "выполнимо одной веткой"],
        "confirmed_facts": [":8012 жив: SGLang glm-5.3-flash-libert",
                            "журнал fork_explore уже пишет fork_rate/wasted_tokens"],
        "paths": [
            {"ref": "A", "strategy": "сначала PR-2 version-check/re-score: без политики "
                                     "протухания вердикты журнала нельзя ни ре-скорить, ни "
                                     "доверить им боёвку"},
            {"ref": "B", "strategy": "сначала PR-3 shadow ForkGate: данные журнала первичны, "
                                     "калибруем пороги на реальных форках, version-check позже"},
        ],
    }
    res = json.loads(fork_explore(args, session_id="dogfood-live"))
    assert res["ok"], res
    packet = res["packet"]
    print("\nLIVE FORK RESULT:", json.dumps(packet, ensure_ascii=False)[:2000])
    # The engine must complete and honour its contract whatever the live model does:
    # either a direction is chosen, or every branch honestly fails its gates.
    if packet["strategy"] == "reject":
        assert packet["winner"] is None
        assert packet["result"]["open_issues"]
    else:
        assert packet["winner"]["path_ref"] in ("A", "B", "synth")
        assert packet["strategy"] in ("select", "select_with_caveat", "synthesize")
    assert estimate_tokens_rough(json.dumps(packet, ensure_ascii=False)) <= 1500
    assert res["journal_ref"].startswith("journal://forks/")
    # PR-2: scoring breakdown must be journaled, never context-leaked
    assert packet["provenance"]["clusters"]
    assert packet["provenance"]["score_weights"].get("judge")
    from tools import fork_explore_journal
    run = fork_explore_journal.get_run(packet["join_id"])
    assert run and len(run["branches"]) >= 2
    for br in run["branches"]:
        comps = json.loads(br["score_components_json"])
        assert "judge_win_rate" in comps and "consensus" in comps
    stats = fork_explore_journal.shadow_stats()
    assert stats["runs"] >= 1 and stats["wasted_tokens"] >= 0
