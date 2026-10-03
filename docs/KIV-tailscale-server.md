# KIV — move the stack to the Tailscale server so Claude can reach it

**Status:** option 2 chosen and built (Oct 2026). Runbook: [SERVER.md](SERVER.md).
Read-only `mcp-public` behind OAuth, exposed through a dedicated Cloudflare Tunnel
(Tailscale Funnel as the alternative).

## Goal
Run the whole stack on the homelab server (on the tailnet) and let Claude reach the
budget-app MCP without the Mac being awake.

## Options (pick when picking this up)
1. **Tailnet-only, via a Claude Desktop/Cowork machine on the tailnet** — simplest, nothing public.
   Claude Desktop's local MCP config points `BUDGET_APP_URL` (or an HTTP MCP client) at
   `http://<server>.<tailnet>.ts.net:8765/mcp`. Still needs *a* machine running Claude.
2. **Remote connector on claude.ai** — cloud scheduled tasks work with no Mac. Claude's cloud
   can't join the tailnet, so the MCP endpoint must be public HTTPS:
   Tailscale **Funnel** (`tailscale funnel 8765`) or the existing **Cloudflare Tunnel**.
   Only expose `MCP_READ_ONLY=true` there, and put real auth in front — check what auth
   claude.ai custom connectors support at that time (OAuth) before exposing anything.
3. Hybrid: public read-only connector for reports, write tools stay tailnet-only.

## Checklist
- [ ] `docker compose --profile mcp up -d` on the server (backend healthchecks green)
- [ ] Scheduler + watch folder on the server; statements dropped via SMB/Syncthing
- [ ] Actual server URL reachable from the bridge container (tailnet DNS / MagicDNS)
- [x] MCP auth decided: option 2, single-owner OAuth (DCR + PKCE), public mode is read-only by construction
- [ ] Update the "Weekly Money & Portfolio Report" scheduled task: drop "Require this
      computer" once the connector is attached to it
- [x] Update docs/MCP.md
