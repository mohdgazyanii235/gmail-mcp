"""Where the Google logins live and how to turn one into a Gmail client."""

import json
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
TOKENS = BASE / "tokens"
CLIENT_SECRET = BASE / "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def check_alias(alias: str) -> str:
    alias = (alias or "").strip().lower()
    if not ALIAS_RE.match(alias):
        raise ValueError("Account names are 1-32 characters: lowercase letters, digits, - or _ (for example: opal).")
    return alias


def token_path(alias: str) -> Path:
    return TOKENS / f"{check_alias(alias)}.json"


def list_aliases() -> list:
    if not TOKENS.exists():
        return []
    return sorted(p.stem for p in TOKENS.glob("*.json"))


def read_info(alias: str) -> dict:
    path = token_path(alias)
    if not path.exists():
        known = ", ".join(list_aliases()) or "none set up yet"
        raise ValueError(f"Unknown account '{alias}'. Available accounts: {known}.")
    return json.loads(path.read_text())


def write_info(alias: str, info: dict) -> None:
    TOKENS.mkdir(mode=0o700, exist_ok=True)
    path = token_path(alias)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def get_service(alias: str):
    """Return (gmail_service, account_email). Refreshes and re-saves the login when needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    info = read_info(alias)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        info.update(json.loads(creds.to_json()))
        write_info(alias, info)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service, info.get("email", "")
