import Link from "next/link";

import { Conversation } from "@/components/conversation";
import { EventDrawer } from "@/components/event-drawer";
import { NoBundle } from "@/components/missing";
import { Badge, Panel, VerdictPill } from "@/components/primitives";
import { RunList } from "@/components/run-list";
import { checksOf, checkLabel, normalizeVerdict } from "@/lib/derive";
import { getEvidence, selectRun } from "@/lib/evidence";
import type { EvidenceBundle } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * The workspace. Conversation is the primary column; the run list drives it;
 * the raw stream sits in a drawer underneath.
 */
export default async function WorkspacePage({
  searchParams,
}: {
  searchParams: Promise<{ run?: string }>;
}) {
  const result = await getEvidence();
  if (!result.ok) return <NoBundle reason={result.reason} source={result.source} />;

  const bundle = result.bundle;
  const { run: requested } = await searchParams;
  const { run, matched } = selectRun(bundle, requested);

  return (
    <div className="pt-6">
      <Header bundle={bundle} />

      <div className="mt-5 grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,17rem)_minmax(0,1fr)]">
        <div className="lg:max-h-[calc(100vh-9rem)] lg:sticky lg:top-20">
          <RunList
            runs={bundle.runs}
            selectedId={run?.run_id ?? null}
            basePath="/evidence"
            unmatched={!matched}
          />
        </div>

        <div className="min-w-0">
          {run ? (
            <>
              <Conversation run={run} />
              <EventDrawer run={run} redaction={bundle.redaction} />
            </>
          ) : (
            <Panel className="p-6 text-sm text-[var(--on-surface-muted)]">
              The bundle loaded but contains no runs, so there is nothing to
              replay. No conclusion is drawn from that.
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * The summary strip. It leads with the checks, INCLUDING the inconclusive ones,
 * because a page that puts four green counters at the top and hides the
 * unexercised check further down is the laundering problem in miniature.
 */
function Header({ bundle }: { bundle: EvidenceBundle }) {
  const checks = checksOf(bundle);
  const integrity = bundle.log_integrity;
  const headline = bundle.headline;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-black tracking-tight text-[var(--on-surface)]">
            Workspace
          </h1>
          <p className="mt-0.5 max-w-[70ch] text-sm text-[var(--on-surface-variant)]">
            What the agent proposed, what a human approved, and what actually
            executed — replayed from the activity log the run wrote as it went.
          </p>
        </div>
        <Link
          href="/verdict"
          className="rounded-lg px-3 py-1.5 text-xs font-bold text-[var(--primary-bright)] hover:bg-[var(--surface-high)]"
        >
          Read the verdict and its qualifiers →
        </Link>
      </div>

      <Panel className="flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3">
        <span className="label-caps">Across {bundle.runs.length} run(s)</span>
        {checks.length === 0 ? (
          <Badge tone="unknown" dashed>
            no checks in this bundle — nothing is claimed
          </Badge>
        ) : (
          checks.map((c) => (
            <span key={c.id} className="flex items-center gap-1.5">
              <span className="text-[0.75rem] text-[var(--on-surface-variant)]">
                {checkLabel(c.id)}
              </span>
              <VerdictPill verdict={normalizeVerdict(c.verdict)} />
            </span>
          ))
        )}
        <span className="ml-auto flex items-center gap-1.5">
          <span className="text-[0.75rem] text-[var(--on-surface-variant)]">Log integrity</span>
          {integrity?.sound ? (
            <Badge tone="success">sound · {integrity.evidence}</Badge>
          ) : (
            <Badge tone="error">
              {integrity?.evidence ? `unsound · ${integrity.evidence}` : "not measured"}
            </Badge>
          )}
        </span>
      </Panel>

      {headline.status !== "claimed" ? (
        <Panel
          className="px-4 py-3"
          style={{
            background:
              headline.status === "refuted" ? "var(--error-surface)" : "var(--unknown-surface)",
          }}
        >
          <p className="text-[0.8125rem] leading-relaxed text-[var(--on-surface)]">
            <strong className="font-black">
              {headline.status === "refuted" ? "Refuted." : "Not claimed."}
            </strong>{" "}
            {headline.claim}
          </p>
        </Panel>
      ) : null}
    </div>
  );
}
