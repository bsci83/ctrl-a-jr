"use client";

import Link from "next/link";

import { clock, durationBetween, normalizeVerdict, shortRunId, stamp } from "@/lib/derive";
import type { Run } from "@/lib/types";
import { Badge, Mono, Panel, cn } from "./primitives";

/**
 * The run list. Selecting a run drives every view, so the selection is a URL
 * parameter rather than component state — the Channels and Verdict tabs read
 * the same `?run=` and a pasted link lands on the same run.
 */
export function RunList({
  runs,
  selectedId,
  basePath,
  unmatched,
}: {
  runs: Run[];
  selectedId: string | null;
  basePath: string;
  /** True when the URL named a run id this bundle does not contain. */
  unmatched?: boolean;
}) {
  return (
    <Panel className="flex min-h-0 flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-2 border-b border-[var(--outline)] px-4 py-2.5">
        <p className="label-caps">Runs</p>
        <Mono className="text-[var(--on-surface-muted)]">{runs.length}</Mono>
      </div>

      {unmatched ? (
        <p className="border-b border-[var(--outline)] bg-[var(--warning-surface)] px-4 py-2 text-[0.6875rem] leading-relaxed text-[var(--warning)]">
          The link named a run id that is not in this bundle. Showing the most
          recent run instead — it is not the run the link asked for.
        </p>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {runs.length === 0 ? (
          <p className="px-4 py-6 text-xs text-[var(--on-surface-muted)]">
            This bundle contains no runs.
          </p>
        ) : (
          <ul className="flex flex-col">
            {runs.map((run) => (
              <RunRow
                key={run.run_id}
                run={run}
                href={`${basePath}?run=${encodeURIComponent(run.run_id)}`}
                active={run.run_id === selectedId}
              />
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

function RunRow({ run, href, active }: { run: Run; href: string; active: boolean }) {
  const gate = run.outcome?.gate;
  const mutations = run.outcome?.mutations_executed ?? 0;
  const inconclusive = run.checks.filter(
    (c) => normalizeVerdict(c.verdict) === "inconclusive",
  ).length;
  const failed = run.checks.filter((c) => normalizeVerdict(c.verdict) === "fail").length;

  return (
    <li>
      <Link
        href={href}
        aria-current={active ? "true" : undefined}
        className={cn(
          "relative block border-b border-[var(--outline-variant)] px-4 py-3 transition-colors",
          active ? "bg-[var(--surface-high)]" : "hover:bg-[var(--surface-high)]/50",
        )}
      >
        {active ? (
          <span
            aria-hidden
            className="absolute top-0 bottom-0 left-0 w-[2px]"
            style={{ background: "var(--primary)" }}
          />
        ) : null}
        <div className="flex items-center justify-between gap-2">
          <Mono className="truncate font-semibold text-[var(--on-surface)]">
            {shortRunId(run.run_id)}
          </Mono>
          <span className="shrink-0 text-[0.625rem] text-[var(--on-surface-muted)]">
            {clock(run.started_at)}
          </span>
        </div>
        <p className="mt-1 truncate text-[0.6875rem] text-[var(--on-surface-muted)]">
          {stamp(run.started_at)} · {durationBetween(run.started_at, run.ended_at)}
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <Badge tone={mutations > 0 ? "primary" : "neutral"}>
            {mutations} mutation{mutations === 1 ? "" : "s"}
          </Badge>
          <Badge tone={gate && gate.requested > 0 ? "info" : "neutral"}>
            {gate?.requested ?? 0} at the gate
          </Badge>
          {failed > 0 ? <Badge tone="error">{failed} failed</Badge> : null}
          {inconclusive > 0 ? (
            <Badge tone="unknown" dashed title="Checks this run did not exercise.">
              {inconclusive} inconclusive
            </Badge>
          ) : null}
        </div>
      </Link>
    </li>
  );
}
