"""Vercel serverless functions: the deployed approval surface.

Nothing here holds a Stripe key, a Gmail password or a Slack bot token. The local
agent keeps every credential and reaches this app OUTBOUND only — it pushes a
pending approval, then polls for the decision. Nothing ever connects inbound to
the operator's machine.

Route files are thin shims over `api/_lib/routes.py`. Vercel does not route files
or directories whose name starts with `_`, so `_lib` ships in the bundle without
becoming an endpoint.
"""
