"""OPTIONAL_ENV_VARS entry factories (moved out of config_defaults.py for the
size ratchet; config_defaults re-imports these names so its body is unchanged).

``url``/``help``/``tools`` are only written when passed; ``password=None`` omits
the key; ``advanced`` is only written when true. Key order matches the plain
``_env`` entries.
"""

from __future__ import annotations

from typing import Any

_OMIT = object()


def env_entry(description: str, prompt: str, **keys: Any) -> dict:
    """One OPTIONAL_ENV_VARS entry; keyword order is preserved as dict key order."""
    return {"description": description, "prompt": prompt, **keys}


def category_factory(category: str, password: Any, advanced: bool):
    """Entry factory for one category with its usual password/advanced defaults."""
    def make(description, prompt, url=_OMIT, *, help=_OMIT, tools=_OMIT, password=password,
             advanced=advanced):
        d = {"description": description, "prompt": prompt}
        d.update((k, v) for k, v in (("help", help), ("url", url), ("tools", tools)) if v is not _OMIT)
        if password is not None:
            d["password"] = password
        d["category"] = category
        if advanced:
            d["advanced"] = True
        return d
    return make


def base_url_entry(prov_factory, name: str, prompt_name: str | None = None) -> dict:
    """Provider ``*_BASE_URL`` override entry (advanced, not a secret)."""
    prompt = f"{prompt_name or name} base URL (leave empty for default)"
    return prov_factory(f"{name} base URL override", prompt, None, password=False)
