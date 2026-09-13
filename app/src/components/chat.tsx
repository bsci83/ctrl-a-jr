"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Badge, Mono, Panel, Withheld, cn } from "./primitives";
import type { AgentTurn, RunPayload, RunSummary } from "@/lib/types-agent";
import { canListen, canSpeak, listen, speak, stopSpeaking } from "@/lib/voice";

/**
 * The console. This is the surface that makes the agent a thing you can talk
 * to rather than a recording you can watch.
 *
 * Every network call goes to this app's OWN /api/agent/* routes. Nothing here
 * knows the agent API's URL or holds its bearer token — see lib/agent.ts.
 *
 * What this component is NOT allowed to be
 * ----------------------------------------
 * An approval surface. When a run suspends, the card below links to the signed,
 * single-use approval page and stops. A second place to say yes is a second
 * place to get "yes" wrong, and this one would have no signature behind it.
 *
 * Text rendering is plain React children throughout: assistant text, tool
 * output previews and approval artifacts all quote a customer's own email, and
 * there is deliberately no code path in this file that turns any of it into
 * markup.
 */

const POLL_MS = 2000;
/** While a run is suspended, ask the API to pick up a decision made elsewhere.
 * `/advance` carries no decision — it can only copy one a human already made. */
const ADVANCE_EVERY_MS = 6000;

type Busy = null | "starting" | "sending" | "loading";

export function Chat() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [run, setRun] = useState<RunPayload | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  // Voice is a progressive enhancement on a working text UI: both directions are
  // capability-checked and off unless the operator turns them on.
  const [voiceOn, setVoiceOn] = useState(false);
  const [listening, setListening] = useState(false);
  const stopListenRef = useRef<null | (() => void)>(null);
  const spokenRef = useRef<string | null>(null);
  const [since, setSince] = useState<number | null>(null);

  const runIdRef = useRef<string | null>(null);
  const inFlight = useRef(false);
  const lastAdvance = useRef(0);
  const bottom = useRef<HTMLDivElement | null>(null);

  const live = run?.status === "running" || run?.status === "awaiting_approval";

  /* ── run list ──────────────────────────────────────────────────────────── */

  const refreshRuns = useCallback(async () => {
    try {
      const response = await fetch("/api/agent/runs", { cache: "no-store" });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        setRunsError(body?.error ?? `The run list failed (${response.status}).`);
        return;
      }
      setRunsError(null);
      setRuns(Array.isArray(body?.runs) ? body.runs : []);
    } catch {
      setRunsError("Could not reach this app's server to list runs.");
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
  }, [refreshRuns]);

  /* ── open a run ────────────────────────────────────────────────────────── */

  const openRun = useCallback(async (runId: string) => {
    runIdRef.current = runId;
    setBusy("loading");
    setError(null);
    try {
      const response = await fetch(`/api/agent/runs/${encodeURIComponent(runId)}`, {
        cache: "no-store",
      });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        setError(body?.error ?? `Could not load that run (${response.status}).`);
        setRun(null);
        return;
      }
      setRun(body as RunPayload);
    } catch {
      setError("Could not reach this app's server.");
    } finally {
      setBusy(null);
    }
  }, []);

  /* ── polling ───────────────────────────────────────────────────────────── */

  useEffect(() => {
    if (!live) return;
    const runId = run?.run_id;
    if (!runId) return;

    const tick = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const suspended = run?.status === "awaiting_approval";
        const due = Date.now() - lastAdvance.current > ADVANCE_EVERY_MS;
        const url = `/api/agent/runs/${encodeURIComponent(runId)}${
          suspended && due ? "/advance" : ""
        }`;
        if (suspended && due) lastAdvance.current = Date.now();
        const response = await fetch(url, {
          method: suspended && due ? "POST" : "GET",
          cache: "no-store",
        });
        const body = await response.json().catch(() => null);
        if (runIdRef.current !== runId) return;
        if (response.ok && body?.run_id) {
          setRun(body as RunPayload);
          if (body.status !== "running" && body.status !== "awaiting_approval") {
            void refreshRuns();
          }
        }
        // A failed poll is not shown. The run is still whatever it is; a
        // transient network blip must not overwrite the conversation with an
        // error banner. A real failure surfaces on the next user action.
      } catch {
        /* see above */
      } finally {
        inFlight.current = false;
      }
    };

    const timer = setInterval(tick, POLL_MS);
    return () => clearInterval(timer);
  }, [live, run?.run_id, run?.status, refreshRuns]);

  /* ── elapsed clock, so a 40s turn does not look like a dead page ───────── */

  const [, forceTick] = useState(0);
  useEffect(() => {
    if (busy === null && !live) {
      setSince(null);
      return;
    }
    setSince((s) => s ?? Date.now());
    const timer = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [busy, live]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [run?.conversation?.length, run?.status]);

  /* ── send ──────────────────────────────────────────────────────────────── */

  const send = useCallback(async () => {
    const message = draft.trim();
    if (!message || busy !== null) return;
    const existing = run && run.status === "done" ? run.run_id : null;
    setBusy(existing ? "sending" : "starting");
    setError(null);
    setSince(Date.now());
    try {
      const url = existing
        ? `/api/agent/runs/${encodeURIComponent(existing)}/messages`
        : "/api/agent/runs";
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        setError(body?.error ?? `The agent refused that (${response.status}).`);
        return;
      }
      setDraft("");
      runIdRef.current = body.run_id;
      setRun(body as RunPayload);
      void refreshRuns();
    } catch {
      setError(
        "The request did not complete. A turn can take a minute; if the run was " +
          "started it will appear in the run list.",
      );
      void refreshRuns();
    } finally {
      setBusy(null);
    }
  }, [draft, busy, run, refreshRuns]);

  const canSend = busy === null && (run === null || run.status === "done" || run.status === "failed");

  useEffect(() => {
    if (!voiceOn) return;
    const turns = run?.conversation ?? [];
    let latest: string | null = null;
    for (const turn of turns) {
      if (turn.kind === "assistant" && turn.text?.trim()) latest = turn.text;
    }
    // Keyed on the text, not an index: polling re-delivers the same
    // conversation every two seconds and would otherwise re-speak it forever.
    if (latest && latest !== spokenRef.current) {
      spokenRef.current = latest;
      speak(latest);
    }
  }, [run, voiceOn]);

  useEffect(() => () => {
    stopListenRef.current?.();
    stopSpeaking();
  }, []);

  const toggleListening = useCallback(() => {
    if (listening) {
      stopListenRef.current?.();
      stopListenRef.current = null;
      setListening(false);
      return;
    }
    // Speaking while listening makes the agent transcribe itself.
    stopSpeaking();
    setListening(true);
    stopListenRef.current = listen(
      (text) => setDraft((d) => (d ? `${d} ${text}` : text)),
      () => {
        stopListenRef.current = null;
        setListening(false);
      },
    );
  }, [listening]);

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,17rem)_minmax(0,1fr)]">
      <div className="lg:sticky lg:top-20 lg:max-h-[calc(100vh-9rem)]">
        <RunSidebar
          runs={runs}
          error={runsError}
          selectedId={run?.run_id ?? null}
          onSelect={openRun}
          onNew={() => {
            runIdRef.current = null;
            setRun(null);
            setError(null);
          }}
          onRefresh={refreshRuns}
        />
      </div>

      <div className="flex min-w-0 flex-col gap-3">
        <StatusStrip run={run} busy={busy} since={since} />

        {error ? (
          <Panel className="px-4 py-3" style={{ background: "var(--error-surface)" }}>
            <p className="text-[0.8125rem] leading-relaxed text-[var(--on-surface)]">{error}</p>
          </Panel>
        ) : null}

        {run === null ? (
          <EmptyState busy={busy} />
        ) : (
          <div className="flex min-w-0 flex-col gap-2">
            {run.conversation.length === 0 ? (
              <p className="text-[0.75rem] text-[var(--on-surface-muted)]">
                This run has no turns recorded yet.
              </p>
            ) : null}
            {run.conversation.map((turn, i) => (
              <Turn key={`${i}:${turn.kind}:${turn.id ?? turn.approval_id ?? ""}`} turn={turn} />
            ))}
            {run.status === "failed" ? <FailedCard detail={run.detail} /> : null}
            {run.hit_limit ? (
              <p className="rounded-lg border border-dashed border-[var(--outline)] px-3 py-2 text-[0.75rem] text-[var(--warning)]">
                The run hit its round limit. What it said is a forced summary, not a
                finished answer.
              </p>
            ) : null}
            {run.note ? (
              <p className="rounded-lg border border-dashed border-[var(--outline)] px-3 py-2 text-[0.75rem] text-[var(--on-surface-variant)]">
                {run.note}
              </p>
            ) : null}
          </div>
        )}

        <div ref={bottom} />

        <Composer
          draft={draft}
          setDraft={setDraft}
          onSend={send}
          canSend={canSend}
          busy={busy}
          status={run?.status ?? null}
          voiceOn={voiceOn}
          setVoiceOn={setVoiceOn}
          listening={listening}
          onToggleListening={toggleListening}
        />
      </div>
    </div>
  );
}

/* ── status ─────────────────────────────────────────────────────────────── */

/**
 * Status in the agent's words, not in reassuring ones. `failed` says failed;
 * `awaiting_approval` says a human has to decide, and does not imply this page
 * is where.
 */
function StatusStrip({
  run,
  busy,
  since,
}: {
  run: RunPayload | null;
  busy: Busy;
  since: number | null;
}) {
  const elapsed = since ? Math.max(0, Math.round((Date.now() - since) / 1000)) : 0;
  const working = busy !== null || run?.status === "running";

  return (
    <Panel className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5">
      <p className="label-caps">Run</p>
      <Mono className="text-[var(--on-surface-variant)]">{run?.run_id ?? "none yet"}</Mono>
      {run ? <StatusBadge status={run.status} /> : <Badge tone="neutral">not started</Badge>}
      {working ? (
        <span className="flex items-center gap-2 text-[0.6875rem] text-[var(--on-surface-muted)]">
          <Spinner />
          {busy === "starting"
            ? "starting the run — one turn is a model round trip plus its tool calls"
            : busy === "sending"
              ? "sending your message"
              : busy === "loading"
                ? "loading the run"
                : "the agent is working"}
          {elapsed > 0 ? ` · ${elapsed}s` : null}
        </span>
      ) : null}
      {run?.status === "awaiting_approval" ? (
        <span className="text-[0.6875rem] text-[var(--on-surface-muted)]">
          polling for a decision made on the approval page
        </span>
      ) : null}
      <span className="ml-auto text-[0.625rem] text-[var(--on-surface-muted)]">
        {typeof run?.rounds === "number" ? `${run.rounds} round(s)` : null}
      </span>
    </Panel>
  );
}

function StatusBadge({ status }: { status: string }) {
  if (status === "running") return <Badge tone="info">running</Badge>;
  if (status === "awaiting_approval") return <Badge tone="warning">awaiting your approval</Badge>;
  if (status === "done") return <Badge tone="success">done</Badge>;
  if (status === "failed") return <Badge tone="error">failed</Badge>;
  // Not a default that resolves to something benign: an unrecognised status is
  // reported as unrecognised.
  return (
    <Badge tone="unknown" dashed title="A status this UI does not recognise.">
      {status || "unknown"}
    </Badge>
  );
}

function Spinner() {
  return (
    <span
      aria-hidden
      className="inline-block size-2.5 animate-spin rounded-full border border-current border-t-transparent"
    />
  );
}

/* ── turns ──────────────────────────────────────────────────────────────── */

function Turn({ turn }: { turn: AgentTurn }) {
  if (turn.kind === "human") return <HumanTurn text={turn.text ?? ""} />;
  if (turn.kind === "assistant") return <AssistantTurn text={turn.text ?? ""} />;
  if (turn.kind === "tool_call") return <ToolChip turn={turn} />;
  if (turn.kind === "approval") return <ApprovalCard turn={turn} />;
  return (
    <div className="rounded-lg border border-dashed border-[var(--outline)] px-3 py-2">
      <Badge tone="unknown" dashed>
        unrecognised turn: {String(turn.kind)}
      </Badge>
    </div>
  );
}

function HumanTurn({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[70ch] rounded-xl bg-[var(--surface-high)] px-4 py-2.5 ghost-border">
        <p className="label-caps mb-1">You</p>
        <p className="text-[0.8125rem] leading-relaxed whitespace-pre-wrap text-[var(--on-surface)]">
          {text}
        </p>
      </div>
    </div>
  );
}

function AssistantTurn({ text }: { text: string }) {
  return (
    <div className="max-w-[75ch]">
      <p className="label-caps mb-1">ctrl-a JR</p>
      <p className="text-[0.8125rem] leading-relaxed whitespace-pre-wrap text-[var(--on-surface)]">
        {text}
      </p>
    </div>
  );
}

function ToolChip({ turn }: { turn: AgentTurn }) {
  const [open, setOpen] = useState(false);
  const status = turn.status ?? "pending";
  const tone = status === "failed" ? "error" : status === "ok" ? "info" : "warning";
  const hasOutput = typeof turn.output === "string" && turn.output.length > 0;

  return (
    <div className="min-w-0 rounded-lg bg-[var(--surface-container)] ghost-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-x-2.5 gap-y-1 px-3 py-2 text-left hover:bg-[var(--surface-high)]"
      >
        <Badge tone={tone}>
          {status === "pending" ? "waiting" : status === "ok" ? "ok" : "failed"}
        </Badge>
        <Mono className="min-w-0 truncate font-semibold text-[var(--on-surface)]">
          {turn.tool || "unnamed tool"}
        </Mono>
        <span className="ml-auto text-[0.625rem] text-[var(--on-surface-muted)]">
          {hasOutput ? (open ? "hide output" : "show output") : "no output recorded"}
        </span>
      </button>
      {open ? (
        <div className="border-t border-[var(--outline)] px-3 py-2">
          {hasOutput ? (
            <pre className="max-h-64 overflow-auto text-[0.6875rem] leading-relaxed whitespace-pre-wrap text-[var(--on-surface-variant)]">
              {turn.output}
            </pre>
          ) : (
            <Withheld>
              {status === "pending"
                ? "this call has not returned — it is the one the run suspended on"
                : "no output recorded"}
            </Withheld>
          )}
          <p className="mt-2 text-[0.625rem] text-[var(--on-surface-muted)]">
            A preview of the tool&rsquo;s own output, clipped by the API. Shown as
            text, never as markup.
          </p>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The gate, inline where the run stopped.
 *
 * It shows the ARTIFACT the approver has to judge and a link to the signed,
 * single-use approval page. There is no approve or deny control here on
 * purpose: that page is the one place a decision is authorised, and a second
 * path would be a second thing to get wrong.
 */
function ApprovalCard({ turn }: { turn: AgentTurn }) {
  return (
    <div
      className="min-w-0 overflow-hidden rounded-xl ghost-border"
      style={{ background: "var(--surface-container)" }}
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--outline)] px-4 py-2.5">
        <Badge tone="warning">gate stopped here</Badge>
        <Mono className="font-semibold text-[var(--on-surface)]">
          {turn.tool || "unnamed tool"}
        </Mono>
        <Mono className="text-[var(--on-surface-muted)]">{turn.approval_id}</Mono>
        <span className="ml-auto">
          <Badge tone={turn.decision === "pending" || !turn.decision ? "unknown" : "info"} dashed={!turn.decision || turn.decision === "pending"}>
            {turn.decision || "pending"}
          </Badge>
        </span>
      </div>

      <div className="px-4 py-3.5">
        <p className="label-caps mb-1.5">What you are approving</p>
        {turn.rendered ? (
          <pre className="max-h-80 overflow-auto rounded-lg border border-[var(--outline)] bg-[var(--surface)] px-3 py-2.5 text-[0.6875rem] leading-relaxed whitespace-pre-wrap text-[var(--on-surface)]">
            {turn.rendered}
          </pre>
        ) : (
          <Withheld>the API returned no rendered artifact for this approval</Withheld>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className="text-[0.6875rem] text-[var(--on-surface-variant)]">
            Payload hash{" "}
            <Mono className="break-all text-[var(--on-surface)]">
              {turn.payload_hash || "not recorded"}
            </Mono>
          </span>
          {turn.approve_url ? (
            <a
              href={turn.approve_url}
              target="_blank"
              rel="noreferrer noopener"
              className="rounded-lg px-3 py-1.5 text-xs font-bold text-white"
              style={{ background: "var(--primary)" }}
            >
              Open the approval page →
            </a>
          ) : (
            <Withheld>no approval link was returned — decide it on the approval surface</Withheld>
          )}
        </div>

        <p className="mt-2.5 max-w-[70ch] text-[0.625rem] leading-relaxed text-[var(--on-surface-muted)]">
          Deciding happens on that page, not here. It is signed and single-use;
          this console only watches for the decision and then asks the agent to
          continue. Nothing on this page can approve, deny or execute anything.
        </p>
      </div>
    </div>
  );
}

function FailedCard({ detail }: { detail?: string }) {
  return (
    <Panel className="px-4 py-3" style={{ background: "var(--error-surface)" }}>
      <p className="text-[0.8125rem] font-black text-[var(--on-surface)]">This run failed.</p>
      <p className="mt-1 text-[0.75rem] leading-relaxed text-[var(--on-surface-variant)]">
        {detail ? detail : "The API recorded no detail for the failure."} A failed run
        cannot be continued — start a new one.
      </p>
    </Panel>
  );
}

/* ── empty state ────────────────────────────────────────────────────────── */

function EmptyState({ busy }: { busy: Busy }) {
  return (
    <Panel className="px-5 py-6">
      <p className="text-sm font-black text-[var(--on-surface)]">
        {busy === "starting" ? "Starting a run…" : "Ask the agent to do something."}
      </p>
      <p className="mt-1.5 max-w-[70ch] text-[0.8125rem] leading-relaxed text-[var(--on-surface-variant)]">
        It can read the shop&rsquo;s inbox over IMAP, price a job from the
        code-owned service menu, raise a Stripe invoice, reply by email and
        escalate anything over the Slack threshold. Every mutating step stops for
        a human decision on the approval page before it happens.
      </p>
      <p className="mt-2 text-[0.6875rem] text-[var(--on-surface-muted)]">
        Try: <em>&ldquo;Check the inbox and quote whoever is waiting longest.&rdquo;</em>
      </p>
    </Panel>
  );
}

/* ── composer ───────────────────────────────────────────────────────────── */

function Composer({
  draft,
  setDraft,
  onSend,
  canSend,
  busy,
  status,
  voiceOn,
  setVoiceOn,
  listening,
  onToggleListening,
}: {
  draft: string;
  setDraft: (v: string) => void;
  onSend: () => void;
  canSend: boolean;
  busy: Busy;
  voiceOn: boolean;
  setVoiceOn: (v: boolean) => void;
  listening: boolean;
  onToggleListening: () => void;
  status: string | null;
}) {
  const blocked =
    status === "awaiting_approval"
      ? "This run is waiting on a human decision, not on another instruction. Decide it on the approval page first."
      : status === "running"
        ? "The agent is still working on this run."
        : null;

  return (
    <div className="sticky bottom-0 mt-1 flex flex-col gap-1.5 bg-[var(--surface)] pt-2 pb-3">
      <div className="flex items-end gap-2 rounded-xl bg-[var(--surface-container)] px-3 py-2.5 ghost-border">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSend();
            }
          }}
          rows={2}
          maxLength={8000}
          disabled={!canSend}
          aria-label="Message the agent"
          placeholder={
            blocked ?? "Tell the agent what to do. Enter to send, Shift+Enter for a new line."
          }
          className="min-w-0 flex-1 resize-none bg-transparent text-[0.8125rem] leading-relaxed text-[var(--on-surface)] outline-none placeholder:text-[var(--on-surface-muted)] disabled:cursor-not-allowed"
        />
        <button
          type="button"
          onClick={onToggleListening}
          disabled={!canSend || !canListen()}
          title={
            canListen()
              ? listening
                ? "Stop listening"
                : "Speak your instruction"
              : "This browser has no speech recognition"
          }
          aria-pressed={listening}
          aria-label="Dictate a message"
          className={cn(
            "shrink-0 rounded-lg px-2.5 py-2 text-sm transition-opacity ghost-border",
            !canSend || !canListen() ? "opacity-30" : "hover:opacity-80",
            listening && "animate-pulse",
          )}
          style={listening ? { background: "var(--danger, #b00)", color: "#fff" } : undefined}
        >
          {listening ? "\u25A0" : "\u{1F3A4}"}
        </button>
        <button
          type="button"
          onClick={() => {
            if (voiceOn) stopSpeaking();
            setVoiceOn(!voiceOn);
          }}
          disabled={!canSpeak()}
          title={canSpeak() ? "Read the agent's replies aloud" : "This browser cannot speak"}
          aria-pressed={voiceOn}
          aria-label="Speak replies"
          className={cn(
            "shrink-0 rounded-lg px-2.5 py-2 text-sm transition-opacity ghost-border",
            !canSpeak() ? "opacity-30" : "hover:opacity-80",
            voiceOn && "font-bold",
          )}
        >
          {voiceOn ? "\u{1F50A}" : "\u{1F507}"}
        </button>
        <button
          type="button"
          onClick={onSend}
          disabled={!canSend || !draft.trim()}
          className={cn(
            "shrink-0 rounded-lg px-4 py-2 text-xs font-bold text-white transition-opacity",
            !canSend || !draft.trim() ? "opacity-40" : "hover:opacity-90",
          )}
          style={{ background: "var(--primary)" }}
        >
          {busy === null ? "Send" : "Working…"}
        </button>
      </div>
      {blocked ? (
        <p className="text-[0.625rem] text-[var(--on-surface-muted)]">{blocked}</p>
      ) : (
        <p className="text-[0.625rem] text-[var(--on-surface-muted)]">
          A turn is one model round trip plus its tool calls — 30 seconds or more is
          normal.
        </p>
      )}
    </div>
  );
}

/* ── run sidebar ────────────────────────────────────────────────────────── */

function RunSidebar({
  runs,
  error,
  selectedId,
  onSelect,
  onNew,
  onRefresh,
}: {
  runs: RunSummary[] | null;
  error: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRefresh: () => void;
}) {
  return (
    <Panel className="flex min-h-0 flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-2 border-b border-[var(--outline)] px-4 py-2.5">
        <p className="label-caps">Runs</p>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onRefresh}
            className="rounded px-2 py-1 text-[0.625rem] font-bold text-[var(--on-surface-variant)] hover:bg-[var(--surface-high)]"
          >
            refresh
          </button>
          <button
            type="button"
            onClick={onNew}
            className="rounded px-2 py-1 text-[0.625rem] font-bold text-[var(--primary-bright)] hover:bg-[var(--surface-high)]"
          >
            + new
          </button>
        </div>
      </div>

      {error ? (
        <p className="border-b border-[var(--outline)] px-4 py-2 text-[0.6875rem] leading-relaxed text-[var(--error)]">
          {error}
        </p>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {runs === null && !error ? (
          <p className="px-4 py-6 text-xs text-[var(--on-surface-muted)]">Loading runs…</p>
        ) : null}
        {runs !== null && runs.length === 0 ? (
          <p className="px-4 py-6 text-xs text-[var(--on-surface-muted)]">
            No runs yet. Send a message to start one.
          </p>
        ) : null}
        <ul className="flex flex-col">
          {(runs ?? []).map((r) => (
            <li key={r.run_id}>
              <button
                type="button"
                onClick={() => onSelect(r.run_id)}
                aria-current={r.run_id === selectedId ? "true" : undefined}
                className={cn(
                  "relative block w-full border-b border-[var(--outline-variant)] px-4 py-3 text-left transition-colors",
                  r.run_id === selectedId
                    ? "bg-[var(--surface-high)]"
                    : "hover:bg-[var(--surface-high)]/50",
                )}
              >
                {r.run_id === selectedId ? (
                  <span
                    aria-hidden
                    className="absolute top-0 bottom-0 left-0 w-[2px]"
                    style={{ background: "var(--primary)" }}
                  />
                ) : null}
                <div className="flex items-center justify-between gap-2">
                  <Mono className="truncate font-semibold text-[var(--on-surface)]">
                    {r.run_id.slice(0, 12)}
                  </Mono>
                  <StatusBadge status={r.status} />
                </div>
                <p className="mt-1 line-clamp-2 text-[0.6875rem] leading-relaxed text-[var(--on-surface-muted)]">
                  {r.first_message || "no opening message recorded"}
                </p>
                <p className="mt-1 text-[0.625rem] text-[var(--on-surface-muted)]">
                  {r.updated_at || "no timestamp"}
                </p>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </Panel>
  );
}
