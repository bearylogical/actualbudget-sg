"""
Single-owner OAuth 2.1 for the public budget-app MCP endpoint (claude.ai custom connector).

claude.ai connects to a remote MCP server with OAuth: it registers itself (Dynamic Client
Registration), sends you to /authorize with PKCE, you sign in, it swaps the code for tokens
and refreshes them on its own. This module is that authorization server, cut down to one
person:

* Sign-in is one owner password (MCP_OWNER_PASSWORD_HASH, made with `python oauth.py hash`).
* Only Claude's callback can register (https://claude.ai/api/mcp/auth_callback and the
  claude.com twin); loopback callbacks for Claude Code only with MCP_ALLOW_LOOPBACK=true.
  Anyone else who finds the URL can't even get a client registered.
* Tokens are random, stored as SHA-256 hashes in SQLite (DATA_DIR/oauth.db): access 1 h,
  refresh 30 days and rotated on every use. A reused (stolen) refresh token revokes the
  whole grant.
* Wrong passwords lock sign-in for 15 minutes after 5 tries.
* Single grant (MCP_SINGLE_GRANT, default on): while Claude holds a live grant, no other
  client can register or sign in, so a sign-in link someone else generated (e.g. by adding
  your URL as a connector in their own claude.ai account) is refused even with the right
  password. To reconnect, run `python oauth.py revoke-all` first.
* Every registration, sign-in, wrong password, lockout, token replay and blocked IP is
  sent to the audit trail (mcp_audit.py → backend → Telegram digest).
* Optional source-IP gate (MCP_ALLOWED_CIDRS, e.g. Anthropic's 160.79.104.0/21): everything
  except the browser pages (/authorize, /login) and /healthz is refused from other addresses.
  It's the WAF rule from docs/SERVER.md, done in-app for plans/tunnels without one.

CLI:
  python oauth.py hash               # prompt for a password, print MCP_OWNER_PASSWORD_HASH
  python oauth.py list               # registered clients and live grants
  python oauth.py revoke-all         # sign Claude out everywhere (it will ask you to sign in again)
  python oauth.py unlock             # clear the wrong-password lockout
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import mcp_audit

SCOPE = "budget:read"
ACCESS_TTL = int(os.getenv("MCP_ACCESS_TTL", "3600"))
REFRESH_TTL = int(os.getenv("MCP_REFRESH_TTL", str(30 * 86400)))
CODE_TTL = 300
PENDING_TTL = 600
MAX_FAILS, LOCK_SECONDS = 5, 900
MAX_CLIENTS = 50

CLAUDE_CALLBACKS = {"https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"}


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── password hashing (scrypt, stdlib) ─────────────────────────────────────────

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return "scrypt:" + base64.b64encode(salt).decode() + ":" + base64.b64encode(dk).decode()


def check_password(pw: str, stored: str) -> bool:
    try:
        kind, salt, dk = stored.split(":")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=2**15, r=8, p=1,
                             maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:
        return False


def redirect_allowed(uri: str, allow_loopback: bool) -> bool:
    if uri in CLAUDE_CALLBACKS:
        return True
    if allow_loopback:
        p = urlparse(uri)
        return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1") and p.path == "/callback"
    return False


# ── storage ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL,
  client_id TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT, expires REAL NOT NULL, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS lockout (k TEXT PRIMARY KEY, fails INTEGER NOT NULL, until REAL NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def gc(self):
        now = time.time()
        for t in ("pending", "codes"):
            self.q(f"DELETE FROM {t} WHERE expires < ?", (now,))
        self.q("DELETE FROM tokens WHERE expires < ?", (now - 86400,))


class OwnerOAuthProvider:
    """OAuthAuthorizationServerProvider for one owner, persisted in SQLite."""

    def __init__(self, public_url: str, password_hash: str, data_dir: Path, allow_loopback: bool = False,
                 single_grant: bool = True):
        self.public_url = public_url.rstrip("/")
        self.single_grant = single_grant
        self.resource = self.public_url + "/mcp"
        self.password_hash = password_hash
        self.allow_loopback = allow_loopback
        self.store = Store(data_dir / "oauth.db")

    # clients ---------------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        r = self.store.q("SELECT info FROM clients WHERE client_id=?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(r[0]["info"]) if r else None

    def connected_clients(self) -> set[str]:
        """Clients holding a live grant (an unused, unexpired refresh token)."""
        return {r["client_id"] for r in self.store.q(
            "SELECT DISTINCT client_id FROM tokens WHERE kind='refresh' AND used=0 AND expires>?", (time.time(),))}

    def _pinned_out(self, client_id: str | None) -> bool:
        live = self.connected_clients()
        return self.single_grant and bool(live) and client_id not in live

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(redirect_allowed(u, self.allow_loopback) for u in uris):
            mcp_audit.auth("registration_refused", ok=False, reason="redirect_uri", redirect_uris=uris[:3])
            raise RegistrationError("invalid_redirect_uri",
                                    "this server only accepts Claude's OAuth callback")
        if self._pinned_out(client_info.client_id):
            mcp_audit.auth("registration_refused", ok=False, reason="already connected",
                           client_name=client_info.client_name)
            raise RegistrationError("invalid_client_metadata",
                                    "Claude is already connected to this server. To connect again, run "
                                    "`python oauth.py revoke-all` on the server first.")
        # keep the table small: DCR registers a new client on every fresh connection
        self.store.q("DELETE FROM clients WHERE client_id IN (SELECT client_id FROM clients ORDER BY created DESC "
                     "LIMIT -1 OFFSET ?) AND client_id NOT IN (SELECT DISTINCT client_id FROM tokens)",
                     (MAX_CLIENTS - 1,))
        self.store.q("INSERT OR REPLACE INTO clients VALUES (?,?,?)",
                     (client_info.client_id, client_info.model_dump_json(), time.time()))
        mcp_audit.auth("registered", client=(client_info.client_id or "")[:12], client_name=client_info.client_name)

    # authorize → our sign-in page --------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not redirect_allowed(str(params.redirect_uri), self.allow_loopback):
            mcp_audit.auth("authorize_refused", ok=False, client=client.client_id[:12], reason="redirect_uri")
            raise AuthorizeError("unauthorized_client", "redirect URI not allowed")
        if self._pinned_out(client.client_id):
            mcp_audit.auth("authorize_refused", ok=False, client=client.client_id[:12], reason="already connected")
            raise AuthorizeError("access_denied", "Claude is already connected to this server. To connect "
                                                  "again, run `python oauth.py revoke-all` on the server first.")
        self.store.gc()
        rid = secrets.token_urlsafe(24)
        self.store.q("INSERT INTO pending VALUES (?,?,?,?)",
                     (rid, client.client_id, params.model_dump_json(), time.time() + PENDING_TTL))
        return f"{self.public_url}/login?req={rid}"

    def pending(self, rid: str) -> tuple[str, AuthorizationParams] | None:
        r = self.store.q("SELECT client_id, params FROM pending WHERE id=? AND expires>?", (rid, time.time()))
        return (r[0]["client_id"], AuthorizationParams.model_validate_json(r[0]["params"])) if r else None

    def locked_for(self) -> int:
        r = self.store.q("SELECT until FROM lockout WHERE k='owner'")
        return max(0, int(r[0]["until"] - time.time())) if r else 0

    def complete_login(self, rid: str, password: str, ip: str | None = None) -> tuple[str | None, str | None]:
        """→ (redirect_url, error). Single-use: the pending request is consumed on success."""
        if (wait := self.locked_for()):
            mcp_audit.auth("login_failed", ok=False, reason="locked", ip=ip)
            return None, f"Too many wrong passwords. Try again in {wait // 60 + 1} min."
        p = self.pending(rid)
        if not p:
            return None, "This sign-in link expired. Start again from Claude."
        client_id, params = p
        if self._pinned_out(client_id):
            mcp_audit.auth("authorize_refused", ok=False, client=client_id[:12], reason="already connected", ip=ip)
            return None, "Claude is already connected. To connect again, run `python oauth.py revoke-all` on the server."
        if not check_password(password, self.password_hash):
            r = self.store.q("SELECT fails FROM lockout WHERE k='owner'")
            fails = (r[0]["fails"] if r else 0) + 1
            until = time.time() + LOCK_SECONDS if fails >= MAX_FAILS else 0
            self.store.q("INSERT OR REPLACE INTO lockout VALUES ('owner',?,?)", (0 if until else fails, until))
            mcp_audit.auth("login_failed", ok=False, client=client_id[:12], ip=ip)
            if until:
                mcp_audit.auth("lockout", ok=False, ip=ip, minutes=LOCK_SECONDS // 60)
            return None, "Wrong password."
        self.store.q("DELETE FROM lockout WHERE k='owner'")
        self.store.q("DELETE FROM pending WHERE id=?", (rid,))
        code = secrets.token_urlsafe(32)
        ac = AuthorizationCode(
            code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_TTL, client_id=client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource or self.resource, subject="owner")
        self.store.q("INSERT INTO codes VALUES (?,?,?)", (_h(code), ac.model_dump_json(), ac.expires_at))
        mcp_audit.auth("signed_in", client=client_id[:12], ip=ip)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), None

    def deny(self, rid: str) -> str | None:
        p = self.pending(rid)
        if not p:
            return None
        self.store.q("DELETE FROM pending WHERE id=?", (rid,))
        mcp_audit.auth("sign_in_denied", client=p[0][:12])
        return construct_redirect_uri(str(p[1].redirect_uri), error="access_denied", state=p[1].state)

    # codes → tokens ----------------------------------------------------------
    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        r = self.store.q("SELECT data FROM codes WHERE hash=? AND expires>?", (_h(authorization_code), time.time()))
        if not r:
            return None
        ac = AuthorizationCode.model_validate_json(r[0]["data"])
        return ac if ac.client_id == client.client_id else None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, grant_id: str | None = None) -> OAuthToken:
        grant_id = grant_id or secrets.token_hex(8)
        at, rt = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = time.time()
        sc = json.dumps(scopes)
        self.store.q("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,0)", (_h(at), "access", grant_id, client_id, sc, resource, now + ACCESS_TTL))
        self.store.q("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,0)", (_h(rt), "refresh", grant_id, client_id, sc, resource, now + REFRESH_TTL))
        return OAuthToken(access_token=at, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=rt, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        gone = self.store.q("DELETE FROM codes WHERE hash=? RETURNING hash", (_h(authorization_code.code),))
        if not gone:                                   # already used → refuse
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        r = self.store.q("SELECT * FROM tokens WHERE hash=? AND kind='refresh'", (_h(refresh_token),))
        if not r or r[0]["client_id"] != client.client_id:
            return None
        row = r[0]
        if row["used"]:                                # replay of a rotated token → kill the grant
            self.store.q("DELETE FROM tokens WHERE grant_id=?", (row["grant_id"],))
            mcp_audit.auth("refresh_replay", ok=False, client=row["client_id"][:12], grant=row["grant_id"])
            return None
        if row["expires"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=json.loads(row["scopes"]),
                            expires_at=int(row["expires"]), resource=row["resource"], subject="owner")

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        r = self.store.q("UPDATE tokens SET used=1 WHERE hash=? AND used=0 RETURNING grant_id", (_h(refresh_token.token),))
        if not r:
            raise TokenError("invalid_grant", "refresh token already used")
        grant = r[0]["grant_id"]
        self.store.q("DELETE FROM tokens WHERE grant_id=? AND kind='access'", (grant,))
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource, grant)

    async def load_access_token(self, token: str) -> AccessToken | None:
        r = self.store.q("SELECT * FROM tokens WHERE hash=? AND kind='access' AND expires>?", (_h(token), time.time()))
        if not r:
            return None
        row = r[0]
        return AccessToken(token=token, client_id=row["client_id"], scopes=json.loads(row["scopes"]),
                           expires_at=int(row["expires"]), resource=row["resource"], subject="owner")

    async def revoke_token(self, token) -> None:
        r = self.store.q("SELECT grant_id FROM tokens WHERE hash=?", (_h(token.token),))
        if r:
            self.store.q("DELETE FROM tokens WHERE grant_id=?", (r[0]["grant_id"],))
            mcp_audit.auth("revoked", grant=r[0]["grant_id"])


# ── sign-in page ──────────────────────────────────────────────────────────────

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Budget app · sign in</title>
<meta name="referrer" content="no-referrer">
<style>
:root{{--bg:#f6f7f9;--card:#fff;--text:#1a1d27;--muted:#5b6170;--border:#d9dce3;--accent:#3987e5;--err:#c0392b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12141b;--card:#1a1d27;--text:#e8eaf0;--muted:#9aa0ad;--border:#2c3040;--accent:#5a9cf0;--err:#ef6f5e}}}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;width:100%;max-width:380px}}
h1{{font-size:18px;margin:0 0 4px}}p{{margin:0 0 16px;color:var(--muted);font-size:13px}}
.who{{font-size:13px;border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:16px}}
.who b{{color:var(--text)}}label{{font-size:13px;color:var(--muted)}}
input{{width:100%;padding:10px;margin:6px 0 14px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--text);font-size:15px}}
.row{{display:flex;flex-direction:row-reverse;gap:8px}}button{{flex:1;padding:10px;border-radius:8px;border:1px solid var(--border);font-size:14px;cursor:pointer;background:transparent;color:var(--text)}}
button.go{{background:var(--accent);border-color:var(--accent);color:#fff}}.err{{color:var(--err);font-size:13px;margin-bottom:12px}}
</style></head><body><form class="card" method="post" action="/login">
<h1>Allow Claude to read your budget?</h1>
<p>Read-only: money summary, weekly snapshot, review queue, health. Nothing in Actual or Ghostfolio can be changed through this connection.</p>
<div class="who">Requested by <b>{client}</b><br>Returns to <b>{host}</b></div>
{error}
<input type="hidden" name="req" value="{rid}">
<label for="pw">Owner password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<div class="row"><button class="go" type="submit" name="action" value="allow">Allow</button>
<button type="submit" name="action" value="deny" formnovalidate>Deny</button></div>
</form></body></html>"""


def login_page(provider: OwnerOAuthProvider, rid: str, error: str = "") -> tuple[int, str]:
    p = provider.pending(rid)
    if not p:
        return 400, "<!doctype html><title>Expired</title><p style='font-family:system-ui;padding:24px'>" \
                    "This sign-in link expired or was already used. Start again from Claude.</p>"
    client_id, params = p
    # the client name is self-declared at registration; show the callback host, which we enforce
    client = provider.store.q("SELECT info FROM clients WHERE client_id=?", (client_id,))
    name = json.loads(client[0]["info"]).get("client_name") if client else None
    return 200, LOGIN_HTML.format(client=html.escape(name or "an MCP client"),
                                  host=html.escape(urlparse(str(params.redirect_uri)).netloc),
                                  rid=html.escape(rid),
                                  error=f'<div class="err">{html.escape(error)}</div>' if error else "")


def install_routes(mcp, provider: OwnerOAuthProvider):
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse

    headers = {"Cache-Control": "no-store", "X-Frame-Options": "DENY",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://claude.ai https://claude.com http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'"}

    @mcp.custom_route("/login", methods=["GET"])
    async def login_get(request: Request):
        status, body = login_page(provider, request.query_params.get("req", ""))
        return HTMLResponse(body, status_code=status, headers=headers)

    @mcp.custom_route("/login", methods=["POST"])
    async def login_post(request: Request):
        form = await request.form()
        rid = str(form.get("req", ""))
        if form.get("action") == "deny":
            url = provider.deny(rid)
            return RedirectResponse(url, status_code=302) if url else HTMLResponse("Cancelled.", headers=headers)
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
        url, err = provider.complete_login(rid, str(form.get("password", "")), ip)
        if url:
            return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
        status, body = login_page(provider, rid, err)
        return HTMLResponse(body, status_code=401 if status == 200 else status, headers=headers)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        return HTMLResponse("ok")


# Your browser needs these from anywhere; the probe hits /healthz from inside the container.
OPEN_PATHS = ("/authorize", "/login", "/healthz")


def parse_cidrs(value: str) -> list:
    return [ipaddress.ip_network(c.strip(), strict=False) for c in value.split(",") if c.strip()]


class SourceIPGate:
    """ASGI wrapper: 403 for requests from outside `cidrs`, except OPEN_PATHS.

    The client address is Cloudflare's CF-Connecting-IP (Cloudflare overwrites it, so it can't
    be spoofed through the tunnel), else the TCP peer. Only cloudflared and other containers on
    the compose network can reach this port directly, so the header is trusted from any peer."""

    def __init__(self, app, cidrs: list):
        self.app, self.cidrs = app, cidrs

    def allowed(self, path: str, headers: dict, peer: str | None) -> bool:
        if path in OPEN_PATHS:                       # exact: no sub-paths exist, so none are opened
            return True
        raw = headers.get(b"cf-connecting-ip", b"").decode().strip() or peer or ""
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in n for n in self.cidrs)

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers") or [])
        peer = (scope.get("client") or (None,))[0]
        if scope["type"] == "http" and not self.allowed(scope.get("path", ""), headers, peer):
            mcp_audit.auth("ip_blocked", ok=False, path=scope.get("path", "")[:60],
                           ip=headers.get(b"cf-connecting-ip", b"").decode()[:45] or peer)
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"text/plain"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b"Forbidden"})
            return
        await self.app(scope, receive, send)


def _cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "hash":
        import getpass
        pw = os.getenv("PASSWORD") or getpass.getpass("Owner password: ")
        if len(pw) < 12:
            sys.exit("Use at least 12 characters — this page is on the public internet.")
        if not os.getenv("PASSWORD") and getpass.getpass("Again: ") != pw:
            sys.exit("Passwords didn't match.")
        print(hash_password(pw))
        return
    store = Store(Path(os.getenv("DATA_DIR", "/data")) / "oauth.db")
    if cmd == "list":
        for r in store.q("SELECT client_id, info, created FROM clients ORDER BY created"):
            info = json.loads(r["info"])
            print(f"client {r['client_id']}  {info.get('client_name')}  {info.get('redirect_uris')}  "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['created']))}")
        for r in store.q("SELECT grant_id, client_id, MAX(expires) e FROM tokens WHERE kind='refresh' AND used=0 GROUP BY grant_id"):
            print(f"grant  {r['grant_id']}  client {r['client_id']}  refresh valid until "
                  f"{time.strftime('%Y-%m-%d', time.localtime(r['e']))}")
    elif cmd == "unlock":
        store.q("DELETE FROM lockout")
        print("Sign-in unlocked.")
    elif cmd == "revoke-all":
        store.q("DELETE FROM tokens")
        mcp_audit.auth("revoke_all")
        print("All tokens revoked. Claude can connect again (sign in from claude.ai).")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
