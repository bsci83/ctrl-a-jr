/**
 * GET  /api/agent/runs  → the recent-run list
 * POST /api/agent/runs  → start a run from a human message
 *
 * A thin, server-side proxy. Its whole reason to exist is that the browser must
 * not hold `CTRLA_JR_PUSH_TOKEN` — see the header of lib/agent.ts. This file
 * adds no policy of its own beyond validating the message.
 */

import { createRun, listRuns } from "@/lib/agent";

import { messageOf, respond } from "../_respond";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
/** One turn can be a model round trip plus IMAP and Stripe calls. */
export const maxDuration = 120;

export async function GET() {
  return respond(await listRuns());
}

export async function POST(request: Request) {
  const parsed = await messageOf(request);
  if ("error" in parsed) return parsed.error;
  return respond(await createRun(parsed.message));
}
