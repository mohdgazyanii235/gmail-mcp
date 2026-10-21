"""Pure logic for the new-mail watcher. Standard library only, so it can be tested anywhere.

The watcher listens for Gmail push notifications (via Pub/Sub), works out which new inbox messages came from
a small list of exact sender addresses, and tells Jarvis about them. Nothing here can send, draft or delete.
"""

import base64
import json
import os
import re
import tempfile
import threading
from email.utils import parseaddr

MAX_HANDLED = 1000

DEFAULT_SENDERS = {
    "tahir@skggroup.co.uk": "Tahir",
    "salesfan@sanyangwood.cn": "Fan",
    "gltrd@eim.ae": "your dad",
    "mohdgazyanii235@gmail.com": "you (test)",
}


def parse_push(data) -> tuple:
    """Decode a Gmail push payload: base64 JSON {emailAddress, historyId}. Returns (email, history_id)."""
    if isinstance(data, str):
        data = data.encode()
    try:
        obj = json.loads(data)
    except ValueError:
        obj = json.loads(base64.b64decode(data))
    email = str(obj.get("emailAddress", "")).strip().lower()
    hid = int(obj.get("historyId", 0))
    if not email or not hid:
        raise ValueError("Push message has no emailAddress or historyId.")
    return email, hid


def sender_name(from_header: str, senders: dict):
    """Exact-address match only. The display name is never used, because anyone can put 'Tahir' in it."""
    addr = parseaddr(from_header or "")[1].strip().lower()
    return senders.get(addr)


def sender_address(from_header: str) -> str:
    return parseaddr(from_header or "")[1].strip().lower()


def _domain(addr: str) -> str:
    return addr.rpartition("@")[2].lower()


def auth_verified(auth_results: list, from_addr: str) -> bool:
    """True when Gmail's own Authentication-Results header says the mail really came from the From domain.

    Accepts dmarc=pass, or dkim=pass signed by the From domain (or a parent of it). Anything else is unverified.
    Gmail writes these headers itself; the first one (added by Gmail) is the one that counts.
    """
    domain = _domain(from_addr)
    if not domain:
        return False
    for value in auth_results[:1]:
        text = (value or "").lower()
        if re.search(r"\bdmarc=pass\b", text):
            return True
        for m in re.finditer(r"\bdkim=pass\b[^;]*", text):
            d = re.search(r"header\.d=([a-z0-9.\-]+)", m.group(0))
            if d and (domain == d.group(1) or domain.endswith("." + d.group(1))):
                return True
    return False


def new_inbox_ids(history_response: dict) -> list:
    """Message ids added to the INBOX in a history.list response, oldest first, no repeats."""
    seen, out = set(), []
    for rec in history_response.get("history", []) or []:
        for added in rec.get("messagesAdded", []) or []:
            msg = added.get("message", {})
            mid = msg.get("id")
            if mid and mid not in seen and "INBOX" in (msg.get("labelIds") or []):
                seen.add(mid)
                out.append(mid)
    return out


class State:
    """Small JSON file: per account the last history id, ids already handled, and notices Jarvis has not accepted yet."""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.data = {"accounts": {}}
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    def _acc(self, alias):
        return self.data["accounts"].setdefault(alias, {"history_id": 0, "handled": [], "pending": []})

    def save(self):
        with self.lock:
            d = os.path.dirname(self.path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)

    def known(self, alias) -> bool:
        return alias in self.data["accounts"] and self.data["accounts"][alias]["history_id"] > 0

    def history_id(self, alias) -> int:
        return int(self._acc(alias)["history_id"])

    def set_history_id(self, alias, hid):
        with self.lock:
            a = self._acc(alias)
            a["history_id"] = max(int(a["history_id"]), int(hid))

    def is_handled(self, alias, mid) -> bool:
        return mid in self._acc(alias)["handled"]

    def mark_handled(self, alias, mid):
        with self.lock:
            a = self._acc(alias)
            if mid not in a["handled"]:
                a["handled"].append(mid)
                del a["handled"][:-MAX_HANDLED]

    def add_pending(self, alias, notice):
        with self.lock:
            a = self._acc(alias)
            if all(p["message_id"] != notice["message_id"] for p in a["pending"]):
                a["pending"].append(notice)

    def pending(self, alias) -> list:
        return list(self._acc(alias)["pending"])

    def clear_pending(self, alias, mid):
        with self.lock:
            a = self._acc(alias)
            a["pending"] = [p for p in a["pending"] if p["message_id"] != mid]
