/**
 * THE ONLY PLACE THIS APP TALKS TO THE AGENT API.
 *
 * Companion to lib/evidence.ts: that module reads a frozen export, this one
 * talks to the live agent. Everything here runs SERVER-SIDE ONLY.
 *
 * Why that matters more than usual
 * --------------------------------
 * `CTRLA_JR_PUSH_TOKEN` is not a read key. A caller holding it can POST
 * /api/runs, and a run sends real email from the shop's Gmail account and
 * creates real Stripe invoices. Putting it in a `NEXT_PUBLIC_` variable, or
 * calling the agent API from a client component, would publish the ability to
 * act as the shop to anyone who opens the page.
 *
 * So:
 *  - the token is read from `process.env` inside this module and nowhere else;
 *  - this module is structurally server-only — it imports `node:process`, which
 *    makes a client-component import a build error rather than a leak;
 *  - the browser reaches the agent exclusively through the route handlers in
 *    src/app/api/agent/**, which call these functions;
 *  - nothing here ever returns the token, or an error string that could quote
 *    it, to a caller.
 */

import process from "node:process";

import type { RunPayload, RunSummary } from "./types-agent";

export type {
  AgentTurn,
  PendingApproval,
  RunPayload,
  RunStatus,
  RunSummary,
} from "./types-agent";

const DEFAULT_BASE = "https://ctrl-a-jr.vercel.app";

/**
 * One model turn plus its tool calls. The agent API deliberately drives ONE
 * `advance` per request, but that single turn reads a real IMAP inbox and calls
 * a real model: 30s+ is normal, not a fault. A 10s timeout here would abort
 * work that the agent then finishes anyway, leaving the UI describing a run
 * state that is already stale.
 */
const TURN_TIMEOUT_MS = 120_000;
const READ_TIMEOUT_MS = 30_000;

/** What a route handler hands back. Never an exception: a transport failure is
 * a state this UI has to draw, not a 500 page. */
export type AgentResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; error: string };

function baseUrl(): string {
  const raw = (process.env.CTRLA_JR_APPROVAL_API || DEFAULT_BASE).trim();
  return raw.replace(/\/+$/, "");
}

function token(): string {
  return (process.env.CTRLA_JR_PUSH_TOKEN || "").trim();
}

/**
 * Fail closed and SAY SO. An unset token is a misconfigured deploy, not an
 * empty inbox; returning an empty run list here would show an operator a clean
 * page for a broken connection.
 */
function missingToken(): { ok: false; status: number; error: string } {
  return {
    ok: false,
    status: 503,
    error:
      "CTRLA_JR_PUSH_TOKEN is not set on this server, so it cannot authenticate " +
      "to the agent API. Nothing was sent.",
  };
}

async function call<T>(
  path: string,
  init: { method: "GET" | "POST"; body?: unknown; timeoutMs: number },
): Promise<AgentResult<T>> {
  const secret = token();
  if (!secret) return missingToken();

  const url = `${baseUrl()}${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: init.method,
      headers: {
        Authorization: `Bearer ${secret}`,
        ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: AbortSignal.timeout(init.timeoutMs),
      cache: "no-store",
    });
  } catch (error) {
    // Deliberately does not interpolate the caught value: a fetch error can
    // quote the request, and the request carries the bearer header.
    const timedOut = error instanceof Error && error.name === "TimeoutError";
    return {
      ok: false,
      status: 504,
      error: timedOut
        ? `The agent did not answer within ${Math.round(init.timeoutMs / 1000)}s. ` +
          "The turn may still be running — reload the run to see where it got to."
        : "Could not reach the agent API.",
    };
  }

  const raw = await response.text();
  let parsed: unknown = null;
  let unparseable = false;
  if (raw) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      unparseable = true;
    }
  }
  // An UNSUCCESSFUL response is allowed to be unparseable — a platform 404 is
  // plain text — and it falls through to the error branch below, which reports
  // the real status. Only a SUCCESS that is not JSON is a protocol problem, and
  // the body is never echoed: it is upstream text of unknown provenance.
  if (unparseable && response.ok) {
    return {
      ok: false,
      status: 502,
      error: `The agent API answered ${response.status} with something that is not JSON.`,
    };
  }

  if (!response.ok) {
    const detail =
      typeof parsed === "object" && parsed !== null && typeof (parsed as { error?: unknown }).error === "string"
        ? (parsed as { error: string }).error
        : `The agent API answered ${response.status}.`;
    // 404 from these routes is what a WRONG OR MISSING TOKEN looks like: the
    // API answers 404 rather than 401 so an unauthenticated caller cannot
    // confirm the endpoint exists. Say that, rather than "no such run".
    const hint =
      response.status === 404
        ? `${detail} (These endpoints answer 404 both for an unknown run and for a rejected token.)`
        : detail;
    return { ok: false, status: response.status, error: hint };
  }

  return { ok: true, status: response.status, data: parsed as T };
}

export function createRun(message: string) {
  return call<RunPayload>("/api/runs", {
    method: "POST",
    body: { message },
    timeoutMs: TURN_TIMEOUT_MS,
  });
}

export function listRuns() {
  return call<{ runs: RunSummary[] }>("/api/runs", {
    method: "GET",
    timeoutMs: READ_TIMEOUT_MS,
  });
}

export function getRun(runId: string) {
  return call<RunPayload>(`/api/runs/${encodeURIComponent(runId)}`, {
    method: "GET",
    timeoutMs: READ_TIMEOUT_MS,
  });
}

export function postMessage(runId: string, message: string) {
  return call<RunPayload>(`/api/runs/${encodeURIComponent(runId)}/messages`, {
    method: "POST",
    body: { message },
    timeoutMs: TURN_TIMEOUT_MS,
  });
}

export function advanceRun(runId: string) {
  return call<RunPayload>(`/api/runs/${encodeURIComponent(runId)}/advance`, {
    method: "POST",
    timeoutMs: TURN_TIMEOUT_MS,
  });
}
