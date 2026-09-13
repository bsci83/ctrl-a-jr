import { ChannelTabs } from "@/components/channel-tabs";
import { NoBundle } from "@/components/missing";
import { Panel } from "@/components/primitives";
import { RunList } from "@/components/run-list";
import { integrationsOf } from "@/lib/derive";
import { getEvidence, selectRun } from "@/lib/evidence";

export const dynamic = "force-dynamic";

/**
 * Per-integration view.
 *
 * The tab list is DERIVED from the tool-name prefixes present in the data —
 * export.integrations_in does this on the Python side and derive.integrationsOf
 * re-derives it here if the bundle ever lacks the precomputed array. Nothing is
 * hardcoded: the bundle this was built against carries five integrations
 * (Stripe, Email, Slack, Reports, Quote), and a sixth appears the day a run
 * emits a tool named after it.
 */
export default async function ChannelsPage({
  searchParams,
}: {
  searchParams: Promise<{ run?: string; tab?: string }>;
}) {
  const result = await getEvidence();
  if (!result.ok) return <NoBundle reason={result.reason} source={result.source} />;

  const bundle = result.bundle;
  const { run: requestedRun, tab } = await searchParams;
  const { run, matched } = selectRun(bundle, requestedRun);
  const integrations = integrationsOf(bundle);

  return (
    <div className="pt-6">
      <h1 className="text-2xl font-black tracking-tight text-[var(--on-surface)]">Channels</h1>
      <p className="mt-0.5 max-w-[75ch] text-sm text-[var(--on-surface-variant)]">
        Each integration&rsquo;s side of the run, in that channel&rsquo;s own frame. The
        tab list comes from the tool names in the log, so a new integration shows
        up here the day it is used — not the day someone remembers to add it.
      </p>

      <div className="mt-5 grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,17rem)_minmax(0,1fr)]">
        <div className="lg:sticky lg:top-20 lg:max-h-[calc(100vh-9rem)]">
          <RunList
            runs={bundle.runs}
            selectedId={run?.run_id ?? null}
            basePath="/channels"
            unmatched={!matched}
          />
        </div>

        <div className="min-w-0">
          {run ? (
            <ChannelTabs run={run} integrations={integrations} activeKey={tab} />
          ) : (
            <Panel className="p-6 text-sm text-[var(--on-surface-muted)]">
              No runs in this bundle, so no channel has anything to show.
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
