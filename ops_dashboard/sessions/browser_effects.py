"""Effect classifier for the agent's click/type (browser design PR 9, §3.3,
B10). Mirror of ``desktop/url-policy/src/effects.rs``; both are tested
against ``desktop/url-policy/tests/effect_vectors.txt``.

Python asks the user BEFORE calling the bridge; Rust re-classifies the element
actually found in the page right before acting and refuses an effectful
action that arrives without ``confirmed``. Deterministic, conservative: when
in doubt, confirm. The model never decides.

Result codes: ``allow`` | ``confirm:<reason>`` | ``deny:<reason>``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

EFFECT_VERBS = (
    "comprar", "compra", "pagar", "pago", "pedido", "tramitar", "reservar", "donar", "transferir",
    "publicar", "enviar", "mandar", "borrar", "eliminar", "suscrib", "darse de baja", "confirmar",
    "registrar", "aceptar y pagar",
    "buy", "purchase", "checkout", "check out", "order", "pay",
    "donate", "transfer", "book", "post", "publish", "send", "submit", "delete", "remove",
    "subscribe", "unsubscribe", "confirm", "sign up", "register", "place order",
    # Irreversible or outward-facing actions the list above did not cover:
    # approving, merging, granting access, deploying, replying, cancelling…
    # A click on one of these changes somebody else's state, so it has to go
    # through the approval gate (2026-10-23).
    "aprobar", "fusionar", "guardar", "autorizar", "conceder", "otorgar", "revocar", "cancelar",
    "aceptar", "aceptar invitacion", "rechazar solicitud", "asignar", "comprar ahora",
    "instalar", "desplegar", "publicar en", "invitar", "compartir", "responder", "comentar",
    "reenviar", "ejecutar", "archivar", "vaciar", "restablecer", "activar", "desactivar",
    "cerrar cuenta", "firmar", "validar", "pujar", "canjear", "contratar", "facturar",
    "approve", "approval", "merge", "accept", "save", "authorize", "grant", "revoke", "cancel",
    "install", "deploy", "uninstall", "upgrade", "downgrade", "invite", "share", "reply", "respond",
    "comment", "forward", "execute", "run", "archive", "purge", "wipe", "reset", "restore",
    "enable", "disable", "unlock", "lock", "sign", "verify", "validate", "confirm payment",
    "close account", "terminate", "suspend", "unsuspend", "reopen", "assign", "add to cart",
    "claim", "redeem", "bid", "subscribe to", "change password", "add member", "remove member",
)
_TEXT_TAGS = ("input", "textarea")
_NOT_TEXT_TYPES = ("checkbox", "radio", "file", "submit", "button", "image", "hidden")


@dataclass
class Element:
    tag: str = ""
    typ: str = ""
    role: str = ""
    name: str = ""
    autocomplete: str = ""
    in_form: bool = False
    href: str | None = None
    payment: bool = False


@dataclass
class Page:
    host: str = ""
    has_password: bool = False
    has_payment: bool = False


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[\W_]+", " ", s).strip()


def has_effect_verb(name: str) -> bool:
    n = " " + normalize(name)
    return any(" " + v in n for v in EFFECT_VERBS)


# Registrable domains (eTLD+1): mirror of desktop/url-policy/src/sites.rs,
# tested against desktop/url-policy/tests/site_vectors.txt. Not the full
# Public Suffix List (see the Rust module for the trade-off).
MULTI_LABEL_SUFFIXES = (
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk",
    "nhs.uk", "police.uk", "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.nz", "org.nz", "net.nz", "govt.nz", "ac.nz", "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "gr.jp", "co.kr", "or.kr", "ne.kr", "go.kr", "ac.kr", "com.br", "net.br", "org.br", "gov.br",
    "com.mx", "org.mx", "gob.mx", "com.ar", "gob.ar", "com.cn", "net.cn", "org.cn", "gov.cn",
    "edu.cn", "com.tw", "org.tw", "com.hk", "org.hk", "com.sg", "edu.sg", "gov.sg", "co.in",
    "net.in", "org.in", "gov.in", "ac.in", "co.za", "org.za", "gov.za", "com.tr", "org.tr",
    "gov.tr", "com.es", "org.es", "gob.es", "nom.es", "edu.es", "co.il", "org.il", "ac.il",
    "gov.il", "com.pl", "net.pl", "org.pl", "com.pt", "com.co", "com.pe", "com.ve", "com.ec",
    "com.uy", "com.do", "com.gt", "com.bo", "com.py", "com.sv", "com.ni", "com.pa", "com.cu",
    "github.io", "gitlab.io", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev",
    "workers.dev", "web.app", "firebaseapp.com", "appspot.com", "blogspot.com",
    "azurewebsites.net", "cloudfront.net", "s3.amazonaws.com", "fly.dev", "onrender.com",
    "glitch.me", "repl.co", "ngrok.io", "ngrok-free.app", "trycloudflare.com", "wordpress.com",
    "substack.com", "notion.site", "webflow.io", "framer.app", "surge.sh", "neocities.org",
)
_SECOND_LEVEL_WORDS = ("co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "nom",
                       "mil")


def _is_ip_literal(h: str) -> bool:
    return ":" in h or (bool(h) and all(lbl.isdigit() and lbl.isascii() for lbl in h.split(".")))


def registrable_domain(host: str) -> str:
    h = (host or "").strip().rstrip(".").lower().lstrip("[").rstrip("]")
    if _is_ip_literal(h):
        return h
    labels = [lbl for lbl in h.split(".") if lbl]
    n = len(labels)
    if n <= 2:
        return ".".join(labels)
    k = 1
    for s in MULTI_LABEL_SUFFIXES:
        sk = s.count(".") + 1
        if k < sk < n and ".".join(labels[n - sk:]) == s:
            k = sk
    if k == 1 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_WORDS:
        k = 2
    return ".".join(labels[n - k - 1:])


def site(host: str) -> str:
    """Registrable domain: what "same site" means for links and the gate."""
    return registrable_domain(host)


def http_host(url: str) -> str | None:
    low = (url or "").strip().lower()
    for p in ("https://", "http://"):
        if low.startswith(p):
            rest = low[len(p):]
            break
    else:
        return None
    auth = re.split(r"[/?#]", rest, maxsplit=1)[0].rsplit("@", 1)[-1]
    host = auth[1:].split("]", 1)[0] if auth.startswith("[") else auth.split(":", 1)[0]
    return host or None


def nav_needs_confirm(known: set[str] | list[str], url: str) -> str | None:
    """The new-site gate (mirror of ``sites::nav_needs_confirm``)."""
    h = http_host(url)
    if h is None:
        return None
    s = registrable_domain(h)
    return None if s in known else s


def _href_host(href: str) -> str | None:
    if "://" not in href:
        return None
    auth = re.split(r"[/?#]", href.split("://", 1)[1], maxsplit=1)[0].rsplit("@", 1)[-1]
    if auth.startswith("["):
        return auth[1:].split("]", 1)[0]
    return auth.split(":", 1)[0]


def _is_password(el: Element) -> bool:
    return el.typ.lower() == "password" or "password" in el.autocomplete.lower()


def _is_payment(el: Element) -> bool:
    return el.payment or el.autocomplete.lower().startswith("cc-")


def classify(action: str, el: Element, page: Page, *, submit: bool = False) -> str:
    """``action``: 'click' | 'type'."""
    tag, typ = el.tag.lower(), el.typ.lower()
    if action == "type":
        if _is_password(el):
            return "deny:password_field"
        if not (tag in _TEXT_TAGS or el.role == "textbox") or typ in _NOT_TEXT_TYPES:
            return "deny:not_a_text_field"
        if _is_payment(el):
            return "confirm:payment_field"
        if submit:
            return "confirm:submit"
        if page.has_password or page.has_payment:
            return "confirm:sensitive_page"
        return "allow"
    if action != "click":
        return "deny:unknown_action"
    if _is_password(el) or typ == "file":
        return "deny:password_field"
    if page.has_password or page.has_payment or _is_payment(el):
        return "confirm:sensitive_page"
    submits = (tag == "button" and typ in ("", "submit")) or (tag == "input" and typ in ("submit", "image"))
    if el.in_form and submits:
        return "confirm:submit"
    if has_effect_verb(el.name):
        return "confirm:effect_verb"
    if tag == "a" and el.href is not None:
        if not el.href.startswith(("http://", "https://")):
            return "confirm:non_http_link"
        hh = _href_host(el.href)
        if hh is None or site(hh) != site(page.host):
            return "confirm:cross_domain"
    return "allow"


def element_from_snapshot(item: dict) -> Element:
    return Element(tag=str(item.get("tag") or ""), typ=str(item.get("type") or ""),
                   role=str(item.get("role") or ""), name=str(item.get("name") or ""),
                   autocomplete=str(item.get("ac") or ""), in_form=bool(item.get("in_form")),
                   href=item.get("href") if isinstance(item.get("href"), str) else None,
                   payment=bool(item.get("payment")))
