"use client";

import { useMemo, useState } from "react";

import { ArtifactFrame, type ArtifactFacts } from "./channel-frames";
import { Badge, Mono, Panel, PanelHeader, Withheld, cn } from "./primitives";
import {
  ABSENT,
  approvalsById,
  clock,
  count,
  durationBetween,
  eventHeadline,
  turnsOf,
} from "@/lib/derive";
import type { Approval, EvidenceEvent, Run } from "@/lib/types";

/**
 * The run, read as a conversation.
 *
 * The log is a flat event stream; the exporter stamps each record with the
 * model turn it belongs to (assign_turns), and this reads those turns back as
 * the primary column: what the model did, the calls it made as inline chips,
 * and — where the gate stopped the run — the approval card the operator
 * actually saw, inline at the point it stopped.
 */

interface Selection {
  key: string;
  facts: ArtifactFacts;
  title: string;
  subtitle: string;
}

export function Conversation({ run }: { run: Run }) {
  const turns = useMemo(() => turnsOf(run), [run]);
  const approvals = useMemo(() => approvalsById(run), [run]);
  const [selected, setSelected] = useState<Selection | null>(null);

  // Events that an approval card already accounts for must not also appear as
  // loose rows — the resolution and the execution belong to the card.
  const consumed = useMemo(() => {
    const set = new Set<string>();
    run.events.forEach((e, i) => {
      if (!e.approval_id) return;
      if (
        e.event === "approval_resolved" ||
        e.event === "approval_auto_denied" ||
        e.event === "tool_call"
      ) {
        set.add(key(e, i));
      }
    });
    return set;
  }, [run]);

  const current =
    selected ??
    defaultSelection(run) ??
    null;

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
      <div className="flex min-w-0 flex-col gap-3">
        <TaskCard run={run} />

        {turns.map((t) => (
          <section key={t.turn} className="min-w-0">
            <TurnHeader turn={t.turn} header={t.header} />
            <div className="mt-2 flex flex-col gap-2 border-l border-[var(--outline)] pl-3 sm:pl-4">
              {t.items.length === 0 ? (
                <p className="py-1 text-[0.75rem] text-[var(--on-surface-muted)]">
                  The model spoke and called nothing. What it said is not in the
                  log — only that the turn happened.
                </p>
              ) : null}
              {t.items.map((e, i) => {
                const k = key(e, indexOf(run, e, i));
                if (e.event === "approval_requested" && e.approval_id) {
                  const a = approvals.get(e.approval_id);
                  return a ? (
                    <ApprovalCard
                      key={k}
                      approval={a}
                      selected={current?.key === k}
                      onSelect={() => setSelected(selectionForApproval(a, k))}
                    />
                  ) : null;
                }
                if (consumed.has(k)) return null;
                if (e.event === "tool_call") {
                  return (
                    <ToolChip
                      key={k}
                      event={e}
                      selected={current?.key === k}
                      onSelect={() => setSelected(selectionForEvent(e, k))}
                    />
                  );
                }
                return <NoticeRow key={k} event={e} />;
              })}
            </div>
          </section>
        ))}

        <OutcomeTiles run={run} />
        <Composer />
      </div>

      <div className="min-w-0">
        <div className="xl:sticky xl:top-20">
          <Canvas selection={current} />
        </div>
      </div>
    </div>
  );
}

/* ── keys ───────────────────────────────────────────────────────────────── */

function key(e: EvidenceEvent, i: number): string {
  return `${i}:${e.event}:${e.ts ?? ""}`;
}

function indexOf(run: Run, e: EvidenceEvent, fallback: number): number {
  const i = run.events.indexOf(e);
  return i >= 0 ? i : fallback;
}

/* ── Task ───────────────────────────────────────────────────────────────── */

/**
 * What the run was asked to do.
 *
 * The task prompt is not in the activity log — the log keeps message content
 * out by design — so this states the standing job the agent is wired for and
 * says plainly that the specific instruction was not recorded, rather than
 * putting a confident sentence in the model's mouth.
 */
function TaskCard({ run }: { run: Run }) {
  const first = run.events[0];
  return (
    <Panel className="p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="primary">task</Badge>
        <Mono className="text-[var(--on-surface-variant)]">{run.run_id}</Mono>
        <span className="text-[0.6875rem] text-[var(--on-surface-muted)]">
          {clock(run.started_at)} → {clock(run.ended_at)} ·{" "}
          {durationBetween(run.started_at, run.ended_at)}
        </span>
      </div>
      <p className="mt-2.5 max-w-[70ch] text-sm leading-relaxed text-[var(--on-surface)]">
        Read the shop&rsquo;s real inbox over IMAP, classify what each customer is
        asking for, price it from the code-owned service menu, raise a Stripe
        invoice, reply, and escalate anything over the Slack threshold. Every
        mutating step stops for a human first.
      </p>
      <p className="mt-2 text-[0.6875rem] leading-relaxed text-[var(--on-surface-muted)]">
        That is the standing job, not a transcript. The exact instruction this
        run was given is <Withheld>not in the activity log</Withheld> — the log
        records tools, approval ids, payload hashes and character counts, never
        message text. Agent{" "}
        <Mono className="text-[var(--on-surface-variant)]">
          {first?.agent ?? ABSENT}
        </Mono>
        .
      </p>
    </Panel>
  );
}

/* ── Turn header ────────────────────────────────────────────────────────── */

function TurnHeader({ turn, header }: { turn: number; header: EvidenceEvent | null }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <p className="text-xs font-black tracking-tight text-[var(--on-surface)]">
        {turn === 0 ? "Before the model spoke" : `Turn ${turn}`}
      </p>
      {header ? (
        <p className="text-[0.6875rem] text-[var(--on-surface-muted)]">
          <Mono className="text-[var(--on-surface-variant)]">
            {header.model ?? ABSENT}
          </Mono>{" "}
          via {header.provider ?? ABSENT} · round {header.round ?? ABSENT} ·{" "}
          {clock(header.ts)}
        </p>
      ) : (
        <p className="text-[0.6875rem] text-[var(--on-surface-muted)]">
          setup, before the first model turn
        </p>
      )}
    </div>
  );
}

/* ── Tool chip ──────────────────────────────────────────────────────────── */

function ToolChip({
  event,
  selected,
  onSelect,
}: {
  event: EvidenceEvent;
  selected: boolean;
  onSelect: () => void;
}) {
  const failed = event.ok === false;
  const mutating = event.mutating === true;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex w-full min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 rounded-lg px-3 py-2 text-left transition-colors ghost-border",
        selected
          ? "bg-[var(--surface-high)] border-[var(--primary)]/50"
          : "bg-[var(--surface-container)] hover:bg-[var(--surface-high)]",
      )}
    >
      <Badge tone={failed ? "error" : mutating ? "primary" : "info"}>
        {failed ? "errored" : mutating ? "mutating" : "read"}
      </Badge>
      <Mono className="min-w-0 truncate font-semibold text-[var(--on-surface)]">
        {event.tool}
      </Mono>
      <span className="text-[0.625rem] text-[var(--on-surface-muted)]">
        {typeof event.result_chars === "number"
          ? `${count(event.result_chars)} chars back`
          : "no size recorded"}
      </span>
      {event.error_type ? (
        <span className="text-[0.625rem] text-[var(--error)]">{event.error_type}</span>
      ) : null}
      <span className="ml-auto text-[0.625rem] text-[var(--on-surface-muted)]">
        {clock(event.ts)}
      </span>
    </button>
  );
}

/* ── Approval card ──────────────────────────────────────────────────────── */

/**
 * Where the gate stopped the run, rendered inline at the point it stopped.
 *
 * It shows three separable facts that the log keeps separate: that the gate
 * OPENED, WHO closed it (a human saying no and the approval surface going away
 * are the same Decision and different evidence), and whether the call then
 * executed. Nothing here is operable — the buttons that decide live on the
 * local approval server, not on a published page.
 */
function ApprovalCard({
  approval,
  selected,
  onSelect,
}: {
  approval: Approval;
  selected: boolean;
  onSelect: () => void;
}) {
  const decided = approval.decision;
  const tone =
    decided === "approved" ? "success" : decided === "denied" ? "error" : "unknown";
  const byMachine = approval.decided_by === "machine";

  return (
    <div
      className={cn(
        "min-w-0 overflow-hidden rounded-xl transition-colors ghost-border",
        selected ? "border-[var(--primary)]/50" : "",
      )}
      style={{ background: "var(--surface-container)" }}
    >
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className="flex w-full flex-wrap items-center gap-2 border-b border-[var(--outline)] px-4 py-2.5 text-left hover:bg-[var(--surface-high)]"
      >
        <Badge tone="warning">gate stopped here</Badge>
        <Mono className="font-semibold text-[var(--on-surface)]">{approval.tool}</Mono>
        <Mono className="text-[var(--on-surface-muted)]">{approval.approval_id}</Mono>
        <span className="ml-auto text-[0.625rem] text-[var(--on-surface-muted)]">
          {clock(approval.requested_at)}
        </span>
      </button>

      <div className="px-4 py-3.5">
        <ArtifactFrame facts={factsOfApproval(approval)} />

        <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
          <Fact label="Decision">
            <Badge tone={tone} dashed={tone === "unknown"}>
              {decided}
            </Badge>{" "}
            <span className="text-[0.6875rem] text-[var(--on-surface-variant)]">
              {byMachine
                ? "by the machine — the approval surface stopped, nobody said no"
                : approval.decided_by === "human"
                  ? "by a human at the approval surface"
                  : "nobody decided it"}
            </span>
          </Fact>
          <Fact label="Waited">
            <Mono className="text-[var(--on-surface)]">
              {durationBetween(approval.requested_at, approval.decided_at)}
            </Mono>
          </Fact>
          <Fact label="Payload hash">
            <Mono className="break-all text-[var(--on-surface)]">
              {approval.payload_hash || ABSENT}
            </Mono>
            <p className="mt-0.5 text-[0.625rem] leading-relaxed text-[var(--on-surface-muted)]">
              First 16 hex of the hash of what was approved. The execution record
              cites the same value — that correlation is what &ldquo;the call that
              ran is the call you saw&rdquo; rests on.
            </p>
          </Fact>
          <Fact label="Then executed">
            {approval.execution_logged ? (
              <Badge tone={approval.executed ? "success" : "error"}>
                {approval.executed ? "yes, and it succeeded" : "attempted, and it failed"}
              </Badge>
            ) : (
              <Badge tone="unknown" dashed>
                no execution recorded
              </Badge>
            )}
          </Fact>
        </dl>

        {approval.machine_reason ? (
          <p className="mt-3 rounded-lg border border-dashed border-[var(--outline)] px-3 py-2 text-[0.6875rem] text-[var(--on-surface-variant)]">
            Machine denial reason:{" "}
            <Mono className="text-[var(--on-surface)]">{approval.machine_reason}</Mono>
          </p>
        ) : null}
      </div>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="label-caps">{label}</dt>
      <dd className="mt-0.5 text-[0.8125rem] text-[var(--on-surface)]">{children}</dd>
    </div>
  );
}

/* ── Notice rows ────────────────────────────────────────────────────────── */

function NoticeRow({ event }: { event: EvidenceEvent }) {
  const tone =
    event.event === "payload_mismatch" || event.event === "provider_transport_failure"
      ? "error"
      : event.event === "tool_refused" || event.event === "round_limit_reached"
        ? "warning"
        : "neutral";
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2 rounded-lg border border-dashed border-[var(--outline)] px-3 py-1.5">
      <Badge tone={tone}>{event.event}</Badge>
      <span className="min-w-0 text-[0.75rem] text-[var(--on-surface-variant)]">
        {eventHeadline(event)}
      </span>
      {event.reason ? (
        <Mono className="text-[var(--on-surface-muted)]">{event.reason}</Mono>
      ) : null}
      <span className="ml-auto text-[0.625rem] text-[var(--on-surface-muted)]">
        {clock(event.ts)}
      </span>
    </div>
  );
}

/* ── Outcome ────────────────────────────────────────────────────────────── */

function OutcomeTiles({ run }: { run: Run }) {
  const o = run.outcome;
  if (!o) return null;
  const g = o.gate;
  const tiles: { label: string; value: string; sub: string }[] = [
    {
      label: "Model turns",
      value: count(o.turns),
      sub: `${count(o.tool_calls)} tool call(s)`,
    },
    {
      label: "Reads",
      value: count(o.reads),
      sub: "never stopped for approval — nothing to undo",
    },
    {
      label: "Mutations executed",
      value: count(o.mutations_executed),
      sub:
        o.mutations_failed > 0
          ? `${count(o.mutations_failed)} more attempted and failed`
          : "none attempted and failed",
    },
    {
      label: "Stopped at the gate",
      value: count(g.requested),
      sub: `${count(g.approved)} approved · ${count(g.denied_by_human)} denied by a human · ${count(
        g.denied_by_machine,
      )} auto-denied · ${count(g.undecided)} undecided`,
    },
  ];

  const anomalies =
    o.payload_mismatches + o.refusals + o.transport_failures + o.mutations_failed;

  return (
    <div className="mt-2 flex flex-col gap-3">
      <p className="label-caps">Outcome</p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {tiles.map((t) => (
          <Panel key={t.label} className="flex min-w-0 flex-col gap-1 p-4">
            <p className="label-caps truncate">{t.label}</p>
            <p className="text-2xl leading-none font-black tabular-nums">{t.value}</p>
            <p className="text-[0.6875rem] leading-relaxed text-[var(--on-surface-muted)]">
              {t.sub}
            </p>
          </Panel>
        ))}
      </div>
      <Panel className="flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3">
        <p className="label-caps">Anomalies</p>
        <Counter label="payload mismatches" n={o.payload_mismatches} bad />
        <Counter label="tool refusals" n={o.refusals} />
        <Counter label="transport failures" n={o.transport_failures} />
        <span className="ml-auto text-[0.6875rem] text-[var(--on-surface-muted)]">
          {anomalies === 0
            ? "None in this run. That is a count from this log, not a claim about the agent."
            : `${count(anomalies)} recorded in this run.`}
        </span>
      </Panel>
    </div>
  );
}

function Counter({ label, n, bad }: { label: string; n: number; bad?: boolean }) {
  return (
    <span className="text-[0.75rem] text-[var(--on-surface-variant)]">
      <span
        className="font-black tabular-nums"
        style={{ color: n > 0 && bad ? "var(--error)" : "var(--on-surface)" }}
      >
        {count(n)}
      </span>{" "}
      {label}
    </span>
  );
}

/* ── Composer (inert) ───────────────────────────────────────────────────── */

/**
 * The conversation shape wants a composer at the bottom. This page is evidence,
 * not a console: there is nothing here that can send, approve or deny. So the
 * composer is rendered disabled and says why, rather than being omitted and
 * leaving the reader to wonder whether it was ever operable.
 */
function Composer() {
  return (
    <div className="mt-1 flex items-center gap-3 rounded-xl border border-dashed border-[var(--outline)] bg-[var(--surface-container)] px-4 py-3">
      <input
        disabled
        aria-label="Message the agent (disabled)"
        placeholder="Replay only — this surface cannot send, approve or deny."
        className="min-w-0 flex-1 bg-transparent text-[0.8125rem] text-[var(--on-surface-muted)] placeholder:text-[var(--on-surface-muted)] disabled:cursor-not-allowed"
      />
      <Badge tone="neutral">disabled</Badge>
    </div>
  );
}

/* ── Canvas ─────────────────────────────────────────────────────────────── */

/**
 * The canvas. ctrl-a's canvas renders whatever the thread item stands for; this
 * one renders the typed artifact for the selected call — built by code from the
 * call's own fields, never from anything the model authored.
 */
function Canvas({ selection }: { selection: Selection | null }) {
  return (
    <Panel className="min-w-0 overflow-hidden">
      <PanelHeader
        title="Canvas"
        meta={
          <span className="text-[0.625rem] text-[var(--on-surface-muted)]">
            rendered by code, not by the model
          </span>
        }
      />
      <div className="px-4 py-4">
        {selection ? (
          <>
            <p className="text-xs font-semibold text-[var(--on-surface)]">
              {selection.title}
            </p>
            <p className="mt-0.5 mb-3 text-[0.6875rem] text-[var(--on-surface-muted)]">
              {selection.subtitle}
            </p>
            <ArtifactFrame facts={selection.facts} />
          </>
        ) : (
          <p className="text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
            Select a tool call or an approval in the conversation to see the card
            it stands for.
          </p>
        )}
      </div>
    </Panel>
  );
}

/* ── Selection helpers ──────────────────────────────────────────────────── */

function factsOfApproval(a: Approval): ArtifactFacts {
  return {
    tool: a.tool,
    renderedChars: a.rendered_chars ?? null,
    payloadHash: a.payload_hash,
    mutating: true,
  };
}

function factsOfEvent(e: EvidenceEvent): ArtifactFacts {
  return {
    tool: e.tool ?? "unknown",
    renderedChars: e.rendered_chars ?? null,
    payloadHash: e.payload_hash ?? null,
    resultChars: e.result_chars ?? null,
    ok: e.ok,
    mutating: e.mutating,
    errorType: e.error_type,
  };
}

function selectionForApproval(a: Approval, k: string): Selection {
  return {
    key: k,
    facts: factsOfApproval(a),
    title: `${a.integration_label} · ${a.tool}`,
    subtitle: `The card the approver saw — approval ${a.approval_id}`,
  };
}

function selectionForEvent(e: EvidenceEvent, k: string): Selection {
  return {
    key: k,
    facts: factsOfEvent(e),
    title: `${e.integration_label ?? "Tool"} · ${e.tool ?? "call"}`,
    subtitle:
      e.mutating === true
        ? "A mutating call — it passed the gate first."
        : "A read-only call — it never reaches the gate.",
  };
}

/** Open the canvas on the run's last approval, so it is never blank on load. */
function defaultSelection(run: Run): Selection | null {
  const a = run.approvals[run.approvals.length - 1];
  if (!a) return null;
  return selectionForApproval(a, `default:${a.approval_id}`);
}
