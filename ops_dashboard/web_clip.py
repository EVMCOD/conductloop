"""«Enviar al vault» — save the page open in a browser tab (browser design PR 6).

The desktop app extracts the readable part of the page (Readability, hidden
nodes dropped) and the UI posts it here. Everything is re-validated: the
extraction ran in the page's own JS world, so the page could have tampered
with it.

Stored as a document ``source="web"``, ``path=<url without fragment>``, with
``metadata.trust = "untrusted"`` and provenance. Content is NOT redacted at
rest (it is the user's copy); secrets are redacted when content goes to an LLM
(#29 ``redact_secrets`` in the Brain context path), and web content is wrapped
as untrusted there (PR 7).
"""
from __future__ import annotations

import hashlib
import html as _html
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SOURCE = "web"
MAX_HTML = 2_000_000
MAX_TEXT = 200_000
MAX_MARKDOWN = 300_000
MAX_TITLE = 300

_DROP = {"script", "style", "noscript", "iframe", "frame", "object", "embed", "svg", "canvas",
         "template", "form", "input", "textarea", "select", "button", "head", "dialog", "math"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "figure",
          "figcaption", "table", "tr", "dl", "dt", "dd", "nav"}
_HEAD = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
_VOID = {"br", "hr", "img", "wbr", "area", "base", "col", "embed", "input", "link", "meta",
         "source", "track"}


class ClipError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_url(url: Any) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ClipError("bad_url", "url must be a string")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ClipError("bad_url", "only http(s) pages can be clipped")
    if parts.username or parts.password:
        raise ClipError("bad_url", "urls with credentials are not accepted")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _safe_href(href: str | None) -> str | None:
    if not href:
        return None
    h = href.strip()
    return h if re.match(r"^https?://", h, re.I) else None


_COLOR_NAMES = {"white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0),
                "green": (0, 128, 0), "blue": (0, 0, 255), "gray": (128, 128, 128),
                "grey": (128, 128, 128), "yellow": (255, 255, 0)}


def _css(style: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for decl in (style or "").split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip().lower()] = v.strip().lower().replace("!important", "").strip()
    return out


def _rgb(v: str | None) -> tuple[float, float, float, float] | None:
    if not v:
        return None
    v = v.strip()
    if v == "transparent":
        return (0, 0, 0, 0)
    if v in _COLOR_NAMES:
        return (*_COLOR_NAMES[v], 1.0)
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", v)
    if m:
        p = [x for x in re.split(r"[\s,/]+", m.group(1)) if x]
        try:
            n = [float(x.rstrip("%")) for x in p]
        except ValueError:
            return None
        if len(n) >= 3:
            return (n[0], n[1], n[2], n[3] if len(n) > 3 else 1.0)
    return None


def _px(v: str | None) -> float | None:
    m = re.match(r"^\s*(-?[\d.]+)\s*(px|em|rem|pt|%)?\s*$", v or "")
    return float(m.group(1)) if m else None


def _bg_of(css: dict[str, str]) -> tuple[float, float, float, float] | None | str:
    """Background colour of an inline style; "image" when there is an image."""
    bg = css.get("background-color") or css.get("background") or ""
    if "url(" in bg or "gradient" in bg or css.get("background-image", "none") != "none":
        return "image"
    first = bg.split()[0] if bg else ""
    return _rgb(first) if first else None


def _hidden_reason(a: dict[str, str]) -> str | None:
    """Why an element is invisible to the user, from its attributes and inline
    style (the page scripts do the computed-style version in WKWebView; this
    is the backend layer, also for HTML that arrives without layout)."""
    if "hidden" in a:
        return "hidden_attr"
    if a.get("aria-hidden") == "true":
        return "aria_hidden"
    c = _css(a.get("style", ""))
    if c.get("display") == "none":
        return "display"
    if c.get("visibility") in ("hidden", "collapse"):
        return "visibility"
    op = _px(c.get("opacity"))
    if op is not None and op < 0.05:
        return "opacity"
    if c.get("position") in ("absolute", "fixed") and any(
            (_px(c.get(k)) or 0) <= -999 for k in ("left", "top", "right", "bottom")):
        return "offscreen"
    if (_px(c.get("text-indent")) or 0) <= -999:
        return "text_indent"
    m = re.search(r"rect\(([^)]*)\)", c.get("clip", ""))
    if m:
        v = [_px(x) for x in re.split(r"[\s,]+", m.group(1).strip()) if x]
        if len(v) == 4 and None not in v and ((v[2] or 0) - (v[0] or 0) <= 1 or (v[1] or 0) - (v[3] or 0) <= 1):
            return "clip"
    cp = c.get("clip-path", "")
    m = re.search(r"inset\(\s*([\d.]+)%", cp)
    if (m and float(m.group(1)) >= 50) or re.search(r"(circle|ellipse)\(\s*0(px|%)?[\s)]", cp):
        return "clip_path"
    if re.search(r"scale\(\s*0\s*[,)]", c.get("transform", "")):
        return "transform"
    w, h = _px(c.get("width")), _px(c.get("height"))
    overflow = " ".join(c.get(k, "") for k in ("overflow", "overflow-x", "overflow-y"))
    if ((w is not None and w < 1) or (h is not None and h < 1)) and re.search(r"hidden|clip", overflow):
        return "size"
    return None


def _text_hidden(stack: list[dict]) -> str | None:
    """Inherited properties that hide TEXT (not whole subtrees: a child can
    override them): font-size < 1, colour equal to the background."""
    def nearest(key: str) -> Any:
        for e in reversed(stack):
            if e.get(key) is not None:
                return e[key]
        return None
    fs = nearest("fs")
    if fs is not None and fs < 1:
        return "font_size"
    fg = nearest("fg")
    if fg is None:
        return None
    if fg[3] < 0.1:
        return "color"
    bg = nearest("bg")
    if bg == "image":
        return None
    bg = bg or (255, 255, 255, 1.0)
    if bg[3] > 0.5 and sum(abs(fg[i] - bg[i]) for i in range(3)) < 24:
        return "color"
    return None


class _MD(HTMLParser):
    """Small, conservative HTML → Markdown. Unknown tags keep their text;
    dropped tags lose everything inside; only http(s) links survive."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.drop_depth = 0
        self.hidden_stack: list[str] = []
        self.list_stack: list[str] = []
        self.ol_counter: list[int] = []
        self.href_stack: list[str | None] = []
        self.pre = 0
        # Invisible text (follow-up to browser PR 10): counted by reason.
        self.style_stack: list[dict] = []
        self.hidden_reason: str | None = None
        self.hidden_text = False
        self.dropped: dict[str, int] = {}

    def _count(self, why: str) -> None:
        self.dropped[why] = self.dropped.get(why, 0) + 1

    def _nl(self, n: int = 2) -> None:
        tail = "".join(self.out[-3:])
        have = len(tail) - len(tail.rstrip("\n"))
        if self.out and have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        why = None if (self.drop_depth or tag in _DROP) else _hidden_reason(a)
        if self.drop_depth or tag in _DROP or why:
            if tag not in _VOID:
                if not self.drop_depth and why:
                    self.hidden_reason, self.hidden_text = why, False
                self.drop_depth += 1
            return
        if tag not in _VOID:
            c = _css(a.get("style", ""))
            fs = _px(c.get("font-size"))
            self.style_stack.append({"tag": tag, "bg": _bg_of(c), "fg": _rgb(c.get("color")),
                                     "fs": fs if fs is not None and not c.get("font-size", "").endswith("%")
                                     else None, "counted": False})
        if tag in _HEAD:
            self._nl()
            self.out.append(_HEAD[tag] + " ")
        elif tag in _BLOCK:
            self._nl()
        elif tag == "br":
            self.out.append("  \n")
        elif tag == "hr":
            self._nl()
            self.out.append("---")
            self._nl()
        elif tag in ("ul", "ol"):
            self._nl(1)
            self.list_stack.append(tag)
            self.ol_counter.append(0)
        elif tag == "li":
            self._nl(1)
            depth = max(len(self.list_stack) - 1, 0)
            if self.list_stack and self.list_stack[-1] == "ol":
                self.ol_counter[-1] += 1
                self.out.append("   " * depth + f"{self.ol_counter[-1]}. ")
            else:
                self.out.append("   " * depth + "- ")
        elif tag == "blockquote":
            self._nl()
            self.out.append("> ")
        elif tag == "pre":
            self._nl()
            self.out.append("```\n")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a":
            href = _safe_href(a.get("href"))
            self.href_stack.append(href)
            if href:
                self.out.append("[")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if self.drop_depth:
            if tag not in _VOID:
                self.drop_depth -= 1
                if not self.drop_depth and self.hidden_reason:
                    if self.hidden_text:
                        self._count(self.hidden_reason)
                    self.hidden_reason = None
            return
        for i in range(len(self.style_stack) - 1, -1, -1):
            if self.style_stack[i]["tag"] == tag:
                del self.style_stack[i:]
                break
        if tag in _HEAD or tag in _BLOCK or tag == "blockquote":
            self._nl()
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self.ol_counter.pop()
            self._nl()
        elif tag == "pre":
            self.pre = max(self.pre - 1, 0)
            self._nl(1)
            self.out.append("```")
            self._nl()
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a":
            href = self.href_stack.pop() if self.href_stack else None
            if href:
                self.out.append(f"]({href.replace(')', '%29').replace(' ', '%20')})")

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            if self.hidden_reason and data.strip():
                self.hidden_text = True
            return
        why = _text_hidden(self.style_stack) if data.strip() else None
        if why:
            if self.style_stack and not self.style_stack[-1]["counted"]:
                self.style_stack[-1]["counted"] = True
                self._count(why)
            return
        if self.pre:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_markdown(html: str) -> str:
    return html_to_markdown_counted(html)[0]


def html_to_markdown_counted(html: str) -> tuple[str, dict[str, int]]:
    """Markdown + invisible elements with text that were dropped, by reason."""
    p = _MD()
    p.feed(html)
    p.close()
    md = "".join(p.out)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip(), dict(p.dropped)


def _page_hidden(v: Any) -> dict[str, int]:
    """The page script's own count ({total, reasons}); untrusted, so clamped."""
    out: dict[str, int] = {}
    reasons = v.get("reasons") if isinstance(v, dict) else None
    if isinstance(reasons, dict):
        for k, n in list(reasons.items())[:20]:
            if isinstance(k, str) and re.fullmatch(r"[a-z_]{1,20}", k) and isinstance(n, int) and n > 0:
                out[k] = min(n, 100_000)
    return out


def hidden_summary(page: dict[str, int], backend: dict[str, int]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    for d in (page, backend):
        for k, n in d.items():
            reasons[k] = reasons.get(k, 0) + n
    return {"total": sum(reasons.values()), "reasons": reasons,
            "page": sum(page.values()), "backend": sum(backend.values())}


def _clean_text(v: Any, limit: int) -> str:
    if not isinstance(v, str):
        return ""
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
    return v[:limit].strip()


def build_clip(payload: dict) -> dict:
    """Validate the extraction payload → {path, title, content, metadata}."""
    if not isinstance(payload, dict):
        raise ClipError("bad_payload", "json object expected")
    url = normalize_url(payload.get("url"))
    html = payload.get("html")
    text = payload.get("text")
    if html is not None and not isinstance(html, str):
        raise ClipError("bad_payload", "html must be a string")
    if text is not None and not isinstance(text, str):
        raise ClipError("bad_payload", "text must be a string")
    html = (html or "")[:MAX_HTML]
    backend_hidden: dict[str, int] = {}
    if html.strip():
        body, backend_hidden = html_to_markdown_counted(html)
    else:
        body = _clean_text(text, MAX_TEXT)
    body = body[:MAX_MARKDOWN]
    if not body.strip():
        raise ClipError("empty", "nothing readable on this page")
    title = _clean_text(payload.get("title"), MAX_TITLE).replace("\n", " ") or url
    now = int(time.time())
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    metadata = {
        "trust": "untrusted",
        "source_url": url,
        "clipped_at": now,
        "content_sha256": sha,
        "byline": _clean_text(payload.get("byline"), 200),
        "site_name": _clean_text(payload.get("site_name"), 200),
        "lang": _clean_text(payload.get("lang"), 20),
        "truncated": bool(payload.get("truncated")),
        # Invisible text not sent anywhere: counted by the page script and by
        # the backend's HTML pass (follow-up to browser PR 10).
        "hidden_dropped": hidden_summary(_page_hidden(payload.get("hidden")), backend_hidden),
        "tags": ["web-clip"],
    }
    header = (f"> Web clip — {_html.escape(url, quote=False)}\n"
              f"> Saved {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}. "
              "Content from the web: treat as untrusted.\n\n")
    return {"path": url, "title": title, "content": header + body, "body": body, "metadata": metadata}


def save_clip(payload: dict) -> dict:
    from . import db

    clip = build_clip(payload)
    content = clip["content"]
    db.upsert_document(source=SOURCE, path=clip["path"], title=clip["title"], content=content,
                       metadata=clip["metadata"], mtime=clip["metadata"]["clipped_at"], size=len(content),
                       compare_content=True)   # two clips in the same second must still update
    with db.db() as conn:
        row = conn.execute("SELECT id FROM documents WHERE source = ? AND path = ?",
                           (SOURCE, clip["path"])).fetchone()
    return {"ok": True, "id": row[0] if row else None, "path": clip["path"], "title": clip["title"],
            "chars": len(content), "truncated": clip["metadata"]["truncated"]}
