"""Tests for the operator policy overlay (``policy_overlay`` + the shipped
``policy.operator.yaml``).

Mirrors the style of ``test_policy.py``: import the loader, build the overlay,
and assert decisions per tool. The contract under test:

* The render tool is ALLOWLISTED — renders are free (codex by default), so the
  per-render spend gate was removed live and this file matches that state.
* The read / client_read / brief tools are ALLOWLISTED.
* In-code defaults still WIN over the overlay (you can't allowlist away a
  baked-in gate; ``rm -rf`` still asks; ``kie_generate`` still asks).
* The shipped ``policy.operator.yaml`` parses to exactly those sets.
* An EMPTY overlay is a pure pass-through to ``policy.evaluate`` — proof that
  loading the overlay doesn't change Ekko's behavior when its (empty) policy
  is in place.

Tool names are the EXACT FULL names Hermes presents to the ``pre_tool_call``
hook: ``mcp_<server>_<tool>`` with single underscores (verified live on the
VPS). The server "pipeline-operator" normalizes to "pipeline_operator", and the
tool functions are already ``pipeline_operator_<verb>``, so the live names are
doubled (e.g. ``mcp_pipeline_operator_pipeline_operator_render``). Matching is
exact equality — there is no fuzzy/suffix matching.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voxhorizon_approvals.policy import evaluate
from voxhorizon_approvals.policy_overlay import PolicyOverlay, load_overlay

#: The shipped operator profile lives next to the package.
OPERATOR_POLICY_PATH = (
    Path(__file__).resolve().parent.parent / "policy.operator.yaml"
)

#: Exact full tool names as Hermes presents them (single-underscore namespacing).
RENDER = "mcp_pipeline_operator_pipeline_operator_render"
READ = "mcp_pipeline_operator_pipeline_operator_read"
CLIENT_READ = "mcp_pipeline_operator_pipeline_operator_client_read"
BRIEF = "mcp_pipeline_operator_pipeline_operator_brief"

# Video (VID-7): video_brief is a free write (allowlisted); video_render is
# threshold-gated in-code (NOT in the overlay sets).
VIDEO_BRIEF = "mcp_pipeline_operator_pipeline_operator_video_brief"
VIDEO_RENDER = "mcp_pipeline_operator_pipeline_operator_video_render"

# P3 stage-persist tools (all allowlisted; none clears a gate).
QA_RESULT = "mcp_pipeline_operator_pipeline_operator_qa_result"
COMPLIANCE_RESULT = "mcp_pipeline_operator_pipeline_operator_compliance_result"
COPY = "mcp_pipeline_operator_pipeline_operator_copy"
SPEC_RESULT = "mcp_pipeline_operator_pipeline_operator_spec_result"
FINALIZE_RESULT = "mcp_pipeline_operator_pipeline_operator_finalize_result"
MONITOR_RESULT = "mcp_pipeline_operator_pipeline_operator_monitor_result"
SIGNAL = "mcp_pipeline_operator_pipeline_operator_signal"

# The HARD launch gate — requires approval (irreversible Meta spend).
LAUNCH = "mcp_pipeline_operator_pipeline_operator_launch"
META_ACTIVATE = "mcp_Meta_ads_activate_entity"

#: Every allowlisted operator tool (read + author + render + the P3 persisters).
ALLOWLISTED_TOOLS = (
    READ,
    CLIENT_READ,
    BRIEF,
    RENDER,
    QA_RESULT,
    COMPLIANCE_RESULT,
    COPY,
    SPEC_RESULT,
    FINALIZE_RESULT,
    MONITOR_RESULT,
    SIGNAL,
    VIDEO_BRIEF,
)


@pytest.fixture
def operator_overlay() -> PolicyOverlay:
    return load_overlay(OPERATOR_POLICY_PATH)


# ---------------------------------------------------------------------------
# The operator profile allowlists render + read/client_read/brief
# ---------------------------------------------------------------------------


def test_render_tool_is_allowlisted(operator_overlay: PolicyOverlay) -> None:
    """Renders are free (codex default), so the render tool is allowlisted —
    the per-render spend gate was removed live; this file matches that."""
    decision = operator_overlay.evaluate(RENDER, {})
    assert decision.action == "allow"
    assert "allowlist" in decision.reason


def test_render_tool_allowed_regardless_of_args(
    operator_overlay: PolicyOverlay,
) -> None:
    decision = operator_overlay.evaluate(
        RENDER,
        {"pipeline_id": "p-1", "kind": "concept_preview", "items": [{}]},
    )
    assert decision.action == "allow"


def test_read_tool_is_allowlisted(operator_overlay: PolicyOverlay) -> None:
    """The read tool is allowed without an operator round-trip."""
    decision = operator_overlay.evaluate(READ, {})
    assert decision.action == "allow"
    assert "allowlist" in decision.reason


def test_client_read_tool_is_allowlisted(
    operator_overlay: PolicyOverlay,
) -> None:
    """The client-read tool (pure GET of brand/offers/do-not-say) is allowed."""
    decision = operator_overlay.evaluate(CLIENT_READ, {})
    assert decision.action == "allow"
    assert "allowlist" in decision.reason


def test_brief_tool_is_allowlisted(operator_overlay: PolicyOverlay) -> None:
    """The brief tool (free write, reviewed via the stage gate) is allowed."""
    decision = operator_overlay.evaluate(BRIEF, {})
    assert decision.action == "allow"


# ---------------------------------------------------------------------------
# P3 stage-persist tools are all allowlisted (free writes; clear no gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    [QA_RESULT, COMPLIANCE_RESULT, COPY, SPEC_RESULT, FINALIZE_RESULT, MONITOR_RESULT, SIGNAL],
)
def test_stage_persist_tools_are_allowlisted(
    operator_overlay: PolicyOverlay, tool: str
) -> None:
    """The post-generation persist tools record evidence + roll the per-creative
    gate; they clear NO gate (the worker adjudicates qa/compliance, the manager
    clears gates), so they are allowlisted — no spend, no round-trip."""
    decision = operator_overlay.evaluate(tool, {})
    assert decision.action == "allow"
    assert "allowlist" in decision.reason


def test_stage_persist_tools_allowed_with_array_payload(
    operator_overlay: PolicyOverlay,
) -> None:
    """The persist tools stay allowed regardless of the array payload shape."""
    decision = operator_overlay.evaluate(
        COPY,
        {"pipeline_id": "p-1", "variants": [{"creative_id": "cr-1"}]},
    )
    assert decision.action == "allow"


# ---------------------------------------------------------------------------
# The HARD launch gate requires approval (irreversible Meta spend)
# ---------------------------------------------------------------------------


def test_launch_tool_requires_approval(operator_overlay: PolicyOverlay) -> None:
    """The Meta launch / activate is irreversible spend — it must ask the
    manager (the HARD launch gate), never allow silently."""
    decision = operator_overlay.evaluate(LAUNCH, {})
    assert decision.action == "ask_operator"
    assert "policy overlay" in decision.reason


def test_meta_activate_tool_requires_approval(
    operator_overlay: PolicyOverlay,
) -> None:
    """The raw Meta activate-entity tool is also gated — defense in depth in
    case the operator reaches the platform tool directly."""
    decision = operator_overlay.evaluate(META_ACTIVATE, {})
    assert decision.action == "ask_operator"


def test_launch_tool_is_not_allowlisted(operator_overlay: PolicyOverlay) -> None:
    """The launch tool must NOT be in the allowlist (gating wins over allowing)."""
    assert LAUNCH not in operator_overlay.allowlist
    assert LAUNCH in operator_overlay.extra_requires_approval


# ---------------------------------------------------------------------------
# Video (VID-7): video_brief allowlisted; video_render threshold-gated (D1)
# ---------------------------------------------------------------------------


def test_video_brief_is_allowlisted(operator_overlay: PolicyOverlay) -> None:
    """Authoring a video brief is a free write, like the image brief."""
    decision = operator_overlay.evaluate(VIDEO_BRIEF, {})
    assert decision.action == "allow"
    assert "allowlist" in decision.reason


def test_video_render_under_threshold_runs_inline(
    operator_overlay: PolicyOverlay,
) -> None:
    """At/under the per-ad threshold the render runs inline (no round-trip).

    The overlay leaves video_render unlisted, so it falls through to the in-code
    conditional spend gate in policy.evaluate.
    """
    decision = operator_overlay.evaluate(
        VIDEO_RENDER, {"pipeline_id": "p-1", "estimated_cost_usd": 1.2}
    )
    assert decision.action == "allow"


def test_video_render_over_threshold_asks(operator_overlay: PolicyOverlay) -> None:
    """Over the threshold the render long-polls the manager (spend)."""
    decision = operator_overlay.evaluate(
        VIDEO_RENDER, {"pipeline_id": "p-1", "estimated_cost_usd": 9.0}
    )
    assert decision.action == "ask_operator"
    assert decision.risk_class == "spend"


def test_video_render_missing_estimate_asks(
    operator_overlay: PolicyOverlay,
) -> None:
    """No estimate fails safe (ask)."""
    decision = operator_overlay.evaluate(VIDEO_RENDER, {"pipeline_id": "p-1"})
    assert decision.action == "ask_operator"


def test_video_render_not_in_overlay_sets(operator_overlay: PolicyOverlay) -> None:
    """video_render is gated in-code by threshold, so it must NOT be in the
    overlay allowlist or extra_requires_approval (else the branch is bypassed)."""
    assert VIDEO_RENDER not in operator_overlay.allowlist
    assert VIDEO_RENDER not in operator_overlay.extra_requires_approval


# ---------------------------------------------------------------------------
# Matching is exact full-name equality — short names do NOT match
# ---------------------------------------------------------------------------


def test_bare_short_render_name_is_not_gated_by_overlay(
    operator_overlay: PolicyOverlay,
) -> None:
    """A bare short name must NOT be gated by the overlay.

    Proves the overlay relies on the exact full live name, not loose/suffix
    matching: ``pipeline_operator_render`` is not the live name Hermes presents,
    so it must fall through to the base engine rather than the overlay's spend
    gate.
    """
    decision = operator_overlay.evaluate("pipeline_operator_render", {})
    assert "policy overlay" not in decision.reason


# ---------------------------------------------------------------------------
# The shipped file parses to exactly the intended sets
# ---------------------------------------------------------------------------


def test_shipped_operator_policy_contents() -> None:
    overlay = load_overlay(OPERATOR_POLICY_PATH)
    # Only the irreversible Meta launch / activate requires approval (the HARD
    # launch gate); renders are free so they stay allowlisted.
    assert overlay.extra_requires_approval == frozenset({LAUNCH, META_ACTIVATE})
    # The allowlist carries the read/author/render tools PLUS the P3
    # stage-persist tools (qa/compliance/copy/spec/finalize/monitor/signal) —
    # all free writes that clear no gate.
    assert overlay.allowlist == frozenset(ALLOWLISTED_TOOLS)
    # execute_code/terminal/shell are the code/shell tools; fetch_url/patch/
    # process are the remaining non-MCP write/exec escape hatches (fetch_url is
    # the one SILENT HTTP-write path) the operator must never use.
    assert overlay.blocklist == frozenset(
        {"execute_code", "terminal", "shell", "fetch_url", "patch", "process"}
    )


def test_launch_not_in_allowlist_shipped() -> None:
    """The shipped file must keep the launch tools OUT of the allowlist."""
    overlay = load_overlay(OPERATOR_POLICY_PATH)
    assert LAUNCH not in overlay.allowlist
    assert META_ACTIVATE not in overlay.allowlist


def test_operator_overlay_blocks_shell_tools() -> None:
    """The shell/code tools are hard-blocked so the operator can't shell out to
    run helper.py (which would hit the spend-gate long-poll and hang); it must
    author payloads directly and persist them via the MCP pipeline tools."""
    overlay = load_overlay(OPERATOR_POLICY_PATH)
    for tool in ("execute_code", "terminal", "shell", "fetch_url", "patch", "process"):
        assert overlay.evaluate(tool, {}).action == "block"


# ---------------------------------------------------------------------------
# In-code defaults WIN over a softening overlay
# ---------------------------------------------------------------------------


def test_overlay_cannot_allowlist_away_requires_approval() -> None:
    """A tool baked into REQUIRES_APPROVAL stays gated even if allowlisted."""
    overlay = PolicyOverlay(
        allowlist=frozenset({"kie_generate"}),
        extra_requires_approval=frozenset(),
        blocklist=frozenset(),
    )
    decision = overlay.evaluate("kie_generate", {})
    assert decision.action == "ask_operator"
    assert decision.risk_class == "spend"


def test_overlay_cannot_allowlist_away_destructive_shell() -> None:
    """``rm -rf`` still asks even if ``shell_command`` is allowlisted."""
    overlay = PolicyOverlay(
        allowlist=frozenset({"shell_command"}),
        extra_requires_approval=frozenset(),
        blocklist=frozenset(),
    )
    decision = overlay.evaluate("shell_command", {"command": "rm -rf /opt"})
    assert decision.action == "ask_operator"
    assert "destructive" in decision.reason


def test_overlay_gating_wins_over_overlay_allowlist() -> None:
    """If a tool is in BOTH overlay sets, gating wins (safer)."""
    overlay = PolicyOverlay(
        allowlist=frozenset({RENDER}),
        extra_requires_approval=frozenset({RENDER}),
        blocklist=frozenset(),
    )
    decision = overlay.evaluate(RENDER, {})
    assert decision.action == "ask_operator"


def test_blocklist_is_highest_precedence() -> None:
    overlay = PolicyOverlay(
        allowlist=frozenset({"some_tool"}),
        extra_requires_approval=frozenset({"some_tool"}),
        blocklist=frozenset({"some_tool"}),
    )
    decision = overlay.evaluate("some_tool", {})
    assert decision.action == "block"
    assert "blocklist" in decision.reason


# ---------------------------------------------------------------------------
# Safe tools and the read shell path still flow through the engine
# ---------------------------------------------------------------------------


def test_overlay_delegates_safe_shell(operator_overlay: PolicyOverlay) -> None:
    """A read-only shell command still flows through to the engine's allow."""
    decision = operator_overlay.evaluate("shell_command", {"command": "git status"})
    assert decision.action == "allow"


def test_overlay_delegates_in_code_allowlist(
    operator_overlay: PolicyOverlay,
) -> None:
    """Tools on the in-code ALLOWLIST keep working under the overlay."""
    decision = operator_overlay.evaluate("read_file", {"path": "/x"})
    assert decision.action == "allow"


def test_overlay_unknown_tool_fails_closed(
    operator_overlay: PolicyOverlay,
) -> None:
    decision = operator_overlay.evaluate("totally_made_up", {})
    assert decision.action == "ask_operator"
    assert decision.risk_class == "unknown"


# ---------------------------------------------------------------------------
# Empty overlay == pure pass-through (Ekko's behavior is unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool, args",
    [
        ("read_file", {"path": "/x"}),
        ("kie_generate", {"prompt": "x"}),
        ("shell_command", {"command": "git status"}),
        ("shell_command", {"command": "rm -rf /"}),
        ("delete_file", {"path": "/x"}),
        ("send_email", {"to": "x"}),
        ("totally_made_up", {}),
        (RENDER, {}),  # unknown to the base engine → asks
    ],
)
def test_empty_overlay_matches_plain_evaluate(tool: str, args: dict) -> None:
    """With an empty policy the overlay decision equals ``policy.evaluate``."""
    overlay = load_overlay(None)
    assert overlay.evaluate(tool, args) == evaluate(tool, args)


def test_missing_file_yields_empty_overlay(tmp_path: Path) -> None:
    overlay = load_overlay(tmp_path / "does-not-exist.yaml")
    assert overlay.allowlist == frozenset()
    assert overlay.extra_requires_approval == frozenset()
    assert overlay.blocklist == frozenset()


# ---------------------------------------------------------------------------
# The dependency-free YAML-subset parser (no PyYAML required)
# ---------------------------------------------------------------------------


def test_subset_parser_block_and_inline(tmp_path: Path) -> None:
    """The hand parser handles block lists, inline ``[]``, and comments."""
    from voxhorizon_approvals.policy_overlay import _parse_policy_subset

    text = (
        "# a comment\n"
        "extra_requires_approval:\n"
        f"  - {RENDER}  # gate spend\n"
        "allowlist:\n"
        f"  - {READ}\n"
        f"  - '{BRIEF}'\n"
        "blocklist: []\n"
    )
    parsed = _parse_policy_subset(text)
    assert parsed["extra_requires_approval"] == [RENDER]
    assert parsed["allowlist"] == [READ, BRIEF]
    assert parsed["blocklist"] == []


def test_subset_parser_inline_list(tmp_path: Path) -> None:
    from voxhorizon_approvals.policy_overlay import _parse_policy_subset

    parsed = _parse_policy_subset('allowlist: [a, "b", c]\n')
    assert parsed["allowlist"] == ["a", "b", "c"]


def test_subset_parser_ignores_unknown_keys() -> None:
    from voxhorizon_approvals.policy_overlay import _parse_policy_subset

    parsed = _parse_policy_subset("bogus_key: [x]\nallowlist: [y]\n")
    assert "bogus_key" not in parsed
    assert parsed["allowlist"] == ["y"]
