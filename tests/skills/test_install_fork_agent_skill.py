"""Contract between the install-fork-agent skill and the shipped fork installers.

The skill documents the exact one-liners and env overrides of fork-install.sh /
fork-install.ps1 (PR#21). If an installer changes its URL, defaults, or override
names, the skill text must be updated in the same change — this test fails first.
Shell scripts are artifacts, not source-under-test in the "never read source"
sense: the assertion is a document contract between two shipped documents.
"""
from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "autonomous-ai-agents" / "install-fork-agent" / "SKILL.md"
SH = REPO / "fork-install.sh"
PS1 = REPO / "fork-install.ps1"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_skill_documents_both_one_liners_verbatim():
    skill = _text(SKILL)
    for installer in (_text(SH), _text(PS1)):
        m = re.search(r"raw\.githubusercontent\.com/\S+/(?:main/)?fork-install\.\w+", installer)
        assert m, "installer header lost its one-liner"
        assert m.group(0) in skill, m.group(0)


def test_skill_lists_every_env_override_the_installers_honour():
    skill = _text(SKILL)
    sh, ps1 = _text(SH), _text(PS1)
    for var in ("HERMES_FORK_URL", "HERMES_FORK_BRANCH", "HERMES_HOME", "DRY_RUN"):
        assert var in sh and var in ps1, f"{var} gone from an installer"
        assert var in skill, f"skill no longer documents {var}"


def test_skill_notes_refusal_and_ff_update_behaviour():
    skill = _text(SKILL).lower()
    # matches the installer's refusal wording ("refusing to touch it") and the
    # ff-only update path present in both variants
    assert "refus" in skill
    assert "--ff-only" in _text(SH) and "--ff-only" in _text(PS1)
    assert "fast-forward" in skill or "ff-only" in skill


def test_skill_states_windows_caveat_and_no_silent_patch_rule():
    skill = _text(SKILL)
    assert "not yet verified" in skill.lower() or "expected-unverified" in skill.lower()
    assert "never silently patch" in skill.lower()
