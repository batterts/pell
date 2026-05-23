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

    def _run_cell(self, source: str) -> None:
        try:
            new_items, stmts = parse_cell(source, f"<cell {self.cell_number}>")
        except (LexError, ParseError) as e:
            print(f"  ! parse error: {e}", file=sys.stderr)
            return
        # Merge new defs into the running session, replacing any prior def
        # with the same name (so a user can iterate on a fn definition).
        for item in new_items:
            self._absorb(item)
        if not stmts:
            # Pure-def cell — recompile to validate the new defs, then exit.
            try:
                emit_anon_block(self.items, [], target=self.target)
            except EmitError as e:
                print(f"  ! compile error: {e}", file=sys.stderr)
                return
            return
        if self.conn is None:
            print("  ! no connection — use \\connect to attach to a database",
                  file=sys.stderr)
            return
        try:
            block = emit_anon_block(self.items, stmts, target=self.target,
                                    source_path=f"<cell {self.cell_number}>")
        except EmitError as e:
            print(f"  ! compile error: {e}", file=sys.stderr)
            return
        try:
            output = self.conn.run_block(block)
        except Exception as e:
            print(f"  ! runtime error: {e}", file=sys.stderr)
            return
        for ln in output:
            print(ln)

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
            print("  (session cleared)")
            return None
        if cmd == "\\show":
            try:
                block = emit_anon_block(self.items, [], target=self.target)
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
