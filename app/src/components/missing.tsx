import { Panel } from "./primitives";

/**
 * What the app shows when it could not read the bundle.
 *
 * Deliberately NOT an empty state with zeroed tiles. "0 unapproved mutating
 * actions" computed over a bundle that failed to load is a green page made out
 * of nothing, which is the exact failure this surface exists to prevent. So a
 * read failure renders as a failure, in the error ink, and no derived number is
 * shown beside it.
 */
export function NoBundle({ reason, source }: { reason: string; source: string }) {
  return (
    <Panel
      className="mt-8 border-[var(--error)]/45 p-6"
      style={{ background: "var(--error-surface)" }}
    >
      <p className="label-caps" style={{ color: "var(--error)" }}>
        No evidence loaded
      </p>
      <p className="mt-2 max-w-[65ch] text-sm leading-relaxed text-[var(--on-surface)]">{reason}</p>
      <p className="mt-3 text-[0.6875rem] text-[var(--on-surface-variant)]">
        Nothing is claimed on this page while the bundle at{" "}
        <code className="font-[family-name:var(--font-mono)]">{source}</code> is
        unreadable. An absent verdict is not a passing one.
      </p>
    </Panel>
  );
}
