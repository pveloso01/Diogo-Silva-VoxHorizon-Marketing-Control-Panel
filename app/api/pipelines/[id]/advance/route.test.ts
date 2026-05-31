/**
 * Tests for `app/api/pipelines/[id]/advance/route.ts`.
 *
 * The route handles two transitions: `configuration → ideation` and
 * `ideation → review`. Each path has its own gate, RPC, brief inserts,
 * and compensating cleanup branches — we drive every branch.
 */
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockClient } from "@/tests/unit/helpers/api-mock";
import { type SupabaseClientMock } from "@/tests/unit/helpers/supabase-mock";

let currentSupabase: SupabaseClientMock = mockClient();

// `@/lib/operator/dispatch` imports `server-only`; neutralise it so the jsdom
// route-test project can load the (partially mocked) module.
vi.mock("server-only", () => ({}));

vi.mock("@/lib/supabase/admin", () => ({
  createAdminClient: () => currentSupabase,
}));

const { enqueueWorkItem } = vi.hoisted(() => ({
  enqueueWorkItem: vi.fn<(opts: unknown) => Promise<{ id: string; duplicate: boolean }>>(
    async () => ({ id: "wi-1", duplicate: false }),
  ),
}));
vi.mock("@/lib/work-queue/enqueue", () => ({ enqueueWorkItem }));

import { POST } from "./route";

const id = "11111111-1111-4111-8111-111111111111";
const params = Promise.resolve({ id });

function req(url: string, init: RequestInit = {}): NextRequest {
  return new NextRequest(new Request(url, init));
}

function withRpc(
  client: SupabaseClientMock,
  fn: (name: string) => { data: unknown; error: { message: string } | null },
): SupabaseClientMock {
  // Silent-failure PR-4: preserve the prior `rpc` spy for the derived-status
  // reducer so the route's `getDerivedStatus` call resolves from the table
  // mock (the route uses `compute_pipeline_status(id)` after migration 0051
  // dropped the `pipelines.status` column).
  const previousRpc = (client as unknown as { rpc?: (...args: unknown[]) => Promise<unknown> }).rpc;
  (client as unknown as { rpc: ReturnType<typeof vi.fn> }).rpc = vi.fn(
    (name: string, args?: unknown) => {
      if (name === "compute_pipeline_status" && previousRpc) {
        return previousRpc(name, args) as Promise<{
          data: unknown;
          error: { message: string } | null;
        }>;
      }
      return Promise.resolve(fn(name));
    },
  );
  return client;
}

const validImagePayload = {
  service: "roofing",
  budget: 1000,
  market: "Miami",
};

const validVideoPayload = {
  script_outline: {
    hook: "Discover the best roofing",
    segments: [{ topic: "Intro", duration_s: 30 }],
  },
  target_duration_s: 30,
  voice_id: "voice-1",
  dimensions: "9x16",
  broll_selection_mode: "review_each",
};

const clientId = "22222222-2222-4222-8222-222222222222";

beforeEach(() => {
  delete process.env.WORKER_URL;
  delete process.env.WORKER_SHARED_SECRET;
  enqueueWorkItem.mockReset();
  enqueueWorkItem.mockResolvedValue({ id: "wi-1", duplicate: false });
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("POST /api/pipelines/:id/advance", () => {
  it("500 read error", async () => {
    currentSupabase = mockClient({
      pipelines: { select: { single: { data: null, error: { message: "x" } } } },
    });
    const res = await POST(
      req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
      {
        params,
      },
    );
    expect(res.status).toBe(500);
  });

  it("404 not found", async () => {
    currentSupabase = mockClient({
      pipelines: { select: { single: { data: null, error: null } } },
    });
    const res = await POST(
      req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
      {
        params,
      },
    );
    expect(res.status).toBe(404);
  });

  it("422 for unsupported status (review)", async () => {
    currentSupabase = mockClient({
      pipelines: { select: { single: { data: { id, status: "review" }, error: null } } },
    });
    const res = await POST(
      req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
      {
        params,
      },
    );
    expect(res.status).toBe(422);
  });

  describe("configuration → ideation", () => {
    it("happy path image-only (200)", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "image",
                  client_id: clientId,
                  config_draft: { image_payload: validImagePayload },
                  advanced_at: {},
                },
                error: null,
              },
            },
            update: {
              single: { data: { id, status: "ideation" }, error: null },
            },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME-2026-0001", error: null }),
      );

      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });

    it("re-tasks the operator for ideation after approving the brief", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "image",
                  client_id: clientId,
                  config_draft: { operator_driven: true, image_payload: validImagePayload },
                  advanced_at: {},
                },
                error: null,
              },
            },
            update: { single: { data: { id, status: "ideation" }, error: null } },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME-2026-0001", error: null }),
      );

      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      // Silent-failure PR-3: the advance enqueues an operator_dispatch
      // work_item (the legacy fire-and-forget dispatchOperator is gone). The
      // auto-emit trigger writes the operator_dispatched pipeline_events row.
      await new Promise((r) => setTimeout(r, 5));
      expect(enqueueWorkItem).toHaveBeenCalledTimes(1);
      const opts = enqueueWorkItem.mock.calls[0]?.[0] as Record<string, unknown>;
      expect(opts.kind).toBe("operator_dispatch");
      expect((opts.payload as Record<string, unknown>).stage).toBe("ideation");
      expect(currentSupabase._spies.from).toHaveBeenCalledWith("pipeline_events");
    });

    it("advances an operator pipeline whose payload fails the strict form schema", async () => {
      // The operator authors a looser, extras-bearing image_payload (validated
      // by the worker, not the form's BriefPayload). The advance must NOT
      // re-validate it (no "image_payload invalid" 422) and must keep the
      // operator's already-authored image_brief_id.
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "image",
                  client_id: clientId,
                  image_brief_id: "op-brief-1",
                  config_draft: {
                    operator_driven: true,
                    image_payload: { offer_text: "x", extras: { foo: "bar" } },
                  },
                  advanced_at: {},
                },
                error: null,
              },
            },
            update: { single: { data: { id, status: "ideation" }, error: null } },
          },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME-2026-0001", error: null }),
      );

      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.error).toBeUndefined();
      expect(body.image_brief_id).toBe("op-brief-1");
      // Silent-failure PR-3: the operator was enqueued via the work_item
      // queue (the legacy fire-and-forget dispatchOperator is gone).
      await new Promise((r) => setTimeout(r, 5));
      expect(enqueueWorkItem).toHaveBeenCalledTimes(1);
      const opts = enqueueWorkItem.mock.calls[0]?.[0] as Record<string, unknown>;
      expect(opts.kind).toBe("operator_dispatch");
    });

    // Silent-failure PR-3: the legacy "operator dispatch rejects" path is
    // gone -- there is no longer a fire-and-forget dispatch call to fail.
    // The work_item enqueue is synchronous and 5xx-on-failure (covered by
    // the dual-write tests below).

    it("happy path video-only (200)", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "video",
                  client_id: clientId,
                  config_draft: { video_payload: validVideoPayload },
                  advanced_at: null,
                },
                error: null,
              },
            },
            update: { single: { data: { id }, error: null } },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          video_briefs: { insert: { single: { data: { id: "vb1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME-V-0001", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });

    it("happy path with format=both", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "both",
                  client_id: clientId,
                  config_draft: {
                    image_payload: validImagePayload,
                    video_payload: validVideoPayload,
                  },
                  advanced_at: {},
                },
                error: null,
              },
            },
            update: { single: { data: { id }, error: null } },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          video_briefs: { insert: { single: { data: { id: "vb1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        (name) =>
          name === "gen_brief_id_human"
            ? { data: "ACME-2026-0001", error: null }
            : { data: "ACME-V-0001", error: null },
      );

      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });

    it("422 when config_draft missing required payloads", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "configuration",
                format_choice: "image",
                config_draft: null,
                advanced_at: {},
              },
              error: null,
            },
          },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("422 on invalid image payload zod failure", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "configuration",
                format_choice: "image",
                config_draft: { image_payload: { service: "bogus" } },
                advanced_at: {},
              },
              error: null,
            },
          },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.error).toBe("image_payload invalid");
    });

    it("422 on invalid video payload zod failure", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "configuration",
                format_choice: "video",
                client_id: clientId,
                config_draft: { video_payload: { script_outline: { hook: "x" } } },
                advanced_at: {},
              },
              error: null,
            },
          },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("422 when client_id missing", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "configuration",
                format_choice: "image",
                client_id: null,
                config_draft: { image_payload: validImagePayload },
                advanced_at: {},
              },
              error: null,
            },
          },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("500 when client lookup errors", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "configuration",
                format_choice: "image",
                client_id: clientId,
                config_draft: { image_payload: validImagePayload },
                advanced_at: {},
              },
              error: null,
            },
          },
        },
        clients: { select: { single: { data: null, error: { message: "boom" } } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("422 when client not found", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "configuration",
                format_choice: "image",
                client_id: clientId,
                config_draft: { image_payload: validImagePayload },
                advanced_at: {},
              },
              error: null,
            },
          },
        },
        clients: { select: { single: { data: null, error: null } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("500 when image RPC fails", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "image",
                  client_id: clientId,
                  config_draft: { image_payload: validImagePayload },
                  advanced_at: {},
                },
                error: null,
              },
            },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
        }),
        () => ({ data: null, error: { message: "rpc fail" } }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when image brief insert fails", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "image",
                  client_id: clientId,
                  config_draft: { image_payload: validImagePayload },
                  advanced_at: {},
                },
                error: null,
              },
            },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: null, error: { message: "dup" } } } },
        }),
        () => ({ data: "X", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when video RPC fails (compensating image delete)", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "both",
                  client_id: clientId,
                  config_draft: {
                    image_payload: validImagePayload,
                    video_payload: validVideoPayload,
                  },
                  advanced_at: {},
                },
                error: null,
              },
            },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
        }),
        (name) =>
          name === "gen_brief_id_human"
            ? { data: "ACME", error: null }
            : { data: null, error: { message: "video rpc fail" } },
      );

      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when video brief insert fails (compensating)", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "both",
                  client_id: clientId,
                  config_draft: {
                    image_payload: validImagePayload,
                    video_payload: validVideoPayload,
                  },
                  advanced_at: {},
                },
                error: null,
              },
            },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          video_briefs: { insert: { single: { data: null, error: { message: "v dup" } } } },
        }),
        () => ({ data: "ACME", error: null }),
      );

      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when pipeline update fails (compensating cleans both briefs)", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "both",
                  client_id: clientId,
                  config_draft: {
                    image_payload: validImagePayload,
                    video_payload: validVideoPayload,
                  },
                  advanced_at: {},
                },
                error: null,
              },
            },
            // Silent-failure PR-3: error rides the base-result on update.
            update: { data: null, error: { message: "race" } },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          video_briefs: { insert: { single: { data: { id: "vb1" }, error: null } } },
        }),
        () => ({ data: "ACME", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when pipeline_events insert fails (silent-failure PR-3: no longer swallowed)", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "image",
                  client_id: clientId,
                  config_draft: { image_payload: validImagePayload },
                  advanced_at: {},
                },
                error: null,
              },
            },
            update: { data: { id }, error: null },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: { message: "events down" } } },
        }),
        () => ({ data: "ACME", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      // Silent-failure PR-3: the stage_advanced event drives the reducer;
      // a failed insert no longer console.warns and returns 200.
      expect(res.status).toBe(500);
      const body = await res.json();
      expect(String(body.error)).toContain("events down");
    });

    // Silent-failure PR-3: the legacy "worker kick" tests are gone -- the
    // route no longer fetch()es the worker; the work_item enqueue is the
    // only producer and the daemon/worker pulls the queued row.

    it("does not throw when worker returns 404 (pre-launch behaviour)", async () => {
      process.env.WORKER_URL = "http://worker.local";
      process.env.WORKER_SHARED_SECRET = "secret";
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("nope", { status: 404 }));
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: {
              single: {
                data: {
                  id,
                  status: "configuration",
                  format_choice: "image",
                  client_id: clientId,
                  config_draft: { image_payload: validImagePayload },
                  advanced_at: {},
                },
                error: null,
              },
            },
            update: { single: { data: { id }, error: null } },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });
  });

  describe("ideation → review", () => {
    it("happy path image (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "ideation",
                format_choice: "image",
                picks: { image: ["c1"] },
                advanced_at: {},
              },
              error: null,
            },
          },
          update: { single: { data: { id, status: "review" }, error: null } },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });

    it("happy path both with arrays of strings (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "ideation",
                format_choice: "both",
                picks: { image: ["c1"], video: ["v1"] },
                advanced_at: { ideation: "t" },
              },
              error: null,
            },
          },
          update: { single: { data: { id }, error: null } },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });

    it("422 missing image picks", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "ideation",
                format_choice: "image",
                picks: {},
                advanced_at: {},
              },
              error: null,
            },
          },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("422 with malformed picks (array)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "ideation",
                format_choice: "image",
                picks: [],
                advanced_at: null,
              },
              error: null,
            },
          },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("filters non-string picks in image array", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "ideation",
                format_choice: "image",
                picks: { image: [123, "c1"] },
                advanced_at: {},
              },
              error: null,
            },
          },
          update: { single: { data: { id }, error: null } },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });

    it("500 when update fails", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "ideation",
                format_choice: "image",
                picks: { image: ["c1"] },
                advanced_at: {},
              },
              error: null,
            },
          },
          // Silent-failure PR-3: error rides the base-result on update.
          update: { data: null, error: { message: "race" } },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when event insert fails (silent-failure PR-3: no longer swallowed)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: {
            single: {
              data: {
                id,
                status: "ideation",
                format_choice: "image",
                picks: { image: ["c1"] },
                advanced_at: {},
              },
              error: null,
            },
          },
          update: { data: { id }, error: null },
        },
        pipeline_events: { insert: { data: null, error: { message: "events down" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
      const body = await res.json();
      expect(String(body.error)).toContain("events down");
    });
  });

  describe("per-creative gated stages (hard block)", () => {
    function perCreativePipeline(status: string) {
      return {
        id,
        status,
        format_choice: "image",
        config_draft: null,
        advanced_at: {},
      };
    }

    it("advances creative_qa when every creative is passed (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("creative_qa"), error: null } },
          update: { single: { data: { id, status: "copy" }, error: null } },
        },
        creative_stage_state: {
          select: { data: [{ status: "passed" }, { status: "passed" }], error: null },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      // Reordered DAG: creative_qa now advances to copy (copy before compliance).
      expect(body.pipeline.status).toBe("copy");
    });

    it("422 HARD-BLOCKS compliance_review while a creative is failed", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("compliance_review"), error: null } },
        },
        creative_stage_state: {
          select: { data: [{ status: "passed" }, { status: "failed" }], error: null },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.field).toBe("rollup");
      expect(body.stage).toBe("compliance_review");
      expect(body.rollup.blocking).toBe(1);
      expect(String(body.error)).toContain("HARD gate");
    });

    it("422 when no creative_stage_state rows exist (uncleared rollup)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("compliance_review"), error: null } },
        },
        creative_stage_state: { select: { data: [], error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("500 when the creative_stage_state read errors", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("creative_qa"), error: null } },
        },
        creative_stage_state: {
          select: { data: null, error: { message: "rollup read failed" } },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("drops a killed creative from the rollup scope so it cannot hold the gate", async () => {
      // creative_qa has one passed (in-scope) creative + one FAILED creative that
      // is killed. The killed creative must NOT hold the gate (E2.3 drift fix), so
      // the rollup clears and the advance succeeds.
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("creative_qa"), error: null } },
          update: { single: { data: { id, status: "copy" }, error: null } },
        },
        creative_stage_state: {
          select: {
            data: [
              { status: "passed", creative_id: "c1" },
              { status: "failed", creative_id: "c2" },
            ],
            error: null,
          },
        },
        // c2 is killed -> dropped from scope.
        creatives: { select: { data: [{ id: "c2" }], error: null } },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.pipeline.status).toBe("copy");
    });

    it("treats overridden + skipped as cleared and advances compliance_review", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("compliance_review"), error: null } },
          update: { single: { data: { id, status: "spec_validation" }, error: null } },
        },
        creative_stage_state: {
          select: { data: [{ status: "overridden" }, { status: "skipped" }], error: null },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.pipeline.status).toBe("spec_validation");
    });

    it("blocked -> override -> advances (integration of the hard gate)", async () => {
      // 1. Blocked: a failed compliance unit holds the gate shut (422).
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("compliance_review"), error: null } },
        },
        creative_stage_state: {
          select: { data: [{ status: "failed" }], error: null },
        },
      });
      const blocked = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(blocked.status).toBe(422);

      // 2. Override (simulated by the manager route): the unit is now overridden.
      // 3. Re-advance: the rollup now reads as cleared, so the gate opens.
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("compliance_review"), error: null } },
          update: { single: { data: { id, status: "spec_validation" }, error: null } },
        },
        creative_stage_state: {
          select: { data: [{ status: "overridden" }], error: null },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const advanced = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(advanced.status).toBe(200);
      const body = await advanced.json();
      expect(body.pipeline.status).toBe("spec_validation");
    });

    it("500 when the pipeline update races (compare-and-set miss)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("spec_validation"), error: null } },
          // Silent-failure PR-3: the route awaits the chain directly; the
          // error rides the base-result on update.
          update: { data: null, error: { message: "race" } },
        },
        creative_stage_state: { select: { data: [{ status: "passed" }], error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when the stage_advanced event insert fails (silent-failure PR-3: no longer swallowed)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreativePipeline("spec_validation"), error: null } },
          update: { data: { id, status: "variant_plan" }, error: null },
        },
        creative_stage_state: { select: { data: [{ status: "passed" }], error: null } },
        pipeline_events: { insert: { data: null, error: { message: "events down" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
      const body = await res.json();
      expect(String(body.error)).toContain("events down");
    });
  });

  describe("FIX-A: dispatch-on-entry for post-generation stages", () => {
    // The post-gen per-creative stages had no dispatch PRODUCER once the
    // pipeline left creative_qa (the verdict-writers were reachable only by the
    // manual routes / e2e harness), so every pipeline deadlocked. Each
    // per-creative advance now enqueues the NEXT stage's producer on entry,
    // branching on operator_driven so the two execution models never both fire.
    function perCreative(status: string, operatorDriven = false) {
      return {
        id,
        status,
        format_choice: "image",
        config_draft: operatorDriven ? { operator_driven: true } : null,
        advanced_at: {},
      };
    }

    it("deterministic copy->compliance_review enqueues worker_compliance", async () => {
      // Reordered DAG: copy now precedes compliance. The copy gate is
      // approved-count based, so mock creatives + approved copy_variants.
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreative("copy"), error: null } },
          update: { single: { data: { id, status: "compliance_review" }, error: null } },
        },
        creatives: { select: { data: [{ id: "c1", status: "draft" }], error: null } },
        copy_variants: {
          select: {
            data: [
              { creative_id: "c1", status: "approved" },
              { creative_id: "c1", status: "approved" },
              { creative_id: "c1", status: "approved" },
            ],
            error: null,
          },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      expect(enqueueWorkItem).toHaveBeenCalledTimes(1);
      const opts = enqueueWorkItem.mock.calls[0]?.[0] as Record<string, unknown>;
      expect(opts.kind).toBe("worker_compliance");
      expect(opts.idempotencyKey).toBe(`wi:${id}:compliance_review`);
      expect((opts.payload as Record<string, unknown>).stage).toBe("compliance_review");
    });

    it("creative_qa->copy is manual (no enqueue); compliance_review->spec_validation enqueues worker_spec", async () => {
      // creative_qa -> copy: deterministic copy is MANUAL, so NO enqueue.
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreative("creative_qa"), error: null } },
          update: { single: { data: { id, status: "copy" }, error: null } },
        },
        creative_stage_state: { select: { data: [{ status: "passed" }], error: null } },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const toCopy = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(toCopy.status).toBe(200);
      expect(enqueueWorkItem).not.toHaveBeenCalled();

      // compliance_review -> spec_validation: enqueues worker_spec.
      enqueueWorkItem.mockClear();
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreative("compliance_review"), error: null } },
          update: { single: { data: { id, status: "spec_validation" }, error: null } },
        },
        creative_stage_state: { select: { data: [{ status: "passed" }], error: null } },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const toSpec = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(toSpec.status).toBe(200);
      expect(enqueueWorkItem).toHaveBeenCalledTimes(1);
      const opts = enqueueWorkItem.mock.calls[0]?.[0] as Record<string, unknown>;
      expect(opts.kind).toBe("worker_spec");
      expect(opts.idempotencyKey).toBe(`wi:${id}:spec_validation`);
    });

    it("deterministic spec_validation->variant_plan enqueues NOTHING", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreative("spec_validation"), error: null } },
          update: { single: { data: { id, status: "variant_plan" }, error: null } },
        },
        creative_stage_state: { select: { data: [{ status: "passed" }], error: null } },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      expect(enqueueWorkItem).not.toHaveBeenCalled();
    });

    it("operator creative_qa->copy enqueues operator_dispatch", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreative("creative_qa", true), error: null } },
          update: { single: { data: { id, status: "copy" }, error: null } },
        },
        creative_stage_state: { select: { data: [{ status: "passed" }], error: null } },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      expect(enqueueWorkItem).toHaveBeenCalledTimes(1);
      const opts = enqueueWorkItem.mock.calls[0]?.[0] as Record<string, unknown>;
      expect(opts.kind).toBe("operator_dispatch");
      expect(opts.idempotencyKey).toBe(`op-disp:${id}:copy:advance`);
      const payload = opts.payload as Record<string, unknown>;
      expect(payload.stage).toBe("copy");
      expect(String(payload.instruction)).toContain(id);
    });

    it("500 when the stage dispatch enqueue fails (not swallowed)", async () => {
      enqueueWorkItem.mockRejectedValueOnce(new Error("work_item insert failed: boom"));
      // compliance_review -> spec_validation dispatches worker_spec, so the
      // enqueue failure surfaces (creative_qa -> copy would be manual / no enqueue).
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: perCreative("compliance_review"), error: null } },
          update: { single: { data: { id, status: "spec_validation" }, error: null } },
        },
        creative_stage_state: { select: { data: [{ status: "passed" }], error: null } },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
      const body = await res.json();
      expect(String(body.error)).toContain("stage dispatch enqueue failed");
    });
  });

  describe("copy → compliance_review (approved-copy gate, no-stall wiring)", () => {
    function copyPipeline() {
      return {
        id,
        status: "copy",
        format_choice: "image",
        config_draft: null,
        advanced_at: {},
      };
    }

    // The copy gate re-derives ≥3 approved copy variants per in-scope creative
    // (NOT the creative_stage_state rollup, which the operator copy tool only
    // ever rolls to in_progress — gating on it would stall the stage).
    it("advances when every creative has >=3 approved copy variants (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: copyPipeline(), error: null } },
          update: { single: { data: { id, status: "compliance_review" }, error: null } },
        },
        creatives: { select: { data: [{ id: "c1", status: "draft" }], error: null } },
        copy_variants: {
          select: {
            data: [
              { creative_id: "c1", status: "approved" },
              { creative_id: "c1", status: "approved" },
              { creative_id: "c1", status: "approved" },
            ],
            error: null,
          },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.pipeline.status).toBe("compliance_review");
    });

    it("422 when a creative is short on approved copy", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: copyPipeline(), error: null } } },
        creatives: { select: { data: [{ id: "c1", status: "draft" }], error: null } },
        copy_variants: {
          select: { data: [{ creative_id: "c1", status: "approved" }], error: null },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.field).toBe("copy");
      expect(body.copy.short).toBe(1);
    });

    it("422 when no in-scope creatives exist", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: copyPipeline(), error: null } } },
        creatives: { select: { data: [{ id: "c1", status: "killed" }], error: null } },
        copy_variants: { select: { data: [], error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("500 when the creatives read errors", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: copyPipeline(), error: null } } },
        creatives: { select: { data: null, error: { message: "boom" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when the copy_variants read errors", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: copyPipeline(), error: null } } },
        creatives: { select: { data: [{ id: "c1", status: "draft" }], error: null } },
        copy_variants: { select: { data: null, error: { message: "boom" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });
  });

  // -------------------------------------------------------------------------
  // B2: the copy gate counts BOTH tracks. A video creative's copy lives in
  // `video_copy_variants` (joined to video_creatives via the pipeline's
  // video_brief_id). Video creatives are always in scope (no `killed` value).
  // -------------------------------------------------------------------------
  describe("copy gate - video track (B2)", () => {
    function videoCopyPipeline(format: "video" | "both") {
      return {
        id,
        status: "copy",
        format_choice: format,
        video_brief_id: "vb1",
        config_draft: null,
        advanced_at: {},
      };
    }

    it("advances a video-only pipeline when the video creative has >=3 approved video copy (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: videoCopyPipeline("video"), error: null } },
          update: { single: { data: { id, status: "compliance_review" }, error: null } },
        },
        video_creatives: { select: { data: [{ id: "vc1", status: "captioned" }], error: null } },
        video_copy_variants: {
          select: {
            data: [
              { creative_id: "vc1", status: "approved" },
              { creative_id: "vc1", status: "approved" },
              { creative_id: "vc1", status: "approved" },
            ],
            error: null,
          },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.pipeline.status).toBe("compliance_review");
    });

    it("422 when a video creative is short on approved video copy", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: videoCopyPipeline("video"), error: null } } },
        video_creatives: { select: { data: [{ id: "vc1", status: "captioned" }], error: null } },
        video_copy_variants: {
          select: { data: [{ creative_id: "vc1", status: "approved" }], error: null },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.field).toBe("copy");
      expect(body.copy.short).toBe(1);
    });

    it("format=both: counts image AND video creatives in the same gate (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: videoCopyPipeline("both"), error: null } },
          update: { single: { data: { id, status: "compliance_review" }, error: null } },
        },
        creatives: { select: { data: [{ id: "c1", status: "draft" }], error: null } },
        copy_variants: {
          select: {
            data: [
              { creative_id: "c1", status: "approved" },
              { creative_id: "c1", status: "approved" },
              { creative_id: "c1", status: "approved" },
            ],
            error: null,
          },
        },
        video_creatives: { select: { data: [{ id: "vc1", status: "captioned" }], error: null } },
        video_copy_variants: {
          select: {
            data: [
              { creative_id: "vc1", status: "approved" },
              { creative_id: "vc1", status: "approved" },
              { creative_id: "vc1", status: "approved" },
            ],
            error: null,
          },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });

    it("format=both: 422 when the video creative is short even though image is satisfied", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: videoCopyPipeline("both"), error: null } } },
        creatives: { select: { data: [{ id: "c1", status: "draft" }], error: null } },
        copy_variants: {
          select: {
            data: [
              { creative_id: "c1", status: "approved" },
              { creative_id: "c1", status: "approved" },
              { creative_id: "c1", status: "approved" },
            ],
            error: null,
          },
        },
        video_creatives: { select: { data: [{ id: "vc1", status: "captioned" }], error: null } },
        video_copy_variants: { select: { data: [], error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.copy.short).toBe(1);
    });

    it("500 when the video_creatives read errors", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: videoCopyPipeline("video"), error: null } } },
        video_creatives: { select: { data: null, error: { message: "vc boom" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when the video_copy_variants read errors", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: videoCopyPipeline("video"), error: null } } },
        video_creatives: { select: { data: [{ id: "vc1", status: "captioned" }], error: null } },
        video_copy_variants: { select: { data: null, error: { message: "vcv boom" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });
  });

  describe("finalize_assets → launch_handoff (no-stall wiring)", () => {
    function finalizePipeline() {
      return {
        id,
        status: "finalize_assets",
        format_choice: "image",
        config_draft: null,
        advanced_at: {},
      };
    }

    it("advances when every creative is finalize_verified (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: finalizePipeline(), error: null } },
          update: { single: { data: { id, status: "launch_handoff" }, error: null } },
        },
        creatives: {
          select: {
            data: [
              { id: "c1", finalize_verified: true },
              { id: "c2", finalize_verified: true },
            ],
            error: null,
          },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.pipeline.status).toBe("launch_handoff");
    });

    it("422 when a creative is not finalize_verified (gate holds)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: finalizePipeline(), error: null } },
        },
        creatives: {
          select: {
            data: [
              { id: "c1", finalize_verified: true },
              { id: "c2", finalize_verified: false },
            ],
            error: null,
          },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.field).toBe("finalize");
      expect(body.finalize.unverified).toBe(1);
    });

    it("422 when no creatives exist for the pipeline (nothing finalized)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: finalizePipeline(), error: null } },
        },
        creatives: { select: { data: [], error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
    });

    it("500 when the creatives read errors", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: finalizePipeline(), error: null } },
        },
        creatives: { select: { data: null, error: { message: "read failed" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when the finalize advance update races", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: finalizePipeline(), error: null } },
          // Silent-failure PR-3: error rides the base-result on update.
          update: { data: null, error: { message: "race" } },
        },
        creatives: { select: { data: [{ id: "c1", finalize_verified: true }], error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });

    it("500 when the stage_advanced event insert fails (silent-failure PR-3: no longer swallowed)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: finalizePipeline(), error: null } },
          update: { data: { id, status: "launch_handoff" }, error: null },
        },
        creatives: { select: { data: [{ id: "c1", finalize_verified: true }], error: null } },
        pipeline_events: { insert: { data: null, error: { message: "events down" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
      const body = await res.json();
      expect(String(body.error)).toContain("events down");
    });
  });

  // -------------------------------------------------------------------------
  // B3: finalize gate counts BOTH tracks. A video creative's finalize state is
  // video_creatives.finalize_verified (migration 0031), joined via the
  // pipeline's video_brief_id.
  // -------------------------------------------------------------------------
  describe("finalize_assets - video track (B3)", () => {
    function videoFinalizePipeline(format: "video" | "both") {
      return {
        id,
        status: "finalize_assets",
        format_choice: format,
        video_brief_id: "vb1",
        config_draft: null,
        advanced_at: {},
      };
    }

    it("advances a video-only pipeline when every video creative is finalize_verified (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: videoFinalizePipeline("video"), error: null } },
          update: { single: { data: { id, status: "launch_handoff" }, error: null } },
        },
        video_creatives: {
          select: { data: [{ id: "vc1", finalize_verified: true }], error: null },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.pipeline.status).toBe("launch_handoff");
    });

    it("422 when a video creative is not finalize_verified (gate holds)", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: videoFinalizePipeline("video"), error: null } } },
        video_creatives: {
          select: {
            data: [
              { id: "vc1", finalize_verified: true },
              { id: "vc2", finalize_verified: false },
            ],
            error: null,
          },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.field).toBe("finalize");
      expect(body.finalize.unverified).toBe(1);
    });

    it("format=both: counts image AND video creatives in the finalize gate (200)", async () => {
      currentSupabase = mockClient({
        pipelines: {
          select: { single: { data: videoFinalizePipeline("both"), error: null } },
          update: { single: { data: { id, status: "launch_handoff" }, error: null } },
        },
        creatives: { select: { data: [{ id: "c1", finalize_verified: true }], error: null } },
        video_creatives: {
          select: { data: [{ id: "vc1", finalize_verified: true }], error: null },
        },
        pipeline_events: { insert: { data: null, error: null } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });

    it("format=both: 422 when the video creative is unverified even though image is verified", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: videoFinalizePipeline("both"), error: null } } },
        creatives: { select: { data: [{ id: "c1", finalize_verified: true }], error: null } },
        video_creatives: {
          select: { data: [{ id: "vc1", finalize_verified: false }], error: null },
        },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(422);
      const body = await res.json();
      expect(body.finalize.unverified).toBe(1);
    });

    it("500 when the video_creatives read errors", async () => {
      currentSupabase = mockClient({
        pipelines: { select: { single: { data: videoFinalizePipeline("video"), error: null } } },
        video_creatives: { select: { data: null, error: { message: "vc read failed" } } },
      });
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
    });
  });

  // -- silent-failure foundational redesign PR-2b: dual-write to work_item --

  describe("dual-write enqueue (operator-driven configuration -> ideation)", () => {
    function operatorDrivenPipeline() {
      return {
        id,
        status: "configuration",
        format_choice: "image",
        client_id: clientId,
        image_brief_id: "op-brief-1",
        config_draft: {
          operator_driven: true,
          image_payload: validImagePayload,
        },
        advanced_at: {},
      };
    }

    it("enqueues an operator_dispatch work_item with the right idempotency_key (200)", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: { single: { data: operatorDrivenPipeline(), error: null } },
            update: { single: { data: { id, status: "ideation" }, error: null } },
          },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME-2026-0001", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      expect(enqueueWorkItem).toHaveBeenCalledTimes(1);
      const opts = enqueueWorkItem.mock.calls[0]?.[0] as Record<string, unknown>;
      expect(opts.kind).toBe("operator_dispatch");
      expect(opts.pipelineId).toBe(id);
      expect(opts.idempotencyKey).toBe(`op-disp:${id}:ideation:config_approved`);
      expect(opts.createdBy).toBe("api/pipelines/advance/retaskOperator");
      const payload = opts.payload as Record<string, unknown>;
      expect(payload.stage).toBe("ideation");
    });

    it("returns 500 when the work_item enqueue throws (operator-driven path)", async () => {
      enqueueWorkItem.mockRejectedValueOnce(new Error("work_item insert failed: boom"));
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: { single: { data: operatorDrivenPipeline(), error: null } },
            update: { single: { data: { id, status: "ideation" }, error: null } },
          },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME-2026-0001", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
      const body = await res.json();
      expect(String(body.error)).toContain("work_item enqueue failed");
      // Silent-failure PR-3: the enqueue was the sole producer; nothing
      // else fired (the legacy fire-and-forget dispatchOperator is gone).
    });

    it("treats a duplicate idempotency_key as 200 (the dedup branch)", async () => {
      enqueueWorkItem.mockResolvedValueOnce({ id: "wi-existing", duplicate: true });
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: { single: { data: operatorDrivenPipeline(), error: null } },
            update: { single: { data: { id, status: "ideation" }, error: null } },
          },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });
  });

  describe("dual-write enqueue (regular configuration -> ideation worker_ideation)", () => {
    function regularPipeline() {
      return {
        id,
        status: "configuration",
        format_choice: "image",
        client_id: clientId,
        config_draft: { image_payload: validImagePayload },
        advanced_at: {},
      };
    }

    it("enqueues a worker_ideation work_item with the right idempotency_key (200)", async () => {
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: { single: { data: regularPipeline(), error: null } },
            update: { single: { data: { id, status: "ideation" }, error: null } },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME-2026-0001", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
      expect(enqueueWorkItem).toHaveBeenCalledTimes(1);
      const opts = enqueueWorkItem.mock.calls[0]?.[0] as Record<string, unknown>;
      expect(opts.kind).toBe("worker_ideation");
      expect(opts.pipelineId).toBe(id);
      expect(opts.idempotencyKey).toBe(`wi:${id}:ideation`);
      // Silent-failure PR-3: createdBy was renamed when the legacy
      // fire-and-forget worker kick (fireWorkerIdeation) was removed.
      expect(opts.createdBy).toBe("api/pipelines/advance/worker_ideation");
    });

    it("returns 500 when the worker_ideation enqueue throws", async () => {
      enqueueWorkItem.mockRejectedValueOnce(new Error("work_item insert failed: boom"));
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: { single: { data: regularPipeline(), error: null } },
            update: { single: { data: { id, status: "ideation" }, error: null } },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(500);
      const body = await res.json();
      expect(String(body.error)).toContain("work_item enqueue failed");
    });

    it("treats a duplicate idempotency_key as 200 (the dedup branch)", async () => {
      enqueueWorkItem.mockResolvedValueOnce({ id: "wi-existing", duplicate: true });
      currentSupabase = withRpc(
        mockClient({
          pipelines: {
            select: { single: { data: regularPipeline(), error: null } },
            update: { single: { data: { id, status: "ideation" }, error: null } },
          },
          clients: { select: { single: { data: { slug: "acme" }, error: null } } },
          briefs: { insert: { single: { data: { id: "ib1" }, error: null } } },
          pipeline_events: { insert: { data: null, error: null } },
        }),
        () => ({ data: "ACME", error: null }),
      );
      const res = await POST(
        req(`http://localhost/api/pipelines/${id}/advance`, { method: "POST" }),
        { params },
      );
      expect(res.status).toBe(200);
    });
  });
});
