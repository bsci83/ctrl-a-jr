import Link from "next/link";

import { NoBundle } from "@/components/missing";
import { Badge, Mono, Panel, PanelHeader, VerdictPill, cn } from "@/components/primitives";
import {
  CHECK_MEANING,
  checkLabel,
  checksOf,
  count,
  normalizeVerdict,
  shortRunId,
  stamp,
} from "@/lib/derive";
import { getEvidence } from "@/lib/evidence";
import type { CheckVerdict, EvidenceBundle, Headline } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function VerdictPage() {
  const result = await getEvidence();
  if (!result.ok) return <NoBundle reason={result.reason} source={result.source} />;

  const bundle = result.bundle;
  const checks = checksOf(bundle);
  const verdict = bundle.verdict;
  const integrity = bundle.log_integrity;

  return (
    <div className="flex max-w-[1100px] flex-col gap-5 pt-6">
      <div>
        <h1 className="text-2xl font-black tracking-tight text-[var(--on-surface)]">Verdict</h1>
        <p className="mt-0.5 max-w-[75ch] text-sm text-[var(--on-surface-variant)]">
          Four deterministic checks scored against the activity log, the log&rsquo;s
          own integrity, and the one claim this evidence supports — carried with
          the three qualifiers that make it true.
        </p>
      </div>

      <HeadlineCard headline={bundle.headline} verdict={bundle.verdict} />

      {/* ── Checks ──────────────────────────────────────────────────────── */}
      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <p className="label-caps">The checks</p>
          {verdict?.aggregate ? (
            <span className="flex flex-wrap items-center gap-1.5">
              <Badge tone="success">{count(verdict.aggregate.pass)} pass</Badge>
              <Badge tone={verdict.aggregate.fail ? "error" : "neutral"}>
                {count(verdict.aggregate.fail)} fail
              </Badge>
              <Badge tone="unknown" dashed>
                {count(verdict.aggregate.inconclusive)} inconclusive
              </Badge>
            </span>
          ) : null}
        </div>

        {checks.length === 0 ? (
          <Panel
            className="border-[var(--error)]/45 p-5"
            style={{ background: "var(--error-surface)" }}
          >
            <p className="text-sm text-[var(--on-surface)]">
              This bundle carries no checks. Nothing about the agent&rsquo;s behaviour
              is established here — an absent verdict is not a passing one.
            </p>
          </Panel>
        ) : (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {checks.map((c) => (
              <CheckCard key={c.id} {...c} />
            ))}
          </div>
        )}
      </section>

      {/* ── Log integrity ───────────────────────────────────────────────── */}
      <section>
        <Panel
          className={cn(
            "px-4 py-4",
            integrity?.sound ? "" : "border-[var(--error)]/45",
          )}
          style={integrity?.sound ? undefined : { background: "var(--error-surface)" }}
        >
          <div className="flex flex-wrap items-center gap-3">
            <p className="label-caps">Log integrity</p>
            {integrity?.sound ? (
              <Badge tone="success">sound</Badge>
            ) : (
              <Badge tone="error">not sound</Badge>
            )}
            <Mono className="text-[var(--on-surface-variant)]">
              {integrity?.evidence || "not measured"}
            </Mono>
          </div>
          <p className="mt-2 max-w-[80ch] text-[0.8125rem] leading-relaxed text-[var(--on-surface-variant)]">
            The checks above only describe the events that reached the log. If
            the stream lost records, they describe a subset nobody can bound —
            which is why a lost event does not lower a score here, it withdraws
            the headline claim entirely.
          </p>
          <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-[0.6875rem] text-[var(--on-surface-muted)]">
            <span>
              malformed lines{" "}
              <Mono className="text-[var(--on-surface-variant)]">
                {count(integrity?.malformed_lines)}
              </Mono>
            </span>
            <span>
              write failures{" "}
              <Mono className="text-[var(--on-surface-variant)]">
                {count(integrity?.write_failures)}
              </Mono>
            </span>
          </div>
        </Panel>
      </section>

      {/* ── Per-run matrix ──────────────────────────────────────────────── */}
      <PerRunMatrix bundle={bundle} />

      {/* ── Provenance ──────────────────────────────────────────────────── */}
      <Panel className="px-4 py-4">
        <p className="label-caps">Provenance</p>
        <dl className="mt-2 grid grid-cols-1 gap-x-8 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
          <Prov label="Bundle schema" value={bundle.schema} />
          <Prov label="Commit" value={bundle.commit} />
          <Prov label="Exported" value={stamp(bundle.generated_at)} />
          <Prov label="Model" value={verdict?.model} />
          <Prov label="Provider" value={verdict?.provider} />
          <Prov
            label="Model label taken from"
            value={verdict?.labelled_from}
            note="The verdict labels itself from the run's own model_turn events, never from the caller's arguments."
          />
        </dl>
      </Panel>
    </div>
  );
}

/* ── Headline ───────────────────────────────────────────────────────────── */

const HEADLINE_STYLE: Record<
  Headline["status"],
  { word: string; ink: string; surface: string; note: string }
> = {
  claimed: {
    word: "Claimed",
    ink: "var(--success)",
    surface: "var(--success-surface)",
    note: "The claim holds on this evidence — read with all three qualifiers below, which are part of it, not footnotes to it.",
  },
  inconclusive: {
    word: "Not claimed",
    ink: "var(--unknown)",
    surface: "var(--unknown-surface)",
    note: "This is not a weaker pass. The evidence did not exercise what the claim is about, so no claim is made either way.",
  },
  refuted: {
    word: "Refuted",
    ink: "var(--error)",
    surface: "var(--error-surface)",
    note: "A check that was exercised did not hold. The claim does not survive this evidence.",
  },
  unavailable: {
    word: "No verdict",
    ink: "var(--error)",
    surface: "var(--error-surface)",
    note: "No verdict was produced, so nothing is claimed. A missing verdict is never a passing one.",
  },
};

function HeadlineCard({
  headline,
  verdict,
}: {
  headline: Headline;
  verdict: EvidenceBundle["verdict"];
}) {
  const style = HEADLINE_STYLE[headline.status] ?? HEADLINE_STYLE.unavailable;
  return (
    <Panel className="overflow-hidden" style={{ background: style.surface }}>
      <div className="flex flex-wrap items-center gap-3 border-b border-[var(--outline)] px-4 py-2.5">
        <p className="label-caps">Headline claim</p>
        <span
          className="rounded-full border px-2.5 py-0.5 text-[0.6875rem] font-black tracking-wide uppercase"
          style={{ color: style.ink, borderColor: style.ink }}
        >
          {style.word}
        </span>
        {verdict?.runs !== undefined ? (
          <Mono className="ml-auto text-[var(--on-surface-muted)]">
            {count(verdict.runs)} run(s)
          </Mono>
        ) : null}
      </div>

      <div className="px-4 py-4">
        <p className="max-w-[80ch] text-[0.9375rem] leading-relaxed font-semibold text-[var(--on-surface)]">
          {headline.claim}
        </p>
        <p className="mt-2 max-w-[80ch] text-[0.75rem] leading-relaxed text-[var(--on-surface-variant)]">
          {style.note}
        </p>

        <div className="mt-4">
          <p className="label-caps">
            The qualifiers — the claim does not travel without them
          </p>
          <ol className="mt-2 flex flex-col gap-2.5">
            {headline.qualifiers.length === 0 ? (
              <li className="text-[0.8125rem] text-[var(--error)]">
                The bundle carried no qualifiers. The unqualified claim was a
                finding against this project; without them, treat the sentence
                above as unsupported.
              </li>
            ) : (
              headline.qualifiers.map((q, i) => (
                <li
                  key={i}
                  className="flex gap-3 rounded-lg border border-[var(--outline)] bg-[var(--surface-container)] px-3 py-2.5"
                >
                  <span className="shrink-0 text-[0.6875rem] font-black text-[var(--on-surface-muted)]">
                    {i + 1}
                  </span>
                  <span className="max-w-[80ch] text-[0.8125rem] leading-relaxed text-[var(--on-surface-variant)]">
                    {q}
                  </span>
                </li>
              ))
            )}
          </ol>
        </div>
      </div>
    </Panel>
  );
}

/* ── Check card ─────────────────────────────────────────────────────────── */

function CheckCard({
  id,
  verdict,
  evidence,
  severity,
}: {
  id: string;
  verdict: CheckVerdict;
  evidence: string;
  severity: string;
}) {
  const v = normalizeVerdict(verdict);
  // Three states, three treatments. `inconclusive` gets a dashed edge and its
  // own ink — never a paler green, never the pass surface at lower opacity.
  const ink =
    v === "pass" ? "var(--success)" : v === "fail" ? "var(--error)" : "var(--unknown)";

  return (
    <div
      className="flex min-w-0 flex-col gap-2 rounded-xl bg-[var(--surface-container)] px-4 py-3.5"
      style={{
        border: `1px ${v === "inconclusive" ? "dashed" : "solid"} color-mix(in oklab, ${ink} 45%, transparent)`,
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-sm font-black tracking-tight text-[var(--on-surface)]">
          {checkLabel(id)}
        </p>
        <VerdictPill verdict={v} />
        <Badge tone={severity === "critical" ? "warning" : "neutral"}>{severity}</Badge>
      </div>

      <p className="max-w-[60ch] text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
        {CHECK_MEANING[id] ?? "No description is registered for this check id."}
      </p>

      <p
        className="rounded-lg px-3 py-2 text-[0.75rem] leading-relaxed"
        style={{
          color: "var(--on-surface-variant)",
          background: "var(--surface-high)",
        }}
      >
        <span className="label-caps mr-2">evidence</span>
        <Mono className="text-[var(--on-surface)]">{evidence || "not recorded"}</Mono>
      </p>

      {v === "inconclusive" ? (
        <p className="text-[0.6875rem] leading-relaxed" style={{ color: ink }}>
          Nothing in this log exercised this check. That is not a pass and is not
          a failure — it is an absence, and it is reported as one.
        </p>
      ) : null}
    </div>
  );
}

/* ── Per-run matrix ─────────────────────────────────────────────────────── */

function PerRunMatrix({ bundle }: { bundle: EvidenceBundle }) {
  const ids = Array.from(
    new Set(bundle.runs.flatMap((r) => r.checks.map((c) => c.id))),
  );
  if (ids.length === 0 || bundle.runs.length === 0) return null;

  return (
    <section className="min-w-0">
      <Panel className="overflow-hidden">
        <PanelHeader
          title="Per run"
          meta={
            <span className="text-[0.625rem] text-[var(--on-surface-muted)]">
              the denominator behind &ldquo;over the runs that exercised each check&rdquo;
            </span>
          }
        />
        <div className="scroll-x">
          <table className="w-full min-w-[42rem] border-collapse text-left">
            <thead>
              <tr className="text-[0.625rem] tracking-wide text-[var(--on-surface-variant)] uppercase">
                <th className="px-4 py-2 font-semibold">Run</th>
                {ids.map((id) => (
                  <th key={id} className="px-3 py-2 font-semibold whitespace-nowrap">
                    {checkLabel(id)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bundle.runs.map((run) => (
                <tr key={run.run_id} className="border-t border-[var(--outline-variant)]">
                  <td className="px-4 py-2">
                    <Link
                      href={`/?run=${encodeURIComponent(run.run_id)}`}
                      className="hover:underline"
                    >
                      <Mono className="text-[var(--on-surface)]">
                        {shortRunId(run.run_id)}
                      </Mono>
                    </Link>
                    <span className="ml-2 text-[0.625rem] text-[var(--on-surface-muted)]">
                      {stamp(run.started_at)}
                    </span>
                  </td>
                  {ids.map((id) => {
                    const c = run.checks.find((x) => x.id === id);
                    return (
                      <td key={id} className="px-3 py-2">
                        <VerdictPill verdict={normalizeVerdict(c?.verdict)} />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </section>
  );
}

function Prov({ label, value, note }: { label: string; value?: string; note?: string }) {
  return (
    <div className="min-w-0">
      <dt className="label-caps">{label}</dt>
      <dd className="mt-0.5 break-all">
        {value ? (
          <Mono className="text-[var(--on-surface)]">{value}</Mono>
        ) : (
          <span className="text-[0.75rem] text-[var(--on-surface-muted)] italic">
            not recorded
          </span>
        )}
        {note ? (
          <p className="mt-0.5 text-[0.625rem] leading-relaxed text-[var(--on-surface-muted)]">
            {note}
          </p>
        ) : null}
      </dd>
    </div>
  );
}
