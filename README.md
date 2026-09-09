# ctrl-a JR

Failed payments pile up because recovering them means sending money-related email to a real
customer, and nobody wants an autonomous agent doing that unsupervised. ctrl-a JR is a small,
local-first agent that does the research — pulls the failed invoice from Stripe, reads what the
customer already said over email — and drafts the recovery email, but it cannot send anything,
post to Slack, or write a report without a human clicking Approve on the actual artifact first.

## What it does

It looks up a business's most overdue open Stripe invoice, reads the customer's email thread for
context, and drafts a recovery email grounded in the real amount and due date on the invoice —
never an estimate. Every action that changes something in the world (sending the email, posting
to Slack, writing the run report, even switching which model is answering) stops at a local
approval page and waits for a person to approve or deny it before it executes.

## The authorization model

Everything here exists to make one property true: **nothing mutates without a human seeing
exactly what will happen and saying yes.**

- **One chokepoint.** Every tool call — read or write — passes through a single `Guard`. There
  is no bypass flag, no trusted path, no per-tool exemption. The only way to create a hole is to
  mislabel a tool as safe, which is why the tool list is small (11 tools) and reviewed by hand.
- **You approve the artifact, not the arguments.** For a send, the approval page renders the
  actual email that would go out to the actual recipient — not a JSON blob of parameters.
  Approving a data structure is not the same as approving an email, so the page never asks you
  to.
- **What you approve is what runs, byte for byte.** The moment you approve something, the agent
  hashes the exact payload. Right before it executes, it re-hashes and compares. If they don't
  match, the call is aborted and logged. Without this, an agent could show you one email and
  send a different one and the approval record would still look clean.
- **A denial is visible, not silent.** If you deny an action, the agent gets a real error back —
  not a quiet no-op — and is instructed to report the denial and stop, not retry the same thing
  through a different tool.

Switching which model is answering (say, after MiniMax goes down and the run wants to fail over
to OpenRouter) goes through the exact same gate as sending an email. A run that quietly finishes
on a different model than it started on is a different system than the one you authorized, so
that switch is never automatic — it pauses the run and asks.

## Quickstart

```bash
pip install -e ".[dev]"
cp .env.example .env   # fill in Stripe test key, Gmail app password, Slack bot token, API keys
ctrl-a-jr run
```

`run` opens a local approvals page in your browser and starts working. Every mutating action —
send, post, write, or provider switch — appears there for you to approve or deny before it
happens. `ctrl-a-jr eval` scores whatever is in the activity log against the deterministic
checks below and writes `verdict.json`.

## How the evals work

The activity log (`~/.ctrl-a/jr/activity.jsonl` by default) is the only input to the eval
harness — it's the same evidence stream the gate itself produces, so reliability here is a
byproduct of the gate being built correctly rather than a separate measurement effort. Three
deterministic checks currently run, each with a right answer computable from the log:

1. **Gate integrity** — every executed mutating call cites an approval that was granted, for
   that specific tool, before the call, exactly once.
2. **Payload integrity** — the payload that executed matches the payload that was approved; zero
   `payload_mismatch` events.
3. **Denial handling** — after a denial, the agent does not retry the same call or attempt an
   equivalent one through another tool.

**No end-to-end eval run against live fixtures has happened yet.** The three checks above are
exercised by the unit and integration test suite (104 tests, `pytest` + `ruff` both green as of
this writing) against scripted fakes, which is enough to say the gate mechanism itself works.
It is not enough to publish a reliability number like "N runs, 0 unapproved actions" — that
claim needs real runs against seeded Stripe test-mode fixtures, which have not been executed.
Two more checks are designed but not implemented: grounding (does the drafted email's amount and
date match the Stripe invoice, judged by a model) and recovery outcome (N of M invoices actually
recovered end to end). Both need live fixtures rather than unit tests to mean anything, so they
are follow-on work, not something claimed here.

## Integrations

Stripe is hand-rolled REST against a **test-mode key only** — a live key is refused at startup,
because this agent drafts email about real money to real customers and the eval design depends
on replayable fixtures. Gmail and Slack are intentionally boring: Gmail uses stdlib `smtplib`
and `imaplib` with a Google App Password, and Slack uses a bot token and one `POST` — no OAuth
broker, no third-party grant, no SDK. That's also what makes the local-first claim mean
something: nothing but Stripe, Google, and Slack ever sees a credential.

## Limitations

Stated plainly, because a project that only lists strengths hasn't been reviewed honestly:

- **The gate protects against a confused agent, not a compromised host.** Anything running as
  the operator can write the activity log or call the tools directly.
- **The local-first claim is unqualified** on the shipped design: no third party holds a grant,
  and credentials plus customer data stay on the operator's machine.
- **Results are model-specific.** The gate's guarantees are structural and hold behind any
  model. Measured numbers would not be — they'd describe one model at one recorded version, and
  a different model needs its own run.
- **The grounding check uses a model as judge**, so it would be the only check that can be wrong
  in both directions. It's reported separately from the deterministic checks for that reason.
- **N is small, whatever it ends up being.** Eval runs cost API calls and wall-clock. The
  published N should be whatever was actually run, stated plainly — never extrapolated.
- **No post-execution approval.** Approving a result before it returns to the model is a real
  improvement and is not implemented; the state enum reserves the state, the transition doesn't
  exist yet.
- **Single operator.** No concurrent approvals, no locking.

## License

MIT — see `LICENSE`.
