"""Manage who can sign in to this server.

  .venv/bin/python manage_auth.py setup           set your password and authenticator app (do this once)
  .venv/bin/python manage_auth.py password        change the password
  .venv/bin/python manage_auth.py reset-2fa       set up a new authenticator code
  .venv/bin/python manage_auth.py sessions        show which apps are connected right now
  .venv/bin/python manage_auth.py revoke-all      sign every app out (Claude will ask you to sign in again)
  .venv/bin/python manage_auth.py log [N]         show the last N sign-in events (default 20)
  .venv/bin/python manage_auth.py smoke-token     make a 5-minute test token (used by smoke_test.sh)
"""

import getpass
import os
import sys
import time
import urllib.parse

import accounts
import oauth

AUTH_FILE = accounts.BASE / "auth.json"
DB_FILE = accounts.BASE / "auth.db"
LOG_FILE = accounts.BASE / "auth.log"
MIN_PASSWORD = 12


def ask_password() -> str:
    while True:
        pw = getpass.getpass("New password (12+ characters, a few random words is good): ")
        if len(pw) < MIN_PASSWORD:
            print(f"Too short. Use at least {MIN_PASSWORD} characters.")
            continue
        if pw != getpass.getpass("Type it again: "):
            print("They did not match. Try again.")
            continue
        return pw


def enrol_totp() -> str:
    secret = oauth.new_totp_secret()
    label = urllib.parse.quote("Pi Gmail")
    uri = f"otpauth://totp/{label}:owner?secret={secret}&issuer={label}&digits=6&period=30"
    print("\nAdd this to an authenticator app on your phone (Google Authenticator, Authy, Microsoft Authenticator,")
    print("1Password, Apple Passwords...). Choose 'enter a setup key' / 'manual entry' and type this key:\n")
    print("    " + " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
    print("\nIf your app can open links, this is the same thing as one link:\n    " + uri)
    print("\nKeep the key somewhere safe (a password manager) in case you lose your phone.\n")
    while True:
        code = input("Now type the 6-digit code your app shows, to prove it works: ").strip()
        if oauth.totp_match(secret, code, time.time()) is not None:
            return secret
        print("That code did not match. Check the key was typed correctly and your phone's clock is automatic.")


def load() -> dict:
    return oauth.load_owner(AUTH_FILE) or {}


def cmd_setup(argv):
    if load():
        if input("A password and authenticator are already set. Replace both? [y/N] ").strip().lower() != "y":
            return 1
    password = ask_password()
    secret = enrol_totp()
    oauth.save_owner(AUTH_FILE, {"password_hash": oauth.hash_password(password), "totp_secret": secret})
    print("\nDone. Sign-in is now protected by your password and authenticator code.")
    return 0


def cmd_password(argv):
    owner = load()
    if not owner:
        print("Nothing set up yet. Run: manage_auth.py setup")
        return 1
    owner["password_hash"] = oauth.hash_password(ask_password())
    oauth.save_owner(AUTH_FILE, owner)
    print("Password changed. Existing connections keep working; run 'revoke-all' to sign everything out.")
    return 0


def cmd_reset_2fa(argv):
    owner = load()
    if not owner:
        print("Nothing set up yet. Run: manage_auth.py setup")
        return 1
    owner["totp_secret"] = enrol_totp()
    oauth.save_owner(AUTH_FILE, owner)
    print("New authenticator saved. The old one no longer works.")
    return 0


def store() -> oauth.Store:
    return oauth.Store(DB_FILE, time.time)


def when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def cmd_sessions(argv):
    s = store()
    s.purge()
    rows = s.db.execute("""SELECT c.name AS name, t.client_id AS client_id, MIN(t.created) AS since,
                                  MAX(CASE WHEN t.kind='refresh' THEN t.expires END) AS until
                           FROM tokens t LEFT JOIN clients c ON c.client_id = t.client_id
                           GROUP BY t.client_id, t.family ORDER BY since""").fetchall()
    if not rows:
        print("No apps are signed in right now.")
        return 0
    for r in rows:
        print(f"{(r['name'] or r['client_id']):20} signed in {when(r['since'])}   stays signed in until {when(r['until'] or r['since'])} unless unused")
    return 0


def cmd_revoke_all(argv):
    store().revoke_all()
    print("Every app has been signed out. Claude will ask you to sign in again the next time it needs your mail.")
    return 0


def cmd_log(argv):
    n = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 20
    if not LOG_FILE.exists():
        print("No sign-in events yet.")
        return 0
    for line in LOG_FILE.read_text().splitlines()[-n:]:
        print(line)
    return 0


def cmd_smoke_token(argv):
    s = store()
    resource = oauth.Config.from_env(accounts.BASE).resource  # needs PUBLIC_HOSTNAME from .env
    access, _ = s.issue_tokens("smoke-test", "mail", resource, "smoke")
    # Short life: shorten the stored expiry to five minutes.
    s.db.execute("UPDATE tokens SET expires=? WHERE family='smoke'", (time.time() + 300,))
    s.db.execute("DELETE FROM tokens WHERE family='smoke' AND kind='refresh'")
    print(access)
    return 0


COMMANDS = {
    "setup": cmd_setup, "password": cmd_password, "reset-2fa": cmd_reset_2fa, "sessions": cmd_sessions,
    "revoke-all": cmd_revoke_all, "log": cmd_log, "smoke-token": cmd_smoke_token,
}


def main(argv) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[argv[1]](argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
