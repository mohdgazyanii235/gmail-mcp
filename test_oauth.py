"""Run with: python3 test_oauth.py   (standard library only; drives the ASGI app directly)"""
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oauth

PUBLIC = "https://pi.example.ts.net"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "correct horse battery staple"
NOW = [1_800_000_000.0]


class Inner:
    """Stands in for the real MCP app."""
    def __init__(self):
        self.lifespan = False
        self.paths = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            self.lifespan = True
            return
        self.paths.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner:" + scope["path"].encode()})


def make_gate(tmp, configured=True, **cfg_kw):
    tmp = Path(tmp)
    cfg = oauth.Config(public_url=PUBLIC, db_path=tmp / "auth.db", auth_path=tmp / "auth.json",
                       log_path=tmp / "auth.log", clock=lambda: NOW[0], **cfg_kw)
    secret = oauth.new_totp_secret()
    if configured:
        oauth.save_owner(cfg.auth_path, {"password_hash": oauth.hash_password(PASSWORD), "totp_secret": secret})
    inner = Inner()
    return oauth.OAuthGate(inner, cfg), inner, secret


async def call(gate, method, path, query="", headers=None, body=b"", form=None, js=None, scope_extra=None):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs.append((b"content-type", b"application/x-www-form-urlencoded"))
    if js is not None:
        body = json.dumps(js).encode()
        hdrs.append((b"content-type", b"application/json"))
    scope = {"type": "http", "method": method, "path": path, "query_string": query.encode(), "headers": hdrs,
             "client": ("203.0.113.9", 1234)}
    scope.update(scope_extra or {})
    sent, chunks = [], [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    await gate(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    out_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, out_body


def run(coro):
    return asyncio.run(coro)


def pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def code_now(secret, offset=0):
    raw = base64.b32decode(secret + "=" * (-len(secret) % 8))
    return oauth.hotp(raw, int(NOW[0] // 30) + offset)


async def register(gate, **over):
    meta = {"client_name": "Claude", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    meta.update(over)
    return await call(gate, "POST", "/register", js=meta)


def auth_query(client_id, challenge, **over):
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "xyz",
         "code_challenge": challenge, "code_challenge_method": "S256", "scope": "mail offline_access",
         "resource": PUBLIC + "/mcp"}
    q.update(over)
    return urllib.parse.urlencode(q)


def req_id_from(body):
    return re.search(rb'name=req value="([^"]+)"', body).group(1).decode()


async def sign_in(gate, secret, client_id, challenge, password=PASSWORD, offset=0, **over):
    status, _, body = await call(gate, "GET", "/authorize", auth_query(client_id, challenge, **over))
    assert status == 200, (status, body[:200])
    rid = req_id_from(body)
    return await call(gate, "POST", "/authorize", form={"req": rid, "password": password, "code": code_now(secret, offset)})


def redirect_params(headers):
    loc = urllib.parse.urlsplit(headers["location"])
    return loc, {k: v[0] for k, v in urllib.parse.parse_qs(loc.query).items()}


async def full_login(gate, secret):
    NOW[0] += 30  # a fresh authenticator code each time (a code works only once)
    s, _, body = await register(gate)
    client_id = json.loads(body)["client_id"]
    verifier, challenge = pkce()
    status, headers, _ = await sign_in(gate, secret, client_id, challenge)
    assert status == 302, status
    _, params = redirect_params(headers)
    return client_id, verifier, params


async def exchange(gate, client_id, verifier, code, redirect=CALLBACK, **extra):
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
            "client_id": client_id, "code_verifier": verifier}
    form.update(extra)
    return await call(gate, "POST", "/token", form=form)


# ------------------------------------------------------------------------------------------

def test_totp_rfc6238_vector():
    secret = b"12345678901234567890"
    assert oauth.hotp(secret, 1) == "287082"   # RFC 4226 / 6238 test value
    assert oauth.hotp(secret, 0) == "755224"
    b32 = base64.b32encode(secret).decode()
    assert oauth.totp_match(b32, "287082", 59) == 1
    assert oauth.totp_match(b32, "287082", 59 + 30) == 1       # one step late is tolerated
    assert oauth.totp_match(b32, "287082", 59 + 90) is None    # three steps late is not
    assert oauth.totp_match(b32, "28708", 59) is None and oauth.totp_match(b32, "abcdef", 59) is None


def test_password_hashing():
    h = oauth.hash_password("pw-1234567890")
    assert h.startswith("scrypt$") and "pw-1234567890" not in h
    assert oauth.verify_password("pw-1234567890", h) and not oauth.verify_password("pw-1234567891", h)
    assert not oauth.verify_password("x", "garbage") and not oauth.verify_password("x", "scrypt$1$2$3$a$b")


def test_mcp_requires_token_and_challenges():
    with tempfile.TemporaryDirectory() as d:
        gate, inner, _ = make_gate(d)
        for hdrs in [None, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}]:
            s, h, b = run(call(gate, "POST", "/mcp", headers=hdrs, js={"jsonrpc": "2.0"}))
            assert s == 401, (s, hdrs)
            assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource"' in h["www-authenticate"]
        assert inner.paths == []
        s, h, _ = run(call(gate, "POST", "/mcp", headers={"Authorization": "Bearer nonsense"}))
        assert 'error="invalid_token"' in h["www-authenticate"]
        s, h, _ = run(call(gate, "GET", "/mcp/anything"))
        assert s == 401, "everything under /mcp is protected"


def test_discovery_documents():
    with tempfile.TemporaryDirectory() as d:
        gate, _, _ = make_gate(d)
        for path in ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"]:
            s, _, b = run(call(gate, "GET", path, headers={"Host": "evil.example.com"}))
            doc = json.loads(b)
            assert s == 200 and doc["resource"] == PUBLIC + "/mcp" and doc["authorization_servers"] == [PUBLIC]
        s, _, b = run(call(gate, "GET", "/.well-known/oauth-authorization-server", headers={"Host": "evil.example.com"}))
        doc = json.loads(b)
        assert doc["issuer"] == PUBLIC and doc["authorization_endpoint"] == PUBLIC + "/authorize"
        assert doc["token_endpoint"] == PUBLIC + "/token" and doc["registration_endpoint"] == PUBLIC + "/register"
        assert doc["code_challenge_methods_supported"] == ["S256"] and "offline_access" in doc["scopes_supported"]
        assert "none" in doc["token_endpoint_auth_methods_supported"]


def test_registration_rules():
    with tempfile.TemporaryDirectory() as d:
        gate, _, _ = make_gate(d)
        s, _, b = run(register(gate))
        doc = json.loads(b)
        assert s == 201 and doc["client_id"] and "client_secret" not in doc and doc["redirect_uris"] == [CALLBACK]
        for bad in [["https://evil.example.com/cb"], ["http://localhost:3000/cb"], [CALLBACK, "https://evil.example.com/cb"], [], "x"]:
            s, _, b = run(register(gate, redirect_uris=bad))
            assert s == 400 and json.loads(b)["error"] == "invalid_redirect_uri", (bad, s)
        s, _, b = run(call(gate, "POST", "/register", body=b"not json"))
        assert s == 400
        s, _, b = run(register(gate, token_endpoint_auth_method="client_secret_post"))
        assert s == 201 and json.loads(b)["client_secret"]
        s, _, _ = run(register(gate, grant_types=["client_credentials"]))
        assert s == 400
    with tempfile.TemporaryDirectory() as d:  # loopback only when switched on
        gate, _, _ = make_gate(d, allow_loopback=True)
        s, _, _ = run(register(gate, redirect_uris=["http://localhost:3118/callback"]))
        assert s == 201
        s, _, _ = run(register(gate, redirect_uris=["http://evil.example.com/callback"]))
        assert s == 400


def test_full_flow_and_refresh_rotation():
    with tempfile.TemporaryDirectory() as d:
        gate, inner, secret = make_gate(d)
        NOW[0] = 1_800_000_000.0
        client_id, verifier, params = run(full_login(gate, secret))
        assert params["state"] == "xyz" and params["iss"] == PUBLIC and params["code"]
        s, h, b = run(exchange(gate, client_id, verifier, params["code"], resource=PUBLIC + "/mcp"))
        tok = json.loads(b)
        assert s == 200 and tok["token_type"] == "Bearer" and tok["access_token"] and tok["refresh_token"]
        assert h["cache-control"] == "no-store" and tok["expires_in"] == oauth.ACCESS_TTL
        # the access token opens /mcp, and the inner app sees the same path
        s, _, b = run(call(gate, "POST", "/mcp", headers={"Authorization": "Bearer " + tok["access_token"]}))
        assert s == 200 and b == b"inner:/mcp"
        # refresh rotates
        refresh = lambda rt: call(gate, "POST", "/token", form={"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id})
        s, _, b = run(refresh(tok["refresh_token"]))
        tok2 = json.loads(b)
        assert s == 200 and tok2["refresh_token"] != tok["refresh_token"] and tok2["access_token"] != tok["access_token"]
        # old refresh token survives a short grace period, then dies
        s, _, _ = run(refresh(tok["refresh_token"]))
        assert s == 200
        NOW[0] += oauth.REFRESH_GRACE + 5
        s, _, b = run(refresh(tok["refresh_token"]))
        assert s == 400 and json.loads(b)["error"] == "invalid_grant"
        # access token expires
        NOW[0] += oauth.ACCESS_TTL + 1
        s, _, _ = run(call(gate, "POST", "/mcp", headers={"Authorization": "Bearer " + tok2["access_token"]}))
        assert s == 401
        # a refresh token is not an access token
        s, _, _ = run(call(gate, "POST", "/mcp", headers={"Authorization": "Bearer " + tok2["refresh_token"]}))
        assert s == 401
        # but the refresh token still works for a month
        s, _, _ = run(refresh(tok2["refresh_token"]))
        assert s == 200
        NOW[0] += oauth.REFRESH_TTL + 1
        s, _, _ = run(refresh(tok2["refresh_token"]))
        assert s == 400


def test_pkce_is_enforced():
    with tempfile.TemporaryDirectory() as d:
        gate, _, secret = make_gate(d)
        client_id, verifier, params = run(full_login(gate, secret))
        wrong = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        s, _, b = run(exchange(gate, client_id, wrong, params["code"]))
        assert s == 400 and json.loads(b)["error"] == "invalid_grant"
        s, _, _ = run(exchange(gate, client_id, "short", params["code"]))
        assert s == 400
        # authorize without PKCE, or with the weak method, is refused (by redirect with an error)
        cid = json.loads(run(register(gate))[2])["client_id"]
        for q in [auth_query(cid, "x" * 43, code_challenge_method="plain"),
                  auth_query(cid, "", code_challenge=""), auth_query(cid, "tooshort")]:
            s, h, _ = run(call(gate, "GET", "/authorize", q))
            _, p = redirect_params(h)
            assert s == 302 and p["error"] == "invalid_request" and p["state"] == "xyz", q


def test_code_is_single_use_and_replay_revokes():
    with tempfile.TemporaryDirectory() as d:
        gate, _, secret = make_gate(d)
        client_id, verifier, params = run(full_login(gate, secret))
        s, _, b = run(exchange(gate, client_id, verifier, params["code"]))
        tok = json.loads(b)
        assert s == 200
        s, _, b = run(exchange(gate, client_id, verifier, params["code"]))
        assert s == 400 and json.loads(b)["error"] == "invalid_grant"
        s, _, _ = run(call(gate, "POST", "/mcp", headers={"Authorization": "Bearer " + tok["access_token"]}))
        assert s == 401, "replaying a code must revoke what it produced"
        # codes expire
        client_id, verifier, params = run(full_login(gate, secret))
        NOW[0] += oauth.CODE_TTL + 1
        s, _, _ = run(exchange(gate, client_id, verifier, params["code"]))
        assert s == 400


def test_code_is_bound_to_client_and_redirect():
    with tempfile.TemporaryDirectory() as d:
        gate, _, secret = make_gate(d)
        client_id, verifier, params = run(full_login(gate, secret))
        other = json.loads(run(register(gate))[2])["client_id"]
        s, _, _ = run(exchange(gate, other, verifier, params["code"]))
        assert s == 400, "another client must not redeem this code"
        s, _, _ = run(exchange(gate, client_id, verifier, params["code"], redirect="https://claude.com/api/mcp/auth_callback"))
        assert s == 400, "redirect_uri must match the authorization request"
        s, _, _ = run(exchange(gate, "unknown-client", verifier, params["code"]))
        assert s == 401


def test_bad_authorize_requests_never_redirect():
    with tempfile.TemporaryDirectory() as d:
        gate, _, _ = make_gate(d)
        cid = json.loads(run(register(gate))[2])["client_id"]
        _, ch = pkce()
        for q in [auth_query("nope", ch),
                  auth_query(cid, ch, redirect_uri="https://evil.example.com/cb"),
                  auth_query(cid, ch, redirect_uri=CALLBACK + "/../x"),
                  auth_query(cid, ch, redirect_uri="")]:
            s, h, b = run(call(gate, "GET", "/authorize", q))
            assert s == 400 and "location" not in h, q
        s, h, _ = run(call(gate, "GET", "/authorize", auth_query(cid, ch, response_type="token")))
        assert s == 302 and redirect_params(h)[1]["error"] == "unsupported_response_type"
        s, h, _ = run(call(gate, "GET", "/authorize", auth_query(cid, ch, resource="https://other.example.com/mcp")))
        assert s == 302 and redirect_params(h)[1]["error"] == "invalid_target"
        s, _, _ = run(call(gate, "GET", "/authorize", auth_query(cid, ch, resource=PUBLIC + "/mcp/")))
        assert s == 200, "a trailing slash on the resource is tolerated"


def test_login_page_is_safe_and_informative():
    with tempfile.TemporaryDirectory() as d:
        gate, _, _ = make_gate(d)
        cid = json.loads(run(register(gate, client_name="<script>alert(1)</script>Claude"))[2])["client_id"]
        _, ch = pkce()
        s, h, b = run(call(gate, "GET", "/authorize", auth_query(cid, ch)))
        text = b.decode()
        assert s == 200 and "<script>" not in text and "&lt;script&gt;" in text
        assert "claude.ai" in text and "type=password" in text and "one-time-code" in text
        assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["cache-control"] == "no-store"


def test_wrong_password_or_code_is_refused():
    with tempfile.TemporaryDirectory() as d:
        gate, _, secret = make_gate(d)
        cid = json.loads(run(register(gate))[2])["client_id"]
        _, ch = pkce()
        for password, offset in [("wrong password", 0), (PASSWORD, 5)]:
            s, h, b = run(sign_in(gate, secret, cid, ch, password=password, offset=offset))
            assert s == 401 and "location" not in h and b"Wrong password or code" in b
        s, _, b = run(call(gate, "POST", "/authorize", form={"req": "made-up", "password": PASSWORD, "code": code_now(secret)}))
        assert s == 400, "an unknown request id is refused"
        s, _, _ = run(call(gate, "POST", "/authorize", form={"password": PASSWORD, "code": code_now(secret)}))
        assert s == 400


def test_totp_code_works_only_once():
    with tempfile.TemporaryDirectory() as d:
        NOW[0] = 1_800_000_000.0
        gate, _, secret = make_gate(d)
        cid = json.loads(run(register(gate))[2])["client_id"]
        _, ch = pkce()
        s, _, _ = run(sign_in(gate, secret, cid, ch))
        assert s == 302
        s, _, _ = run(sign_in(gate, secret, cid, ch))   # same 30-second code again
        assert s == 401, "a code that was already used must be refused"
        NOW[0] += 30
        s, _, _ = run(sign_in(gate, secret, cid, ch))
        assert s == 302


def test_brute_force_lockout():
    with tempfile.TemporaryDirectory() as d:
        NOW[0] = 1_800_000_000.0
        gate, _, secret = make_gate(d)
        cid = json.loads(run(register(gate))[2])["client_id"]
        _, ch = pkce()
        for i in range(oauth.LOGIN_FAIL_LIMIT):
            # a fresh sign-in page each time so the per-page limit is not what stops us
            s, _, _ = run(sign_in(gate, secret, cid, ch, password=f"guess {i}"))
            assert s == 401
        s, h, b = run(sign_in(gate, secret, cid, ch))   # even the right password is refused now
        assert s == 429 and "location" not in h and b"Too many" in b
        NOW[0] += oauth.LOGIN_WINDOW + 5
        s, _, _ = run(sign_in(gate, secret, cid, ch))
        assert s == 302, "lockout ends after the window"


def test_per_page_attempt_limit():
    with tempfile.TemporaryDirectory() as d:
        gate, _, secret = make_gate(d)
        cid = json.loads(run(register(gate))[2])["client_id"]
        _, ch = pkce()
        s, _, body = run(call(gate, "GET", "/authorize", auth_query(cid, ch)))
        rid = req_id_from(body)
        statuses = []
        for i in range(oauth.PENDING_MAX_ATTEMPTS):
            s, _, _ = run(call(gate, "POST", "/authorize", form={"req": rid, "password": "bad", "code": "000000"}))
            statuses.append(s)
        assert statuses[:-1] == [401] * (oauth.PENDING_MAX_ATTEMPTS - 1) and statuses[-1] == 400
        s, _, _ = run(call(gate, "POST", "/authorize", form={"req": rid, "password": PASSWORD, "code": code_now(secret)}))
        assert s == 400, "that page is dead after too many attempts"


def test_sign_in_page_expires():
    with tempfile.TemporaryDirectory() as d:
        NOW[0] = 1_800_000_000.0
        gate, _, secret = make_gate(d)
        cid = json.loads(run(register(gate))[2])["client_id"]
        _, ch = pkce()
        s, _, body = run(call(gate, "GET", "/authorize", auth_query(cid, ch)))
        rid = req_id_from(body)
        NOW[0] += oauth.PENDING_TTL + 1
        s, _, _ = run(call(gate, "POST", "/authorize", form={"req": rid, "password": PASSWORD, "code": code_now(secret)}))
        assert s == 400


def test_not_configured_means_nobody_gets_in():
    with tempfile.TemporaryDirectory() as d:
        gate, _, _ = make_gate(d, configured=False)
        cid = json.loads(run(register(gate))[2])["client_id"]
        _, ch = pkce()
        s, _, b = run(call(gate, "GET", "/authorize", auth_query(cid, ch)))
        assert s == 503 and b"manage_auth.py setup" in b


def test_confidential_client_needs_its_secret():
    with tempfile.TemporaryDirectory() as d:
        gate, _, secret = make_gate(d)
        doc = json.loads(run(register(gate, token_endpoint_auth_method="client_secret_post"))[2])
        cid, csec = doc["client_id"], doc["client_secret"]
        verifier, ch = pkce()
        s, h, _ = run(sign_in(gate, secret, cid, ch))
        code = redirect_params(h)[1]["code"]
        s, _, _ = run(exchange(gate, cid, verifier, code))
        assert s == 401, "no secret"
        s, _, _ = run(exchange(gate, cid, verifier, code, client_secret="wrong"))
        assert s == 401
        s, _, _ = run(exchange(gate, cid, verifier, code, client_secret=csec))
        assert s == 200
        # HTTP Basic works as well
        verifier, ch = pkce()
        NOW[0] += 30
        s, h, _ = run(sign_in(gate, secret, cid, ch))
        code = redirect_params(h)[1]["code"]
        basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
        s, _, _ = run(call(gate, "POST", "/token", headers={"Authorization": "Basic " + basic},
                           form={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier}))
        assert s == 200


def test_token_endpoint_input_handling():
    with tempfile.TemporaryDirectory() as d:
        gate, _, _ = make_gate(d)
        cid = json.loads(run(register(gate))[2])["client_id"]
        s, _, _ = run(call(gate, "POST", "/token", js={"grant_type": "authorization_code", "client_id": cid}))
        assert s == 400, "JSON is not accepted at /token"
        s, _, b = run(call(gate, "POST", "/token", form={"grant_type": "password", "client_id": cid}))
        assert s == 400 and json.loads(b)["error"] == "unsupported_grant_type"
        s, _, _ = run(call(gate, "POST", "/token", form={"grant_type": "refresh_token", "refresh_token": "nope", "client_id": cid}))
        assert s == 400
        s, _, _ = run(call(gate, "POST", "/token", body=b"x" * (oauth.MAX_BODY + 1), form=None,
                           headers={"Content-Type": "application/x-www-form-urlencoded"}))
        assert s == 413


def test_revoke():
    with tempfile.TemporaryDirectory() as d:
        gate, _, secret = make_gate(d)
        client_id, verifier, params = run(full_login(gate, secret))
        tok = json.loads(run(exchange(gate, client_id, verifier, params["code"]))[2])
        bearer = {"Authorization": "Bearer " + tok["access_token"]}
        assert run(call(gate, "POST", "/mcp", headers=bearer))[0] == 200
        s, _, _ = run(call(gate, "POST", "/revoke", form={"token": tok["access_token"], "client_id": client_id}))
        assert s == 200
        assert run(call(gate, "POST", "/mcp", headers=bearer))[0] == 401
        # another client cannot revoke it
        other = json.loads(run(register(gate))[2])["client_id"]
        client_id, verifier, params = run(full_login(gate, secret))
        tok = json.loads(run(exchange(gate, client_id, verifier, params["code"]))[2])
        run(call(gate, "POST", "/revoke", form={"token": tok["access_token"], "client_id": other}))
        assert run(call(gate, "POST", "/mcp", headers={"Authorization": "Bearer " + tok["access_token"]}))[0] == 200


def test_revoke_all_and_storage_hygiene():
    with tempfile.TemporaryDirectory() as d:
        gate, _, secret = make_gate(d)
        client_id, verifier, params = run(full_login(gate, secret))
        tok = json.loads(run(exchange(gate, client_id, verifier, params["code"]))[2])
        gate.store.revoke_all()
        assert run(call(gate, "POST", "/mcp", headers={"Authorization": "Bearer " + tok["access_token"]}))[0] == 401
        # nothing readable is stored: only hashes
        client_id, verifier, params = run(full_login(gate, secret))
        tok = json.loads(run(exchange(gate, client_id, verifier, params["code"]))[2])
        raw = b"".join(p.read_bytes() for p in Path(d).iterdir() if p.is_file())
        for secret_value in (tok["access_token"], tok["refresh_token"], params["code"], PASSWORD):
            assert secret_value.encode() not in raw, "a secret was stored in clear text"
        for name in ("auth.db", "auth.json"):
            assert oct((Path(d) / name).stat().st_mode & 0o777) == "0o600", name
        log = (Path(d) / "auth.log").read_text()
        assert "login_ok" in log and tok["access_token"] not in log and PASSWORD not in log


def test_other_paths_and_lifespan():
    with tempfile.TemporaryDirectory() as d:
        gate, inner, _ = make_gate(d)
        for method, path in [("GET", "/"), ("GET", "/admin"), ("POST", "/mcpx"), ("GET", "/.env"), ("GET", "/token"), ("GET", "/register")]:
            s, _, _ = run(call(gate, method, path))
            assert s == 404, (method, path, s)
        assert inner.paths == []
        sent = []

        async def go():
            await gate({"type": "lifespan"}, None, None)
            await gate({"type": "websocket", "path": "/mcp"}, None, None)

        asyncio.run(go())
        assert inner.lifespan is True


def test_forwarded_ip_is_logged():
    with tempfile.TemporaryDirectory() as d:
        gate, _, _ = make_gate(d)
        run(call(gate, "POST", "/register", headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"},
                 js={"redirect_uris": ["https://evil.example.com/cb"]}))
        assert "198.51.100.7" in (Path(d) / "auth.log").read_text()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"\n{len(tests)} passed")
