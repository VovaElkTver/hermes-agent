"""Shared prefix / branch suffix builders for fork_explore (fork-reasoning PR-1).

The unit of a fork is a TRAJECTORY (куда повернёт работа), not an answer. Every
branch receives the byte-identical SHARED PREFIX (system block + the whole
task/context/criteria block) and differs only in the trailing BRANCH SUFFIX.
Stable prefix + divergent suffix is the RadixAttention / prefix-cache ideal:
when the real cache-fork lands (design doc § Cache-fork path) this layout is
drop-in. See ~/projects/fork-reasoning/SPEC.md §4.2 and docs/fork-explore-design.md.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

BRANCH_SYSTEM_PROMPT = """Ты — исполнитель изолированной ветки dry-роллаута (fork-explore).
Ты разворачиваешь ТРАЕКТОРИЮ: куда пойдёт работа по назначенному пути, шаг за шагом.
Не финальный ответ — маршрут.

ПРАВИЛА ВЕТКИ:
1. Работай только с контекстом задачи ниже. Ты НЕ видишь другие ветки.
2. Ты НЕ можешь ничего писать во внешний мир: любое желаемое действие оформляй
   полем intents (tool, args, reversible) — его выполнит главная сессия после join.
3. Каждый шаг — один JSON-объект строго по схеме:
   {"kind":"action|observation|decision|read_request|stop","content":"1-3 предложения",
    "intents":[{"tool":"...","args":{...},"reversible":true}],
    "paths":["файлы для read_request"],"risks":["..."],"infeasible":false,
    "contradicts":[...]}
4. Если назначенный путь невыполним — верни "infeasible": true с причиной в content.
   Это валидный и полезный результат.
5. Читай только то, что явно нужно для следующего шага; не пересказывай контекст.
6. Если шаг противоречит подтверждённому факту из CONFIRMED FACTS — укажи его
   номер в contradicts (например, "contradicts":[2]). Это сработает жёсткий гейт.
7. Последний шаг траектории — kind=stop."""


def _shared_block(task: str, fork_point: str, shared_context: str,
                  acceptance: list[str], facts: list[str]) -> str:
    parts = ["# FORK TASK (развилка направлений)", task]
    if fork_point:
        parts += ["", "# ТОЧКА РАЗВИЛКИ", fork_point]
    parts += ["", "# SHARED CONTEXT (общий большой контекст — одинаков у всех веток)",
              shared_context]
    parts += ["", "# ACCEPTANCE CRITERIA (критерии приёмки направления)",
              "\n".join(f"{i}. {c}" for i, c in enumerate(acceptance, 1))]
    if facts:
        parts += ["", "# CONFIRMED FACTS (противоречие с ними = дисквалификация ветки)",
                  "\n".join(f"- {f}" for f in facts)]
    parts += ["", "# OUTPUT",
              "Верни ОДИН JSON-объект шага траектории по схеме из системного блока."]
    return "\n".join(parts)


def build_shared_prefix(task: str, fork_point: str, shared_context: str,
                        acceptance: list[str], facts: list[str]) -> list[dict[str, str]]:
    """The byte-stable prefix every branch gets, identical across branches and calls.

    No timestamps, no branch ids, nothing ordering-dependent: two calls with the
    same inputs must produce the same ``prefix_hash``.
    """
    return [
        {"role": "system", "content": BRANCH_SYSTEM_PROMPT},
        {"role": "user", "content": _shared_block(task, fork_point, shared_context,
                                                  acceptance, facts)},
    ]


def build_branch_suffix(ref: str, strategy: str) -> dict[str, str]:
    """The per-branch suffix — appended AFTER the shared prefix (differences go last)."""
    return {"role": "user",
            "content": f"# BRANCH\nbranch: {ref}\nstrategy: {strategy}\n"
                       "Разверни траекторию по этому пути."}


def step_instruction(step_no: int, depth: int) -> dict[str, str]:
    """Identical for all branches at the same step index (keeps the shared prefix
    as long as possible across the rollout)."""
    return {"role": "user",
            "content": f"# STEP {step_no}/{depth}\n"
                       "Следующий шаг траектории — один JSON-объект по схеме. "
                       "Если путь завершён или исчерпан — kind=stop."}


def prefix_hash(prefix: list[dict[str, str]]) -> str:
    """sha256 over the canonical serialization — the journal's cache-fork anchor."""
    blob = json.dumps(prefix, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def dedup_paths(paths: list[dict[str, Any]], threshold: float = 0.9) -> list[dict[str, Any]]:
    """Anti-duplicates (SPEC 5.4): near-identical strategies must not both fan out.
    Paths arrive as {"ref","label","strategy"}; similarity is on lowercased strategy."""
    from difflib import SequenceMatcher
    kept: list[dict[str, Any]] = []
    for p in paths:
        s = (p.get("strategy") or p.get("label") or "").lower().strip()
        dup = any(
            SequenceMatcher(None, s,
                            (k.get("strategy") or k.get("label") or "").lower().strip()).ratio()
            > threshold
            for k in kept)
        if not dup:
            kept.append(p)
    return kept
