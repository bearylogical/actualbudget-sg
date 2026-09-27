"""
Scheduled statement importer.
Watches WATCH_DIR for new .xls/.xlsx/.csv files and imports them into Actual Budget,
running the same categorisation pipeline as the web UI (Actual rules → seed → LLM).

Account routing — each bank's file must land in its own Actual account:
  ACCOUNT_ROUTES='{"uob": "UOB One Card", "posb": "POSB Savings"}'
Keys are matched (case-insensitive substring) against the detected bank
("UOB", "DBS/POSB", "OCBC") and then the file name. Values may be an Actual
account name or id. ACTUAL_ACCOUNT_ID is the fallback when nothing matches.
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path

import requests

from actual_rules import ActualContext
from parsers import parse_bytes, SUPPORTED_EXTENSIONS
from pipeline import enrich

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scheduler")

WATCH_DIR   = Path(os.getenv("WATCH_DIR", "/watch"))
DONE_DIR    = Path(os.getenv("DONE_DIR", "/watch/done"))
ERROR_DIR   = Path(os.getenv("ERROR_DIR", "/watch/error"))
POLL_SECS   = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
BRIDGE_URL  = os.getenv("ACTUAL_BRIDGE_URL", "http://actual-bridge:3001")
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/data/scheduler-config.json"))
HEARTBEAT   = Path(os.getenv("DATA_DIR", "/data")) / "scheduler-heartbeat.json"

# last-run state, published in the heartbeat for the backend's /health
STATE = {"last_file": None, "last_result": None, "last_error": None, "last_run": None}


def write_heartbeat(pending: int):
    """Atomically record that the loop is alive (read by backend /health and healthcheck.py)."""
    data = {
        "ts": time.time(), "pid": os.getpid(), "poll_secs": POLL_SECS, "watch_dir": str(WATCH_DIR),
        "pending_files": pending,
        "config_ok": all(get_cfg(k) for k in ("ACTUAL_SERVER_URL", "ACTUAL_PASSWORD", "ACTUAL_BUDGET_ID")),
        **STATE,
    }
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(HEARTBEAT)
    except Exception as e:
        log.warning(f"could not write heartbeat: {e}")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def get_cfg(key: str, default: str = "") -> str:
    return load_config().get(key, os.getenv(key, default))


def ensure_dirs():
    for d in [WATCH_DIR, DONE_DIR, ERROR_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def ensure_budget_loaded() -> bool:
    try:
        if not requests.get(f"{BRIDGE_URL}/health", timeout=5).ok:
            return False
    except Exception:
        return False

    server_url = get_cfg("ACTUAL_SERVER_URL")
    password   = get_cfg("ACTUAL_PASSWORD")
    budget_id  = get_cfg("ACTUAL_BUDGET_ID")
    enc_pass   = get_cfg("ACTUAL_ENCRYPTION_PASSWORD")

    if not all([server_url, password, budget_id]):
        log.warning("Missing Actual connection config — skipping import")
        return False

    body = {"serverURL": server_url, "password": password, "budgetId": budget_id}
    if enc_pass:
        body["encryptionPassword"] = enc_pass

    try:
        return requests.post(f"{BRIDGE_URL}/budgets/load", json=body, timeout=60).ok
    except Exception as e:
        log.error(f"Failed to load budget: {e}")
        return False


def fetch_context() -> tuple[ActualContext, dict]:
    r = requests.get(f"{BRIDGE_URL}/context", timeout=60)
    r.raise_for_status()
    data = r.json()
    return ActualContext.from_bridge(data), data


def resolve_account(bank: str, filename: str, accounts: list[dict]) -> str:
    routes_raw = get_cfg("ACCOUNT_ROUTES", "")
    routes = routes_raw if isinstance(routes_raw, dict) else (json.loads(routes_raw) if routes_raw else {})
    target = None
    for haystack in (bank.lower(), filename.lower()):
        for key, value in routes.items():
            if key.lower() in haystack:
                target = value
                break
        if target:
            break
    target = target or get_cfg("ACTUAL_ACCOUNT_ID")
    if not target:
        raise ValueError(f"No account for bank={bank} file={filename}: set ACCOUNT_ROUTES or ACTUAL_ACCOUNT_ID")
    for a in accounts:
        if a.get("id") == target or a.get("name", "").strip().lower() == str(target).strip().lower():
            if a.get("closed"):
                raise ValueError(f"Account '{a['name']}' is closed")
            return a["id"]
    raise ValueError(f"Account '{target}' not found in Actual")


def import_transactions(account_id: str, transactions: list[dict]) -> dict:
    rows = []
    for t in transactions:
        legacy = list(t.get("legacy_ids") or [])
        # id format used by older scheduler versions
        legacy.append(f"auto-{t['date']}-{t['description']}-{t['amount']}".replace(" ", "-"))
        rows.append({**t, "legacy_ids": legacy})
    r = requests.post(
        f"{BRIDGE_URL}/import",
        json={"accountId": account_id, "transactions": rows},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def already_processed(path: Path) -> bool:
    """Check if a same-named file already exists in done/ or error/."""
    return (DONE_DIR / path.name).exists() or (ERROR_DIR / path.name).exists()


def process_file(path: Path):
    log.info(f"Processing: {path.name}")
    STATE.update(last_file=path.name, last_run=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    try:
        transactions, bank = parse_bytes(path.read_bytes(), path.name)
        log.info(f"  Parsed {len(transactions)} transactions (bank: {bank})")

        if not ensure_budget_loaded():
            raise RuntimeError("Could not load Actual budget — check connection config")

        ctx, raw = fetch_context()
        account_id = resolve_account(bank, path.name, raw.get("accounts", []))
        use_llm = get_cfg("SCHEDULER_USE_LLM", "true").lower() == "true"
        enriched = enrich(transactions, ctx, account_id=account_id, use_llm=use_llm)
        s = enriched["stats"]
        log.info(f"  Categorised: actual={s['actual']} seed={s['seed']} llm={s['llm']} "
                 f"transfer={s['transfer']} review={s['review']} unmapped={s['unmapped']}")

        result = import_transactions(account_id, enriched["transactions"])
        log.info(f"  Imported: +{result.get('added', 0)} added, ~{result.get('updated', 0)} updated, "
                 f"{result.get('skipped', 0)} skipped")
        if result.get("errors"):
            log.warning(f"  Import errors: {result['errors']}")

        # Timestamp the filename to allow re-importing the same statement name later
        dest = DONE_DIR / f"{path.stem}_{int(time.time())}{path.suffix}"
        shutil.move(str(path), str(dest))
        log.info(f"  Moved to done: {dest.name}")
        STATE.update(last_result="ok", last_error=None,
                     last_counts={k: result.get(k, 0) for k in ("added", "updated", "skipped")})

    except Exception as e:
        log.error(f"  Error: {e}")
        STATE.update(last_result="error", last_error=str(e)[:300])
        dest = ERROR_DIR / path.name
        try:
            shutil.move(str(path), str(dest))
        except Exception:
            pass


def main():
    ensure_dirs()
    log.info(f"Scheduler watching {WATCH_DIR} every {POLL_SECS}s")

    while True:
        todo = [p for p in sorted(WATCH_DIR.iterdir())
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS and not already_processed(p)]
        write_heartbeat(len(todo))
        for path in todo:
            time.sleep(2)  # wait for file write to complete
            if path.exists():
                process_file(path)
                write_heartbeat(len(todo))

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
