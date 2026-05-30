import { NextResponse, type NextRequest } from "next/server";

import { createAdminClient } from "@/lib/supabase/admin";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/creatives?brief_id=<uuid>  OR  ?ids=<id,id,...>  OR  (no params)
 *
 * Lists image creatives either for a brief (ideation grid), by an explicit id
 * set (review picks), or — when neither is supplied — the whole active set for
 * the unified Creatives grid (M4 / #593). Reads via the service-role client;
 * gated by Caddy basic auth.
 *
 * `ids` preserves no particular order (callers re-order client-side); rows are
 * returned oldest-first when querying by brief.
 *
 * The whole-set listing supports the makeover archive view:
 *   - default (`?archived` absent): only active rows (`deleted_at is null`).
 *   - `?archived=true`: only archived rows (`deleted_at is not null`).
 * Newest-first, capped at 1000 rows (the grid filters/sorts client-side).
 *
 * Returns: `{ creatives: Creative[] }`.
 */
export async function GET(req: NextRequest) {
  const url = new URL(req.url);
  const briefId = url.searchParams.get("brief_id");
  const idsParam = url.searchParams.get("ids");
  const archived = url.searchParams.get("archived") === "true";

  const supabase = createAdminClient();

  // Whole-set listing only when BOTH selectors are truly absent. An explicit
  // empty `?ids=` keeps its legacy "no selector -> 400" meaning below.
  if (!briefId && idsParam === null) {
    let query = supabase
      .from("creatives")
      .select("*")
      .order("created_at", { ascending: false })
      .limit(1000);
    query = archived ? query.not("deleted_at", "is", null) : query.is("deleted_at", null);
    const { data, error } = await query;
    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
    return NextResponse.json({ creatives: data ?? [] });
  }

  if (briefId) {
    const { data, error } = await supabase
      .from("creatives")
      .select("*")
      .eq("brief_id", briefId)
      // Exclude soft-deleted rows: the ideation grid must show only live
      // concept previews. A re-dispatch supersedes a prior render by
      // soft-deleting the stale creatives; without this filter they linger as
      // "No render yet" blank cards alongside the real ones.
      .is("deleted_at", null)
      .order("created_at", { ascending: true });
    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
    return NextResponse.json({ creatives: data ?? [] });
  }

  if (idsParam) {
    const ids = idsParam
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (ids.length === 0) {
      return NextResponse.json({ creatives: [] });
    }
    const { data, error } = await supabase.from("creatives").select("*").in("id", ids);
    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
    return NextResponse.json({ creatives: data ?? [] });
  }

  return NextResponse.json({ error: "missing_brief_id_or_ids" }, { status: 400 });
}
