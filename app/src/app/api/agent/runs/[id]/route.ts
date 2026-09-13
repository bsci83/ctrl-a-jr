/** GET /api/agent/runs/<id> → one run's conversation and pending approval. */

import { getRun } from "@/lib/agent";

import { respond } from "../../_respond";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const maxDuration = 60;

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return respond(await getRun(id));
}
