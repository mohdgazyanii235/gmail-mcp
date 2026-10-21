"""Moving and tidying email. Standard library only, so it can be tested anywhere.

Gmail has labels, not folders. "Moving" a message means adding the destination label and taking it out of the inbox.
Deliberately missing: anything that deletes or bins mail. Every change is written to a history file, so it can be undone.
"""

import json
import os
import re
import tempfile
import time

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_IDS = 25
KEEP_BATCHES = 50
SPAM_WORDS = {"spam", "junk"}
REFUSED_WORDS = {"trash", "bin", "deleted", "delete", "sent", "draft", "drafts", "chat", "scheduled", "important", "starred"}


def check_ids(ids) -> list:
    if isinstance(ids, str):
        ids = [ids]
    out = []
    for i in ids or []:
        i = str(i).strip()
        if not SAFE_ID.match(i):
            raise ValueError(f"'{i}' is not a valid message id.")
        if i not in out:
            out.append(i)
    if not out:
        raise ValueError("Give at least one message id.")
    if len(out) > MAX_IDS:
        raise ValueError(f"At most {MAX_IDS} messages can be changed in one go. Do them in smaller batches.")
    return out


def resolve_target(name: str, user_labels: dict):
    """Return (add_label_ids, remove_label_ids, spoken_name) for a destination.

    user_labels maps lower-case label name -> label id for the account's own labels.
    """
    raw = (name or "").strip()
    key = raw.lower()
    if not key:
        raise ValueError("Say where to move the mail: INBOX, SPAM, ARCHIVE or the name of a folder.")
    if key == "inbox":
        return ["INBOX"], ["SPAM"], "the inbox"
    if key in SPAM_WORDS:
        return ["SPAM"], ["INBOX"], "junk"
    if key == "archive":
        return [], ["INBOX"], "the archive"
    if key in REFUSED_WORDS:
        raise ValueError("This server cannot delete, bin or otherwise move mail there. Use SPAM, ARCHIVE, INBOX or a folder of your own.")
    if key in user_labels:
        return [user_labels[key]], ["INBOX", "SPAM"], raw
    known = ", ".join(sorted(user_labels)) or "none yet"
    raise ValueError(f"There is no folder called '{raw}'. Existing folders: {known}. Use create_label first if you want a new one.")


def diff(current: list, add: list, remove: list):
    """What would really change for a message that has these labels now."""
    return [l for l in add if l not in current], [l for l in remove if l in current]


class History:
    """The last few batches of changes per account, kept in a small JSON file so the latest can be undone."""

    def __init__(self, path):
        self.path = str(path)
        self.batches = []
        try:
            with open(self.path) as f:
                self.batches = json.load(f).get("batches", [])
        except (OSError, ValueError):
            pass

    def _save(self):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"batches": self.batches[-KEEP_BATCHES:]}, f)
        os.replace(tmp, self.path)

    def record(self, account: str, action: str, changes: list, clock=time.time) -> int:
        bid = int(clock() * 1000)
        self.batches.append({"id": bid, "ts": clock(), "account": account, "action": action,
                             "changes": changes, "undone": False})
        self._save()
        return bid

    def last(self, account: str):
        for b in reversed(self.batches):
            if b["account"] == account and not b["undone"]:
                return b
        return None

    def mark_undone(self, bid: int):
        for b in self.batches:
            if b["id"] == bid:
                b["undone"] = True
        self._save()


def _groups(changes: list, reverse: bool) -> dict:
    groups = {}
    for c in changes:
        add, rem = (c["removed"], c["added"]) if reverse else (c["added"], c["removed"])
        groups.setdefault((tuple(add), tuple(rem)), []).append(c["id"])
    return groups


def _batch(svc, groups: dict):
    for (add, rem), ids in groups.items():
        svc.users().messages().batchModify(
            userId="me", body={"ids": ids, "addLabelIds": list(add), "removeLabelIds": list(rem)}).execute()


def apply_changes(svc, history: History, account: str, action: str, ids: list, add: list, remove: list) -> dict:
    """Look at each message, change only what needs changing, and record exactly what was done."""
    changes, unchanged, shown = [], [], []
    for mid in ids:
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject"]).execute()
        added, removed = diff(msg.get("labelIds", []), add, remove)
        h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
        info = {"id": mid, "from": h.get("from", ""), "subject": h.get("subject", "")}
        if added or removed:
            changes.append({"id": mid, "added": added, "removed": removed})
            shown.append(info)
        else:
            unchanged.append(info)
    _batch(svc, _groups(changes, reverse=False))
    if changes:
        history.record(account, action, changes)
    return {"status": "done", "action": action, "changed": shown, "already_that_way": unchanged}


def undo_last(svc, history: History, account: str) -> dict:
    batch = history.last(account)
    if not batch:
        return {"status": "nothing to undo"}
    _batch(svc, _groups(batch["changes"], reverse=True))
    history.mark_undone(batch["id"])
    return {"status": "undone", "action": batch["action"], "messages": len(batch["changes"])}
