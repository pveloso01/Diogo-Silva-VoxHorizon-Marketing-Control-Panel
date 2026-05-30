import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let result: { data: unknown; error: { message: string } | null } = { data: [], error: null };
const lastCalls: {
  eq?: [string, unknown];
  in?: [string, unknown[]];
  is?: [string, unknown];
  not?: [string, string, unknown];
} = {};

function chain() {
  const c: Record<string, unknown> = {};
  c.select = vi.fn(() => c);
  c.order = vi.fn(() => c);
  c.limit = vi.fn(() => c);
  c.eq = vi.fn((col: string, val: unknown) => {
    lastCalls.eq = [col, val];
    return c;
  });
  c.in = vi.fn((col: string, val: unknown[]) => {
    lastCalls.in = [col, val];
    return c;
  });
  c.is = vi.fn((col: string, val: unknown) => {
    lastCalls.is = [col, val];
    return c;
  });
  c.not = vi.fn((col: string, op: string, val: unknown) => {
    lastCalls.not = [col, op, val];
    return c;
  });
  (c as { then: unknown }).then = (onF: (v: typeof result) => unknown) =>
    Promise.resolve(result).then(onF);
  return c;
}
const from = vi.fn(() => chain());
vi.mock("@/lib/supabase/admin", () => ({
  createAdminClient: () => ({ from }),
}));

import { GET } from "./route";

function req(qs: string): Request {
  return new Request(`http://x/api/creatives${qs}`);
}

beforeEach(() => {
  result = { data: [], error: null };
  delete lastCalls.eq;
  delete lastCalls.in;
  delete lastCalls.is;
  delete lastCalls.not;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GET /api/creatives", () => {
  it("queries by brief_id", async () => {
    result = { data: [{ id: "c1" }], error: null };
    const res = await GET(req("?brief_id=b1") as never);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ creatives: [{ id: "c1" }] });
    expect(lastCalls.eq).toEqual(["brief_id", "b1"]);
    // Soft-deleted creatives must be excluded from the ideation grid.
    expect(lastCalls.is).toEqual(["deleted_at", null]);
  });

  it("500s on a brief_id query error", async () => {
    result = { data: null, error: { message: "rls" } };
    const res = await GET(req("?brief_id=b1") as never);
    expect(res.status).toBe(500);
    expect((await res.json()).error).toBe("rls");
  });

  it("queries by an explicit id set", async () => {
    result = { data: [{ id: "c1" }, { id: "c2" }], error: null };
    const res = await GET(req("?ids=c1,c2") as never);
    expect(await res.json()).toEqual({ creatives: [{ id: "c1" }, { id: "c2" }] });
    expect(lastCalls.in).toEqual(["id", ["c1", "c2"]]);
  });

  it("returns [] when ids has only separators (empty after split)", async () => {
    const res = await GET(req("?ids=,,") as never);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ creatives: [] });
  });

  it("treats an empty ?ids= as no selector → 400", async () => {
    const res = await GET(req("?ids=") as never);
    expect(res.status).toBe(400);
  });

  it("500s on an ids query error", async () => {
    result = { data: null, error: { message: "denied" } };
    const res = await GET(req("?ids=c1") as never);
    expect(res.status).toBe(500);
  });

  it("lists the whole active set when neither brief_id nor ids is provided", async () => {
    result = { data: [{ id: "c1" }, { id: "c2" }], error: null };
    const res = await GET(req("") as never);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ creatives: [{ id: "c1" }, { id: "c2" }] });
    // Active view filters on deleted_at is null.
    expect(lastCalls.is).toEqual(["deleted_at", null]);
  });

  it("lists the archived set with ?archived=true", async () => {
    result = { data: [{ id: "c9" }], error: null };
    const res = await GET(req("?archived=true") as never);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ creatives: [{ id: "c9" }] });
    expect(lastCalls.not).toEqual(["deleted_at", "is", null]);
  });

  it("500s on a whole-set list query error", async () => {
    result = { data: null, error: { message: "boom" } };
    const res = await GET(req("") as never);
    expect(res.status).toBe(500);
  });
});
