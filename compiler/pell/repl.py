"""pell REPL — terminal notebook for executing pell against a live Oracle.

Each cell is a pell snippet. Definitions (fn / record / error / seq /
enum) accumulate into a running session module; top-level statements
execute as the body of an anonymous PL/SQL block built from the full
session. Output prints below the cell.

Cells are submitted with a blank line (after non-empty content) or
`Ctrl-D`. Slash commands:

    \\save <file>      dump the current session as a .pell file
    \\load <file>      seed the session from a .pell file
    \\sql <stmt>       run one raw SQL statement (SELECT or DML)
    \\reset            clear all accumulated definitions
    \\show             print the synthesized anonymous block that would run next
    \\connect <dsn>    reconnect to a different database
    \\help             show this help
    \\quit             exit
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from . import ast as A
from .driver import Connection, connect
from .emitter import EmitError, emit_anon_block
from .lexer import LexError
from .parser import ParseError, parse_cell


SLASH_HELP = """\
slash commands:
  \\save <file>     dump current session defs to a .pell file
  \\load <file>     append a .pell file's defs into the session
  \\sql <stmt>      run one raw SQL statement and print results
  \\reset           clear all accumulated defs
  \\show            print the anon block that would run on the next cell
  \\connect <dsn>   reconnect to user/pass@host:port/service
  \\help            this message
  \\quit            exit
"""


class Repl:
    """The notebook-style REPL state machine."""

    def __init__(self, conn: Optional[Connection] = None, target: str = "23") -> None:
        self.conn: Optional[Connection] = conn
        self.target = target
        self.items: list[A.Item] = []
        # Snapshots of variable values captured after each cell runs.
        # Maps var_name → (pell_type_str, literal_value_str).
        # Re-injected as `let x: type = <literal>;` at the top of
        # every subsequent cell so variables persist with their actual
        # computed values — no re-execution of the original expression.
        self.var_snapshots: dict[str, tuple[str, str]] = {}
        # Type annotations of variables, tracked so we can re-emit
        # typed let stmts for snapshots.
        self.var_types: dict[str, str] = {}
        self.cell_number = 0
        self.history = InMemoryHistory()
        self.session: PromptSession = PromptSession(history=self.history)

    # -- main loop ---------------------------------------------------------

    def run(self) -> int:
        print("pell repl — multi-line cell, blank line submits; \\help for commands")
        if self.conn is None:
            print("(no connection — use \\connect user/pass@host:port/service to attach)")
        while True:
            self.cell_number += 1
            try:
                cell = self._read_cell(self.cell_number)
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if cell is None:
                self.cell_number -= 1  # skip empty cell, don't bump counter
                continue
            text = cell.strip()
            if text.startswith("\\"):
                self.cell_number -= 1  # slash commands don't count as cells
                if self._handle_slash(text) == "quit":
                    return 0
                continue
            self._run_cell(text)

    # -- cell I/O ----------------------------------------------------------

    def _read_cell(self, n: int) -> Optional[str]:
        """Read one cell. Returns the joined text (str) or None if user
        submitted an empty cell (handled at outer level)."""
        bindings = KeyBindings()

        @bindings.add("c-d")
        def _(event):
            event.current_buffer.validate_and_handle()

        first_prompt = f"[{n}]> "
        cont_prompt = "    > "
        lines: list[str] = []
        while True:
            prompt = first_prompt if not lines else cont_prompt
            try:
                line = self.session.prompt(
                    prompt,
                    key_bindings=bindings,
                    multiline=False,
                )
            except EOFError:
                if not lines:
                    raise
                break
            if line.strip() == "" and lines:
                break
            if line.strip() == "" and not lines:
                # blank line at the start — produce no cell
                return None
            lines.append(line)
            # Slash commands submit on the first line (single-line by design).
            if len(lines) == 1 and line.lstrip().startswith("\\"):
                break
        return "\n".join(lines)

    # -- cell execution ----------------------------------------------------

    _VAR_SENTINEL = "__PELL_REPL_VAR__"
    _NULL_MARKER  = "__PELL_NULL__"

    def _run_cell(self, source: str) -> None:
        try:
            new_items, stmts = parse_cell(source, f"<cell {self.cell_number}>")
        except (LexError, ParseError) as e:
            print(f"  ! parse error: {e}", file=sys.stderr)
            return
        for item in new_items:
            self._absorb(item)
        if not stmts:
            try:
                emit_anon_block(self.items, [], target=self.target)
            except EmitError as e:
                print(f"  ! compile error: {e}", file=sys.stderr)
            return
        if self.conn is None:
            print("  ! no connection — use \\connect to attach to a database",
                  file=sys.stderr)
            return

        # Build let-stmts that replay prior variable VALUES (not the
        # original expressions). Each snapshot becomes
        # `let x: type = <literal>;` so variables persist with their
        # actual computed values from the last successful cell.
        replay_stmts = self._snapshot_to_stmts()
        full_stmts = replay_stmts + stmts

        try:
            block = emit_anon_block(self.items, full_stmts, target=self.target,
                                    source_path=f"<cell {self.cell_number}>")
        except EmitError as e:
            print(f"  ! compile error: {e}", file=sys.stderr)
            return

        # Inject capture lines into the anonymous block so we can read
        # back variable values after execution. We capture EVERY let in
        # this cell + every previously-snapshotted variable.
        cell_lets = [s for s in stmts if isinstance(s, A.LetStmt)]
        all_var_names = list(self.var_snapshots.keys())
        for s in cell_lets:
            if s.name not in all_var_names:
                all_var_names.append(s.name)
            if s.type_annot:
                from .emitter import lower_type
                self.var_types[s.name] = lower_type(s.type_annot)

        block = self._inject_capture_lines(block, all_var_names)

        try:
            output = self.conn.run_block(block)
        except Exception as e:
            print(f"  ! runtime error: {e}", file=sys.stderr)
            return

        # Partition output: sentinel lines → snapshots, rest → user output
        user_lines: list[str] = []
        for ln in output:
            if ln.startswith(self._VAR_SENTINEL):
                self._parse_snapshot(ln)
            else:
                user_lines.append(ln)
        for ln in user_lines:
            print(ln)

    def _inject_capture_lines(self, block: str, var_names: list[str]) -> str:
        """Splice capture-via-DBMS_OUTPUT lines into the anonymous block
        right before `END pell_anon_main;` so we can read variable
        values back after execution."""
        if not var_names:
            return block
        # Each variable emits one line: __PELL_REPL_VAR__<TAB>name<TAB>value
        capture_lines: list[str] = []
        for name in var_names:
            local = f"l_{name}"
            typ = self.var_types.get(name, "").upper()
            # Type-aware serialization: JSON needs JSON_SERIALIZE,
            # DATE/TIMESTAMP need explicit format masks (not NLS-dependent),
            # RAW needs RAWTOHEX, everything else uses TO_CHAR.
            if "JSON" in typ:
                to_text = f"JSON_SERIALIZE({local})"
            elif "TIMESTAMP" in typ:
                to_text = f"TO_CHAR({local}, 'YYYY-MM-DD HH24:MI:SS.FF9')"
            elif "DATE" in typ:
                to_text = f"TO_CHAR({local}, 'YYYY-MM-DD HH24:MI:SS')"
            elif "RAW" in typ:
                to_text = f"RAWTOHEX({local})"
            else:
                to_text = f"TO_CHAR({local})"
            capture_lines.append(
                f"    dbms_output.put_line('{self._VAR_SENTINEL}' "
                f"|| chr(9) || '{name}' "
                f"|| chr(9) || NVL({to_text}, '{self._NULL_MARKER}'));"
            )
        inject = "\n".join(capture_lines) + "\n"
        # Find the END pell_anon_main; and insert before it.
        marker = "  END pell_anon_main;"
        idx = block.rfind(marker)
        if idx == -1:
            return block  # can't find — emit without capture
        return block[:idx] + inject + block[idx:]

    def _parse_snapshot(self, line: str) -> None:
        """Parse one `__PELL_REPL_VAR__\\tname\\tvalue` line and store
        the snapshot."""
        parts = line.split("\t", 2)  # split into at most 3 — value may contain tabs
        if len(parts) < 3:
            return
        name = parts[1]
        raw_value = parts[2]
        if raw_value == self._NULL_MARKER:
            # Variable is NULL — remove from snapshots so it doesn't
            # inject a stale value later.
            self.var_snapshots.pop(name, None)
        else:
            typ = self.var_types.get(name, "VARCHAR2(4000)")
            self.var_snapshots[name] = (typ, raw_value)

    def _snapshot_to_stmts(self) -> list[A.Stmt]:
        """Build LetStmt nodes from the current snapshots. Each becomes
        `let x: type = <literal>;` in the emitted block.

        The reconstruction is type-aware:
          NUMBER     → NumberLit("42")
          BOOLEAN    → BoolLit(true/false)
          JSON       → json::parse("<serialized>")
          DATE       → sql!{select to_date('...','YYYY-MM-DD HH24:MI:SS') from dual}.one()
          TIMESTAMP  → sql!{select to_timestamp('...','YYYY-MM-DD HH24:MI:SS.FF9') from dual}.one()
          RAW        → sql!{select hextoraw('...') from dual}.one()
          text (default) → TextLit("<value>")
        """
        loc = A.Loc("<repl-snapshot>", 0, 0)
        stmts: list[A.Stmt] = []
        for name, (typ, val) in self.var_snapshots.items():
            typ_upper = typ.upper()
            if "NUMBER" in typ_upper or "PLS_INTEGER" in typ_upper:
                expr: A.Expr = A.NumberLit(loc=loc, value=val)
                type_annot: A.TypeRef = A.PrimType(loc=loc, name="number")
            elif "BOOLEAN" in typ_upper:
                expr = A.BoolLit(loc=loc, value=(val.upper() in ("TRUE", "1")))
                type_annot = A.PrimType(loc=loc, name="bool")
            elif "JSON" in typ_upper:
                expr = A.Call(
                    loc=loc,
                    callee=A.Ident(loc=loc, name="json::parse"),
                    args=[A.TextLit(loc=loc, value=val, is_raw=True)],
                )
                type_annot = A.PrimType(loc=loc, name="json")
            elif "DATE" in typ_upper and "TIMESTAMP" not in typ_upper:
                # TO_DATE with the same fixed format we used for capture
                expr = A.SqlBlock(
                    loc=loc,
                    sql=f"select to_date('{val}', 'YYYY-MM-DD HH24:MI:SS') from dual",
                )
                type_annot = A.PrimType(loc=loc, name="date")
            elif "TIMESTAMP" in typ_upper:
                expr = A.SqlBlock(
                    loc=loc,
                    sql=f"select to_timestamp('{val}', 'YYYY-MM-DD HH24:MI:SS.FF9') from dual",
                )
                type_annot = A.PrimType(loc=loc, name="timestamp")
            elif "RAW" in typ_upper:
                expr = A.SqlBlock(
                    loc=loc,
                    sql=f"select hextoraw('{val}') from dual",
                )
                type_annot = A.PrimType(loc=loc, name="bytes")
            else:
                # Default: text / varchar2 / clob
                expr = A.TextLit(loc=loc, value=val, is_raw=True)
                if "CLOB" in typ_upper:
                    type_annot = A.PrimType(loc=loc, name="bigtext")
                else:
                    type_annot = A.PrimType(loc=loc, name="text")
            stmts.append(A.LetStmt(loc=loc, name=name, type_annot=type_annot, value=expr))
        return stmts

    def _absorb(self, item: A.Item) -> None:
        name = getattr(item, "name", None)
        if name:
            self.items = [
                existing for existing in self.items
                if getattr(existing, "name", None) != name
                or type(existing) is not type(item)
            ]
        self.items.append(item)

    # -- slash commands ----------------------------------------------------

    def _handle_slash(self, line: str) -> Optional[str]:
        parts = line.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("\\help", "\\h", "\\?"):
            print(SLASH_HELP)
            return None
        if cmd in ("\\quit", "\\q", "\\exit"):
            return "quit"
        if cmd == "\\reset":
            self.items.clear()
            self.var_snapshots.clear()
            self.var_types.clear()
            print("  (session cleared — defs + variables)")
            return None
        if cmd == "\\show":
            try:
                replay = self._snapshot_to_stmts()
                block = emit_anon_block(self.items, replay, target=self.target)
            except EmitError as e:
                print(f"  ! {e}", file=sys.stderr)
                return None
            print(block)
            return None
        if cmd == "\\save":
            if not arg:
                print("  usage: \\save <file>", file=sys.stderr)
                return None
            self._save(arg)
            return None
        if cmd == "\\load":
            if not arg:
                print("  usage: \\load <file>", file=sys.stderr)
                return None
            self._load(arg)
            return None
        if cmd == "\\sql":
            if not arg.strip():
                print("  usage: \\sql <statement>", file=sys.stderr)
                return None
            self._run_raw_sql(arg)
            return None
        if cmd == "\\connect":
            self._reconnect(arg.strip() or None)
            return None
        print(f"  unknown command: {cmd}  (try \\help)", file=sys.stderr)
        return None

    def _save(self, path: str) -> None:
        # Re-render the session as pell source. Since we don't have an
        # un-emit pass, the most faithful save is the original sources
        # joined back together — but we don't keep those around. Punt to
        # writing a synthesized module file by emitting the items via the
        # parser-friendly textual form is non-trivial; for now, just dump
        # the running anon block (the user can edit by hand).
        try:
            block = emit_anon_block(self.items, [], target=self.target)
        except EmitError as e:
            print(f"  ! {e}", file=sys.stderr)
            return
        Path(path).write_text(block)
        print(f"  saved → {path}  (as PL/SQL; pell-source save TBD)")

    def _load(self, path: str) -> None:
        try:
            src = Path(path).read_text()
        except OSError as e:
            print(f"  ! {e}", file=sys.stderr)
            return
        try:
            new_items, stmts = parse_cell(src, path)
        except (LexError, ParseError) as e:
            print(f"  ! parse error: {e}", file=sys.stderr)
            return
        if stmts:
            print(f"  (ignoring {len(stmts)} top-level statements from {path})")
        for item in new_items:
            self._absorb(item)
        print(f"  loaded {len(new_items)} defs from {path}")

    def _run_raw_sql(self, sql: str) -> None:
        if self.conn is None:
            print("  ! no connection", file=sys.stderr)
            return
        sql = sql.rstrip().rstrip(";")
        head = sql.split(None, 1)[0].lower() if sql.strip() else ""
        try:
            if head == "select":
                rows = self.conn.run_query(sql)
                _print_rows(rows)
            else:
                with self.conn.raw.cursor() as cur:
                    cur.execute(sql)
                    rc = cur.rowcount
                self.conn.raw.commit()
                print(f"  ok ({rc} row{'s' if rc != 1 else ''})")
        except Exception as e:
            print(f"  ! {e}", file=sys.stderr)

    def _reconnect(self, dsn: Optional[str]) -> None:
        try:
            new_conn = connect(dsn)
        except Exception as e:
            print(f"  ! connection failed: {e}", file=sys.stderr)
            return
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = new_conn
        print("  connected")


def _print_rows(rows: list[dict]) -> None:
    if not rows:
        print("  (no rows)")
        return
    cols = list(rows[0].keys())
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    header = " | ".join(c.ljust(w) for c, w in zip(cols, widths))
    sep = "-+-".join("-" * w for w in widths)
    print(header)
    print(sep)
    for r in rows:
        print(" | ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print(f"  ({len(rows)} row{'s' if len(rows) != 1 else ''})")


def run_repl(dsn: Optional[str], target: str = "23") -> int:
    """Entry point for `pell repl`."""
    conn: Optional[Connection] = None
    if dsn or "PELL_DB_URL" in __import__("os").environ:
        try:
            conn = connect(dsn)
        except Exception as e:
            print(f"pell: warning: could not connect: {e}", file=sys.stderr)
            print("  starting REPL without a connection. Use \\connect to attach.",
                  file=sys.stderr)
    return Repl(conn=conn, target=target).run()
