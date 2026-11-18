"""The watch-folder scheduler: each file is a Watch-folder import, moved to done/ or error/."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scheduler"))

import actual_gateway
import import_log
import statement_import as si
from accounts import StatementInfo
from fake_actual import FakeActual
from parsers import _std

INFO = StatementInfo(bank="DBS/POSB", kind="deposit", account_type="POSB Savings", last4="1234",
                     period_start="2026-09-01", period_end="2026-09-30", balance=1000.0)


@pytest.fixture
def sched(tmp_path, monkeypatch):
    import scheduler as s
    for name in ("WATCH_DIR", "DONE_DIR", "ERROR_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(s, name, d)
    monkeypatch.setattr(s, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(s.connection, "CONFIG_FILE", tmp_path / "scheduler-config.json")
    tx = [_std("2026-09-03", "NETS KOPITIAM", 4.5)]
    monkeypatch.setattr(si, "parse_statement", lambda content, name: ([dict(t) for t in tx], "DBS/POSB", INFO))
    gw = FakeActual(accounts=[{"id": "posb", "name": "POSB Savings"}])
    monkeypatch.setattr(actual_gateway, "_default", gw)
    return s, gw, monkeypatch


def test_success_is_logged_and_file_moved(sched):
    s, gw, mp = sched
    mp.setenv("ACCOUNT_ROUTES", '{"posb": "POSB Savings"}')
    f = s.WATCH_DIR / "ACC_TXN.csv"
    f.write_bytes(b"posb-bytes")
    s.process_file(f)

    r = import_log.listing()[0]
    assert (r["source"], r["status"], r["filename"]) == ("scheduler", "imported", "ACC_TXN.csv")
    d = import_log.get(r["id"])
    assert d["account_name"] == "POSB Savings" and d["account_why"] == "ACCOUNT_ROUTES['posb']"
    assert len(d["added_ids"]) == 1 and d["reconcile"] is not None and d["has_file"]
    assert len(gw.rows) == 1 and list(s.DONE_DIR.iterdir())
    assert s.STATE["last_result"] == "ok" and s.STATE["last_counts"]["added"] == 1


def test_failure_records_stage_and_moves_to_error(sched):
    s, gw, mp = sched
    gw.accounts = [{"id": "w", "name": "Wallet"}]           # nothing remembered, routed or recognisable
    f = s.WATCH_DIR / "mystery.csv"
    f.write_bytes(b"?")
    s.process_file(f)
    r = import_log.listing()[0]
    assert (r["status"], r["stage"]) == ("failed", "account") and "which account" in r["error"]
    assert r["label"].startswith("DBS/POSB")             # parsed details survive the failure
    assert (s.ERROR_DIR / "mystery.csv").exists() and gw.rows == []
    assert s.STATE["last_result"] == "error"
