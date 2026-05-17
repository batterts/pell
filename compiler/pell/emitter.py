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


SUPPORTED_TARGETS = ("23", "19c")


def lower_type(
    t: A.TypeRef,
    *,
    param: bool = False,
    target: str = "23",
    sql_context: bool = False,
) -> str:
    """Lower a pell type reference to a PL/SQL type expression.

    `target` controls dialect — `"23"` (default) or `"19c"`. `sql_context=True`
    is set for OBJECT attribute / SQL-column positions where some PL/SQL-only
    types (notably BOOLEAN) aren't legal pre-23.
    """
    if isinstance(t, A.PrimType):
        # 19c-specific lowerings for types that exist or behave differently in 23
        if target == "19c":
            if t.name == "json":
                # JSON datatype is 21c+. On 19c, use VARCHAR2 (or CLOB if we
                # ever need >32K). v0 picks 32767 to match MAX_STRING_SIZE=EXTENDED.
                return "VARCHAR2(32767)"
            if t.name == "bool" and sql_context:
                # BOOLEAN is PL/SQL-only in 19c. Encode as NUMBER(1) at SQL
                # crossings (OBJECT attributes, table columns).
                return "NUMBER(1)"
        m = PRIM_PARAM_MAP if param else PRIM_MAP
        return m.get(t.name, t.name.upper())
    if isinstance(t, A.NamedType):
        # record/error name -> `t_<name>` style
        return _record_type_name(t.name)
    if isinstance(t, A.OptionalType):
        # MVP: Option<T> lowers to the inner T (NULL represents None).
        # Loses Option<Option<T>>; documented limitation for v0.
        return lower_type(t.inner, param=param, target=target, sql_context=sql_context)
    if isinstance(t, A.GenericType):
        if t.base == "Option" and t.params:
            return lower_type(t.params[0], param=param, target=target, sql_context=sql_context)
        if t.base == "Result" and t.params:
            # Result<T, E> lowers to just T at the type level; E propagates via RAISE.
            return lower_type(t.params[0], param=param, target=target, sql_context=sql_context)
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
    def __init__(self, module: A.Module, target: str = "23"):
        if target not in SUPPORTED_TARGETS:
            raise ValueError(
                f"unsupported target {target!r}; must be one of {SUPPORTED_TARGETS}"
            )
        self.module = module
        self.target = target
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
        # Schema-level CREATE TYPE statements emitted before the package
        self._schema_types: list[str] = []
        self._schema_types_emitted: set[str] = set()
        # the record types whose OBJECT form we've emitted (for PIPELINED returns)
        self._obj_emitted: set[str] = set()
        # When emitting an @pipelined fn, remember the cursor parameter name
        # so `for x in <cursor>` lowers to FETCH BULK COLLECT INTO ... LIMIT
        self._cursor_params: dict[str, str] = {}  # param name → element record name
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
        # convenience wrapper that threads self.target through type lowering
        self._lt = lambda t, *, param=False, sql_context=False: lower_type(
            t, param=param, target=self.target, sql_context=sql_context
        )
        self._pipelined_fn_names: set[str] = {
            f.name for f in self._fns
            if any(a.name == "pipelined" for a in f.annotations)
        }

    # ---- entry point ----------------------------------------------------

    def emit(self) -> str:
        # Pre-walk fns so schema-level types (for @pipelined returns) get
        # registered before the package header is emitted.
        for fn in self._fns:
            if any(a.name == "pipelined" for a in fn.annotations):
                self._prepare_pipelined_schema_types(fn)
        chunks: list[str] = []
        chunks.append(self._emit_header())
        if self._errors:
            chunks.append(self._emit_runtime_section())
        if self._schema_types:
            chunks.append("\n".join(self._schema_types))
            chunks.append("")
        chunks.append(self._emit_spec())
        chunks.append("")
        chunks.append(self._emit_body())
        return "\n".join(chunks)

    # ---- pipelined schema types ----------------------------------------

    def _prepare_pipelined_schema_types(self, fn: A.FnDef) -> None:
        """Emit the OBJECT + nested-table CREATE TYPEs needed by an @pipelined
        function's *return* element type. The cursor input's element type only
        needs the package-private RECORD type (declared in the spec); BULK
        COLLECT INTO won't accept a table-of-OBJECT from a multi-column cursor,
        so we don't need a schema-level OBJECT for cursor inputs."""
        rt = fn.return_type
        elem = self._stream_element_type(rt)
        if elem is None:
            raise EmitError(
                f"@pipelined fn {fn.name!r} must return stream<T> where T is a record",
                fn.loc,
            )
        self._emit_obj_type(elem, fn.loc, with_table=True)

    def _emit_obj_type(self, rec_name: str, loc: A.Loc, *, with_table: bool) -> None:
        rec = self._lookup_record(rec_name)
        if rec is None:
            raise EmitError(
                f"pipelined fn references record {rec_name!r} which is not declared in this module",
                loc,
            )
        obj_name = f"{self.pkg}_{rec.name.lower()}_obj"
        if obj_name not in self._obj_emitted:
            # OBJECT attribute types DO take size specifiers (unlike parameter
            # types), so we use the non-param lowering here.
            field_lines = [
                f"  {f.name.lower()} {self._lt(f.type_ref, sql_context=True)}"
                for f in rec.fields
            ]
            self._schema_types.append(
                f"CREATE OR REPLACE TYPE {obj_name} AS OBJECT (\n"
                + ",\n".join(field_lines)
                + "\n);\n/"
            )
            self._obj_emitted.add(obj_name)
        if with_table:
            nt_name = f"{self.pkg}_{rec.name.lower()}_nt"
            nt_key = f"NT:{nt_name}"
            if nt_key not in self._obj_emitted:
                self._schema_types.append(
                    f"CREATE OR REPLACE TYPE {nt_name} AS TABLE OF {obj_name};\n/"
                )
                self._obj_emitted.add(nt_key)

    def _stream_element_type(self, t: Optional[A.TypeRef]) -> Optional[str]:
        if isinstance(t, A.GenericType) and t.base == "stream" and t.params:
            inner = t.params[0]
            if isinstance(inner, A.NamedType):
                return inner.name
        return None

    def _cursor_element_type(self, t: A.TypeRef) -> Optional[str]:
        if isinstance(t, A.GenericType) and t.base == "cursor" and t.params:
            inner = t.params[0]
            if isinstance(inner, A.NamedType):
                return inner.name
        return None

    def _lookup_record(self, name: str) -> Optional[A.RecordDef]:
        for r in self._records:
            if r.name == name:
                return r
        return None

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
        # Pre-walk public fns to register any list types referenced in their
        # signatures so we can emit them in the spec (alongside records).
        spec_list_types: list[str] = []
        seen: set[str] = set()
        for fn in self._fns:
            if not fn.is_pub:
                continue
            for t in [fn.return_type] + [p.type_ref for p in fn.params]:
                decl = self._list_type_decl_for(t)
                if decl is not None and decl[0] not in seen:
                    seen.add(decl[0])
                    spec_list_types.append(decl[1])
        out: list[str] = []
        out.append(f"CREATE OR REPLACE PACKAGE {self.pkg} AS")
        # records (declare types public so callers can reference them)
        for rec in self._records:
            if rec.is_pub:
                out.append(self._render_record_type(rec, indent="  "))
        # list types referenced by public fn signatures
        for line in spec_list_types:
            out.append(line)
        # public fn signatures
        for fn in self._fns:
            if fn.is_pub:
                out.append("  " + self._fn_signature(fn) + ";")
        out.append(f"END {self.pkg};")
        out.append("/")
        # Pre-seed the body's dedup set so we don't redeclare the same TYPE.
        self._list_types_emitted.update(seen)
        return "\n".join(out)

    def _list_type_decl_for(self, t: Optional[A.TypeRef]) -> Optional[tuple[str, str]]:
        """If t is a list<X>, return (type_name, full decl string for the spec).
        Otherwise None."""
        if not isinstance(t, A.GenericType) or t.base != "list" or not t.params:
            return None
        elem_t = t.params[0]
        elem_sql = self._lt(elem_t)
        list_type = f"t_{_safe(_render_type(elem_t))}_list"
        decl = f"  TYPE {list_type} IS TABLE OF {elem_sql} INDEX BY PLS_INTEGER;"
        return (list_type, decl)

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
        # collected list types (assoc array INDEX BY PLS_INTEGER) — those
        # referenced by public fn signatures were already emitted in the spec.
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
            field_lines.append(f"{indent}  {f.name.lower()} {self._lt(f.type_ref)}")
        lines.append(",\n".join(field_lines))
        lines.append(f"{indent});")
        return "\n".join(lines)

    # ---- fn signatures & bodies -----------------------------------------

    def _fn_signature(self, fn: A.FnDef) -> str:
        ann_names = {a.name for a in fn.annotations}
        is_pipelined = "pipelined" in ann_names
        # Pipelined fns get a custom signature: cursor params become SYS_REFCURSOR
        # and the return type is the schema-level nested table.
        if is_pipelined:
            params = ", ".join(
                f"{param_name(p.name)} IN {self._pipelined_param_type(p.type_ref)}"
                for p in fn.params
            )
            elem = self._stream_element_type(fn.return_type)
            assert elem is not None, "checked earlier"
            rec_name = elem.lower()
            nt_name = f"{self.pkg}_{rec_name}_nt"
            sig = f"FUNCTION {fn_pl_name(fn.name)}"
            if params:
                sig += f"({params})"
            sig += f" RETURN {nt_name} PIPELINED"
            return sig
        params = ", ".join(
            f"{param_name(p.name)} IN {self._lt(p.type_ref, param=True)}"
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
            sig += f" RETURN {self._lt(ret, param=True)}"
        # signature-level annotations: DETERMINISTIC, RESULT_CACHE
        if "deterministic" in ann_names:
            sig += " DETERMINISTIC"
        if "result_cache" in ann_names:
            sig += " RESULT_CACHE"
        return sig

    def _pipelined_param_type(self, t: A.TypeRef) -> str:
        """Lower a parameter type when inside a pipelined fn.

        cursor<T> -> SYS_REFCURSOR, everything else uses the normal rules.
        """
        if isinstance(t, A.GenericType) and t.base == "cursor":
            return "SYS_REFCURSOR"
        return self._lt(t, param=True)

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
        self._cursor_params = {}
        for p in fn.params:
            elem = self._cursor_element_type(p.type_ref)
            if elem is not None:
                self._cursor_params[p.name] = elem

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
        is_autonomous = "autonomous" in {a.name for a in fn.annotations}
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
        elif is_autonomous:
            # Autonomous transactions must close with COMMIT or ROLLBACK before
            # the procedure returns (Oracle raises ORA-06519 otherwise). Wrap
            # the body in BEGIN/EXCEPTION: COMMIT on success, ROLLBACK+RAISE
            # on error.
            out.append("    BEGIN")
            if body_stmt_lines:
                out.extend(body_stmt_lines)
            else:
                out.append("      NULL;")
            out.append("      COMMIT;")
            out.append("    EXCEPTION")
            out.append("      WHEN OTHERS THEN")
            out.append("        ROLLBACK;")
            out.append("        RAISE;")
            out.append("    END;")
        else:
            if body_stmt_lines:
                out.extend(body_stmt_lines)
            else:
                out.append("    NULL;")
        # PIPELINED fns must terminate with `RETURN;`
        if "pipelined" in {a.name for a in fn.annotations}:
            out.append("    RETURN;")
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
        if isinstance(s, A.ForallStmt):
            return self._emit_forall(s, indent)
        if isinstance(s, A.MatchStmt):
            return self._emit_match(s, indent)
        if isinstance(s, A.TransactionStmt):
            return self._emit_transaction(s, indent)
        if isinstance(s, A.ExprStmt):
            return self._emit_expr_stmt(s, indent)
        if isinstance(s, A.YieldStmt):
            return self._emit_yield(s, indent)
        return [f"{indent}-- TODO: stmt {type(s).__name__}"]

    def _emit_yield(self, s: A.YieldStmt, indent: str) -> list[str]:
        """`yield Foo { a: 1, b: 2 };` → `PIPE ROW(<pkg>_foo_obj(1, 2));`.

        Only legal inside an @pipelined fn (the typer doesn't enforce this yet
        — a stray yield will simply emit a reference to an undeclared obj).
        """
        v = s.value
        if not isinstance(v, A.StructLit):
            raise EmitError("yield must be a record literal", s.loc)
        rec = self._lookup_record(v.type_name)
        if rec is None:
            raise EmitError(f"yield: unknown record type {v.type_name!r}", s.loc)
        # field order: emit in record-declaration order, looking up each field's value
        provided = {f.name: f.value for f in v.fields}
        missing = [f.name for f in rec.fields if f.name not in provided]
        if missing:
            raise EmitError(
                f"yield {v.type_name}: missing fields {missing}",
                s.loc,
            )
        obj_name = f"{self.pkg}_{v.type_name.lower()}_obj"
        args = ", ".join(self._emit_expr(provided[f.name]) for f in rec.fields)
        return [f"{indent}PIPE ROW({obj_name}({args}));"]

    def _emit_let(self, s: A.LetStmt, indent: str) -> list[str]:
        nm = local_name(s.name)
        # decide type for declaration
        ty = self._lt(s.type_annot) if s.type_annot else None
        # Special case: `let x: list<T> = [a, b, c];` — declare an INDEX BY
        # PLS_INTEGER table, then emit per-index assignments.
        if (
            isinstance(s.type_annot, A.GenericType)
            and s.type_annot.base == "list"
            and isinstance(s.value, A.ListLit)
        ):
            return self._emit_list_let(s, nm, indent)
        # Special case: `let x: list<T> = <expr>;` where expr is not a list
        # literal (typically `sql!{...}.collect()`). Register the list type
        # and the local so subsequent for/forall loops over it work.
        if (
            isinstance(s.type_annot, A.GenericType)
            and s.type_annot.base == "list"
        ):
            return self._emit_list_let_from_expr(s, nm, indent)
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

    def _emit_list_let_from_expr(self, s: A.LetStmt, nm: str, indent: str) -> list[str]:
        """`let xs: list<T> = <expr>;` for non-literal RHS (e.g. .collect())."""
        assert isinstance(s.type_annot, A.GenericType)
        elem_t = s.type_annot.params[0]
        elem_sql = self._lt(elem_t)
        list_type = f"t_{_safe(_render_type(elem_t))}_list"
        if list_type not in self._list_types_emitted:
            self._list_type_decls.append(
                f"  TYPE {list_type} IS TABLE OF {elem_sql} INDEX BY PLS_INTEGER;"
            )
            self._list_types_emitted.add(list_type)
        self._decl(f"{nm} {list_type};")
        self._local_types[s.name] = list_type
        self._list_locals[s.name] = elem_sql
        if s.value is None:
            return []
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
        elem_sql = self._lt(elem_t)
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
        # Reduce any pipeline expressions first so the downstream dispatch can
        # see the underlying SqlBlock or method call shape.
        if isinstance(expr, A.PipelineExpr):
            expr = self._reduce_pipeline(expr)
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
                return self._lt(call.type_args[0])
            if method == "into" and call.type_args:
                return self._lt(call.type_args[0])
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
                    return self._lt(rt.params[0])
                if isinstance(rt, A.OptionalType):
                    return self._lt(rt.inner)
                return self._lt(rt)
        return None

    def _row_type_from_fn_return(self) -> Optional[str]:
        """If the enclosing fn returns Result<T, _> or Option<T> or T directly,
        return the PL/SQL type for T."""
        if self._current_fn is None or self._current_fn.return_type is None:
            return None
        rt = self._current_fn.return_type
        if isinstance(rt, A.GenericType) and rt.base in ("Result", "Option") and rt.params:
            return self._lt(rt.params[0])
        if isinstance(rt, A.OptionalType):
            return self._lt(rt.inner)
        return self._lt(rt)

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
        # `.collect()` on a SELECT → BULK COLLECT INTO
        if isinstance(call.callee, A.MemberAccess) and call.callee.field == "collect":
            recv = call.callee.obj
            _, sql = self._strip_lock_modifiers(recv)
            if sql is not None and not sql.is_dml:
                return self._emit_bulk_collect_into(target, sql, indent)
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

    def _emit_bulk_collect_into(self, target: str, sql: A.SqlBlock, indent: str) -> list[str]:
        """Lower `<sql_select>.collect()` to `SELECT … BULK COLLECT INTO target …`."""
        import re
        sql_text = self._rewrite_binds(sql.sql).strip().rstrip(";")
        m = re.search(r"\s+from\s+", sql_text, re.IGNORECASE)
        if m:
            head = sql_text[:m.start()]
            tail = sql_text[m.end():]
            spliced = f"{head}\n      BULK COLLECT INTO {target}\n      FROM {tail}"
        else:
            spliced = f"{sql_text}\n      BULK COLLECT INTO {target}"
        return [f"{indent}{spliced.strip()};"]

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
        it in at that boundary. If the enclosing fn returns Result<T, ...>
        and its error union contains a NotFound-named variant, NO_DATA_FOUND
        is mapped to that typed error; otherwise re-raised as-is.
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
        # NO_DATA_FOUND handler: typed mapping if fn declares a NotFound variant
        nf_variant = self._notfound_variant_for_current_fn()
        if nf_variant is not None:
            exn = self._exception_name(nf_variant)
            nf_handler = (
                f"{indent}  WHEN NO_DATA_FOUND THEN\n"
                f"{indent}    pell_runtime.set_err('{exn}:1', '');\n"
                f"{indent}    RAISE pell_runtime.{exn};"
            )
        else:
            nf_handler = f"{indent}  WHEN NO_DATA_FOUND THEN RAISE;"
        return [
            f"{indent}BEGIN",
            f"{indent}  {spliced.strip()};",
            f"{indent}EXCEPTION",
            nf_handler,
            f"{indent}  WHEN TOO_MANY_ROWS THEN RAISE;",
            f"{indent}END;",
        ]

    def _notfound_variant_for_current_fn(self) -> Optional[str]:
        """If the current fn returns Result<T, ...> and the error union
        contains a variant named `NotFound` (or ending in `NotFound`),
        return that variant's name. Otherwise None.
        """
        if self._current_fn is None or self._current_fn.return_type is None:
            return None
        rt = self._current_fn.return_type
        if not (isinstance(rt, A.GenericType) and rt.base == "Result" and len(rt.params) >= 2):
            return None
        err = rt.params[1]
        variants: list[str] = []
        if isinstance(err, A.NamedType):
            variants.append(err.name)
        elif isinstance(err, A.ErrorUnionType):
            for v in err.variants:
                if isinstance(v, A.NamedType):
                    variants.append(v.name)
        for v in variants:
            if v == "NotFound" or v.endswith("NotFound"):
                return v
        return None

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
        # for x in <cursor param>: streaming bulk-fetch loop
        if isinstance(s.iterable, A.Ident) and s.iterable.name in self._cursor_params:
            cursor_param = param_name(s.iterable.name)
            elem_rec_name = self._cursor_params[s.iterable.name]
            rec = self._lookup_record(elem_rec_name)
            if rec is None:
                raise EmitError(
                    f"cursor element type {elem_rec_name!r} must be a declared record",
                    s.loc,
                )
            # Bulk-fetch buffer holds RECORDS, not OBJECTs — Oracle won't
            # BULK COLLECT INTO a table-of-OBJECT from a multi-column cursor.
            # The OBJECT type is only needed for PIPE ROW output.
            rec_type = _record_type_name(elem_rec_name)
            buf_type = f"t_{elem_rec_name.lower()}_buf"
            buf_local = local_name(s.var_name) + "_buf"
            self._decl(f"TYPE {buf_type} IS TABLE OF {rec_type} INDEX BY PLS_INTEGER;")
            self._decl(f"{buf_local} {buf_type};")
            idx = f"i_{s.var_name}"
            # Inside the body, references to `s.var_name` resolve to
            # `<buf_local>(idx)`, and `:s.var_name` in sql!{} likewise.
            self._loop_vars.append({s.var_name})
            prev_override = self._loop_var_override.get(s.var_name)
            self._loop_var_override[s.var_name] = f"{buf_local}({idx})"
            body_lines: list[str] = []
            for stmt in s.body:
                body_lines.extend(self._emit_stmt(stmt, indent + "    "))
            self._loop_vars.pop()
            if prev_override is None:
                del self._loop_var_override[s.var_name]
            else:
                self._loop_var_override[s.var_name] = prev_override
            out = [
                f"{indent}LOOP",
                f"{indent}  FETCH {cursor_param} BULK COLLECT INTO {buf_local} LIMIT 100;",
                f"{indent}  EXIT WHEN {buf_local}.COUNT = 0;",
                f"{indent}  FOR {idx} IN 1 .. {buf_local}.COUNT LOOP",
            ]
            out.extend(body_lines)
            out += [
                f"{indent}  END LOOP;",
                f"{indent}END LOOP;",
                f"{indent}CLOSE {cursor_param};",
            ]
            return out
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
        # `for i in xs.indices()` — iterate integer index range FIRST..LAST
        if (
            isinstance(s.iterable, A.Call)
            and isinstance(s.iterable.callee, A.MemberAccess)
            and s.iterable.callee.field == "indices"
            and isinstance(s.iterable.callee.obj, A.Ident)
            and s.iterable.callee.obj.name in self._list_locals
            and not s.iterable.args
        ):
            list_local = local_name(s.iterable.callee.obj.name)
            out = [
                f"{indent}FOR {s.var_name} IN {list_local}.FIRST .. {list_local}.LAST LOOP"
            ]
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

    def _emit_forall(self, s: A.ForallStmt, indent: str) -> list[str]:
        """Lower `forall n in nums { sql!{...:n...} }` to PL/SQL FORALL.

        Body must be a single DML `sql!{}` statement. The loop variable name
        is substituted directly into the DML's bind references — there is no
        intermediate per-iteration local (FORALL uses the iterator as an
        L-value directly).
        """
        if not isinstance(s.iterable, A.Ident) or s.iterable.name not in self._list_locals:
            raise EmitError(
                "forall iterable must be a list-typed local variable",
                s.loc,
            )
        if len(s.body) != 1 or not isinstance(s.body[0], A.ExprStmt) or not isinstance(s.body[0].expr, A.SqlBlock):
            raise EmitError(
                "forall body must be exactly one DML sql!{} statement",
                s.loc,
            )
        sql: A.SqlBlock = s.body[0].expr
        if not sql.is_dml:
            raise EmitError(
                "forall body must be a DML statement (insert/update/delete/merge)",
                s.loc,
            )
        list_local = local_name(s.iterable.name)
        idx = f"i_{s.var_name}"
        # Custom bind rewrite: :<loop_var> → <list_local>(idx); other binds
        # follow the usual param/local rules.
        import re
        loop_var = s.var_name
        def repl(m: "re.Match[str]") -> str:
            name = m.group(1)
            if name == loop_var:
                return f"{list_local}({idx})"
            if name in self._params:
                return param_name(name)
            return local_name(name)
        sql_text = re.sub(
            r"(?<![A-Za-z0-9_]):([A-Za-z_][A-Za-z0-9_]*)",
            repl,
            sql.sql,
        ).strip().rstrip(";")
        out = [f"{indent}FORALL {idx} IN {list_local}.FIRST .. {list_local}.LAST"]
        for line in sql_text.splitlines():
            out.append(f"{indent}  {line}")
        if not out[-1].rstrip().endswith(";"):
            out[-1] = out[-1] + ";"
        return out

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
        if isinstance(e, A.PipelineExpr):
            return self._emit_expr(self._reduce_pipeline(e))
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

    def _reduce_pipeline(self, pe: A.PipelineExpr) -> A.Expr:
        """Reduce `source |> target` to a non-pipeline AST node.

        Cases (target):
          1. Ident(fn) where fn is @pipelined          → wrap as SELECT * FROM
                                                          TABLE(fn(CURSOR(<source SQL>)))
          2. Call(Ident(fn), args) where fn is @pipelined → same as (1) with extra args
          3. Ident(method) ∈ {collect, one, first, ...} → method call on source
          4. Call(Ident(method), args) ∈ {…}            → method call with args
        Source is reduced first if it's itself a PipelineExpr.
        """
        source = pe.source
        if isinstance(source, A.PipelineExpr):
            source = self._reduce_pipeline(source)
        target = pe.target
        # Unwrap to (callee_name, args, type_args)
        callee_name: Optional[str] = None
        args: list[A.Expr] = []
        type_args: list[A.TypeRef] = []
        if isinstance(target, A.Ident):
            callee_name = target.name
        elif isinstance(target, A.Call) and isinstance(target.callee, A.Ident):
            callee_name = target.callee.name
            args = target.args
            type_args = target.type_args
        if callee_name is None:
            raise EmitError(
                "|> target must be a fn name or a method-style call (collect()/one()/…)",
                pe.loc,
            )
        if callee_name in self._pipelined_fn_names:
            return self._wrap_pipelined(source, callee_name, args, pe.loc)
        # treat as a method on the (already-reduced) source
        return A.Call(
            loc=pe.loc,
            callee=A.MemberAccess(loc=pe.loc, obj=source, field=callee_name),
            args=args,
            type_args=type_args,
        )

    def _wrap_pipelined(
        self,
        source: A.Expr,
        fn_name: str,
        extra_args: list[A.Expr],
        loc: A.Loc,
    ) -> A.SqlBlock:
        """Synthesize `select <fields> from table(<fn>(cursor(<source>), <extra>)) t`.

        We project the OBJECT's attributes by name so the downstream caller can
        BULK COLLECT INTO a record-table whose fields match.
        """
        if not isinstance(source, A.SqlBlock):
            raise EmitError(
                "|> requires the upstream of a pipelined fn to be a sql!{ select ... } block",
                loc,
            )
        # Look up the fn to find its return element type's fields
        target_fn = next((f for f in self._fns if f.name == fn_name), None)
        if target_fn is None:
            raise EmitError(f"|> target {fn_name!r} not found in this module", loc)
        elem = self._stream_element_type(target_fn.return_type)
        if elem is None:
            raise EmitError(
                f"|> target {fn_name!r} must return stream<T>",
                loc,
            )
        rec = self._lookup_record(elem)
        if rec is None:
            raise EmitError(
                f"|> target {fn_name!r}: element type {elem!r} is not a declared record",
                loc,
            )
        projection = ", ".join(f"t.{f.name.lower()}" for f in rec.fields)
        inner_sql = source.sql.strip().rstrip(";")
        extra = ", ".join(self._emit_expr(a) for a in extra_args)
        extra_str = f", {extra}" if extra else ""
        wrapped = (
            f"select {projection} from table({fn_name.lower()}"
            f"(cursor({inner_sql}){extra_str})) t"
        )
        return A.SqlBlock(
            loc=loc,
            sql=wrapped,
            binds=source.binds,
            is_dml=False,
            has_returning=False,
        )

    def _emit_text_lit(self, e: A.TextLit) -> str:
        """A text literal with `{expr}` placeholders lowers to `'lit ' || <expr>`.

        The placeholder content is lexed and parsed as a pell expression, so
        identifiers, member access, and method calls all work
        (`{name}`, `{p.field}`, `{bulk.rowcount(i)}`).
        Brace doubling escapes: `{{` → `{`, `}}` → `}`.
        """
        import re
        from .lexer import tokenize
        from .parser import Parser, ParseError
        s = e.value
        if "{" not in s and "}" not in s:
            return _sql_string(s)
        # walk the string, accumulating literal runs and {expr} interpolations
        chunks: list[str] = []
        buf: list[str] = []
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "{" and i + 1 < len(s) and s[i + 1] == "{":
                buf.append("{")
                i += 2
                continue
            if ch == "}" and i + 1 < len(s) and s[i + 1] == "}":
                buf.append("}")
                i += 2
                continue
            if ch == "{":
                # find the matching `}` (no nested {} expected in v0)
                end = s.find("}", i + 1)
                if end == -1:
                    raise EmitError(f"unterminated `{{` in string literal at {e.loc}", e.loc)
                if buf:
                    chunks.append(_sql_string("".join(buf)))
                    buf = []
                expr_src = s[i + 1 : end]
                try:
                    expr_toks = tokenize(expr_src, str(e.loc))
                    p = Parser(expr_toks)
                    sub_expr = p._parse_expr()
                except (ParseError, Exception) as err:
                    raise EmitError(
                        f"bad interpolation `{{{expr_src}}}`: {err}", e.loc
                    )
                chunks.append(self._emit_expr(sub_expr))
                i = end + 1
                continue
            buf.append(ch)
            i += 1
        if buf:
            chunks.append(_sql_string("".join(buf)))
        if not chunks:
            return "''"
        if len(chunks) == 1:
            return chunks[0]
        return "(" + " || ".join(chunks) + ")"

    def _emit_call_expr(self, e: A.Call) -> str:
        # Detect simple method calls and inline them.
        if isinstance(e.callee, A.MemberAccess):
            recv = e.callee.obj
            method = e.callee.field
            # bulk.rowcount(i) / bulk.total() — magic accessors valid right after
            # a FORALL or any DML; lowered to SQL%BULK_ROWCOUNT(i) / SQL%ROWCOUNT.
            # The compiler does not yet enforce "must follow a FORALL" statically.
            if isinstance(recv, A.Ident) and recv.name == "bulk":
                if method == "rowcount" and len(e.args) == 1:
                    return f"SQL%BULK_ROWCOUNT({self._emit_expr(e.args[0])})"
                if method == "total" and not e.args:
                    return "SQL%ROWCOUNT"
                raise EmitError(
                    f"unknown bulk.{method}; expected `bulk.rowcount(i)` or `bulk.total()`",
                    e.loc,
                )
            # List-typed receivers: .len() / .first() / .last() / .at(i)
            if isinstance(recv, A.Ident) and recv.name in self._list_locals:
                list_local = local_name(recv.name)
                if method == "len" and not e.args:
                    return f"{list_local}.COUNT"
                if method == "first" and not e.args:
                    return f"{list_local}.FIRST"
                if method == "last" and not e.args:
                    return f"{list_local}.LAST"
                if method == "at" and len(e.args) == 1:
                    return f"{list_local}({self._emit_expr(e.args[0])})"
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


def emit(module: A.Module, target: str = "23") -> str:
    return Emitter(module, target=target).emit()
