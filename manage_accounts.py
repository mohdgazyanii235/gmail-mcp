"""Add, list or remove Google accounts.

  .venv/bin/python manage_accounts.py add opal
  .venv/bin/python manage_accounts.py list
  .venv/bin/python manage_accounts.py remove opal

'add' prints a link. Open it in a browser on your own computer (with the SSH tunnel running, see the guide),
sign in with the Google account, and approve access.
"""

import sys

import accounts

USAGE = __doc__


def add(alias: str) -> int:
    alias = accounts.check_alias(alias)
    if not accounts.CLIENT_SECRET.exists():
        print(f"Missing {accounts.CLIENT_SECRET}. Copy your client_secret.json from Google Cloud into this folder first.")
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(accounts.CLIENT_SECRET), accounts.SCOPES)
    except ValueError as e:
        print(f"client_secret.json is not usable: {e}\nIt must come from an OAuth client of type 'Desktop app'.")
        return 1

    creds = flow.run_local_server(
        host="localhost",
        port=8080,
        open_browser=False,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="\nOpen this link in your browser and sign in with the account you want to add:\n\n{url}\n",
        success_message="All done. You can close this tab and go back to the terminal.",
    )
    if not creds.refresh_token:
        print("Google did not return a long-lived login. Remove this app at https://myaccount.google.com/permissions "
              "for that account, then run this command again.")
        return 1

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    email = service.users().getProfile(userId="me").execute()["emailAddress"]

    import json

    info = json.loads(creds.to_json())
    info["email"] = email
    previous = accounts.token_path(alias).exists() and accounts.read_info(alias).get("email")
    accounts.write_info(alias, info)
    if previous and previous != email:
        print(f"Note: '{alias}' used to be {previous}. It is now {email}.")
    print(f"\nSaved account '{alias}' for {email}.")
    return 0


def main(argv) -> int:
    if len(argv) >= 2 and argv[1] == "list":
        found = accounts.list_aliases()
        if not found:
            print("No accounts yet.")
        for alias in found:
            print(f"{alias:12} {accounts.read_info(alias).get('email', '')}")
        return 0
    if len(argv) == 3 and argv[1] == "add":
        return add(argv[2])
    if len(argv) == 3 and argv[1] == "remove":
        path = accounts.token_path(argv[2])
        if not path.exists():
            print(f"No account called '{argv[2]}'.")
            return 1
        path.unlink()
        print(f"Removed '{argv[2]}' from this Pi. To fully revoke access, also remove the app at "
              "https://myaccount.google.com/permissions while signed in to that Google account.")
        return 0
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
