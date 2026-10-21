#!/usr/bin/env bash
# Checks the server on the Pi itself, before you involve Claude.   Run: ./smoke_test.sh
cd "$(dirname "$0")"
set -a; . ./.env; set +a
BASE="http://127.0.0.1:${PORT:-8765}"
HOST_HDR="${PUBLIC_HOSTNAME}"
fails=0

ok()   { echo "  PASS  $1"; }
bad()  { echo "  FAIL  $1"; fails=$((fails+1)); }
warn() { echo "  WARN  $1"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
pyget() { python3 -c "import sys,json
try: print(json.load(sys.stdin)$1)
except Exception: print('')"; }

JSON=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'
LIST='{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
CALL='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_accounts","arguments":{}}}'
CALLBACK='https://claude.ai/api/mcp/auth_callback'
CHALLENGE='E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'

echo "Sign-in protection:"
[ -f auth.json ] && ok "password and authenticator are set" || warn "no password set yet. Run: .venv/bin/python manage_auth.py setup"

hdrs="$(curl -s -D - -o /dev/null -X POST "$BASE/mcp" "${JSON[@]}" -d "$INIT")"
echo "$hdrs" | head -1 | grep -q ' 401' && ok "the mail endpoint refuses anyone without a token (401)" || bad "/mcp should answer 401 without a token"
echo "$hdrs" | grep -qi "www-authenticate: Bearer resource_metadata=\"https://$PUBLIC_HOSTNAME/.well-known/oauth-protected-resource\"" \
  && ok "the 401 points Claude to the sign-in discovery document" || bad "401 is missing the resource_metadata pointer"

[ "$(code -X POST "$BASE/mcp" "${JSON[@]}" -H 'Authorization: Bearer not-a-real-token' -d "$INIT")" = "401" ] \
  && ok "a made-up token is refused (401)" || bad "a made-up token should give 401"
[ "$(code "$BASE/")" = "404" ] && ok "unknown paths are refused (404)" || bad "root path should give 404"

PRM="$(curl -s "$BASE/.well-known/oauth-protected-resource")"
[ "$(echo "$PRM" | pyget "['resource']")" = "https://$PUBLIC_HOSTNAME/mcp" ] \
  && ok "discovery: resource address is https://$PUBLIC_HOSTNAME/mcp" || bad "discovery document has the wrong resource: $PRM"
ASM="$(curl -s "$BASE/.well-known/oauth-authorization-server")"
[ "$(echo "$ASM" | pyget "['issuer']")" = "https://$PUBLIC_HOSTNAME" ] && echo "$ASM" | grep -q 'S256' \
  && ok "discovery: sign-in server details are published" || bad "authorization server metadata looks wrong: $ASM"

[ "$(code -X POST "$BASE/register" -H 'Content-Type: application/json' \
   -d '{"client_name":"evil","redirect_uris":["https://evil.example.com/cb"],"token_endpoint_auth_method":"none"}')" = "400" ] \
  && ok "registration refuses callback addresses that are not Claude's (400)" || bad "registration accepted a foreign callback address"

REG="$(curl -s -X POST "$BASE/register" -H 'Content-Type: application/json' \
   -d "{\"client_name\":\"smoke test\",\"redirect_uris\":[\"$CALLBACK\"],\"token_endpoint_auth_method\":\"none\"}")"
CID="$(echo "$REG" | pyget "['client_id']")"
[ -n "$CID" ] && ok "registration accepts Claude's callback address" || bad "registration failed: $REG"

AUTHQ="response_type=code&client_id=$CID&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&state=s&code_challenge=$CHALLENGE&code_challenge_method=S256"
page="$(curl -s -w '\n%{http_code}' "$BASE/authorize?$AUTHQ")"
status="${page##*$'\n'}"
if [ "$status" = "200" ] && echo "$page" | grep -q 'type=password' && echo "$page" | grep -q 'one-time-code'; then
  ok "the sign-in page asks for a password and an authenticator code"
elif [ "$status" = "503" ]; then
  warn "sign-in page says setup is not finished (run manage_auth.py setup)"
else
  bad "sign-in page did not load correctly (status $status)"
fi
EVILQ="${AUTHQ/redirect_uri=https%3A%2F%2Fclaude.ai/redirect_uri=https%3A%2F%2Fevil.example.com}"
[ "$(code "$BASE/authorize?$EVILQ")" = "400" ] \
  && ok "a sign-in link with a foreign return address is refused (400)" || bad "foreign return address should give 400"
tc="$(code -X POST "$BASE/token" -d "grant_type=authorization_code&code=guess&client_id=$CID&code_verifier=x&redirect_uri=$CALLBACK")"
[ "$tc" = "400" ] || [ "$tc" = "401" ] \
  && ok "guessed codes are refused ($tc)" || bad "/token should refuse a guessed code (got $tc)"

echo
echo "Mail server (using a 5-minute test token made on this Pi):"
TOKEN="$(.venv/bin/python manage_auth.py smoke-token 2>/dev/null)"
AUTH=(-H "Authorization: Bearer $TOKEN")
if [ -z "$TOKEN" ]; then
  bad "could not create the test token (is the .venv installed? did setup.sh finish?)"
else
  out="$(curl -s -X POST "$BASE/mcp" -H "Host: $HOST_HDR" "${JSON[@]}" "${AUTH[@]}" -d "$INIT")"
  echo "$out" | grep -q '"serverInfo"' && ok "the server answers a signed-in request" || bad "initialize failed. Output: $out"

  out="$(curl -s -X POST "$BASE/mcp" -H "Host: $HOST_HDR" "${JSON[@]}" "${AUTH[@]}" -d "$LIST")"
  echo "$out" | grep -q 'send_reply' && ok "tools are listed" || bad "tools/list failed. Output: $out"

  [ "$(code -X POST "$BASE/mcp" -H 'Host: evil.example.com' "${JSON[@]}" "${AUTH[@]}" -d "$INIT")" = "421" ] \
    && ok "a foreign Host header is refused (421)" \
    || warn "foreign Host header was not refused (your mcp version may skip this check; sign-in still protects the server)"

  echo
  echo "Accounts the server can see:"
  curl -s -X POST "$BASE/mcp" -H "Host: $HOST_HDR" "${JSON[@]}" "${AUTH[@]}" -d "$CALL" \
    | python3 -c 'import sys,json
try:
    r=json.load(sys.stdin)
    items=r["result"].get("structuredContent",{}).get("result")
    if items is None:
        items=[json.loads(c["text"]) for c in r["result"]["content"] if c.get("type")=="text"]
    print("  (none yet)" if not items else "\n".join("  %-10s %s"%(i["account"],i["email"]) for i in items))
except Exception as e:
    print("  could not read the account list:", e)'
fi

echo
if [ "$fails" = "0" ]; then
  echo "All checks passed."
  echo "Connector address for Claude:  https://$PUBLIC_HOSTNAME/mcp"
  echo "(Claude will open a sign-in page on your Pi. You sign in with your password and authenticator code.)"
else
  echo "$fails check(s) failed. See the Troubleshooting section of the guide, or run: journalctl -u gmail-mcp -n 50 --no-pager"
  exit 1
fi
