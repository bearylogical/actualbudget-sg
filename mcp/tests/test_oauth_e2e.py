"""Runs test_oauth_flow.py against a real public-mode server on a free local port, with and without the
source-IP gate."""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

import oauth
from conftest import MCP_DIR

PW = "correct horse battery"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def pw_hash():
    return oauth.hash_password(PW)


@pytest.mark.parametrize("gate", [False, True], ids=["open", "gated"])
def test_oauth_flow(tmp_path, pw_hash, gate):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env.update(MCP_PUBLIC_URL=base, MCP_OWNER_PASSWORD_HASH=pw_hash, DATA_DIR=str(tmp_path),
               BUDGET_APP_URL="http://127.0.0.1:9")
    if gate:
        env["MCP_ALLOWED_CIDRS"] = "160.79.104.0/21"
    proc = subprocess.Popen([sys.executable, "server.py", "--http", "--port", str(port)], cwd=MCP_DIR, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/healthz", trust_env=False, timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if proc.poll() is not None:
                pytest.fail(f"server exited:\n{proc.stdout.read()}")
            time.sleep(0.2)
        else:
            pytest.fail("server did not come up")
        args = [sys.executable, "test_oauth_flow.py", base] + (["--gate"] if gate else [])
        r = subprocess.run(args, cwd=MCP_DIR, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and "ALL PASSED" in r.stdout, r.stdout + r.stderr
    finally:
        proc.terminate()
        proc.wait(timeout=10)
