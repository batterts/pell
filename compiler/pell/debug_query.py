"""Evaluate a small query expression against a collection debug shadow.

The debugger serializes a `list<Record>` local to a JSON shadow:

    {"count": N, "rows": [ {field: value, ...}, ... ]}

(or a bare array for a list<scalar>). At a breakpoint the IDE reads that
shadow and asks this engine to evaluate a pell-flavored expression
against it — locally, no DB round-trip, no live re-fetch. It's a
snapshot of the collection as of the assignment point.

Supported surface (chained left to right):

    rows                       the whole array
    rows[i]      rows.at(i)     element i (1-based, pell semantics)
    rows.len()   rows.count()   true element count (from "count")
    .field                      record field — projects over a list,
                                accesses a single element
    .where(pred)                filter; pred is `field OP value`
                                joined by `and` / `or`
                                OP in  == != < <= > >=
    .first()                    first element (null if empty)
    .collect()                  materialize the list (identity)

Examples:
    rows[100].user_id
    rows.where(user_id == 1).movie_id          -> [10]   (list)
    rows.where(user_id == 1).first().movie_id  -> 10     (scalar)
    rows.where(rating >= 5).collect()
"""
from __future__ import annotations

import json
import re
from typing import Any


class QueryError(Exception):
    pass


# --- tokenizer --------------------------------------------------------------

_TOK = re.compile(r"""
    \s*(?:
      (?P<num>-?\d+(?:\.\d+)?)
    | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<op><=|>=|==|!=|<|>)
    | (?P<name>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<punc>[().\[\],])
    )
""", re.X)


def _tokenize(s: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(s):
        if s[pos].isspace():
            pos += 1
            continue
        m = _TOK.match(s, pos)
        if not m or m.end() == pos:
            raise QueryError(f"unexpected character at {pos!r}: {s[pos:pos+12]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


# --- predicate (inside where(...)) ------------------------------------------

_CMP = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<":  lambda a, b: a is not None and a < b,
    "<=": lambda a, b: a is not None and a <= b,
    ">":  lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
}


def _lit(tok: tuple[str, str]) -> Any:
    kind, val = tok
    if kind == "num":
        return float(val) if "." in val else int(val)
    if kind == "str":
        return val[1:-1]
    raise QueryError(f"expected a literal, got {val!r}")


def _make_pred(toks: list[tuple[str, str]]):
    """Build a predicate fn(record)->bool from a flat `field OP value
    (and|or field OP value)*` token list (left-to-right, no precedence —
    `and`/`or` chain as written)."""
    # split into comparisons separated by and/or
    i = 0

    def one():
        nonlocal i
        if i + 2 >= len(toks) + 0 or toks[i][0] != "name":
            raise QueryError("predicate must be `field OP value`")
        field = toks[i][1]
        op = toks[i + 1]
        if op[0] != "op":
            raise QueryError(f"expected a comparison operator, got {op[1]!r}")
        value = _lit(toks[i + 2])
        i += 3
        cmp = _CMP[op[1]]
        return lambda rec: cmp(_coerce(rec.get(field)), value)

    pred = one()
    while i < len(toks):
        if toks[i][0] != "name" or toks[i][1].lower() not in ("and", "or"):
            raise QueryError(f"expected `and`/`or`, got {toks[i][1]!r}")
        joiner = toks[i][1].lower()
        i += 1
        rhs = one()
        prev = pred
        if joiner == "and":
            pred = lambda rec, a=prev, b=rhs: a(rec) and b(rec)
        else:
            pred = lambda rec, a=prev, b=rhs: a(rec) or b(rec)
    return pred


def _coerce(v: Any) -> Any:
    """Shadow values arrive as JSON; numeric strings compare numerically."""
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return v


# --- chain evaluation -------------------------------------------------------

def eval_query(shadow_json: str, expr: str) -> Any:
    try:
        data = json.loads(shadow_json)
    except (json.JSONDecodeError, TypeError) as e:
        raise QueryError(f"shadow is not valid JSON: {e}")
    if isinstance(data, dict):
        rows = data.get("rows", data.get("sample", []))
        count = data.get("count", len(rows))
    else:
        rows, count = data, len(data)

    toks = _tokenize(expr)
    if not toks or toks[0][0] != "name":
        raise QueryError("expression must start with the collection name")
    cur: Any = rows
    i = 1
    # whether `cur` is still the full top-level collection (so len() == count)
    is_root = True

    def expect(punc: str):
        nonlocal i
        if i >= len(toks) or toks[i] != ("punc", punc):
            raise QueryError(f"expected {punc!r}")
        i += 1

    while i < len(toks):
        kind, val = toks[i]
        if kind == "punc" and val == "[":
            i += 1
            idx = _lit(toks[i]); i += 1
            expect("]")
            cur = _index(cur, int(idx)); is_root = False
        elif kind == "punc" and val == ".":
            i += 1
            if i >= len(toks) or toks[i][0] != "name":
                raise QueryError("expected a name after '.'")
            name = toks[i][1]; i += 1
            if i < len(toks) and toks[i] == ("punc", "("):
                # method call
                i += 1
                args = []
                depth = 1
                start = i
                while i < len(toks) and depth:
                    if toks[i] == ("punc", "("):
                        depth += 1
                    elif toks[i] == ("punc", ")"):
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
                arg_toks = toks[start:i]
                expect(")")
                cur, is_root = _method(cur, name, arg_toks, count, is_root)
            else:
                cur = _field(cur, name); is_root = False
        else:
            raise QueryError(f"unexpected token {val!r}")
    return cur


def _index(cur: Any, idx: int) -> Any:
    if not isinstance(cur, list):
        raise QueryError("index applies to a list")
    if idx < 1 or idx > len(cur):
        return None        # out of range (or beyond the snapshot)
    return cur[idx - 1]    # 1-based, pell semantics


def _field(cur: Any, name: str) -> Any:
    if cur is None:
        return None        # field of a missing element (e.g. rows[100] beyond the snapshot)
    if isinstance(cur, dict):
        return cur.get(name)
    if isinstance(cur, list):
        return [x.get(name) if isinstance(x, dict) else None for x in cur]
    raise QueryError(f"can't read field {name!r} of {type(cur).__name__}")


def _method(cur, name, arg_toks, count, is_root):
    n = name.lower()
    if n in ("len", "count"):
        if is_root and isinstance(cur, list):
            return count, False
        return (len(cur) if isinstance(cur, list) else 0), False
    if n == "at":
        return _index(cur, int(_lit(arg_toks[0]))), False
    if n == "where":
        if not isinstance(cur, list):
            raise QueryError("where() applies to a list")
        pred = _make_pred(arg_toks)
        return [x for x in cur if isinstance(x, dict) and pred(x)], False
    if n == "first":
        if not isinstance(cur, list):
            raise QueryError("first() applies to a list")
        return (cur[0] if cur else None), False
    if n == "collect":
        return cur, False
    raise QueryError(f"unknown method {name!r}")
