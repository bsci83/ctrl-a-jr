import Link from "next/link";

import { Chat } from "@/components/chat";

export const dynamic = "force-dynamic";

/**
 * The console — the agent as something you can talk to.
 *
 * The three pre-existing surfaces (the replayed workspace, Channels, Verdict)
 * still read the frozen `evidence.json` and still work; they moved down a level
 * to /evidence and are linked from the nav. This page is live: every turn here
 * is a real run against the deployed agent.
 *
 * A server component with a single client child. The page itself fetches
 * nothing — the token lives behind /api/agent/* and the conversation is
 * inherently a client-side loop (send, poll, redraw).
 */
export default function ConsolePage() {
  return (
    <div className="pt-6">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-black tracking-tight text-[var(--on-surface)]">
            Console
          </h1>
          <p className="mt-0.5 max-w-[75ch] text-sm text-[var(--on-surface-variant)]">
            Talk to the live agent. It reads the shop&rsquo;s inbox, prices from the
            code-owned menu and drafts the reply — and it stops at the gate before
            anything leaves the building.
          </p>
        </div>
        <Link
          href="/evidence"
          className="rounded-lg px-3 py-1.5 text-xs font-bold text-[var(--primary-bright)] hover:bg-[var(--surface-high)]"
        >
          Replay a recorded run instead →
        </Link>
      </div>

      <Chat />
    </div>
  );
}
