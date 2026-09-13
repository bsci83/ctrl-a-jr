"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { ArtifactFrame } from "./channel-frames";
import { MenuPanel } from "./menu-panel";
import { Badge, Mono, Panel, PanelHeader, cn } from "./primitives";
import {
  ABSENT,
  approvalsForIntegration,
  clock,
  count,
  eventsForIntegration,
} from "@/lib/derive";
import type { Approval, EvidenceEvent, IntegrationRow, Run } from "@/lib/types";

/**
 * The tab strip is built from `integrations` — which the caller derived from
 * the data. This component must never contain a literal list of integration
 * names, because that is exactly how a fourth integration gets silently
 * dropped. The only per-key knowledge here is cosmetic (an accent colour), and
 * an unknown key falls through to a neutral default rather than disappearing.
 */
export function ChannelTabs({
  run,
  integrations,
  activeKey,
}: {
  run: Run;
  integrations: IntegrationRow[];
  activeKey?: string;
}) {
  // Only integrations this run actually touched get a tab; the bundle-wide list
  // orders them, so the tabs stay stable as you move between runs.
  const present = useMemo(() => {
    const inRun = new Set(run.events.map((e) => e.integration).filter(Boolean) as string[]);
    const known = integrations.filter((i) => inRun.has(i.key));
    // Anything in the run but missing from the bundle-level list still gets a tab.
    const extra = [...inRun]
      .filter((k) => !known.some((i) => i.key === k))
      .map<IntegrationRow>((k) => ({
        key: k,
        label:
          run.events.find((e) => e.integration === k)?.integration_label ?? k,
        events: 0,
        approvals: 0,
      }));
    return [...known, ...extra];
  }, [run, integrations]);

  const initial = present.find((i) => i.key === activeKey)?.key ?? present[0]?.key ?? null;
  const [active, setActive] = useState<string | null>(initial);
  const current = present.find((i) => i.key === active) ?? present[0] ?? null;

  if (present.length === 0) {
    return (
      <Panel className="p-6 text-sm text-[var(--on-surface-muted)]">
        This run touched no integration-namespaced tools.
      </Panel>
    );
  }

  return (
    <div className="min-w-0">
      <div className="scroll-x -mx-1 flex items-center gap-1 border-b border-[var(--outline)] px-1">
        {present.map((i) => {
          const isActive = current?.key === i.key;
          const stats = statsFor(run, i.key);
          return (
            <button
              key={i.key}
              type="button"
              onClick={() => setActive(i.key)}
              aria-selected={isActive}
              role="tab"
              className={cn(
                "relative flex shrink-0 items-center gap-2 px-3.5 py-2.5 text-xs font-bold tracking-wide transition-colors",
                isActive
                  ? "text-[var(--on-surface)]"
                  : "text-[var(--on-surface-variant)] hover:text-[var(--on-surface)]",
              )}
            >
              <span
                aria-hidden
                className="size-2 shrink-0 rounded-full"
                style={{ background: accentFor(i.key) }}
              />
              {i.label}
              <span className="text-[0.625rem] font-medium text-[var(--on-surface-muted)]">
                {stats.events}
              </span>
              {isActive ? (
                <span
                  aria-hidden
                  className="absolute right-0 -bottom-px left-0 h-[2px]"
                  style={{ background: accentFor(i.key) }}
                />
              ) : null}
            </button>
          );
        })}
      </div>

      {current ? <ChannelPanel run={run} integration={current} /> : null}
    </div>
  );
}

function statsFor(run: Run, key: string) {
  const events = eventsForIntegration(run, key);
  return {
    events: events.length,
    approvals: approvalsForIntegration(run, key).length,
  };
}

/* ── One integration's panel ────────────────────────────────────────────── */

function ChannelPanel({ run, integration }: { run: Run; integration: IntegrationRow }) {
  const events = eventsForIntegration(run, integration.key);
  const approvals = approvalsForIntegration(run, integration.key);
  const calls = events.filter((e) => e.event === "tool_call");
  const reads = calls.filter((e) => e.mutating !== true);
  const mutations = calls.filter((e) => e.mutating === true);

  return (
    <div className="mt-4 flex min-w-0 flex-col gap-4">
      <Panel className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <span className="label-caps">{integration.label} in this run</span>
        <Fig n={events.length} label="events" />
        <Fig n={reads.length} label="reads" />
        <Fig n={mutations.length} label="mutations" />
        <Fig n={approvals.length} label="stopped at the gate" />
        <Link
          href={`/?run=${encodeURIComponent(run.run_id)}`}
          className="ml-auto text-[0.6875rem] font-bold text-[var(--primary-bright)] hover:underline"
        >
          see it in the conversation →
        </Link>
      </Panel>

      {approvals.length > 0 ? (
        <section className="flex min-w-0 flex-col gap-3">
          <p className="label-caps">
            What this channel was asked to send — the cards the approver saw
          </p>
          {approvals.map((a) => (
            <ApprovalArtifact key={a.approval_id} approval={a} />
          ))}
        </section>
      ) : (
        <Panel className="px-4 py-3 text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
          Nothing on this channel passed through the approval gate in this run.
          Either it was only read from, or it was not used to send anything.
        </Panel>
      )}

      {integration.key === "quote" ? <MenuPanel /> : null}

      <section className="min-w-0">
        <Panel className="overflow-hidden">
          <PanelHeader
            title={`${integration.label} calls`}
            meta={
              <span className="text-[0.625rem] text-[var(--on-surface-muted)]">
                {count(calls.length)} call(s), in log order
              </span>
            }
          />
          {calls.length === 0 ? (
            <p className="px-4 py-4 text-[0.75rem] text-[var(--on-surface-muted)]">
              No tool calls on this channel — only gate or failure records.
            </p>
          ) : (
            <ul className="flex flex-col">
              {calls.map((e, i) => (
                <CallRow key={`${i}-${e.ts ?? ""}`} event={e} />
              ))}
            </ul>
          )}
        </Panel>
      </section>
    </div>
  );
}

function ApprovalArtifact({ approval }: { approval: Approval }) {
  return (
    <div className="min-w-0">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Mono className="font-semibold text-[var(--on-surface)]">{approval.tool}</Mono>
        <Mono className="text-[var(--on-surface-muted)]">{approval.approval_id}</Mono>
        <Badge
          tone={
            approval.decision === "approved"
              ? "success"
              : approval.decision === "denied"
                ? "error"
                : "unknown"
          }
          dashed={approval.decision !== "approved" && approval.decision !== "denied"}
        >
          {approval.decision}
          {approval.decided_by === "machine" ? " (by the machine)" : ""}
        </Badge>
        {approval.execution_logged ? (
          <Badge tone={approval.executed ? "success" : "error"}>
            {approval.executed ? "executed" : "execution failed"}
          </Badge>
        ) : (
          <Badge tone="unknown" dashed>
            never executed
          </Badge>
        )}
      </div>
      <ArtifactFrame
        facts={{
          tool: approval.tool,
          renderedChars: approval.rendered_chars ?? null,
          payloadHash: approval.payload_hash,
          mutating: true,
        }}
      />
    </div>
  );
}

function CallRow({ event }: { event: EvidenceEvent }) {
  const failed = event.ok === false;
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[var(--outline-variant)] px-4 py-2 last:border-b-0">
      <Badge tone={failed ? "error" : event.mutating ? "primary" : "info"}>
        {failed ? "errored" : event.mutating ? "mutating" : "read"}
      </Badge>
      <Mono className="min-w-0 truncate text-[var(--on-surface)]">{event.tool}</Mono>
      <span className="text-[0.625rem] text-[var(--on-surface-muted)]">
        turn {event.turn ?? ABSENT} ·{" "}
        {typeof event.result_chars === "number"
          ? `${count(event.result_chars)} chars back`
          : "size not recorded"}
      </span>
      {event.error_type ? (
        <Mono className="text-[var(--error)]">{event.error_type}</Mono>
      ) : null}
      <span className="ml-auto text-[0.625rem] text-[var(--on-surface-muted)]">
        {clock(event.ts)}
      </span>
    </li>
  );
}

function Fig({ n, label }: { n: number; label: string }) {
  return (
    <span className="text-[0.75rem] text-[var(--on-surface-variant)]">
      <span className="font-black tabular-nums text-[var(--on-surface)]">{count(n)}</span>{" "}
      {label}
    </span>
  );
}

/** Cosmetic only. An unlisted integration still gets a tab, in neutral ink. */
function accentFor(key: string): string {
  switch (key) {
    case "gmail":
      return "#e05a57";
    case "slack":
      return "#9b59b6";
    case "stripe":
      return "#7a73ff";
    case "write":
      return "#8b95a3";
    case "quote":
      return "#14b8a6";
    default:
      return "var(--on-surface-muted)";
  }
}
