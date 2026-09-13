import type { ReactNode } from "react";

import type { CheckVerdict } from "@/lib/types";

export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/* ── Panel ──────────────────────────────────────────────────────────────── */

export function Panel({
  children,
  className,
  ...rest
}: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("rounded-xl bg-[var(--surface-container)] ghost-border", className)}
      {...rest}
    >
      {children}
    </div>
  );
}

export function PanelHeader({ title, meta }: { title: ReactNode; meta?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--outline)] px-4 py-2.5">
      <p className="label-caps">{title}</p>
      {meta ? <div className="flex items-center gap-2 text-xs">{meta}</div> : null}
    </div>
  );
}

/* ── Badge ──────────────────────────────────────────────────────────────── */

export type Tone = "neutral" | "success" | "warning" | "error" | "info" | "unknown" | "primary";

const TONE_INK: Record<Tone, string> = {
  neutral: "var(--on-surface-variant)",
  success: "var(--success)",
  warning: "var(--warning)",
  error: "var(--error)",
  info: "var(--info)",
  unknown: "var(--unknown)",
  primary: "var(--primary-bright)",
};

export function Badge({
  children,
  tone = "neutral",
  dashed = false,
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  dashed?: boolean;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[0.625rem] font-semibold whitespace-nowrap",
        dashed ? "border border-dashed" : "border border-solid",
      )}
      style={{
        color: TONE_INK[tone],
        borderColor: TONE_INK[tone],
        background: "color-mix(in oklab, currentColor 12%, transparent)",
      }}
    >
      {children}
    </span>
  );
}

/* ── Verdict pill — the most important visual contract in the app ────────── */

/**
 * Three states, three shapes. `inconclusive` is NOT a paler green: it is a
 * different ink, a DASHED border and the word "inconclusive" spelled out, so a
 * check that was never exercised cannot be skim-read as a check that passed.
 * There is no default branch that resolves to the pass styling — anything the
 * caller did not normalise to "pass"/"fail" arrives here as inconclusive.
 */
export function VerdictPill({ verdict }: { verdict: CheckVerdict }) {
  if (verdict === "pass") {
    return (
      <Badge tone="success" title="This check was exercised and it held.">
        <Dot /> pass
      </Badge>
    );
  }
  if (verdict === "fail") {
    return (
      <Badge tone="error" title="This check was exercised and it did not hold.">
        <Cross /> fail
      </Badge>
    );
  }
  return (
    <Badge
      tone="unknown"
      dashed
      title="Not a pass. Nothing in this evidence exercised the check, so it is not measured either way."
    >
      <Hollow /> inconclusive
    </Badge>
  );
}

function Dot() {
  return <span aria-hidden className="inline-block size-1.5 rounded-full bg-current" />;
}

function Hollow() {
  return (
    <span
      aria-hidden
      className="inline-block size-1.5 rounded-full border border-current border-dashed"
    />
  );
}

function Cross() {
  return (
    <span aria-hidden className="inline-block text-[0.7rem] leading-none">
      ×
    </span>
  );
}

/* ── Stat tile ──────────────────────────────────────────────────────────── */

export function Stat({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
}) {
  return (
    <Panel className="flex min-w-0 flex-col gap-1 p-4">
      <p className="label-caps truncate">{label}</p>
      <p
        className="text-2xl leading-none font-black tabular-nums"
        style={{ color: TONE_INK[tone] }}
      >
        {value}
      </p>
      {sub ? (
        <p className="text-[0.6875rem] text-[var(--on-surface-muted)]">{sub}</p>
      ) : null}
    </Panel>
  );
}

/* ── Absence ────────────────────────────────────────────────────────────── */

/**
 * The honest rendering of a field the bundle does not carry. Used instead of a
 * placeholder value anywhere the data is redacted by design — a plausible
 * fabrication on an evidence page is worse than a blank.
 */
export function Withheld({ children }: { children?: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded border border-dashed border-[var(--outline)] px-1.5 py-0.5 text-[0.6875rem] text-[var(--on-surface-muted)] italic">
      {children ?? "not recorded"}
    </span>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cn("font-[family-name:var(--font-mono)] text-[0.75rem]", className)}>
      {children}
    </span>
  );
}
