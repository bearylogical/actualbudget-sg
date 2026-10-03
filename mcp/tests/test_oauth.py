"""Unit tests for the public-mode guards in oauth.py."""
import asyncio

import oauth


def test_password_hash_roundtrip():
    h = oauth.hash_password("correct horse battery")
    assert h.startswith("scrypt:")
    assert oauth.check_password("correct horse battery", h)
    assert not oauth.check_password("wrong", h)


def test_check_password_rejects_malformed_hashes():
    assert not oauth.check_password("x", "")
    assert not oauth.check_password("x", "bcrypt:a:b")
    assert not oauth.check_password("x", "scrypt:not-base64!:nope")


def test_redirect_only_claude_callbacks_by_default():
    for cb in oauth.CLAUDE_CALLBACKS:
        assert oauth.redirect_allowed(cb, allow_loopback=False)
    assert not oauth.redirect_allowed("https://evil.example/cb", allow_loopback=False)
    assert not oauth.redirect_allowed("https://claude.ai/api/mcp/auth_callback/x", allow_loopback=False)
    assert not oauth.redirect_allowed("http://127.0.0.1/callback", allow_loopback=False)


def test_redirect_loopback_only_when_enabled():
    assert oauth.redirect_allowed("http://127.0.0.1:6274/callback", allow_loopback=True)
    assert oauth.redirect_allowed("http://localhost/callback", allow_loopback=True)
    assert not oauth.redirect_allowed("https://127.0.0.1/callback", allow_loopback=True)
    assert not oauth.redirect_allowed("http://127.0.0.1/other", allow_loopback=True)
    assert not oauth.redirect_allowed("http://evil.example/callback", allow_loopback=True)


GATE = oauth.SourceIPGate(None, oauth.parse_cidrs("160.79.104.0/21, 2607:6bc0::/48"))


def test_gate_allows_anthropic_ranges():
    assert GATE.allowed("/mcp", {b"cf-connecting-ip": b"160.79.104.10"}, "10.0.0.2")
    assert GATE.allowed("/mcp", {b"cf-connecting-ip": b"2607:6bc0::1"}, None)
    assert GATE.allowed("/mcp", {b"cf-connecting-ip": b"::ffff:160.79.104.10"}, None)
    assert GATE.allowed("/token", {}, "160.79.111.255")


def test_gate_refuses_outsiders():
    assert not GATE.allowed("/mcp", {b"cf-connecting-ip": b"203.0.113.7"}, "160.79.104.10")
    assert not GATE.allowed("/mcp", {}, "203.0.113.7")
    assert not GATE.allowed("/mcp", {}, None)
    assert not GATE.allowed("/register", {b"cf-connecting-ip": b"garbage"}, None)
    # a prefix of an open path is not an open path
    assert not GATE.allowed("/loginx", {}, "203.0.113.7")


def test_gate_open_paths_reachable_by_anyone():
    for p in ("/authorize", "/login", "/healthz", "/login/extra"):
        assert GATE.allowed(p, {}, "203.0.113.7")


def test_gate_asgi_returns_403():
    sent, called = [], []

    async def app(scope, receive, send):
        called.append(scope["path"])

    async def send(msg):
        sent.append(msg)

    gate = oauth.SourceIPGate(app, oauth.parse_cidrs("160.79.104.0/21"))
    asyncio.run(gate({"type": "http", "path": "/mcp", "headers": [], "client": ("203.0.113.7", 1)}, None, send))
    assert sent[0]["status"] == 403 and not called
    asyncio.run(gate({"type": "http", "path": "/mcp", "headers": [], "client": ("160.79.104.1", 1)}, None, send))
    assert called == ["/mcp"]
