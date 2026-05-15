"""CLI integration tests — run pell as a script."""
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "compiler" / "examples"


def _run_pell(*args):
    """Run the ./pell wrapper from the repo root."""
    return subprocess.run(
        [str(REPO / "pell"), *args],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_build_single_file_to_stdout():
    result = _run_pell("build", str(EXAMPLES / "01_hello.pell"))
    assert result.returncode == 0, result.stderr
    assert "CREATE OR REPLACE PACKAGE hello" in result.stdout


def test_build_directory_to_dir():
    out_dir = REPO / "compiler" / "expected"
    result = _run_pell("build", str(EXAMPLES), "-d", str(out_dir))
    assert result.returncode == 0, result.stderr
    assert (out_dir / "01_hello.sql").exists()


def test_runtime_aggregation():
    result = _run_pell("runtime", str(EXAMPLES))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "CREATE OR REPLACE PACKAGE pell_runtime" in out
    assert "hr_employees_notfound EXCEPTION" in out
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
