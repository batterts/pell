"""Run the utPLSQL vetting suite (examples/36_ut_examples.pell) live.

This is the behavioral layer above the deploy sweep: the deployed
examples are CALLED with real arguments and their results asserted
database-side by utPLSQL. A regression in any covered module's
semantics — not just its compilability — fails here.

Requires PELL_DB_URL and an instance with utPLSQL installed
(./docker/install_utplsql.sh for the harness); skips otherwise.
"""
import os
import re
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
    reason="PELL_DB_URL not set — utPLSQL vetting skipped",
)

# The suite covers every example module — deploy the whole directory
# fresh so the asserts test CURRENT emitter output.
_NEEDED = ["."]


def _ut_installed(conn) -> bool:
    rows = conn.run_query(
        "select count(*) n from all_synonyms "
        "where owner = 'PUBLIC' and synonym_name = 'UT'")
    return int(list(rows[0].values())[0]) > 0


def test_ut_vetting_suite_passes():
    from pell.driver import connect
    conn = connect()
    try:
        if not _ut_installed(conn):
            pytest.skip("utPLSQL not installed — run docker/install_utplsql.sh")
        for name in _NEEDED:
            r = subprocess.run(
                [sys.executable, "-m", "pell", "deploy",
                 str(EXAMPLES / name), "--keep-going",
                 "--out-dir", tempfile.mkdtemp()],
                capture_output=True, text=True, cwd=COMPILER,
                env={**os.environ, "PYTHONPATH": str(COMPILER)},
            )
            assert r.returncode == 0, f"deploy {name} failed:\n{r.stdout}{r.stderr}"

        out = conn.run_block("begin ut.run('ut_pell_examples'); end;")
        report = "\n".join(out)
        m = re.search(
            r"(\d+) tests?, (\d+) failed, (\d+) errored", report)
        assert m, f"couldn't find the utPLSQL summary in:\n{report}"
        tests, failed, errored = (int(g) for g in m.groups())
        assert tests >= 33, f"suite shrank to {tests} tests:\n{report}"
        assert failed == 0 and errored == 0, (
            f"utPLSQL vetting found regressions:\n{report}"
        )
    finally:
        conn.close()
