/**
 * POST /api/agent/runs/<id>/advance → continue a run after a decision.
 *
 * This carries NO decision and cannot. Upstream, `/advance` only copies across
 * a decision a human already made on the signed approval page; if there is
 * none, the run comes back still awaiting and nothing executes. That is why
 * this app has no approve or deny control: there is exactly one place a
 * decision can be made, and it is not here.
 */

import { advanceRun } from "@/lib/agent";

import { respond } from "../../../_respond";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const maxDuration = 120;

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return respond(await advanceRun(id));
}
