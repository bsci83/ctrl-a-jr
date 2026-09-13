import type { ReactNode } from "react";

import { ABSENT, count } from "@/lib/derive";
import { Badge, Mono, Withheld, cn } from "./primitives";

/**
 * Channel-native frames — the ctrl-a receipts idea (show a deliverable inside
 * the channel's own chrome), with artifacts.py's mechanism kept intact.
 *
 * ctrl-a's canvas takes `html` straight from the model. artifacts.py inverts
 * that: renderers are CODE, keyed by tool name, deriving the display from the
 * arguments that will actually execute. These components are the same
 * inversion in React — the tool name selects the component, the component
 * decides the shape, and every value it prints is either a structured field the
 * exporter allowlisted or a constant written here.
 *
 * Consequently there is no `dangerouslySetInnerHTML` in this file or anywhere
 * else in the app. The bundle ships a pre-rendered `artifact_html` string; it
 * is never read. React escapes everything by default and that default is the
 * whole defence, because some of these fields quote a customer's own email.
 *
 * And the customer's text is not here to render: the activity log keeps message
 * bodies out on purpose (design §6), so the frames show the CARD the approver
 * saw with its content marked unrecorded — not a reconstruction of the message.
 */

export interface ArtifactFacts {
  tool: string;
  /** How many characters the approver actually saw rendered, if logged. */
  renderedChars?: number | null;
  /** Truncated hash of the payload that was approved, if this came from the gate. */
  payloadHash?: string | null;
  /** Characters the tool returned, for read-only calls. */
  resultChars?: number | null;
  ok?: boolean;
  mutating?: boolean;
  errorType?: string;
}

const UNRECORDED =
  "The activity log records the tool, the approval id, the payload hash and a character count — never the message text (design §6), so the text cannot be exported.";

/* ── Frame chrome ───────────────────────────────────────────────────────── */

function Frame({
  brand,
  caption,
  accent,
  children,
  warn,
}: {
  brand: string;
  caption: string;
  accent: string;
  children: ReactNode;
  warn?: string;
}) {
  return (
    <div className="overflow-hidden rounded-xl bg-[var(--surface-high)] ghost-border">
      <div className="flex items-center gap-2.5 border-b border-[var(--outline)] px-4 py-2.5">
        <span
          aria-hidden
          className="flex size-6 shrink-0 items-center justify-center rounded-md text-[0.625rem] font-black text-white"
          style={{ backgroundColor: accent }}
        >
          {brand.charAt(0)}
        </span>
        <p className="min-w-0 text-xs font-semibold text-[var(--on-surface)]">
          {brand}{" "}
          <span className="font-normal text-[var(--on-surface-variant)]">{caption}</span>
        </p>
      </div>
      <div className="px-4 py-3.5">{children}</div>
      {warn ? (
        <p className="border-t border-[var(--outline)] bg-[var(--warning-surface)] px-4 py-2 text-[0.6875rem] leading-relaxed text-[var(--warning)]">
          {warn}
        </p>
      ) : null}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 py-1 text-[0.8125rem]">
      <span className="w-20 shrink-0 text-[var(--on-surface-muted)]">{label}</span>
      <span className="min-w-0 flex-1 break-words text-[var(--on-surface)]">{children}</span>
    </div>
  );
}

/**
 * The body slot. Never a reconstruction — it states what the approver saw the
 * size of, and why the text itself is absent.
 */
function RedactedBody({ renderedChars, kind }: { renderedChars?: number | null; kind: string }) {
  return (
    <div className="mt-3 rounded-lg border border-dashed border-[var(--outline)] bg-[var(--surface-container)] px-3 py-2.5">
      <p className="text-[0.6875rem] font-semibold tracking-wide text-[var(--on-surface-variant)] uppercase">
        {kind} withheld
      </p>
      <p className="mt-1 text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
        {typeof renderedChars === "number"
          ? `The approver saw ${count(renderedChars)} rendered character(s). `
          : ""}
        {UNRECORDED}
      </p>
    </div>
  );
}

/* ── Per-tool frames ────────────────────────────────────────────────────── */

const ACCENT = {
  gmail: "#c5221f",
  slack: "#4a154b",
  stripe: "#635bff",
  write: "#5b6470",
  quote: "#0f766e",
  model: "#7c3aed",
} as const;

function EmailFrame({ facts }: { facts: ArtifactFacts }) {
  return (
    <Frame
      brand="Email"
      caption="— will be sent as you"
      accent={ACCENT.gmail}
      warn="This leaves your account and reaches a real customer."
    >
      <Field label="To">
        <Withheld>recipient not recorded</Withheld>
      </Field>
      <Field label="Subject">
        <Withheld>subject not recorded</Withheld>
      </Field>
      <RedactedBody kind="Message body" renderedChars={facts.renderedChars} />
    </Frame>
  );
}

function SlackFrame({ facts }: { facts: ArtifactFacts }) {
  return (
    <Frame brand="Slack" caption="— message to the shop channel" accent={ACCENT.slack}>
      <Field label="Channel">
        <Withheld>channel not recorded</Withheld>
      </Field>
      <RedactedBody kind="Post text" renderedChars={facts.renderedChars} />
    </Frame>
  );
}

function InvoiceSendFrame({ facts }: { facts: ArtifactFacts }) {
  return (
    <Frame
      brand="Stripe"
      caption="— will email this invoice"
      accent={ACCENT.stripe}
      warn="Stripe sends this from your account, with a payment link."
    >
      <Field label="Invoice">
        <Withheld>invoice id not recorded</Withheld>
      </Field>
      {typeof facts.renderedChars === "number" ? (
        <Field label="Card size">
          <Mono>{count(facts.renderedChars)} chars</Mono>
        </Field>
      ) : null}
    </Frame>
  );
}

/**
 * The quote invoice. artifacts.py RE-PRICES this from the menu rather than
 * reading an amount out of the model's arguments — that is the property worth
 * porting. But the arguments (service, size, add-ons) are not in the log, so
 * there is no configuration here to re-price. Printing a number anyway would be
 * inventing the single figure this whole project exists to make trustworthy.
 * So the frame states the property and points at the menu instead.
 */
function QuoteInvoiceFrame() {
  return (
    <Frame
      brand="Stripe"
      caption="— invoice for a menu-priced quote"
      accent={ACCENT.stripe}
      warn="This creates a real invoice on the shop's Stripe account and a payment link the customer can pay. The amount is computed from the service menu in pricing.py, not chosen by the model."
    >
      <Field label="Customer">
        <Withheld>not recorded</Withheld>
      </Field>
      <Field label="Quote">
        <Withheld>configuration not recorded</Withheld>
      </Field>
      <div className="mt-3 rounded-lg border border-dashed border-[var(--outline)] bg-[var(--surface-container)] px-3 py-2.5">
        <p className="text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
          The approval card the operator saw carried an itemised breakdown,
          re-derived from the menu by the same pure function the invoice calls. The
          service, vehicle size and add-ons that selected those line items are not
          in the activity log, so this page cannot reproduce the figure — and will
          not print one it did not get.{" "}
          <span className="text-[var(--on-surface-variant)]">
            The menu itself is on the Quote tab.
          </span>
        </p>
      </div>
    </Frame>
  );
}

function ReportFrame({ facts }: { facts: ArtifactFacts }) {
  return (
    <Frame brand="Report" caption="— written to disk" accent={ACCENT.write}>
      <Field label="File">
        <Withheld>filename not recorded</Withheld>
      </Field>
      <RedactedBody kind="Report contents" renderedChars={facts.renderedChars} />
    </Frame>
  );
}

function ProviderFrame() {
  return (
    <Frame
      brand="Model"
      caption="— switch provider"
      accent={ACCENT.model}
      warn="Approving this changes which model produced the rest of the run."
    >
      <Field label="Provider">
        <Withheld>not recorded</Withheld>
      </Field>
      <Field label="Model">
        <Withheld>not recorded</Withheld>
      </Field>
    </Frame>
  );
}

/**
 * Read-only lookups. These never reach the gate, and the one fact a reader
 * needs about them is that nothing left the building — so they get their own
 * frame rather than the generic table, exactly as artifacts._lookup does.
 */
function LookupFrame({ facts }: { facts: ArtifactFacts }) {
  const failed = facts.ok === false;
  return (
    <div
      className={cn(
        "rounded-xl bg-[var(--surface-high)] px-4 py-3 ghost-border",
        failed && "border-[var(--error)]/40",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={failed ? "error" : "info"}>
          {failed ? "read failed" : "read-only"}
        </Badge>
        <Mono className="text-[var(--on-surface)]">{facts.tool}</Mono>
      </div>
      <p className="mt-2 text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
        {failed
          ? "The lookup errored. Nothing left the shop's accounts either way — a read cannot mutate anything, which is why it never stops for approval."
          : "Nothing left the shop's accounts. Read-only tools do not pass through the approval gate, because there is nothing to undo."}
      </p>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[0.6875rem] text-[var(--on-surface-muted)]">
        <span>
          returned{" "}
          <Mono className="text-[var(--on-surface-variant)]">
            {typeof facts.resultChars === "number" ? `${count(facts.resultChars)} chars` : ABSENT}
          </Mono>
        </span>
        {facts.errorType ? (
          <span>
            error{" "}
            <Mono className="text-[var(--error)]">{facts.errorType}</Mono>{" "}
            <span className="italic">(class name only — reprs carry credentials)</span>
          </span>
        ) : null}
      </div>
    </div>
  );
}

function FallbackFrame({ facts }: { facts: ArtifactFacts }) {
  return (
    <div className="rounded-xl bg-[var(--surface-high)] px-4 py-3 ghost-border">
      <Mono className="text-[var(--on-surface)]">{facts.tool}</Mono>
      <p className="mt-2 text-[0.75rem] leading-relaxed text-[var(--on-surface-muted)]">
        No typed frame is registered for this tool, so nothing is guessed about
        what it does. The exported fields for the call are in the event stream
        drawer.
      </p>
    </div>
  );
}

/* ── The registry, keyed by tool name (artifacts.RENDERERS) ─────────────── */

const READ_ONLY = new Set([
  "stripe_list_failed_payments",
  "stripe_get_customer",
  "stripe_get_invoice",
  "stripe_find_customers",
  "gmail_search_threads",
  "gmail_read_thread",
  "slack_lookup_user",
  "quote_menu",
  "quote_price",
]);

export function ArtifactFrame({ facts }: { facts: ArtifactFacts }) {
  switch (facts.tool) {
    case "gmail_send":
      return <EmailFrame facts={facts} />;
    case "slack_post_message":
      return <SlackFrame facts={facts} />;
    case "stripe_send_invoice":
      return <InvoiceSendFrame facts={facts} />;
    case "stripe_create_quote_invoice":
      return <QuoteInvoiceFrame />;
    case "write_report":
      return <ReportFrame facts={facts} />;
    case "provider_switch":
      return <ProviderFrame />;
    default:
      return READ_ONLY.has(facts.tool) ? (
        <LookupFrame facts={facts} />
      ) : (
        <FallbackFrame facts={facts} />
      );
  }
}
