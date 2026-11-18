"""
Scheduled statement importer.
Watches WATCH_DIR for new .xls/.xlsx/.csv files and imports them into Actual Budget,
running the same categorisation pipeline as the web UI (Actual rules → seed → LLM).

Account routing — each bank's file must land in its own Actual account:
  ACCOUNT_ROUTES='{"uob": "UOB One Card", "posb": "POSB Savings"}'
Keys are matched (case-insensitive substring) against the detected bank
("UOB", "DBS/POSB", "OCBC") and then the file name. Values may be an Actual
account name or id. ACTUAL_ACCOUNT_ID is the fallback when nothing matches.

Each file is a Watch-folder import: the same Statement import module as the web UI
(statement_import.py), committed with no decisions.
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path

from parsers import SUPPORTED_EXTENSIONS
import connection
import review
import statement_import as si

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scheduler")

WATCH_DIR   = Path(os.getenv("WATCH_DIR", "/watch"))
DONE_DIR    = Path(os.getenv("DONE_DIR", "/watch/done"))
ERROR_DIR   = Path(os.getenv("ERROR_DIR", "/watch/error"))
POLL_SECS   = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
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


def get_cfg(key: str, default: str = ""):
    """The saved connection file (written by the web UI, see connection.py) wins over env vars."""
    return connection.load().get(key, os.getenv(key, default))


def account_routes() -> dict:
    raw = get_cfg("ACCOUNT_ROUTES", "")
    return raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})


def ensure_dirs():
    for d in [WATCH_DIR, DONE_DIR, ERROR_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def already_processed(path: Path) -> bool:
    """Check if a same-named file already exists in done/ or error/."""
    return (DONE_DIR / path.name).exists() or (ERROR_DIR / path.name).exists()


def process_file(path: Path):
    """A Watch-folder import (statement_import.import_unattended): done/ on success, error/ otherwise."""
    log.info(f"Processing: {path.name}")
    STATE.update(last_file=path.name, last_run=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    try:
        res = si.import_unattended("scheduler", path.name, path.read_bytes(), routes=account_routes(),
                                   fallback=get_cfg("ACTUAL_ACCOUNT_ID") or None,
                                   use_llm=str(get_cfg("SCHEDULER_USE_LLM", "true")).lower() == "true")
        r = res["import"]
        log.info(f"  {res['statement']['label']} → {res['account_name']} ({res['why']})")
        log.info(f"  Imported: +{r.get('added', 0)} added, ~{r.get('updated', 0)} updated, "
                 f"{r.get('skipped', 0)} skipped")
        for name, st in res["steps"].items():
            if not st["ok"]:
                log.warning(f"  {name} skipped: {st['error']}")
        held = res["steps"].get("hold_duplicates", {}).get("held")
        if held:
            log.warning(f"  {held} possible duplicate(s) held for review")
        rec = (res.get("reconcile") or {}).get("report")
        if rec:
            log.info("  Reconciled: Actual matches the bank balance" if rec["reconciled"] else
                     f"  Balance gap {rec['gap'] / 100 if rec['gap'] is not None else '?'} — fixes queued for review")
        if r.get("errors"):
            log.warning(f"  Import errors: {r['errors']}")
        STATE.update(review_pending=review.counts().get("pending", 0))

        # Timestamp the filename to allow re-importing the same statement name later
        dest = DONE_DIR / f"{path.stem}_{int(time.time())}{path.suffix}"
        shutil.move(str(path), str(dest))
        log.info(f"  Moved to done: {dest.name}")
        STATE.update(last_result="ok", last_error=None,
                     last_counts={k: r.get(k, 0) for k in ("added", "updated", "skipped")})

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
