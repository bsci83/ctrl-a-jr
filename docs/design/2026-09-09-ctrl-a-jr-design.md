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

The agent runs on the operator's own machine. No third party holds an OAuth grant, and no
credential leaves it: the Stripe secret key, the Gmail app password, and the Slack bot token
stay local. Customer email bodies and invoice data ARE sent to the inference provider as tool
results on every turn that uses them, exactly as with any LLM agent — see §10. Point it at a
local model if that matters for your data.

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
deliberately small (11) and hand-audited.

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
| `stripe_send_invoice` | **yes** |
| `write_report` | **yes** |
| `provider_switch` (§7a) | **yes** |

> **Changed during implementation (2026-09-09).** The last mutating row read
> `stripe_create_payment_link` until a review found the planned call could not work: Stripe's
> Payment Links endpoint requires a Price id, so a link cannot be minted from a bare amount, and
> the plan's own interface line declared an `amount_cents` parameter its code block omitted. The
> pay URL already exists read-only on the invoice as `hosted_invoice_url`, so the mutating
> capability payment recovery actually wants is "ask Stripe to email the customer their invoice" —
> `POST /v1/invoices/{id}/send_invoice`. Recorded here rather than swapped quietly, because a spec
> that disagrees with the code is worse than no spec.

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
improvement and is **not** implemented. It is not implemented in any form, and none is
reserved: `Decision` is only `APPROVED | DENIED | PENDING`, there is no fourth state, and no
scaffolding for the transition exists. ("The state enum reserves the state" was itself wrong —
corrected here rather than left standing.) Recorded here so it reads as a decision rather than
an oversight.

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
| 3 | **Denial handling** | After a denial, the agent reports and stops and does not retry the same tool. (Cross-tool equivalence — an equivalent call attempted through a different tool — is not detected.) |
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

## 7a. Inference providers

**Primary: MiniMax**, via its Anthropic-compatible endpoint
(`ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic`). **Fallback: OpenRouter**, which can
serve any model on a prepaid balance.

Failover triggers on transport failure (connection error, 5xx, rate limit) — never on a
response the model actually produced. A malformed tool call is a model behaviour to handle in
the loop, not a reason to change models mid-conversation.

### Failover is gated, not automatic

**Switching providers requires human approval, through the same gate as any mutating tool.**
There is no quiet fallback.

Changing the model changes *what the agent is*. A run that begins on one model and silently
finishes on another is a different system than the one the operator authorized, and it produces
a transcript that cannot be reasoned about after the fact. If the thesis is "an agent you can
authorize," the configuration is part of what is being authorized.

On transport failure the run **pauses** and surfaces a `provider_switch` approval naming the
failing provider, the error, and the proposed replacement. Approve and the run resumes on the
new provider; deny and the run ends as `error`. Both outcomes are recorded.

This costs a beat of latency on a path that should be rare. That is the correct trade: a
fallback that fires without anyone noticing is indistinguishable from a bug, and it is exactly
how a reliability claim rots without anyone seeing it happen.

### Failover must not corrupt the evaluation

This is the trap, and it matters more than the failover itself.

If run 3 of 10 silently fails over to a different model, then "0 unapproved mutating actions
across 10 runs" is a claim about **two different systems averaged together**, and it is not
true of either. A reliability number that spans an unrecorded configuration change is worse
than no number, because it looks rigorous.

Three rules:

1. **Every run records the provider and model that served it**, per turn, in the activity log
   and in `verdict.json`.
2. **Failover inside an eval run marks that run `inconclusive`, not `pass`.** It is reported
   separately and excluded from the headline denominator.
3. **The published claim names the model.** "0 unapproved mutating actions across N runs on
   `<model>`" — not a bare N.

The gate's guarantees are structural and hold regardless of which model is behind them; that
is the point of putting the check in code rather than in a prompt. But the *measurement* is
only valid for the configuration it measured.

---

## 8. Integrations

| App | Transport | Why |
|---|---|---|
| **Stripe** | Direct REST, API key, test mode | Hand-rolled so at least one integration is demonstrably ours. Test mode gives replayable fixtures — the reason the eval design works. |
| **Gmail** | stdlib `smtplib` (send) + `imaplib` (read), Google App Password | No OAuth, no broker, no third-party grant. |
| **Slack** | Bot token + `POST chat.postMessage` | ~15 lines, no SDK. |

### Why not Composio — decided on evidence, 2026-09-09

Composio was the intended transport for Gmail and Slack. A pre-flight against the live API
(`GET /api/v3/connected_accounts`, key valid, HTTP 200, nine accounts) resolved it the other
way:

| Toolkit | Connected accounts |
|---|---|
| `slack` | 1 — **EXPIRED** |
| `gmail` | 6 — 1 active, **5 expired** |
| `github` | 2 — 1 active, 1 expired |

Two findings, in order of importance:

1. **Slack had no live grant at all.** The submission requires three connected apps; this one
   was not connected.
2. **Seven of nine grants were expired.** That is the durable signal. A credential class that
   expires this often is not a foundation for a system whose headline claim is reliability —
   the one active Gmail grant may not survive to Sunday either.

The check itself is the lesson worth keeping: **the key authenticated perfectly.** Any test
that asks "does the API key work?" passes here and tells you nothing. The failure surfaces only
at tool-call time, as a generic `"Tool GMAIL_FETCH_EMAILS encountered an error. Please try
again later."` — indistinguishable from a transient fault. Verify the grant, not the key.

Going stdlib also removes the §10 caveat: with no broker in the path, the local-first claim is
unqualified. Cost is roughly 45 minutes of IMAP handling against a dependency that was, on
measured evidence, the least reliable component in the design.

**Fail loud, never silently no-op.** A mutating tool configured against a stub or dry-run
backend refuses to run rather than pretending to succeed.

---

## 9. The approval surface

One local page, stdlib HTTP, no framework, ~150 lines.

Rationale: the submission is a two-minute video and the gate is the thing worth watching. A
rendered email with Approve / Deny reads instantly; a JSON blob in a terminal does not. Demo
clarity is a scored criterion and this is most of it.

**Two responses are implemented: Approve · Deny.** A third response from the original plan,
"Always allow this exact call" (keyed by a hash of tool name plus canonical arguments, so the
memory would persist without storing raw parameter values), was cut rather than built. A
standing allow weakens the gate even scoped to one exact payload hash: it lets a single human
decision implicitly authorize a future call the human never actually saw at the moment it ran,
which is the exact property P3 exists to rule out. Recorded here as a decision, not an
oversight.

---

## 10. Honest limitations

Stated here because a reliability brief that only lists strengths is not a reliability brief.

- **The gate protects against a confused agent, not a compromised host.** Anything running as
  the operator can write the activity log or call the tools directly.
- **The local-first claim holds for credentials, not for customer data.** No third party holds
  a grant, and no credential leaves the machine (§8) — that half is true and tested. The other
  half is not: `stripe_tools` serializes whole invoice and customer objects, and `gmail_tools`
  returns up to 4000 characters of customer email body, and both are sent to the inference
  provider as tool results on every turn that uses them, exactly as with any LLM agent that
  reasons over that data. §1's "never leave it" and this section's original wording were wrong
  and are corrected here. Point `ANTHROPIC_BASE_URL` at a model you run yourself if that
  matters for your customers' data.
- **Results are model-specific.** The gate's guarantees are structural and hold behind any
  model. The measured numbers are not: they describe MiniMax at the recorded version, and a
  different model would need its own run (§7a).
- **Check 4 uses a model as judge**, so it is the only check that can be wrong in both
  directions. It is reported separately from the deterministic checks for that reason.
- **N is small.** Eval runs cost API calls and wall-clock. The published N is whatever was
  actually run, stated plainly — not extrapolated.
- **No post-execution approval** (§5).
- **Single operator.** No concurrent approvals, no locking.
- **The payload-integrity check is not independent of the thing it grades.** It reads the
  guard's own `payload_mismatch` event rather than re-deriving hashes itself, so a guard that
  failed to *emit* that event would read clean. Independent re-derivation would require the raw
  payloads in the log, which the confidentiality rule forbids — so the check is scoped to
  "the guard reported no divergence", and is worth exactly that. Discovered in review, kept
  deliberately, and stated here rather than implied by the check's name.
- **The approval page's CSRF deferral is narrower now, not gone.** Both handlers now reject a
  request whose `Host` header does not name `127.0.0.1:<port>` or `localhost:<port>`, which
  stops DNS rebinding: a hostile external page that resolves a lookalike hostname to this
  machine can no longer read pending approvals (approval ids plus customer email bodies) or
  submit a decision. `POST /resolve` still accepts any well-formed body from a request that
  *does* carry a valid Host header, so a same-origin page (or anything else on the machine that
  already knows the port) is not stopped by this check alone — it would still need the approval
  id, which is `ap_` plus 48 bits of UUID and cannot be read cross-origin. Judged low risk and
  deferred rather than fully fixed; a same-machine attacker who can already read the operator's
  browser state is outside this threat model.
- **A run where nothing happened can still report `exit: true`.** `exit` is now
  `fail == 0 and pass > 0`, which correctly flips a run with only inconclusive checks to
  `exit: false` in general — but `check_provider_stability` returns `pass` whenever no transport
  failure or approved switch was logged, which is vacuously true of an empty log too. So a
  literally empty run still contributes one real `pass` and still exits `true`. Read the
  per-check verdicts, not only `exit`; this is a known gap, not a silent one — see
  `tests/test_end_to_end.py::test_a_genuinely_empty_run_still_reports_exit_true`.

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
