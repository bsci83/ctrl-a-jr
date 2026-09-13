import { ABSENT, clock } from "@/lib/derive";
import type { EvidenceEvent, Redaction, Run } from "@/lib/types";
import { Badge, Mono } from "./primitives";

/**
 * The raw event stream, collapsed by default.
 *
 * Everything above it is a reading of this. The drawer is where a sceptic
 * checks that reading against the records it came from, so it prints the
 * exported fields verbatim and nothing else — and it names the allowlist that
 * decided which fields exist at all, because the interesting thing about this
 * log is as much what is absent as what is present.
 *
 * `<details>` rather than React state: it works before hydration and it keeps
 * this a server component.
 */
export function EventDrawer({ run, redaction }: { run: Run; redaction?: Redaction }) {
  return (
    <details className="group mt-4 rounded-xl bg-[var(--surface-container)] ghost-border">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 px-4 py-3">
        <Badge tone="neutral">event stream</Badge>
        <span className="text-xs font-semibold text-[var(--on-surface)]">
          {run.events.length} raw record(s)
        </span>
        <span className="text-[0.6875rem] text-[var(--on-surface-muted)]">
          exactly as exported — allowlisted fields only
        </span>
        <span
          aria-hidden
          className="ml-auto text-[0.625rem] text-[var(--on-surface-variant)] group-open:hidden"
        >
          show
        </span>
        <span
          aria-hidden
          className="ml-auto hidden text-[0.625rem] text-[var(--on-surface-variant)] group-open:inline"
        >
          hide
        </span>
      </summary>

      <div className="border-t border-[var(--outline)]">
        <div className="scroll-x max-h-[28rem] overflow-y-auto">
          <table className="w-full min-w-[52rem] border-collapse text-left">
            <thead className="sticky top-0 bg-[var(--surface-high)]">
              <tr className="text-[0.625rem] tracking-wide text-[var(--on-surface-variant)] uppercase">
                <Th>time</Th>
                <Th>turn</Th>
                <Th>event</Th>
                <Th>tool</Th>
                <Th>approval</Th>
                <Th>fields</Th>
              </tr>
            </thead>
            <tbody>
              {run.events.map((e, i) => (
                <tr
                  key={`${i}-${e.event}-${e.ts ?? ""}`}
                  className="border-t border-[var(--outline-variant)] align-top hover:bg-[var(--surface-high)]/40"
                >
                  <Td>
                    <Mono className="text-[var(--on-surface-muted)]">{clock(e.ts)}</Mono>
                  </Td>
                  <Td>
                    <Mono className="text-[var(--on-surface-muted)]">{e.turn ?? ABSENT}</Mono>
                  </Td>
                  <Td>
                    <Mono className="font-semibold text-[var(--on-surface)]">{e.event}</Mono>
                    {e.unknown_event ? (
                      <span className="ml-1">
                        <Badge tone="unknown" dashed>
                          unknown type — fields dropped
                        </Badge>
                      </span>
                    ) : null}
                  </Td>
                  <Td>
                    <Mono className="text-[var(--on-surface-variant)]">{e.tool ?? ABSENT}</Mono>
                  </Td>
                  <Td>
                    <Mono className="text-[var(--on-surface-muted)]">
                      {e.approval_id ?? ABSENT}
                    </Mono>
                  </Td>
                  <Td>
                    <Rest event={e} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {redaction ? (
          <div className="border-t border-[var(--outline)] px-4 py-3">
            <p className="label-caps">Redaction policy: {redaction.policy}</p>
            <p className="mt-1 max-w-[80ch] text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
              {redaction.note}
            </p>
            <p className="mt-2 text-[0.6875rem] text-[var(--on-surface-muted)]">
              Derived on export, not read off a record:{" "}
              <Mono className="text-[var(--on-surface-variant)]">
                {redaction.derived_fields.join(", ")}
              </Mono>
            </p>
          </div>
        ) : null}
      </div>
    </details>
  );
}

const SHOWN_ELSEWHERE = new Set([
  "event",
  "ts",
  "turn",
  "tool",
  "approval_id",
  "run_id",
  "agent",
  "integration",
  "integration_label",
  "unknown_event",
]);

/** Every remaining exported field, printed as key=value. No interpretation. */
function Rest({ event }: { event: EvidenceEvent }) {
  const pairs = Object.entries(event as unknown as Record<string, unknown>).filter(
    ([k, v]) => !SHOWN_ELSEWHERE.has(k) && v !== null && v !== undefined,
  );
  if (pairs.length === 0) {
    return <span className="text-[0.6875rem] text-[var(--on-surface-muted)]">{ABSENT}</span>;
  }
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-0.5">
      {pairs.map(([k, v]) => (
        <Mono key={k} className="text-[var(--on-surface-muted)]">
          {k}=
          <span className="text-[var(--on-surface-variant)]">{String(v)}</span>
        </Mono>
      ))}
    </span>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3 py-2 font-semibold whitespace-nowrap">{children}</th>;
}

function Td({ children }: { children: React.ReactNode }) {
  return <td className="px-3 py-1.5 text-[0.75rem]">{children}</td>;
}
