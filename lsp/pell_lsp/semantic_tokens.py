"""Semantic token computation for pell-lsp.

Scans the source character-by-character to classify keywords, identifiers,
numbers, strings, comments, `sql!{...}` blocks, decorators, and operators.
A post-pass promotes the identifier after `fn`/`record`/`error`/`import`
keywords to its appropriate semantic kind.

Operates directly on source text — robust to parse failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


# LSP semantic-token legend. Indices below MUST match this order.
TOKEN_TYPES: list[str] = [
    "keyword",       # 0
    "function",      # 1
    "type",          # 2  pell records
    "class",         # 3  pell errors
    "parameter",     # 4
    "variable",      # 5  default for unclassified idents
    "property",      # 6  record/error fields
    "string",        # 7
    "number",        # 8
    "comment",       # 9
    "decorator",     # 10 annotations: @deterministic etc.
    "operator",      # 11 multi-char operators only
    "namespace",     # 12 module path in `import`
    "macro",         # 13 sql!{ ... }
]

TOKEN_MODIFIERS: list[str] = [
    "declaration",   # bit 0
    "definition",    # bit 1
    "readonly",      # bit 2
    "static",        # bit 3
    "deprecated",    # bit 4
]


_T_KEYWORD = 0
_T_FUNCTION = 1
_T_TYPE = 2
_T_CLASS = 3
_T_PARAMETER = 4
_T_VARIABLE = 5
_T_PROPERTY = 6
_T_STRING = 7
_T_NUMBER = 8
_T_COMMENT = 9
_T_DECORATOR = 10
_T_OPERATOR = 11
_T_NAMESPACE = 12
_T_MACRO = 13

_MOD_DECLARATION = 1 << 0


# Keywords (mirrors lexer.KEYWORDS).
_KEYWORDS = {
    "module", "import", "pub", "fn", "let", "var", "return", "yield",
    "if", "else", "for", "forall", "in", "match", "transaction",
    "record", "error", "true", "false", "Some", "None", "Ok", "Err",
    "unsafe", "finally", "and", "or", "not",
    "type", "sealed", "aggregate", "case", "self",
    "seq",
    "out", "inout",
}

# Pell built-in primitive types — render as `type`, not `variable`.
_PRIM_TYPES = {
    "number", "int", "text", "bool", "date", "timestamp", "bytes", "json",
    "Unit", "Option", "Result", "list", "map", "set", "cursor", "stream",
    "rowtype",
}

# Oracle SQL keywords (case-insensitive). Highlighted inside sql!{ ... } blocks.
_SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "in", "exists", "between",
    "like", "is", "null", "as", "distinct", "all", "any", "some",
    "insert", "into", "values", "update", "set", "delete",
    "merge", "using", "matched", "returning",
    "join", "inner", "outer", "left", "right", "full", "cross", "on",
    "group", "by", "order", "having", "asc", "desc", "nulls", "first", "last",
    "union", "intersect", "minus",
    "create", "drop", "alter", "truncate", "table", "view", "index",
    "with", "recursive",
    "case", "when", "then", "else", "end",
    "fetch", "next", "only", "rows", "offset",
    "for", "of", "nowait", "skip", "locked", "update",
    "begin", "commit", "rollback", "savepoint",
    "cursor", "open", "close", "loop", "exit",
    "if", "elsif", "while",
    "pipelined", "deterministic", "result_cache",
    "function", "procedure", "package", "body", "trigger",
    "return", "exception", "raise", "pragma",
    "bulk", "collect", "forall", "limit",
    "out", "inout",
    "true", "false",
}

# Multi-char operators (longest first).
_MULTI_OPS = ("..=", "::", "->", "=>", "==", "!=", "<=", ">=", "&&", "||", "..", "|>")


@dataclass
class _Tok:
    line: int       # 0-based
    col: int        # 0-based, in this line
    length: int     # in chars on this line (multi-line tokens split)
    type_idx: int
    mods: int = 0
    text: str = ""  # only populated for identifiers/keywords; used by post-pass


class _Scanner:
    """Char-by-char scanner. Tracks line/col so each emitted token has accurate position."""

    def __init__(self, src: str):
        self.src = src
        self.n = len(src)
        self.i = 0
        self.line = 0
        self.col = 0

    def advance(self, k: int = 1) -> None:
        for _ in range(k):
            if self.i >= self.n:
                return
            if self.src[self.i] == "\n":
                self.line += 1
                self.col = 0
            else:
                self.col += 1
            self.i += 1

    def starts(self, s: str) -> bool:
        return self.src.startswith(s, self.i)

    def peek(self, off: int = 0) -> str:
        p = self.i + off
        return self.src[p] if p < self.n else ""

    def scan(self) -> list[_Tok]:
        out: list[_Tok] = []
        while self.i < self.n:
            ch = self.src[self.i]
            # whitespace
            if ch in " \t\r\n":
                self.advance()
                continue
            # line comment
            if self.starts("//"):
                sl, sc = self.line, self.col
                start = self.i
                while self.i < self.n and self.src[self.i] != "\n":
                    self.advance()
                out.append(_Tok(sl, sc, self.i - start, _T_COMMENT))
                continue
            # block comment
            if self.starts("/*"):
                sl, sc = self.line, self.col
                start = self.i
                self.advance(2)
                while self.i < self.n and not self.starts("*/"):
                    self.advance()
                if self.i < self.n:
                    self.advance(2)
                self._emit_multiline(out, sl, sc, self.src[start : self.i], _T_COMMENT)
                continue
            # string literal
            if ch == '"':
                sl, sc = self.line, self.col
                start = self.i
                self.advance()
                while self.i < self.n and self.src[self.i] != '"':
                    if self.src[self.i] == "\\" and self.i + 1 < self.n:
                        self.advance(2)
                        continue
                    self.advance()
                if self.i < self.n:
                    self.advance()  # closing "
                # strings shouldn't span lines normally, but handle gracefully
                self._emit_multiline(out, sl, sc, self.src[start : self.i], _T_STRING)
                continue
            # sql!{ ... } block
            if self.starts("sql!"):
                j = self.i + 4
                while j < self.n and self.src[j] in " \t":
                    j += 1
                if j < self.n and self.src[j] == "{":
                    # Emit `sql!` marker as macro.
                    sl, sc = self.line, self.col
                    out.append(_Tok(sl, sc, 4, _T_MACRO))
                    self.advance(4)
                    while self.i < self.n and self.src[self.i] in " \t":
                        self.advance()
                    self.advance()  # opening {
                    # Walk the SQL body, emitting per-element tokens.
                    self._scan_sql_body(out)
                    if self.i < self.n:
                        self.advance()  # closing }
                    continue
            # decorator: @name
            if ch == "@":
                sl, sc = self.line, self.col
                start = self.i
                self.advance()
                while self.i < self.n and (self.src[self.i].isalnum() or self.src[self.i] == "_"):
                    self.advance()
                length = self.i - start
                if length > 1:
                    out.append(_Tok(sl, sc, length, _T_DECORATOR))
                continue
            # identifier or keyword
            if ch.isalpha() or ch == "_":
                sl, sc = self.line, self.col
                start = self.i
                while self.i < self.n and (self.src[self.i].isalnum() or self.src[self.i] == "_"):
                    self.advance()
                text = self.src[start : self.i]
                if text in _KEYWORDS:
                    out.append(_Tok(sl, sc, len(text), _T_KEYWORD, text=text))
                else:
                    out.append(_Tok(sl, sc, len(text), _T_VARIABLE, text=text))
                continue
            # number
            if ch.isdigit():
                sl, sc = self.line, self.col
                start = self.i
                while self.i < self.n and self.src[self.i].isdigit():
                    self.advance()
                if self.i < self.n and self.src[self.i] == "." and self.i + 1 < self.n and self.src[self.i + 1].isdigit():
                    self.advance()
                    while self.i < self.n and self.src[self.i].isdigit():
                        self.advance()
                out.append(_Tok(sl, sc, self.i - start, _T_NUMBER))
                continue
            # multi-char operators
            matched = False
            for op in _MULTI_OPS:
                if self.starts(op):
                    sl, sc = self.line, self.col
                    self.advance(len(op))
                    out.append(_Tok(sl, sc, len(op), _T_OPERATOR))
                    matched = True
                    break
            if matched:
                continue
            # single-char punctuation — skip (no semantic class)
            self.advance()
        return out

    def _emit_multiline(self, out: list[_Tok], sl: int, sc: int, text: str, type_idx: int) -> None:
        """Split a multi-line span into one _Tok per line (LSP requires it)."""
        parts = text.split("\n")
        for li, part in enumerate(parts):
            if li == 0:
                out.append(_Tok(sl, sc, len(part), type_idx))
            elif part:
                out.append(_Tok(sl + li, 0, len(part), type_idx))

    def _scan_sql_body(self, out: list[_Tok]) -> None:
        """Walk inside `sql!{...}` body emitting per-element semantic tokens.

        Tracks brace depth so `{` inside SQL strings doesn't terminate prematurely;
        stops at the matching `}` and leaves position there (caller consumes it).
        """
        depth = 1
        while self.i < self.n and depth > 0:
            ch = self.src[self.i]
            if ch in " \t\r\n":
                self.advance()
                continue
            if ch == "{":
                depth += 1
                self.advance()
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    return
                self.advance()
                continue
            # SQL line comment
            if self.starts("--"):
                sl, sc = self.line, self.col
                start = self.i
                while self.i < self.n and self.src[self.i] != "\n":
                    self.advance()
                out.append(_Tok(sl, sc, self.i - start, _T_COMMENT))
                continue
            # SQL block comment
            if self.starts("/*"):
                sl, sc = self.line, self.col
                start = self.i
                self.advance(2)
                while self.i < self.n and not self.starts("*/"):
                    self.advance()
                if self.i < self.n:
                    self.advance(2)
                self._emit_multiline(out, sl, sc, self.src[start : self.i], _T_COMMENT)
                continue
            # SQL single-quoted string
            if ch == "'":
                sl, sc = self.line, self.col
                start = self.i
                self.advance()
                while self.i < self.n and self.src[self.i] != "'":
                    if self.src[self.i] == "\\" and self.i + 1 < self.n:
                        self.advance(2)
                        continue
                    self.advance()
                if self.i < self.n:
                    self.advance()  # closing '
                self._emit_multiline(out, sl, sc, self.src[start : self.i], _T_STRING)
                continue
            # SQL double-quoted identifier
            if ch == '"':
                sl, sc = self.line, self.col
                start = self.i
                self.advance()
                while self.i < self.n and self.src[self.i] != '"':
                    self.advance()
                if self.i < self.n:
                    self.advance()
                out.append(_Tok(sl, sc, self.i - start, _T_VARIABLE))
                continue
            # :bind variables — Oracle parameter binding
            if ch == ":" and self.i + 1 < self.n and (self.src[self.i + 1].isalpha() or self.src[self.i + 1] == "_"):
                sl, sc = self.line, self.col
                start = self.i
                self.advance()  # the ":"
                while self.i < self.n and (self.src[self.i].isalnum() or self.src[self.i] == "_"):
                    self.advance()
                out.append(_Tok(sl, sc, self.i - start, _T_PARAMETER))
                continue
            # Numbers
            if ch.isdigit():
                sl, sc = self.line, self.col
                start = self.i
                while self.i < self.n and self.src[self.i].isdigit():
                    self.advance()
                if self.i < self.n and self.src[self.i] == "." and self.i + 1 < self.n and self.src[self.i + 1].isdigit():
                    self.advance()
                    while self.i < self.n and self.src[self.i].isdigit():
                        self.advance()
                out.append(_Tok(sl, sc, self.i - start, _T_NUMBER))
                continue
            # Identifiers — keywords are case-insensitive in SQL
            if ch.isalpha() or ch == "_":
                sl, sc = self.line, self.col
                start = self.i
                while self.i < self.n and (self.src[self.i].isalnum() or self.src[self.i] == "_"):
                    self.advance()
                text = self.src[start : self.i]
                if text.lower() in _SQL_KEYWORDS:
                    out.append(_Tok(sl, sc, len(text), _T_KEYWORD))
                else:
                    out.append(_Tok(sl, sc, len(text), _T_VARIABLE))
                continue
            # Punctuation / operators inside SQL — emit a single operator token
            # for multi-char Oracle operators, skip single-char.
            if self.starts("||") or self.starts("=>"):
                sl, sc = self.line, self.col
                self.advance(2)
                out.append(_Tok(sl, sc, 2, _T_OPERATOR))
                continue
            self.advance()


def _classify_followers(toks: list[_Tok]) -> None:
    """Upgrade identifiers that follow declaration keywords.

    - `fn NAME` → NAME becomes FUNCTION (declaration)
    - `record NAME` → NAME becomes TYPE (declaration)
    - `error NAME` → NAME becomes CLASS (declaration)
    - `import a::b::c;` → each dotted/coloned segment after `import` is NAMESPACE
    """
    n = len(toks)
    i = 0
    while i < n:
        t = toks[i]
        if t.type_idx == _T_KEYWORD and t.text == "fn":
            j = _next_variable(toks, i + 1)
            if j is not None:
                toks[j].type_idx = _T_FUNCTION
                toks[j].mods |= _MOD_DECLARATION
        elif t.type_idx == _T_KEYWORD and t.text == "record":
            j = _next_variable(toks, i + 1)
            if j is not None:
                toks[j].type_idx = _T_TYPE
                toks[j].mods |= _MOD_DECLARATION
        elif t.type_idx == _T_KEYWORD and t.text == "error":
            j = _next_variable(toks, i + 1)
            if j is not None:
                toks[j].type_idx = _T_CLASS
                toks[j].mods |= _MOD_DECLARATION
        elif t.type_idx == _T_KEYWORD and t.text == "import":
            # Mark all subsequent VARIABLE tokens until end-of-line or ';' as namespace.
            base_line = t.line
            j = i + 1
            while j < n and toks[j].line == base_line:
                if toks[j].type_idx == _T_VARIABLE:
                    toks[j].type_idx = _T_NAMESPACE
                j += 1
        i += 1


def _next_variable(toks: list[_Tok], start: int) -> int | None:
    """Find the next VARIABLE-classified token; return its index or None."""
    for j in range(start, len(toks)):
        if toks[j].type_idx == _T_VARIABLE:
            return j
        # If we hit punctuation (line continues), keep looking. Only stop if
        # we cross a line break with no IDENT yet — that means the keyword
        # was malformed (e.g., `fn\n`).
        if toks[j].line > toks[start].line:
            return None
    return None


def _classify_contextual(src: str, toks: list[_Tok]) -> None:
    """Promote variables to parameter/type/namespace/function based on neighboring punctuation.

    Inspects the raw source character immediately after each VARIABLE token (skipping spaces):
      - `:` → the variable is in a "name :" position → parameter
      - `(` → call site → function
      - `::` → qualifier → namespace
    Also promotes primitive type names (number, text, Result, …) to type unconditionally.
    """
    src_lines = src.split("\n")
    for t in toks:
        if t.type_idx != _T_VARIABLE:
            continue
        # Built-in primitive types — always type.
        text = _slice(src_lines, t)
        if text in _PRIM_TYPES:
            t.type_idx = _T_TYPE
            continue
        # Look at the next non-whitespace char on the same line.
        after = _peek_after(src_lines, t)
        if after == ":" and not _starts(src_lines, t, "::"):
            t.type_idx = _T_PARAMETER
        elif after == "(":
            t.type_idx = _T_FUNCTION
        elif _starts(src_lines, t, "::"):
            t.type_idx = _T_NAMESPACE
        else:
            # Look BEFORE this token: if preceded by `:` or `->`, it's a type position.
            before = _peek_before(src_lines, t)
            if before in (":", ">"):  # ">" covers "->"
                t.type_idx = _T_TYPE


def _slice(lines: list[str], t: _Tok) -> str:
    if t.line >= len(lines):
        return ""
    return lines[t.line][t.col : t.col + t.length]


def _peek_after(lines: list[str], t: _Tok) -> str:
    """First non-whitespace char immediately after the token on the same line."""
    if t.line >= len(lines):
        return ""
    line = lines[t.line]
    j = t.col + t.length
    while j < len(line) and line[j] in " \t":
        j += 1
    return line[j] if j < len(line) else ""


def _starts(lines: list[str], t: _Tok, s: str) -> bool:
    """Check whether the source after the token (skipping whitespace) starts with `s`."""
    if t.line >= len(lines):
        return False
    line = lines[t.line]
    j = t.col + t.length
    while j < len(line) and line[j] in " \t":
        j += 1
    return line.startswith(s, j)


def _peek_before(lines: list[str], t: _Tok) -> str:
    """Last non-whitespace char immediately before the token on the same line."""
    if t.line >= len(lines):
        return ""
    line = lines[t.line]
    j = t.col - 1
    while j >= 0 and line[j] in " \t":
        j -= 1
    return line[j] if j >= 0 else ""


def compute(src: str) -> list[int]:
    """Return the LSP semantic-tokens data array (flat list of ints, groups of 5)."""
    scanner = _Scanner(src)
    toks = scanner.scan()
    _classify_followers(toks)
    _classify_contextual(src, toks)
    # Sort by position (the scan is mostly in order, but multi-line spans
    # interleaved with line comments could be out-of-order in rare cases).
    toks.sort(key=lambda t: (t.line, t.col))
    return list(_encode(toks))


def _encode(toks: list[_Tok]) -> Iterator[int]:
    prev_line = 0
    prev_col = 0
    for t in toks:
        if t.length <= 0:
            continue
        delta_line = t.line - prev_line
        delta_col = t.col - prev_col if delta_line == 0 else t.col
        yield delta_line
        yield delta_col
        yield t.length
        yield t.type_idx
        yield t.mods
        prev_line = t.line
        prev_col = t.col
