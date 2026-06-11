"""Live deploy verification — the test that actually asks Oracle.

The rest of the suite asserts what the emitter WRITES (string and
snapshot checks). That catches compiler regressions but says nothing
about whether the output is valid PL/SQL — reserved words, SQL-only
functions, forward references, nominal type mismatches and the like
only surface when Oracle compiles the package.

This module deploys every example against the database in PELL_DB_URL
and fails on any compile error. Without PELL_DB_URL the module skips
(CI boxes without Oracle still run the rest of the suite), so:

    PELL_DB_URL=... pytest            -> full validation incl. Oracle
    pytest                            -> emitter-behavior tests only

Cross-schema examples that fail with ORA-01031/ORA-04050 are SKIPPED
with a pointer at scripts/setup_example_schemas.sql — that's a grants
problem on the test instance, not a pell bug. Everything else that
fails to install is a real failure.
"""
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
    reason="PELL_DB_URL not set — live deploy verification skipped",
)


def _deploy(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pell", "deploy", str(path),
         "--out-dir", tempfile.mkdtemp()],
        capture_output=True, text=True,
        cwd=COMPILER,
        env={**os.environ, "PYTHONPATH": str(COMPILER)},
    )


@pytest.mark.parametrize(
    "example",
    sorted(EXAMPLES.glob("*.pell")),
    ids=lambda p: p.name,
)
def test_example_deploys_clean(example: Path):
    result = _deploy(example)
    out = result.stdout + result.stderr
    # Environmental, not pell bugs: 01031/04050 = cross-schema CREATE
    # rights missing; 01435/01917 = the target schema itself doesn't
    # exist yet. All cleared by running setup_example_schemas.sql.
    if any(code in out for code in
           ("ORA-01031", "ORA-04050", "ORA-01435", "ORA-01917")):
        pytest.skip(
            f"{example.name}: target schema/grants missing — run "
            "scripts/setup_example_schemas.sql as a DBA"
        )
    assert result.returncode == 0, (
        f"{example.name} failed to deploy:\n"
        + "\n".join(
            ln for ln in out.splitlines()
            if "✗" in ln or "PLS-" in ln or "ORA-" in ln or "error" in ln.lower()
        )[:2000]
    )
