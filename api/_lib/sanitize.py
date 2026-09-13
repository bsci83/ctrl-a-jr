"""Treat the pushed artifact as untrusted markup.

`rendered_artifact` arrives as HTML from `ctrl_a_jr.artifacts`, which escapes
every value it interpolates — but the deployed page must not depend on that being
true, because the text inside it is a customer's own email body and the page has
no login: one injected <script> on this origin can POST /api/decide for any
approval id it can see and approve a real payment email with no human click.

So the page re-derives safety here instead of trusting the pusher: an allowlist
of structural tags, `class` as the only surviving attribute, every text node
re-escaped, and a matched-tag stack so malformed input cannot break out of the
card it is drawn in.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = frozenset({"div", "span", "p", "b", "i", "em", "strong", "br", "code", "pre"})
VOID_TAGS = frozenset({"br"})

# Their text is markup too; showing the source of a <script> is noise, not
# evidence, so the content goes with the tag.
DROP_WITH_CONTENT = frozenset(
    {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript"}
)

# The stylesheet on the page is fixed, so a class is safe; anything else (href,
# src, style, every on*) is not, and there is no artifact that needs one.
CLASS_RE = re.compile(r"^[A-Za-z0-9 _-]{0,120}$")

MAX_DEPTH = 40
MAX_INPUT = 200_000


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        # convert_charrefs=True (the default) decodes entities into text, which
        # this class then re-escapes — so an `&amp;` in the input survives as one
        # `&amp;` rather than being double-escaped into visible noise.
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._open: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_WITH_CONTENT:
            self._drop_depth += 1
            return
        if tag not in ALLOWED_TAGS:
            # Not dropped — escaped. A customer's email body can contain
            # `<https://evil.example|click>` or a raw `<img …>`, and deleting it
            # would show the approver less than the message that actually gets
            # sent. Escaped, it reads as the text it is and executes as nothing.
            self._text(self.get_starttag_text() or f"<{tag}>")
            return
        if self._drop_depth:
            return
        if tag in VOID_TAGS:
            self.out.append("<br>")
            return
        if len(self._open) >= MAX_DEPTH:
            return
        cls = next((v for k, v in attrs if k == "class" and v), None)
        if cls is not None and CLASS_RE.match(cls):
            self.out.append(f'<{tag} class="{html.escape(cls, quote=True)}">')
        else:
            self.out.append(f"<{tag}>")
        self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in DROP_WITH_CONTENT:
            return
        if tag in VOID_TAGS:
            if not self._drop_depth:
                self.out.append("<br>")
            return
        if tag not in ALLOWED_TAGS:
            self._text(self.get_starttag_text() or f"<{tag}/>")
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in DROP_WITH_CONTENT:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag not in ALLOWED_TAGS:
            self._text(f"</{tag}>")
            return
        if self._drop_depth or tag in VOID_TAGS:
            return
        # Only close a tag this sanitizer actually opened. A stray `</div>` in the
        # input would otherwise close the card wrapper and let the rest of the
        # artifact render as page chrome.
        if tag in self._open:
            while self._open:
                open_tag = self._open.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data):
        if not self._drop_depth:
            self.out.append(html.escape(data))

    # Nothing the customer wrote may be silently discarded. HTMLParser routes
    # `<!channel>` — the Slack ping, and anything else shaped like a declaration —
    # here rather than to handle_data, and dropping it would delete text from the
    # email the approver is authorising. Re-escaped, so it is readable and inert.
    def handle_comment(self, data):
        # HTMLParser routes a "bogus comment" here too, which is what `<!channel>`
        # is. The original delimiters are not recoverable through the parser, and
        # keeping the words the approver has to read matters more than
        # reproducing them exactly.
        self._text(f"<!{data.strip('-')}>")

    def handle_decl(self, decl):
        self._text(f"<!{decl}>")

    def handle_pi(self, data):
        self._text(f"<?{data}>")

    def unknown_decl(self, data):
        self._text(f"<![{data}]>")

    def _text(self, raw: str) -> None:
        if not self._drop_depth:
            self.out.append(html.escape(raw))

    def result(self) -> str:
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out)


def sanitize_artifact(markup: str | None) -> str:
    if not markup:
        return ""
    parser = _Sanitizer()
    parser.feed(markup[:MAX_INPUT])
    parser.close()
    return parser.result()


def to_text(markup: str | None) -> str:
    """Plain text of the artifact, for surfaces that cannot render HTML (Slack)."""
    if not markup:
        return ""
    chunks: list[str] = []

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self._drop = 0

        def handle_starttag(self, tag, attrs):
            if tag in DROP_WITH_CONTENT:
                self._drop += 1
            elif tag in ("div", "p", "br"):
                chunks.append("\n")

        def handle_endtag(self, tag):
            if tag in DROP_WITH_CONTENT:
                self._drop = max(0, self._drop - 1)

        def handle_data(self, data):
            if not self._drop:
                chunks.append(data)

        def handle_decl(self, decl):
            # See the sanitizer: `<!channel>` is a declaration to HTMLParser.
            if not self._drop:
                chunks.append(f"<!{decl}>")

    parser = _Text()
    parser.feed(markup[:MAX_INPUT])
    parser.close()
    lines = [line.strip() for line in "".join(chunks).splitlines()]
    return "\n".join(line for line in lines if line)
