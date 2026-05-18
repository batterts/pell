"""Certify which Oracle SQL functions pell can pass through to PL/SQL.

Strategy: for each test we build an anonymous PL/SQL block that has the
same shape pell emits — `DECLARE r VARCHAR2(...); BEGIN r := TO_CHAR(<call>);
:out := r; END;` — and execute it via EXECUTE IMMEDIATE inside a
WHEN-OTHERS handler. Per-test isolation: a compile error in one test
doesn't poison the rest. The Oracle-side test PROVES pass-through works
for that function shape; pell's own lowering for `<bare-fn>(args)` produces
exactly the form we test.

Output: a markdown coverage table.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Connection wrapper — a script (or SQLcl invocation) that takes a `@file`
# argument and runs it against your Oracle instance. Set
# $PELL_SQL_RUNNER to a wrapper that pre-binds your credentials so the
# username/password never reach source control. Falls back to `sql` on
# PATH; you'll then need a tnsnames.ora or wallet for it to connect.
SQLTUN = os.environ.get("PELL_SQL_RUNNER", "sql")


# (slug, category, return_kind, pell_call_expression)
# return_kind picks the right wrapper:
#   "scalar"   → TO_CHAR(<call>)
#   "raw"      → RAWTOHEX(<call>)
#   "date"     → TO_CHAR(<call>, 'YYYY-MM-DD')
#   "ts"       → TO_CHAR(<call>, 'YYYY-MM-DD HH24:MI:SS')
#   "interval" → TO_CHAR(<call>)
TESTS: list[tuple[str, str, str, str]] = [
    # ---- character / string ---------------------------------------
    ("ascii",          "string",  "scalar", 'ascii("A")'),
    ("asciistr",       "string",  "scalar", 'asciistr("hi")'),
    ("chr",            "string",  "scalar", 'chr(65)'),
    ("concat",         "string",  "scalar", 'concat("ab", "cd")'),
    ("initcap",        "string",  "scalar", 'initcap("hello world")'),
    ("instr",          "string",  "scalar", 'instr("hello", "ll")'),
    ("instrb",         "string",  "scalar", 'instrb("hello", "ll")'),
    ("instrc",         "string",  "scalar", 'instrc("hello", "ll")'),
    ("length",         "string",  "scalar", 'length("hi")'),
    ("lengthb",        "string",  "scalar", 'lengthb("hi")'),
    ("lengthc",        "string",  "scalar", 'lengthc("hi")'),
    ("lower",          "string",  "scalar", 'lower("ABC")'),
    ("lpad",           "string",  "scalar", 'lpad("a", 3, "x")'),
    ("ltrim",          "string",  "scalar", 'ltrim("  hi")'),
    ("nls_initcap",    "string",  "scalar", 'nls_initcap("hello")'),
    ("nls_lower",      "string",  "scalar", 'nls_lower("ABC")'),
    ("nls_upper",      "string",  "scalar", 'nls_upper("abc")'),
    ("nlssort",        "string",  "raw",    'nlssort("abc")'),
    ("regexp_count",   "string",  "scalar", 'regexp_count("ababab", "ab")'),
    ("regexp_instr",   "string",  "scalar", 'regexp_instr("a1b2", "[0-9]")'),
    ("regexp_replace", "string",  "scalar", 'regexp_replace("a1b2", "[0-9]", "#")'),
    ("regexp_substr",  "string",  "scalar", 'regexp_substr("a1b2", "[0-9]+")'),
    ("replace",        "string",  "scalar", 'replace("hello", "l", "L")'),
    ("rpad",           "string",  "scalar", 'rpad("a", 3, "x")'),
    ("rtrim",          "string",  "scalar", 'rtrim("hi  ")'),
    ("soundex",        "string",  "scalar", 'soundex("smith")'),
    ("substr",         "string",  "scalar", 'substr("hello", 2, 3)'),
    ("substrb",        "string",  "scalar", 'substrb("hello", 2, 3)'),
    ("substrc",        "string",  "scalar", 'substrc("hello", 2, 3)'),
    ("translate",      "string",  "scalar", 'translate("abc", "abc", "xyz")'),
    ("trim",           "string",  "scalar", 'trim("  hi  ")'),
    ("unistr",         "string",  "scalar", 'unistr("hello")'),
    ("upper",          "string",  "scalar", 'upper("abc")'),

    # ---- numeric --------------------------------------------------
    ("abs",            "numeric", "scalar", 'abs(-5)'),
    ("acos",           "numeric", "scalar", 'acos(0.5)'),
    ("asin",           "numeric", "scalar", 'asin(0.5)'),
    ("atan",           "numeric", "scalar", 'atan(1)'),
    ("atan2",          "numeric", "scalar", 'atan2(1, 1)'),
    ("bitand",         "numeric", "scalar", 'bitand(12, 10)'),
    ("ceil",           "numeric", "scalar", 'ceil(3.2)'),
    ("cos",            "numeric", "scalar", 'cos(0)'),
    ("cosh",           "numeric", "scalar", 'cosh(0)'),
    ("exp",            "numeric", "scalar", 'exp(1)'),
    ("floor",          "numeric", "scalar", 'floor(3.8)'),
    ("ln",             "numeric", "scalar", 'ln(1)'),
    ("log",            "numeric", "scalar", 'log(10, 100)'),
    ("mod",            "numeric", "scalar", 'mod(10, 3)'),
    ("power",          "numeric", "scalar", 'power(2, 10)'),
    ("remainder",      "numeric", "scalar", 'remainder(10, 3)'),
    ("round_1arg",     "numeric", "scalar", 'round(3.567)'),
    ("round_2arg",     "numeric", "scalar", 'round(3.567, 1)'),
    ("sign",           "numeric", "scalar", 'sign(-7)'),
    ("sin",            "numeric", "scalar", 'sin(0)'),
    ("sinh",           "numeric", "scalar", 'sinh(0)'),
    ("sqrt",           "numeric", "scalar", 'sqrt(16)'),
    ("tan",            "numeric", "scalar", 'tan(0)'),
    ("tanh",           "numeric", "scalar", 'tanh(0)'),
    ("trunc_1arg",     "numeric", "scalar", 'trunc(3.7)'),
    ("trunc_2arg",     "numeric", "scalar", 'trunc(3.567, 1)'),

    # ---- date / timestamp ----------------------------------------
    ("sysdate",        "date",    "date",     'sysdate'),
    ("systimestamp",   "date",    "ts",       'systimestamp'),
    ("current_date",   "date",    "date",     'current_date'),
    ("current_ts",     "date",    "ts",       'current_timestamp'),
    ("localtimestamp", "date",    "ts",       'localtimestamp'),
    ("add_months",     "date",    "date",     'add_months(sysdate, 3)'),
    ("last_day",       "date",    "date",     'last_day(sysdate)'),
    ("months_between", "date",    "scalar",   'months_between(sysdate, sysdate)'),
    ("next_day",       "date",    "date",     'next_day(sysdate, "MONDAY")'),
    ("numtodsinterval","date",    "interval", 'numtodsinterval(5, "DAY")'),
    ("numtoyminterval","date",    "interval", 'numtoyminterval(5, "MONTH")'),
    ("sys_extract_utc","date",    "ts",       'sys_extract_utc(systimestamp)'),
    ("dbtimezone",     "date",    "scalar",   'dbtimezone'),
    ("sessiontimezone","date",    "scalar",   'sessiontimezone'),
    ("from_tz",        "date",    "ts",       'from_tz(cast(sysdate as timestamp), "UTC")'),
    ("tz_offset",      "date",    "scalar",   'tz_offset("US/Eastern")'),

    # ---- conversion ----------------------------------------------
    ("hextoraw",       "conv",    "raw",      'hextoraw("48")'),
    ("rawtohex",       "conv",    "scalar",   'rawtohex(hextoraw("48"))'),
    ("to_char_num",    "conv",    "scalar",   'to_char(123)'),
    ("to_char_date",   "conv",    "scalar",   'to_char(sysdate, "YYYY-MM-DD")'),
    ("to_number",      "conv",    "scalar",   'to_number("42")'),
    ("to_date",        "conv",    "date",     'to_date("2024-01-01", "YYYY-MM-DD")'),
    ("to_timestamp",   "conv",    "ts",       'to_timestamp("2024-01-01 12:00:00", "YYYY-MM-DD HH24:MI:SS")'),
    ("to_dsinterval",  "conv",    "interval", 'to_dsinterval("0 01:00:00")'),
    ("to_yminterval",  "conv",    "interval", 'to_yminterval("1-2")'),
    ("to_bin_double",  "conv",    "scalar",   'to_binary_double(1.5)'),
    ("to_bin_float",   "conv",    "scalar",   'to_binary_float(1.5)'),
    ("to_clob",        "conv",    "scalar",   'to_clob("hello")'),
    ("to_blob",        "conv",    "raw",      'to_blob(hextoraw("4865"))'),
    ("compose",        "conv",    "scalar",   'compose("o")'),
    ("decompose",      "conv",    "scalar",   'decompose("a")'),
    # CAST is a SQL operator with its own syntactic form, not a function call.
    # It does not pass through pell's `<ident>(args)` lowering — covered by
    # design.md but not by this audit.

    # ---- null / conditional --------------------------------------
    ("nvl",            "null",    "scalar", 'nvl(1, 0)'),
    ("coalesce",       "null",    "scalar", 'coalesce(1, 2, 3)'),
    ("nullif_eq",      "null",    "scalar", 'nullif(1, 1)'),
    ("nullif_neq",     "null",    "scalar", 'nullif(1, 2)'),
    ("greatest",       "null",    "scalar", 'greatest(1, 2, 3)'),
    ("least",          "null",    "scalar", 'least(1, 2, 3)'),
    # Known SQL-only ones — included as negative tests.
    ("decode",         "null",    "scalar", 'decode(1, 1, "a", 2, "b", "z")'),
    ("nvl2",           "null",    "scalar", 'nvl2(1, "a", "b")'),
    ("lnnvl",          "null",    "scalar", 'lnnvl(1 = 1)'),

    # ---- environment / hash --------------------------------------
    ("user",           "env",     "scalar", 'user'),
    ("uid",            "env",     "scalar", 'uid'),
    ("sys_guid",       "env",     "raw",    'sys_guid()'),
    ("sys_context",    "env",     "scalar", 'sys_context("userenv", "session_user")'),
    ("vsize",          "env",     "scalar", 'vsize("hello")'),
    ("standard_hash",  "env",     "raw",    'standard_hash("hello")'),
    ("dump",           "env",     "scalar", 'dump("hello", 16)'),
    ("ora_hash",       "env",     "scalar", 'ora_hash("hello")'),

    # ---- PL/SQL-callable packaged functions ----------------------
    ("dbms_util_hash", "pkg",     "scalar", 'dbms_utility.get_hash_value("hello", 0, 1073741824)'),
    ("dbms_random_val","pkg",     "scalar", 'dbms_random.value'),
    ("dbms_random_str","pkg",     "scalar", "dbms_random.string('U', 8)"),
    ("utl_raw_xor",    "pkg",     "raw",    'utl_raw.bit_xor(hextoraw("48"), hextoraw("11"))'),
    ("utl_raw_cast",   "pkg",     "raw",    'utl_raw.cast_to_raw("hi")'),
]


def pell_to_plsql(call: str) -> str:
    """Mirror pell's lowering for these expressions.

    - Double-quoted strings → single-quoted
    - `::` qualifier → `.`
    Pell uses these conventions for bare/package calls; the test
    expressions are written in pell style so we can keep a single source
    of truth.
    """
    out = re.sub(r'"([^"]*)"', r"'\1'", call)
    out = out.replace("::", ".")
    return out


def wrap_for_kind(call: str, kind: str) -> str:
    """Render `expr` as a VARCHAR2 result we can compare across types."""
    if kind == "raw":
        return f"RAWTOHEX({call})"
    if kind == "date":
        return f"TO_CHAR({call}, 'YYYY-MM-DD')"
    if kind == "ts":
        return f"TO_CHAR({call}, 'YYYY-MM-DD HH24:MI:SS')"
    if kind == "interval":
        return f"TO_CHAR({call})"
    return f"TO_CHAR({call})"


def main() -> int:
    driver = [
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "SET LINESIZE 4000",
        "SET FEEDBACK OFF",
        "DECLARE",
        "  l_r VARCHAR2(4000);",
        "BEGIN",
    ]
    for slug, _cat, kind, pell_call in TESTS:
        plsql = pell_to_plsql(pell_call)
        wrapped = wrap_for_kind(plsql, kind)
        block = (
            "  BEGIN\n"
            "    DECLARE\n"
            "      l_inner VARCHAR2(4000);\n"
            "    BEGIN\n"
            f"      EXECUTE IMMEDIATE\n"
            f"        'BEGIN :r := {wrapped.replace(chr(39), chr(39)*2)}; END;'\n"
            "        USING OUT l_inner;\n"
            f"      DBMS_OUTPUT.PUT_LINE('OK|{slug}|' || SUBSTR(l_inner, 1, 200));\n"
            "    END;\n"
            "  EXCEPTION WHEN OTHERS THEN\n"
            f"    DBMS_OUTPUT.PUT_LINE('FAIL|{slug}|' || SUBSTR(SQLERRM, 1, 200));\n"
            "  END;"
        )
        driver.append(block)
    driver.append("END;\n/")
    driver.append("EXIT;")

    driver_path = Path("/tmp/fn_audit_v2.sql")
    driver_path.write_text("\n".join(driver))

    print(f"Running {len(TESTS)} isolated tests …")
    res = subprocess.run([SQLTUN, f"@{driver_path}"], capture_output=True, text=True)

    # Parse
    results: dict[str, tuple[str, str]] = {}
    for line in (res.stdout + res.stderr).splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", line.strip())
        m = re.match(r"^(OK|FAIL)\|([a-z0-9_]+)\|(.*)$", line)
        if m:
            results[m.group(2)] = (m.group(1), m.group(3))

    by_cat: dict[str, list[tuple[str, str, str, str]]] = {}
    for slug, cat, _kind, _call in TESTS:
        status, detail = results.get(slug, ("NORUN", "(no DBMS_OUTPUT line)"))
        by_cat.setdefault(cat, []).append((slug, status, detail, _call))

    # Report
    total = len(TESTS)
    n_ok = sum(1 for ts in by_cat.values() for r in ts if r[1] == "OK")
    n_fail = sum(1 for ts in by_cat.values() for r in ts if r[1] == "FAIL")
    n_norun = sum(1 for ts in by_cat.values() for r in ts if r[1] == "NORUN")
    print(f"\n# pell function pass-through audit — {n_ok}/{total} OK")
    print(f"FAIL: {n_fail}  NORUN: {n_norun}\n")
    for cat in ("string", "numeric", "date", "conv", "null", "env", "pkg"):
        if cat not in by_cat:
            continue
        rows = by_cat[cat]
        ok_count = sum(1 for r in rows if r[1] == "OK")
        print(f"## {cat} ({ok_count}/{len(rows)} ok)")
        for slug, status, detail, call in rows:
            mark = "✓" if status == "OK" else ("✗" if status == "FAIL" else "?")
            short_detail = detail[:90] + ("…" if len(detail) > 90 else "")
            print(f"  {mark} {slug:<18} {status:<6} {short_detail}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
