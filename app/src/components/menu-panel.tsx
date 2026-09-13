import { ADDONS, SERVICES, SIZES, SLACK_THRESHOLD_CENTS, adjustedBaseCents, dollars } from "@/lib/menu";
import { Badge, Panel, PanelHeader } from "./primitives";

/**
 * The service menu — the code-owned price list, shown as itself.
 *
 * This is the claim "the model never picks the number" made inspectable. The
 * model classifies a request into a service, a vehicle size and a set of
 * add-ons; the price comes from this table, via a pure function, and the same
 * function prices the approval card and the Stripe invoice, so they cannot
 * disagree.
 *
 * It is labelled as the menu and never as a run's quote. The arguments that
 * would select a row here are not in the activity log, so no row is highlighted
 * and no total is attributed to any customer.
 */
export function MenuPanel() {
  return (
    <Panel className="min-w-0 overflow-hidden">
      <PanelHeader
        title="Service menu — the only place a price comes from"
        meta={<Badge tone="info">from code, not from this run</Badge>}
      />
      <div className="px-4 py-3.5">
        <p className="max-w-[75ch] text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
          Transcribed from <code className="font-[family-name:var(--font-mono)]">src/ctrl_a_jr/pricing.py</code>.
          The model classifies; this table prices. No row below is this run&rsquo;s
          quote — the service, size and add-ons a given request resolved to are
          not recorded in the activity log, so the page will not attribute a
          figure to a customer it cannot evidence.
        </p>

        <div className="scroll-x mt-3">
          <table className="w-full min-w-[34rem] border-collapse text-left text-[0.8125rem]">
            <thead>
              <tr className="text-[0.625rem] tracking-wide text-[var(--on-surface-variant)] uppercase">
                <th className="px-2 py-1.5 font-semibold">Service</th>
                <th className="px-2 py-1.5 text-right font-semibold">Sedan</th>
                {SIZES.filter((s) => s.key !== "sedan").map((s) => (
                  <th key={s.key} className="px-2 py-1.5 text-right font-semibold whitespace-nowrap">
                    {s.label} · {s.percent}%
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {SERVICES.map((svc) => (
                <tr key={svc.key} className="border-t border-[var(--outline-variant)]">
                  <td className="px-2 py-1.5 text-[var(--on-surface)]">{svc.label}</td>
                  {SIZES.map((size) => {
                    const cents = adjustedBaseCents(svc.baseCents, size.percent);
                    const over = cents > SLACK_THRESHOLD_CENTS;
                    return (
                      <td
                        key={size.key}
                        className="px-2 py-1.5 text-right tabular-nums whitespace-nowrap"
                        style={{ color: over ? "var(--warning)" : "var(--on-surface)" }}
                        title={over ? "Above the Slack escalation threshold." : undefined}
                      >
                        {dollars(cents)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2">
          <span className="label-caps">Add-ons</span>
          {ADDONS.map((a) => (
            <span key={a.key} className="text-[0.75rem] text-[var(--on-surface-variant)]">
              {a.label}{" "}
              <span className="font-black tabular-nums text-[var(--on-surface)]">
                {dollars(a.cents)}
              </span>
            </span>
          ))}
        </div>

        <p className="mt-3 text-[0.6875rem] leading-relaxed text-[var(--on-surface-muted)]">
          Anything over{" "}
          <span className="font-black text-[var(--warning)]">
            {dollars(SLACK_THRESHOLD_CENTS)}
          </span>{" "}
          escalates to Slack for a human to see before it becomes a routine
          reply. Amber figures above already cross it on their own, before
          add-ons.
        </p>
      </div>
    </Panel>
  );
}
