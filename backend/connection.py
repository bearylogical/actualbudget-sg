"""
The Actual connection the scheduler uses, saved from the web UI.

When "Load budget" succeeds in the UI, the backend writes the server URL, password,
budget sync id (and encryption password, if any) to DATA_DIR/scheduler-config.json.
That file lives on the volume shared with the scheduler, whose get_cfg() reads it
before env vars — so the scheduler needs no ACTUAL_* entries in docker-compose.yml.
Env vars still work as a fallback for a headless setup. Other keys already in the
file (e.g. ACCOUNT_ROUTES) are left alone.

Disconnecting in the UI (/actual/reset) clears the saved connection, which stops
scheduled imports until a budget is loaded again.

The password is stored in plain text on the Docker volume (file mode 0600).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_FILE = Path(os.getenv("CONFIG_FILE", str(Path(os.getenv("DATA_DIR", "/data")) / "scheduler-config.json")))

# request body field → config key the scheduler reads
FIELDS = {
    "serverURL": "ACTUAL_SERVER_URL",
    "password": "ACTUAL_PASSWORD",
    "budgetId": "ACTUAL_BUDGET_ID",
    "encryptionPassword": "ACTUAL_ENCRYPTION_PASSWORD",
}


def load() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save(body: dict) -> dict:
    """Merge a successful /budgets/load body into the config file."""
    cfg = load()
    for field, key in FIELDS.items():
        if body.get(field):
            cfg[key] = body[field]
        elif field == "encryptionPassword":
            cfg.pop(key, None)          # budget loaded without one → don't keep a stale one
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_FILE)
    return cfg


def clear() -> dict:
    """Forget the saved connection (keeps unrelated keys such as ACCOUNT_ROUTES)."""
    cfg = load()
    if not any(k in cfg for k in FIELDS.values()):
        return cfg
    for key in FIELDS.values():
        cfg.pop(key, None)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_FILE)
    return cfg


def load_body() -> dict | None:
    """The saved connection as a bridge /budgets/load body, or None if incomplete."""
    cfg = load()
    body = {field: cfg[key] for field, key in FIELDS.items() if cfg.get(key)}
    return body if all(body.get(f) for f in ("serverURL", "password", "budgetId")) else None


def status() -> dict:
    """Non-secret view: what's saved, never the passwords."""
    cfg = load()
    return {
        "saved": all(cfg.get(k) for k in ("ACTUAL_SERVER_URL", "ACTUAL_PASSWORD", "ACTUAL_BUDGET_ID")),
        "server_url": cfg.get("ACTUAL_SERVER_URL"),
        "budget_id": cfg.get("ACTUAL_BUDGET_ID"),
        "encrypted": bool(cfg.get("ACTUAL_ENCRYPTION_PASSWORD")),
        "account_routes": cfg.get("ACCOUNT_ROUTES"),
    }
