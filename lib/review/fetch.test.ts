import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockClient } from "@/tests/unit/helpers/api-mock";
import { type SupabaseClientMock } from "@/tests/unit/helpers/supabase-mock";

vi.mock("server-only", () => ({}));

let currentSupabase: SupabaseClientMock = mockClient();
vi.mock("@/lib/supabase/admin", () => ({
  createAdminClient: () => currentSupabase,
}));

import { getClientCplTarget, getCopyVariants, getReviewBundle, getVariantPlan } from "./fetch";

beforeEach(() => {
  currentSupabase = mockClient();
});
afterEach(() => vi.restoreAllMocks());

describe("getReviewBundle", () => {
  it("projects creatives + states + copy + signed urls", async () => {
    currentSupabase = mockClient({
      pipelines: {
        select: {
          single: { data: { format_choice: "image", image_brief_id: "ib1" }, error: null },
        },
      },
      creatives: {
        select: {
          data: [{ id: "a", concept: "A", status: "draft", file_path_supabase: "a.png" }],
        },
      },
      creative_stage_state: {
        select: { data: [{ creative_id: "a", stage: "creative_qa", status: "passed" }] },
      },
      copy_variants: { select: { data: [{ creative_id: "a", status: "approved" }] } },
    });
    const bundle = await getReviewBundle("p1");
    expect(bundle.creatives).toEqual([{ id: "a", concept: "A", status: "draft" }]);
    expect(bundle.states).toHaveLength(1);
    expect(bundle.copyVariants).toHaveLength(1);
    expect(bundle.signedUrls.a).toBe("https://signed.test/a.png");
  });

  it("handles empty result sets", async () => {
    currentSupabase = mockClient();
    const bundle = await getReviewBundle("p1");
    expect(bundle.creatives).toEqual([]);
    expect(bundle.states).toEqual([]);
  });

  // B4: a video pipeline's creatives/copy live in the parity tables; the gate
  // state is the shared neutral creative_stage_state. The bundle additively
  // unions video creatives + video copy variants so the launch preconditions
  // see them like image creatives.
  it("unions video creatives + video copy variants for a video-only pipeline", async () => {
    currentSupabase = mockClient({
      pipelines: {
        select: {
          single: { data: { format_choice: "video", video_brief_id: "vb1" }, error: null },
        },
      },
      video_creatives: {
        select: { data: [{ id: "vc1", status: "captioned", captioned_path: "vc1.mp4" }] },
      },
      video_copy_variants: { select: { data: [{ creative_id: "vc1", status: "approved" }] } },
      creative_stage_state: {
        select: { data: [{ creative_id: "vc1", stage: "spec_validation", status: "passed" }] },
      },
    });
    const bundle = await getReviewBundle("p1");
    expect(bundle.creatives).toEqual([{ id: "vc1", concept: null, status: "captioned" }]);
    expect(bundle.copyVariants).toEqual([{ creative_id: "vc1", status: "approved" }]);
    expect(bundle.states).toHaveLength(1);
    expect(bundle.signedUrls.vc1).toBe("https://signed.test/vc1.mp4");
  });

  it("unions BOTH tracks for a format=both pipeline", async () => {
    currentSupabase = mockClient({
      pipelines: {
        select: {
          single: {
            data: { format_choice: "both", image_brief_id: "ib1", video_brief_id: "vb1" },
            error: null,
          },
        },
      },
      creatives: {
        select: {
          data: [{ id: "c1", concept: "A", status: "draft", file_path_supabase: "a.png" }],
        },
      },
      copy_variants: { select: { data: [{ creative_id: "c1", status: "approved" }] } },
      video_creatives: {
        select: { data: [{ id: "vc1", status: "captioned", captioned_path: "vc1.mp4" }] },
      },
      video_copy_variants: { select: { data: [{ creative_id: "vc1", status: "approved" }] } },
      creative_stage_state: { select: { data: [] } },
    });
    const bundle = await getReviewBundle("p1");
    expect(bundle.creatives.map((c) => c.id).sort()).toEqual(["c1", "vc1"]);
    expect(bundle.copyVariants).toHaveLength(2);
  });

  it("skips the video read when the pipeline has no video_brief_id", async () => {
    currentSupabase = mockClient({
      pipelines: {
        select: { single: { data: { format_choice: "video", video_brief_id: null }, error: null } },
      },
      video_creatives: {
        select: { data: [{ id: "vc1", status: "captioned", captioned_path: "vc1.mp4" }] },
      },
    });
    const bundle = await getReviewBundle("p1");
    expect(bundle.creatives).toEqual([]);
  });
});

describe("getCopyVariants", () => {
  it("returns the variant rows", async () => {
    currentSupabase = mockClient({
      copy_variants: {
        select: { data: [{ id: "v1", creative_id: "a", variant_index: 1, status: "draft" }] },
      },
    });
    const out = await getCopyVariants("p1");
    expect(out).toHaveLength(1);
  });

  it("returns [] when none", async () => {
    expect(await getCopyVariants("p1")).toEqual([]);
  });
});

describe("getVariantPlan", () => {
  it("returns null when no plan exists", async () => {
    currentSupabase = mockClient({
      variant_plan: { select: { single: { data: null, error: null } } },
    });
    expect(await getVariantPlan("p1")).toBeNull();
  });

  it("returns the plan + cells when present", async () => {
    currentSupabase = mockClient({
      variant_plan: {
        select: {
          single: { data: { id: "vp1", test_variable: "creative", hypothesis: "h" }, error: null },
        },
      },
      variant_plan_cell: {
        select: {
          data: [{ id: "c1", cell_index: 0, label: "A", creative_id: "a", copy_variant_id: null }],
        },
      },
    });
    const plan = await getVariantPlan("p1");
    expect(plan?.test_variable).toBe("creative");
    expect(plan?.cells).toHaveLength(1);
  });

  it("defaults cells to [] when the cell query is empty", async () => {
    currentSupabase = mockClient({
      variant_plan: {
        select: {
          single: { data: { id: "vp1", test_variable: "copy", hypothesis: null }, error: null },
        },
      },
    });
    const plan = await getVariantPlan("p1");
    expect(plan?.cells).toEqual([]);
  });
});

describe("getClientCplTarget", () => {
  it("returns null with no client id", async () => {
    expect(await getClientCplTarget(null)).toBeNull();
  });

  it("reads the client's cpl_target", async () => {
    currentSupabase = mockClient({
      clients: { select: { single: { data: { cpl_target: 75 }, error: null } } },
    });
    expect(await getClientCplTarget("client-1")).toBe(75);
  });

  it("returns null when the client has no target", async () => {
    currentSupabase = mockClient({
      clients: { select: { single: { data: { cpl_target: null }, error: null } } },
    });
    expect(await getClientCplTarget("client-1")).toBeNull();
  });
});
