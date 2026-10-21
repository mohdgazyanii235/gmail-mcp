"""Sign-in for a single-owner MCP server. Standard library only.

This is an OAuth 2.1 authorization server plus a bearer-token gate, written as a plain ASGI wrapper
around the real MCP app. It implements what Claude's connector needs:

  * discovery documents (RFC 9728 protected resource, RFC 8414 authorization server)
  * dynamic client registration (RFC 7591), limited to an allow-list of callback addresses
  * an /authorize page where the owner signs in with a password AND an authenticator code
  * authorization code + PKCE (S256 only), rotating refresh tokens, token revocation
  * a 401 + WWW-Authenticate challenge on /mcp for anyone without a valid access token

Only the owner's password hash and TOTP secret (auth.json) and hashed tokens (auth.db) are stored.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import struct
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

SUPPORTED_SCOPES = ["mail", "offline_access"]
DEFAULT_REDIRECTS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
LOGIN_FAIL_LIMIT = 5          # wrong sign-ins allowed ...
LOGIN_WINDOW = 15 * 60        # ... per 15 minutes, across everyone
PENDING_TTL = 10 * 60         # how long the sign-in page stays valid
PENDING_MAX_ATTEMPTS = 5
CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60
REFRESH_GRACE = 30            # an old refresh token keeps working this long after rotation (retries, races)
MAX_CLIENTS = 200
MAX_BODY = 64 * 1024

_PKCE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

@dataclass
class Config:
    public_url: str                       # e.g. https://gmailpi.tail1234.ts.net  (no trailing slash)
    db_path: Path
    auth_path: Path
    log_path: Optional[Path] = None
    resource_path: str = "/mcp"
    allowed_redirects: tuple = DEFAULT_REDIRECTS
    allow_loopback: bool = False          # for Claude Code on your own computer; off by default
    clock: Callable[[], float] = field(default=time.time, repr=False)

    @property
    def resource(self) -> str:
        return self.public_url + self.resource_path

    @classmethod
    def from_env(cls, base: Path) -> "Config":
        host = os.environ.get("PUBLIC_HOSTNAME", "").strip().lower()
        if not host:
            raise SystemExit("PUBLIC_HOSTNAME is not set. Check the .env file.")
        extra = tuple(u.strip() for u in os.environ.get("EXTRA_REDIRECT_URIS", "").split(",") if u.strip())
        allow_loopback = os.environ.get("ALLOW_LOOPBACK_REDIRECTS", "false").strip().lower() in ("1", "true", "yes")
        return cls(
            public_url="https://" + host,
            db_path=base / "auth.db",
            auth_path=base / "auth.json",
            log_path=base / "auth.log",
            allowed_redirects=DEFAULT_REDIRECTS + extra,
            allow_loopback=allow_loopback,
        )


# --------------------------------------------------------------------------------------
# Owner credentials: scrypt password hash + TOTP authenticator code
# --------------------------------------------------------------------------------------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        got = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                             maxmem=128 * 1024 * 1024, dklen=len(expected))
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def hotp(secret: bytes, counter: int, digits: int = 6) -> str:
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_match(secret_b32: str, code: str, now: float, window: int = 1, step: int = 30) -> Optional[int]:
    """Return the matching time-step number, or None. Accepts the current step and one either side."""
    code = (code or "").replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    try:
        secret = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    except Exception:
        return None
    counter = int(now // step)
    matched = None
    for c in range(counter - window, counter + window + 1):
        if hmac.compare_digest(hotp(secret, c), code):
            matched = c
    return matched


def load_owner(path: Path) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text())
        if data.get("password_hash") and data.get("totp_secret"):
            return data
    except (OSError, ValueError):
        pass
    return None


def save_owner(path: Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------------------
# Storage (SQLite). Tokens and codes are stored only as SHA-256 hashes.
# --------------------------------------------------------------------------------------

def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, clock: Callable[[], float]):
        self.clock = clock
        path = Path(path)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS clients(client_id TEXT PRIMARY KEY, secret_hash TEXT, name TEXT,
                                               redirect_uris TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT, state TEXT,
                                               challenge TEXT, scope TEXT, resource TEXT, expires REAL,
                                               attempts INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS codes(hash TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT, challenge TEXT,
                                             scope TEXT, resource TEXT, expires REAL, family TEXT, used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, kind TEXT, client_id TEXT, scope TEXT,
                                              resource TEXT, expires REAL, family TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def now(self) -> float:
        return self.clock()

    def purge(self):
        now = self.now()
        self.db.execute("DELETE FROM pending WHERE expires < ?", (now,))
        self.db.execute("DELETE FROM codes WHERE expires < ?", (now,))
        self.db.execute("DELETE FROM tokens WHERE expires < ?", (now,))

    # clients
    def add_client(self, name, redirect_uris, secret_hash):
        client_id = secrets.token_urlsafe(18)
        count = self.db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        if count >= MAX_CLIENTS:  # drop the oldest clients that hold no live tokens
            self.db.execute("""DELETE FROM clients WHERE client_id IN (
                                 SELECT client_id FROM clients WHERE client_id NOT IN (SELECT client_id FROM tokens)
                                 ORDER BY created LIMIT ?)""", (count - MAX_CLIENTS + 1,))
        self.db.execute("INSERT INTO clients VALUES (?,?,?,?,?)",
                        (client_id, secret_hash, name, json.dumps(redirect_uris), self.now()))
        return client_id

    def get_client(self, client_id):
        if not client_id:
            return None
        row = self.db.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["redirect_uris"] = json.loads(d["redirect_uris"])
        return d

    # pending sign-in requests
    def add_pending(self, **kw):
        req_id = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO pending(id,client_id,redirect_uri,state,challenge,scope,resource,expires) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (req_id, kw["client_id"], kw["redirect_uri"], kw["state"], kw["challenge"],
                         kw["scope"], kw["resource"], self.now() + PENDING_TTL))
        return req_id

    def get_pending(self, req_id):
        if not req_id:
            return None
        row = self.db.execute("SELECT * FROM pending WHERE id=? AND expires>=?", (req_id, self.now())).fetchone()
        return dict(row) if row else None

    def bump_pending(self, req_id):
        self.db.execute("UPDATE pending SET attempts=attempts+1 WHERE id=?", (req_id,))
        row = self.db.execute("SELECT attempts FROM pending WHERE id=?", (req_id,)).fetchone()
        if row and row[0] >= PENDING_MAX_ATTEMPTS:
            self.db.execute("DELETE FROM pending WHERE id=?", (req_id,))
            return True
        return False

    def delete_pending(self, req_id):
        self.db.execute("DELETE FROM pending WHERE id=?", (req_id,))

    # authorization codes
    def add_code(self, p) -> str:
        code = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO codes VALUES (?,?,?,?,?,?,?,?,0)",
                        (sha(code), p["client_id"], p["redirect_uri"], p["challenge"], p["scope"], p["resource"],
                         self.now() + CODE_TTL, secrets.token_hex(8)))
        return code

    def get_code(self, code):
        row = self.db.execute("SELECT * FROM codes WHERE hash=?", (sha(code),)).fetchone()
        return dict(row) if row else None

    def mark_code_used(self, code):
        self.db.execute("UPDATE codes SET used=1 WHERE hash=?", (sha(code),))

    # tokens
    def issue_tokens(self, client_id, scope, resource, family):
        now = self.now()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?)",
                        (sha(access), "access", client_id, scope, resource, now + ACCESS_TTL, family, now))
        self.db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?)",
                        (sha(refresh), "refresh", client_id, scope, resource, now + REFRESH_TTL, family, now))
        return access, refresh

    def get_token(self, token, kind):
        row = self.db.execute("SELECT * FROM tokens WHERE hash=? AND kind=? AND expires>=?",
                              (sha(token), kind, self.now())).fetchone()
        return dict(row) if row else None

    def retire_refresh(self, token):
        """Rotate: the old refresh token stays usable for a short grace period, then dies."""
        self.db.execute("UPDATE tokens SET expires=MIN(expires, ?) WHERE hash=?",
                        (self.now() + REFRESH_GRACE, sha(token)))

    def revoke_hash(self, token):
        self.db.execute("DELETE FROM tokens WHERE hash=?", (sha(token),))

    def revoke_family(self, family):
        self.db.execute("DELETE FROM tokens WHERE family=?", (family,))

    def revoke_all(self):
        for table in ("tokens", "codes", "pending"):
            self.db.execute(f"DELETE FROM {table}")

    # misc
    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))


# --------------------------------------------------------------------------------------
# Small HTTP helpers
# --------------------------------------------------------------------------------------

class BodyTooLarge(Exception):
    pass


async def read_body(receive, limit=MAX_BODY) -> bytes:
    chunks, size = [], 0
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        part = msg.get("body", b"")
        size += len(part)
        if size > limit:
            raise BodyTooLarge()
        chunks.append(part)
        if not msg.get("more_body"):
            break
    return b"".join(chunks)


SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"pragma", b"no-cache"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]
HTML_HEADERS = [
    (b"content-type", b"text/html; charset=utf-8"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"),
]


async def respond(send, status, body: bytes, headers):
    await send({"type": "http.response.start", "status": status, "headers": list(headers) + SECURITY_HEADERS})
    await send({"type": "http.response.body", "body": body})


async def send_json(send, status, obj, extra=()):
    await respond(send, status, json.dumps(obj).encode(),
                  [(b"content-type", b"application/json")] + [(k.encode(), v.encode()) for k, v in extra])


async def send_html(send, status, markup: str):
    await respond(send, status, markup.encode(), HTML_HEADERS)


async def send_redirect(send, location: str):
    await respond(send, 302, b"", [(b"location", location.encode())])


def parse_form(body: bytes) -> dict:
    parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=30)
    return {k: v[0] for k, v in parsed.items()}


def add_params(url: str, params: dict) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v != ""]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def valid_challenge(value: str) -> bool:
    return len(value) == 43 and all(c in _PKCE_CHARS for c in value)


def valid_verifier(value: str) -> bool:
    return 43 <= len(value) <= 128 and all(c in _PKCE_CHARS for c in value)


def pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(expected, challenge)


def filter_scope(requested: str) -> str:
    return " ".join(s for s in requested.split() if s in SUPPORTED_SCOPES) or "mail"


PAGE_CSS = """
body{font-family:system-ui,sans-serif;background:#f4f1ec;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
main{background:#fff;padding:32px;border-radius:12px;max-width:380px;width:calc(100% - 48px);box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 12px} p{line-height:1.45;color:#333} label{display:block;margin:14px 0 4px;font-size:14px}
input{width:100%;box-sizing:border-box;padding:10px;font-size:16px;border:1px solid #bbb;border-radius:6px}
button{margin-top:18px;width:100%;padding:12px;font-size:16px;border:0;border-radius:6px;background:#1f5f4a;color:#fff;cursor:pointer}
.err{background:#fde8e8;color:#8a1c1c;padding:10px;border-radius:6px;font-size:14px} .small{font-size:13px;color:#666}
"""


def page(title: str, inner: str) -> str:
    return ("<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style><body><main>{inner}</main></body></html>")


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------

class OAuthGate:
    def __init__(self, app, cfg: Config):
        self.app = app
        self.cfg = cfg
        self.store = Store(cfg.db_path, cfg.clock)
        self.failures = []  # timestamps of wrong sign-ins (in memory: a restart clears the lockout)

    # ---- plumbing ---------------------------------------------------------------
    def _log(self, scope, event, **fields):
        if not self.cfg.log_path:
            return
        headers = dict(scope.get("headers", []))
        ip = headers.get(b"x-forwarded-for", b"").decode("latin-1").split(",")[0].strip()
        if not ip and scope.get("client"):
            ip = scope["client"][0]
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.cfg.clock())), "event": event, "ip": ip}
        entry.update(fields)
        try:
            with open(self.cfg.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def _challenge_headers(self, error=None):
        parts = [f'Bearer resource_metadata="{self.cfg.public_url}/.well-known/oauth-protected-resource"', 'scope="mail"']
        if error:
            parts.append(f'error="{error}"')
        return [("www-authenticate", ", ".join(parts))]

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            return await self.app(scope, receive, send)
        if kind != "http":
            return
        path, method = scope.get("path", ""), scope.get("method", "GET")
        try:
            self.store.purge()
            if method == "GET" and path in ("/.well-known/oauth-protected-resource",
                                            "/.well-known/oauth-protected-resource" + self.cfg.resource_path):
                return await self._protected_resource(send)
            if method == "GET" and path == "/.well-known/oauth-authorization-server":
                return await self._as_metadata(send)
            if path == "/register" and method == "POST":
                return await self._register(scope, receive, send)
            if path == "/authorize" and method == "GET":
                return await self._authorize_get(scope, send)
            if path == "/authorize" and method == "POST":
                return await self._authorize_post(scope, receive, send)
            if path == "/token" and method == "POST":
                return await self._token(scope, receive, send)
            if path == "/revoke" and method == "POST":
                return await self._revoke(scope, receive, send)
            if path == self.cfg.resource_path or path.startswith(self.cfg.resource_path + "/"):
                return await self._guarded_mcp(scope, receive, send)
            await send_json(send, 404, {"error": "not_found"})
        except BodyTooLarge:
            await send_json(send, 413, {"error": "invalid_request", "error_description": "request body too large"})
        except Exception as exc:  # never leak internals to the caller
            print(f"oauth gate error on {method} {path}: {exc!r}", file=sys.stderr)
            await send_json(send, 500, {"error": "server_error"})

    # ---- discovery ----------------------------------------------------------------
    async def _protected_resource(self, send):
        await send_json(send, 200, {
            "resource": self.cfg.resource,
            "authorization_servers": [self.cfg.public_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mail"],
        })

    async def _as_metadata(self, send):
        base = self.cfg.public_url
        await send_json(send, 200, {
            "issuer": base,
            "authorization_endpoint": base + "/authorize",
            "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register",
            "revocation_endpoint": base + "/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": SUPPORTED_SCOPES,
            "authorization_response_iss_parameter_supported": True,
        })

    def _resource_ok(self, resource: str) -> bool:
        """No resource given is fine. A given one must be this server (a trailing slash is tolerated)."""
        resource = resource.rstrip("/")
        return resource in ("", self.cfg.resource, self.cfg.public_url)

    # ---- redirect rules -------------------------------------------------------------
    def _is_loopback(self, uri: str):
        parts = urllib.parse.urlsplit(uri)
        return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")

    def _redirect_allowed(self, uri: str) -> bool:
        if uri in self.cfg.allowed_redirects:
            return True
        return bool(self.cfg.allow_loopback and self._is_loopback(uri) and not urllib.parse.urlsplit(uri).fragment)

    def _redirect_registered(self, registered: list, requested: str) -> bool:
        if not requested or not self._redirect_allowed(requested):
            return False
        if requested in registered:
            return True
        if self._is_loopback(requested):  # RFC 8252: ignore the port for loopback addresses
            r = urllib.parse.urlsplit(requested)
            for reg in registered:
                g = urllib.parse.urlsplit(reg)
                if self._is_loopback(reg) and (g.scheme, g.hostname, g.path) == (r.scheme, r.hostname, r.path):
                    return True
        return False

    # ---- dynamic client registration ----------------------------------------------------
    async def _register(self, scope, receive, send):
        body = await read_body(receive)
        try:
            meta = json.loads(body or b"{}")
            if not isinstance(meta, dict):
                raise ValueError
        except ValueError:
            return await send_json(send, 400, {"error": "invalid_client_metadata", "error_description": "body must be JSON"})
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 5 or not all(isinstance(u, str) and len(u) <= 2048 for u in uris):
            return await send_json(send, 400, {"error": "invalid_redirect_uri", "error_description": "redirect_uris is required"})
        if not all(self._redirect_allowed(u) for u in uris):
            self._log(scope, "register_refused", uris=uris[:5])
            return await send_json(send, 400, {"error": "invalid_redirect_uri",
                                               "error_description": "this server only accepts Claude's callback address"})
        method = meta.get("token_endpoint_auth_method", "client_secret_basic")
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return await send_json(send, 400, {"error": "invalid_client_metadata", "error_description": "unsupported auth method"})
        grants = meta.get("grant_types", ["authorization_code"])
        if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
            return await send_json(send, 400, {"error": "invalid_client_metadata", "error_description": "unsupported grant type"})
        if meta.get("response_types", ["code"]) != ["code"]:
            return await send_json(send, 400, {"error": "invalid_client_metadata", "error_description": "only code is supported"})
        name = str(meta.get("client_name") or "Unnamed app")[:100]
        client_secret = secrets.token_urlsafe(32) if method != "none" else None
        client_id = self.store.add_client(name, uris, sha(client_secret) if client_secret else None)
        self._log(scope, "register", client=name, client_id=client_id)
        out = {"client_id": client_id, "client_id_issued_at": int(self.cfg.clock()), "client_name": name,
               "redirect_uris": uris, "grant_types": grants, "response_types": ["code"],
               "token_endpoint_auth_method": method}
        if client_secret:
            out["client_secret"] = client_secret
            out["client_secret_expires_at"] = 0
        await send_json(send, 201, out)

    # ---- the sign-in page ---------------------------------------------------------------
    def _login_page(self, client, redirect_uri, req_id, error="") -> str:
        host = urllib.parse.urlsplit(redirect_uri).hostname or ""
        err = f"<p class=err>{html.escape(error)}</p>" if error else ""
        return page("Sign in", f"""
            <h1>Allow access to your email?</h1>
            <p><b>{html.escape(client['name'])}</b> wants to read, draft and send email from the Gmail accounts
            connected to this server.</p>
            <p class=small>It will return to <b>{html.escape(host)}</b>. If you did not just press Connect in Claude, close this page.</p>
            {err}
            <form method=post action=/authorize autocomplete=off>
              <input type=hidden name=req value="{html.escape(req_id)}">
              <label for=pw>Password</label>
              <input id=pw name=password type=password autocomplete=current-password required autofocus>
              <label for=code>6-digit code from your authenticator app</label>
              <input id=code name=code inputmode=numeric autocomplete=one-time-code pattern="[0-9 ]*" maxlength=7 required>
              <button type=submit>Sign in and allow</button>
            </form>""")

    async def _error_page(self, send, status, heading, text=""):
        await send_html(send, status, page(heading, f"<h1>{html.escape(heading)}</h1><p>{html.escape(text)}</p>"))

    async def _authorize_get(self, scope, send):
        q = {k: v[0][:2048] for k, v in urllib.parse.parse_qs(scope.get("query_string", b"").decode("utf-8", "replace")).items()}
        client = self.store.get_client(q.get("client_id", ""))
        redirect_uri = q.get("redirect_uri", "")
        if not client or not self._redirect_registered(client["redirect_uris"], redirect_uri):
            self._log(scope, "authorize_refused", reason="unknown client or redirect")
            return await self._error_page(send, 400, "This sign-in link is not valid",
                                          "Go back to Claude and press Connect again.")
        state = q.get("state", "")

        def fail(error, description):
            return send_redirect(send, add_params(redirect_uri, {"error": error, "error_description": description,
                                                                  "state": state, "iss": self.cfg.public_url}))

        if q.get("response_type") != "code":
            return await fail("unsupported_response_type", "only response_type=code is supported")
        challenge = q.get("code_challenge", "")
        if q.get("code_challenge_method") != "S256" or not valid_challenge(challenge):
            return await fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
        if not self._resource_ok(q.get("resource", "")):
            return await fail("invalid_target", "unknown resource")
        if load_owner(self.cfg.auth_path) is None:
            return await self._error_page(send, 503, "Sign-in is not set up yet",
                                          "On the Raspberry Pi, run: .venv/bin/python manage_auth.py setup")
        req_id = self.store.add_pending(client_id=client["client_id"], redirect_uri=redirect_uri, state=state,
                                        challenge=challenge, scope=filter_scope(q.get("scope", "")),
                                        resource=self.cfg.resource)
        await send_html(send, 200, self._login_page(client, redirect_uri, req_id))

    def _locked_for(self) -> int:
        now = self.cfg.clock()
        self.failures = [t for t in self.failures if now - t < LOGIN_WINDOW]
        if len(self.failures) >= LOGIN_FAIL_LIMIT:
            return int(self.failures[0] + LOGIN_WINDOW - now) + 1
        return 0

    async def _authorize_post(self, scope, receive, send):
        body = await read_body(receive)
        form = parse_form(body)
        pending = self.store.get_pending(form.get("req", ""))
        if not pending:
            return await self._error_page(send, 400, "This sign-in page expired",
                                          "Go back to Claude and press Connect again.")
        client = self.store.get_client(pending["client_id"])
        if not client:
            return await self._error_page(send, 400, "This sign-in link is not valid", "Press Connect again in Claude.")
        wait = self._locked_for()
        if wait:
            self._log(scope, "login_locked")
            return await send_html(send, 429, self._login_page(
                client, pending["redirect_uri"], pending["id"],
                f"Too many wrong attempts. Try again in {wait // 60 + 1} minutes."))
        owner = load_owner(self.cfg.auth_path)
        if owner is None:
            return await self._error_page(send, 503, "Sign-in is not set up yet",
                                          "On the Raspberry Pi, run: .venv/bin/python manage_auth.py setup")
        # Always check both factors so the response does not reveal which one was wrong.
        password_ok = verify_password(form.get("password", "")[:256], owner["password_hash"])
        step = totp_match(owner["totp_secret"], form.get("code", "")[:16], self.cfg.clock())
        last = self.store.get_meta("last_totp_step")
        code_ok = step is not None and (last is None or step > int(last))
        if not (password_ok and code_ok):
            self.failures.append(self.cfg.clock())
            self._log(scope, "login_failed", client=client["name"])
            if self.store.bump_pending(pending["id"]):
                return await self._error_page(send, 400, "Too many wrong attempts on this page",
                                              "Go back to Claude and press Connect again.")
            return await send_html(send, 401, self._login_page(
                client, pending["redirect_uri"], pending["id"], "Wrong password or code. Please try again."))
        self.store.set_meta("last_totp_step", step)  # a code works once
        self.failures = []
        self.store.delete_pending(pending["id"])
        code = self.store.add_code(pending)
        self._log(scope, "login_ok", client=client["name"])
        await send_redirect(send, add_params(pending["redirect_uri"],
                                             {"code": code, "state": pending["state"], "iss": self.cfg.public_url}))

    # ---- token endpoint ---------------------------------------------------------------------
    async def _client_auth(self, scope, form):
        """Return (client, None) or (None, error_response_args)."""
        headers = dict(scope.get("headers", []))
        client_id, secret = form.get("client_id", ""), form.get("client_secret", "")
        basic = headers.get(b"authorization", b"").decode("latin-1")
        if basic.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(basic[6:]).decode().partition(":")
                client_id = urllib.parse.unquote(user)
                secret = urllib.parse.unquote(pw)
            except Exception:
                return None, (401, {"error": "invalid_client"})
        client = self.store.get_client(client_id)
        if not client:
            return None, (401, {"error": "invalid_client"})
        if client["secret_hash"] and not hmac.compare_digest(sha(secret), client["secret_hash"]):
            return None, (401, {"error": "invalid_client"})
        return client, None

    async def _token(self, scope, receive, send):
        body = await read_body(receive)
        headers = dict(scope.get("headers", []))
        if b"application/x-www-form-urlencoded" not in headers.get(b"content-type", b""):
            return await send_json(send, 400, {"error": "invalid_request",
                                               "error_description": "use application/x-www-form-urlencoded"})
        form = parse_form(body)
        client, err = await self._client_auth(scope, form)
        if err:
            return await send_json(send, err[0], err[1])
        grant = form.get("grant_type", "")
        if not self._resource_ok(form.get("resource", "")):
            return await send_json(send, 400, {"error": "invalid_target"})

        if grant == "authorization_code":
            code_value = form.get("code", "")
            row = self.store.get_code(code_value) if code_value else None
            if not row or row["client_id"] != client["client_id"]:
                return await send_json(send, 400, {"error": "invalid_grant"})
            if row["used"]:  # replay: assume theft, kill everything issued from this code
                self.store.revoke_family(row["family"])
                self._log(scope, "code_replay", client=client["name"])
                return await send_json(send, 400, {"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            if (form.get("redirect_uri", "") != row["redirect_uri"] or not valid_verifier(verifier)
                    or not pkce_ok(verifier, row["challenge"])):
                return await send_json(send, 400, {"error": "invalid_grant"})
            self.store.mark_code_used(code_value)
            access, refresh = self.store.issue_tokens(client["client_id"], row["scope"], row["resource"], row["family"])
            self._log(scope, "token_issued", client=client["name"])
            return await self._token_response(send, access, refresh, row["scope"])

        if grant == "refresh_token":
            row = self.store.get_token(form.get("refresh_token", ""), "refresh") if form.get("refresh_token") else None
            if not row or row["client_id"] != client["client_id"]:
                return await send_json(send, 400, {"error": "invalid_grant"})
            self.store.retire_refresh(form["refresh_token"])
            access, refresh = self.store.issue_tokens(client["client_id"], row["scope"], row["resource"], row["family"])
            self._log(scope, "token_refreshed", client=client["name"])
            return await self._token_response(send, access, refresh, row["scope"])

        await send_json(send, 400, {"error": "unsupported_grant_type"})

    async def _token_response(self, send, access, refresh, scope_value):
        await send_json(send, 200, {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                                    "refresh_token": refresh, "scope": scope_value})

    async def _revoke(self, scope, receive, send):
        form = parse_form(await read_body(receive))
        client, err = await self._client_auth(scope, form)
        if err:
            return await send_json(send, err[0], err[1])
        token = form.get("token", "")
        if token:
            for kind in ("access", "refresh"):
                row = self.store.get_token(token, kind)
                if row and row["client_id"] == client["client_id"]:
                    self.store.revoke_hash(token)
        await send_json(send, 200, {})

    # ---- the protected MCP endpoint ---------------------------------------------------------
    async def _guarded_mcp(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin-1")
        row = None
        if auth.lower().startswith("bearer ") and len(auth) < 512:
            row = self.store.get_token(auth[7:].strip(), "access")
        if not row or row["resource"] != self.cfg.resource:
            return await send_json(send, 401, {"error": "unauthorized"},
                                   self._challenge_headers("invalid_token" if auth else None))
        await self.app(scope, receive, send)
