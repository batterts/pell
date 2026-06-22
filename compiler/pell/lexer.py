"""Hand-written lexer for pell.

Token stream is a flat list of Token objects. Whitespace and comments are
skipped; nothing significant about indentation. `sql!{ ... }` is captured
as a single SQL_BLOCK token whose value is the raw text between the
braces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ast import Loc


KEYWORDS = {
    "module", "import", "pub", "fn", "let", "var", "return", "yield",
    "if", "else", "for", "forall", "in", "match", "transaction",
    "record", "error", "true", "false", "Some", "None", "Ok", "Err",
    "null",
    "unsafe", "finally", "and", "or", "not",
    # §5.2 / §5.3 — types, sealed hierarchies, aggregates.
    # `Self` (capitalized) is intentionally NOT a keyword — it's resolved
    # by the parser/emitter as the enclosing type's name (so `Self` lives
    # in type position only and reads as just a named type elsewhere).
    "type", "sealed", "aggregate", "case", "self",
    # External sequence references (no DDL).
    "seq",
    # Parameter modes (default is IN; absent = IN).
    "out", "inout",
    # Finite enum types — variants lower to text constants.
    "enum",
}


@dataclass
class Token:
    kind: str
    value: str
    loc: Loc

    def __repr__(self) -> str:
        return f"Token({self.kind!r}, {self.value!r}, {self.loc})"


class LexError(Exception):
    def __init__(self, msg: str, loc: Loc):
        super().__init__(f"{loc}: {msg}")
        self.loc = loc
        self.msg = msg


class Lexer:
    def __init__(self, source: str, filename: str = "<input>"):
        self.src = source
        self.file = filename
        self.pos = 0
        self.line = 1
        self.col = 1

    # ---- helpers ---------------------------------------------------------

    def _loc(self) -> Loc:
        return Loc(self.file, self.line, self.col)

    def _peek(self, offset: int = 0) -> str:
        p = self.pos + offset
        return self.src[p] if p < len(self.src) else ""

    def _advance(self, n: int = 1) -> str:
        out = self.src[self.pos : self.pos + n]
        for ch in out:
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.pos += n
        return out

    def _starts_with(self, s: str) -> bool:
        return self.src.startswith(s, self.pos)

    # ---- main loop -------------------------------------------------------

    def tokenize(self) -> list[Token]:
        toks: list[Token] = []
        while self.pos < len(self.src):
            ch = self._peek()
            # whitespace
            if ch in " \t\r\n":
                self._advance()
                continue
            # line comment
            if self._starts_with("//"):
                while self.pos < len(self.src) and self._peek() != "\n":
                    self._advance()
                continue
            # block comment (no nesting in v0)
            if self._starts_with("/*"):
                start = self._loc()
                self._advance(2)
                while self.pos < len(self.src) and not self._starts_with("*/"):
                    self._advance()
                if self.pos >= len(self.src):
                    raise LexError("unterminated /* ... */ comment", start)
                self._advance(2)
                continue
            # `sql!{ ... }` raw block — allow optional whitespace between `sql!` and `{`
            if self._starts_with("sql!") and self._matches_macro_block_start("sql!"):
                toks.append(self._read_macro_block("sql!", "SQL_BLOCK"))
                continue
            # `jq!{ ... }` raw block — same shape; lexer just captures the text,
            # the dedicated jq parser handles it at AST-time.
            if self._starts_with("jq!") and self._matches_macro_block_start("jq!"):
                toks.append(self._read_macro_block("jq!", "JQ_BLOCK"))
                continue
            # identifiers / keywords
            if ch.isalpha() or ch == "_":
                toks.append(self._read_ident())
                continue
            # numbers
            if ch.isdigit():
                toks.append(self._read_number())
                continue
            # strings
            if ch == '"':
                toks.append(self._read_string())
                continue
            # raw strings — backticks. No interpolation, no escape processing.
            # Useful for regex patterns, file paths, anything with `{` `}` or
            # backslashes that shouldn't be touched by the lexer.
            if ch == "`":
                toks.append(self._read_raw_string())
                continue
            # regex literal `/pattern/` — only when the previous token leaves
            # us in a position where an expression could start. JavaScript-style
            # disambiguation: after IDENT/NUMBER/STRING/`)` we're after a value
            # and `/` is division; everything else (operators, keywords,
            # openers) means a regex can start here.
            if ch == "/" and not self._starts_with("//") and not self._starts_with("/*"):
                if _regex_allowed_after(toks[-1] if toks else None):
                    toks.append(self._read_regex())
                    continue
            # multi-char operators (longest first)
            for two in ("..=", "::", "->", "=>", "==", "!=", "<=", ">=", "&&", "||", "..", "|>",):
                if self._starts_with(two):
                    loc = self._loc()
                    self._advance(len(two))
                    toks.append(Token(_punct_kind(two), two, loc))
                    break
            else:
                # single-char punctuation
                if ch in "(){}[];,:.@?|&!+*-/%<>=":
                    loc = self._loc()
                    self._advance()
                    toks.append(Token(_punct_kind(ch), ch, loc))
                    continue
                raise LexError(f"unexpected character {ch!r}", self._loc())
        toks.append(Token("EOF", "", self._loc()))
        return toks

    # ---- specialized readers --------------------------------------------

    def _read_ident(self) -> Token:
        loc = self._loc()
        start = self.pos
        while self.pos < len(self.src) and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        text = self.src[start : self.pos]
        if text in KEYWORDS:
            return Token("KW_" + text.upper(), text, loc)
        return Token("IDENT", text, loc)

    def _read_number(self) -> Token:
        loc = self._loc()
        start = self.pos
        while self.pos < len(self.src) and self._peek().isdigit():
            self._advance()
        if self._peek() == "." and self._peek(1).isdigit():
            self._advance()
            while self.pos < len(self.src) and self._peek().isdigit():
                self._advance()
        return Token("NUMBER", self.src[start : self.pos], loc)

    def _read_regex(self) -> Token:
        """Read a `/pattern/` regex literal. Inside the pattern, `\\/` is
        a literal `/` (so the engine sees `\\/` and treats it as `/`), and
        `\\\\` is a literal backslash. Newlines are not allowed — regex
        literals are single-line.

        Flags after the closing `/` (e.g. `/foo/i`) are not yet supported;
        we error out cleanly so the user knows to wait or use a leading
        `(?i)` inside the pattern.
        """
        loc = self._loc()
        self._advance()  # opening /
        start = self.pos
        while self.pos < len(self.src):
            c = self._peek()
            if c == "\\":
                self._advance()
                if self.pos < len(self.src):
                    self._advance()
                continue
            if c == "/":
                break
            if c == "\n":
                raise LexError("regex literal cannot span lines", loc)
            self._advance()
        if self.pos >= len(self.src):
            raise LexError("unterminated regex literal", loc)
        text = self.src[start : self.pos]
        self._advance()  # closing /
        if self.pos < len(self.src) and self._peek() in "imsx":
            raise LexError(
                "regex flags (i/m/s/x) aren't supported yet — for case-insensitive "
                "match, prepend `(?i)` inside the pattern instead", loc,
            )
        return Token("REGEX", text, loc)

    def _read_raw_string(self) -> Token:
        """Read a backtick-delimited raw string. Everything between the
        backticks is preserved verbatim — no `\\n` escapes, no `{name}`
        interpolation. Use this for regex patterns and other content where
        the lexer's default string processing would mangle the source.
        """
        loc = self._loc()
        self._advance()  # opening `
        start = self.pos
        while self.pos < len(self.src) and self._peek() != "`":
            self._advance()
        if self.pos >= len(self.src):
            raise LexError("unterminated raw string literal", loc)
        text = self.src[start : self.pos]
        self._advance()  # closing `
        return Token("RAWSTRING", text, loc)

    def _read_string(self) -> Token:
        loc = self._loc()
        self._advance()  # consume opening "
        out: list[str] = []
        while self.pos < len(self.src) and self._peek() != '"':
            ch = self._peek()
            if ch == "\\":
                self._advance()
                esc = self._peek()
                self._advance()
                mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}
                if esc in mapping:
                    out.append(mapping[esc])
                else:
                    # Preserve unknown escape sequences verbatim — pell strings
                    # are commonly used to carry regex patterns (`\d`, `\w`,
                    # `\s`, `\b`) and SQL fragments (`\%`), so swallowing the
                    # backslash would be more surprising than keeping it.
                    out.append("\\")
                    out.append(esc)
                continue
            out.append(ch)
            self._advance()
        if self.pos >= len(self.src):
            raise LexError("unterminated string literal", loc)
        self._advance()  # closing "
        return Token("STRING", "".join(out), loc)

    def _matches_sql_block_start(self) -> bool:
        return self._matches_macro_block_start("sql!")

    def _matches_macro_block_start(self, prefix: str) -> bool:
        """Check if `<prefix>` (already known) is followed by optional whitespace then `{`."""
        i = self.pos + len(prefix)
        while i < len(self.src) and self.src[i] in " \t\r\n":
            i += 1
        return i < len(self.src) and self.src[i] == "{"

    def _read_macro_block(self, prefix: str, token_kind: str) -> Token:
        """Read a `<prefix>{ ... }` raw block. Used for `sql!{ ... }`
        (token_kind SQL_BLOCK) and `jq!{ ... }` (token_kind JQ_BLOCK).

        Tracks brace depth so braces inside SQL string literals don't break
        out prematurely.
        """
        loc = self._loc()
        self._advance(len(prefix))
        while self.pos < len(self.src) and self._peek() in " \t\r\n":
            self._advance()
        self._advance(1)  # consume the {
        start = self.pos
        depth = 1
        while self.pos < len(self.src) and depth > 0:
            ch = self._peek()
            if ch == "{":
                depth += 1
                self._advance()
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    break
                self._advance()
                continue
            if ch == "'":
                self._advance()
                while self.pos < len(self.src) and self._peek() != "'":
                    if self._peek() == "\\":
                        self._advance()
                    self._advance()
                if self.pos < len(self.src):
                    self._advance()
                continue
            if ch == '"':
                self._advance()
                while self.pos < len(self.src) and self._peek() != '"':
                    self._advance()
                if self.pos < len(self.src):
                    self._advance()
                continue
            if self._starts_with("--"):
                while self.pos < len(self.src) and self._peek() != "\n":
                    self._advance()
                continue
            self._advance()
        if self.pos >= len(self.src):
            raise LexError(f"unterminated {prefix}{{ ... }} block", loc)
        text = self.src[start : self.pos]
        self._advance()  # consume the closing }
        return Token(token_kind, text, loc)


# ---------------------------------------------------------------------------
# Punctuation kind lookup
# ---------------------------------------------------------------------------


_PUNCT: dict[str, str] = {
    "(": "LPAREN", ")": "RPAREN",
    "{": "LBRACE", "}": "RBRACE",
    "[": "LBRACKET", "]": "RBRACKET",
    ";": "SEMI", ",": "COMMA", ":": "COLON", ".": "DOT", "@": "AT",
    "?": "QUESTION", "|": "PIPE", "&": "AMP", "!": "BANG",
    "+": "PLUS", "-": "MINUS", "*": "STAR", "/": "SLASH", "%": "PERCENT",
    "<": "LT", ">": "GT", "=": "EQ",
    "..": "DOTDOT", "..=": "DOTDOTEQ", "::": "COLONCOLON",
    "->": "ARROW", "=>": "FATARROW",
    "==": "EQEQ", "!=": "BANGEQ", "<=": "LE", ">=": "GE",
    "&&": "AMPAMP", "||": "PIPEPIPE",
    "|>": "PIPEGT",
}


def _punct_kind(s: str) -> str:
    return _PUNCT[s]


# Token kinds that *produce a value* in expression context — if one of
# these is the previous token, then `/` is division. Anything else means
# the `/` is starting a regex literal.
_VALUE_TOKEN_KINDS = frozenset({
    "IDENT", "NUMBER", "STRING", "RAWSTRING", "REGEX",
    "RPAREN", "RBRACKET", "RBRACE",
    "KW_TRUE", "KW_FALSE", "KW_NIL", "KW_NULL", "KW_SELF",
    # macro blocks are values
    "SQL_BLOCK", "JQ_BLOCK",
})


def _regex_allowed_after(prev_tok) -> bool:
    if prev_tok is None:
        return True
    return prev_tok.kind not in _VALUE_TOKEN_KINDS


def tokenize(source: str, filename: str = "<input>") -> list[Token]:
    return Lexer(source, filename).tokenize()
