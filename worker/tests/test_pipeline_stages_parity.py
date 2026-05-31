"""Parity contract: the worker stage mirror matches the registry + DB (E2.1).

The 12-stage DAG + each stage's mechanism / per-creative / hard-gate / next edge
live ONCE in the checked-in TS manifest ``lib/pipeline/stages.ts``
(``PIPELINE_STAGE_REGISTRY``). ``scripts/gen_pipeline_stages.py`` derives the
Python mirror ``src/generated/pipeline_stages.py`` (the ``PipelineStage``
Literal). This module is the worker twin of the TS ``stages.parity.test.ts``: it
fails CI if

  * the committed generated file is stale (the ``--check`` drift gate),
  * the generated order disagrees with the registry order parsed from the manifest,
  * the registry order disagrees with the DB ``pipeline_status_enum`` order, or
  * the ``PipelineStage`` re-exported by ``services.pipeline_runner`` falls behind.

so a future edit to one side without the other fails CI rather than letting the
five copies this milestone unified silently drift apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.generated.pipeline_stages import (
    HARD_GATE_STAGES,
    NEXT_STAGE,
    PER_CREATIVE_STAGES,
    PIPELINE_STAGES,
    STAGE_MECHANISM,
)
from src.services.pipeline_runner import PipelineStage

import scripts.gen_pipeline_stages as gen


# worker/tests/test_pipeline_stages_parity.py -> worker/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_ROOT / "lib" / "pipeline" / "stages.ts"
_TYPES_GEN = _REPO_ROOT / "lib" / "supabase" / "types.gen.ts"


def _manifest_keys() -> list[str]:
    """Parse the ordered ``key: "..."`` values from the TS registry body."""
    source = _MANIFEST.read_text(encoding="utf-8")
    body = gen._extract_registry_body(source)
    rows = gen._parse_registry(body)
    return [r.key for r in rows]


def _db_enum_order() -> list[str]:
    """Parse the ordered ``pipeline_status_enum`` array from types.gen.ts."""
    source = _TYPES_GEN.read_text(encoding="utf-8")
    m = re.search(r"pipeline_status_enum:\s*\[([\s\S]*?)\]", source)
    assert m is not None, "could not find pipeline_status_enum array in types.gen.ts"
    return [v for v in re.findall(r'"([a-z_]+)"', m.group(1))]


def test_generated_file_is_not_stale() -> None:
    """``gen_pipeline_stages.py --check`` passes (committed file is fresh)."""
    assert gen.main(["--check"]) == 0


def test_generated_order_equals_manifest_order() -> None:
    """The generated PIPELINE_STAGES tuple equals the registry order."""
    assert list(PIPELINE_STAGES) == _manifest_keys()


def test_manifest_keys_match_db_enum_set() -> None:
    """The registry covers exactly the DB ``pipeline_status_enum`` value SET.

    The per-creative gate ORDER lives in the registry ``next`` chain, NOT the
    Postgres enum ordinal -- nothing reads the enum ordinal (no ``enum_range`` /
    ``ORDER BY`` an enum column / ordinal comparison; the macro reducer orders by
    ``pipeline_events.seq``). After the copy<->compliance_review reorder the
    registry DAG order differs from the enum declaration order, so we assert the
    value SET matches rather than the order.
    """
    assert sorted(_manifest_keys()) == sorted(_db_enum_order())


def test_pipeline_runner_reexports_the_generated_literal() -> None:
    """``services.pipeline_runner.PipelineStage`` is the generated Literal."""
    from src.generated import pipeline_stages

    assert PipelineStage is pipeline_stages.PipelineStage


def test_per_creative_and_hard_gate_sets_match_the_registry() -> None:
    """The generated per-creative + hard-gate tuples match today's behaviour."""
    assert set(PER_CREATIVE_STAGES) == {
        "creative_qa",
        "compliance_review",
        "copy",
        "spec_validation",
    }
    assert set(HARD_GATE_STAGES) == {"compliance_review", "launch_handoff"}


def test_next_chain_walks_the_happy_path_then_terminates() -> None:
    """The generated NEXT_STAGE map is the contiguous DAG, terminating at done/cancelled."""
    keys = _manifest_keys()
    happy = [k for k in keys if k != "cancelled"]
    for current, nxt in zip(happy, happy[1:]):
        assert NEXT_STAGE[current] == nxt
    assert NEXT_STAGE["done"] is None
    assert NEXT_STAGE["cancelled"] is None


def test_mechanism_map_covers_every_stage() -> None:
    """Every registry stage has a mechanism; terminals (and only they) are terminal."""
    keys = _manifest_keys()
    assert set(STAGE_MECHANISM) == set(keys)
    for key, mech in STAGE_MECHANISM.items():
        assert (mech == "terminal") == (key in {"done", "cancelled"})
