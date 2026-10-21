"""Gmail MCP server for several Google accounts, behind OAuth sign-in (password + authenticator code).

Run with:  uvicorn server:app --host 127.0.0.1 --port 8765 --no-access-log
Configuration comes from environment variables (see .env, loaded by systemd).
"""

import functools
import json
import os
import time
from typing import Optional

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import accounts
import mailutil
import oauth

AUTH = oauth.Config.from_env(accounts.BASE)  # exits with a clear message if PUBLIC_HOSTNAME is missing
PUBLIC_HOSTNAME = AUTH.public_url.removeprefix("https://")
ALLOW_SEND = os.environ.get("ALLOW_SEND", "true").strip().lower() in ("1", "true", "yes")
SENT_LOG = accounts.BASE / "sent.log"

# --- MCP server -------------------------------------------------------------

_kwargs = {}
try:
    from mcp.server.transport_security import TransportSecuritySettings

    _hosts = ["127.0.0.1:*", "localhost:*"]
    if PUBLIC_HOSTNAME:
        _hosts += [PUBLIC_HOSTNAME, PUBLIC_HOSTNAME + ":*"]
    _kwargs["transport_security"] = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=_hosts
    )
except ImportError:  # older mcp releases have no host check at all
    pass

mcp = FastMCP("gmail", stateless_http=True, json_response=True, **_kwargs)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
DRAFT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def friendly(fn):
    """Turn Google errors into short messages the model (and you) can act on."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HttpError as e:
            raise RuntimeError(f"Gmail API error {e.resp.status}: {e._get_reason()}") from None
        except RefreshError:
            raise RuntimeError(
                "Google rejected the saved login for this account. On the Pi, run: "
                ".venv/bin/python manage_accounts.py add <account-name>"
            ) from None

    return wrapper


def _require_send():
    if not ALLOW_SEND:
        raise RuntimeError("Sending is switched off on this server (ALLOW_SEND is false in .env). Create a draft instead.")


def _log_send(account: str, to, subject: str, thread_id: str = ""):
    try:
        with open(SENT_LOG, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "account": account,
                                "to": to, "subject": subject, "thread": thread_id}) + "\n")
    except OSError:
        pass


def _summary(msg: dict) -> dict:
    h = msg.get("payload", {}).get("headers", [])
    labels = msg.get("labelIds", [])
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "from": mailutil.header(h, "From"),
        "to": mailutil.header(h, "To"),
        "subject": mailutil.header(h, "Subject"),
        "date": mailutil.header(h, "Date"),
        "snippet": msg.get("snippet", ""),
        "unread": "UNREAD" in labels,
    }


def _trim(text: str, limit: int):
    limit = max(500, min(int(limit), 100000))
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _gather_attachments(svc, attachments, forward_attachments):
    """Collect (filename, mime_type, bytes) from base64 uploads and from files already in someone's mailbox."""
    out = [mailutil.decode_attachment_input(a) for a in (attachments or [])]
    by_message = {}
    for ref in forward_attachments or []:
        mid = str((ref or {}).get("message_id") or "").strip()
        name = str((ref or {}).get("filename") or "").strip()
        if not mid or not name:
            raise ValueError("Each forward_attachments entry needs message_id and filename (as shown by read_message).")
        if mid not in by_message:
            msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
            by_message[mid] = mailutil.list_attachment_parts(msg.get("payload", {}))
        match = [p for p in by_message[mid] if p["filename"] == name]
        if not match:
            raise ValueError(f"No attachment named '{name}' on message {mid}.")
        part = match[0]
        if part["size"] > mailutil.MAX_ATTACHMENT_BYTES:
            raise ValueError(f"'{name}' is too large to forward.")
        att = svc.users().messages().attachments().get(userId="me", messageId=mid, id=part["attachment_id"]).execute()
        out.append((part["filename"], part["mime_type"], mailutil.b64url_decode(att["data"])))
    return out


def _attach_note(items) -> str:
    return ", ".join(f"{n} ({len(d) // 1024} KB)" for n, _, d in items)


def _prepare_reply(svc, my_email: str, message_id: str, body: str, reply_all: bool, cc, files=None):
    orig = svc.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References"],
    ).execute()
    h = orig.get("payload", {}).get("headers", [])
    to, auto_cc = mailutil.reply_recipients(h, my_email, reply_all)
    cc_all = list(auto_cc) + list(cc or [])
    subject = mailutil.reply_subject(mailutil.header(h, "Subject"))
    raw = mailutil.build_raw_message(
        to=to, subject=subject, body=body, cc=cc_all or None,
        in_reply_to=mailutil.header(h, "Message-ID"), references=mailutil.header(h, "References"),
        attachments=files,
    )
    return raw, orig.get("threadId", ""), to, cc_all, subject


# --- Tools: reading ----------------------------------------------------------

@mcp.tool(annotations=READ)
def list_accounts() -> list[dict]:
    """List the Gmail accounts this server can use. Always call this first and pass the 'account' name to other tools."""
    out = []
    for alias in accounts.list_aliases():
        try:
            email = accounts.read_info(alias).get("email", "")
        except Exception:
            email = "(unreadable)"
        out.append({"account": alias, "email": email})
    return out


@mcp.tool(annotations=READ)
@friendly
def search_messages(account: str, query: str = "", max_results: int = 10) -> list[dict]:
    """Search one mailbox with Gmail search syntax, newest first. Examples: 'from:gemma newer_than:30d', 'is:unread in:inbox',
    'subject:invoice has:attachment', 'Lloyds International'. Returns summaries only; call read_message for the full text."""
    svc, _ = accounts.get_service(account)
    max_results = max(1, min(int(max_results), 50))
    res = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    out = []
    for ref in res.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        out.append(_summary(msg))
    return out


@mcp.tool(annotations=READ)
@friendly
def read_message(account: str, message_id: str, max_chars: int = 20000) -> dict:
    """Read one message in full (text body, headers, attachments listed with file name, type and size). Attachment contents
    are not returned, but you can re-send one with forward_attachments on the send and draft tools."""
    svc, _ = accounts.get_service(account)
    msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload", {})
    h = payload.get("headers", [])
    text, _names = mailutil.extract_body_and_attachments(payload)
    attachments = mailutil.list_attachment_parts(payload)
    text, truncated = _trim(text, max_chars)
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "from": mailutil.header(h, "From"),
        "to": mailutil.header(h, "To"),
        "cc": mailutil.header(h, "Cc"),
        "subject": mailutil.header(h, "Subject"),
        "date": mailutil.header(h, "Date"),
        "labels": msg.get("labelIds", []),
        "attachments": attachments,
        "body": text,
        "body_truncated": truncated,
    }


@mcp.tool(annotations=READ)
@friendly
def read_thread(account: str, thread_id: str, max_chars_per_message: int = 4000) -> dict:
    """Read every message in a conversation, oldest first, each trimmed to max_chars_per_message."""
    svc, _ = accounts.get_service(account)
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = []
    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        h = payload.get("headers", [])
        text, attachments = mailutil.extract_body_and_attachments(payload)
        text, truncated = _trim(text, max_chars_per_message)
        messages.append({
            "id": msg["id"],
            "from": mailutil.header(h, "From"),
            "to": mailutil.header(h, "To"),
            "date": mailutil.header(h, "Date"),
            "subject": mailutil.header(h, "Subject"),
            "attachments": attachments,
            "body": text,
            "body_truncated": truncated,
        })
    return {"thread_id": thread_id, "messages": messages}


# --- Tools: drafts (safe, nothing leaves the mailbox) ------------------------

@mcp.tool(annotations=DRAFT)
@friendly
def create_draft(account: str, to: list[str], subject: str, body: str,
                 cc: Optional[list[str]] = None, bcc: Optional[list[str]] = None,
                 attachments: Optional[list[dict]] = None, forward_attachments: Optional[list[dict]] = None) -> dict:
    """Save a new plain-text draft in the account's Drafts folder, optionally with attachments. Nothing is sent.
    Attachments: 'attachments' is a list of {filename, content_base64, mime_type (optional)} for small files you can supply
    yourself (a few hundred KB at most is practical). 'forward_attachments' is a list of {message_id, filename} to attach files that
    already sit in the account's mailbox, found with read_message; use it for anything large. Total limit about 18 MB."""
    svc, _ = accounts.get_service(account)
    files = _gather_attachments(svc, attachments, forward_attachments)
    raw = mailutil.build_raw_message(to, subject, body, cc, bcc, attachments=files)
    d = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return {"draft_id": d["id"], "message_id": d.get("message", {}).get("id", ""), "status": "draft saved, not sent",
            "attached": _attach_note(files)}


@mcp.tool(annotations=DRAFT)
@friendly
def create_reply_draft(account: str, message_id: str, body: str, reply_all: bool = False,
                       cc: Optional[list[str]] = None,
                       attachments: Optional[list[dict]] = None, forward_attachments: Optional[list[dict]] = None) -> dict:
    """Save a draft reply inside the existing conversation (same thread, 'Re:' subject, correct recipients). Nothing is sent.
    'message_id' is the id of the message being replied to. Add extra people with 'cc'.
    Attachments: 'attachments' is a list of {filename, content_base64, mime_type (optional)} for small files you can supply
    yourself (a few hundred KB at most is practical). 'forward_attachments' is a list of {message_id, filename} to attach files that
    already sit in the account's mailbox, found with read_message; use it for anything large. Total limit about 18 MB."""
    svc, email = accounts.get_service(account)
    files = _gather_attachments(svc, attachments, forward_attachments)
    raw, thread_id, to, cc_all, subject = _prepare_reply(svc, email, message_id, body, reply_all, cc, files)
    d = svc.users().drafts().create(userId="me", body={"message": {"raw": raw, "threadId": thread_id}}).execute()
    return {"draft_id": d["id"], "thread_id": thread_id, "to": to, "cc": cc_all, "subject": subject,
            "status": "draft saved, not sent", "attached": _attach_note(files)}


# --- Tools: sending ----------------------------------------------------------

@mcp.tool(annotations=SEND)
@friendly
def send_message(account: str, to: list[str], subject: str, body: str,
                 cc: Optional[list[str]] = None, bcc: Optional[list[str]] = None,
                 attachments: Optional[list[dict]] = None, forward_attachments: Optional[list[dict]] = None) -> dict:
    """SEND a new plain-text email from the chosen account, optionally with attachments. This cannot be undone. Only use when the
    user has clearly asked to send.
    Attachments: 'attachments' is a list of {filename, content_base64, mime_type (optional)} for small files you can supply
    yourself (a few hundred KB at most is practical). 'forward_attachments' is a list of {message_id, filename} to attach files that
    already sit in the account's mailbox, found with read_message; use it for anything large. Total limit about 18 MB."""
    _require_send()
    svc, _ = accounts.get_service(account)
    files = _gather_attachments(svc, attachments, forward_attachments)
    raw = mailutil.build_raw_message(to, subject, body, cc, bcc, attachments=files)
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    _log_send(account, to, subject, sent.get("threadId", ""))
    return {"status": "sent", "message_id": sent["id"], "thread_id": sent.get("threadId", ""), "attached": _attach_note(files)}


@mcp.tool(annotations=SEND)
@friendly
def send_reply(account: str, message_id: str, body: str, reply_all: bool = False,
               cc: Optional[list[str]] = None,
               attachments: Optional[list[dict]] = None, forward_attachments: Optional[list[dict]] = None) -> dict:
    """SEND a reply inside the existing conversation, optionally with attachments. This cannot be undone. Only use when the user has
    clearly asked to send. 'message_id' is the id of the message being replied to.
    Attachments: 'attachments' is a list of {filename, content_base64, mime_type (optional)} for small files you can supply
    yourself (a few hundred KB at most is practical). 'forward_attachments' is a list of {message_id, filename} to attach files that
    already sit in the account's mailbox, found with read_message; use it for anything large. Total limit about 18 MB."""
    _require_send()
    svc, email = accounts.get_service(account)
    files = _gather_attachments(svc, attachments, forward_attachments)
    raw, thread_id, to, cc_all, subject = _prepare_reply(svc, email, message_id, body, reply_all, cc, files)
    sent = svc.users().messages().send(userId="me", body={"raw": raw, "threadId": thread_id}).execute()
    _log_send(account, to + cc_all, subject, thread_id)
    return {"status": "sent", "message_id": sent["id"], "thread_id": thread_id, "to": to, "cc": cc_all, "subject": subject,
            "attached": _attach_note(files)}


@mcp.tool(annotations=SEND)
@friendly
def send_draft(account: str, draft_id: str) -> dict:
    """SEND an existing draft exactly as saved. This cannot be undone. Only use when the user has clearly asked to send."""
    _require_send()
    svc, _ = accounts.get_service(account)
    sent = svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    _log_send(account, "(draft)", f"draft {draft_id}", sent.get("threadId", ""))
    return {"status": "sent", "message_id": sent.get("id", ""), "thread_id": sent.get("threadId", "")}


# --- ASGI app ---------------------------------------------------------------

app = oauth.OAuthGate(mcp.streamable_http_app(), AUTH)
