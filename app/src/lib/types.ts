/**
 * The shape of the evidence bundle, mirrored from src/ctrl_a_jr/export.py.
 *
 * Every optional field here is optional because the EXPORTER may legitimately
 * omit it — the allowlist in `EVENT_FIELDS` only copies a key across when the
 * log record carried it. Nothing in the UI may substitute a value for a missing
 * one; it renders the absence instead.
 *
 * `artifact_html` is typed only so the read boundary can name the field it
 * deletes. The exporter ships a pre-rendered HTML card per call; lib/evidence
 * strips it on the way in, and the cards are rebuilt in React from the
 * structured fields instead. Nothing downstream ever holds the string.
 */

export type CheckVerdict = "pass" | "fail" | "inconclusive";

export interface Check {
  id: string;
  verdict: CheckVerdict;
  evidence: string;
  severity: string;
}

export interface LogIntegrity {
  sound: boolean;
  evidence: string;
  malformed_lines?: number | null;
  write_failures?: number | null;
}

export interface Verdict {
  schema?: string;
  run_id?: string;
  commit?: string;
  model?: string;
  provider?: string;
  labelled_from?: string;
  exit?: boolean;
  runs?: number;
  aggregate?: { pass?: number; fail?: number; inconclusive?: number };
  checks: Check[];
  log_integrity?: LogIntegrity;
  per_run?: { run_id: string; checks: Check[] }[];
}

export type HeadlineStatus = "claimed" | "inconclusive" | "refuted" | "unavailable";

export interface Headline {
  status: HeadlineStatus;
  claim: string;
  qualifiers: string[];
}

/** One exported log record. Extra keys are whatever the allowlist let through. */
export interface EvidenceEvent {
  event: string;
  run_id?: string;
  ts?: string;
  agent?: string;
  turn?: number;

  provider?: string;
  model?: string;
  round?: number;

  tool?: string;
  ok?: boolean;
  mutating?: boolean;
  approval_id?: string | null;
  result_chars?: number;
  error_type?: string;

  reason?: string;
  payload_hash?: string;
  rendered_chars?: number;
  decision?: string;

  block_type?: string;
  max_rounds?: number;

  integration?: string;
  integration_label?: string;

  unknown_event?: boolean;
  /** Present but never rendered. See the note at the top of this file. */
  artifact_html?: string;
}

export interface Approval {
  approval_id: string;
  tool: string;
  payload_hash: string;
  rendered_chars?: number | null;
  requested_at?: string;
  decided_at?: string;
  decision: "approved" | "denied" | "pending" | string;
  decided_by: "human" | "machine" | "nobody" | string;
  executed: boolean;
  execution_logged?: boolean;
  machine_reason?: string;
  integration: string;
  integration_label: string;
  artifact_html?: string;
}

export interface IntegrationRow {
  key: string;
  label: string;
  events: number;
  approvals: number;
}

export interface Outcome {
  turns: number;
  tool_calls: number;
  reads: number;
  mutations_executed: number;
  mutations_failed: number;
  refusals: number;
  payload_mismatches: number;
  transport_failures: number;
  gate: {
    requested: number;
    approved: number;
    denied_by_human: number;
    denied_by_machine: number;
    undecided: number;
  };
  integrations: IntegrationRow[];
}

export interface Run {
  run_id: string;
  started_at?: string | null;
  ended_at?: string | null;
  events: EvidenceEvent[];
  approvals: Approval[];
  outcome: Outcome;
  checks: Check[];
}

export interface Redaction {
  policy: string;
  note: string;
  exported_fields: Record<string, string[]>;
  derived_fields: string[];
}

export interface EvidenceBundle {
  schema: string;
  generated_at: string;
  commit: string;
  redaction: Redaction;
  log_integrity: LogIntegrity;
  verdict: Verdict | null;
  headline: Headline;
  integrations: IntegrationRow[];
  runs: Run[];
}

/**
 * What the UI gets. A bundle, or an explicit failure — never a fabricated
 * empty bundle, because an empty bundle renders as "a run with nothing wrong
 * in it" and that is exactly the laundering this page must not do.
 */
export type EvidenceResult =
  | { ok: true; bundle: EvidenceBundle; source: string }
  | { ok: false; reason: string; source: string };
