"""Tests for the Wave A operator-tool routes.

Covers the endpoints on the pipeline-tools router:

  * GET  /work/pipeline/tools/{pipeline_id} -- read shape + compact client
                                              enrichment.
  * GET  /work/client/{client_id}           -- client-context shape + 404.
  * POST /work/pipeline/tools/brief         -- validate + upsert + event.
  * POST /work/pipeline/tools/render        -- events + cost + creatives,
                                              per-item error handling.

Silent-failure PR-4: the legacy `/work/pipeline/tools/dispatch` route + the
`operator_bridge` module it called are deleted. Operator dispatches now ride
the unified `work_item` queue (kind=operator_dispatch); the dashboard
enqueues via `lib/work-queue/enqueue.ts` synchronously and gets a 5xx on
failure -- the silent-failure class the bridge swallowed is structurally
closed.

Mirrors the test conventions in ``test_pipeline_route.py`` (a narrow
Supabase + Kie double, background tasks run synchronously by the
TestClient).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


SHARED_SECRET = "test-secret-for-pipeline-tools"


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provision env + reset cached settings, queue, and operator bridge."""
    monkeypatch.setenv("WORKER_SHARED_SECRET", SHARED_SECRET)
    monkeypatch.setenv("WORKER_CORS_ORIGIN", "http://localhost:3000")
    monkeypatch.setenv("WORKER_PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("BROLL_STORE_BACKEND", "local")
    monkeypatch.setenv("BROLL_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("TAILSCALE_HOSTNAME", "voxhorizon-worker-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")

    from src.config import get_settings
    from src.services.queue import reset_queue

    get_settings.cache_clear()
    reset_queue()
    yield
    get_settings.cache_clear()
    reset_queue()


# ---------------------------------------------------------------------------
# Supabase + Kie test doubles (modelled on test_pipeline_route.py)
# ---------------------------------------------------------------------------


class _ToolsSupabase:
    """Stand-in for the supabase-py client used by the operator tools.

    ``pipeline_row`` / ``brief_row`` set the rows returned on
    ``maybe_single`` selects; ``creatives_rows`` / ``events_rows`` back the
    multi-row selects the read path issues; ``rpc_return`` is what
    ``rpc('gen_brief_id_human')`` yields. Inserts / updates / uploads are
    captured for assertions.
    """

    def __init__(self) -> None:
        self.pipeline_row: dict | None = None
        self.brief_row: dict | None = None
        self.client_row: dict | None = None
        # Client-context tables (migration 0012). ``client_profile_row`` backs
        # the 1:1 maybe_single read; the rest back the child-table multi reads.
        self.client_profile_row: dict | None = None
        self.client_offers_rows: list[dict] = []
        self.client_offer_constraints_rows: list[dict] = []
        self.client_services_rows: list[dict] = []
        self.client_value_props_rows: list[dict] = []
        self.client_assets_rows: list[dict] = []
        self.client_past_projects_rows: list[dict] = []
        self.creatives_rows: list[dict] = []
        self.events_rows: list[dict] = []
        self.rpc_return: Any = "acme-2026-05-20-001"

        self.inserts: list[tuple[str, dict]] = []
        self.updates: list[tuple[str, dict]] = []
        self.storage_uploads: list[tuple[str, bytes]] = []
        self.rpc_calls: list[tuple[str, dict]] = []

    def table(self, name: str) -> "_ToolsTable":
        return _ToolsTable(self, name)

    def rpc(self, fn: str, params: dict) -> "_ToolsRpc":
        self.rpc_calls.append((fn, params))
        # Silent-failure PR-4: ``compute_pipeline_status`` (migration 0050) is
        # the canonical pipeline-status source after 0051 dropped the column.
        # The fake folds the pipeline_row's ``status`` field into the RPC
        # response so existing tests keep their `pipeline_row['status']`
        # seeds without growing a parallel rpc fixture.
        if fn == "compute_pipeline_status":
            status = None
            if isinstance(self.pipeline_row, dict):
                status = self.pipeline_row.get("status")
            return _ToolsRpc(status)
        return _ToolsRpc(self.rpc_return)

    @property
    def storage(self) -> "_ToolsStorage":
        return _ToolsStorage(self)


class _ToolsRpc:
    def __init__(self, value: Any) -> None:
        self._value = value

    def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=self._value)


class _ToolsTable:
    def __init__(self, sb: _ToolsSupabase, name: str) -> None:
        self.sb = sb
        self.name = name
        self._filters: list[tuple[str, str]] = []
        self._select: str | None = None
        self._insert_data: dict | None = None
        self._update_data: dict | None = None
        self._maybe_single = False
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    def select(self, columns: str) -> "_ToolsTable":
        self._select = columns
        return self

    def eq(self, col: str, val: str) -> "_ToolsTable":
        self._filters.append((col, val))
        return self

    def order(self, col: str, *, desc: bool = False) -> "_ToolsTable":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "_ToolsTable":
        self._limit = n
        return self

    def maybe_single(self) -> "_ToolsTable":
        self._maybe_single = True
        return self

    def insert(self, data: dict) -> "_ToolsTable":
        self._insert_data = data
        return self

    def update(self, data: dict) -> "_ToolsTable":
        self._update_data = data
        return self

    def execute(self) -> SimpleNamespace:
        if self._insert_data is not None:
            self.sb.inserts.append((self.name, self._insert_data))
            row = {
                **self._insert_data,
                "id": f"{self.name}-id-{len(self.sb.inserts)}",
            }
            return SimpleNamespace(data=[row])
        if self._update_data is not None:
            self.sb.updates.append((self.name, self._update_data))
            return SimpleNamespace(data=[{**self._update_data, "id": "u-id"}])

        # maybe_single selects:
        if self._maybe_single:
            if self.name == "pipelines":
                return SimpleNamespace(data=self.sb.pipeline_row)
            if self.name == "briefs":
                return SimpleNamespace(data=self.sb.brief_row)
            if self.name == "clients":
                return SimpleNamespace(data=self.sb.client_row)
            if self.name == "client_profiles":
                return SimpleNamespace(data=self.sb.client_profile_row)
            return SimpleNamespace(data=None)

        # multi-row selects:
        if self.name == "creatives":
            rows = [
                r
                for r in self.sb.creatives_rows
                if all(r.get(c) == v for c, v in self._filters)
            ]
            return SimpleNamespace(data=rows)

        # Client-context child tables (migration 0012). Each is filtered by the
        # eq() filters, then ordered by sort_order like the route issues it.
        _child_sources: dict[str, list[dict]] = {
            "client_offers": self.sb.client_offers_rows,
            "client_offer_constraints": self.sb.client_offer_constraints_rows,
            "client_services": self.sb.client_services_rows,
            "client_value_props": self.sb.client_value_props_rows,
            "client_assets": self.sb.client_assets_rows,
            "client_past_projects": self.sb.client_past_projects_rows,
        }
        if self.name in _child_sources or self.name == "pipeline_events":
            source = (
                self.sb.events_rows
                if self.name == "pipeline_events"
                else _child_sources[self.name]
            )
            rows = [
                r
                for r in source
                if all(r.get(c) == v for c, v in self._filters)
            ]
            if self._order:
                col, desc = self._order
                rows = sorted(rows, key=lambda r: r.get(col, ""), reverse=desc)
            if self._limit:
                rows = rows[: self._limit]
            return SimpleNamespace(data=rows)

        return SimpleNamespace(data=None)


class _ToolsStorage:
    def __init__(self, sb: _ToolsSupabase) -> None:
        self.sb = sb

    def from_(self, bucket: str) -> "_ToolsBucket":
        return _ToolsBucket(self.sb)


class _ToolsBucket:
    def __init__(self, sb: _ToolsSupabase) -> None:
        self.sb = sb

    def upload(self, *, path: str, file: bytes, file_options: dict) -> None:
        self.sb.storage_uploads.append((path, bytes(file)))


class _StubKieClient:
    """Drop-in for KieClient returning canned bytes + metadata."""

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def generate_image_full(
        self, prompt: str, ratio: str, *, resolution: str = "2K"
    ) -> Any:
        from src.services.kie import KieGenerationResult

        return KieGenerationResult(
            image_bytes=b"PNGBYTES",
            task_id=f"task-{ratio}",
            source_url=f"https://kie/{ratio}.png",
            aspect_ratio=ratio,
            resolution=resolution,
        )


class _FailKieClient(_StubKieClient):
    """Kie double that fails on the 9x16 ratio to exercise per-item errors."""

    async def generate_image_full(
        self, prompt: str, ratio: str, *, resolution: str = "2K"
    ) -> Any:
        from src.services.kie import KieError

        if ratio == "9x16":
            raise KieError("simulated kie failure")
        return await super().generate_image_full(
            prompt, ratio, resolution=resolution
        )


@pytest.fixture
def tools_sb(monkeypatch: pytest.MonkeyPatch) -> _ToolsSupabase:
    """Install the tools Supabase stub everywhere the routes read it."""
    sb = _ToolsSupabase()

    from src.routes import pipeline_tools
    from src.services import atomic_inserts, pipeline_runner

    monkeypatch.setattr(pipeline_tools, "get_supabase_admin", lambda: sb)
    monkeypatch.setattr(pipeline_runner, "get_supabase_admin", lambda: sb)
    monkeypatch.setattr(atomic_inserts, "get_supabase_admin", lambda: sb)
    return sb


@pytest.fixture
def client() -> TestClient:
    from src.main import create_app

    return TestClient(create_app())


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {SHARED_SECRET}"}


def _pipeline_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "p-1",
        "status": "ideation",
        "format_choice": "image",
        "client_id": "c-1",
        "image_brief_id": "ib-1",
        "video_brief_id": None,
        "config_draft": {},
        "picks": {"image": [], "video": []},
        "advanced_at": {},
        "created_at": "2026-05-20T00:00:00Z",
    }
    row.update(overrides)
    return row


# ===========================================================================
# GET /work/pipeline/tools/{pipeline_id}
# ===========================================================================


def test_read_requires_auth(client: TestClient) -> None:
    resp = client.get("/work/pipeline/tools/p-1")
    assert resp.status_code == 401


def test_read_404_when_missing(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = None
    resp = client.get("/work/pipeline/tools/p-nope", headers=_auth())
    assert resp.status_code == 404


def test_read_returns_full_shape(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(
        config_draft={"image_payload": {"market": "Austin"}},
        picks={"image": ["cr-final-1"], "video": []},
    )
    tools_sb.brief_row = {
        "id": "ib-1",
        "payload": {"market": "Austin, TX", "offer_text": "$99 inspection"},
    }
    tools_sb.creatives_rows = [
        {
            "id": "cr-c1",
            "brief_id": "ib-1",
            "concept": "trust",
            "ratio": "1x1",
            "version": "v0.ideation",
            "file_path_supabase": "ib-1/trust-1x1-v0.ideation.png",
        },
        {
            "id": "cr-final-1",
            "brief_id": "ib-1",
            "concept": "trust",
            "ratio": "9x16",
            "version": "v1.0",
            "file_path_supabase": "ib-1/trust-9x16-v1.0.png",
        },
    ]
    tools_sb.events_rows = [
        {
            "id": "ev-1",
            "pipeline_id": "p-1",
            "kind": "task_done",
            "stage": "ideation",
            "payload": {"creative_id": "cr-c1"},
            "created_at": "2026-05-20T00:00:05Z",
        },
        {
            "id": "ev-2",
            "pipeline_id": "p-1",
            "kind": "stage_advanced",
            "stage": "ideation",
            "payload": {},
            "created_at": "2026-05-20T00:00:01Z",
        },
    ]

    resp = client.get("/work/pipeline/tools/p-1", headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["pipeline_id"] == "p-1"
    assert body["status"] == "ideation"
    assert body["format_choice"] == "image"
    assert body["config_draft"] == {"image_payload": {"market": "Austin"}}
    assert body["picks"] == {"image": ["cr-final-1"], "video": []}
    assert body["brief"] == {
        "id": "ib-1",
        "payload": {"market": "Austin, TX", "offer_text": "$99 inspection"},
    }
    # Concepts = v0.ideation only; finals = v1*.
    assert [c["creative_id"] for c in body["concepts"]] == ["cr-c1"]
    assert [f["creative_id"] for f in body["finals"]] == ["cr-final-1"]
    # Events tail returned oldest-first.
    assert [e["id"] for e in body["events_tail"]] == ["ev-2", "ev-1"]


def test_read_null_brief_when_unlinked(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(image_brief_id=None)
    resp = client.get("/work/pipeline/tools/p-1", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["brief"] is None
    assert body["concepts"] == []
    assert body["finals"] == []


def test_read_client_null_when_no_client_row(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """Pipeline linked to a client_id whose row is gone → client: null."""
    tools_sb.pipeline_row = _pipeline_row(client_id="c-1")
    tools_sb.client_row = None
    resp = client.get("/work/pipeline/tools/p-1", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["client"] is None


def test_read_client_null_when_unlinked(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """No client_id on the pipeline → client: null, no client reads."""
    tools_sb.pipeline_row = _pipeline_row(client_id=None)
    resp = client.get("/work/pipeline/tools/p-1", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["client"] is None


def test_read_enriches_compact_client_block(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """A linked client surfaces a COMPACT client block on the pipeline read."""
    tools_sb.pipeline_row = _pipeline_row(client_id="c-1")
    tools_sb.client_row = {
        "id": "c-1",
        "slug": "acme-roofing",
        "name": "Acme Roofing",
        "service_type": "roofing",
        "brand_colors": {"primary": "#0a3d62"},
    }
    tools_sb.client_profile_row = {
        "client_id": "c-1",
        "tone": "warm, expert",
        "targeting": "Within 150 miles from ZIP 91405",
        "targeting_address": "14431 Valerio Street #203, Van Nuys, CA 91405",
        "targeting_zip": "91405",
        "targeting_radius_miles": 150,
        "targeting_type": "radius",
    }
    tools_sb.client_offers_rows = [
        {"client_id": "c-1", "offer_text": "$99 inspection", "active": True, "sort_order": 0},
        {"client_id": "c-1", "offer_text": "free quote", "active": False, "sort_order": 1},
    ]
    tools_sb.client_offer_constraints_rows = [
        {"client_id": "c-1", "constraint_text": "never say 'guaranteed approval'", "sort_order": 0},
    ]
    tools_sb.client_value_props_rows = [
        {"client_id": "c-1", "kind": "usp", "prop_text": "family-owned 25 yrs", "sort_order": 0},
        {"client_id": "c-1", "kind": "usp", "prop_text": "4.9 stars", "sort_order": 1},
        {"client_id": "c-1", "kind": "usp", "prop_text": "lifetime warranty", "sort_order": 2},
        {"client_id": "c-1", "kind": "usp", "prop_text": "fourth usp", "sort_order": 3},
        {"client_id": "c-1", "kind": "differentiator", "prop_text": "owner on every job", "sort_order": 0},
    ]

    resp = client.get("/work/pipeline/tools/p-1", headers=_auth())
    assert resp.status_code == 200, resp.text
    block = resp.json()["client"]
    assert block is not None
    assert block["client_id"] == "c-1"
    assert block["name"] == "Acme Roofing"
    assert block["service_type"] == "roofing"
    assert block["tone"] == "warm, expert"
    assert block["offers"] == [
        {"offer_text": "$99 inspection", "active": True},
        {"offer_text": "free quote", "active": False},
    ]
    assert block["offer_constraints"] == ["never say 'guaranteed approval'"]
    # Compact: top 3 USPs only, differentiators not in the compact block.
    assert block["top_usps"] == [
        "family-owned 25 yrs",
        "4.9 stars",
        "lifetime warranty",
    ]
    # Structured geo-targeting block rides along on the compact read.
    assert block["targeting"] == {
        "address": "14431 Valerio Street #203, Van Nuys, CA 91405",
        "zip": "91405",
        "radius_miles": 150,
        "type": "radius",
        "description": "Within 150 miles from ZIP 91405",
    }


# ===========================================================================
# GET /work/client/{client_id}
# ===========================================================================


def test_client_read_requires_auth(client: TestClient) -> None:
    resp = client.get("/work/client/c-1")
    assert resp.status_code == 401


def test_client_read_404_when_missing(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.client_row = None
    resp = client.get("/work/client/c-nope", headers=_auth())
    assert resp.status_code == 404


def test_client_read_returns_full_shape(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.client_row = {
        "id": "c-1",
        "slug": "acme-roofing",
        "name": "Acme Roofing",
        "service_type": "roofing",
        "brand_colors": {"primary": "#0a3d62", "accent": "#fa983a"},
    }
    tools_sb.client_profile_row = {
        "client_id": "c-1",
        "tone": "warm, expert, no-pressure",
        "years_in_business": 25,
        "google_rating": 4.9,
        "warranty": "lifetime workmanship",
        "primary_city": "Austin",
        "state": "TX",
        "targeting": "Within 150 miles from ZIP 91405",
        "targeting_address": "14431 Valerio Street #203, Van Nuys, CA 91405",
        "targeting_zip": "91405",
        "targeting_radius_miles": 150,
        "targeting_type": "radius",
        "needs_input": [],
        "raw_profile": {"source": "file"},
    }
    tools_sb.client_offers_rows = [
        {"client_id": "c-1", "offer_text": "$99 roof inspection", "active": True, "sort_order": 0},
        {"client_id": "c-1", "offer_text": "$0 down financing", "active": True, "sort_order": 1},
        {"client_id": "c-1", "offer_text": "old promo", "active": False, "sort_order": 2},
    ]
    tools_sb.client_offer_constraints_rows = [
        {"client_id": "c-1", "constraint_text": "no 'guaranteed approval'", "sort_order": 0},
        {"client_id": "c-1", "constraint_text": "no price claims without '+tax'", "sort_order": 1},
    ]
    tools_sb.client_services_rows = [
        {"client_id": "c-1", "service_name": "roof replacement", "sort_order": 0},
        {"client_id": "c-1", "service_name": "storm repair", "sort_order": 1},
    ]
    tools_sb.client_value_props_rows = [
        {"client_id": "c-1", "kind": "usp", "prop_text": "family-owned 25 yrs", "sort_order": 0},
        {"client_id": "c-1", "kind": "differentiator", "prop_text": "owner on every job", "sort_order": 0},
        {"client_id": "c-1", "kind": "usp", "prop_text": "4.9 stars on 700+ reviews", "sort_order": 1},
    ]
    tools_sb.client_assets_rows = [
        {
            "client_id": "c-1",
            "kind": "logo",
            "source": "drive",
            "ref": "drive-id-123",
            "formats": "1x1",
            "label": "primary logo",
            "sort_order": 0,
        },
    ]
    tools_sb.client_past_projects_rows = [
        {"client_id": "c-1", "url": "https://acme.example/project-1", "sort_order": 0},
        {"client_id": "c-1", "url": "https://acme.example/project-2", "sort_order": 1},
    ]

    resp = client.get("/work/client/c-1", headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["client_id"] == "c-1"
    assert body["slug"] == "acme-roofing"
    assert body["name"] == "Acme Roofing"
    assert body["service_type"] == "roofing"
    assert body["brand_colors"] == {"primary": "#0a3d62", "accent": "#fa983a"}
    # Full typed profile passed through verbatim.
    assert body["profile"]["years_in_business"] == 25
    assert body["profile"]["google_rating"] == 4.9
    assert body["profile"]["raw_profile"] == {"source": "file"}
    # Clean structured geo-targeting block (description = the free-text prose).
    assert body["targeting"] == {
        "address": "14431 Valerio Street #203, Van Nuys, CA 91405",
        "zip": "91405",
        "radius_miles": 150,
        "type": "radius",
        "description": "Within 150 miles from ZIP 91405",
    }
    # Offers carry offer_text + active (all of them, active flag preserved).
    assert body["offers"] == [
        {"offer_text": "$99 roof inspection", "active": True},
        {"offer_text": "$0 down financing", "active": True},
        {"offer_text": "old promo", "active": False},
    ]
    # Constraints are flat text, in sort order.
    assert body["offer_constraints"] == [
        "no 'guaranteed approval'",
        "no price claims without '+tax'",
    ]
    assert body["services"] == ["roof replacement", "storm repair"]
    # Value props split by kind.
    assert body["value_props"] == {
        "usps": ["family-owned 25 yrs", "4.9 stars on 700+ reviews"],
        "differentiators": ["owner on every job"],
    }
    assert body["assets"] == [
        {
            "kind": "logo",
            "source": "drive",
            "ref": "drive-id-123",
            "formats": "1x1",
            "label": "primary logo",
        }
    ]
    assert body["past_projects"] == [
        "https://acme.example/project-1",
        "https://acme.example/project-2",
    ]


def test_client_read_null_profile_and_empty_children(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """Client row present but profile unfilled + no child rows → degrades."""
    tools_sb.client_row = {
        "id": "c-2",
        "slug": "newco",
        "name": "NewCo",
        "service_type": "remodeling",
        "brand_colors": None,
    }
    tools_sb.client_profile_row = None

    resp = client.get("/work/client/c-2", headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile"] is None
    # No profile row -> no structured targeting block.
    assert body["targeting"] is None
    assert body["offers"] == []
    assert body["offer_constraints"] == []
    assert body["services"] == []
    assert body["value_props"] == {"usps": [], "differentiators": []}
    assert body["assets"] == []
    assert body["past_projects"] == []


# ===========================================================================
# POST /work/pipeline/tools/brief
# ===========================================================================


def test_brief_requires_auth(client: TestClient) -> None:
    resp = client.post("/work/pipeline/tools/brief", json={})
    assert resp.status_code == 401


def test_brief_validates_required_keys(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row()
    # `market` is the one required creative key; an image_payload missing it
    # (offer_text/angles are now optional) still fails validation.
    resp = client.post(
        "/work/pipeline/tools/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "image_payload": {"offer_text": "x", "angles": ["y"]},
        },
    )
    assert resp.status_code == 422


def test_brief_inserts_when_unlinked(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(image_brief_id=None)
    tools_sb.client_row = {"slug": "acme"}

    resp = client.post(
        "/work/pipeline/tools/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "image_payload": {
                "market": "Austin, TX",
                "offer_text": "$99 inspection",
                "angles": ["trust", "savings"],
            },
            "notes": "owner-led trust angle",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["brief_id"] == "briefs-id-1"

    # A briefs row was inserted with the constraint-satisfying defaults.
    brief_inserts = [d for n, d in tools_sb.inserts if n == "briefs"]
    assert len(brief_inserts) == 1
    payload = brief_inserts[0]["payload"]
    assert payload["market"] == "Austin, TX"
    assert payload["offer_text"] == "$99 inspection"
    assert payload["angles"] == ["trust", "savings"]
    # service + budget backfilled so the briefs.payload CHECK is satisfied.
    assert "service" in payload and "budget" in payload
    assert brief_inserts[0]["status"] == "draft"
    assert brief_inserts[0]["brief_id_human"]  # minted via rpc

    # The pipeline was linked + config_draft merged.
    pipe_updates = [d for n, d in tools_sb.updates if n == "pipelines"]
    assert pipe_updates and pipe_updates[0]["image_brief_id"] == "briefs-id-1"
    assert pipe_updates[0]["config_draft"]["notes"] == "owner-led trust angle"

    # brief_authored event emitted.
    events = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    assert any(e["kind"] == "brief_authored" for e in events)


def test_brief_updates_when_already_linked(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """Idempotent re-author: existing image_brief_id → UPDATE, not INSERT."""
    tools_sb.pipeline_row = _pipeline_row(image_brief_id="ib-existing")

    resp = client.post(
        "/work/pipeline/tools/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "image_payload": {
                "market": "Dallas, TX",
                "offer_text": "free quote",
                "angles": ["urgency"],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["brief_id"] == "ib-existing"

    # No new briefs INSERT; the existing brief was UPDATEd.
    assert not any(n == "briefs" for n, _ in tools_sb.inserts)
    brief_updates = [d for n, d in tools_sb.updates if n == "briefs"]
    assert len(brief_updates) == 1
    assert brief_updates[0]["payload"]["market"] == "Dallas, TX"


def test_brief_persists_concepts(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """concepts ride along on the brief payload + config_draft for the
    deterministic render to fan out over later."""
    tools_sb.pipeline_row = _pipeline_row(image_brief_id="ib-existing")

    concepts = [
        {"concept": "before_after__a", "prompt": "pa", "offer_text": "$99"},
        {"concept": "savings__b", "prompt": "pb"},
    ]
    resp = client.post(
        "/work/pipeline/tools/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "image_payload": {
                "market": "Austin, TX",
                "offer_text": "$99 inspection",
                "angles": ["before_after", "savings"],
            },
            "concepts": concepts,
        },
    )
    assert resp.status_code == 200, resp.text

    # Persisted on the brief payload.
    brief_updates = [d for n, d in tools_sb.updates if n == "briefs"]
    assert brief_updates[0]["payload"]["concepts"] == concepts
    # Mirrored onto config_draft for the read/deterministic render.
    pipe_updates = [d for n, d in tools_sb.updates if n == "pipelines"]
    assert pipe_updates[0]["config_draft"]["concepts"] == concepts
    # Event records the count.
    events = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    authored = [e for e in events if e["kind"] == "brief_authored"]
    assert authored[0]["payload"]["concept_count"] == 2


def test_brief_concepts_first_persists_and_maps_concept_name(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """A concepts-first brief (market + concepts keyed ``concept_name``, NO
    top-level offer_text/angles) validates, persists the briefs row, and merges
    config_draft.image_payload + config_draft.concepts. ``concept_name`` is
    normalised to the canonical ``concept`` key the render path reads."""
    tools_sb.pipeline_row = _pipeline_row(image_brief_id=None)
    tools_sb.client_row = {"slug": "acme"}

    resp = client.post(
        "/work/pipeline/tools/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "image_payload": {
                "market": "Austin, TX",
                "service": "roofing",
                "audience": "homeowners",
            },
            "concepts": [
                {
                    "concept_name": "before_after__a",
                    "prompt": "pa",
                    "use_case": "feed",
                    "qa_notes": "n",
                },
                {"concept_name": "savings__b", "prompt": "pb"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # briefs row inserted; CHECK-satisfying service + budget backfilled.
    brief_inserts = [d for n, d in tools_sb.inserts if n == "briefs"]
    assert len(brief_inserts) == 1
    payload = brief_inserts[0]["payload"]
    assert payload["market"] == "Austin, TX"
    assert "service" in payload and "budget" in payload
    # concept_name was mapped onto concept; concept_name dropped; extras kept.
    stored = payload["concepts"]
    assert [c["concept"] for c in stored] == ["before_after__a", "savings__b"]
    assert all("concept_name" not in c for c in stored)
    assert stored[0]["use_case"] == "feed"

    # config_draft mirrors both image_payload and the concept plan.
    pipe_updates = [d for n, d in tools_sb.updates if n == "pipelines"]
    assert pipe_updates[0]["config_draft"]["image_payload"]["market"] == "Austin, TX"
    assert [
        c["concept"] for c in pipe_updates[0]["config_draft"]["concepts"]
    ] == ["before_after__a", "savings__b"]


def test_brief_image_payload_concept_spec_accept_aliases() -> None:
    """Schema-level back-compat: an offer-first BriefImagePayload still
    validates, a concepts-first one (no offer_text/angles) validates, and
    ConceptSpec maps concept_name -> concept."""
    from src.routes.pipeline_tools import BriefImagePayload, ConceptSpec

    # Offer-first (back-compat) still valid.
    offer_first = BriefImagePayload(
        market="Austin", offer_text="$99", angles=["trust"]
    )
    assert offer_first.offer_text == "$99"

    # Concepts-first valid with only market (offer_text/angles optional).
    concepts_first = BriefImagePayload(market="Austin")
    assert concepts_first.offer_text is None
    assert concepts_first.angles is None

    # ConceptSpec accepts concept_name and normalises to concept.
    spec = ConceptSpec(concept_name="before_after__a", prompt="p")
    dumped = spec.model_dump(exclude_none=True)
    assert dumped["concept"] == "before_after__a"
    assert "concept_name" not in dumped
    # Explicit `concept` still wins / works.
    assert ConceptSpec(concept="x", prompt="p").concept == "x"


def test_brief_404_when_pipeline_missing(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = None
    resp = client.post(
        "/work/pipeline/tools/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-nope",
            "image_payload": {
                "market": "X",
                "offer_text": "Y",
                "angles": ["z"],
            },
        },
    )
    assert resp.status_code == 404


# ===========================================================================
# POST /work/pipeline/tools/video/brief  (VID-7)
# ===========================================================================


def _video_payload(**over: Any) -> dict[str, Any]:
    base = {
        "market": "Austin TX roofing",
        "offer_text": "$99 roof inspection",
        "angles": ["before_after", "urgency"],
        "target_duration_s": 24,
        "voice_id": "voice-xyz",
    }
    base.update(over)
    return base


def test_video_brief_requires_auth(client: TestClient) -> None:
    resp = client.post("/work/pipeline/tools/video/brief", json={})
    assert resp.status_code == 401


def test_video_brief_validates_required_keys(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row()
    # Missing target_duration_s + voice_id (video-required).
    resp = client.post(
        "/work/pipeline/tools/video/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "video_payload": {
                "market": "Austin",
                "offer_text": "$99",
                "angles": ["urgency"],
            },
        },
    )
    assert resp.status_code == 422


def test_video_brief_inserts_and_splits_columns(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(video_brief_id=None)
    tools_sb.client_row = {"slug": "acme"}

    resp = client.post(
        "/work/pipeline/tools/video/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "video_payload": _video_payload(
                hook_style="problem_callout", broll_selection_mode="auto"
            ),
            "notes": "storm season push",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["brief_id"] == "video_briefs-id-1"

    vb_inserts = [d for n, d in tools_sb.inserts if n == "video_briefs"]
    assert len(vb_inserts) == 1
    row = vb_inserts[0]
    # First-class columns land at the top level of the row.
    assert row["target_duration_s"] == 24
    assert row["voice_id"] == "voice-xyz"
    assert row["hook_style"] == "problem_callout"
    assert row["broll_selection_mode"] == "auto"
    assert row["status"] == "draft"
    assert row["brief_id_human"]  # minted via rpc
    # Strategy fields live in the jsonb payload, NOT as columns.
    assert row["payload"]["market"] == "Austin TX roofing"
    assert row["payload"]["angles"] == ["before_after", "urgency"]
    assert "target_duration_s" not in row["payload"]

    # Pipeline linked + config_draft merged.
    pipe_updates = [d for n, d in tools_sb.updates if n == "pipelines"]
    assert pipe_updates and pipe_updates[0]["video_brief_id"] == "video_briefs-id-1"
    assert pipe_updates[0]["config_draft"]["notes"] == "storm season push"

    events = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    authored = [e for e in events if e["kind"] == "brief_authored"]
    assert authored and authored[0]["payload"]["kind"] == "video"


def test_video_brief_updates_when_linked(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(video_brief_id="vb-existing")
    resp = client.post(
        "/work/pipeline/tools/video/brief",
        headers=_auth(),
        json={"pipeline_id": "p-1", "video_payload": _video_payload()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["brief_id"] == "vb-existing"
    assert not any(n == "video_briefs" for n, _ in tools_sb.inserts)
    vb_updates = [d for n, d in tools_sb.updates if n == "video_briefs"]
    assert len(vb_updates) == 1
    assert vb_updates[0]["voice_id"] == "voice-xyz"


def test_video_brief_persists_concepts(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(video_brief_id="vb-existing")
    concepts = [
        {"concept": "urgency__a", "angle": "urgency", "script": {"hook": "h1"}},
        {"concept": "savings__b", "angle": "savings", "script": {"hook": "h2"}},
    ]
    resp = client.post(
        "/work/pipeline/tools/video/brief",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "video_payload": _video_payload(),
            "concepts": concepts,
        },
    )
    assert resp.status_code == 200, resp.text
    vb_updates = [d for n, d in tools_sb.updates if n == "video_briefs"]
    assert vb_updates[0]["payload"]["concepts"] == concepts
    pipe_updates = [d for n, d in tools_sb.updates if n == "pipelines"]
    assert pipe_updates[0]["config_draft"]["video_concepts"] == concepts
    events = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    authored = [e for e in events if e["kind"] == "brief_authored"]
    assert authored[0]["payload"]["concept_count"] == 2


def test_video_brief_404_when_pipeline_missing(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = None
    resp = client.post(
        "/work/pipeline/tools/video/brief",
        headers=_auth(),
        json={"pipeline_id": "nope", "video_payload": _video_payload()},
    )
    assert resp.status_code == 404


# ===========================================================================
# POST /work/pipeline/tools/render
# ===========================================================================


def test_render_requires_auth(client: TestClient) -> None:
    resp = client.post("/work/pipeline/tools/render", json={})
    assert resp.status_code == 401


def test_render_concept_preview(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """concept_preview → 1x1 / 1K / v0.ideation, with task + cost events."""
    tools_sb.pipeline_row = _pipeline_row()
    monkeypatch.setenv("KIE_AI_API_KEY", "test-kie")
    from src.config import get_settings

    get_settings.cache_clear()
    from src.routes import pipeline_tools

    monkeypatch.setattr(pipeline_tools, "KieClient", _StubKieClient)

    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "concept_preview",
            "items": [
                {"concept": "trust", "prompt": "owner on a roof, golden hour"},
                {"concept": "savings", "prompt": "happy homeowner, $99 sticker"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert len(body["renders"]) == 2
    assert body["errors"] == []
    # 2 previews at the shared Kie per-image price each (E4.2: was a drifting
    # 0.02 literal, now reads from pricing.kie_image_cost == 0.05).
    assert body["total_cost_usd"] == pytest.approx(0.10)

    creatives = [d for n, d in tools_sb.inserts if n == "creatives"]
    assert len(creatives) == 2
    for c in creatives:
        assert c["ratio"] == "1x1"
        assert c["version"] == "v0.ideation"
        # 1K resolution + operator authorship marker in prompt_used.
        assert c["prompt_used"]["resolution"] == "1K"
        assert c["prompt_used"]["author"] == "operator"

    pe = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    kinds = [e["kind"] for e in pe]
    assert kinds.count("task_queued") == 2
    assert kinds.count("task_running") == 2
    assert kinds.count("task_done") == 2
    cost = [e for e in pe if e["kind"] == "cost_recorded"]
    assert len(cost) == 2
    assert all(e["stage"] == "ideation" for e in cost)
    assert all(e["payload"]["api"] == "kie.ai" for e in cost)


def test_render_final_both_ratios(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """final → 1x1 + 9x16 / 2K / v1.0 with parent_creative_id set."""
    tools_sb.pipeline_row = _pipeline_row(status="generation")
    monkeypatch.setenv("KIE_AI_API_KEY", "test-kie")
    from src.config import get_settings

    get_settings.cache_clear()
    from src.routes import pipeline_tools

    monkeypatch.setattr(pipeline_tools, "KieClient", _StubKieClient)

    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "final",
            "items": [
                {
                    "concept": "trust",
                    "prompt": "owner on a roof, golden hour, photoreal",
                    "offer_text": "$99 inspection",
                    "parent_creative_id": "cr-c1",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["renders"]) == 2
    assert sorted(r["ratio"] for r in body["renders"]) == ["1x1", "9x16"]
    assert body["total_cost_usd"] == pytest.approx(0.10)

    creatives = [d for n, d in tools_sb.inserts if n == "creatives"]
    assert len(creatives) == 2
    for c in creatives:
        assert c["version"] == "v1.0"
        assert c["prompt_used"]["resolution"] == "2K"
        assert c["prompt_used"]["parent_creative_id"] == "cr-c1"

    pe = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    done = [e for e in pe if e["kind"] == "task_done"]
    assert all(e["stage"] == "generation" for e in done)
    assert all(e["payload"]["parent_creative_id"] == "cr-c1" for e in done)


def test_render_final_requires_parent(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools_sb.pipeline_row = _pipeline_row()
    monkeypatch.setenv("KIE_AI_API_KEY", "test-kie")
    from src.config import get_settings

    get_settings.cache_clear()
    from src.routes import pipeline_tools

    monkeypatch.setattr(pipeline_tools, "KieClient", _StubKieClient)

    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "final",
            "items": [{"concept": "trust", "prompt": "a roof"}],
        },
    )
    assert resp.status_code == 400
    assert "parent_creative_id" in resp.json()["detail"]


def test_render_per_item_error_continues(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ratio emits task_error but the batch keeps going."""
    tools_sb.pipeline_row = _pipeline_row(status="generation")
    monkeypatch.setenv("KIE_AI_API_KEY", "test-kie")
    from src.config import get_settings

    get_settings.cache_clear()
    from src.routes import pipeline_tools

    monkeypatch.setattr(pipeline_tools, "KieClient", _FailKieClient)

    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "final",
            "items": [
                {
                    "concept": "trust",
                    "prompt": "a roof",
                    "parent_creative_id": "cr-c1",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 1x1 succeeded, 9x16 failed.
    assert len(body["renders"]) == 1
    assert body["renders"][0]["ratio"] == "1x1"
    assert len(body["errors"]) == 1
    assert body["errors"][0]["ratio"] == "9x16"

    pe = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    assert sum(1 for e in pe if e["kind"] == "task_done") == 1
    err = [e for e in pe if e["kind"] == "task_error"]
    assert len(err) == 1
    assert err[0]["payload"]["ratio"] == "9x16"


def test_render_404_when_pipeline_missing(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = None
    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={
            "pipeline_id": "p-nope",
            "kind": "concept_preview",
            "items": [{"concept": "x", "prompt": "y"}],
        },
    )
    assert resp.status_code == 404


def test_render_400_when_no_brief(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(image_brief_id=None)
    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "concept_preview",
            "items": [{"concept": "x", "prompt": "y"}],
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Deterministic render (items omitted → fan out over the persisted plan)
# ---------------------------------------------------------------------------


def _use_stub_kie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIE_AI_API_KEY", "test-kie")
    from src.config import get_settings

    get_settings.cache_clear()
    from src.routes import pipeline_tools

    monkeypatch.setattr(pipeline_tools, "KieClient", _StubKieClient)


def test_render_deterministic_concept_preview_renders_all_persisted(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """items omitted → render EVERY persisted concept in ONE pass."""
    tools_sb.pipeline_row = _pipeline_row(
        config_draft={
            "concepts": [
                {"concept": "before_after__a", "prompt": "pa", "offer_text": "$99"},
                {"concept": "owner_led_trust__b", "prompt": "pb"},
                {"concept": "savings__c", "prompt": "pc"},
                {"concept": "authority__d", "prompt": "pd"},
            ]
        }
    )
    _use_stub_kie(monkeypatch)

    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={"pipeline_id": "p-1", "kind": "concept_preview"},  # NO items
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["renders"]) == 4  # all 4 persisted concepts (1x1 each)
    assert body["skipped"] == []
    creatives = [d for n, d in tools_sb.inserts if n == "creatives"]
    assert {c["concept"] for c in creatives} == {
        "before_after__a",
        "owner_led_trust__b",
        "savings__c",
        "authority__d",
    }
    assert all(c["version"] == "v0.ideation" for c in creatives)


def test_render_deterministic_skips_already_rendered(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry renders only the REMAINDER — the stuck-at-1/N production fix."""
    tools_sb.pipeline_row = _pipeline_row(
        config_draft={
            "concepts": [
                {"concept": "before_after__a", "prompt": "pa"},
                {"concept": "savings__c", "prompt": "pc"},
            ]
        }
    )
    # one concept already landed at v0.ideation (the stuck state)
    tools_sb.creatives_rows = [
        {
            "id": "cr-a",
            "brief_id": "ib-1",
            "concept": "before_after__a",
            "ratio": "1x1",
            "version": "v0.ideation",
            "file_path_supabase": "ib-1/a.png",
        }
    ]
    _use_stub_kie(monkeypatch)

    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={"pipeline_id": "p-1", "kind": "concept_preview"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["renders"]) == 1
    assert body["skipped"] == ["before_after__a"]
    creatives = [d for n, d in tools_sb.inserts if n == "creatives"]
    assert [c["concept"] for c in creatives] == ["savings__c"]


def test_render_deterministic_ignores_soft_deleted(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A soft-deleted creative must NOT count as already-rendered.

    Regression: a soft-deleted final used to satisfy the idempotent-resume
    'done' check, so re-rendering an archived/superseded concept was skipped
    forever (it stalled the e087bbe1 generation recovery).
    """
    tools_sb.pipeline_row = _pipeline_row(
        config_draft={"concepts": [{"concept": "before_after__a", "prompt": "pa"}]}
    )
    # The only stored creative for the concept is SOFT-DELETED -> must be ignored.
    tools_sb.creatives_rows = [
        {
            "id": "cr-a",
            "brief_id": "ib-1",
            "concept": "before_after__a",
            "ratio": "1x1",
            "version": "v0.ideation",
            "file_path_supabase": "ib-1/a.png",
            "deleted_at": "2026-05-30T00:00:00Z",
        }
    ]
    _use_stub_kie(monkeypatch)

    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={"pipeline_id": "p-1", "kind": "concept_preview"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == []  # soft-deleted row is not counted as done
    assert len(body["renders"]) == 1
    creatives = [d for n, d in tools_sb.inserts if n == "creatives"]
    assert [c["concept"] for c in creatives] == ["before_after__a"]


def test_render_deterministic_empty_plan_is_clean_noop(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools_sb.pipeline_row = _pipeline_row(config_draft={})
    _use_stub_kie(monkeypatch)
    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={"pipeline_id": "p-1", "kind": "concept_preview"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["renders"] == []
    assert not [d for n, d in tools_sb.inserts if n == "creatives"]


def test_render_deterministic_final_from_picks(
    client: TestClient,
    tools_sb: _ToolsSupabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """items omitted, kind=final → one final per pick, parent threaded."""
    tools_sb.pipeline_row = _pipeline_row(
        status="generation",
        picks={"image": ["cr-pick"], "video": []},
        config_draft={
            "concepts": [
                {"concept": "savings__c", "prompt": "pc", "offer_text": "$99"}
            ]
        },
    )
    tools_sb.creatives_rows = [
        {
            "id": "cr-pick",
            "brief_id": "ib-1",
            "concept": "savings__c",
            "ratio": "1x1",
            "version": "v0.ideation",
            "file_path_supabase": "ib-1/c.png",
        }
    ]
    _use_stub_kie(monkeypatch)

    resp = client.post(
        "/work/pipeline/tools/render",
        headers=_auth(),
        json={"pipeline_id": "p-1", "kind": "final"},  # NO items
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(r["ratio"] for r in body["renders"]) == ["1x1", "9x16"]
    creatives = [d for n, d in tools_sb.inserts if n == "creatives"]
    assert len(creatives) == 2
    for c in creatives:
        assert c["version"] == "v1.0"
        assert c["prompt_used"]["parent_creative_id"] == "cr-pick"


# ===========================================================================
# POST /work/pipeline/tools/store_creative
# ===========================================================================


import base64 as _base64


def _png_b64() -> str:
    """A short, valid base64 blob standing in for codex-rendered PNG bytes."""
    return _base64.b64encode(b"CODEXPNGBYTES").decode("ascii")


def test_store_creative_requires_auth(client: TestClient) -> None:
    resp = client.post("/work/pipeline/tools/store_creative", json={})
    assert resp.status_code == 401


def test_store_creative_concept_preview(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """concept_preview → ideation/v0.ideation, running+done+cost(0) events."""
    tools_sb.pipeline_row = _pipeline_row()

    resp = client.post(
        "/work/pipeline/tools/store_creative",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "concept_preview",
            "concept": "trust",
            "ratio": "1x1",
            "version": "v0.ideation",
            "prompt": "owner on a roof, golden hour",
            "image_b64": _png_b64(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The fake mints creative ids as ``creatives-id-<n>`` where n is the
    # running insert count (the task_running event inserts before the
    # creative), so assert the shape rather than a hardcoded counter.
    assert body["creative_id"].startswith("creatives-id-")
    assert body["version"] == "v0.ideation"
    assert body["file_path_supabase"] == "ib-1/trust-1x1-v0.ideation.png"

    # Bytes uploaded (the decoded base64, not the base64 string).
    assert len(tools_sb.storage_uploads) == 1
    upload_path, upload_bytes = tools_sb.storage_uploads[0]
    assert upload_path == "ib-1/trust-1x1-v0.ideation.png"
    assert upload_bytes == b"CODEXPNGBYTES"

    # One creative row, stamped with the codex backend + operator authorship.
    creatives = [d for n, d in tools_sb.inserts if n == "creatives"]
    assert len(creatives) == 1
    assert creatives[0]["ratio"] == "1x1"
    assert creatives[0]["version"] == "v0.ideation"
    assert creatives[0]["prompt_used"]["author"] == "operator"
    assert creatives[0]["prompt_used"]["backend"] == "openai-codex"
    assert creatives[0]["prompt_used"]["model"] == "openai-codex/gpt-image-2"

    pe = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    kinds = [e["kind"] for e in pe]
    assert kinds.count("task_running") == 1
    assert kinds.count("task_done") == 1
    assert "task_error" not in kinds
    # Cost recorded at $0 against openai-codex, in the ideation stage.
    cost = [e for e in pe if e["kind"] == "cost_recorded"]
    assert len(cost) == 1
    assert cost[0]["stage"] == "ideation"
    assert cost[0]["payload"]["api"] == "openai-codex"
    assert cost[0]["payload"]["subtotal"] == 0


def test_store_creative_final_9x16(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    """final + 9x16 → generation/v1.0 with parent_creative_id threaded through."""
    tools_sb.pipeline_row = _pipeline_row(status="generation")

    resp = client.post(
        "/work/pipeline/tools/store_creative",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "final",
            "concept": "trust",
            "ratio": "9x16",
            "version": "v1.0",
            "prompt": "owner on a roof, photoreal, vertical",
            "image_b64": _png_b64(),
            "offer_text": "$99 inspection",
            "parent_creative_id": "cr-c1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "v1.0"
    assert body["file_path_supabase"] == "ib-1/trust-9x16-v1.0.png"

    creatives = [d for n, d in tools_sb.inserts if n == "creatives"]
    assert len(creatives) == 1
    assert creatives[0]["ratio"] == "9x16"
    assert creatives[0]["version"] == "v1.0"
    assert creatives[0]["offer_text"] == "$99 inspection"
    assert creatives[0]["prompt_used"]["parent_creative_id"] == "cr-c1"

    pe = [d for n, d in tools_sb.inserts if n == "pipeline_events"]
    done = [e for e in pe if e["kind"] == "task_done"]
    assert len(done) == 1
    assert done[0]["stage"] == "generation"
    assert done[0]["payload"]["parent_creative_id"] == "cr-c1"
    cost = [e for e in pe if e["kind"] == "cost_recorded"]
    assert cost[0]["payload"]["subtotal"] == 0
    assert cost[0]["payload"]["api"] == "openai-codex"


def test_store_creative_final_requires_parent(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(status="generation")
    resp = client.post(
        "/work/pipeline/tools/store_creative",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "final",
            "concept": "trust",
            "ratio": "1x1",
            "version": "v1.0",
            "prompt": "a roof",
            "image_b64": _png_b64(),
        },
    )
    assert resp.status_code == 400
    assert "parent_creative_id" in resp.json()["detail"]


def test_store_creative_rejects_bad_base64(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row()
    resp = client.post(
        "/work/pipeline/tools/store_creative",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "concept_preview",
            "concept": "trust",
            "ratio": "1x1",
            "version": "v0.ideation",
            "prompt": "a roof",
            "image_b64": "!!!not-base64!!!",
        },
    )
    assert resp.status_code == 400
    assert "base64" in resp.json()["detail"]
    # Nothing persisted on a decode failure.
    assert not any(n == "creatives" for n, _ in tools_sb.inserts)
    assert tools_sb.storage_uploads == []


def test_store_creative_404_when_pipeline_missing(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = None
    resp = client.post(
        "/work/pipeline/tools/store_creative",
        headers=_auth(),
        json={
            "pipeline_id": "p-nope",
            "kind": "concept_preview",
            "concept": "x",
            "ratio": "1x1",
            "version": "v0.ideation",
            "prompt": "y",
            "image_b64": _png_b64(),
        },
    )
    assert resp.status_code == 404


def test_store_creative_400_when_no_brief(
    client: TestClient, tools_sb: _ToolsSupabase
) -> None:
    tools_sb.pipeline_row = _pipeline_row(image_brief_id=None)
    resp = client.post(
        "/work/pipeline/tools/store_creative",
        headers=_auth(),
        json={
            "pipeline_id": "p-1",
            "kind": "concept_preview",
            "concept": "x",
            "ratio": "1x1",
            "version": "v0.ideation",
            "prompt": "y",
            "image_b64": _png_b64(),
        },
    )
    assert resp.status_code == 400


# Silent-failure PR-4: the legacy `POST /work/pipeline/tools/dispatch` route
# was deleted along with the `operator_bridge` module it called. The
# operator-daemon claims operator_dispatch work_items from the unified queue
# instead, so these tests are gone.
