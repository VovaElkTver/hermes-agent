"""Config + endpoint resolution for fork_explore (reads ``fork_explore:`` in config.yaml).

Defaults live in DEFAULT_CONFIG (hermes_cli/config_defaults.py) — the single
source; runtime reading goes through ``load_config_readonly`` (the sanctioned
presence-sensitive loader) and deep-merges over those defaults.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_fork_config() -> dict[str, Any]:
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    cfg = dict(DEFAULT_CONFIG.get("fork_explore") or {})
    try:
        from hermes_cli.config import load_config_readonly
        user = load_config_readonly()
        block = user.get("fork_explore") if isinstance(user, dict) else None
        if isinstance(block, dict):
            cfg = _deep_merge(cfg, block)
    except (ImportError, OSError, ValueError, TypeError, RuntimeError) as exc:
        # unreadable/broken config → built-in defaults, never a hard fail; a bad
        # fork config must not take tool discovery or the whole turn down
        logger.warning("fork_explore config read failed (%s); using defaults", exc, exc_info=True)
    return cfg


def resolve_chat_client(endpoint: dict[str, Any], task: str) -> tuple[Any, str] | tuple[None, None]:
    """Resolve an OpenAI-compatible chat client for branch/judge calls.

    Empty base_url → provider 'auto' (the session's main model for side tasks);
    explicit base_url → 'custom' passthrough (e.g. SGLang :8012 judge). API keys
    resolve through secret_scope when an env var is named, else 'EMPTY' (local
    SGLang/vLLM accept any bearer).
    """
    base_url = str((endpoint or {}).get("base_url") or "").strip()
    model = str((endpoint or {}).get("model") or "").strip()
    api_key_env = str((endpoint or {}).get("api_key_env") or "").strip()
    api_key = ""
    if api_key_env:
        try:
            from agent.secret_scope import get_secret
            api_key = str(get_secret(api_key_env, default="") or "")
        except (ImportError, RuntimeError) as exc:
            # UnscopedSecretError is a RuntimeError: no profile scope at boot → local
            # endpoints still work with any bearer, so fall through to 'EMPTY'
            logger.debug("fork_explore secret %s unavailable: %s", api_key_env, exc)
    try:
        from agent.auxiliary_client import resolve_provider_client
        client, resolved_model = resolve_provider_client(
            "custom" if base_url else "auto", model or None,
            explicit_base_url=base_url or None,
            explicit_api_key=api_key or "EMPTY",
            task=task)
        if client is None:
            return None, None
        return client, resolved_model or model
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        logger.warning("fork_explore: endpoint resolution failed: %s", exc, exc_info=True)
        return None, None
