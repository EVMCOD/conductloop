"""Linter for agent-proposed skills (PR L6, design doc §3.5).

Strict *writer* rules (the reader in validate.py is tolerant). Codes marked
``block`` stop approval until the user edits the proposal; ``warn`` codes are
advisory. Heuristic by design: false positives are acceptable, the user can
always edit the text and re-lint.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .validate import MAX_BODY_LINES, MAX_BODY_TOKENS, MAX_COMPATIBILITY, MAX_DESCRIPTION, MAX_NAME, NAME_RE

MAX_FILES = 10
MAX_FILE_BYTES = 64 * 1024
MAX_CONTENT_BYTES = 128 * 1024
REF_PATH_RE = re.compile(r"^references/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.(md|txt)$")
WRITER_FIELDS = ("name", "description", "license", "compatibility", "metadata")

# ⛔ secrets (common key shapes + "key = value" assignments with a long value)
_SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:sk|rk|pk)_live_[A-Za-z0-9]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|contraseña)\b[\"']?\s*[:=]\s*[\"']?"
               r"(?!<|\$\{|\$[A-Z_]|your|tu_|xxx|\.\.\.)[A-Za-z0-9_+/=.\-]{12,}"),
]
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/)(?!<|\{|\$|you|user|usuario|me/|name)[A-Za-z0-9._-]+|"
                           r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_EMAIL_OK = re.compile(r"@(example\.(com|org|net)|ejemplo\.(com|es))$", re.IGNORECASE)
# ⛔ obvious prompt injection / hidden text / exfiltration
_INJECTION_RES = [
    re.compile(r"(?i)\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|earlier|all|system)\b"
               r".{0,20}\b(instructions?|rules|prompt|messages?)\b"),
    re.compile(r"(?i)\b(ignora|olvida|descarta)\b.{0,30}\b(instrucciones|reglas|indicaciones)\b"),
    re.compile(r"(?i)\byou are now\b|\bahora eres\b|\bnew system prompt\b|\bnuevo prompt de sistema\b"),
    re.compile(r"(?i)\b(do not|don't|never|no)\b.{0,20}\b(tell|inform|show|mention|avises|digas|muestres)\b"
               r".{0,20}\b(the )?(user|usuario)\b"),
]
_HIDDEN_RES = [
    re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]"),
    re.compile(r"<!--"),
    re.compile(r"(?i)<\s*(script|iframe|object|embed)\b|<[a-z][^>]*\son[a-z]+\s*="),
    re.compile(r"(?i)style\s*=\s*[\"'][^\"']*(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0)"),
]
_EXFIL_RES = [
    re.compile(r"(?i)\b(curl|wget|iwr|invoke-webrequest)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|fi|k)?sh\b"),
    re.compile(r"(?i)\b(curl|wget)\b[^\n]*\s(-d|--data[a-z-]*|-F|--form|-T|--upload-file|--post-(data|file))\b"),
    re.compile(r"(?i)\bbase64\s+(-d|--decode)\b[^\n]*\|\s*(ba|z)?sh\b"),
    re.compile(r"(?i)\beval\s+\"?\$\((curl|wget)"),
    re.compile(r"(?i)\bnc\b[^\n]*\s-e\s"),
    re.compile(r"(?i)\b(send|post|upload|exfiltrate|envía|envia|manda|sube)\b.{0,40}"
               r"\b(keys?|secrets?|tokens?|credentials?|passwords?|claves?|credenciales|contraseñas?|\.env|ssh)\b"
               r".{0,40}\bhttps?://"),
]
_INCIDENT_LINE = re.compile(r"^\s*(\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[(ERROR|WARN|INFO)\]|Traceback \(most recent|"
                            r"\d{2}:\d{2}:\d{2}\s)")
_REF_LINK = re.compile(r"(?:\]\(|`|\s)(references/[^\s)`]+)")


@dataclass(frozen=True)
class Finding:
    level: str  # "block" | "warn"
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _b(code: str, msg: str) -> Finding:
    return Finding("block", code, msg)


def _w(code: str, msg: str) -> Finding:
    return Finding("warn", code, msg)


def blocking(findings: list[dict[str, str]] | list[Finding]) -> bool:
    return any((f.level if isinstance(f, Finding) else f.get("level")) == "block" for f in findings)


def find_secrets(text: str) -> list[str]:
    return [m.group(0) for rx in _SECRET_RES for m in rx.finditer(text)]


def redact(text: str) -> str:
    for rx in _SECRET_RES:
        text = rx.sub("[REDACTED]", text)
    return text


def scan_text(text: str, where: str) -> list[Finding]:
    """Security checks shared by SKILL.md and reference files."""
    out: list[Finding] = []
    if find_secrets(text):
        out.append(_b("secret", f"{where}: looks like a secret (API key, token, private key)"))
    homes = {m.group(0) for m in _HOME_PATH_RE.finditer(text)}
    if homes:
        out.append(_b("user_path", f"{where}: absolute path of a user's home ({sorted(homes)[0]}…); use ~ or a placeholder"))
    emails = {m.group(0) for m in _EMAIL_RE.finditer(text) if not _EMAIL_OK.search(m.group(0))}
    if emails:
        out.append(_b("email", f"{where}: contains an email address; use a placeholder"))
    if any(rx.search(text) for rx in _INJECTION_RES):
        out.append(_b("injection", f"{where}: looks like an instruction to override other instructions"))
    if any(rx.search(text) for rx in _HIDDEN_RES):
        out.append(_b("hidden_text", f"{where}: hidden text (HTML comment, invisible unicode or hidden style)"))
    if any(rx.search(text) for rx in _EXFIL_RES):
        out.append(_b("exfiltration", f"{where}: pipes a download into a shell or sends data to a URL"))
    return out


def lint(content: str | None, files: dict[str, Any] | None, skill_name: str,
         *, other_descriptions: dict[str, str] | None = None) -> tuple[dict[str, Any] | None, str, list[Finding]]:
    """Lint a proposed SKILL.md. Returns (frontmatter, body, findings).
    ``frontmatter`` is None when it cannot be parsed."""
    from .parse import parse_skill_text  # lazy: PyYAML
    findings: list[Finding] = []
    if not NAME_RE.match(skill_name or "") or len(skill_name) > MAX_NAME:
        findings.append(_b("name_invalid", "skill name must be 1-64 lowercase letters, digits and single hyphens"))
    content = content or ""
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return None, "", findings + [_b("too_large", "SKILL.md is too large")]
    parsed = parse_skill_text(content)
    fm, body = parsed.frontmatter, parsed.body
    if fm is None or parsed.errors:
        findings.append(_b("frontmatter", "SKILL.md must start with valid YAML frontmatter between '---' lines"))
    else:
        name = fm.get("name")
        if name != skill_name:
            findings.append(_b("name_mismatch", f"frontmatter 'name' must be '{skill_name}' (the directory name)"))
        desc = fm.get("description")
        if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= MAX_DESCRIPTION:
            findings.append(_b("description", f"'description' must be 1-{MAX_DESCRIPTION} characters"))
        comp = fm.get("compatibility")
        if comp is not None and (not isinstance(comp, str) or not 1 <= len(comp) <= MAX_COMPATIBILITY):
            findings.append(_b("compatibility", f"'compatibility' must be a 1-{MAX_COMPATIBILITY} char string"))
        meta = fm.get("metadata")
        if meta is not None and not (isinstance(meta, dict)
                                     and all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items())):
            findings.append(_b("metadata", "'metadata' must be a map of strings to strings"))
        if "allowed-tools" in fm:
            findings.append(_w("allowed_tools_removed",
                               "'allowed-tools' is removed from agent proposals and never grants tools"))
        extra = [k for k in fm if k not in WRITER_FIELDS and k != "allowed-tools"]
        if extra:
            findings.append(_w("extra_fields", f"fields not in the specification are dropped: {', '.join(map(str, extra))}"))
        if not body.strip():
            findings.append(_b("empty_body", "the body (instructions) is empty"))
    findings += scan_text(content, "SKILL.md")

    files = files or {}
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        findings.append(_b("files", f"at most {MAX_FILES} reference files"))
        files = {}
    for rel, text in files.items():
        if not isinstance(rel, str) or not REF_PATH_RE.match(rel):
            findings.append(_b("file_path", f"only references/<file>.md|.txt (one level) are allowed: {str(rel)[:80]}"))
            continue
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FILE_BYTES:
            findings.append(_b("file_size", f"{rel}: must be text under {MAX_FILE_BYTES // 1024} KB"))
            continue
        findings += scan_text(text, rel)

    # ⚠️ advisory
    lines = body.count("\n") + 1 if body else 0
    if lines > MAX_BODY_LINES:
        findings.append(_w("body_lines", f"body has {lines} lines (spec recommends < {MAX_BODY_LINES})"))
    if len(body) / 4 > MAX_BODY_TOKENS:
        findings.append(_w("body_tokens", f"body is ~{len(body) // 4} tokens (spec recommends < {MAX_BODY_TOKENS})"))
    for ref in _REF_LINK.findall(body):
        if ref.count("/") > 1:
            findings.append(_w("nested_reference", f"references should be one level deep: {ref[:80]}"))
            break
    if sum(1 for ln in body.splitlines() if _INCIDENT_LINE.match(ln)) >= 5:
        findings.append(_w("incident_log", "looks like a log of what happened; write the lesson (procedure, pitfalls, checks)"))
    if fm is not None and isinstance(fm.get("description"), str) and other_descriptions:
        mine = _words(fm["description"])
        for other, desc in other_descriptions.items():
            if other == skill_name:
                continue
            theirs = _words(desc)
            if mine and theirs and len(mine & theirs) / len(mine | theirs) >= 0.6:
                findings.append(_w("duplicate", f"very similar to the existing skill '{other}'"))
                break
    return fm, body, findings


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-záéíóúñü0-9]{3,}", text.lower())}
