"""Conversational run endpoints — the door a human talks to the agent through.

Until this file existed, the agent's task was a constant in `cli.py`: it ran one
predetermined job from a terminal and exited. Nobody could ask it anything.

  POST /api/runs                 bearer  start a run from a human message
  GET  /api/runs                 bearer  recent runs, for a list view
  GET  /api/runs/<id>            bearer  one run's conversation + pending approval
  POST /api/runs/<id>/messages   bearer  add a human turn to a finished run
  POST /api/runs/<id>/advance    bearer  continue after an approval was decided
  POST /api/slack/events         Slack v0 signature  @mention / DM starts a run

What this file is NOT allowed to be
-----------------------------------
A way around the gate. Every tool still executes inside `Guard.dispatch` via
`resume.advance`, which suspends on anything mutating; nothing here calls a tool,
approves anything, or writes a decision it was not handed. `POST /advance` does
not carry a decision — it copies one the human already made on the approval
surface onto the run row, through `RunStore.record_decision`, which is
conditional on the run still awaiting THAT approval and on no decision already
being recorded. At-most-once execution is `RunStore.claim_pending`, a database
compare-and-set, and there is deliberately no second mechanism here: a Python
"have we run this yet" check would lose the race it exists to win.

Serverless time budget
----------------------
One request drives ONE `advance`, which is one model turn plus its tool calls.
It never loops until the run is finished. A run that needs another turn comes
back `running`/`awaiting_approval` and the caller calls again — so the worst case
for a single invocation is one model round trip, not a whole task.

Errors never carry exception text from storage or a provider. A failed Turso call
raises with the request it attempted, and the request carries the Authorization
header.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from . import routes, slack
from .agent import AgentBuild, MissingCredential, build_agent
from .http import Response, bearer_ok, json_response, not_found
from .store import Record, StoreUnavailable

# Imported through `.agent`, which puts `src/` on sys.path first.
from ctrl_a_jr.resume import advance  # noqa: E402
from ctrl_a_jr.runstate import (  # noqa: E402
    STATUS_AWAITING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    RunState,
    RunStore,
    StorageError,
)

# Tests inject these. Production leaves them None and builds from the
# environment, the same convention `routes.STORE` already uses.
RUN_STORE: RunStore | None = None
AGENT: AgentBuild | None = None

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# A human turn, not a document upload. Long enough for a pasted email, short
# enough that it cannot be used to blow out the model's context or the row.
MAX_MESSAGE = 8000
LIST_LIMIT = 25
# Tool output echoed into the conversation view. The full text lives in the
# stored history; a UI list does not need it and a response does not want it.
OUTPUT_PREVIEW = 500

# Slack bookkeeping. Both tables live beside `jr_runs` in the same database, so
# a dedupe claim survives the cold start that a process-local set would not.
SLACK_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS jr_slack_events (
      event_id   TEXT PRIMARY KEY,
      thread_key TEXT,
      run_id     TEXT,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jr_slack_threads (
      thread_key TEXT PRIMARY KEY,
      run_id     TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
)

STORAGE_DOWN = "run state is unavailable"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clip(text: str, limit: int = OUTPUT_PREVIEW) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

def _run_store() -> RunStore:
    return RUN_STORE if RUN_STORE is not None else RunStore.from_env()


def _agent() -> AgentBuild:
    return AGENT if AGENT is not None else build_agent()


def _agent_or_error() -> tuple[AgentBuild | None, Response | None]:
    """503 naming the unset variable — never a half-built registry."""
    try:
        return _agent(), None
    except MissingCredential as exc:
        # Only variable NAMES reach this string; `agent.MissingCredential` is
        # constructed from names alone.
        return None, json_response(503, {"error": str(exc)})


def _store_or_error() -> tuple[RunStore | None, Response | None]:
    try:
        return _run_store(), None
    except StorageError as exc:
        # from_env's message names TURSO_DATABASE_URL / TURSO_AUTH_TOKEN and
        # contains no value of either.
        return None, json_response(503, {"error": str(exc)})


# --------------------------------------------------------------------------
# conversation rendering
# --------------------------------------------------------------------------

def conversation(state: RunState) -> list[dict]:
    """The run as an ordered list of turns a UI can draw.

    Derived from the persisted history, not from raw model JSON: a caller that
    had to understand Anthropic content blocks to render a chat would end up
    reimplementing this, differently, per client.

    Kinds: `human` / `assistant` (with `text`), `tool_call` (with `tool`, `id`,
    `status` one of pending|ok|failed, and a clipped `output`). Order is emission
    order, so a tool call appears where the model made it.
    """
    turns: list[dict] = []
    position: dict[str, int] = {}

    for message in state.messages:
        if not isinstance(message, dict):
            continue
        role = "human" if message.get("role") == "user" else "assistant"
        content = message.get("content")
        if isinstance(content, str):
            if content:
                turns.append({"kind": role, "text": content})
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = str(block.get("text") or "")
                if text:
                    turns.append({"kind": "assistant", "text": text})
            elif kind == "tool_use":
                use_id = str(block.get("id") or "")
                turns.append({
                    "kind": "tool_call",
                    "tool": str(block.get("name") or ""),
                    "id": use_id,
                    # Answered below if a tool_result for it was persisted. A call
                    # still `pending` is the one the run suspended on.
                    "status": "pending",
                })
                position[use_id] = len(turns) - 1
            elif kind == "tool_result":
                index = position.get(str(block.get("tool_use_id") or ""))
                if index is None:
                    continue
                turns[index]["status"] = "failed" if block.get("is_error") else "ok"
                turns[index]["output"] = _clip(str(block.get("content") or ""))

    # The model's closing text is stored in `result_text`, NOT in `messages`
    # (see `resume._run_rounds`), so a conversation built from messages alone
    # would end one turn short — missing the answer the human asked for.
    if state.result_text:
        turns.append({"kind": "assistant", "text": state.result_text})
    return turns


def _pending_payload(state: RunState, headers) -> dict | None:
    pending = state.pending
    if pending is None or state.status != STATUS_AWAITING:
        return None
    payload = {
        "kind": "approval",
        "approval_id": pending.approval_id,
        "tool": pending.tool,
        # The ARTIFACT the human must judge, not the arguments (spec §5 P2).
        "rendered": pending.rendered,
        "payload_hash": pending.payload_hash,
        "decision": state.decision or "pending",
    }
    try:
        payload["approve_url"] = routes.approve_url(pending.approval_id, headers)
    except LookupError:
        # CTRLA_JR_PUSH_TOKEN unset. Unreachable behind `bearer_ok`, which fails
        # closed on the same variable; the card is still returned without a link
        # rather than the whole response failing.
        pass
    return payload


def _register_approval(state: RunState, headers) -> str | None:
    """Publish the suspended call to the approval surface a human already uses.

    Without this a run started from chat would suspend on a ticket that
    /api/approve/<id>, /api/decide and the Slack buttons know nothing about —
    the gate would hold and nobody could ever answer it.

    INSERT OR IGNORE inside `put`, so a retried request cannot reset an approval
    a human has already decided back to pending.
    """
    pending = state.pending
    if pending is None:
        return None
    record = Record(
        approval_id=pending.approval_id,
        tool=pending.tool,
        payload_hash=pending.payload_hash,
        rendered_artifact=pending.rendered or f"{pending.tool} (no rendering available)",
        created_at=_now(),
        # No args: this row is read by the display surfaces, and the arguments
        # that actually execute are the ones in `jr_runs`, hashed and compared
        # there. A second copy is a second thing that can disagree.
        args_json=None,
    )
    try:
        routes._store().put(record)
    except StoreUnavailable:
        # The run is already suspended and persisted; the tool has not run. The
        # only loss is that the human cannot answer yet, so say so rather than
        # failing a request whose work succeeded.
        return "approval surface unavailable; the run is suspended and nothing executed"
    return None


def _bridge_decision(store: RunStore, state: RunState) -> None:
    """Copy a decision made on the approval surface onto the run row.

    The two stores are separate on purpose — `approvals` is what humans click,
    `jr_runs` is what executes — and this is the only join between them. It can
    only ever COPY: `record_decision` writes solely when the run is still
    awaiting that exact approval id with no decision recorded, so a stale ticket
    or a second call cannot manufacture an authorisation.
    """
    pending = state.pending
    if pending is None or state.status != STATUS_AWAITING or state.decision is not None:
        return
    try:
        record = routes._store().get(pending.approval_id)
    except StoreUnavailable:
        return
    if record is None or record.decision not in ("approved", "denied"):
        return
    store.record_decision(state.run_id, pending.approval_id, record.decision)


def _run_payload(store: RunStore, run_id: str, headers, note: str | None = None) -> dict:
    """Always rendered from RELOADED state, never from the in-memory RunStatus.

    What a caller is shown has to be what is durably recorded — a response
    describing a state that failed to persist is how a UI ends up telling
    somebody an email was sent.
    """
    state = store.load(run_id)
    if state is None:
        return {"run_id": run_id, "status": STATUS_FAILED, "conversation": [],
                "pending_approval": None, "detail": "no such run"}
    pending = _pending_payload(state, headers)
    turns = conversation(state)
    if pending is not None:
        turns = [*turns, pending]
    payload = {
        "run_id": state.run_id,
        "status": state.status,
        "conversation": turns,
        "pending_approval": pending,
        "text": state.result_text,
        "rounds": state.round_index,
        "hit_limit": state.hit_limit,
        "detail": state.detail,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }
    if note:
        payload["note"] = note
    return payload


# --------------------------------------------------------------------------
# the work: one advance, never a loop
# --------------------------------------------------------------------------

def _drive(store: RunStore, build: AgentBuild, run_id: str, headers) -> dict:
    """One `advance`, then publish any approval it suspended on."""
    advance(run_id, build.client, build.guard, store)
    state = store.load(run_id)
    note = None
    if state is not None and state.status == STATUS_AWAITING:
        note = _register_approval(state, headers)
    return _run_payload(store, run_id, headers, note)


def _start_run(store: RunStore, build: AgentBuild, message: str, headers) -> dict:
    """Create a run whose FIRST USER TURN IS THE HUMAN'S MESSAGE.

    This is the whole point of the file: the task is no longer a constant.
    """
    state = store.create_run(build.system, message, build.max_rounds)
    return _drive(store, build, state.run_id, headers)


def _append_message(store: RunStore, build: AgentBuild, state: RunState,
                    message: str, headers) -> dict:
    messages = list(state.messages)
    if state.result_text:
        # `resume` keeps the closing text out of `messages`. Appending a human
        # turn straight after would hand the model a history in which it never
        # replied, and it would answer the previous question again.
        messages.append({"role": "assistant",
                         "content": [{"type": "text", "text": state.result_text}]})
    messages.append({"role": "user", "content": message})

    store.save(RunState(
        run_id=state.run_id, status=STATUS_RUNNING, system=state.system,
        messages=messages, round_index=state.round_index,
        # A fresh round budget per human turn. Carrying the old ceiling means the
        # second question in a conversation gets whatever rounds the first left
        # over — often none, so it would answer with a forced summary.
        max_rounds=state.round_index + build.max_rounds,
        pending=None, decision=None, result_text="", hit_limit=False, detail="",
        created_at=state.created_at,
    ))
    return _drive(store, build, state.run_id, headers)


def _refusal_for(state: RunState) -> Response | None:
    """Why a new human message cannot be accepted right now."""
    if state.status == STATUS_AWAITING:
        pending = state.pending
        detail = (f"approval {pending.approval_id} for {pending.tool}"
                  if pending else "an approval")
        return json_response(409, {
            "error": "this run is waiting on a human decision, not on another "
                     f"instruction: decide {detail} first, then POST "
                     f"/api/runs/{state.run_id}/advance.",
            "run_id": state.run_id,
            "status": state.status,
        })
    if state.status == STATUS_RUNNING:
        return json_response(409, {
            "error": "this run is still in progress; wait for it to finish or "
                     "suspend before adding a message.",
            "run_id": state.run_id, "status": state.status,
        })
    if state.status == STATUS_FAILED:
        # A failed run may hold a claimed approval whose outcome is unknown.
        # Continuing it would stack a new instruction on top of a tool call
        # nobody can say ran or not.
        return json_response(409, {
            "error": "this run failed and cannot be continued; start a new one.",
            "run_id": state.run_id, "status": state.status, "detail": state.detail,
        })
    if state.status != STATUS_DONE:
        # Allowlist, not a denylist: a status added to runstate.py later must
        # default to "cannot take a new instruction", not to "go ahead".
        return json_response(409, {
            "error": f"a run in state {state.status!r} cannot take a new message.",
            "run_id": state.run_id, "status": state.status,
        })
    return None


# --------------------------------------------------------------------------
# request parsing
# --------------------------------------------------------------------------

def _json_object(body: bytes) -> tuple[dict | None, Response | None]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, json_response(400, {"error": "body must be JSON"})
    if not isinstance(data, dict):
        return None, json_response(400, {"error": "body must be a JSON object"})
    return data, None


def _message_of(data: dict) -> tuple[str, Response | None]:
    raw = data.get("message")
    if not isinstance(raw, str) or not raw.strip():
        return "", json_response(400, {"error": "message is required and must be a "
                                                "non-empty string"})
    if len(raw) > MAX_MESSAGE:
        return "", json_response(413, {"error": f"message must be at most "
                                                f"{MAX_MESSAGE} characters"})
    return raw.strip(), None


def _segments(path: str) -> list[str]:
    return [s for s in urlsplit(path).path.strip("/").split("/") if s]


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

def runs(method: str, path: str, headers, body: bytes) -> Response:
    """Everything under /api/runs. Auth first, before anything is parsed.

    404 rather than 401/403, matching every other endpoint here: these routes can
    cause real email to leave and real Stripe invoices to exist, and a 401 would
    confirm to an unauthenticated caller that they had found them.
    """
    if not bearer_ok(headers):
        # Nothing is read, nothing is written, no store is even constructed.
        return not_found()

    segments = _segments(path)
    if segments[:2] != ["api", "runs"]:
        return not_found()
    tail = segments[2:]

    try:
        if not tail:
            if method == "POST":
                return _create(headers, body)
            if method == "GET":
                return _list(headers)
            return json_response(405, {"error": "method not allowed"})

        run_id = tail[0]
        if not RUN_ID_RE.match(run_id):
            return not_found()

        if len(tail) == 1:
            if method != "GET":
                return json_response(405, {"error": "method not allowed"})
            return _get(run_id, headers)

        if len(tail) == 2 and tail[1] == "messages":
            if method != "POST":
                return json_response(405, {"error": "method not allowed"})
            return _message(run_id, headers, body)

        if len(tail) == 2 and tail[1] == "advance":
            if method != "POST":
                return json_response(405, {"error": "method not allowed"})
            return _advance(run_id, headers)

        return not_found()
    except StorageError:
        # No exception text: `StorageError` can quote a database's own message,
        # and the sanitising is one refactor from being lost.
        return json_response(503, {"error": STORAGE_DOWN})


def _create(headers, body: bytes) -> Response:
    data, error = _json_object(body)
    if error is not None:
        return error
    message, error = _message_of(data)
    if error is not None:
        return error
    build, error = _agent_or_error()
    if error is not None:
        # Checked BEFORE the store is touched, so a misconfigured deploy leaves
        # no half-started run row behind.
        return error
    store, error = _store_or_error()
    if error is not None:
        return error
    return json_response(201, _start_run(store, build, message, headers))


def _get(run_id: str, headers) -> Response:
    store, error = _store_or_error()
    if error is not None:
        return error
    if store.load(run_id) is None:
        return not_found()
    return json_response(200, _run_payload(store, run_id, headers))


def _message(run_id: str, headers, body: bytes) -> Response:
    data, error = _json_object(body)
    if error is not None:
        return error
    message, error = _message_of(data)
    if error is not None:
        return error
    build, error = _agent_or_error()
    if error is not None:
        return error
    store, error = _store_or_error()
    if error is not None:
        return error
    state = store.load(run_id)
    if state is None:
        return not_found()
    refusal = _refusal_for(state)
    if refusal is not None:
        return refusal
    return json_response(200, _append_message(store, build, state, message, headers))


def _advance(run_id: str, headers) -> Response:
    build, error = _agent_or_error()
    if error is not None:
        return error
    store, error = _store_or_error()
    if error is not None:
        return error
    state = store.load(run_id)
    if state is None:
        return not_found()
    # Copy across a decision the human made on the approval surface. If there is
    # none, `advance` sees an undecided pending call and returns still awaiting —
    # it does not execute, and this route has no way to make it.
    _bridge_decision(store, state)
    return json_response(200, _drive(store, build, run_id, headers))


def _list(headers) -> Response:
    store, error = _store_or_error()
    if error is not None:
        return error
    return json_response(200, {"runs": recent_runs(store)})


def recent_runs(store: RunStore, limit: int = LIST_LIMIT) -> list[dict]:
    """Newest-first summary rows for a list view.

    Uses `RunStore._execute` deliberately. `runstate.py` belongs to another
    workstream and is finished; a read-only projection is not worth a change
    there, and reaching for the store's own transport is strictly better than
    opening a second connection to the same database with its own copy of the
    auth handling.
    """
    from ctrl_a_jr.runstate import SCHEMA

    results = store._execute([
        (SCHEMA, []),
        ("SELECT run_id, status, messages, created_at, updated_at FROM jr_runs "
         "ORDER BY updated_at DESC LIMIT ?", [limit]),
    ])
    rows = store._rows(results[-1]) if results else []
    out = []
    for row in rows:
        out.append({
            "run_id": str(row.get("run_id") or ""),
            "status": str(row.get("status") or ""),
            "first_message": _first_message(row.get("messages")),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        })
    return out


def _first_message(raw: object) -> str:
    """The human's opening turn, for the list label. Never raises on a bad row:
    one unreadable history must not blank the whole list."""
    if not isinstance(raw, str):
        return ""
    try:
        messages = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return _clip(content, 200)
    return ""


# --------------------------------------------------------------------------
# Slack events: the inbound door
# --------------------------------------------------------------------------

MENTION_RE = re.compile(r"^\s*<@[A-Z0-9]+>\s*")
RETRY_HEADER = "X-Slack-Retry-Num"


def _header(headers, name: str) -> str:
    get = getattr(headers, "get", None)
    if get is None:
        return ""
    return str(get(name) or get(name.lower()) or "").strip()


def _strip_mention(text: str) -> str:
    """`<@U123> quote the Tahoe job` -> `quote the Tahoe job`."""
    return MENTION_RE.sub("", str(text or "")).strip()


def _ensure_slack_schema(store: RunStore) -> None:
    store._execute([(sql, []) for sql in SLACK_SCHEMA])


def claim_slack_event(store: RunStore, event_id: str) -> bool:
    """Take ownership of one Slack event delivery, once. THE duplicate-invoice guard.

    Slack re-delivers an event if it does not get a 200 within three seconds, and
    one agent turn is slower than three seconds — so retries are the normal case,
    not the edge case. Without this, three deliveries of one @mention are three
    runs, three approval cards, and if the operator approves each, three invoices.

    Same shape as `RunStore.claim_pending` and for the same reason: INSERT OR
    IGNORE is a compare-and-set in the DATABASE, so two invocations racing on the
    same event id produce exactly one winner. A process-local set would not even
    survive the cold start between the original delivery and its retry.
    """
    results = store._execute([
        *[(sql, []) for sql in SLACK_SCHEMA],
        ("INSERT OR IGNORE INTO jr_slack_events (event_id, created_at) VALUES (?, ?)",
         [event_id, _now()]),
    ])
    return bool(results and results[-1].get("affected_row_count"))


def _record_slack_event(store: RunStore, event_id: str, thread_key: str, run_id: str) -> None:
    store._execute([
        ("UPDATE jr_slack_events SET thread_key = ?, run_id = ? WHERE event_id = ?",
         [thread_key, run_id, event_id]),
        ("INSERT OR IGNORE INTO jr_slack_threads (thread_key, run_id, created_at) "
         "VALUES (?, ?, ?)", [thread_key, run_id, _now()]),
    ])


def _run_for_thread(store: RunStore, thread_key: str) -> str | None:
    """So a reply in the same Slack thread continues the same conversation."""
    _ensure_slack_schema(store)
    results = store._execute([
        ("SELECT run_id FROM jr_slack_threads WHERE thread_key = ?", [thread_key]),
    ])
    rows = store._rows(results[-1]) if results else []
    return str(rows[0]["run_id"]) if rows else None


def _is_from_a_bot(payload: dict, event: dict) -> bool:
    """Drop anything the app itself (or any bot) said.

    The agent posts its answer into the same thread it is listening to. Without
    this the reply is a new event, which starts a run, which posts a reply — an
    unbounded loop in a real workspace, each turn of it able to ask for approval
    to send real email.
    """
    if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_changed",
                                                       "message_deleted"}:
        return True
    speaker = str(event.get("user") or "")
    if not speaker:
        return True
    authorizations = payload.get("authorizations")
    if isinstance(authorizations, list):
        for auth in authorizations:
            if isinstance(auth, dict) and str(auth.get("user_id") or "") == speaker:
                return True
    return False


def slack_events(method: str, path: str, headers, body: bytes) -> Response:
    """POST /api/slack/events — an @mention or DM becomes a run.

    Authentication is Slack's v0 signature and nothing else, verified by the SAME
    `slack.verify_signature` the interactive endpoint uses, before the body is
    parsed and before any store is constructed. This endpoint can cause real
    email to be drafted and real invoices to be prepared; an unverified caller
    must reach none of it.
    """
    if method != "POST":
        return json_response(405, {"error": "method not allowed"})
    if not slack.verify_signature(headers, body):
        return not_found()

    data, error = _json_object(body)
    if error is not None:
        return error

    kind = str(data.get("type") or "")
    if kind == "url_verification":
        # The handshake Slack performs when the Request URL is saved. Nothing is
        # stored and no run is created — it is a proof-of-ownership echo.
        challenge = data.get("challenge")
        if not isinstance(challenge, str):
            return json_response(400, {"error": "challenge must be a string"})
        return json_response(200, {"challenge": challenge})

    if kind != "event_callback":
        return json_response(200, {"ok": True, "ignored": kind or "unknown"})

    # A retry means our first delivery is either still running in another
    # invocation or already finished. Either way, starting the work again here is
    # exactly the duplicate-invoice failure. The event-id claim below would also
    # catch it; this is the cheap check that avoids even loading the store.
    if _header(headers, RETRY_HEADER):
        return json_response(200, {"ok": True, "retry": True})

    event = data.get("event")
    if not isinstance(event, dict):
        return json_response(200, {"ok": True, "ignored": "no event"})
    event_type = str(event.get("type") or "")
    if event_type == "message" and str(event.get("channel_type") or "") != "im":
        # Channel chatter the bot can merely see is not addressed to it.
        return json_response(200, {"ok": True, "ignored": "not a DM"})
    if event_type not in {"app_mention", "message"}:
        return json_response(200, {"ok": True, "ignored": event_type or "unknown"})
    if _is_from_a_bot(data, event):
        return json_response(200, {"ok": True, "ignored": "bot"})

    message = _strip_mention(event.get("text"))
    if not message:
        return json_response(200, {"ok": True, "ignored": "empty"})
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE]

    event_id = str(data.get("event_id") or "")
    channel = str(event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
    if not event_id or not channel or not thread_ts:
        return json_response(200, {"ok": True, "ignored": "incomplete event"})

    build, error = _agent_or_error()
    if error is not None:
        return error
    store, error = _store_or_error()
    if error is not None:
        return error

    try:
        if not claim_slack_event(store, event_id):
            # Already handled by another delivery of the same event.
            return json_response(200, {"ok": True, "duplicate": event_id})

        thread_key = f"{channel}:{thread_ts}"
        existing = _run_for_thread(store, thread_key)
        payload, refused = _slack_turn(store, build, existing, message, headers)
        run_id = payload.get("run_id", "") if payload else ""
        if run_id:
            _record_slack_event(store, event_id, thread_key, run_id)
    except StorageError:
        return json_response(503, {"error": STORAGE_DOWN})

    _reply_in_thread(build, channel, thread_ts, payload, refused)
    return json_response(200, {"ok": True, "run_id": run_id})


def _slack_turn(store: RunStore, build: AgentBuild, existing: str | None,
                message: str, headers) -> tuple[dict, str | None]:
    """Continue the thread's run when it can take a turn; otherwise start one."""
    if existing:
        state = store.load(existing)
        if state is not None:
            refusal = _refusal_for(state)
            if refusal is not None:
                # The gate owes a decision, or the run is mid-flight. The human
                # gets told, and no second run is started behind their back.
                return (_run_payload(store, existing, headers),
                        json.loads(refusal.body.decode("utf-8")).get("error", ""))
            return _append_message(store, build, state, message, headers), None
    return _start_run(store, build, message, headers), None


def _reply_in_thread(build: AgentBuild, channel: str, thread_ts: str,
                     payload: dict, refused: str | None) -> None:
    """Answer in the thread the question arrived in.

    The destination is the EVENT's channel, which Slack signed — not anything the
    model produced. `SlackClient.post_message` still has no channel parameter, so
    the model-facing path is unchanged; see `SlackClient.post_reply`.

    A Slack failure here must not fail the request: the run has already happened
    and is durably recorded. Losing the reply is bad; retrying the whole event
    because the reply failed would be worse.
    """
    status = payload.get("status") if payload else ""
    run_id = payload.get("run_id", "") if payload else ""
    blocks = None

    if refused:
        text = refused
    elif status == STATUS_AWAITING:
        pending = payload.get("pending_approval") or {}
        text = (f"I need approval before I can run `{pending.get('tool', '?')}`. "
                f"(run `{run_id}`)")
        # Slack rejects the ENTIRE message when a url button carries no url, so
        # a missing approve link must cost the card, not the reply.
        if pending.get("approve_url"):
            try:
                blocks = slack.approval_blocks(
                    approval_id=str(pending.get("approval_id") or ""),
                    tool=str(pending.get("tool") or ""),
                    # No args are published to the approval surface, so the card
                    # falls back to the rendered artifact — the same string the
                    # approval page shows.
                    args=None,
                    fallback_text=str(pending.get("rendered") or ""),
                    payload_hash=str(pending.get("payload_hash") or ""),
                    approve_url=str(pending["approve_url"]),
                )
            except Exception:  # noqa: BLE001 - a card we cannot build is still a reply
                blocks = None
    elif status == STATUS_FAILED:
        text = f"That run failed: {payload.get('detail') or 'unknown reason'} (run `{run_id}`)"
    else:
        text = payload.get("text") or f"Working on it. (run `{run_id}`)"

    try:
        build.slack.post_reply(channel, thread_ts, text, blocks)
    except Exception:  # noqa: BLE001 - never the exception text: it can quote the request
        return
