import httpx

import bridge_client
import connection


class FakeBridge:
    """Bridge that forgets its budget until /budgets/load is called."""

    def __init__(self):
        self.loaded, self.loads = False, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={"budgetLoaded": self.loaded})
        if req.url.path == "/budgets/load":
            self.loads.append(req.content)
            self.loaded = True
            return httpx.Response(200, json={"ok": True})
        if not self.loaded:
            return httpx.Response(400, json={"error": "No budget loaded"})
        return httpx.Response(200, json={"categories": []})


def _patch(monkeypatch, bridge):
    transport = httpx.MockTransport(bridge.handler)
    real = httpx.Client
    client = real(transport=transport)
    monkeypatch.setattr(bridge_client.httpx, "request", lambda m, u, **kw: client.request(m, u, **kw))
    monkeypatch.setattr(bridge_client.httpx, "get", lambda u, **kw: client.get(u, **kw))
    monkeypatch.setattr(bridge_client.httpx, "post", lambda u, **kw: client.post(u, **kw))


def test_call_reloads_saved_budget_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "CONFIG_FILE", tmp_path / "cfg.json")
    connection.save({"serverURL": "http://actual:5006", "password": "pw", "budgetId": "sync-1"})
    bridge = FakeBridge()
    _patch(monkeypatch, bridge)
    assert bridge_client.call("GET", "/context") == {"categories": []}
    assert len(bridge.loads) == 1 and b"sync-1" in bridge.loads[0]


def test_call_without_saved_connection_still_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "CONFIG_FILE", tmp_path / "cfg.json")
    bridge = FakeBridge()
    _patch(monkeypatch, bridge)
    try:
        bridge_client.call("GET", "/context")
        assert False, "expected BridgeError"
    except bridge_client.BridgeError as e:
        assert "No budget loaded" in str(e)
    assert bridge.loads == []
