# Gmail connector for Claude, running on a Raspberry Pi

This sets up a small server on your Raspberry Pi that lets Claude read, draft and send email from your four Gmail accounts. It costs nothing to run (Google's Gmail API and Tailscale's free plan are enough). Plan on about 90 minutes the first time.

**What you end up with**

- A server on the Pi that holds a login for each of your accounts.
- A public https address for that server, protected by a real sign-in: your password plus a code from an authenticator app on your phone, the same kind of login Carly uses.
- A custom connector in Claude pointing at that address. Claude opens your sign-in page once, you sign in, and from then on it stays connected.

**What Claude will be able to do:** list your accounts, search a mailbox with Gmail search syntax, read a message or a whole conversation, save drafts (new or as a reply inside an existing thread), and send emails, replies, or saved drafts. It can attach files to new emails, replies and drafts: small files that Claude supplies itself, or files already sitting in a mailbox (for example forwarding an attachment from a received email). It cannot delete mail, change labels, or read the contents of attachments.

**Read this before you start**

- The connector address is not a secret. Knowing it gets a stranger nothing: every request without a valid token is refused, and getting a token needs your password and a fresh authenticator code. Even so, pick a long password you do not use anywhere else, and keep the authenticator setup key somewhere safe.
- Emails can contain text that tries to give Claude instructions. For that reason, keep Claude's approval prompt switched on for the three "send" tools (Part 8, step 4).
- The sign-in code was tested thoroughly on its own, but the whole thing has not yet run against the real Google and Claude services. Part 7 is a smoke test that tells you whether it works before you involve Claude.

**What you need**

- Raspberry Pi 4 or 5 (2 GB of RAM or more), a microSD card (16 GB or more), a power supply, and either an ethernet cable or Wi-Fi details.
- A laptop or desktop on the same network, with a web browser and a terminal (Terminal on Mac, PowerShell on Windows 10 and 11).
- The `gmail-mcp.tar.gz` file that came with this guide.
- A Google account to own the Google Cloud project (your personal Gmail is fine) and an account to sign in to Tailscale (a Google, Microsoft or GitHub login works).
- A phone with an authenticator app (Google Authenticator, Microsoft Authenticator, Authy, 1Password or Apple Passwords all work).

Throughout this guide, `USER` is the username you choose in Part 2 and `gmailpi` is the Pi's name. Replace them if you pick something else.

---

## Part 1. Create the Google login system (on your laptop)

Google needs to know an app is asking for access to your mail. You create that app once, for free. All four of your accounts will use it.

1. Go to https://console.cloud.google.com and sign in with your personal Google account.
2. Click the project picker at the top, choose **New project**, name it `pi-gmail`, and click **Create**. Make sure the new project is selected afterwards.
3. Open https://console.cloud.google.com/apis/library/gmail.googleapis.com and click **Enable**.
4. Open the main menu and choose **Google Auth Platform** (older screens call it **OAuth consent screen**). Click **Get started** and fill in:
   - App name: `Pi Gmail`
   - User support email: your email
   - Audience: **External**
   - Contact email: your email
   - Tick the agreement box and click **Create**.
5. Open **Data Access** (older screens: "Scopes"), click **Add or remove scopes**, paste this into the box for manually adding scopes, add it, and **Save**:
   ```
   https://www.googleapis.com/auth/gmail.modify
   ```
6. Open **Audience** and click **Publish app**, then confirm. The status should now say **In production**. Do not skip this. While an app is in "Testing", Google expires its logins after 7 days and your connector would break every week. For personal use you do not need Google's verification; you will just click through one "unverified app" warning per account in Part 6.
7. Open **Clients**, click **Create client**, choose application type **Desktop app**, name it anything, and click **Create**. Click **Download JSON**.
8. Rename the downloaded file to exactly `client_secret.json` and keep it somewhere you can find it.

**If any of your accounts is a Google Workspace account** (opalnoak.co.uk, sanyang.co.uk or thebountyregister.com may be): Workspace administrators can block outside apps. If you are the administrator, open https://admin.google.com, go to **Security, Access and data control, API controls, Manage third-party app access**, choose to add an app by its OAuth client ID, paste the client ID from your project (shown on the Clients page), and mark it as trusted. If someone else administers it, they need to do this.

---

## Part 2. Put the operating system on the Pi (on your laptop)

1. Download and install **Raspberry Pi Imager** from https://www.raspberrypi.com/software/.
2. Put the microSD card in your laptop. Open Imager and choose:
   - **Device:** your Pi model
   - **Operating system:** Raspberry Pi OS (other), then **Raspberry Pi OS Lite (64-bit)**
   - **Storage:** your SD card
3. Click **Next**, then **Edit settings** (it may say "Customisation"). Set:
   - Hostname: `gmailpi`
   - Username and password: choose your own and write them down. This is `USER`.
   - Wi-Fi name and password (skip if you will use an ethernet cable)
   - Time zone: Europe/London
   - Under **Services**, tick **Enable SSH** and choose **Use password authentication**
4. Save, confirm, and let it write the card. This takes a few minutes.
5. Put the card in the Pi, plug in the cable if you are using one, and power it on. Wait about three minutes for the first boot.

---

## Part 3. Connect to the Pi and update it

1. On your laptop, open a terminal and run:
   ```
   ssh USER@gmailpi.local
   ```
   Type `yes` if it asks about authenticity, then your password. You are now typing commands on the Pi.
   If `gmailpi.local` is not found, look up the Pi's IP address in your router's device list and use `ssh USER@THE.IP.ADDRESS` instead.
2. Update everything (about 5 minutes):
   ```
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y python3-venv curl
   ```

---

## Part 4. Install Tailscale (on the Pi, plus one browser step)

Tailscale gives the Pi a permanent public https address without opening anything on your router.

1. On the Pi:
   ```
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
2. It prints a link. Open it in your laptop browser, sign in, and approve the Pi.
3. In your laptop browser open https://login.tailscale.com/admin/dns. Make sure **MagicDNS** is on, and click **Enable HTTPS** under HTTPS Certificates.
4. Back on the Pi, check it has a name:
   ```
   tailscale status
   ```
   You should see a line for your Pi. Its full address will look like `gmailpi.something-something.ts.net`.

---

## Part 5. Copy the project to the Pi and install it

1. **Open a second terminal on your laptop** (leave the Pi one open). Go to the folder that holds the two files, then run:
   ```
   scp gmail-mcp.tar.gz client_secret.json USER@gmailpi.local:~/
   ```
2. In the Pi terminal:
   ```
   tar -xzf gmail-mcp.tar.gz
   mv client_secret.json gmail-mcp/
   cd gmail-mcp
   ./setup.sh
   ```
   `setup.sh` takes a few minutes. It builds the Python environment, works out your Pi's public name, and installs the server as a service that starts by itself whenever the Pi boots. It asks for your Pi password once. At the end it should say "Setup finished".
3. Check the service is running:
   ```
   sudo systemctl status gmail-mcp --no-pager
   ```
   Look for `active (running)` in green. Press `q` if it waits for you.
4. Choose your sign-in password and set up the authenticator:
   ```
   .venv/bin/python manage_auth.py setup
   ```
   It asks for a new password twice (12 or more characters; a few random words is good). Then it shows a setup key. In your phone's authenticator app choose "add account", then "enter a setup key", and type the key in (any account name is fine). Finally type the 6-digit code your app shows to prove it works. Save the setup key in a password manager as well, so losing your phone does not lock you out.

---

## Part 6. Add your four Google accounts

Signing in needs a browser, and the Pi has none, so you borrow your laptop's through a tunnel.

1. **On your laptop**, open a new terminal and run this. Leave it open for this whole part:
   ```
   ssh -L 8080:localhost:8080 USER@gmailpi.local
   ```
2. In that terminal, run (this is the first of four; the name is just a label):
   ```
   cd ~/gmail-mcp
   .venv/bin/python manage_accounts.py add opal
   ```
3. It prints a long link. Copy it into your laptop browser. Tip: use a private/incognito window so Google does not auto-pick the wrong account.
4. Sign in with the account that belongs to that name. Google will say **"Google hasn't verified this app"**. That is expected, because you made the app yourself. Click **Advanced**, then **Go to Pi Gmail (unsafe)**. On the next screen **tick the box** that allows reading, composing and sending email, then click **Continue**.
5. The browser shows "All done". The terminal prints `Saved account 'opal' for ...`.
6. Repeat steps 2 to 5 for the other accounts, using a different name each time. Suggested names:

   | Name | Account |
   |---|---|
   | `opal` | mo@opalnoak.co.uk |
   | `bounty` | mo@thebountyregister.com |
   | `personal` | mohdgazyanii235@gmail.com |
   | `sanyang` | sales@sanyang.co.uk |

7. Check them:
   ```
   .venv/bin/python manage_accounts.py list
   ```
   You should see all four names with the right addresses. You can now close the tunnel terminal.

---

## Part 7. Test it on the Pi, then switch on the public address

1. In the Pi terminal:
   ```
   cd ~/gmail-mcp
   ./smoke_test.sh
   ```
   You want every line to say `PASS`, followed by your four accounts and then "All checks passed". The first block proves that strangers are refused; the second proves the mail server works. A `WARN` about the foreign Host header is fine. If anything says `FAIL`, stop here and read Troubleshooting at the end.
2. Turn on the public address:
   ```
   sudo tailscale funnel --bg 8765
   ```
   If it prints a link saying Funnel is not enabled, open it in your browser, approve, and run the command again.
3. Confirm it works from outside. On your laptop (not the Pi), replace the name with your Pi's real address from `tailscale status`:
   ```
   curl -i -X POST https://gmailpi.something-something.ts.net/mcp
   ```
   The answer should start with `401` and include a `WWW-Authenticate` line. That proves the address is live and that it refuses anyone who has not signed in.
4. Get your connector address:
   ```
   ./show_url.sh
   ```
   It prints one line starting with `https://` and ending in `/mcp`. Copy the whole line. It is safe to paste anywhere.

---

## Part 8. Add it to Claude

1. In Claude, open **Settings**, then **Connectors**, and choose **Add custom connector**. (Custom connectors depend on your plan. If you do not see the option, check your plan's connector settings.)
2. Name: `Gmail (Pi)`. URL: paste the address from Part 7. Leave any optional OAuth client ID and secret fields empty; the server registers Claude automatically. Click **Add**, then **Connect**.
3. A sign-in page from your Pi opens. Check the address bar shows your `ts.net` name, then enter your password and the current 6-digit code from your authenticator and press **Sign in**. You land back in Claude with the connector connected.
4. Start a new chat, enable the connector from the tools menu, and ask: *"List my Gmail accounts."* You should see your four names and addresses. Then try: *"Search the opal account for unread mail from the last 3 days."*
5. In the connector's settings, if your Claude offers per-tool permissions, set the read tools (`list_accounts`, `search_messages`, `read_message`, `read_thread`) and the two draft tools to **Always allow**, and set `send_message`, `send_reply` and `send_draft` to **Needs approval**. Claude will then ask you before every send.
6. Final test: ask Claude to *"Create a draft reply on the Gemma Briggs thread in my personal account"*, then open Gmail and check that the draft is sitting inside that conversation. Only after that, try a real send, to yourself first.

You can now remove the Carly connector and any account you no longer want connected.

---

## Everyday use and upkeep

**Handy commands (on the Pi, in `~/gmail-mcp`)**

| What | Command |
|---|---|
| See recent server messages | `journalctl -u gmail-mcp -n 50 --no-pager` |
| Restart the server | `sudo systemctl restart gmail-mcp` |
| Show the connector address again | `./show_url.sh` |
| Change your password | `.venv/bin/python manage_auth.py password` |
| New authenticator (lost phone) | `.venv/bin/python manage_auth.py reset-2fa` |
| See which apps are signed in | `.venv/bin/python manage_auth.py sessions` |
| Sign everything out | `.venv/bin/python manage_auth.py revoke-all` |
| See sign-in attempts | `.venv/bin/python manage_auth.py log 30` |
| List accounts | `.venv/bin/python manage_accounts.py list` |
| Re-login or add an account | `.venv/bin/python manage_accounts.py add NAME` (with the tunnel from Part 6) |
| Remove an account from the Pi | `.venv/bin/python manage_accounts.py remove NAME` |
| See everything that was sent | `cat sent.log` |

`sent.log` records the time, account, recipients and subject of every email sent (never the body).

**Keep it patched.** About once a month run `sudo apt update && sudo apt full-upgrade -y`, and now and then `.venv/bin/pip install -U -r requirements.txt && sudo systemctl restart gmail-mcp`. To get automatic security updates: `sudo apt install -y unattended-upgrades`.

**Signing in.** Claude stays signed in for about 30 days after it last used the connector, renewing itself quietly. If it ever asks you to sign in again, that is normal: open the sign-in page and enter your password and a fresh code. Each code works once, so if you just used it, wait for the next one.

**Wrong guesses.** After 5 wrong passwords or codes in 15 minutes the sign-in page locks for everyone, including you, for the rest of that window. Waiting 15 minutes or running `sudo systemctl restart gmail-mcp` clears it. Run `manage_auth.py log` to see who was trying. If you see attempts you did not make, change your password and run `revoke-all`.

**If you think someone got in** (an unknown sign-in in the log, a lost phone, a leaked password):

1. `.venv/bin/python manage_auth.py revoke-all` signs every app out.
2. `.venv/bin/python manage_auth.py password` and `reset-2fa` give you new credentials.
3. Check `cat sent.log` for anything you did not send.

**Emergency stops**

- Stop sending only: in `.env` set `ALLOW_SEND=false`, then `sudo systemctl restart gmail-mcp`. Drafts and reading still work.
- Take the whole thing offline: `sudo tailscale funnel reset`. Your own `ssh` access keeps working.
- Sign Claude out right now: `.venv/bin/python manage_auth.py revoke-all`.
- Cut Google's access for good: sign in to each Google account, open https://myaccount.google.com/permissions, and remove "Pi Gmail".

---

## Troubleshooting

**`ssh` says it cannot find `gmailpi.local`.** Use the Pi's IP address from your router instead, or try again after a few minutes.

**`setup.sh` says it cannot read the Tailscale name.** Run `sudo tailscale up` again, confirm in the Tailscale admin page that MagicDNS is on (Part 4, step 3), then re-run `./setup.sh`.

**`systemctl status` shows `failed`.** Run `journalctl -u gmail-mcp -n 50 --no-pager` and read the last lines. If it mentions a missing module or something about `mcp`, run `.venv/bin/pip install -U -r requirements.txt` and restart the service. If it mentions `PUBLIC_HOSTNAME`, check that `.env` exists and has that line.

**Smoke test: "initialize failed" or an error mentioning Host or 421.** `PUBLIC_HOSTNAME` in `.env` must match the name `tailscale status` shows. Fix it, restart the service, and run the test again.

**Smoke test passes, but Claude cannot connect.** Check `sudo tailscale funnel status` shows your address pointing at port 8765. Check the URL in Claude ends in `/mcp` with nothing after it. Run `./smoke_test.sh` again and look at the sign-in protection block: the discovery lines must pass.

**Claude connects, but an account gives "403" or "Insufficient Permission".** At sign-in you must tick the box allowing Gmail access. Remove the app from that Google account (https://myaccount.google.com/permissions), then add the account again and tick the box. Also confirm the Gmail API is enabled in Part 1, step 3.

**"Google rejected the saved login" after some days.** Your app is probably still in "Testing" (Part 1, step 6). Publish it to production, then add the affected accounts again.

**Sign-in shows "access blocked" or "admin policy".** That is a Google Workspace account whose administrator has not allowed the app (end of Part 1).

**Sign-in link gives a "can't connect to localhost:8080" page.** The tunnel from Part 6, step 1 is not running. Start it and try again, using the same terminal that runs the `add` command.

**The sign-in page says "setup is not finished".** You have not run `.venv/bin/python manage_auth.py setup` yet (Part 5, step 4).

**The sign-in page says the password or code is wrong, and you are sure it is right.** Your phone's clock must be set automatically, since the code depends on the time. Also remember each code works only once, so wait for the next one. If you have tried 5 times, you are locked for 15 minutes (see "Wrong guesses").

**You lost the phone.** On the Pi run `.venv/bin/python manage_auth.py reset-2fa` and set up the new device.

**Claude says the connector's sign-in failed or the redirect was refused.** The server only accepts Claude's official callback addresses. If you are using a different Claude app that uses another one, the error will show it; add it to `.env` as `EXTRA_REDIRECT_URIS=https://that-address` and restart the service.

## Bridge for Command Pi

`bridge.py` is a small local service (127.0.0.1:8767, bearer `BRIDGE_TOKEN`) that lets Command Pi's sandboxed scripts work on mail without ever holding a Google login. It offers accounts, search, read, folders, mark read/unread (one message or a whole search, undoable as one batch), move (to INBOX, SPAM, ARCHIVE or your own folder) and undo. It has no send, no draft and no delete. Install with `./install-bridge.sh`, then run `sudo scripts/connect-bridge.sh` from the command-pi repo. Optional `BRIDGE_ACCOUNTS=personal,sales` limits mailboxes. Tests: `python3 test_organise.py && python3 test_bridge.py`.

## Google Drive (read only)

Jarvis can search and read the Drive of any account you connect, and can never change it: the sign-in only asks for Google's view-only permission (`drive.readonly`), and the server has no write tool. Drive logins are kept in `drive_tokens/`, separate from the Gmail logins in `tokens/`.

### A. Google Cloud, on your laptop (once)

1. Go to https://console.cloud.google.com and sign in with the Google account that owns the `pi-gmail` project. Check that `pi-gmail` is shown in the project picker at the top.
2. Open https://console.cloud.google.com/apis/library/drive.googleapis.com and click **Enable**.
3. Open the main menu, choose **Google Auth Platform**, then **Data Access**.
4. Click **Add or remove scopes**. In the box for manually adding scopes paste `https://www.googleapis.com/auth/drive.readonly`, click **Add to table**, then **Update**, then **Save**. Keep the Gmail scope that is already there.
5. Open **Audience** and check the status says **In production**. If it says Testing, click **Publish app** and confirm (Testing logins expire after 7 days).
6. If any of the accounts is a Google Workspace account (opalnoak.co.uk, sanyang.co.uk, thebountyregister.com) and someone else administers it, they must allow the app's Drive access too (admin.google.com, Security, Access and data control, API controls, Manage third-party app access).

### B. Update the Pi

1. On your laptop open a terminal and run `ssh USER@gmailpi.local`.
2. Run `cd ~/gmail-mcp && git pull`. It should list `drivelib.py` among the changed files.
3. Run `.venv/bin/pip install -r requirements.txt`.
4. Run `.venv/bin/python manage_accounts.py list` and note the names (for example `opal`, `bounty`, `sanyang` or `sales`). Use exactly those names below.

### C. Sign in to Drive, once per account

1. Open a second terminal on your laptop and run `ssh -L 8080:localhost:8080 USER@gmailpi.local`. Leave it open.
2. In that terminal run `cd ~/gmail-mcp && .venv/bin/python manage_accounts.py add-drive NAME`, using the first name from step B4.
3. Copy the long link it prints into a private browser window on your laptop.
4. Sign in with the Google account that belongs to that name. Click **Advanced**, then **Go to Pi Gmail (unsafe)**. Tick the box allowing Google Drive viewing and click **Continue**.
5. The browser says "All done" and the terminal says `Saved read-only Drive access for 'NAME'`.
6. Repeat steps 2 to 5 for each of the other two accounts.
7. Run `.venv/bin/python manage_accounts.py list`. Each of the three should say `Drive: read only`.

### D. Restart and test

1. `sudo systemctl restart gmail-mcp`, then `sudo systemctl status gmail-mcp --no-pager`, which should say active (running).
2. Close the tunnel terminal.
3. `cd ~/pi-jarvis && ./scripts/update.sh`.
4. Say: "Jarvis, which Google Drives can you see?" then "search the sales drive for wholesale".

If Jarvis says Drive is not connected, check step C7. If Google says "access not configured", the Drive API from step A2 is not enabled yet. Remove one account's Drive access with `manage_accounts.py remove-drive NAME`.
