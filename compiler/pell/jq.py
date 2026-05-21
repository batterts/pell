"""jq sub-parser for the `jq!{ ... }` macro.

Supports a small but useful subset of jq:

    source | .path[] | select(.field op literal [and|or .field op literal ...]) | .field

Grammar:
    block      := source "|" pipeline
    pipeline   := stage ("|" stage)*
    stage      := path_stage | select_stage | projection_stage
    path_stage := "." IDENT ("." IDENT)* ("[]" | "[*]")?
    select     := "select" "(" filter ("and"|"or" filter)* ")"
    filter     := "." IDENT ("." IDENT)* op literal
    op         := "==" | "!=" | "<" | "<=" | ">" | ">="
    literal    := NUMBER | STRING

The output is a `(source, path, filters, projection)` tuple suitable for
direct construction of a `JqBlock`. Path is rendered as a JSON path
(`$.users[*]`); the bracket iterator `[*]` is appended for any segment
ending in `[]` or `[*]`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class JqParseError(Exception):
    pass


_COMPARISON_OPS = ("==", "!=", "<=", ">=", "<", ">")


@dataclass
class _Filter:
    field_path: str  # JSON path within the row, e.g. "$.age"
    op: str
    literal: Any
    join: str = ""


@dataclass
class _Parsed:
    source: str
    path: str
    filters: list[_Filter]
    projection: Optional[str]


class _Cursor:
    """Tiny char cursor with whitespace skipping. The jq surface is small
    enough that hand-rolled recursive descent stays under a hundred lines."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def eof(self) -> bool:
        return self.pos >= len(self.text)

    def peek(self) -> str:
        return self.text[self.pos] if not self.eof() else ""

    def starts_with(self, s: str) -> bool:
        return self.text.startswith(s, self.pos)

    def skip_ws(self) -> None:
        while not self.eof() and self.peek() in " \t\r\n":
            self.pos += 1

    def consume(self, s: str) -> None:
        self.skip_ws()
        if not self.starts_with(s):
            raise JqParseError(f"expected {s!r} at {self.text[self.pos:self.pos+20]!r}")
        self.pos += len(s)

    def consume_ident(self) -> str:
        self.skip_ws()
        start = self.pos
        if self.eof() or not (self.peek().isalpha() or self.peek() == "_"):
            raise JqParseError(f"expected identifier at {self.text[self.pos:self.pos+20]!r}")
        while not self.eof() and (self.peek().isalnum() or self.peek() == "_"):
            self.pos += 1
        return self.text[start : self.pos]


def parse_jq(text: str) -> _Parsed:
    """Parse a jq!{...} block body and return (source, path, filters, projection)."""
    cur = _Cursor(text)

    cur.skip_ws()
    source = cur.consume_ident()

    cur.skip_ws()
    cur.consume("|")

    path = "$"
    filters: list[_Filter] = []
    projection: Optional[str] = None
    saw_iterator = False

    while True:
        cur.skip_ws()
        if cur.eof():
            break

        if cur.starts_with("select"):
            # Spelled out: "select" must be followed by "(" — guard against
            # an identifier prefix.
            save = cur.pos
            cur.pos += len("select")
            cur.skip_ws()
            if cur.peek() != "(":
                cur.pos = save
                # fall through to projection / path parsing
            else:
                cur.consume("(")
                _parse_filter_list(cur, filters)
                cur.skip_ws()
                cur.consume(")")
                _maybe_consume_pipe(cur)
                continue

        if cur.peek() == ".":
            # Either a path segment (extending the JSON path) before the
            # iterator was seen, or a final projection after.
            cur.pos += 1
            # `.` immediately followed by `[]` or `[*]` means "iterate the
            # whole source" — no segment.
            if cur.starts_with("[]") or cur.starts_with("[*]"):
                if cur.starts_with("[]"):
                    cur.pos += 2
                else:
                    cur.pos += 3
                if saw_iterator:
                    raise JqParseError("nested iterators are not supported")
                saw_iterator = True
                path += "[*]"
                _maybe_consume_pipe(cur)
                continue
            segs = [cur.consume_ident()]
            while True:
                cur.skip_ws()
                if cur.peek() == ".":
                    # Lookahead: ".[" is invalid in our subset; ".ident" continues
                    if cur.pos + 1 < len(cur.text) and (
                        cur.text[cur.pos + 1].isalpha() or cur.text[cur.pos + 1] == "_"
                    ):
                        cur.pos += 1
                        segs.append(cur.consume_ident())
                        continue
                break

            cur.skip_ws()
            had_iterator_here = False
            if cur.starts_with("[]"):
                cur.pos += 2
                had_iterator_here = True
            elif cur.starts_with("[*]"):
                cur.pos += 3
                had_iterator_here = True

            joined = ".".join(segs)
            if not saw_iterator:
                # Still extending the iteration path.
                path = path + "." + joined
                if had_iterator_here:
                    saw_iterator = True
                    path += "[*]"
            else:
                # After the iterator — this is a per-row projection.
                projection = joined
                if had_iterator_here:
                    raise JqParseError("iterator after final projection is not supported")

            _maybe_consume_pipe(cur)
            continue

        if cur.eof():
            break
        raise JqParseError(f"unexpected token at {cur.text[cur.pos:cur.pos+20]!r}")

    if not saw_iterator:
        # Default to iterating the source array if no [] was seen
        path = path + "[*]" if path == "$" else path + "[*]"
        saw_iterator = True

    return _Parsed(source=source, path=path, filters=filters, projection=projection)


def _maybe_consume_pipe(cur: _Cursor) -> None:
    cur.skip_ws()
    if cur.peek() == "|":
        cur.pos += 1


def _parse_filter_list(cur: _Cursor, out: list[_Filter]) -> None:
    first = _parse_one_filter(cur)
    out.append(first)
    while True:
        cur.skip_ws()
        if cur.starts_with("and"):
            cur.pos += 3
            f = _parse_one_filter(cur)
            f.join = "and"
            out.append(f)
            continue
        if cur.starts_with("or"):
            cur.pos += 2
            f = _parse_one_filter(cur)
            f.join = "or"
            out.append(f)
            continue
        break


def _parse_one_filter(cur: _Cursor) -> _Filter:
    cur.skip_ws()
    if cur.peek() != ".":
        raise JqParseError(f"expected '.field' in select(), got {cur.text[cur.pos:cur.pos+20]!r}")
    cur.pos += 1
    segs = [cur.consume_ident()]
    while True:
        cur.skip_ws()
        if cur.peek() == "." and cur.pos + 1 < len(cur.text) and (
            cur.text[cur.pos + 1].isalpha() or cur.text[cur.pos + 1] == "_"
        ):
            cur.pos += 1
            segs.append(cur.consume_ident())
            continue
        break
    field_path = "$." + ".".join(segs)

    cur.skip_ws()
    op = None
    for candidate in _COMPARISON_OPS:
        if cur.starts_with(candidate):
            op = candidate
            cur.pos += len(candidate)
            break
    if op is None:
        raise JqParseError(f"expected comparison op at {cur.text[cur.pos:cur.pos+10]!r}")

    cur.skip_ws()
    literal = _parse_literal(cur)
    return _Filter(field_path=field_path, op=op, literal=literal)


def _parse_literal(cur: _Cursor) -> Any:
    cur.skip_ws()
    if cur.eof():
        raise JqParseError("expected literal, got EOF")
    ch = cur.peek()
    if ch == '"' or ch == "'":
        return _parse_string(cur, ch)
    if ch == "-" or ch.isdigit():
        return _parse_number(cur)
    if cur.starts_with("true"):
        cur.pos += 4
        return True
    if cur.starts_with("false"):
        cur.pos += 5
        return False
    if cur.starts_with("null"):
        cur.pos += 4
        return None
    raise JqParseError(f"expected literal at {cur.text[cur.pos:cur.pos+20]!r}")


def _parse_string(cur: _Cursor, quote: str) -> str:
    cur.pos += 1
    start = cur.pos
    while not cur.eof() and cur.peek() != quote:
        if cur.peek() == "\\":
            cur.pos += 2
        else:
            cur.pos += 1
    if cur.eof():
        raise JqParseError("unterminated string literal in jq filter")
    s = cur.text[start : cur.pos]
    cur.pos += 1
    return s


def _parse_number(cur: _Cursor) -> float:
    start = cur.pos
    if cur.peek() == "-":
        cur.pos += 1
    while not cur.eof() and (cur.peek().isdigit() or cur.peek() == "."):
        cur.pos += 1
    raw = cur.text[start : cur.pos]
    return float(raw) if "." in raw else int(raw)
