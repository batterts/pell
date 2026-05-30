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
from pell.emitter import EmitError, lower_type
from pell.lexer import LexError
from pell.parser import ParseError, parse

from . import semantic_tokens as _semtok


SERVER_NAME = "pell-lsp"
SERVER_VERSION = "0.0.1"

logger = logging.getLogger(SERVER_NAME)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = LanguageServer(SERVER_NAME, SERVER_VERSION)

# Cache of parsed modules per URI — lets hover/symbols/completion reuse the AST.
_cache: dict[str, Optional[A.Module]] = {}


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
    _validate(params.text_document.uri)


def _validate(uri: str) -> None:
    doc = server.workspace.get_text_document(uri)
    src = doc.source
    diagnostics: list[lsp.Diagnostic] = []
    module: Optional[A.Module] = None
    try:
        module = parse(src, doc.path or uri)
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
    if loc is None:
        return lsp.Diagnostic(
            range=_one_char_range(0, 0),
            message=msg,
            severity=lsp.DiagnosticSeverity.Error,
            source=SERVER_NAME,
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


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def on_definition(params: lsp.DefinitionParams) -> Optional[lsp.Location]:
    uri = params.text_document.uri
    module = _cache.get(uri)
    if module is None:
        return None
    doc = server.workspace.get_text_document(uri)
    src = doc.source
    word, _ = _word_at(src, params.position)
    if word is None:
        return None
    for item in module.items:
        if isinstance(item, (A.FnDef, A.RecordDef, A.ErrorDef)) and item.name == word:
            return lsp.Location(uri=uri, range=_range_from_loc(item.loc, src))
    return None


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
    logging.basicConfig(level=logging.WARNING)
    server.start_io()


if __name__ == "__main__":
    main()
