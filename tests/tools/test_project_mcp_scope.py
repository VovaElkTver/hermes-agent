"""Project-scoped MCP toolset hiding (.hermes/mcp-scope.yaml).

Covers the elk fork's lazy/project-scoped tool loading: out-of-scope MCP
servers must be hidden from the agent's tool refresh path when the session
CWD lives under a project with a scope file, and fail-open everywhere else.
"""

from __future__ import annotations

import os

import pytest

from tools import mcp_tool_agent as mta


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
    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: {})
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
    p = _write_scope(fake_cwd, "enabled: true\n\t nonsense: [")
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
    enabled, disabled = mta._resolve_refresh_toolsets(agent, None, None)
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
    enabled, disabled = mta._resolve_refresh_toolsets(agent, None, ["web"])
    assert "mcp-beta" in disabled
    # persistence on the explicit path: the agent keeps the effective selection
    assert "mcp-beta" in (agent.disabled_toolsets or [])
