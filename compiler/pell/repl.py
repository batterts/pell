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
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
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
  \\exec <block>    run a PL/SQL anon block (BEGIN ... END;) and
                   print DBMS_OUTPUT — multi-line, blank line or `/` submits
  \\reset           clear all accumulated defs + variables
  \\show            print the anon block that would run on the next cell
  \\vars            list all persisted variables with types and values
  \\defs            list all accumulated definitions (fn, record, error, ...)
  \\state           show both \\defs and \\vars at once
  \\connect <dsn>   reconnect to user/pass@host:port/service
  \\help            this message
  \\quit            exit
"""


def scan_project_signatures(root: Optional[Path] = None) -> dict[str, A.TypeRef]:
    """Walk `root` (default: cwd) for *.pell files and build a
    `<pkg>::<fn>` → return-type map of pub fn signatures.

    Also includes the pell repo's runtime/ dir (where stdlib modules
    like `catalog`, `pell_re` live) so callers get type inference for
    deployed stdlib calls without needing the source in their project.

    For dotted modules like `pell_test.hello`, registers both the
    fully-qualified form (`pell_test::hello::fn`) AND the short form
    (`hello::fn`) — the short form lets callers connected to that
    schema use the natural un-prefixed call.

    Best-effort: parse errors on individual files don't stop the scan.
    """
    sigs: dict[str, A.TypeRef] = {}
    seen_dirs: set[Path] = set()

    def _scan(base: Path) -> None:
        if not base.is_dir() or base.resolve() in seen_dirs:
            return
        seen_dirs.add(base.resolve())
        for path in base.rglob("*.pell"):
            if any(p in ("built", "out", ".git", "node_modules", "expected")
                   for p in path.parts):
                continue
            try:
                src = path.read_text(encoding="utf-8")
                from .parser import parse
                mod = parse(src, str(path))
            except Exception:
                continue
            fq_pkg = mod.name.replace(".", "::")
            short_pkg = mod.name.split(".")[-1] if "." in mod.name else mod.name
            for item in mod.items:
                if not (isinstance(item, A.FnDef) and item.is_pub):
                    continue
                # Procedures (no `-> T`) get a synthesized Unit type so
                # they still show up in completion menus and type-inference
                # lookups (where the receiver code treats Unit specially
                # for "discard the result" checks).
                ret = item.return_type or A.PrimType(loc=item.loc, name="Unit")
                sigs[f"{fq_pkg}::{item.name}"] = ret
                if short_pkg != fq_pkg:
                    sigs[f"{short_pkg}::{item.name}"] = ret

    _scan(root or Path.cwd())
    # The pell repo's runtime/ dir, sibling to this package. Walks
    # `<repo>/compiler/pell/repl.py` → `<repo>/compiler/runtime/`.
    runtime = Path(__file__).resolve().parent.parent / "runtime"
    _scan(runtime)
    return sigs


def scan_project_records(root: Optional[Path] = None) -> dict[str, list]:
    """Walk `root` (default: cwd) + pell runtime dir for *.pell files
    and collect every pub record's field list. Returns
    `<RecordName>` → `list[FieldDef]`.

    Used by the REPL completer for type-aware `.` completion: when
    you're typing `i.<tab>` and `i` is the loop variable of a fn
    returning `list<ColumnInfo>`, we look up ColumnInfo's fields
    here and offer them as suggestions.
    """
    records: dict[str, list] = {}
    seen_dirs: set[Path] = set()

    def _scan(base: Path) -> None:
        if not base.is_dir() or base.resolve() in seen_dirs:
            return
        seen_dirs.add(base.resolve())
        for path in base.rglob("*.pell"):
            if any(p in ("built", "out", ".git", "node_modules", "expected")
                   for p in path.parts):
                continue
            try:
                src = path.read_text(encoding="utf-8")
                from .parser import parse
                mod = parse(src, str(path))
            except Exception:
                continue
            for item in mod.items:
                if isinstance(item, A.RecordDef):
                    records[item.name] = list(item.fields)

    _scan(root or Path.cwd())
    runtime = Path(__file__).resolve().parent.parent / "runtime"
    _scan(runtime)
    return records


class PellCompleter(Completer):
    """Tab-completion for the REPL.

    Three contexts are recognized:

      1. After `<pkg>::` (or `<pkg>::<partial>`) — suggest function
         names from the project signature registry. Most useful case.
         e.g. `catalog::<tab>` → list_tables, columns_of, errors_for, …

      2. Bare identifier with no `::` — suggest pell keywords,
         in-scope variables, accumulated def names (fn / record /
         error), and known package prefixes (suffixed with `::`).

      3. After `\\` at start of line — slash commands (\\help, \\sql,
         \\reset, etc.).

    Stays out of the way in string literals and comments — those
    typically don't have meaningful pell completions.
    """

    _KEYWORDS = frozenset({
        "fn", "pub", "let", "mut", "record", "type", "error", "enum",
        "sealed", "aggregate", "if", "else", "for", "in", "while",
        "return", "match", "true", "false", "null", "import", "as",
        "module", "Ok", "Err", "Some", "None",
    })
    _SLASH_COMMANDS = (
        "\\help", "\\quit", "\\reset", "\\show", "\\vars", "\\defs",
        "\\state", "\\load ", "\\save ", "\\sql ", "\\exec ", "\\connect ",
    )

    def __init__(self, repl: "Repl") -> None:
        self._repl = repl

    def _resolve_record_fields(self, receiver: str, text_so_far: str) -> Optional[list]:
        """Resolve `receiver` to a record's field list, or None.

        Search order:
          1. Loop scope — `for <receiver> in <call>(...)` somewhere
             earlier in the cell. Look up the call's return type in
             the project signature registry; if it's `list<RecordName>`,
             find RecordName's fields.
          2. Let-stmt scope — `let <receiver>: RecordName = ...` or
             `let <receiver> = pkg::fn(...)` where fn returns a record.
          3. Snapshot scope — record vars persisted from prior cells.

        Returns the list of FieldDef on hit, None on miss.
        """
        import re as _re
        # 1. for-loop scope
        for_pat = _re.compile(
            rf"\bfor\s+{_re.escape(receiver)}\s+in\s+([A-Za-z_][\w:]*)\s*\(",
        )
        for m in for_pat.finditer(text_so_far):
            call_name = m.group(1)
            ret = self._repl._project_signatures.get(call_name)
            if ret is None:
                continue
            rec_name = self._extract_list_record_name(ret)
            if rec_name and rec_name in self._repl._project_records:
                return self._repl._project_records[rec_name]
            if rec_name:
                # Also check user-defined records in the running session
                for item in self._repl.items:
                    if isinstance(item, A.RecordDef) and item.name == rec_name:
                        return list(item.fields)

        # 2. let-stmt with explicit record annotation
        let_typed_pat = _re.compile(
            rf"\blet\s+{_re.escape(receiver)}\s*:\s*([A-Za-z_]\w*)\s*=",
        )
        for m in let_typed_pat.finditer(text_so_far):
            rec_name = m.group(1)
            if rec_name in self._repl._project_records:
                return self._repl._project_records[rec_name]
            for item in self._repl.items:
                if isinstance(item, A.RecordDef) and item.name == rec_name:
                    return list(item.fields)

        # 3. let-stmt with inferred type from cross-pkg call
        let_inferred_pat = _re.compile(
            rf"\blet\s+{_re.escape(receiver)}\s*=\s*([A-Za-z_][\w:]*)\s*\(",
        )
        for m in let_inferred_pat.finditer(text_so_far):
            call_name = m.group(1)
            ret = self._repl._project_signatures.get(call_name)
            if ret is None:
                continue
            # Scalar record return: `-> RecordName`
            if isinstance(ret, A.NamedType) and ret.name in self._repl._project_records:
                return self._repl._project_records[ret.name]

        # 4. Snapshot scope (record persisted from prior cells)
        rec_name = self._repl._var_record_names.get(receiver)
        if rec_name is not None and rec_name in self._repl._project_records:
            return self._repl._project_records[rec_name]

        return None

    @staticmethod
    def _extract_list_record_name(ret: A.TypeRef) -> Optional[str]:
        """If `ret` is `list<RecordName>`, return `RecordName`. Else None."""
        if (isinstance(ret, A.GenericType) and ret.base == "list"
                and len(ret.params) == 1
                and isinstance(ret.params[0], A.NamedType)):
            return ret.params[0].name
        return None

    @staticmethod
    def _render_type_for_display(t: A.TypeRef) -> str:
        """Compact type rendering for completion popups."""
        if isinstance(t, A.PrimType):
            return t.name
        if isinstance(t, A.NamedType):
            return t.name
        if isinstance(t, A.GenericType):
            inner = ", ".join(
                PellCompleter._render_type_for_display(p) for p in t.params
            )
            return f"{t.base}<{inner}>"
        return "?"

    def get_completions(self, document: Document, complete_event):
        line = document.current_line_before_cursor
        text_so_far = document.text_before_cursor
        import re as _re

        # Slash commands — only at the start of a line.
        if document.cursor_position_col > 0 and line.startswith("\\"):
            for cmd in self._SLASH_COMMANDS:
                if cmd.startswith(line):
                    yield Completion(cmd, start_position=-len(line))
            return

        # Field-access completion: `<ident>.<partial>` where we can
        # resolve <ident> to a record-typed variable. The receiver
        # must be a bare ident — chained `pkg::fn().field` style isn't
        # yet recognized. The `(?<![:.])` lookbehind keeps us out of
        # `pkg::` and `obj.member.` contexts.
        # Returns unconditionally when we match the `<ident>.<partial>`
        # shape: even if we can't resolve fields (e.g. ident is a
        # primitive-typed loop var), falling through to the bare-
        # identifier path here would surface keywords / packages /
        # vars in a `.foo` position where they make no sense.
        mfield = _re.search(r"(?<![:\.])([A-Za-z_]\w*)\.(\w*)$", line)
        if mfield is not None:
            recv = mfield.group(1)
            partial = mfield.group(2)
            fields = self._resolve_record_fields(recv, text_so_far)
            if fields is not None:
                for f in fields:
                    if f.name.startswith(partial):
                        type_label = self._render_type_for_display(f.type_ref)
                        yield Completion(
                            f.name,
                            start_position=-len(partial),
                            display=f"{f.name}: {type_label}",
                        )
            return

        # `<pkg>::<partial>` — most useful case.
        m = _re.search(r"([A-Za-z_]\w*)::(\w*)$", line)
        if m is not None:
            pkg = m.group(1)
            partial = m.group(2)
            seen: set[str] = set()
            for key in self._repl._project_signatures.keys():
                if not key.startswith(f"{pkg}::"):
                    continue
                fn_name = key.split("::", 1)[1]
                if "::" in fn_name:
                    continue  # skip deeper-namespaced entries
                if not fn_name.startswith(partial):
                    continue
                if fn_name in seen:
                    continue
                seen.add(fn_name)
                # Add `(` so the user lands inside the arg list. We
                # could compute the right paren too but bare `(` is
                # less surprising when the fn takes args.
                yield Completion(
                    fn_name + "(",
                    start_position=-len(partial),
                    display=fn_name + "(...)",
                )
            return

        # Bare identifier — match the partial word the user is typing.
        m = _re.search(r"([A-Za-z_]\w*)$", line)
        partial = m.group(1) if m else ""

        # Collect candidates: keywords + accumulated def names +
        # known package prefixes (suggested with the `::` already
        # appended so the user can chain).
        candidates: list[tuple[str, str]] = []  # (insert, display)
        for kw in self._KEYWORDS:
            candidates.append((kw, kw))
        for item in self._repl.items:
            name = getattr(item, "name", None)
            if name is None:
                continue
            kind = type(item).__name__.replace("Def", "").lower()
            candidates.append((name, f"{name}  ({kind})"))
        # Package prefixes from the signature registry (one per
        # distinct pkg). For dotted names like `pell_test::hello`,
        # also offer the short form (`hello`).
        pkgs: set[str] = set()
        for key in self._repl._project_signatures.keys():
            head = key.split("::", 1)[0]
            pkgs.add(head)
            # Tail (last segment) — `pell_test::hello::fn` → `hello`
            segs = key.split("::")
            if len(segs) >= 3:
                pkgs.add(segs[-2])
        for pkg in pkgs:
            candidates.append((pkg + "::", f"{pkg}::"))
        # Variables in current snapshot scope
        for var in self._repl.var_snapshots.keys():
            if "." in var:
                continue  # skip record-field keys
            candidates.append((var, f"{var}  (var)"))

        yielded: set[str] = set()
        for insert, display in candidates:
            if not insert.startswith(partial):
                continue
            if insert in yielded:
                continue
            yielded.add(insert)
            yield Completion(
                insert,
                start_position=-len(partial),
                display=display,
            )


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
        # Pell record names for record-typed variables, so we can build
        # StructLit replay nodes: e.g. "alice" → "Employee"
        self._var_record_names: dict[str, str] = {}
        # Cross-package signature registry: "<pkg>::<fn>" → return TypeRef.
        # Built by walking the current project for .pell files and
        # parsing their pub fn signatures. Lets the emitter infer the
        # return type of `hello::user_tables()` without you having to
        # annotate the let, AND lets it declare the local with the
        # foreign collection type (avoids the PLS-00382 nominal type
        # clash on cross-package list returns).
        self._project_signatures: dict[str, A.TypeRef] = {}
        # Record name → field list (for completer's type-aware `.` completion)
        self._project_records: dict[str, list] = {}
        self._scan_project_signatures()
        self.cell_number = 0
        self.history = InMemoryHistory()
        self.session: PromptSession = PromptSession(
            history=self.history,
            completer=PellCompleter(self),
            # Make Tab cycle through the popup instead of inserting
            # a literal tab — that's the muscle memory most REPL users
            # have. (Up/Down works too.)
            complete_while_typing=False,
        )

    def _scan_project_signatures(self) -> None:
        """Populate the project signature + record registries from the cwd."""
        self._project_signatures = scan_project_signatures()
        self._project_records = scan_project_records()
        # The emitter needs the (pkg, name) -> RecordDef form (distinct
        # from the completion-oriented name -> fields map above) so that
        # cross-package record constructors resolve in cells.
        from .emitter import scan_project_records as _emitter_scan_records
        from .emitter import scan_project_modules as _scan_modules
        try:
            self._project_records_emit = _emitter_scan_records()
        except Exception:
            self._project_records_emit = {}
        try:
            self._project_modules = _scan_modules()
        except Exception:
            self._project_modules = {}

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
            # Exception: `\exec` opens a multi-line anon block — terminate on
            # blank line (handled above) or a line that's just `/` (sqlplus
            # muscle memory).
            if len(lines) == 1 and line.lstrip().startswith("\\"):
                head = line.lstrip().split(None, 1)[0]
                if head != "\\exec":
                    break
            elif lines and line.strip() == "/":
                # Bare `/` mid-block also submits.
                lines.pop()
                break
        return "\n".join(lines)

    # -- cell execution ----------------------------------------------------

    _VAR_SENTINEL = "__PELL_REPL_VAR__"
    _NULL_MARKER  = "__PELL_NULL__"

    def _infer_from_registry(self, value: "Optional[A.Expr]") -> Optional[str]:
        """If `value` is a known cross-package call, return its
        PL/SQL return type. Used by capture to know when a var is
        a collection (skip) vs. scalar (serialize)."""
        if value is None:
            return None
        if not isinstance(value, A.Call):
            return None
        if not (isinstance(value.callee, A.Ident) and "::" in value.callee.name):
            return None
        ret = self._project_signatures.get(value.callee.name)
        if ret is None:
            return None
        # For list returns, return the foreign type spelling. The
        # capture code checks for "T_" prefix to skip — but foreign
        # types like "hello.t_text_list" don't start with T_. Synthesize
        # the canonical form for the capture skip check.
        if (isinstance(ret, A.GenericType) and ret.base == "list"
                and len(ret.params) == 1):
            from .emitter import _render_type
            elem = _render_type(ret.params[0])
            return f"T_{elem.upper()}_LIST"
        from .emitter import lower_type
        return lower_type(ret)

    def _warn_ephemeral_lets(self, cell_lets: "list[A.LetStmt]") -> None:
        """For lets whose type can't round-trip through capture (lists,
        cursors, OBJECT types), print a one-line note so the user knows
        the variable is single-cell only — and won't be confused by an
        opaque PL/SQL error in the next cell.
        """
        for s in cell_lets:
            typ = self.var_types.get(s.name, "").upper()
            if not typ:
                continue
            if (typ.startswith("T_") and typ.endswith("_LIST")) or "SYS_REFCURSOR" in typ:
                surface = "list" if "_LIST" in typ else "cursor"
                print(
                    f"  (note: `{s.name}` is a {surface} — won't persist to next cell yet, "
                    f"declare in the same cell as you use it)",
                    file=sys.stderr,
                )

    _ORA_LINE_RE = __import__("re").compile(r"line (\d+), column (\d+)")

    def _print_runtime_error(self, err_msg: str, block: str) -> None:
        """Print a runtime error with the offending line from the
        emitted block, annotated with a caret pointing at the column.

        Oracle's `line N, column M` references the position inside the
        anonymous block we sent — but the user can't see that block,
        so the line/col is meaningless without context. This pulls the
        line out and prints it inline."""
        print(f"  ! runtime error: {err_msg}", file=sys.stderr)
        # Parse the first line/col reference from the error.
        m = self._ORA_LINE_RE.search(err_msg)
        if m is None:
            return
        line_no = int(m.group(1))
        col_no = int(m.group(2))
        lines = block.splitlines()
        if line_no <= 0 or line_no > len(lines):
            return
        # Show a 1-line window with a caret. Could expand to a 3-line
        # window if we ever want more context.
        offending = lines[line_no - 1]
        print(f"\n  in the emitted block:", file=sys.stderr)
        print(f"  {line_no:4} | {offending}", file=sys.stderr)
        print(f"  {'':4} | {' ' * max(0, col_no - 1)}^", file=sys.stderr)

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
                emit_anon_block(self.items, [], target=self.target,
                                project_signatures=self._project_signatures,
                                project_records=getattr(self, '_project_records_emit', None),
                                project_modules=getattr(self, '_project_modules', None))
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
                                    source_path=f"<cell {self.cell_number}>",
                                    project_signatures=self._project_signatures,
                                project_records=getattr(self, '_project_records_emit', None),
                                project_modules=getattr(self, '_project_modules', None))
        except EmitError as e:
            print(f"  ! compile error: {e}", file=sys.stderr)
            return

        # Inject capture lines into the anonymous block so we can read
        # back variable values after execution. We capture EVERY base
        # variable from this cell + every base from prior snapshots.
        # The capture function expands record types into per-field
        # capture lines itself, so we ONLY pass base names (no dotted
        # keys) to avoid double-capture.
        cell_lets = [s for s in stmts if isinstance(s, A.LetStmt)]
        all_var_names: list[str] = []
        for k in self.var_snapshots.keys():
            base = k.split(".", 1)[0]
            if base not in all_var_names:
                all_var_names.append(base)
        for s in cell_lets:
            if s.name not in all_var_names:
                all_var_names.append(s.name)
            if s.type_annot:
                from .emitter import lower_type
                self.var_types[s.name] = lower_type(s.type_annot)
                if isinstance(s.type_annot, A.NamedType):
                    self._var_record_names[s.name] = s.type_annot.name
            else:
                inferred = self._infer_from_registry(s.value)
                if inferred is not None:
                    self.var_types[s.name] = inferred

        block = self._inject_capture_lines(block, all_var_names)

        try:
            output = self.conn.run_block(block)
        except Exception as e:
            self._print_runtime_error(str(e), block)
            return

        # After a successful run, warn about cell-lets whose type
        # can't be carried into the next cell. Saves the user from
        # the confusing "identifier L_FOO must be declared" error
        # on the next cell.
        self._warn_ephemeral_lets(cell_lets)

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

            # Record types (T_<NAME>) — capture each field individually.
            # Look up the RecordDef from the accumulated items so we
            # know the field names and types.
            if typ.startswith("T_") and not typ.endswith("_LIST"):
                rec = self._find_record_for_type(typ)
                if rec is not None:
                    for f in rec.fields:
                        from .emitter import lower_type
                        field_typ = lower_type(f.type_ref).upper()
                        field_local = f"{local}.{f.name.lower()}"
                        to_text = self._scalar_to_text(field_local, field_typ)
                        capture_lines.append(
                            f"    dbms_output.put_line('{self._VAR_SENTINEL}' "
                            f"|| chr(9) || '{name}.{f.name}' "
                            f"|| chr(9) || NVL({to_text}, '{self._NULL_MARKER}'));"
                        )
                continue

            # Skip non-serializable types we can't handle yet (lists,
            # cursors, OBJECT TYPEs).
            if typ.startswith("T_") or typ.startswith("SYS_REFCURSOR"):
                continue

            to_text = self._scalar_to_text(local, typ)
            capture_lines.append(
                f"    dbms_output.put_line('{self._VAR_SENTINEL}' "
                f"|| chr(9) || '{name}' "
                f"|| chr(9) || NVL({to_text}, '{self._NULL_MARKER}'));"
            )
        if not capture_lines:
            return block
        inject = "\n".join(capture_lines) + "\n"
        # Find the END pell_anon_main; and insert before it.
        marker = "  END pell_anon_main;"
        idx = block.rfind(marker)
        if idx == -1:
            return block  # can't find — emit without capture
        return block[:idx] + inject + block[idx:]

    @staticmethod
    def _scalar_to_text(local_expr: str, typ: str) -> str:
        """Return the PL/SQL expression that serializes `local_expr`
        (a PL/SQL local or field reference) to text, based on its type."""
        if "JSON" in typ:
            return f"JSON_SERIALIZE({local_expr})"
        if "TIMESTAMP" in typ:
            return f"TO_CHAR({local_expr}, 'YYYY-MM-DD HH24:MI:SS.FF9')"
        if "DATE" in typ:
            return f"TO_CHAR({local_expr}, 'YYYY-MM-DD HH24:MI:SS')"
        if "RAW" in typ:
            return f"RAWTOHEX({local_expr})"
        return f"TO_CHAR({local_expr})"

    def _find_record_for_type(self, pl_type: str) -> Optional[A.RecordDef]:
        """Find the RecordDef whose lowered PL/SQL type name matches
        `pl_type`. E.g. `T_EMPLOYEE` matches RecordDef(name='Employee').
        """
        from .emitter import _record_type_name
        for item in self.items:
            if isinstance(item, A.RecordDef):
                if _record_type_name(item.name).upper() == pl_type:
                    return item
        return None

    def _parse_snapshot(self, line: str) -> None:
        """Parse one `__PELL_REPL_VAR__\\tname\\tvalue` line and store
        the snapshot. `name` may be a dotted record field like `alice.id`."""
        parts = line.split("\t", 2)
        if len(parts) < 3:
            return
        name = parts[1]
        raw_value = parts[2]
        if raw_value == self._NULL_MARKER:
            self.var_snapshots.pop(name, None)
        else:
            # For dotted names (record fields), look up the field type
            # from the RecordDef. For scalars, use var_types.
            if "." in name:
                base, field = name.split(".", 1)
                base_typ = self.var_types.get(base, "").upper()
                rec = self._find_record_for_type(base_typ)
                if rec is not None:
                    from .emitter import lower_type
                    fdef = next((f for f in rec.fields if f.name == field), None)
                    typ = lower_type(fdef.type_ref) if fdef else "VARCHAR2(4000)"
                else:
                    typ = "VARCHAR2(4000)"
            else:
                typ = self.var_types.get(name, "VARCHAR2(4000)")
            self.var_snapshots[name] = (typ, raw_value)

    def _scalar_val_to_expr(self, typ: str, val: str) -> A.Expr:
        """Build an AST expression node from a captured scalar value.

        Date / timestamp / bytes use Oracle's TO_DATE / TO_TIMESTAMP /
        HEXTORAW built-ins which are PL/SQL-callable directly — no
        SELECT FROM dual wrapper needed.
        """
        loc = A.Loc("<repl-snapshot>", 0, 0)
        t = typ.upper()
        if "NUMBER" in t or "PLS_INTEGER" in t or "INTEGER" in t:
            return A.NumberLit(loc=loc, value=val)
        if t == "BOOLEAN":
            return A.BoolLit(loc=loc, value=(val.upper() in ("TRUE", "1")))
        if "JSON" in t:
            return A.Call(
                loc=loc,
                callee=A.Ident(loc=loc, name="json::parse"),
                args=[A.TextLit(loc=loc, value=val, is_raw=True)],
            )
        if "DATE" in t and "TIMESTAMP" not in t:
            return self._builtin_call(loc, "to_date", val, "YYYY-MM-DD HH24:MI:SS")
        if "TIMESTAMP" in t:
            return self._builtin_call(loc, "to_timestamp", val, "YYYY-MM-DD HH24:MI:SS.FF9")
        if "RAW" in t:
            return self._builtin_call(loc, "hextoraw", val)
        return A.TextLit(loc=loc, value=val, is_raw=True)

    @staticmethod
    def _builtin_call(loc: A.Loc, fn: str, *text_args: str) -> A.Expr:
        """Build `fn('arg1', 'arg2', ...)` — emits as a direct PL/SQL
        function call (TO_DATE, TO_TIMESTAMP, HEXTORAW, etc.)."""
        return A.Call(
            loc=loc,
            callee=A.Ident(loc=loc, name=fn),
            args=[A.TextLit(loc=loc, value=a, is_raw=True) for a in text_args],
        )

    @staticmethod
    def _scalar_type_annot(typ: str) -> A.TypeRef:
        """Build a pell TypeRef for a PL/SQL type string."""
        loc = A.Loc("<repl-snapshot>", 0, 0)
        t = typ.upper()
        if "NUMBER" in t or "PLS_INTEGER" in t:
            return A.PrimType(loc=loc, name="number")
        if t == "BOOLEAN":
            return A.PrimType(loc=loc, name="bool")
        if "JSON" in t:
            return A.PrimType(loc=loc, name="json")
        if "TIMESTAMP" in t:
            return A.PrimType(loc=loc, name="timestamp")
        if "DATE" in t:
            return A.PrimType(loc=loc, name="date")
        if "RAW" in t:
            return A.PrimType(loc=loc, name="bytes")
        if "CLOB" in t:
            return A.PrimType(loc=loc, name="bigtext")
        return A.PrimType(loc=loc, name="text")

    def _snapshot_to_stmts(self) -> list[A.Stmt]:
        """Build LetStmt nodes from the current snapshots.

        Scalar snapshots (keys without dots) become typed literal lets.
        Record-field snapshots (keys like `alice.id`, `alice.name`) are
        grouped by base name and become a single StructLit let:
            let alice: Employee = Employee { id: 1, name: "Alice", level: 5 };
        """
        loc = A.Loc("<repl-snapshot>", 0, 0)
        stmts: list[A.Stmt] = []
        # Partition snapshots into scalars and record-field groups.
        scalars: dict[str, tuple[str, str]] = {}
        record_fields: dict[str, dict[str, tuple[str, str]]] = {}  # base → {field → (typ, val)}
        for key, (typ, val) in self.var_snapshots.items():
            if "." in key:
                base, field = key.split(".", 1)
                record_fields.setdefault(base, {})[field] = (typ, val)
            else:
                scalars[key] = (typ, val)

        # Scalars — each becomes `let x: type = <literal>;`
        for name, (typ, val) in scalars.items():
            expr = self._scalar_val_to_expr(typ, val)
            type_annot = self._scalar_type_annot(typ)
            stmts.append(A.LetStmt(loc=loc, name=name, type_annot=type_annot, value=expr))

        # Records — each group becomes `let alice: Employee = Employee { ... };`
        for base, fields in record_fields.items():
            rec_name = self._var_record_names.get(base)
            if rec_name is None:
                continue  # can't reconstruct without the pell record name
            field_inits: list[A.FieldInit] = []
            for fname, (ftyp, fval) in fields.items():
                field_inits.append(A.FieldInit(
                    loc=loc,
                    name=fname,
                    value=self._scalar_val_to_expr(ftyp, fval),
                ))
            expr = A.StructLit(loc=loc, type_name=rec_name, fields=field_inits)
            type_annot = A.NamedType(loc=loc, name=rec_name)
            stmts.append(A.LetStmt(loc=loc, name=base, type_annot=type_annot, value=expr))

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

    # -- state display -----------------------------------------------------

    def _show_vars(self) -> None:
        """Print all persisted variables with their types and current values."""
        if not self.var_snapshots:
            print("  (no variables)")
            return
        # Calculate column widths for alignment
        rows: list[tuple[str, str, str]] = []
        for name, (typ, val) in sorted(self.var_snapshots.items()):
            # Truncate long values for display
            display_val = val if len(val) <= 80 else val[:77] + "..."
            rows.append((name, typ, display_val))
        name_w = max(len(r[0]) for r in rows)
        type_w = max(len(r[1]) for r in rows)
        print(f"  {'Name':<{name_w}}  {'Type':<{type_w}}  Value")
        print(f"  {'-' * name_w}  {'-' * type_w}  {'-' * 40}")
        for name, typ, val in rows:
            print(f"  {name:<{name_w}}  {typ:<{type_w}}  {val}")

    @staticmethod
    def _render_type_ref(t) -> str:
        """Render a pell AST type reference as its surface name."""
        if t is None:
            return ""
        if isinstance(t, A.PrimType):
            return t.name
        if isinstance(t, A.NamedType):
            return t.name
        if isinstance(t, A.GenericType):
            inner = ", ".join(Repl._render_type_ref(p) for p in t.params)
            return f"{t.base}<{inner}>"
        if isinstance(t, A.OptionalType):
            return f"Option<{Repl._render_type_ref(t.inner)}>"
        return str(t)

    def _show_defs(self) -> None:
        """Print all accumulated definitions (fn, record, error, etc.)."""
        if not self.items:
            print("  (no definitions)")
            return
        for item in self.items:
            kind = type(item).__name__
            name = getattr(item, "name", "?")
            detail = ""
            if isinstance(item, A.FnDef):
                params = ", ".join(
                    f"{p.name}: {self._render_type_ref(p.type_ref)}"
                    for p in item.params
                ) if item.params else ""
                ret = f" -> {self._render_type_ref(item.return_type)}" if item.return_type else ""
                detail = f"({params}){ret}"
                kind = "fn"
            elif isinstance(item, A.RecordDef):
                fields = ", ".join(
                    f"{f.name}: {self._render_type_ref(f.type_ref)}" for f in item.fields
                )
                detail = f" {{ {fields} }}"
                kind = "record"
            elif isinstance(item, A.ErrorDef):
                fields = ", ".join(
                    f"{f.name}: {self._render_type_ref(f.type_ref)}" for f in item.fields
                )
                detail = f" {{ {fields} }}"
                kind = "error"
            elif hasattr(item, '__class__'):
                kind = kind.replace("Def", "").lower()
            vis = "pub " if item.is_pub else ""
            print(f"  {vis}{kind} {name}{detail}")

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
            self._var_record_names.clear()
            print("  (session cleared — defs + variables)")
            return None
        if cmd == "\\vars":
            self._show_vars()
            return None
        if cmd == "\\defs":
            self._show_defs()
            return None
        if cmd == "\\state":
            self._show_defs()
            if self.items and self.var_snapshots:
                print()
            self._show_vars()
            return None
        if cmd == "\\show":
            try:
                replay = self._snapshot_to_stmts()
                block = emit_anon_block(self.items, replay, target=self.target,
                                        project_signatures=self._project_signatures,
                                project_records=getattr(self, '_project_records_emit', None),
                                project_modules=getattr(self, '_project_modules', None))
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
        if cmd == "\\exec":
            if not arg.strip():
                print("  usage: \\exec BEGIN ... END;   (multi-line ok — "
                      "blank line or `/` submits)", file=sys.stderr)
                return None
            self._run_anon_block(arg)
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
            block = emit_anon_block(self.items, [], target=self.target,
                                project_signatures=self._project_signatures,
                                project_records=getattr(self, '_project_records_emit', None),
                                project_modules=getattr(self, '_project_modules', None))
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
        # Strip the `module <name>;` header if present — parse_cell
        # doesn't accept it (cells are module-less snippets).
        import re as _re
        src = _re.sub(r'(?m)^\s*module\s+[\w.]+\s*;\s*\n?', '', src, count=1)
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
        head = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else ""
        # `\sql BEGIN …` and `\sql DECLARE …` are PL/SQL anon blocks —
        # route to the anon-block path so DBMS_OUTPUT is captured and
        # the trailing `END;` isn't mangled by the SELECT/DML stripper.
        if head in ("begin", "declare"):
            self._run_anon_block(sql)
            return
        sql = sql.rstrip().rstrip(";")
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

    def _run_anon_block(self, sql: str) -> None:
        """Run a PL/SQL anonymous block (BEGIN ... END; or DECLARE ...
        BEGIN ... END;) and print DBMS_OUTPUT. Adds the BEGIN/END wrap
        if the user provided a bare statement list."""
        if self.conn is None:
            print("  ! no connection", file=sys.stderr)
            return
        body = sql.strip()
        # Strip a trailing `/` (sqlplus terminator) — Oracle doesn't
        # accept it inside cursor.execute, but users type it from habit.
        if body.endswith("/"):
            body = body[:-1].rstrip()
        head = body.split(None, 1)[0].lower() if body else ""
        if head not in ("begin", "declare"):
            # Wrap bare statements as anon block. Ensure terminator.
            if not body.rstrip().endswith(";"):
                body += ";"
            body = f"BEGIN {body} END;"
        elif not body.rstrip().endswith(";"):
            body += ";"
        try:
            out_lines = self.conn.run_block(body)
        except Exception as e:
            print(f"  ! {e}", file=sys.stderr)
            return
        for ln in out_lines:
            print(ln)
        if not out_lines:
            print("  ok")

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
