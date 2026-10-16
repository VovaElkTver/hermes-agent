"""Plugin env vars survive a models.dev rename (catalog-drift regression, 2026-10-16).

commandcode's models.dev entry flipped COMMANDCODE_API_KEY -> CMD_API_KEY; resolution must
keep the documented plugin names or every existing user's provider silently loses auth.
"""
from types import SimpleNamespace

import hermes_cli.providers as prov


def test_plugin_env_vars_survive_catalog_rename(monkeypatch):
    # Simulate the drifted catalog: commandcode present, key renamed, plugin var gone.
    info = SimpleNamespace(name="CommandCode", env=("CMD_API_KEY",), api="https://api.commandcode.ai/provider/v1",
                           doc="https://commandcode.ai/docs/provider")
    monkeypatch.setattr(prov, "_models_dev_info", lambda *a, **k: info)
    pdef = prov.get_provider("commandcode")
    assert pdef is not None
    # documented name first (what doctor/config_defaults/the plugin key off),
    assert "COMMANDCODE_API_KEY" in pdef.api_key_env_vars
    assert pdef.api_key_env_vars[0] == "COMMANDCODE_API_KEY"
    # ...and the catalog's new name still available, so a user who set CMD_API_KEY works too.
    assert "CMD_API_KEY" in pdef.api_key_env_vars
    assert pdef.base_url == "https://api.commandcode.ai/provider/v1"


def test_no_plugin_profile_leaves_catalog_order_untouched(monkeypatch):
    info = SimpleNamespace(name="WhoKnows", env=("WHOKNOWS_API_KEY",), api="https://x.test/v1", doc="")
    monkeypatch.setattr(prov, "_models_dev_info", lambda *a, **k: info)
    monkeypatch.setattr(prov, "_plugin_profile_pdef", lambda name: None)
    import providers as _p
    monkeypatch.setattr(_p, "get_provider_profile", lambda name: None)
    pdef = prov.get_provider("whoknows")
    assert pdef is not None
    assert pdef.api_key_env_vars == ("WHOKNOWS_API_KEY",)
