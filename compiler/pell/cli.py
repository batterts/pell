"""pell CLI — v0.

Usage:
    pell build <file.pell>            -> emits to stdout
    pell build <file.pell> -o <out>   -> writes to file
    pell build <dir>                  -> compiles every .pell file in the dir
    pell parse <file.pell>            -> prints AST
    pell tokens <file.pell>           -> prints token stream
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .emitter import emit, emit_anon_block, EmitError
from .lexer import tokenize
from .parser import parse, parse_cell, ParseError
from .lexer import LexError


def cmd_build(args: argparse.Namespace) -> int:
    inputs = _collect_inputs(args.input)
    if not inputs:
        print(f"pell: no .pell files found in {args.input!r}", file=sys.stderr)
        return 2
    if args.output and len(inputs) > 1:
        print("pell: -o requires a single input file (or a directory output)", file=sys.stderr)
        return 2
    # Scan sibling .pell files for cross-package context — pub fn
    # signatures (so call-site type inference works) and pub records
    # (so `pkg::Record { … }` struct lits lower correctly). Best-effort:
    # any failure falls back to local-only resolution.
    try:
        from .repl import scan_project_signatures
        from .emitter import scan_project_records as _scan_records
        from .emitter import scan_project_modules as _scan_modules
        project_signatures = scan_project_signatures()
        project_records = _scan_records()
        project_modules = _scan_modules()
    except Exception:
        project_signatures = {}
        project_records = {}
        project_modules = {}
    failures = 0
    for src_path in inputs:
        try:
            src = src_path.read_text()
            module = parse(src, str(src_path))
            sql = emit(module, target=args.target,
                       source_text=src, source_path=str(src_path),
                       reproducible=getattr(args, "reproducible", False),
                       debug=getattr(args, "debug", False),
                       trace=getattr(args, "trace", False),
                       project_signatures=project_signatures,
                       project_records=project_records,
                       project_modules=project_modules)
        except (LexError, ParseError, EmitError) as e:
            print(f"pell: {e}", file=sys.stderr)
            failures += 1
            continue
        except ValueError as e:
            print(f"pell: {e}", file=sys.stderr)
            return 2
        # `@stub` modules emit nothing — signature-only, never deployed.
        # Skip the destination write entirely (don't litter empty .sql files).
        if not sql.strip():
            print(f"  ({src_path.name}: @stub module — no SQL emitted)")
            continue
        # destination
        if args.output:
            out_path = Path(args.output)
        elif args.dir_output:
            out_path = Path(args.dir_output) / (src_path.stem + ".sql")
        else:
            out_path = None
        if out_path is None:
            print(sql)
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(sql)
            print(f"  → {out_path}")
    return 1 if failures else 0


def cmd_parse(args: argparse.Namespace) -> int:
    path = Path(args.input)
    src = path.read_text()
    try:
        m = parse(src, str(path))
    except (LexError, ParseError) as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    _dump_ast(m)
    return 0


def cmd_runtime(args: argparse.Namespace) -> int:
    """Aggregate all error declarations from a project into a pell_runtime.sql."""
    from . import ast as A
    inputs = _collect_inputs(args.input)
    if not inputs:
        print(f"pell: no .pell files found in {args.input!r}", file=sys.stderr)
        return 2
    # gather (full-mangled-module-name, error_name, category) tuples.
    # We use the full mangled name (with schema prefix when present) so
    # `hr.employees::NotFound` and `acct.employees::NotFound` get distinct
    # global identifiers `hr_employees_notfound` vs `acct_employees_notfound`.
    errors: list[tuple[str, str, str]] = []
    failures = 0
    for src_path in inputs:
        try:
            module = parse(src_path.read_text(), str(src_path))
        except (LexError, ParseError) as e:
            print(f"pell: {e}", file=sys.stderr)
            failures += 1
            continue
        # Schema-qualified package name (mangled) keeps the pell_runtime
        # exception identifiers globally unique across schemas.
        full = module.qualified_name.replace(".", "_")
        for item in module.items:
            if isinstance(item, A.ErrorDef):
                errors.append((full, item.name, getattr(item, "category", "propagate")))
    # emit a merged pell_runtime.sql
    code_start = 20100
    lines = [
        "-- pell_runtime — merged across the project",
        "-- Generated by `pell runtime`; do not edit by hand.",
        "",
        "CREATE OR REPLACE CONTEXT pell_err USING pell_runtime;",
        "/",
        "",
        "CREATE OR REPLACE PACKAGE pell_runtime AS",
        "  PROCEDURE set_err(p_key IN VARCHAR2, p_payload IN VARCHAR2);",
        "  PROCEDURE clear_err(p_key IN VARCHAR2);",
        "  FUNCTION  get_err(p_key IN VARCHAR2) RETURN VARCHAR2;",
        "",
    ]
    # Per-category SQLCODE counters — must mirror the per-module emitter's
    # ranges so single-module installs and project-wide pell_runtime agree.
    # propagate -20100..-20199 / skip -20200..-20299 / panic -20300..-20399.
    bases = {"propagate": 20100, "skip": 20200, "panic": 20300}
    counters = {"propagate": 0, "skip": 0, "panic": 0}
    for pkg, err, cat in errors:
        exn = f"{pkg}_{err}".lower()
        code = -(bases[cat] + counters[cat])
        counters[cat] += 1
        lines.append(f"  {exn} EXCEPTION;  -- {cat}")
        lines.append(f"  PRAGMA EXCEPTION_INIT({exn}, {code});")
    lines += [
        "END pell_runtime;",
        "/",
        "",
        "CREATE OR REPLACE PACKAGE BODY pell_runtime AS",
        "  PROCEDURE set_err(p_key IN VARCHAR2, p_payload IN VARCHAR2) IS",
        "  BEGIN",
        "    DBMS_SESSION.SET_CONTEXT('pell_err', p_key, p_payload);",
        "  END set_err;",
        "",
        "  PROCEDURE clear_err(p_key IN VARCHAR2) IS",
        "  BEGIN",
        "    DBMS_SESSION.CLEAR_CONTEXT('pell_err', p_key);",
        "  END clear_err;",
        "",
        "  FUNCTION get_err(p_key IN VARCHAR2) RETURN VARCHAR2 IS",
        "  BEGIN",
        "    RETURN SYS_CONTEXT('pell_err', p_key);",
        "  END get_err;",
        "END pell_runtime;",
        "/",
    ]
    output = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output)
        print(f"  → {args.output} ({len(errors)} error variants)")
    else:
        print(output)
    return 1 if failures else 0


def cmd_tokens(args: argparse.Namespace) -> int:
    path = Path(args.input)
    src = path.read_text()
    try:
        for t in tokenize(src, str(path)):
            print(t)
    except LexError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------


def _collect_inputs(input_arg: str) -> list[Path]:
    """Collect .pell files from a path. Directories are walked
    recursively, but a few well-known noise directories are skipped
    (.git, .venv, node_modules, build, out, expected, __pycache__).
    """
    skip_dirs = {".git", ".venv", "node_modules", "build", "out",
                 "expected", "__pycache__", "target"}
    p = Path(input_arg)
    if p.is_file():
        return [p]
    if not p.is_dir():
        return []
    out: list[Path] = []
    for path in p.rglob("*.pell"):
        if any(part in skip_dirs for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


def _dump_ast(node, depth: int = 0) -> None:
    """Crude AST pretty-printer."""
    from dataclasses import is_dataclass, fields
    pad = "  " * depth
    if is_dataclass(node):
        cls = type(node).__name__
        print(f"{pad}{cls}")
        for f in fields(node):
            if f.name == "loc":
                continue
            v = getattr(node, f.name)
            print(f"{pad}  {f.name}:", end="")
            if isinstance(v, list):
                print()
                for item in v:
                    _dump_ast(item, depth + 2)
            elif is_dataclass(v):
                print()
                _dump_ast(v, depth + 2)
            else:
                print(f" {v!r}")
    else:
        print(f"{pad}{node!r}")


def cmd_deploy(args: argparse.Namespace) -> int:
    """Build every .pell in <input> and install the resulting .sql files
    against PELL_DB_URL in the correct order:

      1. pell_runtime.sql (if any module declares pell errors)
      2. pell_re.sql      (if --with-re given, or any module uses re::)
      3. each module .sql (in alphabetical order; cross-module deps are
                           the user's problem — pell doesn't infer them)

    Emits progress per file; on the first failure, reports the offending
    statement and bails (unless --keep-going).
    """
    import re as _re_mod

    project = Path(args.input).resolve()
    inputs = _collect_inputs(args.input)
    if not inputs:
        print(f"pell: no .pell files found in {args.input!r}", file=sys.stderr)
        return 2

    # Build everything first to catch compile errors before connecting.
    # Default output dir: `<project>/plsql`. The legacy `built/` name
    # implied "ephemeral, gitignore me" — but pell's emitted SQL is a
    # human-readable, SHA-anchored deployment artifact you commit. New
    # default reflects that intent. Existing projects that still use
    # `built/` keep working by passing `--out-dir built`.
    default_root = project if project.is_dir() else project.parent
    out_dir = Path(args.out_dir) if args.out_dir else (default_root / "plsql")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"pell deploy: build → {out_dir}")
    # Same cross-package scan as cmd_build so deploys handle
    # `pkg::Record { … }` and cross-pkg fn type inference correctly.
    try:
        from .repl import scan_project_signatures
        from .emitter import scan_project_records as _scan_records
        from .emitter import scan_project_modules as _scan_modules
        project_signatures = scan_project_signatures()
        project_records = _scan_records()
        project_modules = _scan_modules()
    except Exception:
        project_signatures = {}
        project_records = {}
        project_modules = {}
    built_files: list[Path] = []
    uses_re = False
    uses_logger = False
    has_errors = False
    failures = 0
    for src_path in inputs:
        try:
            src = src_path.read_text()
            module = parse(src, str(src_path))
            sql = emit(module, target=args.target, source_text=src,
                       source_path=str(src_path), reproducible=args.reproducible,
                       debug=getattr(args, "debug", False),
                       trace=getattr(args, "trace", False),
                       project_signatures=project_signatures,
                       project_records=project_records,
                       project_modules=project_modules)
        except (LexError, ParseError, EmitError) as e:
            print(f"  ✗ {src_path.name}: {e}", file=sys.stderr)
            failures += 1
            if not args.keep_going:
                return 1
            continue
        # @stub modules emit empty SQL — never deploy them (they'd clobber
        # the real Oracle SYS package they describe).
        if not sql.strip():
            print(f"  ⊘ {src_path.name}: @stub module — skipped")
            continue
        if "pell_re." in sql:
            uses_re = True
        if "pell_runtime." in sql:
            has_errors = True
        if "logger." in sql:
            uses_logger = True
        out_path = out_dir / (src_path.stem + ".sql")
        out_path.write_text(sql)
        built_files.append(out_path)
        print(f"  ✓ {src_path.name} → {out_path.name}")
    if failures:
        print(f"pell deploy: {failures} build error(s); aborting before install", file=sys.stderr)
        return 1

    if args.build_only:
        return 0

    # Generate pell_runtime.sql if any module declared errors.
    runtime_sql = out_dir / "pell_runtime.sql"
    if has_errors:
        print(f"pell deploy: generating pell_runtime.sql ...")
        # Aggregate from the whole PROJECT directory, even when deploying
        # a single file — pell_runtime is shared; regenerating it from
        # one file would drop every other module's exceptions and
        # invalidate their packages.
        runtime_scope = project if project.is_dir() else project.parent
        runtime_args = argparse.Namespace(input=str(runtime_scope), output=str(runtime_sql))
        cmd_runtime(runtime_args)

    # Locate pell_re.sql if regex is in use. Look first in the repo's
    # compiler/runtime/ dir (canonical), then alongside the built files.
    # __file__ is compiler/pell/cli.py → parents[1] is compiler/.
    canonical_runtime = Path(__file__).resolve().parents[1] / "runtime"
    re_sql: Path | None = None
    if uses_re or args.with_re:
        for candidate in (canonical_runtime / "pell_re.sql",
                          out_dir / "pell_re.sql"):
            if candidate.exists():
                re_sql = candidate
                break

    # Same for logger.sql when any module references logger::*.
    logger_sql: Path | None = None
    if uses_logger:
        for candidate in (canonical_runtime / "logger.sql",
                          out_dir / "logger.sql"):
            if candidate.exists():
                logger_sql = candidate
                break

    # Install — connect via PELL_DB_URL / --connect, then apply each file.
    try:
        from . import driver
    except ImportError as e:
        print(f"pell: `pell deploy` needs the optional driver dependency; "
              f"install with: pip install -e .[repl]\n  ({e})", file=sys.stderr)
        return 2
    try:
        conn = driver.connect(args.connect)
    except Exception as e:
        print(f"pell: connection failed: {e}", file=sys.stderr)
        return 1

    install_order: list[Path] = []
    if has_errors:
        install_order.append(runtime_sql)
    if re_sql is not None:
        install_order.append(re_sql)
    if logger_sql is not None:
        install_order.append(logger_sql)
    install_order.extend(sorted(built_files))

    print(f"pell deploy: install {len(install_order)} file(s) → "
          f"{(args.connect or '$PELL_DB_URL')}")
    deploy_failures = 0
    try:
        if getattr(args, "debug", False):
            # Units must carry PL/SQL debug info for DBMS_DEBUG_JDWP to
            # stop in them (same as SQL Developer's "Compile for Debug").
            with conn.raw.cursor() as _cur:
                _cur.execute("ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 1")
                _cur.execute("ALTER SESSION SET PLSQL_DEBUG = TRUE")
            print("  (debug build: PLSQL_DEBUG=TRUE, OPTIMIZE_LEVEL=1)")
        for path in install_order:
            try:
                conn.execute_install(path.read_text())
                print(f"  ✓ {path.name}")
                # Shared runtime packages are referenced unqualified from
                # packages deployed into OTHER schemas (via the public
                # synonyms the setup script creates) — make them callable.
                stem = path.stem
                if stem in ("pell_runtime", "logger", "pell_re"):
                    try:
                        with conn.raw.cursor() as _c:
                            _c.execute(f"GRANT EXECUTE ON {stem} TO PUBLIC")
                    except Exception:
                        pass  # single-schema setups don't need it
            except Exception as e:
                deploy_failures += 1
                print(f"  ✗ {path.name}: {e}", file=sys.stderr)
                if not args.keep_going:
                    return 1
        conn.commit()
    finally:
        conn.close()

    return 1 if deploy_failures else 0


def cmd_sql(args: argparse.Namespace) -> int:
    """Run a `.sql` file (typically a pell build output) against the
    connected database. Splits on the SQL*Plus `/` terminator, executes
    each statement in order, and prints any DBMS_OUTPUT a statement
    produced. On any statement failure, prints the position + error
    and stops.
    """
    try:
        source = Path(args.input).read_text()
    except OSError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    try:
        from . import driver
    except ImportError as e:
        print(f"pell: `pell sql` needs the optional driver dependency; "
              f"install with: pip install -e .[repl]\n  ({e})", file=sys.stderr)
        return 2
    try:
        conn = driver.connect(args.connect)
    except Exception as e:
        print(f"pell: connection failed: {e}", file=sys.stderr)
        return 1
    stmts = driver._split_script(source)
    failures = 0
    try:
        for idx, stmt in enumerate(stmts, start=1):
            head = stmt.lstrip().split(None, 1)[0] if stmt.strip() else "<empty>"
            label = f"[{idx}/{len(stmts)}] {head[:60]}"
            try:
                with conn.raw.cursor() as cur:
                    cur.execute(driver._strip_terminator(stmt))
                    # Snapshot cur.warning BEFORE doing anything else on
                    # this cursor — every cur.execute / cur.callproc
                    # (including the ones DBMS_OUTPUT drain uses) resets
                    # the attribute. CREATE OR REPLACE always succeeds
                    # at the SQL level even when the body has compile
                    # errors; the warning carries DPY-7000 / ORA-24344
                    # in that case. We then pull the actual error rows
                    # from USER_ERRORS and surface them so `pell sql`
                    # doesn't report ok on a broken PACKAGE BODY.
                    dirty = cur.warning is not None
                    output = driver._drain_dbms_output(cur)
                    if dirty:
                        obj = driver._parse_create_object(stmt)
                        if obj is not None:
                            name, obj_type, owner = obj
                            errors = driver._user_errors_for(cur, name, obj_type, owner)
                            if not errors:
                                errors = [(0, 0, str(cur.warning))]
                            raise driver.InstallError(name, obj_type, errors)
                print(f"{label}  ok")
                for ln in output:
                    print(f"  {ln}")
            except Exception as e:
                failures += 1
                print(f"{label}  FAIL", file=sys.stderr)
                print(f"  {e}", file=sys.stderr)
                if args.stop_on_error:
                    break
        conn.commit()
    finally:
        conn.close()
    return 1 if failures else 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Rename a pell project's old build-output directory to the new
    default `plsql/`. Idempotent, dry-run by default.

    Existing projects scaffolded by older pell versions have a `built/`
    dir gitignored and never committed. The new convention puts the
    lowered SQL in `plsql/`, expects it committed, and pairs with the
    IntelliJ plugin's per-project `targetDirName` setting.

    Steps:
      1. Rename <root>/<from> → <root>/<to> (if the source exists and
         the target doesn't).
      2. Remove the `<from>/` line from .gitignore.
      3. Print a summary of what changed.

    With --dry-run, prints what *would* happen without touching disk.
    """
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"pell: not a directory: {root}", file=sys.stderr)
        return 2
    src = root / args.from_dir
    dst = root / args.to_dir
    actions: list[str] = []

    if src.is_dir() and not dst.exists():
        actions.append(f"rename: {src} → {dst}")
    elif src.is_dir() and dst.exists():
        actions.append(f"skip:   {src} exists, {dst} also exists "
                       f"(merge manually)")
    elif not src.is_dir():
        actions.append(f"skip:   {src} doesn't exist")

    gitignore = root / ".gitignore"
    new_gitignore: Optional[str] = None
    if gitignore.is_file():
        lines = gitignore.read_text().splitlines()
        kept = [
            l for l in lines
            if l.strip() not in (args.from_dir, f"{args.from_dir}/", f"/{args.from_dir}", f"/{args.from_dir}/")
        ]
        if len(kept) != len(lines):
            actions.append(f"edit:   .gitignore — drop {args.from_dir}/")
            new_gitignore = "\n".join(kept) + ("\n" if kept else "")

    if not actions:
        print("pell migrate: nothing to do")
        return 0

    for a in actions:
        print(f"  {a}")
    if args.dry_run:
        print("\n(dry-run — re-run without --dry-run to apply)")
        return 0

    # Apply
    if src.is_dir() and not dst.exists():
        src.rename(dst)
    if new_gitignore is not None:
        gitignore.write_text(new_gitignore)
    print("\npell migrate: done — commit the rename + .gitignore "
          "change, then `pell deploy` from now on writes to "
          f"{args.to_dir}/")
    return 0


def cmd_repl(args: argparse.Namespace) -> int:
    try:
        from .repl import run_repl
    except ImportError as e:
        print(f"pell: `pell repl` needs the optional repl dependencies; "
              f"install with: pip install -e .[repl]\n  ({e})", file=sys.stderr)
        return 2
    return run_repl(args.connect, target=args.target)


def cmd_exec(args: argparse.Namespace) -> int:
    """Compile a pell cell (file or `-e` snippet) into an anonymous PL/SQL
    block and run it against the connected database."""
    if args.expr is not None and args.input is not None:
        print("pell: pass either a file or -e, not both", file=sys.stderr)
        return 2
    if args.expr is not None:
        source = args.expr
        source_path = "<-e>"
    elif args.input is not None:
        source_path = args.input
        source = Path(args.input).read_text()
    else:
        source = sys.stdin.read()
        source_path = "<stdin>"
    try:
        items, stmts = parse_cell(source, source_path)
    except (LexError, ParseError) as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    if not items and not stmts:
        return 0
    # Scan the cwd for sibling .pell files so cross-package call
    # inference + record resolution + access checks work the same way
    # they do in the REPL and `pell build`.
    try:
        from .repl import scan_project_signatures
        from .emitter import scan_project_records as _scan_records
        from .emitter import scan_project_modules as _scan_modules
        project_signatures = scan_project_signatures()
        project_records = _scan_records()
        project_modules = _scan_modules()
    except Exception:
        project_signatures = {}
        project_records = {}
        project_modules = {}
    try:
        block = emit_anon_block(items, stmts, target=args.target,
                                source_path=source_path,
                                trace=getattr(args, "trace", False),
                                project_signatures=project_signatures,
                                project_records=project_records,
                                project_modules=project_modules)
    except EmitError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(block)
        return 0
    try:
        from . import driver
    except ImportError as e:
        print(f"pell: `pell exec` needs the optional driver dependency; "
              f"install with: pip install -e .[repl]\n  ({e})", file=sys.stderr)
        return 2
    try:
        conn = driver.connect(args.connect)
    except Exception as e:
        print(f"pell: connection failed: {e}", file=sys.stderr)
        return 1
    try:
        lines = conn.run_block(block)
    except Exception as e:
        print(f"pell: execution failed: {e}", file=sys.stderr)
        if args.show_block:
            print(block, file=sys.stderr)
        return 1
    finally:
        conn.close()
    for ln in lines:
        print(ln)
    return 0


# ---------------------------------------------------------------------------
# pell stubgen — connect via PELL_DB_URL, introspect a SYS / user package
# via ALL_ARGUMENTS, and write a `@stub module pkg;` file with pub fn
# declarations matching the database's actual signatures.
# ---------------------------------------------------------------------------


# Oracle DATA_TYPE -> pell surface type. Conservative: unmapped types
# fall through to a `text` placeholder with a TODO comment so the
# generated file parses but flags itself for review.
_ORACLE_TYPE_TO_PELL: dict[str, str] = {
    "VARCHAR2": "text",
    "VARCHAR": "text",
    "NVARCHAR2": "text",
    "CHAR": "text",
    "NCHAR": "text",
    "CLOB": "bigtext",
    "NCLOB": "bigtext",
    "LONG": "text",
    "NUMBER": "number",
    "INTEGER": "number",
    "PLS_INTEGER": "number",
    "BINARY_INTEGER": "number",
    "BINARY_FLOAT": "number",
    "BINARY_DOUBLE": "number",
    "FLOAT": "number",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
    "TIMESTAMP WITH TIME ZONE": "timestamp",
    "TIMESTAMP WITH LOCAL TIME ZONE": "timestamp",
    "BOOLEAN": "bool",
    "PL/SQL BOOLEAN": "bool",
    "RAW": "bytes",
    "LONG RAW": "bytes",
    "BLOB": "bytes",
    "REF CURSOR": "cursor<dyn>",
    "PL/SQL REF CURSOR": "cursor<dyn>",
    "PL/SQL RECORD": "json",  # opaque; user can refine
}


# Pell reserved words — when Oracle's argument name collides, stubgen
# appends an underscore so the file parses.
_PELL_RESERVED = frozenset({
    "module", "import", "pub", "fn", "let", "var", "return", "yield",
    "if", "else", "for", "forall", "in", "match", "transaction",
    "record", "error", "true", "false", "some", "none", "ok", "err",
    "unsafe", "finally", "and", "or", "not",
    "type", "sealed", "aggregate", "case", "self",
    "seq", "out", "inout", "enum",
})


def _safe_param_name(name: str) -> str:
    """Rename `type` → `type_` etc. so generated stubs parse."""
    if name.lower() in _PELL_RESERVED:
        return name + "_"
    return name


def _oracle_to_pell_type(data_type: str) -> tuple[str, str]:
    """Return (pell_type, optional_TODO_marker). Empty marker means
    a confident mapping; non-empty means we punted to a placeholder."""
    if not data_type:
        return "text", "  // TODO: Oracle reported empty data_type"
    pell = _ORACLE_TYPE_TO_PELL.get(data_type.upper())
    if pell is None:
        return "text", f"  // TODO: unmapped Oracle type {data_type!r}"
    return pell, ""


def _stubgen_one(
    conn, owner: str, pkg: str, dest: "Path",
) -> tuple[bool, int, int, int]:
    """Generate stub for a single Oracle package, writing to `dest`.

    Returns (success, fn_count, overload_skip_count, todo_count). Success
    is False when the package has no introspectable surface (privileges
    or empty package).
    """
    rows = conn.run_query(
        """
        SELECT object_name,
               subprogram_id,
               sequence,
               argument_name,
               position,
               data_type,
               in_out,
               overload
          FROM all_arguments
         WHERE owner = :o AND package_name = :p
         ORDER BY object_name, subprogram_id, sequence
        """,
        {"o": owner, "p": pkg},
    )
    # ALL_ARGUMENTS misses zero-arg procedures — pull from ALL_PROCEDURES.
    proc_rows = conn.run_query(
        """
        SELECT procedure_name AS object_name,
               subprogram_id
          FROM all_procedures
         WHERE owner = :o AND object_name = :p
           AND procedure_name IS NOT NULL
        """,
        {"o": owner, "p": pkg},
    )
    if not rows and not proc_rows:
        return False, 0, 0, 0
    # Package-spec PL/SQL TYPE declarations (ALL_PLSQL_TYPES). Each row
    # describes one record/varray/nested-table. We translate records to
    # `pub record` and collections to a list-type alias comment (pell
    # doesn't have type aliases; collections still surface as `list<T>`
    # at use sites, but the declaration is documented).
    type_rows = conn.run_query(
        """
        SELECT type_name, typecode
          FROM all_plsql_types
         WHERE owner = :o AND package_name = :p
         ORDER BY type_name
        """,
        {"o": owner, "p": pkg},
    )
    # Per-type attributes for record types.
    type_attr_rows = conn.run_query(
        """
        SELECT type_name, attr_name, attr_type_name, length, precision
          FROM all_plsql_type_attrs
         WHERE owner = :o AND package_name = :p
         ORDER BY type_name, attr_no
        """,
        {"o": owner, "p": pkg},
    ) if type_rows else []
    # Build record decls
    from collections import defaultdict
    record_decls: list[str] = []
    todo_count = 0
    type_attr_by_type: dict[str, list[dict]] = defaultdict(list)
    for ar in type_attr_rows:
        type_attr_by_type[ar["type_name"]].append(ar)
    for tr in type_rows:
        if tr["typecode"] != "PL/SQL RECORD":
            continue
        attrs = type_attr_by_type.get(tr["type_name"], [])
        if not attrs:
            continue
        fields: list[str] = []
        for a in attrs:
            fname = _safe_param_name(a["attr_name"].lower())
            ftype, marker = _oracle_to_pell_type(a["attr_type_name"] or "")
            if marker:
                todo_count += 1
            fields.append(f"  {fname}: {ftype},")
        record_decls.append(
            f"pub record {tr['type_name']} {{\n"
            + "\n".join(fields)
            + "\n}"
        )
    # Group fn rows by (object_name, subprogram_id) — overloads stay
    # distinct so we can pick one per name + report the rest.
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        grouped[(r["object_name"], r["subprogram_id"] or 0)].append(r)
    # Fold in zero-arg procedures.
    for pr in proc_rows:
        key = (pr["object_name"], pr["subprogram_id"] or 0)
        if key not in grouped:
            grouped[key] = []
    seen_names: set[str] = set()
    body_lines: list[str] = []
    skipped_overloads: list[str] = []
    for (proc_name, _sid), arg_rows in sorted(grouped.items()):
        if proc_name in seen_names:
            skipped_overloads.append(proc_name)
            continue
        seen_names.add(proc_name)
        return_row = next(
            (r for r in arg_rows if (r["position"] or 0) == 0), None,
        )
        param_rows = sorted(
            [r for r in arg_rows if (r["position"] or 0) > 0],
            key=lambda r: r["position"],
        )
        params: list[str] = []
        for r in param_rows:
            pname = _safe_param_name((r["argument_name"] or "arg").lower())
            ptype, marker = _oracle_to_pell_type(r["data_type"])
            if marker:
                todo_count += 1
            mode = ""
            if r["in_out"] == "OUT":
                mode = "out "
            elif r["in_out"] == "IN/OUT":
                mode = "inout "
            params.append(f"{mode}{pname}: {ptype}")
        sig_params = ", ".join(params)
        if return_row is not None:
            ret_type, _ = _oracle_to_pell_type(return_row["data_type"])
            body_lines.append(
                f"pub fn {proc_name.lower()}({sig_params}) -> {ret_type} {{ }}"
            )
        else:
            body_lines.append(f"pub fn {proc_name.lower()}({sig_params}) {{ }}")
    # Header + assembly.
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    header = [
        f"// Stub for {owner}.{pkg}.",
        f"// Generated by `pell stubgen` from ALL_ARGUMENTS on {now}.",
        "// Signature-only — not deployed. `@stub` skips emit, but the",
        "// scanner picks these up so `import` resolution and completion",
        "// work.",
    ]
    if skipped_overloads:
        header.append(
            "// NOTE: skipped overloads (pell has no overloading in v0): "
            + ", ".join(sorted(set(skipped_overloads)))
        )
    if todo_count:
        header.append(
            f"// NOTE: {todo_count} unmapped Oracle type(s) — review TODOs."
        )
    sections = ["\n".join(header), "@stub", f"module {pkg.lower()};"]
    if record_decls:
        sections.append("\n\n".join(record_decls))
    if body_lines:
        sections.append("\n".join(body_lines))
    file_text = "\n\n".join(sections) + "\n"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(file_text)
    return True, len(seen_names), len(skipped_overloads), todo_count


def cmd_stubgen(args: argparse.Namespace) -> int:
    """Generate pell stub files by introspecting Oracle packages.

    Two modes:
      * Single package — `pell stubgen <pkg>` introspects that one
        package and writes runtime/stubs/<pkg>.pell.
      * Whole schema — `pell stubgen --all` queries ALL_OBJECTS for
        every PACKAGE under the owner and stubs each in turn.
    """
    if not args.all and not args.package:
        print(
            "pell stubgen: provide a package name or use --all to stub "
            "every package in the owner's schema.",
            file=sys.stderr,
        )
        return 2
    try:
        from . import driver
    except ImportError as e:
        print(
            f"pell: `pell stubgen` needs the optional driver dependency; "
            f"install with: pip install -e .[repl]\n  ({e})",
            file=sys.stderr,
        )
        return 2
    try:
        conn = driver.connect(args.connect)
    except Exception as e:
        print(f"pell: connection failed: {e}", file=sys.stderr)
        return 1
    owner = args.owner.upper() if args.owner else "SYS"
    if args.all:
        # Enumerate every package in the owner's schema.
        try:
            pkg_rows = conn.run_query(
                """
                SELECT object_name
                  FROM all_objects
                 WHERE owner = :o AND object_type = 'PACKAGE'
                 ORDER BY object_name
                """,
                {"o": owner},
            )
        except Exception as e:
            conn.close()
            print(f"pell stubgen: failed to list packages: {e}", file=sys.stderr)
            return 1
        if not pkg_rows:
            conn.close()
            print(
                f"pell stubgen: no packages found in {owner}. check the "
                f"owner spelling and that you have privileges on ALL_OBJECTS.",
                file=sys.stderr,
            )
            return 1
        out_dir = Path(args.dir_output) if args.dir_output else Path("runtime/stubs")
        ok = skip = fail = 0
        total_fns = 0
        try:
            for r in pkg_rows:
                pkg = r["object_name"]
                dest = out_dir / f"{pkg.lower()}.pell"
                try:
                    success, fns, overloads, todos = _stubgen_one(
                        conn, owner, pkg, dest,
                    )
                except Exception as e:
                    fail += 1
                    print(f"  ✗ {owner}.{pkg}: {e}", file=sys.stderr)
                    continue
                if not success:
                    skip += 1
                    print(f"  ⊘ {owner}.{pkg}: no introspectable surface")
                    continue
                ok += 1
                total_fns += fns
                print(
                    f"  ✓ {owner}.{pkg} → {dest}  "
                    f"({fns} fn(s), {overloads} overload(s), {todos} TODO(s))"
                )
        finally:
            conn.close()
        print(
            f"\npell stubgen --all: {ok} pkg(s) generated, "
            f"{skip} skipped, {fail} failed, {total_fns} total fns."
        )
        return 0 if fail == 0 else 1
    # Single-package mode.
    pkg = args.package.upper()
    if args.output:
        dest = Path(args.output)
    elif args.dir_output:
        dest = Path(args.dir_output) / f"{pkg.lower()}.pell"
    else:
        dest = Path("runtime/stubs") / f"{pkg.lower()}.pell"
    try:
        success, fns, overloads, todos = _stubgen_one(conn, owner, pkg, dest)
    finally:
        conn.close()
    if not success:
        print(
            f"pell stubgen: no arguments found for {owner}.{pkg}. "
            f"check the package exists and you have privileges on "
            f"ALL_ARGUMENTS.",
            file=sys.stderr,
        )
        return 1
    print(
        f"pell stubgen: {owner}.{pkg} → {dest}  "
        f"({fns} fn(s), {overloads} overload(s) skipped, {todos} TODO(s))"
    )
    return 0


def cmd_srcmap(args: argparse.Namespace) -> int:
    """Print the pell↔PL/SQL line map for a .pell file as JSON.

    Compiles with debug markers in-memory (no DB, no files) and scans
    the emitted text. Because a `pell deploy --debug` deploys exactly
    this text, the map is valid for the deployed units too.

    With --anon, the file is compiled as an exec script (anonymous
    block) instead — the map's unit lines are block lines, valid for
    the $Oracle.Block.* class the block becomes under debug-target.
    """
    import json
    from .srcmap import srcmap_json, MARKER_RE

    src_path = Path(args.input)
    try:
        src = src_path.read_text()
    except OSError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1

    if getattr(args, "anon", False):
        try:
            items, stmts = parse_cell(src, str(src_path))
            block = emit_anon_block(items, stmts, target=args.target,
                                    source_path=str(src_path), debug=True)
        except (LexError, ParseError, EmitError) as e:
            print(f"pell: {e}", file=sys.stderr)
            return 1
        entries = []
        for i, line in enumerate(block.splitlines(), start=1):
            mk = MARKER_RE.search(line)
            if mk:
                entries.append({"pell_line": int(mk.group(1)), "unit_line": i})
        print(json.dumps({
            "pell_file": str(src_path),
            "units": [{
                "kind": "BLOCK", "schema": None, "name": None,
                "jdwp_class": "$Oracle.Block", "entries": entries,
            }],
        }, indent=2))
        return 0

    try:
        module = parse(src, str(src_path))
    except (LexError, ParseError) as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    try:
        from .repl import scan_project_signatures
        from .emitter import scan_project_records as _scan_records
        from .emitter import scan_project_modules as _scan_modules
        project_signatures = scan_project_signatures()
        project_records = _scan_records()
        project_modules = _scan_modules()
    except Exception:
        project_signatures = {}
        project_records = {}
        project_modules = {}
    try:
        sql = emit(module, target=args.target, source_text=src,
                   source_path=str(src_path), reproducible=True, debug=True,
                   project_signatures=project_signatures,
                   project_records=project_records,
                   project_modules=project_modules)
    except EmitError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    print(json.dumps(srcmap_json(sql, str(src_path)), indent=2))
    return 0


def cmd_debug_source(args: argparse.Namespace) -> int:
    """Print a deployed unit's source from ALL_SOURCE.

    The debugger shows this when a stack frame lands in a unit with no
    pell source (the logger runtime, hand-written PL/SQL, SYS code the
    user can see). Line N of the output == JDWP unit line N.
    """
    try:
        from . import driver
    except ImportError as e:
        print(f"pell: needs the driver dependency (pip install -e .[repl])\n  ({e})",
              file=sys.stderr)
        return 2
    owner, _, name = args.unit.rpartition(".")
    if not owner:
        print("pell: unit must be OWNER.NAME", file=sys.stderr)
        return 2
    try:
        conn = driver.connect(args.connect)
    except Exception as e:
        print(f"pell: connection failed: {e}", file=sys.stderr)
        return 1
    try:
        rows = conn.run_query(
            "select text from all_source "
            "where owner = upper(:o) and name = upper(:n) "
            "and type = upper(:t) order by line",
            {"o": owner, "n": name, "t": args.type.replace("_", " ")},
        )
        if not rows:
            print(f"pell: no source for {args.unit} ({args.type})", file=sys.stderr)
            return 1
        for r in rows:
            # one column; lines already end with \n in ALL_SOURCE
            sys.stdout.write(str(list(r.values())[0]))
        return 0
    finally:
        conn.close()


_UT_REPORTERS = {
    "doc": "ut_documentation_reporter()",
    "junit": "ut_junit_reporter()",
    "sonar": "ut_sonar_test_reporter()",
    "teamcity": "ut_teamcity_reporter()",
    "coverage": "ut_coverage_html_reporter()",
    "cobertura": "ut_coverage_cobertura_reporter()",
}


_UT_NOT_INSTALLED_HELP = """\
pell test: utPLSQL is not installed (or not reachable) on this database.

  `pell test` runs your @suite / @test modules through utPLSQL's
  ut.run(). It is a one-time, per-database install (do it as a DBA):

    1. Download a release and unpack it:
         https://github.com/utPLSQL/utPLSQL/releases/latest
       (grab utPLSQL.tar.gz or utPLSQL.zip)

    2. From the unpacked `source/` directory, connected as SYSDBA:
         sqlplus sys/<password>@<db> as sysdba @install_headless.sql

       That creates the UT3 schema WITH public synonyms and grants to
       PUBLIC, so ut.run() is callable from every schema. Override the
       schema / password / tablespace positionally if you like:
         @install_headless.sql UT3 UT3 USERS

  Install guide:
    https://github.com/utPLSQL/utPLSQL/blob/develop/docs/userguide/install.md

  Already installed, but in a schema WITHOUT public synonyms? Tell pell
  where it lives:
         pell test <suite> --ut-schema UT3
    or set PELL_UT_SCHEMA=UT3 (in the IDE: Settings -> Tools -> pell).
"""


def _ut_owner(args: argparse.Namespace) -> Optional[str]:
    """The schema utPLSQL is installed in, when it isn't reachable via
    public synonym. CLI flag wins over the PELL_UT_SCHEMA env var; None
    means "resolve `ut` unqualified" (the default public-synonym install)."""
    flag = (getattr(args, "ut_schema", None) or "").strip()
    if flag:
        return flag
    env = (os.environ.get("PELL_UT_SCHEMA") or "").strip()
    return env or None


def _utplsql_available(conn, owner: Optional[str]) -> bool:
    """True when the UT package is visible to the connected schema —
    either via public synonym (default install) or in `owner`. Cheap
    pre-flight so a missing install yields actionable guidance instead
    of a raw PLS-00201 from deep inside ut.run()."""
    if owner:
        rows = conn.run_query(
            "select count(*) n from all_objects "
            "where object_type = 'PACKAGE' and object_name = 'UT' "
            "and owner = upper(:o)",
            {"o": owner},
        )
    else:
        # Any UT package the caller can see (own schema, public synonym,
        # or a direct grant) counts — that's exactly what ut.run() needs
        # to resolve unqualified.
        rows = conn.run_query(
            "select count(*) n from all_objects "
            "where object_type = 'PACKAGE' and object_name = 'UT'"
        )
    return int(list(rows[0].values())[0]) > 0


def cmd_test(args: argparse.Namespace) -> int:
    """Run a utPLSQL suite (`@suite` pell module or any annotated
    package) and emit the chosen report.

        pell test ut_pell_examples                      # console (doc)
        pell test ut_pell_examples --reporter junit -o results.xml
        pell test ut_pell_examples --reporter coverage -o cov.html

    Coverage runs instrument the tested code database-side; line
    numbers refer to the deployed PL/SQL (pair with `pell srcmap` to
    read them against pell source).
    """
    try:
        from . import driver
    except ImportError as e:
        print(f"pell: needs the driver dependency (pip install -e .[repl])\n  ({e})",
              file=sys.stderr)
        return 2
    # utPLSQL lives behind public synonyms in the default install, so
    # `ut` / `ut_*` resolve unqualified. When it's installed to a schema
    # without those synonyms, --ut-schema / PELL_UT_SCHEMA names the
    # owner and we qualify every framework reference with it.
    owner = _ut_owner(args)
    pfx = f"{owner}." if owner else ""
    reporter = pfx + _UT_REPORTERS[args.reporter]
    suite = args.suite
    if args.reporter in ("coverage", "cobertura"):
        # Limit instrumentation to the schemas under test, or coverage
        # reports drown in SYS noise.
        block = (
            f"begin {pfx}ut.run('" + suite + "', " + reporter + ", "
            f"a_coverage_schemes => {pfx}ut_varchar2_list(USER)); end;"
        )
    else:
        block = f"begin {pfx}ut.run('" + suite + "', " + reporter + "); end;"
    try:
        conn = driver.connect(args.connect)
    except Exception as e:
        print(f"pell: connection failed: {e}", file=sys.stderr)
        return 1
    try:
        # Fail fast with install guidance rather than a cryptic
        # PLS-00201 from inside ut.run() when utPLSQL isn't there.
        if not _utplsql_available(conn, owner):
            print(_UT_NOT_INSTALLED_HELP, file=sys.stderr)
            return 3
        lines = conn.run_block(block)
    except Exception as e:
        print(f"pell: ut.run failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    text = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(text)
        print(f"  → {args.output} ({len(lines)} lines)")
    else:
        print(text)
    # Exit code mirrors the run: the doc reporter prints a summary line;
    # structured reporters carry failures in their own format, so query
    # the summary separately when we can't parse one.
    import re as _re
    m = _re.search(r"(\d+) tests?, (\d+) failed, (\d+) errored", text)
    if m:
        return 1 if (int(m.group(2)) or int(m.group(3))) else 0
    if args.reporter == "junit":
        return 1 if _re.search(r'<(failure|error)[ >]', text) else 0
    return 0


def cmd_debug_serve(args: argparse.Namespace) -> int:
    """SQL-transport debug engine (DBMS_DEBUG over two outbound
    connections) speaking JSON lines on stdin/stdout. See
    pell/debug_serve.py for the protocol. No network ACE, no inbound
    connection — works anywhere `pell exec` works (plus the
    DEBUG CONNECT SESSION grant)."""
    source_path = args.input
    try:
        source = Path(args.input).read_text()
    except OSError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    try:
        items, stmts = parse_cell(source, source_path)
    except (LexError, ParseError) as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    if not items and not stmts:
        print("pell: script is empty — nothing to debug", file=sys.stderr)
        return 2
    try:
        from .repl import scan_project_signatures
        from .emitter import scan_project_records as _scan_records
        from .emitter import scan_project_modules as _scan_modules
        project_signatures = scan_project_signatures()
        project_records = _scan_records()
        project_modules = _scan_modules()
    except Exception:
        project_signatures = {}
        project_records = {}
        project_modules = {}
    try:
        block = emit_anon_block(items, stmts, target=args.target,
                                source_path=source_path, debug=True,
                                project_signatures=project_signatures,
                                project_records=project_records,
                                project_modules=project_modules)
    except EmitError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    try:
        from .debug_serve import serve
    except ImportError as e:
        print(f"pell: needs the driver dependency (pip install -e .[repl])\n  ({e})",
              file=sys.stderr)
        return 2
    return serve(block, args.connect)


def cmd_debug_target(args: argparse.Namespace) -> int:
    """Run a pell exec script as the *target* session of a debug run.

    The IDE listens on a JDWP port; this command connects the database
    session back to it (DBMS_DEBUG_JDWP.CONNECT_TCP), executes the
    script's anonymous block — during which the IDE controls stepping —
    then disconnects and prints any DBMS_OUTPUT the block produced.
    """
    try:
        host, _, port_s = args.jdwp.rpartition(":")
        port = int(port_s)
        if not host:
            raise ValueError
    except ValueError:
        print(f"pell: --jdwp must be HOST:PORT, got {args.jdwp!r}", file=sys.stderr)
        return 2

    source_path = args.input
    try:
        source = Path(args.input).read_text()
    except OSError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    try:
        items, stmts = parse_cell(source, source_path)
    except (LexError, ParseError) as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1
    if not items and not stmts:
        print("pell: script is empty — nothing to debug", file=sys.stderr)
        return 2
    try:
        from .repl import scan_project_signatures
        from .emitter import scan_project_records as _scan_records
        from .emitter import scan_project_modules as _scan_modules
        project_signatures = scan_project_signatures()
        project_records = _scan_records()
        project_modules = _scan_modules()
    except Exception:
        project_signatures = {}
        project_records = {}
        project_modules = {}
    try:
        block = emit_anon_block(items, stmts, target=args.target,
                                source_path=source_path, debug=True,
                                project_signatures=project_signatures,
                                project_records=project_records,
                                project_modules=project_modules)
    except EmitError as e:
        print(f"pell: {e}", file=sys.stderr)
        return 1

    try:
        from . import driver
    except ImportError as e:
        print(f"pell: `pell debug-target` needs the optional driver dependency; "
              f"install with: pip install -e .[repl]\n  ({e})", file=sys.stderr)
        return 2
    try:
        conn = driver.connect(args.connect)
    except Exception as e:
        print(f"pell: connection failed: {e}", file=sys.stderr)
        return 1

    # The IDE parses these two lines to sync with the target's lifecycle.
    print(f"pell debug-target: connecting to jdwp {host}:{port}", flush=True)
    try:
        with conn.raw.cursor() as cur:
            # The anonymous block itself must carry debug info or its
            # frames report line -1 and stepping through the stub is
            # impossible. (Deployed packages get this from deploy --debug.)
            cur.execute("ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 1")
            cur.execute("ALTER SESSION SET PLSQL_DEBUG = TRUE")
            cur.execute(
                "begin dbms_debug_jdwp.connect_tcp(:h, :p); end;",
                h=host, p=port,
            )
        if getattr(args, "wait_for_go", False):
            # The session is attached but Oracle does NOT suspend it —
            # without a barrier the block can finish before the debugger
            # arms its ClassPrepare/breakpoint requests. The IDE writes a
            # line to our stdin once it's ready.
            print("pell debug-target: jdwp connected; awaiting go", flush=True)
            sys.stdin.readline()
        print("pell debug-target: jdwp connected; running block", flush=True)
        try:
            lines = conn.run_block(block)
        finally:
            # Disconnect even if the block raised, or the session stays
            # pinned to a dead listener.
            try:
                with conn.raw.cursor() as cur:
                    cur.execute("begin dbms_debug_jdwp.disconnect; end;")
            except Exception:
                pass
        for ln in lines:
            print(ln)
        print("pell debug-target: block finished", flush=True)
        return 0
    except Exception as e:
        msg = str(e)
        hint = _explain_debug_run_error(msg)
        print(f"pell: debug run failed: {msg}", file=sys.stderr)
        if hint:
            print(hint, file=sys.stderr)
        return 1
    finally:
        conn.close()


def _explain_debug_run_error(msg: str) -> Optional[str]:
    """Map an opaque Oracle internal error from a debug run to an
    actionable explanation. Oracle's JDWP debug VM can't *execute*
    certain otherwise-valid constructs while attached — most often a
    FORALL that binds individual fields of a collection of records
    (what `forall x in <list<Record>>` lowers to). The package compiles
    and runs fine without --debug; only live stepping trips it."""
    m = msg.upper()
    internal = ("ORA-00600" in m or "PLS-707" in m
                or "[15419]" in m or "[2649]" in m
                or "UNSUPPORTED CONSTRUCT" in m)
    if not internal:
        return None
    return (
        "  └─ This is an Oracle debugger limitation: its JDWP debug VM\n"
        "     can't EXECUTE some constructs while attached — classically a\n"
        "     FORALL over record-collection fields. pell already lowers its\n"
        "     own `forall` to a steppable FOR loop in --debug builds, so a\n"
        "     pell program shouldn't hit this; if you do, it's likely a\n"
        "     hand-written sql!{} PL/SQL block or another such construct.\n"
        "     The code is valid and runs fine without --debug. Redeploy\n"
        "     that unit without --debug to run it natively, or rework the\n"
        "     offending construct."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pell", description=f"pell compiler v{__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="compile .pell to PL/SQL")
    b.add_argument("input", help="path to a .pell file or a directory of them")
    b.add_argument("-o", "--output", help="output .sql file (single input only)")
    b.add_argument("-d", "--dir-output", help="output directory (when compiling a dir)")
    b.add_argument(
        "--target",
        choices=("23", "19c"),
        default="23",
        help="Oracle target version (default: 23). 19c downgrades JSON to "
        "VARCHAR2(32767) and BOOLEAN-in-OBJECT to NUMBER(1).",
    )
    b.add_argument(
        "--reproducible",
        action="store_true",
        help="omit the build timestamp and uncommitted-working-tree hash from "
        "the preamble so output is byte-stable across runs from the same "
        "source + commit. Useful for golden snapshots.",
    )
    b.add_argument(
        "--debug",
        action="store_true",
        help="append `-- @pell:<line>` markers to emitted statements so the "
        "debugger can map PL/SQL lines back to pell source",
    )
    b.add_argument(
        "--trace",
        action="store_true",
        help="instrument every statement with a `[pell-trace] file:line` "
        "DBMS_OUTPUT line — zero-privilege execution tracing",
    )
    b.set_defaults(func=cmd_build)

    pa = sub.add_parser("parse", help="print AST for a .pell file")
    pa.add_argument("input")
    pa.set_defaults(func=cmd_parse)

    t = sub.add_parser("tokens", help="print token stream for a .pell file")
    t.add_argument("input")
    t.set_defaults(func=cmd_tokens)

    r = sub.add_parser("runtime", help="aggregate error decls into a pell_runtime.sql")
    r.add_argument("input", help="path to a .pell file or a directory of them")
    r.add_argument("-o", "--output", help="write to this file instead of stdout")
    r.set_defaults(func=cmd_runtime)

    ex = sub.add_parser(
        "exec",
        help="compile a pell cell to an anonymous PL/SQL block and run it",
    )
    ex.add_argument("input", nargs="?", help="path to a .pell file (or omit for stdin)")
    ex.add_argument("-e", "--expr", help="inline pell source to execute")
    ex.add_argument("-c", "--connect", help="user/pass@host:port/service (or set PELL_DB_URL)")
    ex.add_argument("--target", choices=("23", "19c"), default="23")
    ex.add_argument("--dry-run", action="store_true",
                    help="print the emitted block and exit; don't connect to a database")
    ex.add_argument("--show-block", action="store_true",
                    help="on error, also print the emitted block to stderr")
    ex.add_argument("--trace", action="store_true",
                    help="print a `[pell-trace] file:line` line before every "
                         "statement executes — the no-privilege fallback when "
                         "DEBUG CONNECT SESSION can't be granted")
    ex.set_defaults(func=cmd_exec)

    rp = sub.add_parser("repl", help="interactive pell notebook against a live Oracle")
    rp.add_argument("-c", "--connect", help="user/pass@host:port/service (or set PELL_DB_URL)")
    rp.add_argument("--target", choices=("23", "19c"), default="23")
    rp.set_defaults(func=cmd_repl)

    dp = sub.add_parser(
        "deploy",
        help="build a .pell project and install every emitted .sql against PELL_DB_URL "
             "(pell_runtime + pell_re + module spec/body) in the right order",
    )
    dp.add_argument("input", help="path to a .pell file or a directory of them")
    dp.add_argument("-c", "--connect", help="user/pass@host:port/service (or set PELL_DB_URL)")
    dp.add_argument("--target", choices=("23", "19c"), default="23")
    dp.add_argument("--reproducible", action="store_true",
                    help="omit volatile preamble fields so output is byte-stable")
    dp.add_argument("--out-dir", "-d",
                    help="where to write the lowered .sql files "
                         "(default: <input>/plsql)")
    dp.add_argument("--with-re", action="store_true",
                    help="install pell_re.sql even if no module references re::")
    dp.add_argument("--build-only", action="store_true",
                    help="run the build half; skip the install half")
    dp.add_argument("--keep-going", action="store_true",
                    help="don't stop on the first failure; collect them and exit non-zero at the end")
    dp.add_argument("--debug", action="store_true",
                    help="debug build: emit @pell line markers and compile "
                         "units with PL/SQL debug info (PLSQL_DEBUG=TRUE, "
                         "OPTIMIZE_LEVEL=1) so the debugger can stop in them")
    dp.add_argument("--trace", action="store_true",
                    help="instrument deployed units with `[pell-trace]` "
                         "DBMS_OUTPUT lines — zero-privilege execution tracing")
    dp.set_defaults(func=cmd_deploy)

    sm = sub.add_parser(
        "srcmap",
        help="print the pell↔PL/SQL line map (JSON) for a .pell file — the "
             "map the debugger uses to place breakpoints and map frames",
    )
    sm.add_argument("input", help="path to a .pell file")
    sm.add_argument("--target", choices=("23", "19c"), default="23")
    sm.add_argument("--anon", action="store_true",
                    help="map the file as an exec script (anonymous block) "
                         "instead of a module — unit lines are block lines")
    sm.set_defaults(func=cmd_srcmap)

    tst = sub.add_parser(
        "test",
        help="run a utPLSQL suite (@suite pell module) and emit a report "
             "(doc/junit/sonar/teamcity/coverage/cobertura)",
    )
    tst.add_argument("suite", help="suite path for ut.run — package name, "
                     "schema.package, or schema.package.test_fn")
    tst.add_argument("--reporter", choices=sorted(_UT_REPORTERS), default="doc")
    tst.add_argument("-o", "--output", help="write the report to this file "
                     "(default: stdout)")
    tst.add_argument("--ut-schema", dest="ut_schema",
                     help="schema utPLSQL is installed in, when it isn't "
                          "reachable via public synonym (default: resolve "
                          "`ut` unqualified; or set PELL_UT_SCHEMA)")
    tst.add_argument("-c", "--connect", help="user/pass@host:port/service (or set PELL_DB_URL)")
    tst.set_defaults(func=cmd_test)

    ds = sub.add_parser(
        "debug-source",
        help="print a deployed unit's source from ALL_SOURCE (debugger "
             "fallback for frames with no pell source)",
    )
    ds.add_argument("unit", help="OWNER.NAME")
    ds.add_argument("--type", default="PACKAGE_BODY",
                    help="unit type, underscores for spaces (default PACKAGE_BODY)")
    ds.add_argument("-c", "--connect", help="user/pass@host:port/service (or set PELL_DB_URL)")
    ds.set_defaults(func=cmd_debug_source)

    dt = sub.add_parser(
        "debug-target",
        help="run a pell exec script as a JDWP debug target: the session "
             "connects to the listener, executes the script, disconnects",
    )
    dt.add_argument("input", help="path to the .pell stub/script to execute")
    dt.add_argument("--jdwp", required=True, metavar="HOST:PORT",
                    help="the IDE's JDWP listener to connect back to")
    dt.add_argument("-c", "--connect", help="user/pass@host:port/service (or set PELL_DB_URL)")
    dt.add_argument("--target", choices=("23", "19c"), default="23")
    dt.add_argument("--wait-for-go", action="store_true",
                    help="after the jdwp connection, wait for a line on stdin "
                         "before running the block (lets the debugger arm "
                         "breakpoints first — the IDE always passes this)")
    dt.set_defaults(func=cmd_debug_target)

    dsv = sub.add_parser(
        "debug-serve",
        help="SQL-transport debug engine: drive DBMS_DEBUG over two outbound "
             "connections, speaking JSON lines on stdio (no network ACE, "
             "works through SSH tunnels)",
    )
    dsv.add_argument("input", help="path to the .pell stub/script to execute")
    dsv.add_argument("-c", "--connect", help="user/pass@host:port/service (or set PELL_DB_URL)")
    dsv.add_argument("--target", choices=("23", "19c"), default="23")
    dsv.set_defaults(func=cmd_debug_serve)

    sq = sub.add_parser(
        "sql",
        help="run a .sql script against the connected database (pell build output, "
             "deploy scripts, ad-hoc DDL/DML)",
    )
    sq.add_argument("input", help="path to a .sql file")
    sq.add_argument("-c", "--connect", help="user/pass@host:port/service (or set PELL_DB_URL)")
    sq.add_argument("--stop-on-error", action="store_true",
                    help="stop at the first failed statement (default: continue, report at end)")
    sq.set_defaults(func=cmd_sql)

    mig = sub.add_parser(
        "migrate",
        help="rename a project's build-output directory (idempotent). "
             "Use to move existing projects from the legacy `built/` "
             "to the new default `plsql/`.",
    )
    mig.add_argument("root", nargs="?", default=".",
                     help="project root (default: cwd)")
    mig.add_argument("--from", dest="from_dir", default="built",
                     help="current directory name (default: built)")
    mig.add_argument("--to", dest="to_dir", default="plsql",
                     help="new directory name (default: plsql)")
    mig.add_argument("--dry-run", action="store_true",
                     help="show what would change without touching disk")
    mig.set_defaults(func=cmd_migrate)

    sg = sub.add_parser(
        "stubgen",
        help="connect via PELL_DB_URL and generate `@stub module pkg;` "
             "files by introspecting Oracle's ALL_ARGUMENTS / "
             "ALL_PLSQL_TYPES. Use --all to bulk-stub a schema.",
    )
    sg.add_argument(
        "package",
        nargs="?",
        help="Oracle package name (e.g. dbms_output, utl_url, your_pkg). "
             "Omit when using --all.",
    )
    sg.add_argument(
        "--all",
        action="store_true",
        help="stub every package in the owner's schema. Writes to "
             "runtime/stubs/<pkg>.pell per package.",
    )
    sg.add_argument(
        "--owner",
        default="SYS",
        help="schema that owns the package (default: SYS). For legacy "
             "user code, set to your app schema: --owner=APP_SCHEMA --all",
    )
    sg.add_argument(
        "-c", "--connect",
        help="user/pass@host:port/service (or set PELL_DB_URL)",
    )
    sg.add_argument(
        "-o", "--output",
        help="write to this specific path (single-pkg mode only)",
    )
    sg.add_argument(
        "-d", "--dir-output",
        help="write into this directory (filename derived from package). "
             "Default: runtime/stubs/",
    )
    sg.set_defaults(func=cmd_stubgen)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
