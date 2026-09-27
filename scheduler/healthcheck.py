"""Docker HEALTHCHECK for the scheduler: healthy while the heartbeat is fresh."""
import json
import os
import sys
import time
from pathlib import Path

hb_file = Path(os.getenv("DATA_DIR", "/data")) / "scheduler-heartbeat.json"
try:
    hb = json.loads(hb_file.read_text())
except Exception as e:
    sys.exit(f"no heartbeat: {e}")
limit = max(3 * int(hb.get("poll_secs", 30)), 300)  # allow for long LLM calls mid-import
age = time.time() - float(hb.get("ts", 0))
sys.exit(0 if age <= limit else f"heartbeat {age:.0f}s old (limit {limit}s)")
