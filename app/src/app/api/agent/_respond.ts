/**
 * The shared shape every /api/agent/* handler answers with.
 *
 * The browser never learns anything about the upstream call except a status
 * and a sentence: no headers, no URL, and above all no bearer token. An
 * `AgentResult` is already sanitised by lib/agent; this just puts it on the
 * wire and keeps every handler from inventing its own error envelope.
 */

import { NextResponse } from "next/server";

import type { AgentResult } from "@/lib/agent";

export function respond<T>(result: AgentResult<T>): NextResponse {
  if (!result.ok) {
    return NextResponse.json(
      { error: result.error },
      { status: result.status, headers: { "Cache-Control": "no-store" } },
    );
  }
  return NextResponse.json(result.data, {
    status: result.status,
    headers: { "Cache-Control": "no-store" },
  });
}

/** The human's message, validated here so an empty POST never becomes a run. */
export async function messageOf(
  request: Request,
): Promise<{ message: string } | { error: NextResponse }> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return { error: NextResponse.json({ error: "body must be JSON" }, { status: 400 }) };
  }
  const raw = (body as { message?: unknown } | null)?.message;
  if (typeof raw !== "string" || !raw.trim()) {
    return {
      error: NextResponse.json(
        { error: "message is required and must be a non-empty string" },
        { status: 400 },
      ),
    };
  }
  return { message: raw.trim() };
}
