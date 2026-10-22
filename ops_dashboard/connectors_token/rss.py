"""RSS / Atom feeds (no authentication) — read + ingest entries into the
vault. No writes.

The user lists the feed URLs (https only). Each feed host must resolve to a
public IP (the kit refuses loopback/private/link-local addresses), only
same-host redirects are followed (at most 3), responses are capped at 5 MB
and XML with a DOCTYPE/ENTITY declaration is refused before parsing (no
entity-expansion bombs, no external entities). Entry HTML is reduced to plain
text; everything is ingested as untrusted.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET  # noqa: S405  (DOCTYPE/ENTITY refused before parsing)
from collections.abc import Iterator
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from .. import connector_registry as registry
from . import base

FEED_RE = r"https://[^\s<>\"']{4,500}"
MAX_FEEDS = 50
MAX_ENTRIES = 50
MAX_REDIRECTS = 3
_UNSAFE_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
# The byte regex above only sees declarations in ASCII-compatible encodings; a
# UTF-16/32 (or EBCDIC) feed would hide them from it while expat still expands
# them. So only ASCII-compatible encodings are accepted at all.
_XML_ENCODING = re.compile(rb"""^<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_ASCII_COMPATIBLE = {"utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1", "latin1",
                     "iso-8859-15", "windows-1252", "cp1252"}
_HTMLISH = ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "script", "style")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(data)


def html_to_text(s: str) -> str:
    p = _Text()
    try:
        p.feed(s or "")
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML: fall back to tag stripping
        return re.sub(r"<[^>]*>", " ", s or "")
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", "".join(p.out))).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _markup(el: ET.Element | None) -> str:
    """Entry body as HTML for html_to_text. Atom ``type="xhtml"`` content is
    child elements, not text: serialize it so paragraph breaks survive and
    script/style stay recognisable (itertext() would merge them in)."""
    if el is None:
        return ""
    if el.get("type") == "xhtml" or any(_local(c.tag) in _HTMLISH for c in el.iter() if c is not el):
        inner = "".join(ET.tostring(c, encoding="unicode", method="html") for c in el)
        return re.sub(r"<(/?)[a-z0-9]+:", r"<\1", (el.text or "") + inner)
    return _text(el)


def _check_encoding(data: bytes) -> None:
    head = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    if b"\x00" in data or not head.lstrip().startswith(b"<"):
        raise base.TokenError("unsafe_xml", "only UTF-8 / ASCII-compatible feeds are accepted", 422)
    m = _XML_ENCODING.match(head.lstrip())
    if m and m.group(1).decode().lower() not in _ASCII_COMPATIBLE:
        raise base.TokenError("unsafe_xml", "only UTF-8 / ASCII-compatible feeds are accepted", 422)


def _date(s: str) -> int:
    if not s:
        return 0
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except (TypeError, ValueError):
        pass
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _link(entry: ET.Element, base_url: str) -> str:
    for c in entry:
        if _local(c.tag) == "link":
            href = c.get("href")
            if href and c.get("rel", "alternate") == "alternate":
                return urljoin(base_url, href)
            if not href and _text(c):
                return urljoin(base_url, _text(c))
    return ""


def parse_feed(data: bytes, feed_url: str) -> tuple[str, list[dict]]:
    _check_encoding(data)
    if _UNSAFE_XML.search(data[:65536]) or _UNSAFE_XML.search(data):
        raise base.TokenError("unsafe_xml", "feeds with DOCTYPE/ENTITY declarations are refused", 422)
    try:
        root = ET.fromstring(data)  # noqa: S314
    except ET.ParseError as e:
        raise base.TokenError("bad_feed", f"not a valid RSS/Atom document: {e}", 422) from e
    channel = _child(root, "channel")
    feed_title = _text(_child(channel if channel is not None else root, "title")) or urlparse(feed_url).hostname or ""
    entries = [el for el in root.iter() if _local(el.tag) in ("item", "entry")][:MAX_ENTRIES]
    out = []
    for ent in entries:
        body = (_markup(_child(ent, "encoded")) or _markup(_child(ent, "content"))
                or _markup(_child(ent, "description")) or _markup(_child(ent, "summary")))
        link = _link(ent, feed_url)
        out.append({"title": _text(_child(ent, "title")), "link": link if link.startswith("https://") else "",
                    "id": _text(_child(ent, "guid", "id")) or link or _text(_child(ent, "title")),
                    "body": html_to_text(body),
                    "updated": _date(_text(_child(ent, "updated", "pubdate", "published", "date")))})
    return feed_title, out


def _fetch(c: base.TokenClient, url: str) -> bytes:
    host = urlparse(url).hostname
    for _ in range(MAX_REDIRECTS + 1):
        r = c.request("GET", url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"})
        if r.status_code in (301, 302, 303, 307, 308):
            nxt = urljoin(url, r.headers.get("location", ""))
            if urlparse(nxt).scheme != "https" or urlparse(nxt).hostname != host:
                raise base.TokenError("redirect_refused", f"{host} redirects to another host; use the final feed URL", 502)
            url = nxt
            continue
        return c._check(r).content
    raise base.TokenError("too_many_redirects", host or "", 502)


def feed_hosts(config: dict) -> tuple[str, ...]:
    return tuple(dict.fromkeys((urlparse(u).hostname or "").lower() for u in config.get("feeds") or []))


def whoami(c: base.TokenClient) -> str:
    n = len(base.config_of(CONNECTOR).get("feeds") or [])
    return f"{n} feed(s)"


def sync(c: base.TokenClient, config: dict) -> Iterator[base.Item]:
    feeds = config.get("feeds") or []
    if not feeds:
        raise base.TokenError("no_feeds", "add at least one feed URL first", 400)
    failures: list[str] = []
    for url in feeds:
        try:
            title, entries = parse_feed(_fetch(c, url), url)
        except base.TokenError as e:
            failures.append(e.code)
            continue
        except Exception:  # noqa: BLE001 - timeout/TLS/connection on one feed must not stop the others
            failures.append("network_error")
            continue
        host = urlparse(url).hostname or ""
        for en in entries:
            key = hashlib.sha256(f"{url}\n{en['id']}".encode()).hexdigest()[:20]
            yield base.Item(kind="entry", key=key, title=f"{title} — {en['title']}".strip(" —"),
                            content=f"{title} · {en['link'] or url}\n\n{en['body']}", url=en["link"],
                            updated=en["updated"], tags=("rss", host))
    if failures and len(failures) == len(feeds):
        raise base.TokenError(failures[0], "every feed failed", 502)


CONNECTOR = base.register_connector(base.TokenConnector(
    spec=registry.ConnectorSpec(
        id="rss", name="RSS / Atom", auth="token",
        # Placeholder: RSS never uses ConnectorClient; TokenClient only talks to
        # the feed hosts the user configured (hosts_from_config, public IPs only).
        allowed_hosts=("feeds.user-configured.invalid",),
        actions={"sync": registry.ActionSpec("sync", "read", "Import feed entries into the vault", base.sync_action("rss"))}),
    token_mode="none", whoami=whoami, sync=sync, hosts_from_config=feed_hosts,
    config_fields=(base.ConfigField("feeds", "list", "Feeds (URL https, uno por línea)", "Feeds (https URL, one per line)",
                                    pattern=FEED_RE, max_items=MAX_FEEDS),),
    token_help_es="Sin cuenta ni token: añade las URL https de los feeds RSS o Atom. Solo se leen; no hay escrituras.",
    token_help_en="No account or token: add the https URLs of RSS or Atom feeds. Read-only; there are no writes.",
))
