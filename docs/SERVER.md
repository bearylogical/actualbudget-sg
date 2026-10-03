# Running on the homelab VM + the claude.ai connector

This setup runs the whole stack on a Docker VM. A **read-only** budget-app MCP is exposed as a
**claude.ai custom connector**, so scheduled Claude tasks (like the weekly report) can reach your
money data without your Mac being awake.

```
claude.ai (cloud) ──HTTPS──▶ Cloudflare ──tunnel──▶ cloudflared ─▶ mcp-public ─▶ backend ─▶ actual-bridge ─▶ Actual server
     160.79.104.0/21          (WAF rule)            (VM, outbound only)  read-only    (127.0.0.1)
                                                                          + OAuth
you, on the tailnet ─────────────────────────────────────────────▶ frontend :3000 / mcp :8765 (write tools, private)
```

Here is what reaches the internet:

* **Exposed:** only `mcp-public`, and only through a tunnel. It has no write tools at all. `review_decide`, `review_scan`, `reconcile_account`, `ibkr_to_ghostfolio_import` and the rest aren't registered in public mode.
* **Sign-in:** OAuth sign-in with your owner password, as claude.ai requires.
* **Callbacks:** only Claude's callback (`https://claude.ai/api/mcp/auth_callback`) can register as a client.
* **Tokens:** access tokens last 1 hour. Refresh tokens last 30 days and rotate on every use. A replayed refresh token revokes the whole grant.
* **Brute force:** sign-in locks for 15 minutes after 5 wrong passwords.
* **One connection:** once Claude is connected, no other client can register or sign in, even with the right password. Someone who adds your URL as a connector in *their* claude.ai account and sends you the sign-in link gets refused. To reconnect, run `oauth.py revoke-all` first. Turn it off with `MCP_SINGLE_GRANT=false`.
* **Audit trail:** every tool call (both MCP servers) and every sign-in, wrong password, lockout, token replay and blocked IP is recorded, and summarised to Telegram (see [Audit trail](#audit-trail)).
* **Source IP:** only Anthropic's range (`160.79.104.0/21`) can reach the MCP and token endpoints. Your browser only gets `/authorize` and `/login`. `mcp-public` checks this itself (`MCP_ALLOWED_CIDRS`), and a Cloudflare WAF rule can add a second check at the edge.
* **Not exposed:** the backend, bridge, UI and the write-enabled `mcp` stay off the internet.
  The UI has no login of its own, and anyone who can reach port 3000 can read and change everything through `/api`. Traefik's authentication only covers requests that go through Traefik. If Traefik runs in Docker on the same VM, put it on the compose network and set `FRONTEND_PORT=127.0.0.1:3000`. Otherwise, firewall port 3000 to Traefik's address.

## 1. On the VM

```sh
git clone https://github.com/bearylogical/actualbudget-sg.git budget-app && cd budget-app
cp .env.example .env 2>/dev/null || touch .env
```

`.env`:

```sh
GEMINI_API_KEY=…
GHOSTFOLIO_URL=https://ghostfolio.local.bearylogical.net
GHOSTFOLIO_TOKEN=…
WATCH_DIR_HOST=/srv/budget-statements      # folder you drop statements into (SMB / Syncthing)
BACKEND_PORT=127.0.0.1:8010                # only if 8000 is taken (Portainer uses it); nothing on the VM needs this port
FRONTEND_PORT=3000                         # change if 3000 is taken

# connector (step 4)
MCP_PUBLIC_URL=https://budget-mcp.mangk.uk  # no trailing slash, no /mcp
MCP_OWNER_PASSWORD_HASH=scrypt:…
CLOUDFLARE_TUNNEL_TOKEN=…
```

The bridge's `@actual-app/api` is matched to your Actual server at build time from
`ACTUAL_SERVER_URL` in `.env`, so after upgrading Actual run `make bridge`.
To check the two versions (and rebuild with `--up`):

```sh
make check
```

## 2. Move your data from the Mac

The review queue, what it learned, the account map, category aliases and the saved Actual
connection all live in the `scheduler-data` volume. The budget itself is on the Actual server
and doesn't move.

```sh
# Mac (in the repo)
docker compose stop scheduler
scripts/move-data.sh export                 # → budget-app-data.tgz (contains the saved Actual password)
scp budget-app-data.tgz vm:budget-app/ && rm budget-app-data.tgz

# VM
scripts/move-data.sh import budget-app-data.tgz && rm budget-app-data.tgz
```

## 3. Start the stack

```sh
make up      # version check → build → start, waiting until healthy → load budget → health
```

Open the UI at `http://<vm>:3000`. Put it behind Traefik if you like (`budget-app.local.mangk.uk`).
Check the Actual connection loaded and a statement dropped into `WATCH_DIR_HOST` gets imported.
Then stop the stack on the Mac (`docker compose down`) so only one scheduler runs.

## 4. The connector

**Password hash.** Use a long password that you don't use anywhere else.

```sh
docker compose build mcp-public
docker compose run --rm --no-deps mcp-public python oauth.py hash   # paste into MCP_OWNER_PASSWORD_HASH
```

**Tunnel (recommended: a dedicated Cloudflare Tunnel).**

1. Go to Cloudflare Zero Trust → Networks → Tunnels → Create tunnel (cloudflared) and name it `budget-mcp`.
2. Copy the token into `CLOUDFLARE_TUNNEL_TOKEN`.
3. Add a route → **Published application** (this used to be called "Public Hostname"): subdomain `budget-mcp`, domain `mangk.uk`, path empty, service `HTTP` → `budget-mcp-public:8765`. Don't add any other routes to this tunnel.
4. Optional: add the WAF rule below.

This is a separate tunnel from the Home Assistant one, so revoking it can't affect anything else.

**IP allowlist.** `mcp-public` already refuses everything except `/authorize`, `/login` and
`/healthz` unless the request comes from Anthropic's `160.79.104.0/21`. It reads the client
address from Cloudflare's `CF-Connecting-IP` header. This works on the Free plan and needs no
setup. To change the range, set `MCP_ALLOWED_CIDRS` in `.env` (comma-separated).

**WAF rule (optional, defence in depth).** This blocks the same traffic at Cloudflare's edge, before
it reaches the tunnel. Go to your domain → Security → Security rules → Custom rules, with action **Block**:

```
(http.host eq "budget-mcp.mangk.uk"
 and not ip.src in {160.79.104.0/21}
 and not starts_with(http.request.uri.path, "/authorize")
 and not starts_with(http.request.uri.path, "/login"))
```

```sh
docker compose --profile connector up -d
docker compose logs -f mcp-public cloudflared
```

**Alternative: Tailscale Funnel.** Use this instead of Cloudflare if you'd rather not involve it.
The URL becomes `https://<vm>.<tailnet>.ts.net`. Funnel doesn't send `CF-Connecting-IP`, so turn
the IP allowlist off (`MCP_ALLOWED_CIDRS=` in `.env`). Then the password and lockout are the only gate.

```sh
MCP_ALLOWED_CIDRS= docker compose --profile connector up -d mcp-public      # skip cloudflared
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8766
# MCP_PUBLIC_URL=https://<vm>.<tailnet>.ts.net  (needs the funnel nodeAttr in your tailnet policy)
```

**Smoke test from anywhere.**

```sh
curl -s https://budget-mcp.mangk.uk/healthz                                       # ok
docker compose logs mcp-public | grep "Source-IP gate on"                         # allowlist active
curl -si -X POST https://budget-mcp.mangk.uk/mcp | head -1                       # 403 (IP allowlist) from your machine
```

## 5. Add it to Claude

1. In claude.ai, go to Settings → Connectors → **Add custom connector**.
2. Name it `Budget app`. The URL is `https://budget-mcp.mangk.uk/mcp` (exactly, with `/mcp`).
3. Leave the OAuth client ID and secret empty, because Claude registers itself.
4. Click **Connect**. Your sign-in page opens ("Allow Claude to read your budget?"). Enter the owner password and click **Allow**.
5. The tools appear: `weekly_snapshot`, `money_summary`, `get_transactions`, `monthly_trends`, `list_accounts`, `review_pending`, `list_categories`, `review_memory`, `ibkr_to_ghostfolio_preview`, `health`.

Then open the **Weekly Money & Portfolio Report** scheduled task, attach the connector and turn
off **Require this computer**.

## Audit trail

Both MCP servers send every tool call (tool name, summarised arguments, the Claude client, result, duration) and every security event to the backend (`/audit/mcp`, stored in `audit.db` in the `scheduler-data` volume, kept 180 days). Each event is also printed to the container log as an `AUDIT {...}` line. The UI can't reach these endpoints, because nginx refuses `/api/audit/`.

Every `TELEGRAM_DIGEST_HOURS` (default 24) the backend sends one message with:
* calls per tool and per client, and errors;
* every write call (tool, time, arguments, success);
* sign-ins, registrations and revocations. Security events are flagged ⚠️: wrong passwords, lockouts, refused registrations or sign-ins, refresh-token replays and blocked IPs.

Nothing is sent for a quiet period.

Setup:
1. In Telegram, ask @BotFather for `/newbot` and copy the token.
2. Send your bot any message, then open `https://api.telegram.org/bot<token>/getUpdates` and copy `chat.id`.
3. Add `TELEGRAM_BOT_TOKEN=…` and `TELEGRAM_CHAT_ID=…` to `.env`, then run `docker compose up -d backend`.
4. Test: `docker compose exec backend python -c "import audit; print(audit.digest(force=True))"`.

| Task | Command |
|---|---|
| Last 24 h, as JSON | `docker compose exec backend python -c "import audit,time,json; print(json.dumps(audit.summarize(time.time()-86400), indent=1))"` |
| Preview the digest text | `docker compose exec backend python -c "import audit; print(audit.digest(force=True, send=False)['text'])"` |
| Raw events | `docker compose logs mcp-public mcp \| grep AUDIT` |

## Operating it

| Task | Command |
|---|---|
| Who's connected | `docker compose exec mcp-public python oauth.py list` |
| Sign Claude out everywhere | `docker compose exec mcp-public python oauth.py revoke-all` (Claude asks you to sign in again) |
| Reconnect Claude (e.g. "already connected" on Connect) | `revoke-all` as above, then Connect in claude.ai |
| Change the password | new hash → `.env` → `docker compose --profile connector up -d mcp-public`. Existing tokens stay valid until revoked, so revoke too |
| Kill switch | `docker compose stop cloudflared` (or delete the tunnel) |
| Locked out after wrong passwords | wait 15 minutes, or `docker compose exec mcp-public python oauth.py unlock` |

Your own Claude Desktop can keep using the write-enabled `mcp` over the tailnet:
`MCP_PRIVATE_BIND=<vm tailscale IP>`, `MCP_PRIVATE_HOSTS=<vm>.<tailnet>.ts.net:*,<vm tailscale IP>:*`,
then `docker compose --profile mcp up -d mcp` and point an HTTP MCP client at
`http://<vm>.<tailnet>.ts.net:8765/mcp`.

Developing the auth: `mcp/test_oauth_flow.py` walks the whole claude.ai flow (DCR, PKCE,
sign-in, token, refresh rotation and replay, lockout) against a local server.
