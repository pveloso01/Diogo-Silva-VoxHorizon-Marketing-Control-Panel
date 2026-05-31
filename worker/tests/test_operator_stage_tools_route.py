"""Contract tests for the P3 operator stage-persist routes.

Covers the five endpoints on the operator-stage-tools router with the full
matrix the rebuild requires (happy / 401 / 422 / idempotency / error):

  * POST /work/pipeline/tools/copy            — upsert copy_variants + arm gate.
  * POST /work/pipeline/tools/spec_result     — write spec_check + roll gate.
  * POST /work/pipeline/tools/finalize_result — record creatives finalize cols.
  * POST /work/pipeline/tools/monitor_result  — campaign_perf_image + cpl_real.
  * POST /work/pipeline/tools/signal          — operator_dispatches lifecycle.

Uses the shared harness (``client`` + ``fake_supabase``) from conftest.py. The
fake is additionally installed on the ``operator_stage_tools`` module here
(conftest patches the pipeline_tools/runner modules; this route is new).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import FakeSupabase, SHARED_SECRET


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {SHARED_SECRET}"}


def _pipeline_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "p-1",
        "status": "copy",
        "format_choice": "image",
        "client_id": "c-1",
        "image_brief_id": "ib-1",
        "video_brief_id": None,
        "config_draft": {},
        "picks": {"image": ["cr-1"], "video": []},
        "advanced_at": {},
        "created_at": "2026-05-22T00:00:00Z",
    }
    row.update(overrides)
    return row


@pytest.fixture
def stage_sb(fake_supabase: FakeSupabase, monkeypatch: pytest.MonkeyPatch) -> FakeSupabase:
    """The shared fake, also installed on the new operator_stage_tools module."""
    from src.routes import operator_stage_tools

    monkeypatch.setattr(operator_stage_tools, "get_supabase_admin", lambda: fake_supabase)
    return fake_supabase


# ===========================================================================
# POST /work/pipeline/tools/copy
# ===========================================================================


def test_copy_requires_auth(client: TestClient) -> None:
    resp = client.post("/work/pipeline/tools/copy", json={})
    assert resp.status_code == 401


def test_copy_422_on_empty_variants(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/copy",
        headers=_auth(),
        json={"pipeline_id": "p-1", "variants": []},
    )
    assert resp.status_code == 422


def test_copy_404_when_pipeline_missing(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", None)
    resp = client.post(
        "/work/pipeline/tools/copy",
        headers=_auth(),
        json={
            "pipeline_id": "ghost",
            "variants": [{"creative_id": "cr-1", "headline": "h"}],
        },
    )
    assert resp.status_code == 404


def test_copy_happy_inserts_variants_and_arms_gate(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/copy",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "variants": [
                {
                    "creative_id": "cr-1",
                    "platform": "meta",
                    "variant_index": 1,
                    "headline": "New roof, no surprises",
                    "primary_text": "Storm damage? We handle the claim.",
                    "cta": "Get a free inspection",
                    "validation": {"headline_chars": 23},
                },
                {
                    "creative_id": "cr-1",
                    "platform": "meta",
                    "variant_index": 2,
                    "headline": "Roof leak? Act fast",
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert len(body["variants"]) == 2
    # One creative, two variants -> one rollup entry armed to in_progress.
    assert len(body["rollup"]) == 1
    assert body["rollup"][0]["stage_state"] == "in_progress"
    assert body["rollup"][0]["variant_count"] == 2

    inserted_tables = [t for t, _ in stage_sb.inserts]
    assert inserted_tables.count("copy_variants") == 2
    assert "creative_stage_state" in inserted_tables
    # Primary text maps onto the back-compat `body` column.
    copy_rows = [r for t, r in stage_sb.inserts if t == "copy_variants"]
    assert copy_rows[0]["body"] == "Storm damage? We handle the claim."
    assert copy_rows[0]["status"] == "draft"


def test_copy_idempotent_updates_existing_variant(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    # Pre-seed an existing variant + gate row for the unique key.
    stage_sb.seed(
        "copy_variants",
        [{"id": "cv-existing", "creative_id": "cr-1", "platform": "meta", "variant_index": 1}],
    )
    stage_sb.seed(
        "creative_stage_state",
        [{"id": "css-existing", "creative_id": "cr-1", "stage": "copy", "status": "in_progress"}],
    )
    resp = client.post(
        "/work/pipeline/tools/copy",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "variants": [{"creative_id": "cr-1", "variant_index": 1, "headline": "rev"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variants"][0]["copy_variant_id"] == "cv-existing"
    # No new copy_variants insert — it was an update of the existing row.
    assert all(t != "copy_variants" for t, _ in stage_sb.inserts)
    updated = [(t, r) for t, r in stage_sb.updates if t == "copy_variants"]
    assert updated and updated[0][1]["headline"] == "rev"


def test_copy_routes_video_creative_to_video_table(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """A video creative's copy is written to video_copy_variants, not copy_variants."""
    stage_sb.set_single("pipelines", _pipeline_row())
    stage_sb.seed("video_creatives", [{"id": "vc-1", "brief_id": "vb-1"}])
    resp = client.post(
        "/work/pipeline/tools/copy",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "variants": [
                {
                    "creative_id": "vc-1",
                    "headline": "One storm from a leak",
                    "primary_text": "Book your $99 inspection.",
                    "cta": "Book now",
                    "validation": {"humanized": True},
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    inserted = [t for t, _ in stage_sb.inserts]
    assert "video_copy_variants" in inserted
    assert "copy_variants" not in inserted
    vrows = [r for t, r in stage_sb.inserts if t == "video_copy_variants"]
    assert vrows[0]["body"] == "Book your $99 inspection."
    assert vrows[0]["humanized"] is True
    assert vrows[0]["status"] == "draft"
    # The per-creative gate still arms (format-agnostic).
    assert any(t == "creative_stage_state" for t, _ in stage_sb.inserts)
    assert resp.json()["rollup"][0]["stage_state"] == "in_progress"


def test_copy_video_upserts_existing(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """Re-persisting a video creative's copy UPDATES in place (0031 unique key)."""
    stage_sb.set_single("pipelines", _pipeline_row())
    stage_sb.seed("video_creatives", [{"id": "vc-1", "brief_id": "vb-1"}])
    # Existing row for the unique key (creative_id, platform, variant_index).
    stage_sb.seed(
        "video_copy_variants",
        [{"id": "vcv-1", "creative_id": "vc-1", "platform": "meta", "variant_index": 1}],
    )
    resp = client.post(
        "/work/pipeline/tools/copy",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "variants": [
                {
                    "creative_id": "vc-1",
                    "platform": "meta",
                    "variant_index": 1,
                    "headline": "rev",
                    "primary_text": "x",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["variants"][0]["copy_variant_id"] == "vcv-1"
    # Updated the existing row in place; no new insert.
    assert all(t != "video_copy_variants" for t, _ in stage_sb.inserts)
    vupd = [r for t, r in stage_sb.updates if t == "video_copy_variants"]
    assert vupd and vupd[0]["headline"] == "rev"


# ===========================================================================
# POST /work/pipeline/tools/spec_result
# ===========================================================================


def test_spec_happy_passes_when_all_placements_pass(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"creative_id": "cr-1", "placement": "feed", "ratio": "1x1", "status": "pass"},
                {"creative_id": "cr-1", "placement": "stories", "ratio": "9x16", "status": "pass"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 2
    assert body["rollup"][0]["stage_state"] == "passed"
    assert [t for t, _ in stage_sb.inserts].count("spec_check") == 2


def test_spec_failing_placement_holds_gate_failed(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"creative_id": "cr-1", "placement": "feed", "status": "pass"},
                {"creative_id": "cr-1", "placement": "stories", "status": "fail"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    # A failing placement must NOT auto-pass the gate — it holds it failed.
    assert resp.json()["rollup"][0]["stage_state"] == "failed"


def test_spec_warn_holds_gate_in_progress(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [{"creative_id": "cr-1", "placement": "feed", "status": "warn"}],
        },
    )
    assert resp.json()["rollup"][0]["stage_state"] == "in_progress"


def test_spec_422_on_bad_status(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [{"creative_id": "cr-1", "placement": "feed", "status": "bogus"}],
        },
    )
    assert resp.status_code == 422


def test_spec_422_on_bad_placement(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    # An out-of-vocabulary placement (the operator inventing "feed_square") is a
    # clean 422 at the API boundary, NOT a raw 500 when the value would hit the
    # Postgres placement_enum column. Regression for the spec-stage outage.
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"creative_id": "cr-1", "placement": "feed_square", "ratio": "1x1", "status": "pass"}
            ],
        },
    )
    assert resp.status_code == 422


def test_spec_422_on_bad_ratio(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"creative_id": "cr-1", "placement": "feed", "ratio": "2x3", "status": "pass"}
            ],
        },
    )
    assert resp.status_code == 422


def test_spec_idempotent_updates_existing_check(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    stage_sb.seed(
        "spec_check",
        [{"id": "sc-1", "creative_id": "cr-1", "platform": "meta", "placement": "feed"}],
    )
    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [{"creative_id": "cr-1", "placement": "feed", "status": "pass"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["spec_check_id"] == "sc-1"
    assert all(t != "spec_check" for t, _ in stage_sb.inserts)


# ===========================================================================
# POST /work/pipeline/tools/finalize_result
# ===========================================================================


def test_finalize_happy_records_creative_cols(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    stage_sb.seed("creatives", [{"id": "cr-1", "finalize_verified": False}])
    resp = client.post(
        "/work/pipeline/tools/finalize_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {
                    "creative_id": "cr-1",
                    "asset_name": "2026-05-22 | Roof Trust v1 | $99",
                    "drive_folder_id": "drv-folder-1",
                    "file_path_drive": "drive://acme/roof-trust-v1.png",
                    "verified": True,
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recorded"][0]["creative_id"] == "cr-1"
    assert body["skipped"] == []
    updates = [r for t, r in stage_sb.updates if t == "creatives"]
    assert updates and updates[0]["finalize_verified"] is True
    assert updates[0]["drive_folder_id"] == "drv-folder-1"
    assert "finalized_at" in updates[0]


def test_finalize_skips_already_verified(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    stage_sb.seed("creatives", [{"id": "cr-1", "finalize_verified": True}])
    resp = client.post(
        "/work/pipeline/tools/finalize_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [{"creative_id": "cr-1", "asset_name": "x", "verified": True}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recorded"] == []
    assert body["skipped"] == ["cr-1"]
    assert all(t != "creatives" for t, _ in stage_sb.updates)


def test_finalize_404_on_missing_creative(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    # No creatives seeded -> maybe_single returns None -> 404.
    resp = client.post(
        "/work/pipeline/tools/finalize_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [{"creative_id": "ghost", "asset_name": "x", "verified": True}],
        },
    )
    assert resp.status_code == 404


def test_finalize_422_on_missing_asset_name(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/finalize_result",
        headers=_auth(),
        json={"pipeline_id": "p-1", "results": [{"creative_id": "cr-1"}]},
    )
    assert resp.status_code == 422


# ===========================================================================
# POST /work/pipeline/tools/monitor_result
# ===========================================================================


def test_monitor_happy_computes_cpl_real(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    resp = client.post(
        "/work/pipeline/tools/monitor_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {
                    "campaign_id": "camp-1",
                    "ad_entity_id": "ae-1",
                    "window_days": 30,
                    "spend": 300.0,
                    "ghl_leads": 12,
                    "ctr": 0.018,
                    "verdict": "keep",
                    "verdict_reason": "CPL under target",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Real CPL = spend / GHL leads = 300 / 12 = 25.0 (GHL is lead truth).
    assert body["recorded"][0]["cpl_real"] == 25.0
    assert body["tally"] == {"kill": 0, "watch": 0, "keep": 1}
    perf = [r for t, r in stage_sb.inserts if t == "campaign_perf_image"]
    assert perf and perf[0]["cpl_real"] == 25.0
    assert perf[0]["leads_ghl"] == 12
    assert perf[0]["pipeline_id"] == "p-1"


def test_monitor_zero_ghl_leads_cpl_is_none(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    resp = client.post(
        "/work/pipeline/tools/monitor_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"campaign_id": "camp-1", "window_days": 7, "spend": 80.0, "ghl_leads": 0, "verdict": "kill"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    # No leads -> undefined CPL (None), never a divide-by-zero.
    assert resp.json()["recorded"][0]["cpl_real"] is None


def test_monitor_idempotent_skips_today(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    from datetime import datetime, timezone

    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    today = datetime.now(timezone.utc).date().isoformat()
    stage_sb.seed(
        "campaign_perf_image",
        [
            {
                "id": "perf-old",
                "client_id": "c-1",
                "campaign_id": "camp-1",
                "window_days": 30,
                "pulled_at": f"{today}T01:00:00+00:00",
            }
        ],
    )
    resp = client.post(
        "/work/pipeline/tools/monitor_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "client_id": "c-1",
            "results": [
                {"campaign_id": "camp-1", "window_days": 30, "spend": 300.0, "ghl_leads": 10, "verdict": "keep"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == ["camp-1"]
    assert body["recorded"] == []


def test_monitor_422_on_bad_verdict(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    resp = client.post(
        "/work/pipeline/tools/monitor_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [{"campaign_id": "c", "window_days": 7, "verdict": "explode"}],
        },
    )
    assert resp.status_code == 422


# ===========================================================================
# POST /work/pipeline/tools/monitor_action_result (monitor connector)
# ===========================================================================


def test_monitor_action_requires_auth(client: TestClient) -> None:
    resp = client.post("/work/pipeline/tools/monitor_action_result", json={})
    assert resp.status_code == 401


def test_monitor_action_404_when_pipeline_missing(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", None)
    resp = client.post(
        "/work/pipeline/tools/monitor_action_result",
        headers=_auth(),
        json={
            "pipeline_id": "ghost",
            "result": {"decision": "kill", "campaign_id": "camp-1"},
        },
    )
    assert resp.status_code == 404


def test_monitor_action_kill_records_pause(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """A kill records a monitor_action_result row with no budget (a pause)."""
    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    resp = client.post(
        "/work/pipeline/tools/monitor_action_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "client_id": "c-1",
            "result": {
                "decision": "kill",
                "campaign_id": "camp-1",
                "ad_entity_id": "ae-1",
                "approved_by": "manager@vox",
                "notes": "CPL 3x target, zero GHL leads after $120 spend",
                "meta_payload": {"status": "PAUSED"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["decision"] == "kill"
    rows = [r for t, r in stage_sb.inserts if t == "monitor_action_result"]
    assert rows and rows[0]["decision"] == "kill"
    assert rows[0]["campaign_id"] == "camp-1"
    assert rows[0]["ad_entity_id"] == "ae-1"
    assert rows[0]["approved_by"] == "manager@vox"
    # A kill is a pause -> no target_budget on the row.
    assert "target_budget" not in rows[0]
    # The executed action is surfaced on the timeline.
    events = [r for t, r in stage_sb.inserts if t == "pipeline_events"]
    assert any(e["kind"] == "monitor_action_recorded" for e in events)


def test_monitor_action_scale_records_target_budget(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """A scale records the new daily_budget the operator wrote to Meta."""
    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    resp = client.post(
        "/work/pipeline/tools/monitor_action_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "result": {
                "decision": "scale",
                "campaign_id": "camp-1",
                "target_budget": 7500,
                "approved_by": "manager@vox",
                "meta_payload": {"daily_budget": 7500},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "scale"
    rows = [r for t, r in stage_sb.inserts if t == "monitor_action_result"]
    assert rows and rows[0]["decision"] == "scale"
    assert rows[0]["target_budget"] == 7500


def test_monitor_action_scale_without_budget_is_422(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """A scale MUST carry the new budget -- omitting it is a 422."""
    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    resp = client.post(
        "/work/pipeline/tools/monitor_action_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "result": {"decision": "scale", "campaign_id": "camp-1"},
        },
    )
    assert resp.status_code == 422


def test_monitor_action_422_on_bad_decision(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    resp = client.post(
        "/work/pipeline/tools/monitor_action_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "result": {"decision": "explode", "campaign_id": "camp-1"},
        },
    )
    assert resp.status_code == 422


def test_monitor_action_422_on_nonpositive_budget(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row(status="monitor"))
    resp = client.post(
        "/work/pipeline/tools/monitor_action_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "result": {"decision": "scale", "campaign_id": "camp-1", "target_budget": 0},
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Video routing (VID-12): finalize -> video_creatives, monitor -> campaign_perf_video
# ---------------------------------------------------------------------------


def test_finalize_routes_video_creative_to_video_table(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """A video creative's finalize report writes video_creatives, not creatives."""
    stage_sb.set_single("pipelines", _pipeline_row(format_choice="video"))
    stage_sb.seed("video_creatives", [{"id": "vc-1", "finalize_verified": False}])
    resp = client.post(
        "/work/pipeline/tools/finalize_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {
                    "creative_id": "vc-1",
                    "asset_name": "2026-05-24 | Storm Leak v1 | $99",
                    "file_path_drive": "drive://acme/storm-leak-v1.mp4",
                    "verified": True,
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"][0]["creative_id"] == "vc-1"
    vupd = [r for t, r in stage_sb.updates if t == "video_creatives"]
    assert vupd and vupd[0]["finalize_verified"] is True
    assert vupd[0]["file_path_drive"] == "drive://acme/storm-leak-v1.mp4"
    assert all(t != "creatives" for t, _ in stage_sb.updates)


def test_finalize_video_skips_already_verified(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row(format_choice="video"))
    stage_sb.seed("video_creatives", [{"id": "vc-1", "finalize_verified": True}])
    resp = client.post(
        "/work/pipeline/tools/finalize_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [{"creative_id": "vc-1", "asset_name": "x", "verified": True}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] == ["vc-1"]
    assert all(t != "video_creatives" for t, _ in stage_sb.updates)


def test_monitor_routes_video_pipeline_to_campaign_perf_video(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """A video pipeline's monitor read writes campaign_perf_video + the funnel."""
    stage_sb.set_single(
        "pipelines", _pipeline_row(format_choice="video", status="monitor")
    )
    resp = client.post(
        "/work/pipeline/tools/monitor_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {
                    "campaign_id": "camp-1",
                    "window_days": 7,
                    "spend": 50.0,
                    "ghl_leads": 5,
                    "verdict": "keep",
                    "hook_rate": 0.32,
                    "watch_time_p50": 6.5,
                    "completion_p100": 0.18,
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"][0]["cpl_real"] == 10.0
    vins = [r for t, r in stage_sb.inserts if t == "campaign_perf_video"]
    assert vins, "expected a campaign_perf_video insert"
    assert vins[0]["hook_rate"] == 0.32
    assert vins[0]["watch_time_p50"] == 6.5
    assert vins[0]["completion_p100"] == 0.18
    assert all(t != "campaign_perf_image" for t, _ in stage_sb.inserts)


# ===========================================================================
# POST /work/pipeline/tools/signal
# ===========================================================================


def _signal_event_inserts(sb: FakeSupabase) -> list[dict[str, object]]:
    return [r for t, r in sb.inserts if t == "pipeline_events"]


def test_signal_opens_dispatch_row(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/signal",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "dispatch_id": "d-1",
            "stage": "copy",
            "status": "running",
            "expected_status": "copy",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["terminal"] is False
    # Silent-failure PR-4: the signal lands on the pipeline_events audit log,
    # NOT on the work_item queue. Treating signals as work_items would cause
    # the operator-daemon to re-claim every audit entry and spawn an empty
    # hermes chat -- the silent-failure class the redesign exists to close.
    inserted = _signal_event_inserts(stage_sb)
    assert inserted and inserted[0]["kind"] == "operator_signal"
    assert inserted[0]["stage"] == "copy"
    assert inserted[0]["payload"]["db_status"] == "running"
    assert inserted[0]["payload"]["signal"] == "running"
    assert inserted[0]["payload"]["dispatch_id"] == "d-1"
    # Negative: no work_item was enqueued.
    assert [r for t, r in stage_sb.inserts if t == "work_item"] == []


def test_signal_terminal_close_marks_terminal_payload(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/signal",
        headers=_auth(),
        json={"pipeline_id": "p-1", "dispatch_id": "d-2", "status": "completed", "stage": "copy"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["terminal"] is True
    inserted = _signal_event_inserts(stage_sb)
    assert inserted and inserted[0]["payload"]["terminal"] is True
    assert inserted[0]["kind"] == "operator_signal"


def test_signal_stale_maps_to_completed(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/signal",
        headers=_auth(),
        json={"pipeline_id": "p-1", "dispatch_id": "d-3", "status": "stale", "stage": "copy"},
    )
    # A stale/duplicate dispatch is a no-op that is "done".
    assert resp.json()["status"] == "completed"


def test_signal_error_maps_to_failed(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/signal",
        headers=_auth(),
        json={"pipeline_id": "p-1", "dispatch_id": "d-4", "status": "error", "error": "read failed"},
    )
    body = resp.json()
    assert body["status"] == "failed"
    assert body["terminal"] is True


def test_signal_is_append_only_audit(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """A repeated signal lands as a repeated audit entry (no de-dup).

    Signals are AUDIT events, not work units -- a repeated narration verb
    from the operator skill is a legitimate second audit entry, not a
    duplicate to suppress. Idempotency would be wrong semantics here.
    """
    stage_sb.set_single("pipelines", _pipeline_row())
    for _ in range(2):
        resp = client.post(
            "/work/pipeline/tools/signal",
            headers=_auth(),
            json={"pipeline_id": "p-1", "dispatch_id": "d-5", "status": "completed", "stage": "copy"},
        )
        assert resp.status_code == 200, resp.text
    inserted = _signal_event_inserts(stage_sb)
    assert len(inserted) == 2
    # Both entries have the same payload shape; both are operator_signal.
    assert all(r["kind"] == "operator_signal" for r in inserted)
    assert all(r["payload"]["dispatch_id"] == "d-5" for r in inserted)


def test_signal_422_on_bad_status(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    stage_sb.set_single("pipelines", _pipeline_row())
    resp = client.post(
        "/work/pipeline/tools/signal",
        headers=_auth(),
        json={"pipeline_id": "p-1", "dispatch_id": "d-6", "status": "made-up"},
    )
    assert resp.status_code == 422


def test_signal_requires_auth(client: TestClient) -> None:
    resp = client.post("/work/pipeline/tools/signal", json={})
    assert resp.status_code == 401


# ===========================================================================
# GET /work/pipeline/tools/{id} — the extended per-creative stage_state rollup
# ===========================================================================


def test_read_returns_per_creative_stage_rollup(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """The read tool now carries a per-creative gate rollup so the operator
    resumes the per-creative stages by skip-done."""
    # No brief/client to keep the read narrow; the rollup is the assertion.
    stage_sb.set_single(
        "pipelines",
        _pipeline_row(status="creative_qa", image_brief_id=None, client_id=None),
    )
    stage_sb.seed(
        "creative_stage_state",
        [
            # _fetch_creative_rollup filters by pipeline_id (= the path id "p-1"),
            # so the seeded rows must carry it or the rollup comes back empty.
            {"pipeline_id": "p-1", "creative_id": "cr-1", "stage": "creative_qa", "status": "passed"},
            {"pipeline_id": "p-1", "creative_id": "cr-1", "stage": "copy", "status": "pending"},
            {"pipeline_id": "p-1", "creative_id": "cr-2", "stage": "creative_qa", "status": "failed"},
        ],
    )
    stage_sb.seed(
        "creatives",
        [
            {"id": "cr-1", "status": "draft"},
            {"id": "cr-2", "status": "draft"},
        ],
    )
    resp = client.get("/work/pipeline/tools/p-1", headers=_auth())
    assert resp.status_code == 200, resp.text
    rollup = {c["creative_id"]: c for c in resp.json()["creatives"]}
    assert rollup["cr-1"]["stage_state"]["creative_qa"] == "passed"
    assert rollup["cr-1"]["stage_state"]["copy"] == "pending"
    assert rollup["cr-2"]["stage_state"]["creative_qa"] == "failed"
    assert rollup["cr-1"]["status"] == "draft"


def test_read_rollup_empty_when_no_stage_state(
    client: TestClient, stage_sb: FakeSupabase
) -> None:
    """A pre-QA pipeline has no per-creative state -> the rollup degrades to []."""
    stage_sb.set_single(
        "pipelines",
        _pipeline_row(status="ideation", image_brief_id=None, client_id=None),
    )
    resp = client.get("/work/pipeline/tools/p-1", headers=_auth())
    assert resp.status_code == 200, resp.text
    assert resp.json()["creatives"] == []


# ===========================================================================
# Spec backstop for VIDEO creatives (E3.3 #492)
# ===========================================================================


def _install_spec_probe(monkeypatch: pytest.MonkeyPatch, probe) -> None:  # noqa: ANN001
    """Patch ``operator_stage_tools.video_probe.probe_video`` to return ``probe``."""
    from src.routes import operator_stage_tools

    async def _fake_probe(path, *, ffprobe_bin=None):  # noqa: ANN001
        return probe

    monkeypatch.setattr(operator_stage_tools.video_probe, "probe_video", _fake_probe)


def _conformant_reel_probe():
    from src.services import video_probe as vp

    return vp.parse_probe(
        {
            "format": {"format_name": "mov,mp4,m4a", "duration": "18.5"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30/1",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
    )


def _nonconformant_probe():
    """Wrong ratio (1:1 on a 9:16 reel rail) + wrong codec -> a hard spec fail."""
    from src.services import video_probe as vp

    return vp.parse_probe(
        {
            "format": {"format_name": "webm", "duration": "18.5"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "vp9",
                    "width": 1080,
                    "height": 1080,
                },
                {"codec_type": "audio", "codec_name": "opus"},
            ],
        }
    )


def test_spec_backstop_downgrades_operator_pass_on_bad_video(
    client: TestClient, stage_sb: FakeSupabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3.3 INVARIANT: an operator 'pass' on a non-conformant video is downgraded.

    The operator submits ``status='pass'`` for a reels placement, but the actual
    asset is a webm/vp9 1:1 (wrong container, codec, and ratio). The worker
    recomputes from the probed facts and DOWNGRADES the persisted status to
    ``fail`` -- the operator can never pass a non-conformant asset.
    """
    stage_sb.set_single("pipelines", _pipeline_row(format_choice="video"))
    stage_sb.seed(
        "video_creatives",
        [{"id": "vc-1", "captioned_path": "vb-1/cap.mp4", "composed_path": None}],
    )
    stage_sb.set_storage_object("vb-1/cap.mp4", b"\x00fake")
    _install_spec_probe(monkeypatch, _nonconformant_probe())

    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"creative_id": "vc-1", "placement": "reels", "status": "pass"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The gate is held failed, not passed.
    assert body["rollup"][0]["stage_state"] == "failed"
    written = body["results"][0]
    assert written["status"] == "fail"
    assert written["submitted_status"] == "pass"
    assert written["backstop_downgraded"] is True

    # The persisted spec_check row carries the worker recompute evidence.
    spec_rows = [r for t, r in stage_sb.inserts if t == "spec_check"]
    assert spec_rows and spec_rows[0]["status"] == "fail"
    assert spec_rows[0]["checks"]["worker_backstop"]["backstop"] == "fail"


def test_spec_backstop_keeps_operator_pass_on_good_video(
    client: TestClient, stage_sb: FakeSupabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conformant video keeps the operator-submitted pass (backstop only tightens)."""
    stage_sb.set_single("pipelines", _pipeline_row(format_choice="video"))
    stage_sb.seed(
        "video_creatives",
        [{"id": "vc-1", "captioned_path": "vb-1/cap.mp4", "composed_path": None}],
    )
    stage_sb.set_storage_object("vb-1/cap.mp4", b"\x00fake")
    _install_spec_probe(monkeypatch, _conformant_reel_probe())

    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"creative_id": "vc-1", "placement": "reels", "status": "pass"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rollup"][0]["stage_state"] == "passed"
    assert body["results"][0]["status"] == "pass"
    assert body["results"][0]["backstop_downgraded"] is False


def test_spec_backstop_does_not_touch_image_creatives(
    client: TestClient, stage_sb: FakeSupabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image creative (absent from video_creatives) keeps the operator status."""
    stage_sb.set_single("pipelines", _pipeline_row())

    called = {"probe": False}

    async def _should_not_run(path, *, ffprobe_bin=None):  # noqa: ANN001
        called["probe"] = True
        raise AssertionError("probe must not run for an image creative")

    from src.routes import operator_stage_tools

    monkeypatch.setattr(operator_stage_tools.video_probe, "probe_video", _should_not_run)

    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [{"creative_id": "cr-1", "placement": "feed", "status": "pass"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "pass"
    assert called["probe"] is False


def test_spec_backstop_unverifiable_video_downgrades_to_fail(
    client: TestClient, stage_sb: FakeSupabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe failure on a video asset downgrades to fail (never trust unverifiable)."""
    stage_sb.set_single("pipelines", _pipeline_row(format_choice="video"))
    stage_sb.seed(
        "video_creatives",
        [{"id": "vc-1", "captioned_path": "vb-1/cap.mp4", "composed_path": None}],
    )
    stage_sb.set_storage_object("vb-1/cap.mp4", b"\x00fake")

    from src.routes import operator_stage_tools
    from src.services import video_probe as vp

    async def _raise_probe(path, *, ffprobe_bin=None):  # noqa: ANN001
        raise vp.ProbeError("corrupt asset")

    monkeypatch.setattr(operator_stage_tools.video_probe, "probe_video", _raise_probe)

    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"creative_id": "vc-1", "placement": "reels", "status": "pass"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "fail"


def test_spec_backstop_unknown_placement_keeps_status(
    client: TestClient, stage_sb: FakeSupabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A placement with no spec entry keeps the operator status (recompute skipped)."""
    stage_sb.set_single("pipelines", _pipeline_row(format_choice="video"))
    stage_sb.seed(
        "video_creatives",
        [{"id": "vc-1", "captioned_path": "vb-1/cap.mp4", "composed_path": None}],
    )
    _install_spec_probe(monkeypatch, _conformant_reel_probe())

    resp = client.post(
        "/work/pipeline/tools/spec_result",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "results": [
                {"creative_id": "vc-1", "placement": "search", "status": "pass"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "pass"
