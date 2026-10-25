"""Local mail bridge for Command Pi. Run with:  .venv/bin/python bridge.py   (installed by install-bridge.sh)

Command Pi's scripts have no network and no Google logins. They ask Command Pi's broker, which calls this service on
127.0.0.1 with BRIDGE_TOKEN. This service holds the logins, and offers only: list accounts, search, read, list or create
folders, mark read/unread (one message or a whole search), move (one message or a search), and undo the last change.
There is deliberately no send, no draft and no delete or bin, and every change is recorded so it can be undone.

Settings (.env): BRIDGE_TOKEN (required), BRIDGE_PORT (default 8767), BRIDGE_ACCOUNTS (optional comma list; default all).
"""

import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import accounts
import mailutil
import organise

log = logging.getLogger("bridge")
HISTORY = organise.History(accounts.BASE / "state" / "organise.json")
MAX_BODY = 20000


def allowed_accounts() -> list:
    wanted = [a.strip() for a in os.environ.get("BRIDGE_ACCOUNTS", "").split(",") if a.strip()]
    have = accounts.list_aliases()
    return [a for a in have if not wanted or a in wanted]


def _summary(msg: dict) -> dict:
    h = msg.get("payload", {}).get("headers", [])
    return {"id": msg["id"], "thread_id": msg.get("threadId", ""), "from": mailutil.header(h, "From"),
            "to": mailutil.header(h, "To"), "subject": mailutil.header(h, "Subject"), "date": mailutil.header(h, "Date"),
            "snippet": msg.get("snippet", ""), "unread": "UNREAD" in msg.get("labelIds", [])}


def _user_labels(svc) -> dict:
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    return {l["name"].lower(): l["id"] for l in labels if l.get("type") == "user"}


def _ids_or_query(body: dict):
    if body.get("message_ids"):
        return organise.check_ids(body["message_ids"]), None
    return None, str(body.get("query", "")).strip()


def run(op: str, body: dict, get_service=accounts.get_service, history=HISTORY, allowed=allowed_accounts) -> dict:
    """The whole menu. Raises ValueError with a plain message for anything not allowed."""
    if op == "accounts":
        out = []
        for a in allowed():
            try:
                out.append({"account": a, "email": accounts.read_info(a).get("email", "")})
            except Exception:  # noqa: BLE001
                out.append({"account": a, "email": "(unreadable)"})
        return {"accounts": out}
    account = str(body.get("account", "")).strip().lower()
    if account not in allowed():
        raise ValueError(f"Unknown account '{account}'. Available: {', '.join(allowed()) or 'none'}.")
    svc, _ = get_service(account)
    if op == "search":
        n = max(1, min(int(body.get("max_results", 10)), 50))
        res = svc.users().messages().list(userId="me", q=str(body.get("query", ""))[:300], maxResults=n).execute()
        return {"messages": [_summary(svc.users().messages().get(
            userId="me", id=r["id"], format="metadata", metadataHeaders=["From", "To", "Subject", "Date"]).execute())
            for r in res.get("messages", [])]}
    if op == "read":
        mid = organise.check_ids(body.get("message_id"))[0]
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = msg.get("payload", {})
        text, _names = mailutil.extract_body_and_attachments(payload)
        limit = max(500, min(int(body.get("max_chars", 20000)), 100000))
        h = payload.get("headers", [])
        return {"id": msg["id"], "from": mailutil.header(h, "From"), "to": mailutil.header(h, "To"),
                "subject": mailutil.header(h, "Subject"), "date": mailutil.header(h, "Date"),
                "labels": msg.get("labelIds", []), "body": text[:limit], "body_truncated": len(text) > limit}
    if op == "labels":
        return {"folders": sorted(l["name"] for l in svc.users().labels().list(userId="me").execute().get("labels", [])
                                  if l.get("type") == "user")}
    if op == "create_label":
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 100 or any(ord(c) < 32 for c in name) or \
                name.lower() in organise.REFUSED_WORDS | organise.SPAM_WORDS | {"inbox", "archive"}:
            raise ValueError("That is not a usable folder name.")
        if name.lower() in _user_labels(svc):
            return {"status": "already exists", "folder": name}
        svc.users().labels().create(userId="me", body={
            "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
        return {"status": "created", "folder": name}
    if op in ("mark_read", "mark_unread"):
        read = op == "mark_read"
        ids, query = _ids_or_query(body)
        if ids:
            add, remove = ([], ["UNREAD"]) if read else (["UNREAD"], [])
            return organise.apply_changes(svc, history, account, "mark read" if read else "mark unread", ids, add, remove)
        return organise.bulk_mark_read(svc, history, account, query or "in:inbox", read=read)
    if op == "move":
        add, remove, nice = organise.resolve_target(str(body.get("to_folder", "")), _user_labels(svc))
        ids, query = _ids_or_query(body)
        if ids:
            return organise.apply_changes(svc, history, account, f"move to {nice}", ids, add, remove)
        return organise.bulk_move(svc, history, account, query, add, remove, nice)
    if op == "undo":
        return organise.undo_last(svc, history, account)
    raise ValueError("Unknown mail operation.")


def make_handler(token: str, runner=run):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()):
                return self._send(403, {"error": "forbidden"})
            op = self.path.strip("/").removeprefix("mail/")
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), MAX_BODY)) or b"{}")
                result = runner(op, body)
                log.info("%s %s -> ok", op, body.get("account", ""))
                return self._send(200, {"ok": True, "result": result})
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001 - Google errors, bad json, etc.
                log.warning("%s failed: %s", op, e)
                return self._send(502, {"ok": False, "error": f"Gmail error: {str(e)[:200]}"})
    return H


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    token = os.environ.get("BRIDGE_TOKEN", "")
    if len(token) < 32:
        sys.exit("Set BRIDGE_TOKEN (32+ random characters) in .env.")
    port = int(os.environ.get("BRIDGE_PORT", "8767"))
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(token)).serve_forever()


if __name__ == "__main__":
    main()
