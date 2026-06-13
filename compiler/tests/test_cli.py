"""CLI integration tests — run pell as a script."""
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
COMPILER = REPO / "compiler"
EXAMPLES = COMPILER / "examples"

# The ./pell wrapper hardcodes compiler/.venv — outside a full checkout
# (e.g. the docker test image, which only carries compiler/) run the
# module under the current interpreter instead.
_WRAPPER = REPO / "pell"
_WRAPPER_OK = _WRAPPER.exists() and (COMPILER / ".venv" / "bin" / "python").exists()


def _run_pell(*args):
    """Run the pell CLI (the ./pell wrapper when available)."""
    if _WRAPPER_OK:
        cmd = [str(_WRAPPER), *args]
    else:
        cmd = [sys.executable, "-m", "pell", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(COMPILER)},
    )


def test_build_single_file_to_stdout():
    result = _run_pell("build", str(EXAMPLES / "01_hello.pell"))
    assert result.returncode == 0, result.stderr
    assert "CREATE OR REPLACE PACKAGE hello" in result.stdout


def test_build_directory_to_dir():
    out_dir = REPO / "compiler" / "expected"
    # --reproducible omits volatile preamble fields so the snapshots stay
    # byte-stable across runs (and diffs in PRs show only real changes).
    result = _run_pell("build", str(EXAMPLES), "-d", str(out_dir), "--reproducible")
    assert result.returncode == 0, result.stderr
    assert (out_dir / "01_hello.sql").exists()


def test_runtime_aggregation():
    result = _run_pell("runtime", str(EXAMPLES))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "CREATE OR REPLACE PACKAGE pell_runtime" in out
    assert "hr_staffing_notfound EXCEPTION" in out
    assert "billing_charges_overdraft EXCEPTION" in out


def test_tokens_command():
    result = _run_pell("tokens", str(EXAMPLES / "01_hello.pell"))
    assert result.returncode == 0, result.stderr
    assert "KW_MODULE" in result.stdout


def test_parse_command():
    result = _run_pell("parse", str(EXAMPLES / "01_hello.pell"))
    assert result.returncode == 0, result.stderr
    assert "Module" in result.stdout
    assert "FnDef" in result.stdout


# --- pell test: utPLSQL owner resolution + availability pre-flight -------

def test_ut_owner_resolution(monkeypatch):
    import argparse
    from pell import cli
    monkeypatch.delenv("PELL_UT_SCHEMA", raising=False)
    # No flag, no env → unqualified (None).
    assert cli._ut_owner(argparse.Namespace(ut_schema=None)) is None
    # Flag wins.
    assert cli._ut_owner(argparse.Namespace(ut_schema="UT3")) == "UT3"
    # Env when no flag.
    monkeypatch.setenv("PELL_UT_SCHEMA", "MYUT")
    assert cli._ut_owner(argparse.Namespace(ut_schema=None)) == "MYUT"
    # Flag still wins over env; blanks normalize to None.
    assert cli._ut_owner(argparse.Namespace(ut_schema="UT3")) == "UT3"
    assert cli._ut_owner(argparse.Namespace(ut_schema="   ")) == "MYUT"


def test_utplsql_available_query(monkeypatch):
    from pell import cli

    class FakeConn:
        def __init__(self, count):
            self.count = count
            self.last = None
        def run_query(self, sql, binds=None):
            self.last = (sql, binds)
            return [{"n": self.count}]

    # Present.
    assert cli._utplsql_available(FakeConn(1), None) is True
    # Absent.
    assert cli._utplsql_available(FakeConn(0), None) is False
    # Owner-qualified path binds the owner.
    c = FakeConn(1)
    cli._utplsql_available(c, "UT3")
    assert "owner = upper(:o)" in c.last[0]
    assert c.last[1] == {"o": "UT3"}


def test_explain_debug_run_error_maps_internal_errors():
    from pell import cli
    # The user's actual ORA-00600 from debugging a FORALL-over-records.
    real = ("DPY-4011: the database or network closed the connection\n"
            "ORA-00600: internal error code, arguments: [15419], "
            "[severe error during PL/SQL execution], [2649], [], [], []\n"
            "ORA-06544: PL/SQL: internal error, arguments: [2649], []\n"
            "ORA-06553: PLS-707: unsupported construct or internal error [2649]")
    hint = cli._explain_debug_run_error(real)
    assert hint is not None
    assert "without --debug" in hint and "ClassPrepare" in hint
    # Ordinary errors don't trigger the hint.
    assert cli._explain_debug_run_error("ORA-00942: table or view does not exist") is None
