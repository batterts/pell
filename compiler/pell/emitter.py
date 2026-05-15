"""PL/SQL emitter for pell.

Walks a Module AST and emits a CREATE OR REPLACE PACKAGE + PACKAGE BODY
pair. Also emits a one-time pell_runtime stub if errors are declared.

This is the v0 emitter: it covers a useful subset and emits sensible
PL/SQL for the patterns shown in design.md. Sections marked `-- TODO`
in the output indicate constructs that aren't fully lowered yet.
"""

from __future__ import annotations

from typing import Optional

from . import ast as A


class EmitError(Exception):
    def __init__(self, msg: str, loc: A.Loc):
        super().__init__(f"{loc}: {msg}")
        self.loc = loc
        self.msg = msg


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

PRIM_MAP = {
    "number":    "NUMBER",
    "int":       "PLS_INTEGER",
    "text":      "VARCHAR2(4000)",
    "bool":      "BOOLEAN",
    "date":      "DATE",
    "timestamp": "TIMESTAMP",
    "interval":  "INTERVAL DAY TO SECOND",
    "bytes":     "RAW(2000)",
    "json":      "JSON",
    "Unit":      "",   # procedure return; never appears as a slot
    "Never":     "",   # never-returns; same
}

# PL/SQL refuses size specifiers on parameter types and function returns.
PRIM_PARAM_MAP = {
    "number":    "NUMBER",
    "int":       "PLS_INTEGER",
    "text":      "VARCHAR2",
    "bool":      "BOOLEAN",
    "date":      "DATE",
    "timestamp": "TIMESTAMP",
    "interval":  "INTERVAL DAY TO SECOND",
    "bytes":     "RAW",
    "json":      "JSON",
    "Unit":      "",
    "Never":     "",
}


def lower_type(t: A.TypeRef, *, param: bool = False) -> str:
    """Lower a pell type reference to a PL/SQL type expression."""
    if isinstance(t, A.PrimType):
        m = PRIM_PARAM_MAP if param else PRIM_MAP
        return m.get(t.name, t.name.upper())
    if isinstance(t, A.NamedType):
        # record/error name -> `t_<name>` style
        return _record_type_name(t.name)
    if isinstance(t, A.OptionalType):
        # MVP: Option<T> lowers to the inner T (NULL represents None).
        # Loses Option<Option<T>>; documented limitation for v0.
        return lower_type(t.inner, param=param)
    if isinstance(t, A.GenericType):
        if t.base == "Option" and t.params:
            return lower_type(t.params[0], param=param)
        if t.base == "Result" and t.params:
            # Result<T, E> lowers to just T at the type level; E propagates via RAISE.
            return lower_type(t.params[0], param=param)
        if t.base == "list" and t.params:
            return f"t_{_safe(_render_type(t.params[0]))}_list"
        if t.base == "map" and len(t.params) == 2:
            return f"t_{_safe(_render_type(t.params[0]))}_to_{_safe(_render_type(t.params[1]))}"
        if t.base == "set" and t.params:
            return f"t_{_safe(_render_type(t.params[0]))}_set"
        return f"t_{_safe(t.base)}"
    if isinstance(t, A.ErrorUnionType):
        # Error unions never appear in a slot; they're for Result<T, _>'s second param
        return ""
    return ""


def _render_type(t: A.TypeRef) -> str:
    if isinstance(t, A.PrimType):
        return t.name
    if isinstance(t, A.NamedType):
        return t.name
    if isinstance(t, A.OptionalType):
        return _render_type(t.inner) + "_opt"
    if isinstance(t, A.GenericType):
        return t.base + "_" + "_".join(_render_type(p) for p in t.params)
    return "unknown"


def _record_type_name(name: str) -> str:
    return "t_" + _safe(name).lower()


def _safe(s: str) -> str:
    return s.replace(".", "_").replace("::", "_")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def local_name(pell_name: str) -> str:
    return "l_" + pell_name


def param_name(pell_name: str) -> str:
    return "p_" + pell_name


def fn_pl_name(pell_name: str) -> str:
    return pell_name


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class Emitter:
    def __init__(self, module: A.Module):
        self.module = module
        self.pkg = module.package_name
        # collected during emission of a function body
        self._declares: list[str] = []  # PL/SQL declaration lines (no trailing ;)
        self._decl_seen: set[str] = set()
        self._sql_var_counter = 0
        self._params: set[str] = set()  # current fn's param names
        self._current_fn: Optional[A.FnDef] = None
        # local-name -> declared PL/SQL type (so we can pick record-field projections)
        self._local_types: dict[str, str] = {}
        # module-level list-of-T types we've already declared (so we don't redeclare)
        self._list_types_emitted: set[str] = set()
        # list-type declarations to inject into the package body header
        self._list_type_decls: list[str] = []
        # which locals are lists (kept so for-loops over them iterate correctly)
        self._list_locals: dict[str, str] = {}  # name -> element type spelling
        # name → PL/SQL identifier override (used by list-loop shadows)
        self._loop_var_override: dict[str, str] = {}
        # cursor FOR-loop variables in scope — referenced bare, no l_ prefix
        self._loop_vars: list[set[str]] = []
        # stack of "currently inside a transaction" — outer-most flag identifier
        # we use to mark committed.
        self._tx_stack: list[str] = []
        # collected once per module
        self._records: list[A.RecordDef] = [i for i in module.items if isinstance(i, A.RecordDef)]
        self._errors: list[A.ErrorDef] = [i for i in module.items if isinstance(i, A.ErrorDef)]
        self._fns: list[A.FnDef] = [i for i in module.items if isinstance(i, A.FnDef)]
        # all module-private and module-public fn names
        self._fn_names: set[str] = {f.name for f in self._fns}

    # ---- entry point ----------------------------------------------------

    def emit(self) -> str:
        chunks: list[str] = []
        chunks.append(self._emit_header())
        if self._errors:
            chunks.append(self._emit_runtime_section())
        chunks.append(self._emit_spec())
        chunks.append("")
        chunks.append(self._emit_body())
        return "\n".join(chunks)

    def _emit_header(self) -> str:
        return (
            f"-- Generated by pell v0 from module {self.module.name}\n"
            f"-- DO NOT EDIT — regenerate with `pell build`\n"
        )

    def _emit_runtime_section(self) -> str:
        """Per-module additions to pell_runtime: one EXCEPTION per error variant + setters.

        In a real multi-module build, pell_runtime is one shared package. For
        the v0 single-file emitter, we emit one section per module that the
        user can collate.
        """
        lines: list[str] = []
        lines.append(f"-- Additions to pell_runtime for module {self.module.name}:")
        lines.append("--   (collate these into a single CREATE OR REPLACE PACKAGE pell_runtime)")
        for e in self._errors:
            exn = self._exception_name(e.name)
            lines.append(f"--   {exn} EXCEPTION;")
            lines.append(f"--   PRAGMA EXCEPTION_INIT({exn}, -{20100 + self._errors.index(e)});")
        lines.append("")
        return "\n".join(lines)

    def _exception_name(self, err_name: str) -> str:
        return f"{self.pkg}_{err_name}".lower()

    # ---- spec & body ----------------------------------------------------

    def _emit_spec(self) -> str:
        out: list[str] = []
        out.append(f"CREATE OR REPLACE PACKAGE {self.pkg} AS")
        # records (declare types public so callers can reference them)
        for rec in self._records:
            if rec.is_pub:
                out.append(self._render_record_type(rec, indent="  "))
        # public fn signatures
        for fn in self._fns:
            if fn.is_pub:
                out.append("  " + self._fn_signature(fn) + ";")
        out.append(f"END {self.pkg};")
        out.append("/")
        return "\n".join(out)

    def _emit_body(self) -> str:
        # walk fns first so list-type declarations get registered
        fn_chunks: list[str] = []
        for fn in self._fns:
            fn_chunks.append(self._fn_body(fn))
        out: list[str] = []
        out.append(f"CREATE OR REPLACE PACKAGE BODY {self.pkg} AS")
        # private record types
        for rec in self._records:
            if not rec.is_pub:
                out.append(self._render_record_type(rec, indent="  "))
        # collected list types (assoc array INDEX BY PLS_INTEGER)
        for decl in self._list_type_decls:
            out.append(decl)
        # all fns
        for chunk in fn_chunks:
            out.append("")
            out.append(chunk)
        out.append(f"END {self.pkg};")
        out.append("/")
        return "\n".join(out)

    def _render_record_type(self, rec: A.RecordDef, indent: str = "") -> str:
        lines = [f"{indent}TYPE {_record_type_name(rec.name)} IS RECORD ("]
        field_lines = []
        for f in rec.fields:
            field_lines.append(f"{indent}  {f.name.lower()} {lower_type(f.type_ref)}")
        lines.append(",\n".join(field_lines))
        lines.append(f"{indent});")
        return "\n".join(lines)

    # ---- fn signatures & bodies -----------------------------------------

    def _fn_signature(self, fn: A.FnDef) -> str:
        params = ", ".join(
            f"{param_name(p.name)} IN {lower_type(p.type_ref, param=True)}"
            for p in fn.params
        )
        ret = fn.return_type
        if ret is None or _is_unit_like(ret):
            sig = f"PROCEDURE {fn_pl_name(fn.name)}"
        else:
            sig = f"FUNCTION {fn_pl_name(fn.name)}"
        if params:
            sig += f"({params})"
        if not (ret is None or _is_unit_like(ret)):
            sig += f" RETURN {lower_type(ret, param=True)}"
        # signature-level annotations: DETERMINISTIC, RESULT_CACHE
        ann_names = {a.name for a in fn.annotations}
        if "deterministic" in ann_names:
            sig += " DETERMINISTIC"
        if "result_cache" in ann_names:
            sig += " RESULT_CACHE"
        return sig

    def _fn_body_pragmas(self, fn: A.FnDef) -> list[str]:
        """Body-level pragmas: PRAGMA UDF, PRAGMA AUTONOMOUS_TRANSACTION."""
        ann_names = {a.name for a in fn.annotations}
        out: list[str] = []
        if "autonomous" in ann_names:
            out.append("PRAGMA AUTONOMOUS_TRANSACTION;")
        if "udf" in ann_names:
            out.append("PRAGMA UDF;")
        return out

    def _check_annotation_conflicts(self, fn: A.FnDef) -> None:
        """Raise a compile-time error on illegal annotation combinations."""
        ann_names = {a.name for a in fn.annotations}
        if "udf" in ann_names and "autonomous" in ann_names:
            raise EmitError(
                f"fn {fn.name!r}: @udf and @autonomous are mutually exclusive — UDF assumes "
                f"the fn participates in the calling SQL's transaction",
                fn.loc,
            )

    def _fn_body(self, fn: A.FnDef) -> str:
        # reset per-fn state
        self._declares = []
        self._decl_seen = set()
        self._sql_var_counter = 0
        self._params = {p.name for p in fn.params}
        self._current_fn = fn
        self._local_types = {}
        self._list_locals = {}

        self._check_annotation_conflicts(fn)

        # walk body to collect declarations and assemble statements
        sig = self._fn_signature(fn)
        # If there's a `finally` clause, lower as:
        #   procedure pell_finally_body is begin <finally> end;
        #   begin
        #     begin <body>
        #     exception when others then
        #       begin pell_finally_body; exception when others then null; end;
        #       raise;
        #     end;
        #     pell_finally_body;
        #   end;
        has_finally = fn.finally_body is not None

        body_stmt_lines: list[str] = []
        body_indent = "      " if has_finally else "    "
        for s in fn.body:
            body_stmt_lines.extend(self._emit_stmt(s, indent=body_indent))

        finally_stmt_lines: list[str] = []
        if has_finally:
            assert fn.finally_body is not None
            for s in fn.finally_body:
                finally_stmt_lines.extend(self._emit_stmt(s, indent="      "))

        body_pragmas = self._fn_body_pragmas(fn)

        out: list[str] = []
        out.append(f"  {sig} IS")
        for d in self._declares:
            out.append(f"    {d}")
        if has_finally:
            out.append(f"    PROCEDURE pell_finally_body IS")
            out.append(f"    BEGIN")
            if finally_stmt_lines:
                out.extend(finally_stmt_lines)
            else:
                out.append("      NULL;")
            out.append(f"    END pell_finally_body;")
        for p in body_pragmas:
            out.append(f"    {p}")
        out.append(f"  BEGIN")
        if has_finally:
            out.append("    BEGIN")
            if body_stmt_lines:
                out.extend(body_stmt_lines)
            else:
                out.append("      NULL;")
            out.append("    EXCEPTION")
            out.append("      WHEN OTHERS THEN")
            out.append("        BEGIN pell_finally_body; EXCEPTION WHEN OTHERS THEN NULL; END;")
            out.append("        RAISE;")
            out.append("    END;")
            out.append("    pell_finally_body;")
        else:
            if body_stmt_lines:
                out.extend(body_stmt_lines)
            else:
                out.append("    NULL;")
        out.append(f"  END {fn_pl_name(fn.name)};")
        return "\n".join(out)

    # ---- declarations ---------------------------------------------------

    def _decl(self, line: str) -> None:
        if line not in self._decl_seen:
            self._declares.append(line)
            self._decl_seen.add(line)

    # ---- statements -----------------------------------------------------

    def _emit_stmt(self, s: A.Stmt, indent: str) -> list[str]:
        if isinstance(s, A.LetStmt):
            return self._emit_let(s, indent)
        if isinstance(s, A.AssignStmt):
            tgt = self._emit_expr(s.target)
            val = self._emit_expr(s.value)
            return [f"{indent}{tgt} := {val};"]
        if isinstance(s, A.ReturnStmt):
            return self._emit_return(s, indent)
        if isinstance(s, A.IfStmt):
            return self._emit_if(s, indent)
        if isinstance(s, A.ForStmt):
            return self._emit_for(s, indent)
        if isinstance(s, A.MatchStmt):
            return self._emit_match(s, indent)
        if isinstance(s, A.TransactionStmt):
            return self._emit_transaction(s, indent)
        if isinstance(s, A.ExprStmt):
            return self._emit_expr_stmt(s, indent)
        return [f"{indent}-- TODO: stmt {type(s).__name__}"]

    def _emit_let(self, s: A.LetStmt, indent: str) -> list[str]:
        nm = local_name(s.name)
        # decide type for declaration
        ty = lower_type(s.type_annot) if s.type_annot else None
        # Special case: `let x: list<T> = [a, b, c];` — declare an INDEX BY
        # PLS_INTEGER table, then emit per-index assignments.
        if (
            isinstance(s.type_annot, A.GenericType)
            and s.type_annot.base == "list"
            and isinstance(s.value, A.ListLit)
        ):
            return self._emit_list_let(s, nm, indent)
        # generic type lookup if we can do it cheaply
        if ty is None:
            ty = self._infer_decl_type(s.value)
        if ty is None:
            ty = "NUMBER"  # fallback; user should annotate
            self._decl(f"{nm} {ty};  -- TODO: inferred, please annotate")
        else:
            self._decl(f"{nm} {ty};")
        self._local_types[s.name] = ty
        # if value present, emit assignment
        if s.value is None:
            return []
        # special handling if the expression is a QuestionMark on a Result-typed call
        return self._emit_assign_to(nm, s.value, indent)

    def _emit_list_let(self, s: A.LetStmt, nm: str, indent: str) -> list[str]:
        """Lower `let xs: list<T> = [v1, v2, ...];` to:

            -- module-level:
            TYPE t_<T>_list IS TABLE OF <T> INDEX BY PLS_INTEGER;
            -- DECLARE:
            l_xs t_<T>_list;
            -- BEGIN body:
            l_xs(1) := v1;
            l_xs(2) := v2;
            ...
        """
        assert isinstance(s.type_annot, A.GenericType)
        assert isinstance(s.value, A.ListLit)
        elem_t = s.type_annot.params[0]
        elem_sql = lower_type(elem_t)
        # PLS_INTEGER for the index — matches BINARY_INTEGER in older syntax
        list_type = f"t_{_safe(_render_type(elem_t))}_list"
        if list_type not in self._list_types_emitted:
            self._list_type_decls.append(
                f"  TYPE {list_type} IS TABLE OF {elem_sql} INDEX BY PLS_INTEGER;"
            )
            self._list_types_emitted.add(list_type)
        self._decl(f"{nm} {list_type};")
        self._local_types[s.name] = list_type
        self._list_locals[s.name] = elem_sql
        lines: list[str] = []
        for i, el in enumerate(s.value.elements, start=1):
            lines.append(f"{indent}{nm}({i}) := {self._emit_expr(el)};")
        return lines

    def _emit_assign_to(self, target: str, expr: A.Expr, indent: str) -> list[str]:
        """Emit one or more PL/SQL statements that assign `expr` to `target`."""
        if isinstance(expr, A.QuestionMark):
            inner = expr.inner
            # if it's a sql!{}.one()? pattern, emit a select-into; the RAISE on no-data is the propagation
            if isinstance(inner, A.Call):
                return self._emit_questionmark_call(target, inner, indent)
        if isinstance(expr, A.Call):
            return self._emit_call_assignment(target, expr, indent)
        # sql!{}.one() (no question mark) — still emit the select-into
        if isinstance(expr, A.Call):
            return self._emit_call_assignment(target, expr, indent)
        return [f"{indent}{target} := {self._emit_expr(expr)};"]

    def _infer_decl_type(self, value: Optional[A.Expr]) -> Optional[str]:
        """Best-effort inference of PL/SQL declaration type from an init expression."""
        if value is None:
            return None
        if isinstance(value, A.NumberLit):
            return "NUMBER"
        if isinstance(value, A.TextLit):
            return "VARCHAR2(4000)"
        if isinstance(value, A.BoolLit):
            return "BOOLEAN"
        if isinstance(value, A.QuestionMark):
            return self._infer_decl_type(value.inner)
        if isinstance(value, A.Call):
            return self._infer_call_type(value)
        if isinstance(value, A.OkExpr):
            return self._infer_decl_type(value.inner)
        if isinstance(value, A.StructLit):
            return _record_type_name(value.type_name)
        return None

    def _infer_call_type(self, call: A.Call) -> Optional[str]:
        # .one() / .first() / .one_or_none() on a sql!{} (possibly wrapped in lock modifiers)
        if isinstance(call.callee, A.MemberAccess):
            method = call.callee.field
            recv = call.callee.obj
            if method in ("one", "first", "one_or_none"):
                _, sql = self._strip_lock_modifiers(recv)
                if sql is not None:
                    rt = self._row_type_from_fn_return()
                    if rt is not None:
                        return rt
                    return "VARCHAR2(4000)"
            if method == "rowcount":
                return "PLS_INTEGER"
            if method == "returning" and call.type_args:
                return lower_type(call.type_args[0])
            if method == "into" and call.type_args:
                return lower_type(call.type_args[0])
            # chained call: t = (sql!{...}.returning::<T>()).one()
            if method in ("one", "first", "one_or_none") and isinstance(recv, A.Call):
                inner_ty = self._infer_call_type(recv)
                if inner_ty is not None:
                    return inner_ty
        # plain function call → look up in the module's fn list
        if isinstance(call.callee, A.Ident):
            target_fn = next((f for f in self._fns if f.name == call.callee.name), None)
            if target_fn is not None and target_fn.return_type is not None:
                rt = target_fn.return_type
                if isinstance(rt, A.GenericType) and rt.base in ("Result", "Option") and rt.params:
                    return lower_type(rt.params[0])
                if isinstance(rt, A.OptionalType):
                    return lower_type(rt.inner)
                return lower_type(rt)
        return None

    def _row_type_from_fn_return(self) -> Optional[str]:
        """If the enclosing fn returns Result<T, _> or Option<T> or T directly,
        return the PL/SQL type for T."""
        if self._current_fn is None or self._current_fn.return_type is None:
            return None
        rt = self._current_fn.return_type
        if isinstance(rt, A.GenericType) and rt.base in ("Result", "Option") and rt.params:
            return lower_type(rt.params[0])
        if isinstance(rt, A.OptionalType):
            return lower_type(rt.inner)
        return lower_type(rt)

    def _emit_questionmark_call(self, target: str, inner_call: A.Call, indent: str) -> list[str]:
        """Emit `target := <call>?` — propagate Err via RAISE."""
        # If the inner call is sql!{}[.for_update()...].one() — turn into a SELECT INTO.
        if isinstance(inner_call.callee, A.MemberAccess) and inner_call.callee.field == "one":
            recv, sql = self._strip_lock_modifiers(inner_call.callee.obj)
            if sql is not None:
                return self._emit_select_into(target, sql, indent, expect_exactly_one=True)
        # If the inner call is sql!{}.first()? — same but raise NotFound if not found
        if isinstance(inner_call.callee, A.MemberAccess) and inner_call.callee.field == "first":
            recv, sql = self._strip_lock_modifiers(inner_call.callee.obj)
            if sql is not None:
                return self._emit_first_loop(target, sql, indent, propagate_none=True)
        # general: assume the call returns the value and may raise (already-shaped errors via RAISE)
        return [f"{indent}{target} := {self._emit_expr(inner_call)};"]

    def _strip_lock_modifiers(self, expr: A.Expr) -> tuple[Optional[A.Expr], Optional[A.SqlBlock]]:
        """Unwrap `.for_update()` / `.nowait()` / `.skip_locked()` modifiers and
        return the underlying SqlBlock (with the FOR UPDATE clause appended).

        Returns (None, None) if the expr doesn't ultimately bottom out in a SqlBlock.
        """
        lock_parts: list[str] = []
        cur = expr
        while isinstance(cur, A.Call) and isinstance(cur.callee, A.MemberAccess):
            method = cur.callee.field
            if method == "for_update":
                lock_parts.insert(0, "FOR UPDATE")
            elif method == "nowait":
                lock_parts.append("NOWAIT")
            elif method == "skip_locked":
                lock_parts.append("SKIP LOCKED")
            elif method == "wait" and cur.args:
                arg = self._emit_expr(cur.args[0])
                lock_parts.append(f"WAIT {arg}")
            elif method == "for_update_of" and cur.args:
                cols = ", ".join(self._emit_expr(a) for a in cur.args)
                lock_parts.insert(0, f"FOR UPDATE OF {cols}")
            else:
                break
            cur = cur.callee.obj
        if isinstance(cur, A.SqlBlock):
            if lock_parts:
                new_sql = A.SqlBlock(
                    loc=cur.loc,
                    sql=cur.sql.rstrip().rstrip(";") + " " + " ".join(lock_parts),
                    binds=cur.binds,
                    is_dml=cur.is_dml,
                    has_returning=cur.has_returning,
                )
                return cur, new_sql
            return cur, cur
        return None, None

    def _emit_call_assignment(self, target: str, call: A.Call, indent: str) -> list[str]:
        # .one() with no ? — same select-into but on NO_DATA_FOUND we raise a generic invariant? For v0, leave it as raise.
        if isinstance(call.callee, A.MemberAccess) and call.callee.field in ("one", "first"):
            recv = call.callee.obj
            _, sql_with_locks = self._strip_lock_modifiers(recv)
            if sql_with_locks is not None:
                if call.callee.field == "one":
                    return self._emit_select_into(target, sql_with_locks, indent, expect_exactly_one=True)
                else:
                    return self._emit_first_loop(target, sql_with_locks, indent, propagate_none=False)
            # `sql!{INSERT ... RETURNING ...}.returning::<T>().one()` — DML RETURNING INTO
            if isinstance(recv, A.Call) and isinstance(recv.callee, A.MemberAccess) and recv.callee.field == "returning":
                inner_sql = recv.callee.obj
                if isinstance(inner_sql, A.SqlBlock) and inner_sql.is_dml and inner_sql.has_returning:
                    return self._emit_dml_returning(target, inner_sql, indent)
        # .rowcount() — emit the DML inline then assign SQL%ROWCOUNT
        if isinstance(call.callee, A.MemberAccess) and call.callee.field == "rowcount":
            recv = call.callee.obj
            if isinstance(recv, A.SqlBlock) and recv.is_dml:
                return self._emit_dml_with_rowcount(target, recv, indent)
        return [f"{indent}{target} := {self._emit_expr(call)};"]

    def _emit_dml_returning(self, target: str, sql: A.SqlBlock, indent: str) -> list[str]:
        """Lower `sql!{INSERT ... RETURNING col}.returning::<T>().one()` to a DML
        with `RETURNING col INTO target`.
        """
        import re
        sql_text = self._rewrite_binds(sql.sql).strip().rstrip(";")
        # Splice INTO target right after the RETURNING clause's projection.
        # We assume the RETURNING clause is the *trailing* part: `... returning <projection>`.
        m = re.search(r"\breturning\b", sql_text, re.IGNORECASE)
        if m:
            head = sql_text[:m.end()]
            proj = sql_text[m.end():].strip()
            spliced = f"{head} {proj} INTO {target}"
        else:
            spliced = sql_text  # shouldn't happen given has_returning=True
        return [f"{indent}{spliced};"]

    def _emit_dml_with_rowcount(self, target: str, sql: A.SqlBlock, indent: str) -> list[str]:
        sql_text = self._rewrite_binds(sql.sql).strip().rstrip(";")
        return [
            f"{indent}{sql_text};",
            f"{indent}{target} := SQL%ROWCOUNT;",
        ]

    def _emit_select_into(self, target: str, sql: A.SqlBlock, indent: str, expect_exactly_one: bool) -> list[str]:
        """Lower a single-row SELECT INTO.

        PL/SQL requires INTO between the SELECT list and FROM, so we splice
        it in at that boundary.
        """
        import re
        sql_text = self._rewrite_binds(sql.sql).strip().rstrip(";")
        m = re.search(r"\s+from\s+", sql_text, re.IGNORECASE)
        if m:
            head = sql_text[:m.start()]
            tail = sql_text[m.end():]
            spliced = f"{head}\n      INTO {target}\n      FROM {tail}"
        else:
            spliced = f"{sql_text}\n      INTO {target}"
        return [
            f"{indent}BEGIN",
            f"{indent}  {spliced.strip()};",
            f"{indent}EXCEPTION",
            f"{indent}  WHEN NO_DATA_FOUND THEN RAISE;",
            f"{indent}  WHEN TOO_MANY_ROWS THEN RAISE;",
            f"{indent}END;",
        ]

    def _emit_first_loop(self, target: str, sql: A.SqlBlock, indent: str, propagate_none: bool) -> list[str]:
        """Lower a `.first()` to a cursor FOR loop with FETCH FIRST 1 ROWS ONLY."""
        sql_text = self._rewrite_binds(sql.sql).strip().rstrip(";")
        if "fetch first" not in sql_text.lower():
            sql_text += "\n  FETCH FIRST 1 ROWS ONLY"
        lines = [
            f"{indent}DECLARE",
            f"{indent}  l_found BOOLEAN := FALSE;",
            f"{indent}BEGIN",
            f"{indent}  FOR r IN (",
        ]
        for line in sql_text.splitlines():
            lines.append(f"{indent}    {line}")
        lines += [
            f"{indent}  ) LOOP",
            f"{indent}    {target} := r;  -- TODO: project to target record shape",
            f"{indent}    l_found := TRUE;",
            f"{indent}    EXIT;",
            f"{indent}  END LOOP;",
        ]
        if propagate_none:
            lines.append(f"{indent}  IF NOT l_found THEN RAISE NO_DATA_FOUND; END IF;")
        lines.append(f"{indent}END;")
        return lines

    def _emit_return(self, s: A.ReturnStmt, indent: str) -> list[str]:
        is_proc = (
            self._current_fn is not None
            and (self._current_fn.return_type is None or _is_unit_like(self._current_fn.return_type))
        )
        prefix: list[str] = []
        # If we're inside one or more transaction blocks, commit them before
        # returning normally. Err returns raise and let the handler roll back.
        if self._tx_stack and not (s.value is not None and isinstance(s.value, A.ErrExpr)):
            for flag in self._tx_stack:
                prefix.append(f"{indent}COMMIT;")
                prefix.append(f"{indent}{flag} := TRUE;")
        # If the enclosing fn has a `finally` clause, prepend a call to it
        # on the success path (Err returns get the EXCEPTION handler's call).
        if (
            self._current_fn is not None
            and self._current_fn.finally_body is not None
            and not (s.value is not None and isinstance(s.value, A.ErrExpr))
        ):
            prefix.append(f"{indent}pell_finally_body;")
        if s.value is None:
            return prefix + [f"{indent}RETURN;"]
        # `return Err(...)` → set payload + RAISE (always, even in procedures)
        if isinstance(s.value, A.ErrExpr):
            return self._emit_err_return(s.value.inner, indent)
        # In a procedure, the value is conventionally Ok(()); just RETURN;
        if is_proc:
            return prefix + [f"{indent}RETURN;"]
        # `return Ok(x)` → RETURN x;
        if isinstance(s.value, A.OkExpr):
            return prefix + [f"{indent}RETURN {self._emit_expr(s.value.inner)};"]
        return prefix + [f"{indent}RETURN {self._emit_expr(s.value)};"]

    def _emit_err_return(self, payload_expr: A.Expr, indent: str) -> list[str]:
        """Lower `return Err(<variant>)` to: set SYS_CONTEXT payload + RAISE."""
        if isinstance(payload_expr, A.StructLit):
            err_name = payload_expr.type_name
            exn = self._exception_name(err_name)
            fields = " || '|' || ".join(
                f"'{f.name}=' || {self._emit_expr(f.value)}"
                for f in payload_expr.fields
            )
            payload = fields if fields else "''"
            return [
                f"{indent}pell_runtime.set_err('{exn}:1', {payload});",
                f"{indent}RAISE pell_runtime.{exn};",
            ]
        if isinstance(payload_expr, A.Ident):
            # zero-payload error variant
            exn = self._exception_name(payload_expr.name)
            return [f"{indent}RAISE pell_runtime.{exn};"]
        return [f"{indent}-- TODO: Err({type(payload_expr).__name__})"]

    def _emit_if(self, s: A.IfStmt, indent: str) -> list[str]:
        out = [f"{indent}IF {self._emit_expr(s.cond)} THEN"]
        for stmt in s.then_body:
            out.extend(self._emit_stmt(stmt, indent + "  "))
        if s.else_body is not None:
            out.append(f"{indent}ELSE")
            for stmt in s.else_body:
                out.extend(self._emit_stmt(stmt, indent + "  "))
        out.append(f"{indent}END IF;")
        return out

    def _emit_for(self, s: A.ForStmt, indent: str) -> list[str]:
        # for x in sql!{...} — cursor FOR loop
        if isinstance(s.iterable, A.SqlBlock):
            sql_text = self._rewrite_binds(s.iterable.sql).strip().rstrip(";")
            out = [f"{indent}FOR {s.var_name} IN ("]
            for line in sql_text.splitlines():
                out.append(f"{indent}  {line}")
            out.append(f"{indent}) LOOP")
            self._loop_vars.append({s.var_name})
            for stmt in s.body:
                out.extend(self._emit_stmt(stmt, indent + "  "))
            self._loop_vars.pop()
            out.append(f"{indent}END LOOP;")
            return out
        # for x in <list-typed local>: iterate via assoc-array FOR loop
        if isinstance(s.iterable, A.Ident) and s.iterable.name in self._list_locals:
            list_local = local_name(s.iterable.name)
            elem_t = self._list_locals[s.iterable.name]
            # Use an integer loop variable and bind the loop name to list_local(i)
            idx = f"i_{s.var_name}"
            out = [f"{indent}FOR {idx} IN {list_local}.FIRST .. {list_local}.LAST LOOP"]
            # Make the loop variable reference the array element inside the body
            # by introducing a per-iteration local. Cheapest: declare it once at
            # the function level and reassign each iteration.
            shadow = local_name(s.var_name) + "_iter"
            self._decl(f"{shadow} {elem_t};")
            out.append(f"{indent}  {shadow} := {list_local}({idx});")
            # Push a shadow scope: references to `var_name` inside the body
            # resolve to `shadow` (via _loop_vars + a dedicated map).
            self._loop_vars.append({s.var_name})
            # We map the loop var name to the shadow via an override stack.
            prev_override = self._loop_var_override.get(s.var_name)
            self._loop_var_override[s.var_name] = shadow
            for stmt in s.body:
                out.extend(self._emit_stmt(stmt, indent + "  "))
            self._loop_vars.pop()
            if prev_override is None:
                del self._loop_var_override[s.var_name]
            else:
                self._loop_var_override[s.var_name] = prev_override
            out.append(f"{indent}END LOOP;")
            return out
        # for i in range expressions — generic numeric for
        if isinstance(s.iterable, A.BinOp) and s.iterable.op in ("..", "..="):
            lo = self._emit_expr(s.iterable.left)
            hi = self._emit_expr(s.iterable.right)
            # exclusive `..`: subtract 1 from hi; inclusive `..=`: use hi as-is
            hi_expr = hi if s.iterable.op == "..=" else f"({hi}) - 1"
            out = [f"{indent}FOR {s.var_name} IN {lo} .. {hi_expr} LOOP"]
            self._loop_vars.append({s.var_name})
            for stmt in s.body:
                out.extend(self._emit_stmt(stmt, indent + "  "))
            self._loop_vars.pop()
            out.append(f"{indent}END LOOP;")
            return out
        return [f"{indent}-- TODO: for x in non-sql, non-range iterable"]

    def _emit_match(self, s: A.MatchStmt, indent: str) -> list[str]:
        """Lower a match into an IF/ELSIF chain.

        For v0: handles Ok(x)/Err(...)/Some(x)/None on a scrutinee that
        will *already have raised* if Err. So the structure becomes a
        BEGIN ... EXCEPTION ... END wrapper instead of a chain.
        """
        scrut_code = self._emit_expr(s.scrutinee)
        # Detect whether this match has Err arms (then needs exception wrapping)
        has_err = any(isinstance(a.pattern, A.VariantPattern) and a.pattern.name == "Err" for a in s.arms)
        if has_err:
            return self._emit_match_with_errors(s, scrut_code, indent)
        # plain pattern match on Some/None/wildcard/binding
        out = []
        for i, arm in enumerate(s.arms):
            cond = self._pattern_to_cond(arm.pattern, scrut_code)
            kw = "IF" if i == 0 else "ELSIF"
            if cond == "TRUE":
                kw = "ELSE"
                out.append(f"{indent}{kw}")
            else:
                out.append(f"{indent}{kw} {cond} THEN")
            body = arm.body
            if isinstance(body, list):
                for stmt in body:
                    out.extend(self._emit_stmt(stmt, indent + "  "))
            else:
                out.append(f"{indent}  {self._emit_expr(body)};")
        out.append(f"{indent}END IF;")
        return out

    def _emit_match_with_errors(self, s: A.MatchStmt, scrut_code: str, indent: str) -> list[str]:
        """When the match has Err arms, wrap the scrutinee in a BEGIN block and use EXCEPTION."""
        # collect arms
        ok_arms = [a for a in s.arms if not (isinstance(a.pattern, A.VariantPattern) and a.pattern.name == "Err")]
        err_arms = [a for a in s.arms if isinstance(a.pattern, A.VariantPattern) and a.pattern.name == "Err"]
        out = [f"{indent}DECLARE", f"{indent}  l_match_v VARCHAR2(4000);"]
        out.append(f"{indent}BEGIN")
        out.append(f"{indent}  l_match_v := {scrut_code};  -- TODO: type-aware projection")
        # ok arms
        for arm in ok_arms:
            body = arm.body
            if isinstance(body, list):
                for st in body:
                    out.extend(self._emit_stmt(st, indent + "  "))
            else:
                out.append(f"{indent}  {self._emit_expr(body)};")
        out.append(f"{indent}EXCEPTION")
        for arm in err_arms:
            pat = arm.pattern
            assert isinstance(pat, A.VariantPattern)
            # Err(NotFound { ... }) → WHEN pell_runtime.<pkg>_notfound THEN
            if pat.args and isinstance(pat.args[0], A.VariantPattern):
                vp: A.VariantPattern = pat.args[0]
                exn = self._exception_name(vp.name)
                out.append(f"{indent}  WHEN pell_runtime.{exn} THEN")
            else:
                out.append(f"{indent}  WHEN OTHERS THEN  -- TODO: more specific error binding")
            body = arm.body
            if isinstance(body, list):
                for st in body:
                    out.extend(self._emit_stmt(st, indent + "  "))
            else:
                out.append(f"{indent}    {self._emit_expr(body)};")
        out.append(f"{indent}END;")
        return out

    def _pattern_to_cond(self, p: A.Pattern, scrut: str) -> str:
        if isinstance(p, A.WildcardPattern):
            return "TRUE"
        if isinstance(p, A.BindingPattern):
            return "TRUE"
        if isinstance(p, A.VariantPattern):
            if p.name == "None":
                return f"({scrut}) IS NULL"
            if p.name == "Some":
                return f"({scrut}) IS NOT NULL"
        if isinstance(p, A.LiteralPattern):
            v = p.value
            if isinstance(v, bool):
                return f"({scrut}) = {'TRUE' if v else 'FALSE'}"
            if isinstance(v, str):
                return f"({scrut}) = '{v}'"
            return f"({scrut}) = {v}"
        return "TRUE  -- TODO: pattern"

    def _emit_transaction(self, s: A.TransactionStmt, indent: str) -> list[str]:
        sp = f"pell_sp_{len(self._tx_stack)}"
        committed_flag = f"l_committed_{len(self._tx_stack)}"
        out = [
            f"{indent}DECLARE",
            f"{indent}  {committed_flag} BOOLEAN := FALSE;",
            f"{indent}BEGIN",
            f"{indent}  SAVEPOINT {sp};",
        ]
        self._tx_stack.append(committed_flag)
        for stmt in s.body:
            out.extend(self._emit_stmt(stmt, indent + "  "))
        self._tx_stack.pop()
        # If the body's last statement is an unconditional return, the trailing
        # COMMIT is unreachable; it was already injected before the return.
        body_ends_in_return = bool(s.body) and isinstance(s.body[-1], A.ReturnStmt)
        if not body_ends_in_return:
            out.append(f"{indent}  COMMIT;")
            out.append(f"{indent}  {committed_flag} := TRUE;")
        out += [
            f"{indent}EXCEPTION",
            f"{indent}  WHEN OTHERS THEN",
            f"{indent}    IF NOT {committed_flag} THEN ROLLBACK TO {sp}; END IF;",
            f"{indent}    RAISE;",
            f"{indent}END;",
        ]
        return out

    def _emit_expr_stmt(self, s: A.ExprStmt, indent: str) -> list[str]:
        e = s.expr
        # sql!{} write as a bare statement
        if isinstance(e, A.SqlBlock):
            sql_text = self._rewrite_binds(e.sql).strip().rstrip(";")
            lines = [f"{indent}{line}" for line in (sql_text + ";").splitlines()]
            return lines
        # otherwise just emit the expression with `;`
        return [f"{indent}{self._emit_expr(e)};"]

    # ---- expressions ----------------------------------------------------

    def _emit_expr(self, e: A.Expr) -> str:
        if isinstance(e, A.NumberLit):
            return e.value
        if isinstance(e, A.TextLit):
            return self._emit_text_lit(e)
        if isinstance(e, A.BoolLit):
            return "TRUE" if e.value else "FALSE"
        if isinstance(e, A.UnitLit):
            return "NULL"
        if isinstance(e, A.NoneExpr):
            return "NULL"
        if isinstance(e, A.SomeExpr):
            return self._emit_expr(e.inner)
        if isinstance(e, A.OkExpr):
            return self._emit_expr(e.inner)
        if isinstance(e, A.ErrExpr):
            return f"NULL  /* Err({type(e.inner).__name__}) - should be in `return Err(...)` */"
        if isinstance(e, A.Ident):
            return self._lower_ident(e.name)
        if isinstance(e, A.MemberAccess):
            return f"{self._emit_expr(e.obj)}.{e.field.lower()}"
        if isinstance(e, A.BinOp):
            return self._emit_binop(e)
        if isinstance(e, A.UnaryOp):
            if e.op == "!":
                return f"NOT ({self._emit_expr(e.operand)})"
            return f"{e.op}{self._emit_expr(e.operand)}"
        if isinstance(e, A.Call):
            return self._emit_call_expr(e)
        if isinstance(e, A.QuestionMark):
            # In expression position, ? on Result just yields the value (errors raise).
            return self._emit_expr(e.inner)
        if isinstance(e, A.StructLit):
            return "/* TODO: struct lit in expr position */"
        if isinstance(e, A.SqlBlock):
            return "/* TODO: bare sql block in expr position */"
        return f"/* TODO: {type(e).__name__} */"

    def _emit_binop(self, e: A.BinOp) -> str:
        op_map = {
            "&&": "AND", "||": "OR",
            "==": "=", "!=": "<>",
            "+": "+", "-": "-", "*": "*", "/": "/",
            "<": "<", "<=": "<=", ">": ">", ">=": ">=",
            "%": "MOD",
        }
        op = op_map.get(e.op, e.op)
        left = self._emit_expr(e.left)
        right = self._emit_expr(e.right)
        if op == "MOD":
            return f"MOD({left}, {right})"
        return f"({left} {op} {right})"

    def _emit_text_lit(self, e: A.TextLit) -> str:
        """A text literal with `{name}` placeholders lowers to `'lit ' || name`.

        Only simple identifier interpolation is supported in v0; complex
        `{a.b}` etc. are emitted as concatenations of those identifiers (the
        rest of the expression lowering kicks in).
        """
        import re
        s = e.value
        if "{" not in s:
            return _sql_string(s)
        parts = re.split(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}", s)
        # parts alternates: literal, name, literal, name, ...
        chunks: list[str] = []
        for i, p in enumerate(parts):
            if i % 2 == 0:
                if p:
                    chunks.append(_sql_string(p))
            else:
                # name may contain `.` for field access; treat as `obj.field`
                if "." in p:
                    head, *rest = p.split(".")
                    expr = self._lower_ident(head)
                    for r in rest:
                        expr = f"{expr}.{r.lower()}"
                    chunks.append(expr)
                else:
                    chunks.append(self._lower_ident(p))
        if not chunks:
            return "''"
        return "(" + " || ".join(chunks) + ")"

    def _emit_call_expr(self, e: A.Call) -> str:
        # Detect simple method calls and inline them.
        if isinstance(e.callee, A.MemberAccess):
            recv = e.callee.obj
            method = e.callee.field
            if method == "into" and e.type_args:
                # value.into::<T>() — for the MVP we just copy fields, but here in
                # expression position we can't easily decompose; rely on type compatibility.
                return self._emit_expr(recv)
            if method == "expect":
                # value.expect("msg")
                msg = e.args[0] if e.args else A.TextLit(e.loc, "expect failed")
                msg_text = msg.value if isinstance(msg, A.TextLit) else "expect failed"
                return (
                    f"COALESCE({self._emit_expr(recv)}, "
                    f"RAISE_APPLICATION_ERROR(-20001, 'pell invariant: {msg_text}'))"
                )
            if method == "rowcount":
                return "SQL%ROWCOUNT"
            # generic method call → free-function style with receiver as first arg
            args_code = [self._emit_expr(recv)] + [self._emit_expr(a) for a in e.args]
            return f"{method}({', '.join(args_code)})"
        # plain function call
        callee = self._emit_expr(e.callee)
        args_code = [self._emit_expr(a) for a in e.args]
        return f"{callee}({', '.join(args_code)})"

    def _lower_ident(self, name: str) -> str:
        """Map a pell identifier (possibly qualified with ::) to PL/SQL."""
        if "::" in name:
            parts = name.split("::")
            return ".".join(parts[:-1]).lower() + "." + parts[-1].lower()
        if name in self._fn_names:
            return name.lower()
        if name in self._params:
            return param_name(name)
        # cursor FOR-loop variables are referenced bare, no prefix
        for scope in reversed(self._loop_vars):
            if name in scope:
                # honor any override (list-loop shadow); else use the name as-is
                return self._loop_var_override.get(name, name)
        return local_name(name)

    # ---- bind rewriting -------------------------------------------------

    def _rewrite_binds(self, sql: str) -> str:
        """:name in the SQL refers to a pell binding (param, local, or loop variable);
        lower to PL/SQL variable references using the correct prefix.
        """
        import re
        def repl(m: "re.Match[str]") -> str:
            name = m.group(1)
            if name in self._params:
                return param_name(name)
            # check active loop-variable overrides (for list-iter shadow names)
            for scope in reversed(self._loop_vars):
                if name in scope:
                    return self._loop_var_override.get(name, name)
            return local_name(name)
        return re.sub(r"(?<![A-Za-z0-9_]):([A-Za-z_][A-Za-z0-9_]*)", repl, sql)


def _is_unit_like(t: A.TypeRef) -> bool:
    if isinstance(t, A.PrimType) and t.name in ("Unit", "Never"):
        return True
    if isinstance(t, A.GenericType) and t.base == "Result" and t.params:
        return _is_unit_like(t.params[0])
    return False


def _sql_string(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def emit(module: A.Module) -> str:
    return Emitter(module).emit()
