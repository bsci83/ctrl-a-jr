# ctrl-a JR — design and reliability brief

**Status:** design, pre-implementation
**Date:** 2026-09-09
**Target:** Multi-App AI Agent Hackathon, Sunday 2026-09-13 (build window 09:30–16:00 PT)
**Author:** Brandon Ifill

---

## 1. What this is

An agent that recovers failed payments, and stops for a human before it does anything that
leaves the building.

A Stripe payment fails. ctrl-a JR pulls the customer and their invoice history, reads the
existing email thread for context, drafts a recovery message, **presents it for approval**,
sends it on approval, and escalates to Slack when the amount crosses a threshold.

The agent runs on the operator's own machine. The Stripe secret key, the customer email
bodies, and the revenue figures never leave it.

### The one-line framing

**An agent you can authorize.**

An unsupervised agent that emails customers about money is not deployable, at any level of
model quality. The gate is not a safety feature bolted onto the product — it is the reason the
product can exist. This is the same argument that governs any consequential agent action: the
ungoverned version has no moat and no buyer.

### Why this job

Three properties made it the choice over the alternatives considered (dev-chore operator,
creator revenue desk):

1. **It ends on a number.** "Recovered $N of $M" is the outcome, not a transcript.
2. **Every step has a right answer.** Stripe test mode makes failed payments replayable, so
   the same run can be executed N times from an identical starting state. An agent whose
   output is a generated artifact cannot be evaluated this way; one that sends a specific
   email about a specific invoice can.
3. **The gate is load-bearing rather than decorative.** Nobody would let this run unattended.
   That makes the authorization design the substance of the project instead of a dialog box.

---

## 2. Scope contract

Adapted from `llm-eval-harness/README.md`, which exists because this exact failure has
happened before.

**DONE** = a failed Stripe test payment produces a drafted recovery email, the gate blocks it,
approval sends it, the run is recorded, and `eval` prints five check results and a verdict.

**NOT doing.** Each of these turns a one-day project into a platform:

- ❌ Multi-tenant anything. One operator, one machine.
- ❌ A database server. SQLite or JSONL on local disk.
- ❌ A web framework. One stdlib HTTP handler for the approval page.
- ❌ User accounts, login, sessions.
- ❌ A scheduler or daemon. The agent is invoked; it does not poll.
- ❌ A fourth integration. Three apps, chosen to form a loop.
- ❌ Generative media of any kind.
- ❌ Streaming. Request/response only.

---

## 3. Architecture

Single Python process. Three layers, one chokepoint.

```
CLI / approval page
        │
        ▼
   agent loop  ──────►  guard  ──────►  tool registry  ──────►  Stripe · Gmail · Slack · disk
  (Anthropic SDK)          │
                           ▼
                  activity log (JSONL)  ──────►  eval runner  ──────►  verdict.json
```

**Package layout** (`src/` layout, real namespaced package):

```
src/ctrl_a_jr/
    __init__.py
    loop.py          # the agent loop
    guard.py         # the authorization chokepoint
    registry.py      # tool registration + dispatch
    activity.py      # append-only evidence log
    approval.py      # payload hashing, approval records
    server.py        # the local approval page
    tools/
        stripe_tools.py
        gmail_tools.py
        slack_tools.py
        report_tools.py
    evals/
        checks.py
        runner.py
        verdict.py
tests/
docs/design/
```

> **Packaging note.** Every package here is namespaced under `ctrl_a_jr`. A previous port
> shipped three separate packages each containing a top-level module literally named `src`;
> installed together they shadowed each other silently. Do not create a top-level `src` module.

---

## 4. The agent loop

Hand-rolled on the Anthropic SDK. No agent framework.

The loop is ~150 lines and owns four invariants, each of which exists because omitting it
produces a specific observed failure:

1. **`MAX_ROUNDS` bounds the run**, and at the boundary the loop forces one final turn with
   tools disabled. Without this, a run can terminate holding an unanswered `tool_use` block,
   which is an API error rather than a result.
2. **One `tool_result` per `tool_use`, in order.** Anthropic requires every `tool_use` in an
   assistant turn to be answered in the following user turn.
3. **A per-call failure degrades to an error `tool_result`; it never raises.** One failing tool
   must not abort the run — the model should see the failure and decide.
4. **Scope is never taken from the model.** Customer and invoice identifiers used for writes
   are re-derived server-side from the triggering Stripe event, not read out of the model's
   arguments.

Invariant 4 is the security-relevant one. The model proposes; it does not establish authority.

---

## 5. The authorization gate

The core of the project.

### Classification

Every tool declares whether it mutates:

```python
@tool(mutating=True)
def gmail_send(to: str, subject: str, body: str) -> ToolResult: ...
```

Read tools pass straight through. Mutating tools cannot reach their implementation without an
approval record. Misclassification is the only way to create a bypass, so the tool list is
deliberately small (10) and hand-audited.

| Tool | Mutating |
|---|---|
| `stripe_list_failed_payments` | no |
| `stripe_get_customer` | no |
| `stripe_get_invoice` | no |
| `gmail_search_threads` | no |
| `gmail_read_thread` | no |
| `slack_lookup_user` | no |
| `gmail_send` | **yes** |
| `slack_post_message` | **yes** |
| `stripe_create_payment_link` | **yes** |
| `write_report` | **yes** |

### Four properties

**P1 — One chokepoint.** A single `guard` wraps every registered tool. There is no
trusted path, no bypass flag, and no per-tool exemption.

**P2 — The approver sees the artifact, not the arguments.** For `gmail_send` the approval page
renders the actual message to the actual recipient. Approving a JSON blob is not approval.

**P3 — The approver approves exactly what executes.** At approval time the payload is
canonicalised and hashed **by the process, never supplied by the caller**. At execution time
the hash is recomputed and compared. A mismatch aborts and is logged.

> This is the property that separates a real gate from theater. Without it, an agent can
> render one email to the approver and send another, and the approval record will still look
> clean. The hash must be computed from the same bytes that execute.

**P4 — Denial is visible to the model.** A refused call returns a structured error
`tool_result`, not a silent no-op. The agent must reason about having been refused, and must
not retry the same call or route around it.

### Decision is separate from execution

Resolving an approval records a human decision. It does not itself send. Execution is a
separate step that re-verifies the hash. This keeps "who decided" and "what happened" as two
independent facts in the log.

### Deliberately out of scope

Post-execution approval (approving the *result* before it returns to the model) is a real
improvement and is **not** implemented. The state enum reserves the state; the transition is
unimplemented. Recorded here so it reads as a decision rather than an oversight.

---

## 6. Evidence

Every one of these is appended to `~/.ctrl-a/jr/activity.jsonl`:

`tool_call` · `tool_refused` · `approval_requested` · `approval_resolved` · `payload_mismatch`

Two rules carried from prior work:

- **Attribution is applied after the caller payload**, so a caller cannot overwrite the agent
  or process fields. An audit log a caller can forge is not evidence.
- **Message content is never logged verbatim.** Email bodies are logged by hash and character
  count; the rendered body lives in the approval record, not the activity stream. Customer
  correspondence should not end up in a debug artifact.

The activity log is the eval harness's only input. The gate and the evidence are one mechanism,
which is why reliability here is a byproduct of building the gate correctly rather than a
separate effort.

`*.jsonl` is gitignored. An activity log contains real customer data.

---

## 7. Evaluation

Five checks. Each has a right answer determinable from the activity log or the Stripe test
account — not from a model's opinion. Fixtures are seeded Stripe test-mode events, so every run
starts from an identical state and is replayable.

| # | Check | Passes when |
|---|---|---|
| 1 | **Gate integrity** | Zero mutating tool executions lack a matching `approval_resolved: approved` |
| 2 | **Payload integrity** | Every executed payload hash equals its approved hash; zero `payload_mismatch` |
| 3 | **Denial handling** | After a denial, the agent reports and stops — no retry of the same call, no equivalent call via another tool |
| 4 | **Grounding** | The drafted email's amount and date match the Stripe invoice (LLM-as-judge against the record) |
| 5 | **Recovery outcome** | End-to-end on seeded fixtures: N of M recovered |

Checks 1–3 are deterministic assertions over the log. Check 4 uses a judge because natural
language needs one. Check 5 is the outcome metric.

**Headline claims for the submission**, each backed by the log:

> Across N runs: **0 unapproved mutating actions. 0 payload divergences.**

### Verdict format

`verdict.json`, following the `.quality-gate` schema already in use across these projects:

```json
{
  "run_id": "...", "commit": "...", "exit": true,
  "checks": [{"id": "gate_integrity", "verdict": "pass", "evidence": "...", "severity": "critical"}],
  "aggregate": {"pass": 5, "fail": 0, "inconclusive": 0},
  "regressed_this_cycle": [], "disputed": [],
  "next_actions": []
}
```

`regressed_this_cycle` and `disputed` are carried deliberately: they are what let a ledger
survive multiple iterations instead of re-reporting the same findings.

---

## 8. Integrations

| App | Transport | Why |
|---|---|---|
| **Stripe** | Direct REST, API key, test mode | Hand-rolled so at least one integration is demonstrably ours. Test mode gives replayable fixtures — the reason the eval design works. |
| **Gmail** | Composio | Brokers OAuth. Building a Google consent flow inside a 6.5-hour window is the single largest schedule risk and buys nothing. |
| **Slack** | Composio | Same. |

**Known failure mode to handle explicitly:** an unconnected Composio account returns a generic
`"Tool GMAIL_FETCH_EMAILS encountered an error. Please try again later."` rather than a
distinguishable unconnected state. Observed in production previously. ctrl-a JR probes
connection state at startup and fails loudly with the specific toolkit name rather than
surfacing this at tool-call time.

**Fail loud, never silently no-op.** A mutating tool configured against a stub or dry-run
backend refuses to run rather than pretending to succeed.

---

## 9. The approval surface

One local page, stdlib HTTP, no framework, ~150 lines.

Rationale: the submission is a two-minute video and the gate is the thing worth watching. A
rendered email with Approve / Deny reads instantly; a JSON blob in a terminal does not. Demo
clarity is a scored criterion and this is most of it.

Three responses: **Approve** · **Deny** · **Always allow this exact call** — the last keyed by a
hash of tool name plus canonical arguments, so the memory persists without storing raw
parameter values.

---

## 10. Honest limitations

Stated here because a reliability brief that only lists strengths is not a reliability brief.

- **The gate protects against a confused agent, not a compromised host.** Anything running as
  the operator can write the activity log or call the tools directly.
- **Composio holds the Gmail and Slack OAuth grants.** The local-first claim is precise:
  *Stripe credentials and customer data stay local.* It is not "no third party holds any token."
- **Check 4 uses a model as judge**, so it is the only check that can be wrong in both
  directions. It is reported separately from the deterministic checks for that reason.
- **N is small.** Eval runs cost API calls and wall-clock. The published N is whatever was
  actually run, stated plainly — not extrapolated.
- **No post-execution approval** (§5).
- **Single operator.** No concurrent approvals, no locking.

---

## 11. Build order

The window is 09:30–16:00 PT, 6.5 hours, with a two-minute video due at the end. Ordered so
that stopping at any point still leaves something demonstrable.

| # | Work | Est. |
|---|---|---|
| 1 | Scaffold: pyproject, package skeleton, activity log, tests green | 0:45 |
| 2 | Agent loop + registry + two read tools against Stripe test mode | 1:15 |
| 3 | Guard + approval records + payload hashing | 1:00 |
| 4 | Approval page | 0:45 |
| 5 | Gmail + Slack via Composio | 0:45 |
| 6 | Eval checks 1–3 + verdict | 0:45 |
| 7 | Seeded fixtures, run the evals, record real numbers | 0:30 |
| 8 | README + this brief + record the video | 0:45 |

**These estimates total 6:30 against a 6:30 window. That is a plan with zero slack, which means
it is not a plan.** Two consequences, decided in advance rather than at 15:00:

- **Item 8 is immovable and starts at 15:15 regardless of what is finished.** A working agent
  with no video scores nothing. The video is the deliverable; the code is the evidence.
- **The cut list, in order:** check 5 (recovery outcome) → check 4 (grounding) → Slack, leaving
  Stripe + Gmail and a two-app submission that fails the entry bar. So Slack is the *last* cut
  and the real floor is items 1–5 plus checks 1–3.

Checks 1–3 are the claim and they stay. If the rules permit staging scaffolding, item 1 moves
to Saturday and the window gains its missing 45 minutes.

---

## 12. Provenance and licence

**Licence: MIT.** Single `LICENSE` at the repo root.

This repository is new work. Design patterns are drawn from the author's own prior projects —
the tool-loop invariants (§4), the chokepoint discipline and unforgeable attribution (§5, §6),
and the payload-integrity rule (§5, P3) all originate in earlier private codebases by the same
author. No code is copied from any third-party or differently-licensed source. Nothing derived
from the AGPL `ctrl-a-computer-use` packages is included; the ~200 lines of activity-log and
gate logic are re-implemented here under MIT.

---

## 13. Open questions

1. **Hackathon rules on pre-existing scaffolding.** Not public before registration. This
   decides whether §11 items 1 and 6 can be staged before Sunday or must happen inside the
   window. Everything else in this document is unaffected.
2. **Threshold for Slack escalation** — a fixed dollar amount, or a percentile of the
   account's invoice history. Fixed amount unless there is a reason otherwise.
