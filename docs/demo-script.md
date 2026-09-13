# Demo video script — 2:00 hard cap

Judging weights: technical execution 30%, **reliability & evaluation 25%**, usefulness 20%,
originality 15%, demo clarity 10%. Two minutes is short. Every second below is spent on
something a judge is scoring.

**Before recording, have these open in tabs, in this order:**

1. The chat surface (or a terminal with `ctrl-a-jr run` ready)
2. Gmail — the shop inbox, showing the three quote requests
3. The approval page
4. Stripe test dashboard — Invoices
5. Slack — `#ar-escalations`
6. A terminal with `ctrl-a-jr eval` ready to run

---

## 0:00 – 0:15 · What it is (do not skip, do not ramble)

> "An auto-detailing shop gets quote requests by email. This is an agent that reads them,
> prices them, invoices them, and answers the customer — across Gmail, Stripe and Slack.
> It cannot do any of that without a human approving the exact thing that's about to happen."

**On screen:** the inbox with three real customer emails.

## 0:15 – 0:50 · The agent actually working (the money shot)

Type, out loud:

> "What quote requests are waiting? Quote the biggest job and invoice it."

**Let the tool calls render.** Do not narrate every one. Say only:

> "It's reading its own inbox, reading the actual emails, and pricing each job."

Then land the point that separates this from every other demo today:

> "The model decides *what* the job is — interior detail, SUV, pet hair. It does **not** decide
> what it costs. The price comes from a menu in code. The invoice tool has **no amount
> parameter** — there's no field for a model-written number to arrive in."

## 0:50 – 1:15 · The gate (show, don't describe)

Agent stops. Approval card on screen.

> "It's stopped. This is the approval — and it's the rendered invoice, itemised, not a JSON
> blob. Approving a data structure isn't the same as approving a $1,023 invoice."

**Click Approve.** Then in three fast cuts:

- **Stripe** → the invoice exists, real amount
- **Gmail** → the reply went to the customer with a real payment link
- **Slack** → `#ar-escalations` has the escalation, because it's over $500

> "Three apps, one request, every write approved."

## 1:15 – 1:45 · Reliability — this is 25% of the score, give it the time

Run `ctrl-a-jr eval`. Show `verdict.json` on screen.

> "Every tool call goes through one chokepoint, which writes an append-only log the agent
> can't forge — attribution is stamped after the caller's payload. This scores that log."

Point at the actual lines:

> "Zero unapproved mutating actions. Zero payload divergences — what executed is what was
> approved, hashed before you see it and re-checked before it runs."

**Then point at the inconclusive one. This is the strongest thirty seconds in the video:**

> "And this check says **inconclusive** — not pass. Nothing's been denied yet, so there's no
> evidence the agent respects a refusal, and it refuses to claim there is. The denominators
> are the same idea: 'held in 3 of 3 runs that exercised it, 2 of 5 didn't.' A run that never
> touched the gate proves nothing about it, so it isn't counted."

## 1:45 – 2:00 · Close

> "The agent is a hand-rolled loop — no framework — and four dependencies. The gate isn't a
> prompt instruction, it's a chokepoint that can't be bypassed, and the proof isn't a claim in
> the README, it's an assertion over a log. Repo and README are linked."

---

## Rules for the recording

- **Do not apologise for anything.** No "this part is rough", no "we ran out of time".
- **Do not explain the architecture diagram.** Show it working instead.
- **If a live run fails mid-record, keep the take going and say what the log shows.** A real
  failure handled visibly is better than a third retake — reliability is the criterion.
- Cut ruthlessly. If it's 2:10, drop the closing line, not the inconclusive explanation.

## The three sentences to land, if nothing else

1. The model classifies; the code prices. There is no amount parameter.
2. Nothing mutates without a human approving the rendered artifact.
3. The reliability numbers say "inconclusive" where the evidence is missing — which is why
   the ones that say "pass" are worth something.
