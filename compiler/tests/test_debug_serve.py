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


def _deploy(example: str):
    r = subprocess.run(
        [sys.executable, "-m", "pell", "deploy",
         str(EXAMPLES / example), "--debug",
         "--out-dir", tempfile.mkdtemp()],
        capture_output=True, text=True, cwd=COMPILER,
        env={**os.environ, "PYTHONPATH": str(COMPILER)},
    )
    assert r.returncode == 0, f"deploy {example} failed:\n{r.stdout}{r.stderr}"


@pytest.fixture(scope="module", autouse=True)
def _deploy_examples():
    _deploy("01_hello.pell")
    _deploy("18_json.pell")


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
        assert byname.get("name") == "w1", byname

        s.send(cmd="step_over")
        ev = s.recv()
        assert ev["event"] == "suspended" and ev["line"] == 6, ev

        # 2nd + 3rd iterations hit the breakpoint again; then it ends.
        s.send(cmd="continue")
        ev = s.recv()
        assert ev["event"] == "suspended" and ev["line"] == 5, ev
        s.send(cmd="locals")
        ev = s.recv()
        assert {v["name"]: v["value"] for v in ev["values"]}.get("name") == "w2", ev

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


def test_json_local_value_via_debug_shadow(tmp_path):
    """Debug builds emit a `<name>__pdbg` scalar shadow for opaque
    locals; the debugger reads it so a JSON local shows its actual
    value (not <OPAQUE>), and the shadow + compiler temporaries stay
    hidden."""
    script = tmp_path / "stub.pell"
    script.write_text(
        "import json_demo;\n"
        "let j = json_demo::build_simple();\n"
        "p(\"{j}\");\n"
    )
    s = Serve(script)
    try:
        assert s.recv()["event"] == "ready"
        # build_simple: break on RETURN l_j (unit line 64) — by then the
        # shadow `l_j__pdbg := ...` (line 62) has run, so it holds the
        # current value.
        s.send(cmd="set_breakpoint", owner=None, name="JSON_DEMO", namespace=2,
               line=64, id="bp1")
        assert s.recv()["event"] == "bp_set"
        s.send(cmd="run")
        ev = s.recv()
        assert ev["event"] == "suspended" and ev["name"] == "JSON_DEMO", ev

        s.send(cmd="locals")
        ev = s.recv()
        assert ev["event"] == "locals", ev
        byname = {v["name"]: v for v in ev["values"]}
        # Compiler temporaries and the shadow itself never surface.
        assert not any("pell_jobj" in n.lower() for n in byname), list(byname)
        assert not any(n.endswith("__PDBG") for n in byname), list(byname)
        # The user's JSON local now shows its ACTUAL value.
        assert "j" in byname, list(byname)
        val = byname["j"]["value"]
        assert val.startswith("{") and "Alice" in val, byname["j"]

        s.send(cmd="continue")
        ev = s.recv(timeout=90)
        assert ev["event"] == "terminated", ev
        assert s.p.wait(15) == 0
    finally:
        s.close()


def test_return_value_shown_before_step_out(tmp_path):
    """Debug builds capture a bare-identifier return into the `pell$ret`
    shadow; the debugger surfaces it as "↩ return" at the RETURN line,
    so you see the value before stepping out."""
    script = tmp_path / "stub.pell"
    script.write_text(
        "import json_demo;\n"
        "let j = json_demo::build_simple();\n"
        "p(\"{j}\");\n"
    )
    s = Serve(script)
    try:
        assert s.recv()["event"] == "ready"
        # build_simple RETURN l_j is unit line 64 (after the l_j__pdbg
        # and pell$ret shadow updates on 62/63).
        s.send(cmd="set_breakpoint", owner=None, name="JSON_DEMO", namespace=2,
               line=64, id="bp1")
        assert s.recv()["event"] == "bp_set"
        s.send(cmd="run")
        assert s.recv()["event"] == "suspended"
        s.send(cmd="locals")
        ev = s.recv()
        byname = {v["name"]: v["value"] for v in ev["values"]}
        assert "↩ return" in byname, list(byname)
        assert "Alice" in byname["↩ return"], byname["↩ return"]
        # The raw shadow name never leaks.
        assert not any("PELL$RET" in n for n in byname), list(byname)
        s.send(cmd="continue")
        assert s.recv(timeout=90)["event"] == "terminated"
        assert s.p.wait(15) == 0
    finally:
        s.close()
