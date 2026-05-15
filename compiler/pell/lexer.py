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
    "unsafe", "finally", "and", "or", "not",
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
            if self._starts_with("sql!") and self._matches_sql_block_start():
                toks.append(self._read_sql_block())
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
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}.get(esc, esc))
                continue
            out.append(ch)
            self._advance()
        if self.pos >= len(self.src):
            raise LexError("unterminated string literal", loc)
        self._advance()  # closing "
        return Token("STRING", "".join(out), loc)

    def _matches_sql_block_start(self) -> bool:
        """Check if `sql!` (already known) is followed by optional whitespace then `{`."""
        i = self.pos + len("sql!")
        while i < len(self.src) and self.src[i] in " \t\r\n":
            i += 1
        return i < len(self.src) and self.src[i] == "{"

    def _read_sql_block(self) -> Token:
        """Read a `sql!{ ... }` raw block.

        Tracks brace depth so braces inside SQL string literals (single or
        double quoted) and `q'[...]'` quoted literals don't break out
        prematurely.
        """
        loc = self._loc()
        self._advance(len("sql!"))
        # skip whitespace between sql! and {
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
            # SQL single-quoted string
            if ch == "'":
                self._advance()
                while self.pos < len(self.src) and self._peek() != "'":
                    if self._peek() == "\\":
                        self._advance()
                    self._advance()
                if self.pos < len(self.src):
                    self._advance()
                continue
            # SQL double-quoted identifier
            if ch == '"':
                self._advance()
                while self.pos < len(self.src) and self._peek() != '"':
                    self._advance()
                if self.pos < len(self.src):
                    self._advance()
                continue
            # SQL line comment
            if self._starts_with("--"):
                while self.pos < len(self.src) and self._peek() != "\n":
                    self._advance()
                continue
            self._advance()
        if self.pos >= len(self.src):
            raise LexError("unterminated sql!{ ... } block", loc)
        sql_text = self.src[start : self.pos]
        self._advance()  # consume the closing }
        return Token("SQL_BLOCK", sql_text, loc)


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


def tokenize(source: str, filename: str = "<input>") -> list[Token]:
    return Lexer(source, filename).tokenize()
