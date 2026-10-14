"""Project-scoped MCP toolset hiding (.hermes/mcp-scope.yaml).

Covers the elk fork's lazy/project-scoped tool loading: out-of-scope MCP
servers must be hidden from the agent's tool refresh path when the session
CWD lives under a project with a scope file, and fail-open everywhere else.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from tools import mcp_tool_agent as mta


@pytest.fixture()
def outside_home_tmp(request, tmp_path_factory):
    """A scratch dir guaranteed NOT to live under the real Hermes home
    (TMPDIR may point into ~/.hermes/cache/scratch; the home-IO guard
    refuses even stat'ing ancestors of such paths)."""
    from pathlib import Path
    import shutil
    base = tmp_path_factory.mktemp("outside-home")
    if str(base).startswith(str(Path.home() / ".hermes")):
        base = Path(tempfile.gettempdir()) / f"hermes-test-{os.getpid()}-{base.name}"
        base.mkdir(parents=True, exist_ok=True)
        request.addfinalizer(lambda: shutil.rmtree(base, ignore_errors=True))
    return base


class _Agent:
    def __init__(self, cwd: str):
        self._current_task_id = "t1"
        self.enabled_toolsets: list[str] | None = None
        self.disabled_toolsets: list[str] | None = None
        self._cwd = cwd


@pytest.fixture()
def fake_cwd(monkeypatch, tmp_path):
    """Pin the authoritative workspace root to a temp dir."""
    root = tmp_path / "proj"
    root.mkdir()
    import tools.file_tools_paths as ftp
    monkeypatch.setattr(
        ftp, "_authoritative_workspace_root", lambda task_id="default": str(root)
    )
    # Neutralize the global full-catalog switch resolution differences.
    from hermes_cli import config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config_readonly", dict)
    return root


def _write_scope(root, body: str):
    d = root / ".hermes"
    d.mkdir(exist_ok=True)
    p = d / "mcp-scope.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_no_scope_file_is_noop(fake_cwd, monkeypatch):
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha", "beta"},
    )
    assert mta._project_mcp_scope_disabled_toolsets(_Agent("")) == []


def test_out_of_scope_servers_hidden(fake_cwd, monkeypatch):
    _write_scope(fake_cwd, "enabled: true\nservers:\n  - alpha\n")
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha", "beta", "gamma"},
    )
    out = mta._project_mcp_scope_disabled_toolsets(_Agent(""))
    assert out == ["mcp-beta", "mcp-gamma"]


def test_disabled_scope_file_is_noop(fake_cwd, monkeypatch):
    _write_scope(fake_cwd, "enabled: false\nservers:\n  - alpha\n")
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha", "beta"},
    )
    assert mta._project_mcp_scope_disabled_toolsets(_Agent("")) == []


def test_broken_scope_file_fails_open(fake_cwd, monkeypatch):
    _write_scope(fake_cwd, "enabled: true\n\t nonsense: [")
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha"},
    )
    # invalid YAML -> parse error -> no-op (full catalog), never an exception
    assert mta._project_mcp_scope_disabled_toolsets(_Agent("")) == []


def test_full_catalog_switch_wins(fake_cwd, monkeypatch):
    _write_scope(fake_cwd, "enabled: true\nservers:\n  - alpha\n")
    from hermes_cli import config as cfgmod
    monkeypatch.setattr(
        cfgmod,
        "load_config_readonly",
        lambda: {"tools": {"mcp_scope": {"full": True}}},
    )
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha", "beta"},
    )
    assert mta._project_mcp_scope_disabled_toolsets(_Agent("")) == []


def test_project_scope_shadows_global(fake_cwd, monkeypatch, tmp_path):
    _write_scope(fake_cwd, "enabled: true\nservers:\n  - alpha\n")
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha", "beta"},
    )
    agent = _Agent("")
    out = mta._project_mcp_scope_disabled_toolsets(agent)
    assert out == ["mcp-beta"]


def test_resolve_refresh_applies_scope(monkeypatch, fake_cwd):
    _write_scope(fake_cwd, "enabled: true\nservers:\n  - alpha\n")
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha", "beta"},
    )
    agent = _Agent("")
    agent.disabled_toolsets = ["web"]
    _enabled, disabled = mta._resolve_refresh_toolsets(agent, None, None)
    assert "mcp-beta" in disabled
    assert "web" in disabled


def test_refresh_persist_not_dropped_when_scoped(monkeypatch, fake_cwd):
    """Explicit override path must persist the scoped-disabled selection."""
    _write_scope(fake_cwd, "enabled: true\nservers:\n  - alpha\n")
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha", "beta"},
    )
    agent = _Agent("")
    _enabled, disabled = mta._resolve_refresh_toolsets(agent, None, ["web"])
    assert "mcp-beta" in disabled
    # persistence on the explicit path: the agent keeps the effective selection
    assert "mcp-beta" in (agent.disabled_toolsets or [])


def test_home_fallback_scopes_when_no_recorded_cwd(monkeypatch, outside_home_tmp):
    """No recorded session CWD: the probe follows get_hermes_home(), so a global
    ``<HERMES_HOME>/mcp-scope.yaml`` keeps the narrow catalog — and it never
    stats a stray process CWD."""
    import tools.file_tools_paths as ftp
    import hermes_constants
    from hermes_cli import config as cfgmod

    home = outside_home_tmp / "home"
    home.mkdir()
    monkeypatch.setattr(ftp, "_authoritative_workspace_root", lambda task_id="default": None)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    monkeypatch.setattr(cfgmod, "load_config_readonly", dict)
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha", "beta"},
    )
    _write_scope(home, "enabled: true\nservers:\n  - alpha\n")
    assert mta._project_mcp_scope_disabled_toolsets(_Agent("")) == ["mcp-beta"]


def test_home_fallback_no_file_is_noop(monkeypatch, outside_home_tmp):
    """Home without a scope file: full catalog, no error."""
    import tools.file_tools_paths as ftp
    import hermes_constants
    from hermes_cli import config as cfgmod

    home = outside_home_tmp / "home"
    home.mkdir()
    monkeypatch.setattr(ftp, "_authoritative_workspace_root", lambda task_id="default": None)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    monkeypatch.setattr(cfgmod, "load_config_readonly", dict)
    monkeypatch.setattr(
        "tools.mcp_tool_discovery.get_registered_mcp_server_names",
        lambda: {"alpha"},
    )
    assert mta._project_mcp_scope_disabled_toolsets(_Agent("")) == []
