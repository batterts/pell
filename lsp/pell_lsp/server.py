"""pell Language Server.

Wraps the existing parser/typer/emitter from `compiler/pell/` and exposes
diagnostics, document symbols, hover, and completion via the Language
Server Protocol.

Run via the wrapper script `lsp/run.sh` (which sets PYTHONPATH).
"""

from __future__ import annotations

import logging
import re
from dataclasses import fields, is_dataclass
from typing import Optional

from pygls.lsp.server import LanguageServer
from lsprotocol import types as lsp

# Imports from the compiler package — wrapper script puts compiler/ on PYTHONPATH.
from pell import ast as A
from pell.emitter import (
    EmitError, emit, lower_type, scan_project_records,
    scan_project_definitions, scan_project_references,
    _render_type,
)
from pell.lexer import LexError
from pell.parser import ParseError, parse

from . import semantic_tokens as _semtok


SERVER_NAME = "pell-lsp"
SERVER_VERSION = "0.5.0"

logger = logging.getLogger(SERVER_NAME)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = LanguageServer(SERVER_NAME, SERVER_VERSION)

# Cache of parsed modules per URI — lets hover/symbols/completion reuse the AST.
_cache: dict[str, Optional[A.Module]] = {}

# Cached cross-package context — scanned lazily on first validate, then
# reused until any .pell file is saved (didSave invalidates). Avoids
# walking the project tree on every keystroke.
#   _project_ctx[root_path] -> (signatures, records, definitions, references)
# where definitions: (pkg, name) -> Loc  (goto-def target)
#       references:  (pkg, name) -> list[Loc]  (find-usages results)
_project_ctx: dict[str, tuple[dict, dict, dict, dict]] = {}


def _project_root_for(doc_path: Optional[str]) -> str:
    """Locate the project root for a doc URI: nearest ancestor that
    contains a `runtime/` or `pyproject.toml` or `.git/`. Falls back
    to the doc's directory. Used as both the scan root and the
    cache key — multi-root workspaces get distinct entries.
    """
    from pathlib import Path as _Path
    if not doc_path:
        return ""
    here = _Path(doc_path).resolve().parent
    for p in (here, *here.parents):
        if (p / ".git").exists() or (p / "pyproject.toml").exists() \
                or (p / "runtime").is_dir():
            return str(p)
    return str(here)


def _get_project_ctx(
    doc_path: Optional[str],
) -> tuple[dict, dict, dict, dict]:
    """Lazy/cached project scan. Returns (signatures, records,
    definitions, references); empty dicts if anything goes wrong (so
    emit still runs with local-only resolution and the user sees the
    existing unknown-type errors). All four are computed in one
    scan-trigger so we never pay for the file walk twice."""
    root = _project_root_for(doc_path)
    if root in _project_ctx:
        return _project_ctx[root]
    from pathlib import Path as _Path
    root_path = _Path(root) if root else None
    try:
        from pell.repl import scan_project_signatures as _scan_sigs
        sigs = _scan_sigs(root=root_path) if root_path else {}
    except Exception:
        sigs = {}
    try:
        recs = scan_project_records(root_path) if root_path else {}
    except Exception:
        recs = {}
    try:
        defs = scan_project_definitions(root_path) if root_path else {}
    except Exception:
        defs = {}
    try:
        refs = scan_project_references(root_path) if root_path else {}
    except Exception:
        refs = {}
    _project_ctx[root] = (sigs, recs, defs, refs)
    return _project_ctx[root]


# ---------------------------------------------------------------------------
# Lifecycle: parse on open, change, save
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def on_open(params: lsp.DidOpenTextDocumentParams) -> None:
    _validate(params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def on_change(params: lsp.DidChangeTextDocumentParams) -> None:
    _validate(params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def on_save(params: lsp.DidSaveTextDocumentParams) -> None:
    # Any save can change the set of pub records / signatures another
    # file depends on — invalidate the project context cache so the
    # next validate pulls fresh.
    _project_ctx.clear()
    _validate(params.text_document.uri)


@server.feature(lsp.WORKSPACE_DID_CHANGE_CONFIGURATION)
def on_did_change_configuration(
    params: lsp.DidChangeConfigurationParams,
) -> None:
    """LSP4IJ pushes a workspace/didChangeConfiguration notification on
    every connect/save. We don't expose configuration knobs yet — this
    no-op handler exists solely to silence the pygls "unknown method"
    warning that otherwise spams the LSP console."""
    return None


def _validate(uri: str) -> None:
    doc = server.workspace.get_text_document(uri)
    src = doc.source
    diagnostics: list[lsp.Diagnostic] = []
    module: Optional[A.Module] = None
    try:
        module = parse(src, doc.path or uri)
        # Run the emitter so EmitError-level checks (unused returns,
        # bare pure expressions, type mismatches, missing fields,
        # cross-package signature errors, ...) surface as red squiggles
        # too. Without this the IDE only sees parse-level issues and
        # happily shows "No problems found" on code that won't compile.
        # Pass the cached project context so cross-package record
        # constructors (`pkg::Record { … }`) and fn calls resolve.
        sigs, recs, _defs, _refs = _get_project_ctx(doc.path)
        emit(module, source_text=src, source_path=doc.path or uri,
             project_signatures=sigs, project_records=recs)
    except (LexError, ParseError) as e:
        diagnostics.append(_diagnostic_from_error(e, src))
    except EmitError as e:
        diagnostics.append(_diagnostic_from_error(e, src))
    except Exception as e:
        # Unexpected — surface as a server-level error
        logger.exception("validation failure")
        diagnostics.append(
            lsp.Diagnostic(
                range=_one_char_range(0, 0),
                message=f"pell-lsp internal error: {e}",
                severity=lsp.DiagnosticSeverity.Error,
                source=SERVER_NAME,
            )
        )
    _cache[uri] = module
    server.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


def _diagnostic_from_error(e: Exception, src: str) -> lsp.Diagnostic:
    loc = getattr(e, "loc", None)
    msg = getattr(e, "msg", str(e))
    # EmitError carries a stable code (e.g. "pell.unused-return") that
    # the codeAction handler matches on to offer quickfixes. Empty
    # string means "no auto-fix available".
    code = getattr(e, "code", "") or None
    if loc is None:
        return lsp.Diagnostic(
            range=_one_char_range(0, 0),
            message=msg,
            severity=lsp.DiagnosticSeverity.Error,
            source=SERVER_NAME,
            code=code,
        )
    line = max(loc.line - 1, 0)
    col = max(loc.col - 1, 0)
    # Try to extend the range to the end of the offending token.
    end_col = _end_of_token(src, line, col)
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=line, character=col),
            end=lsp.Position(line=line, character=end_col),
        ),
        message=msg,
        severity=lsp.DiagnosticSeverity.Error,
        source=SERVER_NAME,
        code=code,
    )


# ---------------------------------------------------------------------------
# Code actions — IDE quickfixes for diagnostics that carry a known code.
# Currently fixes two flavors:
#   pell.unused-return  — fn call whose return is discarded
#   pell.unused-value   — bare expression with no side effect
# Both are fixed identically: insert `let _ = ` at the start of the
# offending expression. The user can then rename `_` to a real binding.
# ---------------------------------------------------------------------------


_LET_UNDERSCORE_CODES = {"pell.unused-return", "pell.unused-value"}


@server.feature(
    lsp.TEXT_DOCUMENT_CODE_ACTION,
    lsp.CodeActionOptions(code_action_kinds=[lsp.CodeActionKind.QuickFix]),
)
def on_code_action(
    params: lsp.CodeActionParams,
) -> Optional[list[lsp.CodeAction]]:
    actions: list[lsp.CodeAction] = []
    uri = params.text_document.uri
    for diag in params.context.diagnostics:
        if diag.code in _LET_UNDERSCORE_CODES:
            actions.append(_let_underscore_action(uri, diag))
        elif diag.code == "pell.missing-import":
            action = _missing_import_action(uri, diag)
            if action is not None:
                actions.append(action)
        elif diag.code == "pell.dot-vs-colon":
            action = _dot_vs_colon_action(uri, diag)
            if action is not None:
                actions.append(action)
        elif diag.code == "pell.missing-return":
            action = _missing_return_action(uri, diag)
            if action is not None:
                actions.append(action)
        elif diag.code == "pell.unknown-struct-type":
            action = _unknown_struct_type_action(uri, diag)
            if action is not None:
                actions.append(action)
    return actions or None


def _dot_vs_colon_action(
    uri: str, diag: lsp.Diagnostic,
) -> Optional[lsp.CodeAction]:
    """Rewrite `pkg.fn(...)` -> `pkg::fn(...)` based on the
    emitter's suggestion. The diagnostic message contains both
    forms wrapped in backticks: `wrong` ... `right`."""
    m = re.search(r"`([\w.]+\([^)]*\))`.*`([\w:]+\([^)]*\))`", diag.message)
    if m is None:
        # Less ambitious form — just swap the first `.` between
        # word chars on the diagnostic's range.
        return None
    wrong, right = m.group(1), m.group(2)
    try:
        doc = server.workspace.get_text_document(uri)
    except Exception:
        return None
    src = doc.source
    # Find the wrong text near the diagnostic range and replace.
    lines = src.splitlines()
    if diag.range.start.line >= len(lines):
        return None
    line = lines[diag.range.start.line]
    idx = line.find(wrong)
    if idx < 0:
        return None
    edit = lsp.WorkspaceEdit(changes={uri: [lsp.TextEdit(
        range=lsp.Range(
            start=lsp.Position(line=diag.range.start.line, character=idx),
            end=lsp.Position(line=diag.range.start.line, character=idx + len(wrong)),
        ),
        new_text=right,
    )]})
    return lsp.CodeAction(
        title=f"Rewrite as `{right}`",
        kind=lsp.CodeActionKind.QuickFix,
        diagnostics=[diag],
        edit=edit,
        is_preferred=True,
    )


# Zero-value literals per pell type — feeds the missing-return quickfix.
_ZERO_VALUES = {
    "text": '""',
    "number": "0",
    "bool": "false",
    "date": "sql! { select sysdate from dual }.scalar()",
    "timestamp": "sql! { select systimestamp from dual }.scalar()",
    "json": 'json::parse("null")',
    "bytes": "hextoraw('')",
}


def _missing_return_action(
    uri: str, diag: lsp.Diagnostic,
) -> Optional[lsp.CodeAction]:
    """Insert `    return <zero-value>;` at the bottom of the fn body
    (right before the closing `}`). We don't know the type from the
    diagnostic alone, so the message-parser pulls it from the
    "declared `-> <type>`" phrase in the emitter's error text."""
    m = re.search(r"declared `-> ([\w<>\s,]+)`", diag.message)
    inferred = m.group(1).strip() if m else ""
    # list<T> -> []
    if inferred.startswith("list<"):
        zero = "[]"
    elif inferred.startswith("Option<"):
        zero = "None"
    elif inferred.startswith("cursor<"):
        zero = "sql! { select null from dual where 1 = 0 }"
    else:
        zero = _ZERO_VALUES.get(inferred, f"{inferred} {{ }}")
    try:
        doc = server.workspace.get_text_document(uri)
    except Exception:
        return None
    src = doc.source
    lines = src.splitlines()
    # diag.range points at the fn decl line ('pub fn foo(...) ->
    # Type {'). Walk forward to find the matching `}` at the same
    # indentation as the opening — that's where to insert.
    start = diag.range.start.line
    if start >= len(lines):
        return None
    depth = 0
    end_line = start
    started = False
    for i in range(start, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth == 0:
                    end_line = i
                    break
        else:
            continue
        break
    if not started:
        return None
    # Insert just before the closing brace's line.
    indent = "    "
    edit = lsp.WorkspaceEdit(changes={uri: [lsp.TextEdit(
        range=lsp.Range(
            start=lsp.Position(line=end_line, character=0),
            end=lsp.Position(line=end_line, character=0),
        ),
        new_text=f"{indent}return {zero};\n",
    )]})
    return lsp.CodeAction(
        title=f"Add `return {zero};`",
        kind=lsp.CodeActionKind.QuickFix,
        diagnostics=[diag],
        edit=edit,
        is_preferred=True,
    )


def _unknown_struct_type_action(
    uri: str, diag: lsp.Diagnostic,
) -> Optional[lsp.CodeAction]:
    """Insert the suggested `record Foo { … }` definition near the top
    of the file (after `module ...;` and any `import`s). The emitter's
    error message includes "did you mean: record Foo { ... }" — we
    pull the literal record decl out and insert it."""
    m = re.search(r"did you mean: (record [^\n]+\})", diag.message)
    if m is None:
        return None
    record_decl = m.group(1)
    try:
        doc = server.workspace.get_text_document(uri)
    except Exception:
        return None
    src = doc.source
    lines = src.splitlines()
    # Insert after the last `import ...;` line — if none, after the
    # `module ...;`. Look at the top 20 lines only.
    insert_line = 0
    for i, line in enumerate(lines[:20]):
        s = line.strip()
        if s.startswith("module ") and s.endswith(";"):
            insert_line = max(insert_line, i + 1)
        if s.startswith("import ") and s.endswith(";"):
            insert_line = max(insert_line, i + 1)
    edit = lsp.WorkspaceEdit(changes={uri: [lsp.TextEdit(
        range=lsp.Range(
            start=lsp.Position(line=insert_line, character=0),
            end=lsp.Position(line=insert_line, character=0),
        ),
        new_text=f"\npub {record_decl}\n",
    )]})
    return lsp.CodeAction(
        title="Create record definition",
        kind=lsp.CodeActionKind.QuickFix,
        diagnostics=[diag],
        edit=edit,
        is_preferred=True,
    )


def _let_underscore_action(
    uri: str, diag: lsp.Diagnostic,
) -> lsp.CodeAction:
    # WorkspaceEdit: pure-insert at diag.range.start. An empty
    # range means "insert here" — no characters are replaced.
    edit = lsp.WorkspaceEdit(
        changes={
            uri: [
                lsp.TextEdit(
                    range=lsp.Range(
                        start=diag.range.start,
                        end=diag.range.start,
                    ),
                    new_text="let _ = ",
                )
            ]
        }
    )
    return lsp.CodeAction(
        title="Bind result with `let _ = `",
        kind=lsp.CodeActionKind.QuickFix,
        diagnostics=[diag],
        edit=edit,
        is_preferred=True,
    )


def _missing_import_action(
    uri: str, diag: lsp.Diagnostic,
) -> Optional[lsp.CodeAction]:
    """Insert `import <pkg>;` at the top of the file (after `module …;`).
    The package name is parsed out of the diagnostic message — the
    emitter formats it as "package `pkg` is not imported. Add `import
    pkg;` …", so we extract from the backticks."""
    m = re.search(r"`(import [^`]+;)`", diag.message)
    if m is None:
        return None
    import_line = m.group(1)
    # Find insertion point: the line right after the `module ...;`.
    # Read the doc source to locate it.
    try:
        doc = server.workspace.get_text_document(uri)
    except Exception:
        return None
    src = doc.source
    insert_line = 0
    for i, line in enumerate(src.splitlines()):
        s = line.strip()
        if s.startswith("module ") and s.endswith(";"):
            insert_line = i + 1
            break
    edit = lsp.WorkspaceEdit(
        changes={
            uri: [
                lsp.TextEdit(
                    range=lsp.Range(
                        start=lsp.Position(line=insert_line, character=0),
                        end=lsp.Position(line=insert_line, character=0),
                    ),
                    new_text=f"{import_line}\n",
                )
            ]
        }
    )
    return lsp.CodeAction(
        title=f"Add `{import_line}`",
        kind=lsp.CodeActionKind.QuickFix,
        diagnostics=[diag],
        edit=edit,
        is_preferred=True,
    )


def _end_of_token(src: str, line: int, col: int) -> int:
    """Approximate token end on the given line — read until non-word character
    so the diagnostic underline covers the offending word."""
    try:
        line_text = src.splitlines()[line]
    except IndexError:
        return col + 1
    m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", line_text[col:])
    return col + (m.end() if m else 1)


def _one_char_range(line: int, col: int) -> lsp.Range:
    return lsp.Range(
        start=lsp.Position(line=line, character=col),
        end=lsp.Position(line=line, character=col + 1),
    )


def _range_from_loc(loc: A.Loc, src: str) -> lsp.Range:
    line = max(loc.line - 1, 0)
    col = max(loc.col - 1, 0)
    end_col = _end_of_token(src, line, col)
    return lsp.Range(
        start=lsp.Position(line=line, character=col),
        end=lsp.Position(line=line, character=end_col),
    )


# ---------------------------------------------------------------------------
# Document symbols (outline)
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def on_document_symbol(params: lsp.DocumentSymbolParams) -> list[lsp.DocumentSymbol]:
    uri = params.text_document.uri
    module = _cache.get(uri)
    if module is None:
        return []
    doc = server.workspace.get_text_document(uri)
    src = doc.source
    symbols: list[lsp.DocumentSymbol] = []
    for item in module.items:
        sym = _item_to_symbol(item, src)
        if sym is not None:
            symbols.append(sym)
    return symbols


def _item_to_symbol(item: A.Item, src: str) -> Optional[lsp.DocumentSymbol]:
    if isinstance(item, A.FnDef):
        kind = lsp.SymbolKind.Function
        detail = _format_fn_signature(item)
        name = item.name
    elif isinstance(item, A.RecordDef):
        kind = lsp.SymbolKind.Struct
        detail = f"record ({len(item.fields)} fields)"
        name = item.name
    elif isinstance(item, A.ErrorDef):
        kind = lsp.SymbolKind.Class
        detail = f"error ({len(item.fields)} fields)"
        name = item.name
    elif isinstance(item, A.ImportStmt):
        kind = lsp.SymbolKind.Module
        detail = "import"
        name = item.path
    else:
        return None
    rng = _range_from_loc(item.loc, src)
    children: list[lsp.DocumentSymbol] = []
    if isinstance(item, A.RecordDef) or isinstance(item, A.ErrorDef):
        for f in item.fields:
            children.append(
                lsp.DocumentSymbol(
                    name=f.name,
                    detail=_format_type(f.type_ref),
                    kind=lsp.SymbolKind.Field,
                    range=_range_from_loc(f.loc, src),
                    selection_range=_range_from_loc(f.loc, src),
                )
            )
    return lsp.DocumentSymbol(
        name=name,
        detail=detail,
        kind=kind,
        range=rng,
        selection_range=rng,
        children=children or None,
    )


# ---------------------------------------------------------------------------
# Hover
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def on_hover(params: lsp.HoverParams) -> Optional[lsp.Hover]:
    uri = params.text_document.uri
    module = _cache.get(uri)
    if module is None:
        return None
    doc = server.workspace.get_text_document(uri)
    src = doc.source
    word, word_range = _word_at(src, params.position)
    if word is None:
        return None
    info = _resolve_symbol(module, word)
    if info is None:
        return None
    return lsp.Hover(
        contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=info),
        range=word_range,
    )


def _next_ident_on_line(
    src: str, pos: lsp.Position,
) -> tuple[Optional[str], Optional[lsp.Range]]:
    """Scan forward from `pos` to the next IDENT on the same line.
    Used as a fallback for Find Usages / Rename when the cursor
    landed on a keyword (`fn`, `record`, etc.) — we want the
    declared name, not the keyword."""
    lines = src.splitlines()
    if pos.line >= len(lines):
        return (None, None)
    line = lines[pos.line]
    col = min(pos.character, len(line))
    import re
    m = re.search(r"[A-Za-z_][A-Za-z0-9_]*", line[col:])
    if m is None:
        return (None, None)
    abs_start = col + m.start()
    abs_end = col + m.end()
    return (
        line[abs_start:abs_end],
        lsp.Range(
            start=lsp.Position(line=pos.line, character=abs_start),
            end=lsp.Position(line=pos.line, character=abs_end),
        ),
    )


def _word_at(src: str, pos: lsp.Position) -> tuple[Optional[str], Optional[lsp.Range]]:
    lines = src.splitlines()
    if pos.line >= len(lines):
        return (None, None)
    line = lines[pos.line]
    col = min(pos.character, len(line))
    # find word boundaries
    left = col
    while left > 0 and (line[left - 1].isalnum() or line[left - 1] == "_"):
        left -= 1
    right = col
    while right < len(line) and (line[right].isalnum() or line[right] == "_"):
        right += 1
    if left == right:
        return (None, None)
    return (
        line[left:right],
        lsp.Range(
            start=lsp.Position(line=pos.line, character=left),
            end=lsp.Position(line=pos.line, character=right),
        ),
    )


def _resolve_symbol(module: A.Module, name: str) -> Optional[str]:
    """Look up `name` as a top-level item in the module. Returns Markdown
    describing the declaration if found, else None."""
    for item in module.items:
        if isinstance(item, A.FnDef) and item.name == name:
            return f"```pell\n{_format_fn_signature(item)}\n```\n\n*function in `{module.name}`*"
        if isinstance(item, A.RecordDef) and item.name == name:
            field_lines = "\n".join(
                f"  {f.name}: {_format_type(f.type_ref)}," for f in item.fields
            )
            return f"```pell\nrecord {item.name} {{\n{field_lines}\n}}\n```"
        if isinstance(item, A.ErrorDef) and item.name == name:
            if item.fields:
                field_lines = "\n".join(
                    f"  {f.name}: {_format_type(f.type_ref)}," for f in item.fields
                )
                return f"```pell\nerror {item.name} {{\n{field_lines}\n}}\n```"
            return f"```pell\nerror {item.name};\n```\n\n*zero-payload error*"
    return None


def _format_fn_signature(fn: A.FnDef) -> str:
    pub = "pub " if fn.is_pub else ""
    params = ", ".join(f"{p.name}: {_format_type(p.type_ref)}" for p in fn.params)
    ret = ""
    if fn.return_type is not None:
        ret = f" -> {_format_type(fn.return_type)}"
    annotations = "\n".join(f"@{a.name}" for a in fn.annotations)
    if annotations:
        annotations += "\n"
    return f"{annotations}{pub}fn {fn.name}({params}){ret}"


def _format_type(t: A.TypeRef) -> str:
    if isinstance(t, A.PrimType):
        return t.name
    if isinstance(t, A.NamedType):
        return t.name
    if isinstance(t, A.OptionalType):
        return _format_type(t.inner) + "?"
    if isinstance(t, A.GenericType):
        params = ", ".join(_format_type(p) for p in t.params)
        return f"{t.base}<{params}>"
    if isinstance(t, A.ErrorUnionType):
        return " | ".join(_format_type(v) for v in t.variants)
    return "?"


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

# Triggered after `.`, `@`, `(`.
COMPLETION_TRIGGER_CHARS = [".", "@", ":"]


KEYWORDS = [
    "module", "import", "pub", "fn", "let", "var", "return", "yield",
    "if", "else", "for", "forall", "in", "match", "transaction",
    "record", "error", "true", "false", "Some", "None", "Ok", "Err",
    "finally", "unsafe",
]

ANNOTATIONS = [
    ("deterministic", "Function is deterministic — Oracle's DETERMINISTIC clause"),
    ("result_cache", "Function result cache — RESULT_CACHE clause"),
    ("udf", "User-defined function pragma — fewer SQL ↔ PL/SQL context switches"),
    ("autonomous", "Autonomous transaction context"),
    ("pipelined", "Pipelined table function — uses PIPE ROW"),
    ("test", "Marks a function as a unit test"),
    ("test(db)", "Test that needs a configured DB"),
    ("deprecated", "Deprecation marker; shows warning at call sites"),
    ("must_use", "Caller must bind, match, or `?` the return value"),
]


METHODS_ON_SQL_READ = [
    ("one", "`Result<T, NotFound | TooMany>` — exactly one row"),
    ("one_or_none", "`Result<Option<T>, TooMany>` — at-most-one row"),
    ("first", "`Option<T>` — first row or None"),
    ("collect", "`list<T>` — BULK COLLECT INTO all rows"),
    ("for_update", "Add `FOR UPDATE` locking clause"),
    ("for_update_of", "Add `FOR UPDATE OF cols` locking clause"),
]


METHODS_ON_RESULT = [
    ("if_empty", "`.if_empty(E)` — substitute error variant E for NotFound"),
    ("if_many", "`.if_many(E)` — substitute error variant E for TooMany"),
]


METHODS_ON_DML = [
    ("returning", "`.returning::<T>()` — capture RETURNING INTO"),
    ("rowcount", "`.rowcount()` — rows affected via SQL%ROWCOUNT"),
]


METHODS_ON_LIST = [
    ("len", "Number of elements (`xs.COUNT`)"),
    ("first", "First index (`xs.FIRST`)"),
    ("last", "Last index (`xs.LAST`)"),
    ("at", "Element at index"),
    ("indices", "Iterate the index range FIRST .. LAST"),
]


METHODS_ON_BULK = [
    ("rowcount", "`bulk.rowcount(i)` — rows affected on iteration i"),
    ("total", "`bulk.total()` — SQL%ROWCOUNT total"),
]


METHODS_ON_TEXT = [
    ("contains",    "`.contains(t)` — `INSTR(s, t) > 0`"),
    ("starts_with", "`.starts_with(t)` — `s LIKE t || '%'`"),
    ("ends_with",   "`.ends_with(t)` — `s LIKE '%' || t`"),
    ("is_empty",    "`.is_empty()` — null-or-empty check"),
    ("split",       "`.split(delim)` — returns `list<text>` via REGEXP_SUBSTR"),
    ("length",      "`.length()` — `LENGTH(s)` (Oracle pass-through)"),
    ("upper",       "`.upper()` — `UPPER(s)` (Oracle pass-through)"),
    ("lower",       "`.lower()` — `LOWER(s)` (Oracle pass-through)"),
    ("trim",        "`.trim()` — `TRIM(s)` (Oracle pass-through)"),
    ("substr",      "`.substr(start, len?)` — `SUBSTR(s, …)` (Oracle pass-through)"),
]


METHODS_ON_DATE = [
    ("year",   "`.year()` — `EXTRACT(YEAR FROM d)`"),
    ("month",  "`.month()` — `EXTRACT(MONTH FROM d)`"),
    ("day",    "`.day()` — `EXTRACT(DAY FROM d)`"),
    ("hour",   "`.hour()` — `EXTRACT(HOUR FROM ts)` (timestamp only)"),
    ("minute", "`.minute()` — `EXTRACT(MINUTE FROM ts)`"),
    ("second", "`.second()` — `EXTRACT(SECOND FROM ts)`"),
    ("add_months", "`.add_months(n)` — `ADD_MONTHS(d, n)` (Oracle pass-through)"),
]


@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
    lsp.CompletionOptions(trigger_characters=COMPLETION_TRIGGER_CHARS),
)
def on_completion(params: lsp.CompletionParams) -> lsp.CompletionList:
    uri = params.text_document.uri
    doc = server.workspace.get_text_document(uri)
    src = doc.source
    lines = src.splitlines()
    if params.position.line >= len(lines):
        return lsp.CompletionList(is_incomplete=False, items=[])
    line = lines[params.position.line][: params.position.character]
    items = list(_completions_for_line_prefix(
        line, src, uri, params.position.line, params.position.character,
    ))
    return lsp.CompletionList(is_incomplete=False, items=items)


def _completions_for_line_prefix(
    line: str, src: str, uri: str,
    line_no: int = 0, char_no: int = 0,
) -> list[lsp.CompletionItem]:
    # `@` context — annotation list
    if line.rstrip().endswith("@"):
        return [
            lsp.CompletionItem(
                label=name,
                kind=lsp.CompletionItemKind.Property,
                detail=detail,
                documentation=lsp.MarkupContent(
                    kind=lsp.MarkupKind.Markdown, value=detail
                ),
            )
            for name, detail in ANNOTATIONS
        ]

    # `.` context — generic method menu (we don't yet type-check the
    # receiver). Matches both "trailing dot" and "dot + partial word"
    # (the latter happens when the IDE re-queries as the user types
    # more letters). When some chars have already been typed after the
    # `.`, we emit an explicit text_edit range so the client knows to
    # REPLACE just those chars, not guess at the word boundary (which
    # can run all the way back to file start if the receiver contains
    # underscores or other word-chars the client doesn't recognize).
    dot_match = re.search(r"\.(\w*)$", line)
    if dot_match is not None:
        # If the immediate context is `bulk.<chars>`, use the bulk methods
        if re.search(r"\bbulk\s*\.\w*$", line):
            methods = METHODS_ON_BULK
        else:
            methods = (
                METHODS_ON_SQL_READ + METHODS_ON_RESULT + METHODS_ON_DML
                + METHODS_ON_LIST + METHODS_ON_TEXT + METHODS_ON_DATE
            )
        # The replacement range covers the partial word AFTER the dot.
        partial_len = len(dot_match.group(1))
        replace_start = lsp.Position(line=line_no, character=char_no - partial_len)
        replace_end = lsp.Position(line=line_no, character=char_no)
        return [
            lsp.CompletionItem(
                label=name + "()",
                kind=lsp.CompletionItemKind.Method,
                detail=detail,
                filter_text=name,  # so the IDE filters on the bare name as user types
                text_edit=lsp.TextEdit(
                    range=lsp.Range(start=replace_start, end=replace_end),
                    new_text=f"{name}()",
                ),
            )
            for name, detail in methods
        ]

    # `pkg::partial` context — restrict to that package's exposed
    # surface (pub fns + pub records). Triggers on both ":" and "::"
    # since the IDE sometimes re-queries mid-word.
    qual_match = re.search(r"(\w+)::(\w*)$", line)
    if qual_match is not None:
        pkg, partial = qual_match.group(1), qual_match.group(2)
        # Derive a filesystem path from the URI. `file:///foo` → `/foo`.
        doc_path = uri[len("file://"):] if uri.startswith("file://") else uri
        sigs, recs, _defs, _refs = _get_project_ctx(doc_path)
        partial_len = len(partial)
        replace_start = lsp.Position(
            line=line_no, character=char_no - partial_len
        )
        replace_end = lsp.Position(line=line_no, character=char_no)
        out: list[lsp.CompletionItem] = []
        # Fns from project_signatures keyed by `pkg::name`.
        prefix = f"{pkg}::"
        for key, ret_type in sigs.items():
            if not key.startswith(prefix):
                continue
            fname = key[len(prefix):]
            if "::" in fname:
                continue  # deeper qualification (foo::bar::baz) — skip
            out.append(lsp.CompletionItem(
                label=fname,
                kind=lsp.CompletionItemKind.Function,
                detail=f"fn -> {_render_type(ret_type)}",
                filter_text=fname,
                text_edit=lsp.TextEdit(
                    range=lsp.Range(start=replace_start, end=replace_end),
                    new_text=fname,
                ),
            ))
        # Records from project_records keyed by (pkg, name).
        for (rec_pkg, rec_name) in recs.keys():
            if rec_pkg != pkg:
                continue
            out.append(lsp.CompletionItem(
                label=rec_name,
                kind=lsp.CompletionItemKind.Struct,
                detail="record",
                filter_text=rec_name,
                text_edit=lsp.TextEdit(
                    range=lsp.Range(start=replace_start, end=replace_end),
                    new_text=rec_name,
                ),
            ))
        return out

    # No trigger — keyword and in-scope identifier completions
    items: list[lsp.CompletionItem] = []
    items.extend(
        lsp.CompletionItem(label=kw, kind=lsp.CompletionItemKind.Keyword)
        for kw in KEYWORDS
    )
    module = _cache.get(uri)
    if module is not None:
        for item in module.items:
            if isinstance(item, A.FnDef):
                items.append(
                    lsp.CompletionItem(
                        label=item.name,
                        kind=lsp.CompletionItemKind.Function,
                        detail=_format_fn_signature(item),
                    )
                )
            elif isinstance(item, A.RecordDef):
                items.append(
                    lsp.CompletionItem(
                        label=item.name,
                        kind=lsp.CompletionItemKind.Struct,
                        detail=f"record",
                    )
                )
            elif isinstance(item, A.ErrorDef):
                items.append(
                    lsp.CompletionItem(
                        label=item.name,
                        kind=lsp.CompletionItemKind.Class,
                        detail="error",
                    )
                )
    return items


# ---------------------------------------------------------------------------
# Go-to-definition (within file only for v0)
# ---------------------------------------------------------------------------


def _name_range_for_item(item: "A.Item", src: str) -> lsp.Range:
    """Resolve the range covering an item's NAME (not its leading
    keyword). item.loc points at `fn`/`record`/`error`; we scan
    forward on that line for the identifier so goto-def lands the
    cursor on the symbol — required so a follow-up Find Usages at
    that position resolves correctly."""
    import re
    name = getattr(item, "name", None)
    lines = src.splitlines()
    line_idx = item.loc.line - 1
    if not name or line_idx < 0 or line_idx >= len(lines):
        return _range_from_loc(item.loc, src)
    line = lines[line_idx]
    start_col = max(item.loc.col - 1, 0)
    m = re.search(r"\b" + re.escape(name) + r"\b", line[start_col:])
    if m is None:
        return _range_from_loc(item.loc, src)
    name_col = start_col + m.start()
    return lsp.Range(
        start=lsp.Position(line=line_idx, character=name_col),
        end=lsp.Position(line=line_idx, character=name_col + len(name)),
    )


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def on_definition(params: lsp.DefinitionParams) -> Optional[lsp.Location]:
    uri = params.text_document.uri
    module = _cache.get(uri)
    doc = server.workspace.get_text_document(uri)
    src = doc.source
    word, _ = _word_at(src, params.position)
    if word is None:
        return None
    # Local-module lookup first — same-file goto stays fastest.
    if module is not None:
        for item in module.items:
            if isinstance(item, (A.FnDef, A.RecordDef, A.ErrorDef)) \
                    and item.name == word:
                return lsp.Location(
                    uri=uri,
                    range=_name_range_for_item(item, src),
                )
    # Cross-file: extract the qualifier (if any) and consult the
    # project definitions index. For bare `name`, we look up every
    # registered (pkg, name) and return the unique match if one
    # exists — same conservative bare-name rule as the emitter.
    pkg = _qualifier_before(src, params.position)
    _, _, defs, _ = _get_project_ctx(doc.path)
    if pkg is not None:
        loc = defs.get((pkg, word))
        if loc is not None:
            return _location_from_loc(loc)
    else:
        matches = [v for k, v in defs.items() if k[1] == word]
        if len(matches) == 1:
            return _location_from_loc(matches[0])
    return None


def _qualifier_before(src: str, pos: lsp.Position) -> Optional[str]:
    """If the position sits inside `<qualifier>::<word>`, return the
    qualifier (lowered to PL/SQL-safe form). Otherwise None."""
    lines = src.splitlines()
    if pos.line >= len(lines):
        return None
    line = lines[pos.line]
    col = min(pos.character, len(line))
    # Walk left to the start of the current word.
    left = col
    while left > 0 and (line[left - 1].isalnum() or line[left - 1] == "_"):
        left -= 1
    # Expect `::` immediately before that.
    if left < 2 or line[left - 2:left] != "::":
        return None
    # Walk further left to capture the qualifier word.
    qend = left - 2
    qstart = qend
    while qstart > 0 and (line[qstart - 1].isalnum() or line[qstart - 1] == "_"):
        qstart -= 1
    if qstart == qend:
        return None
    return line[qstart:qend].replace("::", "_").replace(".", "_")


def _location_from_loc(loc: "A.Loc") -> lsp.Location:
    """Build an LSP Location from a pell Loc, reading the file lazily
    to convert (line, col) → a precise Range covering the symbol."""
    from pathlib import Path as _Path
    file_path = loc.file
    file_uri = f"file://{file_path}"
    try:
        src = _Path(file_path).read_text(encoding="utf-8")
    except Exception:
        src = ""
    return lsp.Location(uri=file_uri, range=_range_from_loc(loc, src))


# ---------------------------------------------------------------------------
# Find Usages — cross-project references to a `pkg::name` symbol.
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_REFERENCES)
def on_references(
    params: lsp.ReferenceParams,
) -> Optional[list[lsp.Location]]:
    uri = params.text_document.uri
    doc = server.workspace.get_text_document(uri)
    src = doc.source
    word, _ = _word_at(src, params.position)
    # If the cursor landed on a keyword (`fn`, `record`, etc.), scan
    # forward to the actual declaration name. IntelliJ jumps cursor to
    # the goto-def result range and then invokes Find Usages there;
    # before the scan_project_definitions name-loc fix shipped, the
    # cursor would land on the keyword. Keep the fallback for users
    # who manually click on a keyword.
    _KEYWORDS = {"fn", "pub", "record", "error", "aggregate", "type",
                 "enum", "sealed", "seq"}
    if word in _KEYWORDS:
        word, _ = _next_ident_on_line(src, params.position)
    if word is None:
        return None
    pkg = _qualifier_before(src, params.position)
    _, _, defs, refs = _get_project_ctx(doc.path)
    # If no explicit qualifier, look up the package via the
    # definitions index — if the symbol is uniquely owned by one
    # package, use that. Otherwise we can't reliably distinguish.
    if pkg is None:
        owners = [k[0] for k in defs.keys() if k[1] == word]
        if len(set(owners)) != 1:
            return None
        pkg = owners[0]
    sites = list(refs.get((pkg, word), []))
    # Bare-call references — only safe to include when the symbol is
    # unique by name across the project (otherwise we'd attribute calls
    # to the wrong fn). Bare-call scanner over-captures: it matches
    # locals, params, even keywords like `if (...)`. Filtering against
    # known def names (which include kind discrimination via _is_pub
    # at scan time) keeps the noise down.
    owners_for_name = {k[0] for k in defs.keys() if k[1] == word}
    if len(owners_for_name) == 1:
        sites.extend(refs.get((None, word), []))
    # Optionally include the declaration site too — most IDEs expect it.
    if params.context.include_declaration:
        decl = defs.get((pkg, word))
        if decl is not None:
            sites.append(decl)
    # Deduplicate by (file, line, col) — bare and qualified scanners
    # can both hit the same source position when a qualifier sits on
    # the same line.
    seen_keys: set[tuple[str, int, int]] = set()
    out: list[lsp.Location] = []
    for loc in sites:
        key = (loc.file, loc.line, loc.col)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(_location_from_loc(loc))
    return out or None


# ---------------------------------------------------------------------------
# Semantic tokens (highlighting)
# ---------------------------------------------------------------------------


_LEGEND = lsp.SemanticTokensLegend(
    token_types=_semtok.TOKEN_TYPES,
    token_modifiers=_semtok.TOKEN_MODIFIERS,
)


@server.feature(
    lsp.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    lsp.SemanticTokensRegistrationOptions(legend=_LEGEND, full=True),
)
def on_semantic_tokens_full(params: lsp.SemanticTokensParams) -> lsp.SemanticTokens:
    doc = server.workspace.get_text_document(params.text_document.uri)
    return lsp.SemanticTokens(data=_semtok.compute(doc.source))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    # Surface the running version on stderr so the IDE's LSP console
    # banner shows which build is actually attached. Easier than
    # squinting at the initialize trace's serverInfo JSON.
    import sys, subprocess, os
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        sha = "unknown"
    print(
        f"[{SERVER_NAME} v{SERVER_VERSION} @ {sha}] starting on stdio",
        file=sys.stderr, flush=True,
    )
    logging.basicConfig(level=logging.WARNING)
    server.start_io()


if __name__ == "__main__":
    main()
