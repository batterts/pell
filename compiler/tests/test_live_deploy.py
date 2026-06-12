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


# External objects the examples reference but (by design) don't create —
# pell emits no DDL. 14_sequence reads employee_id_seq; 15_retry inserts
# into the retry tables; 16/29's @touches pinning cursors need the dyn_*
# and pivot_orders tables to exist at compile time.
_DEMO_OBJECTS = [
    "CREATE SEQUENCE employee_id_seq",
    "CREATE TABLE retry_jobs ("
    " name VARCHAR2(100), status VARCHAR2(20), attempted_at TIMESTAMP)",
    "CREATE TABLE retry_audit_log (action VARCHAR2(100), ts TIMESTAMP)",
    "CREATE TABLE dyn_orders (id NUMBER, status VARCHAR2(20))",
    "CREATE TABLE dyn_archive_orders (id NUMBER, status VARCHAR2(20))",
    "CREATE TABLE pivot_orders ("
    " product VARCHAR2(50), region VARCHAR2(20), sales NUMBER)",
    # 04_inventory deploys into INVENTORY and reads/writes its skus
    # table. The proper table (seq-backed id DEFAULT) is created by
    # setup_example_schemas.sql in DBA context; creating the DEFAULT
    # cross-schema needs READ on the sequence (ORA-41900), so this
    # fixture fallback only guarantees a compile-sufficient shape.
    "CREATE SEQUENCE inventory.skus_seq",
    "CREATE TABLE inventory.skus ("
    " id NUMBER, code VARCHAR2(40), qty NUMBER, expires_at DATE)",
    # Compile-sufficient shapes for every schema-qualified example;
    # the DBA setup script owns the canonical versions.
    "CREATE TABLE hr.employees (id NUMBER, name VARCHAR2(100),"
    " email VARCHAR2(100), grade NUMBER)",
    "CREATE TABLE billing.accounts (id NUMBER, balance NUMBER, frozen NUMBER)",
    "CREATE TABLE reports.orders (order_id NUMBER, total NUMBER, created_at DATE)",
    "CREATE TABLE lookups.countries (code VARCHAR2(8), name VARCHAR2(80))",
    "CREATE TABLE lookups.audit_tbl (event VARCHAR2(200), ts DATE)",
    "CREATE TABLE orders.orders (id NUMBER, customer_id NUMBER,"
    " status VARCHAR2(20), total NUMBER, created_at DATE)",
    "CREATE TABLE orders.order_lines (id NUMBER, order_id NUMBER,"
    " sku_id NUMBER, qty NUMBER)",
    "CREATE TABLE orders.skus (id NUMBER, qty NUMBER)",
    "CREATE TABLE orders.audit_log (id NUMBER, event VARCHAR2(200),"
    " order_id NUMBER, occurred_at DATE)",
    "CREATE TABLE auditing.ledger (entry_id NUMBER, account_id NUMBER,"
    " amount NUMBER, ts DATE)",
    "CREATE TABLE bulk.num_table (n NUMBER)",
    "CREATE TABLE market.stocktable (sym VARCHAR2(10), price NUMBER)",
]


@pytest.fixture(scope="module", autouse=True)
def _provision_examples_env():
    """Make the sweep self-contained on a fresh database.

    Creates the demo tables/sequence above (idempotent — ORA-00955
    swallowed) and installs runtime/catalog.pell, which 28_repl_demo
    imports.
    """
    from pell.driver import connect
    c = connect()
    try:
        with c.raw.cursor() as cur:
            for ddl in _DEMO_OBJECTS:
                try:
                    cur.execute(ddl)
                except Exception as e:
                    if "ORA-00955" not in str(e):  # name already used
                        raise
    finally:
        c.close()
    r = _deploy(COMPILER / "runtime" / "catalog.pell")
    assert r.returncode == 0, (
        f"runtime/catalog.pell failed to deploy:\n{r.stdout}{r.stderr}"
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
