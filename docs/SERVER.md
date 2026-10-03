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
* **Not exposed:** the backend, bridge, UI and the write-enabled `mcp` stay off the internet.

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

Check `@actual-app/api` in `actual-bridge/package.json` matches your Actual server's version
(`https://budget.local.mangk.uk/info`) and pin it if it doesn't.

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
docker compose up -d --build
docker compose ps                           # all healthy
docker compose exec backend python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').read()[:300])"
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
3. Add one Public Hostname: `budget-mcp.mangk.uk` → `HTTP` → `budget-mcp-public:8765`. Leave everything else on this tunnel unrouted.
4. Add the WAF rule below.

This is a separate tunnel from the Home Assistant one, so revoking it can't affect anything else.

**WAF rule** (Security → WAF → Custom rules, action **Block**). Only Anthropic may call the MCP
and token endpoints. Your browser only needs `/authorize` and `/login`.

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
The URL becomes `https://<vm>.<tailnet>.ts.net`. Funnel can't do the IP rule above, so the
password and lockout are the only gate.

```sh
docker compose --profile connector up -d mcp-public      # skip cloudflared
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8766
# MCP_PUBLIC_URL=https://<vm>.<tailnet>.ts.net  (needs the funnel nodeAttr in your tailnet policy)
```

**Smoke test from anywhere.**

```sh
curl -s https://budget-mcp.mangk.uk/.well-known/oauth-protected-resource/mcp   # JSON, resource …/mcp
curl -si -X POST https://budget-mcp.mangk.uk/mcp | head -1                       # 401 (or 403 from the WAF)
```

## 5. Add it to Claude

1. In claude.ai, go to Settings → Connectors → **Add custom connector**.
2. Name it `Budget app`. The URL is `https://budget-mcp.mangk.uk/mcp` (exactly, with `/mcp`).
3. Leave the OAuth client ID and secret empty, because Claude registers itself.
4. Click **Connect**. Your sign-in page opens ("Allow Claude to read your budget?"). Enter the owner password and click **Allow**.
5. The tools appear: `weekly_snapshot`, `money_summary`, `list_accounts`, `review_pending`, `list_categories`, `review_memory`, `ibkr_to_ghostfolio_preview`, `health`.

Then open the **Weekly Money & Portfolio Report** scheduled task, attach the connector and turn
off **Require this computer**.

## Operating it

| Task | Command |
|---|---|
| Who's connected | `docker compose exec mcp-public python oauth.py list` |
| Sign Claude out everywhere | `docker compose exec mcp-public python oauth.py revoke-all` (Claude asks you to sign in again) |
| Change the password | new hash → `.env` → `docker compose --profile connector up -d mcp-public`. Existing tokens stay valid until revoked, so revoke too |
| Kill switch | `docker compose stop cloudflared` (or delete the tunnel) |
| Locked out after wrong passwords | wait 15 minutes, or `docker compose exec mcp-public python oauth.py unlock` |

Your own Claude Desktop can keep using the write-enabled `mcp` over the tailnet:
`MCP_PRIVATE_BIND=<vm tailscale IP>`, `MCP_PRIVATE_HOSTS=<vm>.<tailnet>.ts.net:*,<vm tailscale IP>:*`,
then `docker compose --profile mcp up -d mcp` and point an HTTP MCP client at
`http://<vm>.<tailnet>.ts.net:8765/mcp`.

Developing the auth: `mcp/test_oauth_flow.py` walks the whole claude.ai flow (DCR, PKCE,
sign-in, token, refresh rotation and replay, lockout) against a local server.
