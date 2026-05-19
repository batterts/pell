"""Shallow SQL dependency extractor for pell.

Walks the raw text of `sql!{...}` blocks and pulls out the tables, views,
and DBLINK targets they reference. The output drives the per-module
dependency manifest that pell emits as a comment block at the top of
generated PL/SQL — so a DBA reading the file knows what objects the
module touches without spelunking ALL_DEPENDENCIES, and so pell itself
has ground truth for cross-module impact analysis.

What this is NOT:
- A full SQL parser. It looks for `FROM` / `JOIN` / `INSERT INTO` /
  `UPDATE` / `DELETE FROM` / `MERGE INTO` keywords and grabs the next
  identifier-ish token. Subqueries, CTE references, complex aliases all
  flow through with reasonable but not perfect fidelity.
- Aware of synonyms or grants. The names are returned verbatim
  (lowercased); the consumer decides what's real.

Known limitations (documented, accepted for v1):
- CTE names (WITH … AS …) get returned as if they were tables. Most are
  harmless extra entries; downstream tooling (schema-snapshot M4) can
  filter via ALL_TABLES intersection.
- `SELECT … INTO local_var FROM t` correctly extracts `t`, NOT
  `local_var` — INTO is only matched after INSERT or MERGE keywords.
- Dynamic SQL inside `sql!{}` (concatenated text expressions) won't be
  seen because pell's `sql!{}` is literal text; dynamic SQL is the
  `unsafe dyn_sql!{}` form (deferred), which carries its own explicit
  `touches (…)` clause.
"""

from __future__ import annotations

import re
from typing import Iterable


# Match a table-introducing keyword followed by an identifier.
# Captured group 1 is the table reference, including optional schema prefix
# (`schema.table`) and optional DBLINK suffix (`table@dblink`).
_TABLE_INTRO = re.compile(
    r"\b(?:"
    r"FROM"
    r"|JOIN"
    r"|INSERT\s+INTO"
    r"|UPDATE"
    r"|DELETE\s+FROM"
    r"|MERGE\s+INTO"
    r")\s+"
    r"([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?(?:@[a-zA-Z_]\w*)?)",
    re.IGNORECASE,
)


def extract_sql_deps(sql_text: str) -> tuple[set[str], set[str]]:
    """Return (tables, dblinks) referenced by a single SQL fragment.

    Both are lowercased sets of identifiers. Tables may include a schema
    qualifier (e.g. `hr.employees`). DBLINKs are bare names (`prod_db`).
    """
    if not sql_text:
        return set(), set()
    cleaned = _strip_strings_and_comments(sql_text)
    tables: set[str] = set()
    dblinks: set[str] = set()
    for m in _TABLE_INTRO.finditer(cleaned):
        ref = m.group(1).strip().lower()
        if "@" in ref:
            name, _, link = ref.partition("@")
            if name:
                tables.add(name)
            if link:
                dblinks.add(link)
        else:
            tables.add(ref)
    # SQL keywords that look like identifiers and might leak through the
    # regex when they immediately follow FROM/JOIN/etc. (e.g. `SELECT ...
    # FROM DUAL`). DUAL is the obvious one; we keep it visible because
    # it IS a system table, but downstream consumers can filter it out.
    return tables, dblinks


def collect_module_deps(module) -> dict:
    """Walk every `SqlBlock` in a Module's AST and aggregate dependencies.

    Returns a dict with sorted-list values:
        {"tables":  ["hr.employees", "departments"],
         "dblinks": ["prod_db"],
         "sequences": ["employee_id_seq"],
         "modules":   ["accounts", "audit"]}

    `sequences` comes from `pub seq` declarations (AST-visible).
    `modules` is currently empty until cross-module call tracking is
    surfaced — placeholder for future work.
    """
    from . import ast as A
    all_tables: set[str] = set()
    all_dblinks: set[str] = set()
    sequences: set[str] = set()
    for item in module.items:
        if isinstance(item, A.SequenceDef):
            sequences.add(item.name.lower())
            continue
        # Recurse through every statement-bearing item for sql blocks.
        for node in _walk_items_for_sql(item):
            t, d = extract_sql_deps(node.sql)
            all_tables |= t
            all_dblinks |= d
    return {
        "tables":    sorted(all_tables),
        "dblinks":   sorted(all_dblinks),
        "sequences": sorted(sequences),
        "modules":   [],
    }


def _walk_items_for_sql(item) -> Iterable:
    """Yield every SqlBlock found inside a top-level item.

    Walks into fn bodies, finally clauses, method bodies, sealed-case
    method bodies, aggregate step/merge/finish bodies, and through any
    statement that nests child blocks (if/for/match/transaction).
    """
    from . import ast as A
    if isinstance(item, A.FnDef):
        yield from _walk_stmts_for_sql(item.body)
        if item.finally_body:
            yield from _walk_stmts_for_sql(item.finally_body)
    elif isinstance(item, A.TypeDef):
        for m in item.methods:
            yield from _walk_stmts_for_sql(m.body)
    elif isinstance(item, A.SealedTypeDef):
        for m in item.methods:
            yield from _walk_stmts_for_sql(m.body)
        for case in item.cases:
            for m in case.methods:
                yield from _walk_stmts_for_sql(m.body)
    elif isinstance(item, A.AggregateDef):
        yield from _walk_stmts_for_sql(item.step_body)
        if item.merge_body:
            yield from _walk_stmts_for_sql(item.merge_body)
        yield from _walk_stmts_for_sql(item.finish_body)


def _walk_stmts_for_sql(stmts):
    """Recursively descend into a statement list, yielding every SqlBlock."""
    from . import ast as A
    for s in stmts:
        if isinstance(s, A.LetStmt) and s.value is not None:
            yield from _walk_expr_for_sql(s.value)
        elif isinstance(s, A.AssignStmt):
            yield from _walk_expr_for_sql(s.value)
        elif isinstance(s, A.ReturnStmt) and s.value is not None:
            yield from _walk_expr_for_sql(s.value)
        elif isinstance(s, A.ExprStmt):
            yield from _walk_expr_for_sql(s.expr)
        elif isinstance(s, A.YieldStmt):
            yield from _walk_expr_for_sql(s.value)
        elif isinstance(s, A.IfStmt):
            yield from _walk_stmts_for_sql(s.then_body)
            if s.else_body:
                yield from _walk_stmts_for_sql(s.else_body)
        elif isinstance(s, (A.ForStmt, A.ForallStmt)):
            yield from _walk_expr_for_sql(s.iterable)
            yield from _walk_stmts_for_sql(s.body)
        elif isinstance(s, A.MatchStmt):
            yield from _walk_expr_for_sql(s.scrutinee)
            for arm in s.arms:
                body = arm.body
                if isinstance(body, list):
                    yield from _walk_stmts_for_sql(body)
                else:
                    yield from _walk_expr_for_sql(body)
        elif isinstance(s, A.TransactionStmt):
            yield from _walk_stmts_for_sql(s.body)


def _walk_expr_for_sql(e):
    """Yield SqlBlocks from an expression tree. Recurses into Call args,
    member access objects, struct-lit field values, etc."""
    from . import ast as A
    if isinstance(e, A.SqlBlock):
        yield e
    elif isinstance(e, A.QuestionMark):
        yield from _walk_expr_for_sql(e.inner)
    elif isinstance(e, A.Call):
        yield from _walk_expr_for_sql(e.callee)
        for a in e.args:
            yield from _walk_expr_for_sql(a)
    elif isinstance(e, A.MemberAccess):
        yield from _walk_expr_for_sql(e.obj)
    elif isinstance(e, A.BinOp):
        yield from _walk_expr_for_sql(e.left)
        yield from _walk_expr_for_sql(e.right)
    elif isinstance(e, A.UnaryOp):
        yield from _walk_expr_for_sql(e.operand)
    elif isinstance(e, A.StructLit):
        for f in e.fields:
            yield from _walk_expr_for_sql(f.value)
    elif isinstance(e, A.ListLit):
        for el in e.elements:
            yield from _walk_expr_for_sql(el)
    elif isinstance(e, A.PipelineExpr):
        yield from _walk_expr_for_sql(e.source)
        yield from _walk_expr_for_sql(e.target)
    elif isinstance(e, A.OkExpr):
        yield from _walk_expr_for_sql(e.inner)
    elif isinstance(e, A.ErrExpr):
        yield from _walk_expr_for_sql(e.inner)
    elif isinstance(e, A.SomeExpr):
        yield from _walk_expr_for_sql(e.inner)
    elif isinstance(e, A.IfExpr):
        yield from _walk_expr_for_sql(e.cond)
        yield from _walk_stmts_for_sql(e.then_body)
        if e.else_body:
            yield from _walk_stmts_for_sql(e.else_body)
    elif isinstance(e, A.MatchExpr):
        yield from _walk_expr_for_sql(e.scrutinee)
        for arm in e.arms:
            body = arm.body
            if isinstance(body, list):
                yield from _walk_stmts_for_sql(body)
            else:
                yield from _walk_expr_for_sql(body)


def _strip_strings_and_comments(sql: str) -> str:
    """Remove SQL string literals and comments so they don't yield false
    positives in the table-extraction regex (e.g. `'FROM employees'` as a
    literal). The replacement preserves length-ish so error offsets stay
    plausible if we ever wire them in."""
    # Block comments first (greedy across lines), then line comments,
    # then strings. Order matters because a comment might contain a quote.
    out = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    out = re.sub(r"--[^\n]*", "", out)
    out = re.sub(r"'(?:[^']|'')*'", "''", out)
    return out
