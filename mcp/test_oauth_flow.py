"""End-to-end check of the claude.ai OAuth flow against a LOCAL public-mode MCP server.

  PASSWORD="correct horse battery" python oauth.py hash   # → H
  MCP_PUBLIC_URL=http://127.0.0.1:8799 MCP_OWNER_PASSWORD_HASH="$H" DATA_DIR=/tmp/oauth-test \
    python server.py --http --port 8799 &
  python test_oauth_flow.py http://127.0.0.1:8799

With the source-IP gate (MCP_ALLOWED_CIDRS=160.79.104.0/21 on the server), add --gate: requests
then pose as Anthropic via CF-Connecting-IP, and outsiders are checked to get 403.

Ends by tripping the 15-minute lockout, so don’t point it at the real server.
"""
import base64, hashlib, re, secrets, sys, json
from urllib.parse import urlparse, parse_qs
import httpx

args = [a for a in sys.argv[1:] if a != "--gate"]
GATE = "--gate" in sys.argv
BASE = args[0] if args else "http://127.0.0.1:8799"
PW = "correct horse battery"
CB = "https://claude.ai/api/mcp/auth_callback"
c = httpx.Client(base_url=BASE, trust_env=False, follow_redirects=False, timeout=20,
                 headers={"CF-Connecting-IP": "160.79.104.10"} if GATE else {})
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
MCP_HDRS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}

# 0. source-IP gate: outsiders only reach the browser pages
if GATE:
    out = httpx.Client(base_url=BASE, trust_env=False, follow_redirects=False, timeout=20,
                       headers={"CF-Connecting-IP": "203.0.113.7"})
    for path in ("/mcp", "/token", "/register", "/.well-known/oauth-authorization-server"):
        r = out.request("POST" if path in ("/mcp", "/token", "/register") else "GET", path)
        ok(r.status_code == 403, f"outsider refused on {path} ({r.status_code})")
    r = out.post("/mcp", json=init, headers={**MCP_HDRS, "CF-Connecting-IP": "::ffff:160.79.104.10"})
    ok(r.status_code == 401, f"IPv4-mapped Anthropic address let through ({r.status_code})")
    ok(out.get("/healthz").status_code == 200, "outsider can reach /healthz")
    ok(out.get("/login?req=x").status_code != 403, "outsider can reach /login")
    ok(out.get("/authorize").status_code != 403, "outsider can reach /authorize")
    ok(httpx.get(BASE + "/mcp", trust_env=False).status_code == 403, "no header + non-Anthropic peer refused")

# 1. unauthenticated → 401 with resource_metadata
r = c.post("/mcp", json=init, headers=MCP_HDRS)
ok(r.status_code == 401 and "resource_metadata=" in r.headers.get("www-authenticate", ""), f"401 + resource_metadata ({r.headers.get('www-authenticate')})")
rm_url = re.search(r'resource_metadata="([^"]+)"', r.headers["www-authenticate"]).group(1)
prm = c.get(urlparse(rm_url).path).json()
ok(prm["resource"].rstrip("/") == BASE + "/mcp", f"protected resource = {prm['resource']}")
asm = c.get("/.well-known/oauth-authorization-server").json()
ok("S256" in asm["code_challenge_methods_supported"] and asm.get("registration_endpoint"), "AS metadata: S256 + DCR")

# 2. DCR: foreign callback refused, Claude's accepted
r = c.post("/register", json={"redirect_uris": ["https://evil.example/cb"], "client_name": "x",
                              "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"]})
ok(r.status_code == 400, f"foreign redirect refused ({r.status_code})")
r = c.post("/register", json={"redirect_uris": [CB], "client_name": "Claude", "token_endpoint_auth_method": "none",
                              "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
ok(r.status_code == 201, f"Claude registered ({r.status_code})")
client = r.json(); cid = client["client_id"]
r = c.post("/register", json={"redirect_uris": [CB], "client_name": "Claude", "token_endpoint_auth_method": "none",
                              "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
other = r.json()["client_id"]                # a second client, registered before anyone is connected

# 3. authorize → login page
ver = secrets.token_urlsafe(48)
chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
r = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": CB, "state": "st8",
                                "code_challenge": chal, "code_challenge_method": "S256", "scope": "budget:read",
                                "resource": BASE + "/mcp"})
ok(r.status_code == 302 and "/login?req=" in r.headers["location"], "authorize → login page")
rid = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
page = c.get(f"/login?req={rid}")
ok(page.status_code == 200 and "claude.ai" in page.text, "login page shows callback host")

# 4. wrong password, then right
r = c.post("/login", data={"req": rid, "password": "nope", "action": "allow"})
ok(r.status_code == 401 and "Wrong password" in r.text, "wrong password rejected")
r = c.post("/login", data={"req": rid, "password": PW, "action": "allow"})
ok(r.status_code == 302 and r.headers["location"].startswith(CB), "right password → back to Claude")
q = parse_qs(urlparse(r.headers["location"]).query)
ok(q["state"] == ["st8"], "state echoed")
code = q["code"][0]
r = c.post("/login", data={"req": rid, "password": PW, "action": "allow"})
ok(r.status_code in (400, 401) and "expired" in r.text, "login link is single-use")

# 5. token exchange (form-encoded), bad verifier first
r = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB, "client_id": cid,
                           "code_verifier": "wrong" * 10})
ok(r.status_code == 400, "bad PKCE verifier refused")
r = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB, "client_id": cid,
                           "code_verifier": ver, "resource": BASE + "/mcp"})
ok(r.status_code == 200, f"token issued ({r.status_code} {r.text[:120]})")
tok = r.json()

# 5b. single grant: once Claude is connected, nobody else can register or sign in
r = c.post("/register", json={"redirect_uris": [CB], "client_name": "Claude", "token_endpoint_auth_method": "none",
                              "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
ok(r.status_code == 400 and "already connected" in r.text, f"new registration refused while connected ({r.status_code})")
r = c.get("/authorize", params={"response_type": "code", "client_id": other, "redirect_uri": CB, "state": "x",
                                "code_challenge": chal, "code_challenge_method": "S256", "scope": "budget:read"})
ok(r.status_code in (302, 400) and "/login?req=" not in r.headers.get("location", ""),
   f"other client can't reach sign-in while connected ({r.status_code} {r.headers.get('location', '')[:60]})")
r = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB, "client_id": cid, "code_verifier": ver})
ok(r.status_code == 400, "code is single-use")

# 6. MCP with the token: tools are read-only
H = {**MCP_HDRS, "Authorization": f"Bearer {tok['access_token']}"}
r = c.post("/mcp", json=init, headers=H)
ok(r.status_code == 200, f"initialize with token ({r.status_code})")
sid = r.headers.get("mcp-session-id")
H2 = {**H, **({"mcp-session-id": sid} if sid else {})}
c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=H2)
r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=H2)
body = r.text
data = json.loads(body.split("data: ", 1)[1].split("\n")[0]) if "data: " in body else r.json()
names = sorted(t["name"] for t in data["result"]["tools"])
print("     tools:", ", ".join(names))
ok(not {"review_decide", "review_scan", "ibkr_to_ghostfolio_import", "reconcile_account", "category_scan", "review_ask_ai"} & set(names),
   "no write tools exposed")
ok("weekly_snapshot" in names and "money_summary" in names, "report tools present")
r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "health", "arguments": {}}}, headers=H2)
ok(r.status_code == 200, "tool call works (backend offline here, so it reports an error)")

# 7. wrong Host header refused (DNS-rebinding / random vhost)
r = c.post("/mcp", json=init, headers={**H, "Host": "evil.example"})
ok(r.status_code == 421, f"foreign Host refused ({r.status_code})")

# 8. refresh rotates; replaying the old refresh token kills the grant
r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
ok(r.status_code == 200, "refresh works"); tok2 = r.json()
ok(tok2["refresh_token"] != tok["refresh_token"], "refresh token rotated")
r = c.post("/mcp", json=init, headers={**MCP_HDRS, "Authorization": f"Bearer {tok['access_token']}"})
ok(r.status_code == 401, "old access token dead after refresh")
r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
ok(r.status_code == 400, "replayed refresh token refused")
r = c.post("/mcp", json=init, headers={**MCP_HDRS, "Authorization": f"Bearer {tok2['access_token']}"})
ok(r.status_code == 401, "replay revoked the whole grant")

# 9. lockout after 5 wrong passwords
r = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": CB, "state": "s",
                                "code_challenge": chal, "code_challenge_method": "S256"})
rid = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
for _ in range(5):
    c.post("/login", data={"req": rid, "password": "bad", "action": "allow"})
r = c.post("/login", data={"req": rid, "password": PW, "action": "allow"})
ok("Too many" in r.text and r.status_code != 302, "locked out after 5 wrong tries, even with the right password")
print("ALL PASSED")
