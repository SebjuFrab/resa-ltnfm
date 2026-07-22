"""Small allow-list HTML sanitizer for staff-authored email content."""

from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "em",
    "h2",
    "h3",
    "i",
    "li",
    "ol",
    "p",
    "strong",
    "u",
    "ul",
}
VOID_TAGS = {"br"}
BLOCKED_WITH_CONTENT = {
    "applet",
    "audio",
    "canvas",
    "embed",
    "iframe",
    "math",
    "object",
    "script",
    "style",
    "svg",
    "template",
    "video",
}
ALLOWED_LINK_SCHEMES = {"http", "https", "mailto"}
BLOCK_TAGS = {"blockquote", "br", "h2", "h3", "li", "ol", "p", "ul"}
MAX_RICH_TEXT_LENGTH = 200_000


def _safe_href(value):
    value = unescape(value or "").strip()
    if not value or any(ord(character) < 32 for character in value):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ALLOWED_LINK_SCHEMES:
        return ""
    return value


class _AllowListHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.open_tags = []
        self.blocked_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.blocked_depth:
            if tag in BLOCKED_WITH_CONTENT:
                self.blocked_depth += 1
            return
        if tag in BLOCKED_WITH_CONTENT:
            self.blocked_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        rendered_attrs = ""
        if tag == "a":
            values = {name.lower(): value for name, value in attrs if value is not None}
            href = _safe_href(values.get("href", ""))
            title = values.get("title", "").strip()
            if href:
                rendered_attrs += f' href="{escape(href, quote=True)}"'
            if title:
                rendered_attrs += f' title="{escape(title[:300], quote=True)}"'
        self.parts.append(f"<{tag}{rendered_attrs}>")
        if tag not in VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        tag = tag.lower()
        if tag not in VOID_TAGS and tag in self.open_tags:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.blocked_depth:
            if tag in BLOCKED_WITH_CONTENT:
                self.blocked_depth -= 1
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS or tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data):
        if not self.blocked_depth:
            self.parts.append(escape(data, quote=False))

    def close(self):
        super().close()
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")

    def rendered(self):
        return "".join(self.parts).strip()


class _PlainTextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def rendered(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line).strip()


def sanitize_rich_html(value):
    """Return safe email HTML containing only a small formatting allow-list."""
    value = str(value or "")
    if len(value) > MAX_RICH_TEXT_LENGTH:
        raise ValueError("Le contenu enrichi est trop long.")
    parser = _AllowListHTMLParser()
    parser.feed(value)
    parser.close()
    return parser.rendered()


def rich_html_to_text(value):
    """Create a readable plain-text alternative from already sanitized HTML."""
    parser = _PlainTextHTMLParser()
    parser.feed(value or "")
    parser.close()
    return parser.rendered()
