/** POST /api/agent/runs/<id>/messages → add a human turn to a finished run. */

import { postMessage } from "@/lib/agent";

import { messageOf, respond } from "../../../_respond";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const maxDuration = 120;

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const parsed = await messageOf(request);
  if ("error" in parsed) return parsed.error;
  return respond(await postMessage(id, parsed.message));
}
