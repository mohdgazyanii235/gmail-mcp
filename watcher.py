"""New-mail watcher. Run with:  .venv/bin/python watcher.py   (installed as the gmail-watcher service by install-watcher.sh)

Gmail pushes "something changed" notes to a Pub/Sub topic. This process pulls them, finds new inbox messages from a
short list of exact sender addresses, and POSTs a small notice to Jarvis. It reads headers only and cannot send,
draft or delete anything. Jarvis fetches the message itself through the Gmail MCP server.

Settings (from .env): WATCH_ACCOUNTS (default "sales"), WATCH_TOPIC (projects/<id>/topics/<topic>),
WATCH_SUBSCRIPTION (projects/<id>/subscriptions/<name>), PUBSUB_KEY (service account JSON path),
JARVIS_NOTIFY_URL (default http://127.0.0.1:8000/api/new-mail), JARVIS_NOTIFY_TOKEN, WATCH_CATCHUP_SECONDS (900),
WATCH_RENEW_SECONDS (43200), WATCH_SENDERS (optional JSON file of {"address": "spoken name"}).
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import accounts
import watchlogic as wl

log = logging.getLogger("watcher")

ACCOUNTS = [a.strip() for a in os.environ.get("WATCH_ACCOUNTS", "sales").split(",") if a.strip()]
TOPIC = os.environ.get("WATCH_TOPIC", "")
SUBSCRIPTION = os.environ.get("WATCH_SUBSCRIPTION", "")
KEY = os.environ.get("PUBSUB_KEY", str(accounts.BASE / "pubsub-key.json"))
NOTIFY_URL = os.environ.get("JARVIS_NOTIFY_URL", "http://127.0.0.1:8000/api/new-mail")
NOTIFY_TOKEN = os.environ.get("JARVIS_NOTIFY_TOKEN", "")
CATCHUP = int(os.environ.get("WATCH_CATCHUP_SECONDS", "900"))
RENEW = int(os.environ.get("WATCH_RENEW_SECONDS", "43200"))
STATE = wl.State(accounts.BASE / "state" / "watch.json")
LOCKS = {a: threading.Lock() for a in ACCOUNTS}


def load_senders() -> dict:
    path = os.environ.get("WATCH_SENDERS", "")
    if path:
        with open(path) as f:
            return {k.strip().lower(): v for k, v in json.load(f).items()}
    return dict(wl.DEFAULT_SENDERS)


SENDERS = load_senders()


def _headers_of(msg):
    return msg.get("payload", {}).get("headers", [])


def describe(svc, alias, mid):
    """Fetch headers for one message and return a notice dict if it should reach Jarvis, else None."""
    msg = svc.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["From", "Subject", "Date", "Authentication-Results"],
    ).execute()
    if "INBOX" not in msg.get("labelIds", []):
        return None
    hs = _headers_of(msg)
    frm = next((h["value"] for h in hs if h["name"].lower() == "from"), "")
    name = wl.sender_name(frm, SENDERS)
    if not name:
        return None
    addr = wl.sender_address(frm)
    auth = [h["value"] for h in hs if h["name"].lower() == "authentication-results"]
    subject = next((h["value"] for h in hs if h["name"].lower() == "subject"), "")
    date = next((h["value"] for h in hs if h["name"].lower() == "date"), "")
    return {
        "account": alias, "message_id": mid, "thread_id": msg.get("threadId", ""),
        "sender": name, "sender_address": addr, "verified": wl.auth_verified(auth, addr),
        "subject": subject[:300], "date": date,
    }


def notify(notice) -> bool:
    req = urllib.request.Request(
        NOTIFY_URL, data=json.dumps(notice).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {NOTIFY_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError) as e:
        log.warning("Jarvis not reachable yet (%s); will retry", e)
        return False


def flush_pending(alias):
    for notice in STATE.pending(alias):
        if notify(notice):
            STATE.clear_pending(alias, notice["message_id"])
            log.info("delivered queued notice for %s", notice["message_id"])
    STATE.save()


def handle_ids(svc, alias, ids):
    for mid in ids:
        if STATE.is_handled(alias, mid):
            continue
        notice = describe(svc, alias, mid)
        STATE.mark_handled(alias, mid)
        if notice:
            log.info("new mail from %s (verified=%s)", notice["sender"], notice["verified"])
            if not notify(notice):
                STATE.add_pending(alias, notice)
    STATE.save()


def start_watch(svc, alias):
    res = svc.users().watch(userId="me", body={
        "topicName": TOPIC, "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE"}).execute()
    log.info("%s: watch renewed, expires %s", alias, res.get("expiration"))
    return int(res["historyId"])


def baseline(svc, alias, hid):
    """First run: remember where we are and mark recent mail as already seen, so old emails are not announced."""
    res = svc.users().messages().list(userId="me", q="in:inbox newer_than:2d", maxResults=100).execute()
    for ref in res.get("messages", []):
        STATE.mark_handled(alias, ref["id"])
    STATE.set_history_id(alias, hid)
    STATE.save()
    log.info("%s: first run, baseline set at history %s", alias, hid)


def process_history(svc, alias):
    """Everything new since the last history id we saw."""
    from googleapiclient.errors import HttpError

    start = STATE.history_id(alias)
    ids, token, latest = [], None, start
    try:
        while True:
            kw = {"userId": "me", "startHistoryId": str(start), "historyTypes": ["messageAdded"], "labelId": "INBOX"}
            if token:
                kw["pageToken"] = token
            res = svc.users().history().list(**kw).execute()
            ids += wl.new_inbox_ids(res)
            latest = max(latest, int(res.get("historyId", latest)))
            token = res.get("nextPageToken")
            if not token:
                break
    except HttpError as e:
        if e.resp.status == 404:  # history too old: fall back to a search
            log.warning("%s: history expired, using a search instead", alias)
            ids = [r["id"] for r in svc.users().messages().list(
                userId="me", q="in:inbox newer_than:2d", maxResults=50).execute().get("messages", [])]
            latest = int(svc.users().getProfile(userId="me").execute()["historyId"])
        else:
            raise
    handle_ids(svc, alias, ids)
    STATE.set_history_id(alias, latest)
    STATE.save()


def sync(alias):
    with LOCKS[alias]:
        svc, _ = accounts.get_service(alias)
        if not STATE.known(alias):
            baseline(svc, alias, start_watch(svc, alias))
            return
        process_history(svc, alias)
        flush_pending(alias)


def on_message(message):
    try:
        email, _ = wl.parse_push(message.data)
        for alias in ACCOUNTS:
            if accounts.read_info(alias).get("email", "").lower() == email:
                sync(alias)
        message.ack()
    except Exception:
        log.exception("could not process a push message; it will be retried")
        message.nack()


def periodic():
    last_renew = {a: 0.0 for a in ACCOUNTS}
    while True:
        for alias in ACCOUNTS:
            try:
                if time.time() - last_renew[alias] >= RENEW:
                    svc, _ = accounts.get_service(alias)
                    with LOCKS[alias]:
                        hid = start_watch(svc, alias)
                        if not STATE.known(alias):
                            baseline(svc, alias, hid)
                    last_renew[alias] = time.time()
                sync(alias)  # the catch-up check, in case a push note was lost
            except Exception:
                log.exception("%s: periodic check failed", alias)
        time.sleep(CATCHUP)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (TOPIC and SUBSCRIPTION and NOTIFY_TOKEN):
        sys.exit("Set WATCH_TOPIC, WATCH_SUBSCRIPTION and JARVIS_NOTIFY_TOKEN in .env first.")
    from google.cloud import pubsub_v1

    sub = pubsub_v1.SubscriberClient.from_service_account_file(KEY)
    future = sub.subscribe(SUBSCRIPTION, callback=on_message)
    threading.Thread(target=periodic, daemon=True).start()
    log.info("watching %s for %s", ",".join(ACCOUNTS), ", ".join(sorted(SENDERS)))
    try:
        future.result()
    except KeyboardInterrupt:
        future.cancel()


if __name__ == "__main__":
    main()
