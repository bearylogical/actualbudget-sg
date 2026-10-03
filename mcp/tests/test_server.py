"""server.py configures itself at import time from env vars, so each mode runs in a fresh interpreter."""
import json
import os
import subprocess
import sys

import pytest

import oauth
from conftest import MCP_DIR

WRITE_TOOLS = {"review_scan", "category_scan", "review_ask_ai", "review_decide", "reconcile_account",
               "ibkr_to_ghostfolio_import", "ghostfolio_clear_cash",
               "ghostfolio_update_activity", "ghostfolio_delete_activity"}
# The exact set a read-only / public server exposes. Adding a tool here is a conscious decision:
# anything listed is reachable from claude.ai once signed in.
READ_TOOLS = {"ghostfolio_list_activities", "money_summary", "list_accounts", "review_pending", "list_categories", "review_memory",
              "ibkr_to_ghostfolio_preview", "weekly_snapshot", "get_transactions", "monthly_trends", "health"}

LIST_TOOLS = "import asyncio, json, server; print(json.dumps(sorted(t.name for t in asyncio.run(server.mcp.list_tools()))))"


def run_server(code: str, tmp_path, **env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    base.update(DATA_DIR=str(tmp_path), BUDGET_APP_URL="http://127.0.0.1:9", **env)
    return subprocess.run([sys.executable, "-c", code], cwd=MCP_DIR, env=base, capture_output=True, text=True,
                          timeout=60)


def tools(tmp_path, **env) -> set[str]:
    r = run_server(LIST_TOOLS, tmp_path, **env)
    assert r.returncode == 0, r.stderr
    return set(json.loads(r.stdout.strip().splitlines()[-1]))


@pytest.fixture(scope="module")
def pw_hash():
    return oauth.hash_password("pw")


def test_private_mode_exposes_everything(tmp_path):
    assert tools(tmp_path) == READ_TOOLS | WRITE_TOOLS


def test_read_only_mode_hides_write_tools(tmp_path):
    assert tools(tmp_path, MCP_READ_ONLY="true") == READ_TOOLS


def test_public_mode_is_always_read_only(tmp_path, pw_hash):
    got = tools(tmp_path, MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD_HASH=pw_hash,
                MCP_READ_ONLY="false")
    assert got == READ_TOOLS


def test_public_mode_refuses_without_password_hash(tmp_path):
    r = run_server("import server", tmp_path, MCP_PUBLIC_URL="https://mcp.example.com")
    assert r.returncode != 0 and "MCP_OWNER_PASSWORD_HASH" in r.stderr


@pytest.mark.parametrize("url", ["", "http://mcp.example.com"])
def test_require_public_refuses_private_fallback(tmp_path, url):
    r = run_server("import server", tmp_path, MCP_REQUIRE_PUBLIC="true", MCP_PUBLIC_URL=url)
    assert r.returncode != 0 and "MCP_REQUIRE_PUBLIC" in r.stderr


def test_req_maps_http_errors(monkeypatch):
    import httpx
    import server

    def fake(status, body):
        return lambda *a, **k: httpx.Response(status, **body)

    monkeypatch.setattr(httpx, "request", fake(200, {"json": {"ok": 1}}))
    assert server._req("GET", "/x") == {"ok": 1}
    monkeypatch.setattr(httpx, "request", fake(404, {"json": {"detail": "nope"}}))
    assert server._req("GET", "/x") == {"error": "nope"}
    monkeypatch.setattr(httpx, "request", fake(502, {"text": "<html>bad gateway</html>"}))
    assert server._req("GET", "/x") == {"error": "<html>bad gateway</html>"}
