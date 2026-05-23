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
        just `/` (SQL*Plus convention)."""
        with self.raw.cursor() as cur:
            for stmt in _split_script(sql_script):
                cur.execute(stmt)

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
