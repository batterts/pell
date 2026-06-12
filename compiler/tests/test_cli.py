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
