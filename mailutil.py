"""Pure helpers for reading and building email. Standard library only, so they can be tested anywhere."""

import base64
import mimetypes
import re
from email.message import EmailMessage
from email.utils import getaddresses, formataddr, parseaddr
from html.parser import HTMLParser


def b64url_decode(data: str) -> bytes:
    if not data:
        return b""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def header(headers: list, name: str) -> str:
    """Case-insensitive lookup in a Gmail API headers list."""
    name = name.lower()
    for h in headers or []:
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_body_and_attachments(payload: dict):
    """Walk a Gmail 'full' payload. Returns (body_text, [attachment_filenames])."""
    plain, html, attachments = [], [], []

    def walk(part):
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if filename and body.get("attachmentId"):
            attachments.append(filename)
        elif mime == "text/plain" and body.get("data"):
            plain.append(b64url_decode(body["data"]).decode("utf-8", "replace"))
        elif mime == "text/html" and body.get("data"):
            html.append(b64url_decode(body["data"]).decode("utf-8", "replace"))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload or {})
    if plain:
        text = "\n".join(plain).strip()
    elif html:
        text = html_to_text("\n".join(html))
    else:
        text = ""
    return text, attachments


def list_attachment_parts(payload: dict) -> list:
    """Every real attachment in a Gmail 'full' payload: [{filename, mime_type, size, attachment_id}]."""
    out = []

    def walk(part):
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if filename and body.get("attachmentId"):
            out.append({"filename": filename, "mime_type": part.get("mimeType", "application/octet-stream"),
                        "size": body.get("size", 0), "attachment_id": body["attachmentId"]})
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload or {})
    return out


MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024  # Gmail's limit is 25 MB for the whole encoded email


def decode_attachment_input(item: dict):
    """Turn {filename, content_base64, mime_type?} into (filename, mime_type, bytes), with checks."""
    if not isinstance(item, dict):
        raise ValueError("Each attachment must be an object with filename and content_base64.")
    filename = str(item.get("filename") or "").strip()
    if not filename or any(c in filename for c in "\r\n/\\\x00"):
        raise ValueError("Each attachment needs a plain file name (no slashes or line breaks).")
    raw = item.get("content_base64")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Attachment '{filename}' has no content_base64.")
    cleaned = re.sub(r"\s+", "", raw)
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        data = base64.b64decode(cleaned, altchars=b"-_" if ("-" in cleaned or "_" in cleaned) else None, validate=True)
    except Exception:
        raise ValueError(f"Attachment '{filename}' is not valid base64.") from None
    if not data:
        raise ValueError(f"Attachment '{filename}' is empty.")
    mime = str(item.get("mime_type") or "").strip() or (mimetypes.guess_type(filename)[0] or "application/octet-stream")
    if "/" not in mime or any(c in mime for c in "\r\n;"):
        mime = "application/octet-stream"
    return filename, mime, data


def _clean_list(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    out = []
    for v in value:
        # getaddresses understands quoted names such as "Briggs, Gemma" <g@example.com>
        for name, addr in getaddresses([v.replace(";", ",")]):
            if not addr:
                continue
            out.append(formataddr((name, addr)) if name else addr)
    return out


def _guard_header_injection(*values: str):
    for v in values:
        if v and ("\r" in v or "\n" in v):
            raise ValueError("Line breaks are not allowed in address or subject fields.")


def build_raw_message(
    to,
    subject: str,
    body: str,
    cc=None,
    bcc=None,
    in_reply_to: str = "",
    references: str = "",
    attachments=None,
) -> str:
    """Build an email (plain text, optional attachments) and return it base64url-encoded for the Gmail API.
    'attachments' is a list of (filename, mime_type, bytes)."""
    raw_addrs = []
    for v in (to, cc, bcc):
        raw_addrs += [v] if isinstance(v, str) else list(v or [])
    _guard_header_injection(subject, in_reply_to, references, *raw_addrs)
    to_l, cc_l, bcc_l = _clean_list(to), _clean_list(cc), _clean_list(bcc)
    if not to_l:
        raise ValueError("At least one recipient is required.")
    msg = EmailMessage()
    msg["To"] = ", ".join(to_l)
    if cc_l:
        msg["Cc"] = ", ".join(cc_l)
    if bcc_l:
        msg["Bcc"] = ", ".join(bcc_l)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
    msg.set_content(body)
    total = 0
    for filename, mime, data in attachments or []:
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments are too large. The total must stay under about 18 MB.")
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return b64url_encode(msg.as_bytes())


def reply_subject(subject: str) -> str:
    subject = subject or ""
    return subject if re.match(r"(?i)^\s*re:", subject) else f"Re: {subject}".strip()


def reply_recipients(headers: list, my_email: str, reply_all: bool = False):
    """Work out To and Cc for a reply. Returns (to_list, cc_list)."""
    me = my_email.lower()
    reply_to = header(headers, "Reply-To") or header(headers, "From")
    to_pairs = getaddresses([reply_to])
    to = [formataddr(p) for p in to_pairs if p[1] and p[1].lower() != me]
    cc = []
    if reply_all:
        seen = {parseaddr(t)[1].lower() for t in to} | {me}
        for p in getaddresses([header(headers, "To"), header(headers, "Cc")]):
            if p[1] and p[1].lower() not in seen:
                seen.add(p[1].lower())
                cc.append(formataddr(p))
    if not to:  # replying to a message you sent yourself: keep the original recipients
        to = [formataddr(p) for p in getaddresses([header(headers, "To")]) if p[1]]
    return to, cc
