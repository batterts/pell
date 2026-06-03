"""Oracle driver wrapper for `pell exec` and `pell repl`.

This module is the only place pell touches `oracledb`. It exposes a thin
helpful surface:

* `connect(dsn)` opens a thin-mode connection.
* `Connection.run_block(sql, binds)` runs an anonymous PL/SQL block and
  returns DBMS_OUTPUT lines as a list[str].
* `Connection.run_query(sql, binds)` runs a SELECT and returns a list of
  dicts keyed by lowercase column name; CLOB columns are read as full
  Python str.

Bind handling auto-promotes Python `str` values longer than 4000 bytes to
CLOB binds so big payloads pass through without the caller having to
construct LOBs by hand. Smaller strings stay as VARCHAR2.
"""

from __future__ import annotations

import os
from typing import Any, Optional


# Python str values above this length become CLOB binds rather than VARCHAR2.
# Oracle's SQL-level VARCHAR2 maxes at 4000 bytes (32K with MAX_STRING_SIZE
# EXTENDED, but that's not universal). 4000 is the safe cutoff.
CLOB_BIND_THRESHOLD = 4000


def connect(dsn: Optional[str] = None) -> "Connection":
    """Open a connection to Oracle in thin mode.

    `dsn` accepts the URL form `user/pass@host:port/service`. If omitted,
    falls back to the `PELL_DB_URL` environment variable.
    """
    import oracledb

    url = dsn or os.environ.get("PELL_DB_URL")
    if not url:
        raise RuntimeError(
            "no connection string. Pass --connect user/pass@host:port/service "
            "or set the PELL_DB_URL environment variable."
        )
    user, password, host, port, service = _parse_dsn(url)
    raw = oracledb.connect(
        user=user, password=password,
        host=host, port=port, service_name=service,
    )
    return Connection(raw)


def _parse_dsn(url: str) -> tuple[str, str, str, int, str]:
    """`user/pass@host:port/service` → tuple."""
    if "@" not in url:
        raise ValueError(f"bad DSN {url!r}: missing `@`")
    creds, hostpart = url.split("@", 1)
    if "/" not in creds:
        raise ValueError(f"bad DSN {url!r}: credentials must be `user/pass`")
    user, password = creds.split("/", 1)
    if "/" not in hostpart:
        raise ValueError(f"bad DSN {url!r}: missing `/service`")
    hostport, service = hostpart.rsplit("/", 1)
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = hostport, 1521
    return user, password, host, port, service


class InstallError(Exception):
    """Raised by Connection.execute_install when USER_ERRORS shows a
    compile error on a freshly-created object. Message lists each
    error as `LINE:POSITION  TEXT` so cmd_deploy's `✗` line pinpoints
    the offending object."""

    def __init__(
        self, name: str, obj_type: str, errors: list[tuple[int, int, str]],
    ) -> None:
        self.name = name
        self.obj_type = obj_type
        self.errors = errors
        body = "\n".join(
            f"    {line}:{pos}  {text}" for line, pos, text in errors
        )
        super().__init__(
            f"{obj_type} {name} compiled with {len(errors)} error(s):\n{body}"
        )


class Connection:
    """Thin wrapper around an oracledb connection.

    Adds DBMS_OUTPUT capture, CLOB-aware bind preparation, and convenience
    helpers for anonymous blocks and SELECTs.
    """

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        with self.raw.cursor() as cur:
            cur.callproc("dbms_output.enable", [None])

    def run_block(self, pl_sql: str, binds: Optional[dict[str, Any]] = None) -> list[str]:
        """Execute an anonymous PL/SQL block; return DBMS_OUTPUT lines.

        Trailing `/` and `;` are tolerated so emitter output can be passed
        straight through.
        """
        block = _strip_terminator(pl_sql)
        with self.raw.cursor() as cur:
            final_binds = _finalize_binds(cur, binds or {})
            cur.execute(block, final_binds)
            return _drain_dbms_output(cur)

    def run_query(self, sql: str, binds: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """Execute a SELECT; return list of {col_lowercase: value} dicts.

        CLOB columns are materialized to Python str. Date/timestamp etc.
        come back as the driver's natural Python types (datetime).
        """
        with self.raw.cursor() as cur:
            final_binds = _finalize_binds(cur, binds or {})
            cur.execute(_strip_terminator(sql), final_binds)
            cols = [d[0].lower() for d in cur.description]
            rows: list[dict[str, Any]] = []
            for r in cur:
                rows.append({name: _read_lob(val) for name, val in zip(cols, r)})
            return rows

    def execute_install(self, sql_script: str) -> None:
        """Run a `/`-terminated multi-statement install script — the format
        the build emitter produces. Statements split on lines that are
        just `/` (SQL*Plus convention).

        Oracle's CREATE OR REPLACE *always succeeds* at the SQL level
        even when the body has compile errors. The dirty case is
        signaled as ORA-24344 / DPY-7000 ("success with compilation
        error") which python-oracledb surfaces on `cur.warning` rather
        than raising. So:

          1. Run the stmt.
          2. If `cur.warning` is set, parse out the object name + type
             from the DDL and pull error rows from USER_ERRORS, raise
             InstallError with the details.
          3. If no warning, object compiled clean — no extra query.

        Net effect: clean deploys do zero USER_ERRORS roundtrips; only
        the actual error case pays for the lookup.
        """
        with self.raw.cursor() as cur:
            for stmt in _split_script(sql_script):
                cur.execute(stmt)
                if cur.warning is None:
                    continue
                obj = _parse_create_object(stmt)
                if obj is None:
                    # Oracle warned but we can't pin the object — surface
                    # the raw warning so the user knows something compiled
                    # dirty even if we can't pinpoint where.
                    raise InstallError(
                        "<unknown>", "<unknown>",
                        [(0, 0, str(cur.warning))],
                    )
                name, obj_type = obj
                errors = _user_errors_for(cur, name, obj_type)
                if errors:
                    raise InstallError(name, obj_type, errors)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


# ---------------------------------------------------------------------------
# Bind / LOB helpers
# ---------------------------------------------------------------------------


def _finalize_binds(cur: Any, binds: dict[str, Any]) -> dict[str, Any]:
    """Walk bind dict; any str > CLOB_BIND_THRESHOLD bytes becomes an
    oracledb CLOB Variable bound to the cursor so Oracle accepts large
    payloads in PL/SQL CLOB params and SQL VARCHAR2 columns alike."""
    import oracledb
    out: dict[str, Any] = {}
    for k, v in binds.items():
        if isinstance(v, str) and len(v.encode("utf-8")) > CLOB_BIND_THRESHOLD:
            var = cur.var(oracledb.DB_TYPE_CLOB)
            var.setvalue(0, v)
            out[k] = var
        else:
            out[k] = v
    return out


def _read_lob(value: Any) -> Any:
    """Materialize a LOB locator to its full str/bytes payload."""
    if hasattr(value, "read") and not isinstance(value, str):
        try:
            return value.read()
        except Exception:
            return value
    return value


# ---------------------------------------------------------------------------
# DBMS_OUTPUT capture
# ---------------------------------------------------------------------------


def _drain_dbms_output(cur: Any) -> list[str]:
    """Loop DBMS_OUTPUT.GET_LINE until status != 0 — picks up everything the
    block printed."""
    lines: list[str] = []
    line_var = cur.var(str, 32767)
    status_var = cur.var(int)
    while True:
        cur.callproc("dbms_output.get_line", [line_var, status_var])
        if status_var.getvalue() != 0:
            break
        v = line_var.getvalue()
        lines.append("" if v is None else v)
    return lines


# ---------------------------------------------------------------------------
# Script helpers
# ---------------------------------------------------------------------------


def _strip_terminator(s: str) -> str:
    """Drop the SQL*Plus terminator `/` (with surrounding whitespace) so
    emit-style output can be re-fed into `cur.execute()`. The final `;`
    of `END;` is required PL/SQL syntax and must NOT be stripped."""
    t = s.rstrip()
    if t.endswith("/"):
        t = t[:-1].rstrip()
    return t


def _split_script(sql_script: str) -> list[str]:
    """Split a `/`-terminated multi-statement script into individual
    statements suitable for `cur.execute()`."""
    chunks: list[str] = []
    buf: list[str] = []
    for line in sql_script.splitlines():
        if line.strip() == "/":
            stmt = "\n".join(buf).strip()
            if stmt:
                chunks.append(stmt)
            buf = []
        else:
            buf.append(line)
    tail = "\n".join(buf).strip()
    if tail:
        chunks.append(tail)
    return chunks


# ---------------------------------------------------------------------------
# USER_ERRORS pinning — Oracle's CREATE OR REPLACE always succeeds at the
# SQL level. For object types that have a compile phase (packages, types,
# triggers, views, ...), errors only surface in USER_ERRORS. These helpers
# let execute_install detect that without false positives.
# ---------------------------------------------------------------------------

import re as _re

# Order matters: longer specifiers first so PACKAGE BODY doesn't get
# matched as PACKAGE with leftover BODY. Same for TYPE BODY.
# The name part is a dotted identifier: either schema-qualified
# (`scott.foo`), bare (`foo`), or quoted (`"My Pkg"`). Each segment
# is bare-or-quoted independently. We capture the whole thing so we
# can strip down to the last segment afterwards (USER_ERRORS keys on
# the schema-local name, not the qualified form). Pell emits modules
# like `pell_test.hello` as a single dotted identifier in the DDL,
# which the previous "[A-Za-z_$][A-Za-z0-9_$]*" pattern stopped at
# the first dot — that captured the schema name `pell_test` and
# the subsequent USER_ERRORS lookup never matched anything, so a
# clearly-broken PACKAGE BODY was reported as deployed-OK.
_CREATE_OBJECT_RE = _re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?"
    r"(PACKAGE\s+BODY|TYPE\s+BODY|PACKAGE|TYPE|"
    r"FUNCTION|PROCEDURE|TRIGGER|VIEW)\s+"
    r"((?:\"[^\"]+\"|[A-Za-z_$][A-Za-z0-9_$]*)"
    r"(?:\.(?:\"[^\"]+\"|[A-Za-z_$][A-Za-z0-9_$]*))*)",
    _re.IGNORECASE,
)


def _parse_create_object(stmt: str) -> Optional[tuple[str, str]]:
    """Return (name_upper, type_upper) of the object being created by
    `stmt`, or None if it's not a CREATE OR REPLACE for an object type
    that has a USER_ERRORS row.

    Schema-qualified names ("scott.foo") AND dotted-module names
    ("pell_test.hello", which pell emits for `module pell_test.hello`)
    are stripped to the last segment — that's how Oracle stores the
    object name in USER_OBJECTS / USER_ERRORS.
    """
    m = _CREATE_OBJECT_RE.search(stmt)
    if m is None:
        return None
    obj_type = _re.sub(r"\s+", " ", m.group(1).upper())
    raw_name = m.group(2)
    # Last segment of a dotted name. Strip surrounding quotes if any.
    last = raw_name.rsplit(".", 1)[-1]
    if last.startswith('"') and last.endswith('"'):
        last = last[1:-1]
    return last.upper(), obj_type


def _user_errors_for(
    cur: Any, name: str, obj_type: str,
) -> list[tuple[int, int, str]]:
    """Pull all errors for (name, type) from USER_ERRORS, ordered by
    sequence. Returns [] when the object compiled clean."""
    cur.execute(
        "SELECT line, position, text FROM user_errors "
        "WHERE name = :n AND type = :t "
        "ORDER BY sequence",
        {"n": name, "t": obj_type},
    )
    return [(int(line), int(pos), text) for line, pos, text in cur.fetchall()]
