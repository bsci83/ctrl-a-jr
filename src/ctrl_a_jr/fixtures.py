"""Seed and tear down the inbound-quote scenario: Stripe customers + real email.

This is DEV TOOLING, not agent capability. Nothing here is registered as a tool:
the agent may look a customer up and invoice a quoted job, but it must never be
able to create a customer or send mail *as* one.

Three safety rules, all load-bearing:

1. **Test mode only.** A key that is not `sk_test_*` is refused at construction,
   the same rule the agent's own StripeClient enforces.
2. **Teardown only touches what this module made.** Every Stripe object is
   stamped with `metadata[ctrl_a_jr_fixture]=true`, and teardown deletes nothing
   without it. A teardown that could reach real customer data would be worse
   than no teardown.
3. **The mail is real.** The requests are delivered by SMTP to the shop inbox so
   the agent has to find and parse an actual human message. Handing the details
   to the agent would demo nothing: parsing the email IS the capability.

Mail teardown is deliberately manual. The agent's IMAP path is readonly=True
because it bypasses the approval gate, and a fixture module that deleted mail
would need a write-capable path into the same mailbox. Every seeded message
carries `X-Ctrl-A-Jr-Fixture: true`, so a search on that header finds them all.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any
from urllib.parse import urlencode

import httpx

from . import pricing
from .tools.gmail_tools import _SMTP

API = "https://api.stripe.com/v1"
FIXTURE_TAG = "ctrl_a_jr_fixture"
FIXTURE_HEADER = "X-Ctrl-A-Jr-Fixture"

# Three requests, because "handle the biggest job" needs a real answer and a
# single fixture makes any choice look correct.
#
# `expect` is the classification a correct read of `body` produces. It is NOT
# given to the agent — it is here so the tests can price each request from the
# menu and prove that exactly one crosses the Slack threshold. The prompt says
# escalate above pricing.SLACK_THRESHOLD_CENTS and the task says handle the
# biggest job; if the biggest job sat under the threshold the agent's natural
# path would never touch Slack and the demo would silently be a two-app demo.
# The fixtures and the prompt have to be read together; neither is wrong alone.
#
# The bodies are free text on purpose: no labelled fields, model years and
# nicknames instead of sizes ("Tahoe", "F-250"), and the add-ons described the
# way a customer describes them ("covered in dog hair", "previous owner
# smoked"). The agent has to classify, not pattern-match a form.
DEFAULT_FIXTURES = [
    {
        "name": "Marcus Webb",
        "subject": "interior cleaning for my Tahoe?",
        "body": (
            "Hey there,\n\n"
            "Got a 2019 Tahoe that honestly needs help inside. Two labs ride in the "
            "back every weekend and the seats are covered in dog hair, plus the kids "
            "have done a number on the carpets. Outside is fine, I run it through the "
            "wash myself.\n\n"
            "Any chance you could get it in this week, and what would that run me?\n\n"
            "Thanks,\nMarcus Webb\n"
        ),
        "expect": {"service": "interior_detail", "size": "suv", "addons": ["pet_hair"]},
    },
    {
        "name": "Denise Okafor",
        "subject": "ceramic coating quote - F-250",
        "body": (
            "Good morning,\n\n"
            "I just picked up a 2022 F-250 and I want to protect the paint properly "
            "before winter - everyone keeps telling me ceramic is the way to go, so "
            "that's what I'm after.\n\n"
            "One other thing: the previous owner smoked in it and you can still smell "
            "it on a warm day. If there's something you can do about that, add it to "
            "the quote.\n\n"
            "No rush on timing, I'd rather it be done right.\n\n"
            "Denise Okafor\n"
        ),
        "expect": {"service": "ceramic_coating", "size": "truck", "addons": ["ozone"]},
    },
    {
        "name": "Ray Alvarez",
        "subject": "wash and wax?",
        "body": (
            "Hi - looking for a price on getting the outside of my Civic cleaned up. "
            "It's a 2016, paint's gone dull and there's some tree sap on the hood from "
            "where I park at work. Inside is already clean, I keep on top of that.\n\n"
            "Cheapest option that actually makes it look good is fine by me.\n\n"
            "- Ray\n"
        ),
        "expect": {"service": "exterior_detail", "size": "sedan", "addons": []},
    },
]


class _Httpx:
    def request(self, method: str, url: str, headers=None, data=None, timeout=30):
        r = httpx.request(method, url, headers=headers, data=data, timeout=timeout)
        r.raise_for_status()
        return r.json()


class FixtureSeeder:
    """Creates and removes the quote-request scenario for an eval run."""

    def __init__(self, api_key: str, email: str, app_password: str = "",
                 http: Any | None = None, smtp: Any | None = None) -> None:
        if not isinstance(api_key, str) or not api_key.startswith("sk_test_"):
            raise ValueError(
                "refusing to seed against a non-test Stripe key: expected sk_test_*. "
                "Fixtures create and delete objects; that must never touch live data."
            )
        if not email or "@" not in email:
            raise ValueError(
                "a deliverable email address is required — the requests are delivered to "
                "it and the agent replies to it, so you need to be able to read both."
            )
        self.api_key = api_key
        self.email = email
        self.app_password = app_password
        self.http = http or _Httpx()
        # Tracked separately from `self.smtp`: an injected transport is a test
        # double and needs no credential, while the real one cannot send without
        # the app password and must say so before creating any Stripe object.
        self.smtp_injected = smtp is not None
        self.smtp = smtp or _SMTP(email, app_password)

    # ── transport ────────────────────────────────────────────────────────────

    def _post(self, path: str, data: dict) -> dict:
        return self.http.request(
            "POST", f"{API}/{path}",
            headers={"Authorization": f"Bearer {self.api_key}"}, data=data,
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API}/{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return self.http.request(
            "GET", url, headers={"Authorization": f"Bearer {self.api_key}"},
        )

    def _delete(self, path: str) -> dict:
        return self.http.request(
            "DELETE", f"{API}/{path}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    # ── seeding ──────────────────────────────────────────────────────────────

    def send_request_email(self, name: str, subject: str, body: str) -> bytes:
        """Deliver one customer's request to the shop inbox, for real.

        The From address must be the authenticated account — Gmail rewrites
        anything else — so the customer's identity rides in the display name and
        in their sign-off, which is where a real reader gets it from anyway.
        """
        msg = EmailMessage()
        msg["From"] = f"{name} <{self.email}>"
        msg["To"] = self.email
        msg["Subject"] = subject
        msg[FIXTURE_HEADER] = "true"
        msg.set_content(body)
        raw = msg.as_bytes()
        self.smtp.send(self.email, self.email, raw)
        return raw

    def seed_one(self, name: str, subject: str, body: str, expect: dict | None = None) -> dict:
        """One tagged Stripe customer plus the email that asks for the quote.

        No invoice is created here. The invoice is the agent's job, priced from
        the menu — seeding one would be pre-computing the answer.
        """
        customer = self._post("customers", {
            "name": name,
            "email": self.email,
            f"metadata[{FIXTURE_TAG}]": "true",
        })
        self.send_request_email(name, subject, body)
        row = {"customer_id": customer["id"], "name": name, "email": self.email,
               "subject": subject}
        if expect:
            # Priced from the menu, never written down as a literal: a hardcoded
            # expected total would drift the moment the menu changed and the
            # fixtures would quietly stop matching what the agent can charge.
            quote = pricing.quote(**expect)
            row["expected_total_cents"] = quote["total_cents"]
            row["expected_total"] = quote["total"]
            row["over_slack_threshold"] = quote["over_slack_threshold"]
        return row

    def seed(self, fixtures: list[dict] | None = None) -> list[dict]:
        if not self.smtp_injected and not self.app_password:
            raise ValueError(
                "GMAIL_APP_PASSWORD is required to seed: the requests are delivered by "
                "SMTP so the agent has real mail to find and parse."
            )
        return [self.seed_one(**f) for f in (fixtures or DEFAULT_FIXTURES)]

    # ── listing and teardown ─────────────────────────────────────────────────

    def list_fixtures(self) -> list[dict]:
        """Every customer this module created, by tag."""
        found = []
        for c in self._get("customers", {"limit": 100}).get("data", []):
            if (c.get("metadata") or {}).get(FIXTURE_TAG) == "true":
                found.append({"customer_id": c["id"], "name": c.get("name"),
                              "email": c.get("email")})
        return found

    def teardown(self) -> list[str]:
        """Delete ONLY tagged customers. Deleting a customer voids their invoices.

        An untagged object is never touched, even in test mode — the tag is the
        only thing standing between this and someone's real customer list if a
        live key ever slipped past the constructor. Seeded mail is not deleted;
        see the module docstring.
        """
        removed = []
        for row in self.list_fixtures():
            self._delete(f"customers/{row['customer_id']}")
            removed.append(row["customer_id"])
        return removed
