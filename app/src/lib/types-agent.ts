/**
 * The wire shapes of the live agent API (`api/_lib/runs.py`).
 *
 * Types only, in their own module, so a CLIENT component can describe what it
 * renders without importing `lib/agent.ts` — which holds the bearer token and
 * is structurally server-only. A type-only import is erased at build time, but
 * "the types live somewhere the client can safely reach" is a property worth
 * making structural rather than remembering.
 *
 * Every field beyond `run_id`/`status`/`conversation` is optional because the
 * API may legitimately omit it. Nothing in the UI may substitute a value for a
 * missing one — it renders the absence.
 */

export type RunStatus = "running" | "awaiting_approval" | "done" | "failed";

/**
 * One turn as `runs.conversation()` emits it. `kind` is widened to `string` for
 * the unrecognised case: a kind added to the API later must render as
 * "unrecognised", not crash and not silently disappear.
 */
export interface AgentTurn {
  kind: "human" | "assistant" | "tool_call" | "approval" | (string & {});
  text?: string;
  /* tool_call */
  tool?: string;
  id?: string;
  status?: "pending" | "ok" | "failed";
  output?: string;
  /* approval */
  approval_id?: string;
  rendered?: string;
  payload_hash?: string;
  decision?: string;
  approve_url?: string;
}

export interface PendingApproval {
  kind: "approval";
  approval_id: string;
  tool: string;
  rendered?: string;
  payload_hash?: string;
  decision?: string;
  approve_url?: string;
}

export interface RunPayload {
  run_id: string;
  status: RunStatus | (string & {});
  conversation: AgentTurn[];
  pending_approval: PendingApproval | null;
  text?: string;
  rounds?: number;
  hit_limit?: boolean;
  detail?: string;
  created_at?: string;
  updated_at?: string;
  note?: string;
}

export interface RunSummary {
  run_id: string;
  status: string;
  first_message: string;
  created_at: string;
  updated_at: string;
}
