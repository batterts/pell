"""Runtime EXECUTION tests — deploy real examples, call them, assert results.

One rung above test_live_deploy.py: compiling clean doesn't mean the
package computes the right answer. These tests deploy a handful of
deterministic, table-free examples and invoke their pub fns through
the driver, asserting actual outputs.

Skipped without PELL_DB_URL (same contract as test_live_deploy):

    PELL_DB_URL=... pytest tests/test_live_execution.py

Examples covered (deliberately ones with no table dependencies):
    01_hello          string interpolation through a fn
    24_strings        text_utils: case, reverse-via-SQL, predicates
    22_linked_list    sealed-type recursion: from_list/summarize/reversed
    21_re_capture     pell_re named-group capture into records
    18_json           JSON construct/extract round-trips
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
    reason="PELL_DB_URL not set — live execution tests skipped",
)

_NEEDED = [
    "01_hello.pell",
    "24_strings.pell",
    "22_linked_list.pell",
    "21_re_capture.pell",
    "18_json.pell",
]


@pytest.fixture(scope="module")
def conn():
    """Deploy the needed examples once, then hand out a connection."""
    for name in _NEEDED:
        r = subprocess.run(
            [sys.executable, "-m", "pell", "deploy", str(EXAMPLES / name),
             "--out-dir", tempfile.mkdtemp()],
            capture_output=True, text=True, cwd=COMPILER,
            env={**os.environ, "PYTHONPATH": str(COMPILER)},
        )
        assert r.returncode == 0, f"deploy {name} failed:\n{r.stdout}{r.stderr}"
    from pell.driver import connect
    c = connect()
    yield c
    c.close()


def _scalar(conn, expr: str):
    """Evaluate a single SQL-callable expression and return its value."""
    rows = conn.run_query(f"select {expr} as v from dual")
    return rows[0]["v"]


def _block(conn, body: str) -> list[str]:
    # Drain any DBMS_OUTPUT left in the session buffer by an earlier
    # test that raised mid-call — otherwise stale lines prepend to this
    # block's output and the assertion mysteriously fails.
    from pell.driver import _drain_dbms_output
    with conn.raw.cursor() as cur:
        _drain_dbms_output(cur)
    return conn.run_block(body)


# ---- 01_hello -------------------------------------------------------------

def test_greet_interpolates(conn):
    assert _scalar(conn, "hello.greet('world')") == "hello, world"


# ---- 24_strings -----------------------------------------------------------

def test_text_utils_upper_lower(conn):
    assert _scalar(conn, "text_utils.to_upper('abc')") == "ABC"
    assert _scalar(conn, "text_utils.to_lower('ABC')") == "abc"


def test_text_utils_reversed_via_sql_engine(conn):
    # reversed() routes through sql!{ select reverse(...) }.scalar() —
    # exercises the SQL-only-function workaround end to end.
    assert _scalar(conn, "text_utils.reversed('abc')") == "cba"


def test_text_utils_predicates(conn):
    # bool fns aren't SQL-callable pre-23 bool binding; go through PL/SQL.
    out = _block(conn, """
        BEGIN
          IF text_utils.has('haystack', 'hay') THEN
            dbms_output.put_line('has=Y');
          END IF;
          IF NOT text_utils.is_blank('x') THEN
            dbms_output.put_line('blank=N');
          END IF;
        END;
    """)
    assert out == ["has=Y", "blank=N"]


def test_text_utils_replace_all(conn):
    assert _scalar(conn, "text_utils.replace_all('a-b-c', '-', '+')") == "a+b+c"


# ---- 22_linked_list -------------------------------------------------------

def test_linked_list_summarize(conn):
    out = _block(conn, """
        DECLARE
          xs  collections_linked_list.t_number_list;
          lst t_intlist;
          s   collections_linked_list.t_summary;
        BEGIN
          xs(1) := 4; xs(2) := 0; xs(3) := 7;
          lst := collections_linked_list.from_list(xs);
          s := collections_linked_list.summarize(lst);
          dbms_output.put_line('count=' || s.count);
          dbms_output.put_line('total=' || s.total);
          dbms_output.put_line('zero=' ||
            CASE WHEN s.contains_zero THEN 'Y' ELSE 'N' END);
        END;
    """)
    assert out == ["count=3", "total=11", "zero=Y"]


def test_linked_list_reverse_preserves_sum(conn):
    out = _block(conn, """
        DECLARE
          xs  collections_linked_list.t_number_list;
          lst t_intlist;
          rev t_intlist;
        BEGIN
          xs(1) := 1; xs(2) := 2; xs(3) := 3;
          lst := collections_linked_list.from_list(xs);
          rev := collections_linked_list.reversed(lst);
          dbms_output.put_line('sum=' || rev.sum());
          dbms_output.put_line('len=' || rev.len());
        END;
    """)
    assert out == ["sum=6", "len=3"]


# ---- 21_re_capture --------------------------------------------------------

def test_re_capture_email(conn):
    out = _block(conn, """
        DECLARE
          e re_capture_demo.t_email;
        BEGIN
          e := re_capture_demo.parse_email('bob@example.com');
          dbms_output.put_line('user=' || e.user);
          dbms_output.put_line('domain=' || e.domain);
        END;
    """)
    assert out == ["user=bob", "domain=example.com"]


def test_re_capture_kv(conn):
    out = _block(conn, """
        DECLARE
          kv re_capture_demo.t_kv;
        BEGIN
          kv := re_capture_demo.parse_kv('limit=250');
          dbms_output.put_line(kv.k || '->' || kv.v);
        END;
    """)
    assert out == ["limit->250"]


# ---- 18_json --------------------------------------------------------------

def test_json_list_things(conn):
    v = _scalar(conn, "json_serialize(json_demo.list_things(1, 2, 3))")
    assert v == "[1,2,3]"


def test_json_extract_text(conn):
    v = _scalar(
        conn,
        "json_demo.extract_text(json('{\"a\":{\"b\":\"deep\"}}'), '$.a.b')",
    )
    assert v == "deep"
