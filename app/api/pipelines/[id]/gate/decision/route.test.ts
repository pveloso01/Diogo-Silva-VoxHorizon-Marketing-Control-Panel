/**
 * Tests for `app/api/pipelines/[id]/gate/decision/route.ts`.
 *
 * The unified manager-clear path for soft per-creative gate states. Its
 * load-bearing invariant is the required, non-empty `note` (an unaudited clear
 * is rejected 422). We drive the malformed-body, validation, missing-pipeline,
 * missing-gate-row, accept (soft -> passed), and override (failed -> overridden
 * + finding audit + event) branches across both stages.
 */
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mockClient } from "@/tests/unit/helpers/api-mock";
import { type SupabaseClientMock } from "@/tests/unit/helpers/supabase-mock";

let currentSupabase: SupabaseClientMock = mockClient();

vi.mock("server-only", () => ({}));
vi.mock("@/lib/supabase/admin", () => ({
  createAdminClient: () => currentSupabase,
}));

import { POST } from "./route";

const id = "11111111-1111-4111-8111-111111111111";
const creativeId = "22222222-2222-4222-8222-222222222222";
const params = Promise.resolve({ id });

function req(body: unknown, opts: { invalidJson?: boolean } = {}): NextRequest {
  return new NextRequest(
    new Request(`http://localhost/api/pipelines/${id}/gate/decision`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: opts.invalidJson ? "{not json" : JSON.stringify(body),
    }),
  );
}

function happyClient(stateStatus = "in_progress", updatedStatus = "passed") {
  return mockClient({
    pipelines: { select: { single: { data: { id }, error: null } } },
    creative_stage_state: {
      select: { single: { data: { id: "css1", status: stateStatus }, error: null } },
      update: { single: { data: { id: "css1", status: updatedStatus }, error: null } },
    },
    compliance_finding: { update: { data: null, error: null } },
    pipeline_events: { insert: { data: null, error: null } },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("POST /api/pipelines/:id/gate/decision", () => {
  it("400 on malformed JSON", async () => {
    currentSupabase = happyClient();
    const res = await POST(req(null, { invalidJson: true }), { params });
    expect(res.status).toBe(400);
  });

  it("422 when note is empty (no unaudited clear)", async () => {
    currentSupabase = happyClient();
    const res = await POST(
      req({ creative_id: creativeId, stage: "spec_validation", decision: "accept", note: "  " }),
      { params },
    );
    expect(res.status).toBe(422);
  });

  it("422 on an unknown stage", async () => {
    currentSupabase = happyClient();
    const res = await POST(
      req({ creative_id: creativeId, stage: "creative_qa", decision: "accept", note: "ok" }),
      { params },
    );
    expect(res.status).toBe(422);
  });

  it("404 when the pipeline does not exist", async () => {
    currentSupabase = mockClient({
      pipelines: { select: { single: { data: null, error: null } } },
    });
    const res = await POST(
      req({ creative_id: creativeId, stage: "spec_validation", decision: "accept", note: "ok" }),
      { params },
    );
    expect(res.status).toBe(404);
  });

  it("404 when the gate row does not exist", async () => {
    currentSupabase = mockClient({
      pipelines: { select: { single: { data: { id }, error: null } } },
      creative_stage_state: { select: { single: { data: null, error: null } } },
    });
    const res = await POST(
      req({ creative_id: creativeId, stage: "spec_validation", decision: "accept", note: "ok" }),
      { params },
    );
    expect(res.status).toBe(404);
  });

  it("accepts a soft spec warn -> passed", async () => {
    currentSupabase = happyClient("in_progress", "passed");
    const res = await POST(
      req({
        creative_id: creativeId,
        stage: "spec_validation",
        decision: "accept",
        note: "Manager confirmed the 4x5 before/after crop is fine.",
      }),
      { params },
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { status: string; decision: string };
    expect(body.status).toBe("passed");
    expect(body.decision).toBe("accept");
  });

  it("overrides a hard compliance fail -> overridden", async () => {
    currentSupabase = happyClient("failed", "overridden");
    const res = await POST(
      req({
        creative_id: creativeId,
        stage: "compliance_review",
        decision: "override",
        note: "Legal reviewed; releasing the block.",
        decided_by: "diogo",
      }),
      { params },
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { status: string };
    expect(body.status).toBe("overridden");
  });
});
