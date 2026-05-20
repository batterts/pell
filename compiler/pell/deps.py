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
from typing import Iterable, Optional


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
    """Walk every AST node in a Module and aggregate external dependencies.

    Returns a dict with sorted-list values:
        {"tables":    [...],   # from sql!{} bodies
         "dblinks":   [...],   # from sql!{} bodies (`tbl@dblink`)
         "sequences": [...],   # from `pub seq` declarations
         "packages":  [...]}   # from `<pkg>::<member>` references

    A `packages` reference is anything with `::` in an Ident name —
    user-defined other pell modules (`accounts::transfer`), Oracle
    built-ins (`dbms_utility::get_hash_value`), or stdlib helpers
    (`log::info`). We don't try to distinguish: any external package the
    code calls into shows up here, and a DBA reading the manifest can
    classify by name.

    `pell_runtime` is added automatically when the module declares any
    errors — every Err propagation goes through pell_runtime's set_err.
    """
    from . import ast as A
    all_tables: set[str] = set()
    all_dblinks: set[str] = set()
    sequences: set[str] = set()
    packages: set[str] = set()
    has_errors = False
    for item in module.items:
        if isinstance(item, A.SequenceDef):
            sequences.add(item.name.lower())
            continue
        if isinstance(item, A.ErrorDef):
            has_errors = True
            continue
        # Recurse through every statement-bearing item for SqlBlocks AND
        # for any qualified Ident reference (the `::` package syntax).
        for node in _walk_items_for_sql(item):
            t, d = extract_sql_deps(node.sql)
            all_tables |= t
            all_dblinks |= d
        for ident_name in _walk_items_for_qualified_idents(item):
            pkg = _package_from_qualified(ident_name)
            if pkg is not None:
                packages.add(pkg)
    if has_errors:
        packages.add("pell_runtime")
    # Filter out enum names — `EnumName::VARIANT` references look like
    # package references (`pkg::member`) to the AST walker, but they're
    # really compile-time literal lookups, not cross-package calls.
    from . import ast as A
    enum_names = {i.name.lower() for i in module.items if isinstance(i, A.EnumDef)}
    packages -= enum_names
    return {
        "tables":    sorted(all_tables),
        "dblinks":   sorted(all_dblinks),
        "sequences": sorted(sequences),
        "packages":  sorted(packages),
    }


def _package_from_qualified(name: str) -> Optional[str]:
    """Extract the package portion of `pkg::member` or `pkg::sub::member`.

    Everything before the LAST `::` is the package; the final segment is
    the member. Returns None if the name has no `::`.

        accounts::transfer            -> "accounts"
        dbms_utility::get_hash_value  -> "dbms_utility"
        foo::bar::baz                 -> "foo.bar"   (joined for display)
    """
    if "::" not in name:
        return None
    parts = name.split("::")
    if len(parts) < 2:
        return None
    return ".".join(parts[:-1]).lower()


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


def _walk_items_for_qualified_idents(item) -> Iterable[str]:
    """Yield every `<pkg>::<member>` identifier name found inside a top-level
    item. Mirrors _walk_items_for_sql but collects Ident names instead of
    SqlBlock nodes.
    """
    from . import ast as A
    if isinstance(item, A.FnDef):
        yield from _walk_stmts_for_idents(item.body)
        if item.finally_body:
            yield from _walk_stmts_for_idents(item.finally_body)
    elif isinstance(item, A.TypeDef):
        for m in item.methods:
            yield from _walk_stmts_for_idents(m.body)
    elif isinstance(item, A.SealedTypeDef):
        for m in item.methods:
            yield from _walk_stmts_for_idents(m.body)
        for case in item.cases:
            for m in case.methods:
                yield from _walk_stmts_for_idents(m.body)
    elif isinstance(item, A.AggregateDef):
        yield from _walk_stmts_for_idents(item.step_body)
        if item.merge_body:
            yield from _walk_stmts_for_idents(item.merge_body)
        yield from _walk_stmts_for_idents(item.finish_body)


def _walk_stmts_for_idents(stmts) -> Iterable[str]:
    from . import ast as A
    for s in stmts:
        if isinstance(s, A.LetStmt) and s.value is not None:
            yield from _walk_expr_for_idents(s.value)
        elif isinstance(s, A.AssignStmt):
            yield from _walk_expr_for_idents(s.target)
            yield from _walk_expr_for_idents(s.value)
        elif isinstance(s, A.ReturnStmt) and s.value is not None:
            yield from _walk_expr_for_idents(s.value)
        elif isinstance(s, A.ExprStmt):
            yield from _walk_expr_for_idents(s.expr)
        elif isinstance(s, A.YieldStmt):
            yield from _walk_expr_for_idents(s.value)
        elif isinstance(s, A.IfStmt):
            yield from _walk_expr_for_idents(s.cond)
            yield from _walk_stmts_for_idents(s.then_body)
            if s.else_body:
                yield from _walk_stmts_for_idents(s.else_body)
        elif isinstance(s, (A.ForStmt, A.ForallStmt)):
            yield from _walk_expr_for_idents(s.iterable)
            yield from _walk_stmts_for_idents(s.body)
        elif isinstance(s, A.MatchStmt):
            yield from _walk_expr_for_idents(s.scrutinee)
            for arm in s.arms:
                body = arm.body
                if isinstance(body, list):
                    yield from _walk_stmts_for_idents(body)
                else:
                    yield from _walk_expr_for_idents(body)
        elif isinstance(s, A.TransactionStmt):
            yield from _walk_stmts_for_idents(s.body)


def _walk_expr_for_idents(e) -> Iterable[str]:
    """Yield qualified-Ident names from an expression tree. Only yields
    names containing `::`; the caller extracts the package portion."""
    from . import ast as A
    if isinstance(e, A.Ident) and "::" in e.name:
        yield e.name
    elif isinstance(e, A.Call):
        yield from _walk_expr_for_idents(e.callee)
        for a in e.args:
            yield from _walk_expr_for_idents(a)
    elif isinstance(e, A.MemberAccess):
        yield from _walk_expr_for_idents(e.obj)
    elif isinstance(e, A.QuestionMark):
        yield from _walk_expr_for_idents(e.inner)
    elif isinstance(e, A.BinOp):
        yield from _walk_expr_for_idents(e.left)
        yield from _walk_expr_for_idents(e.right)
    elif isinstance(e, A.UnaryOp):
        yield from _walk_expr_for_idents(e.operand)
    elif isinstance(e, A.StructLit):
        for f in e.fields:
            yield from _walk_expr_for_idents(f.value)
    elif isinstance(e, A.ListLit):
        for el in e.elements:
            yield from _walk_expr_for_idents(el)
    elif isinstance(e, A.PipelineExpr):
        yield from _walk_expr_for_idents(e.source)
        yield from _walk_expr_for_idents(e.target)
    elif isinstance(e, A.OkExpr):
        yield from _walk_expr_for_idents(e.inner)
    elif isinstance(e, A.ErrExpr):
        yield from _walk_expr_for_idents(e.inner)
    elif isinstance(e, A.SomeExpr):
        yield from _walk_expr_for_idents(e.inner)
    elif isinstance(e, A.IfExpr):
        yield from _walk_expr_for_idents(e.cond)
        yield from _walk_stmts_for_idents(e.then_body)
        if e.else_body:
            yield from _walk_stmts_for_idents(e.else_body)
    elif isinstance(e, A.MatchExpr):
        yield from _walk_expr_for_idents(e.scrutinee)
        for arm in e.arms:
            body = arm.body
            if isinstance(body, list):
                yield from _walk_stmts_for_idents(body)
            else:
                yield from _walk_expr_for_idents(body)


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
