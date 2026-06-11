"""SQL-transport debugger protocol test — drives `pell debug-serve`
exactly as the IDE does: JSON commands on stdin, events on stdout,
against a live Oracle (skipped without PELL_DB_URL).

Counterpart of the JDWP OracleJdwpProtocolTest, for the transport that
needs no inbound connection.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

COMPILER = Path(__file__).parent.parent
EXAMPLES = COMPILER / "examples"
DB_URL = os.environ.get("PELL_DB_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="PELL_DB_URL not set — debug-serve protocol test skipped",
)


class Serve:
    def __init__(self, script: Path):
        self.p = subprocess.Popen(
            [sys.executable, "-m", "pell", "debug-serve", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=EXAMPLES,
            env={**os.environ, "PYTHONPATH": str(COMPILER),
                 "PELL_SERVE_DEBUG": "1"},
        )

    def send(self, **cmd):
        self.p.stdin.write(json.dumps(cmd) + "\n")
        self.p.stdin.flush()

    def recv(self, timeout=60):
        import select
        r, _, _ = select.select([self.p.stdout], [], [], timeout)
        if not r:
            self.p.kill()
            raise AssertionError(
                f"timed out waiting for an event; serve stderr:\n{self.p.stderr.read()}")
        line = self.p.stdout.readline()
        assert line, f"serve exited early: {self.p.stderr.read()}"
        return json.loads(line)

    def close(self):
        if self.p.poll() is None:
            self.p.kill()


@pytest.fixture(scope="module", autouse=True)
def _deploy_hello():
    r = subprocess.run(
        [sys.executable, "-m", "pell", "deploy",
         str(EXAMPLES / "01_hello.pell"), "--debug",
         "--out-dir", tempfile.mkdtemp()],
        capture_output=True, text=True, cwd=COMPILER,
        env={**os.environ, "PYTHONPATH": str(COMPILER)},
    )
    assert r.returncode == 0, f"deploy failed:\n{r.stdout}{r.stderr}"


def test_breakpoint_step_locals_terminate(tmp_path):
    script = tmp_path / "stub.pell"
    script.write_text(
        "import hello;\n"
        "for i in 1..=3 {\n"
        "    let x = hello::greet(\"w{i}\");\n"
        "}\n"
        "p(\"done\");\n"
    )
    s = Serve(script)
    try:
        ev = s.recv()
        assert ev["event"] == "ready", ev

        # HELLO body line 5 = the logger.info statement (pinned by srcmap).
        s.send(cmd="set_breakpoint", owner=None, name="HELLO", namespace=2,
               line=5, id="bp1")
        ev = s.recv()
        assert ev["event"] == "bp_set" and ev["status"] == 0, ev

        s.send(cmd="run")
        ev = s.recv()
        assert ev["event"] == "suspended" and ev["reason"] == "breakpoint", ev
        assert ev["name"] == "HELLO" and ev["line"] == 5, ev
        # stack: HELLO on top, the anon block below it.
        assert ev["stack"][0]["name"] == "HELLO", ev["stack"]

        s.send(cmd="locals")
        ev = s.recv()
        assert ev["event"] == "locals", ev
        byname = {v["name"]: v["value"] for v in ev["values"]}
        assert byname.get("P_NAME") == "w1", byname

        s.send(cmd="step_over")
        ev = s.recv()
        assert ev["event"] == "suspended" and ev["line"] == 6, ev

        # 2nd + 3rd iterations hit the breakpoint again; then it ends.
        s.send(cmd="continue")
        ev = s.recv()
        assert ev["event"] == "suspended" and ev["line"] == 5, ev
        s.send(cmd="locals")
        ev = s.recv()
        assert {v["name"]: v["value"] for v in ev["values"]}.get("P_NAME") == "w2", ev

        s.send(cmd="continue")
        ev = s.recv()
        assert ev["event"] == "suspended", ev
        s.send(cmd="continue")
        ev = s.recv(timeout=90)
        assert ev["event"] == "terminated", ev
        assert ev["error"] is None, ev
        assert "done" in (ev["output"] or []), ev
        assert s.p.wait(15) == 0
    finally:
        s.close()


def test_stop_aborts_target(tmp_path):
    script = tmp_path / "stub.pell"
    script.write_text(
        "import hello;\n"
        "for i in 1..=100000 {\n"
        "    let x = hello::greet(\"loop\");\n"
        "}\n"
    )
    s = Serve(script)
    try:
        assert s.recv()["event"] == "ready"
        s.send(cmd="set_breakpoint", owner=None, name="HELLO", namespace=2,
               line=5, id="bp1")
        assert s.recv()["event"] == "bp_set"
        s.send(cmd="run")
        assert s.recv()["event"] == "suspended"
        s.send(cmd="stop")
        ev = s.recv(timeout=60)
        assert ev["event"] == "terminated", ev
        assert s.p.wait(15) == 0
    finally:
        s.close()
