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


# Pell param mode → PL/SQL parameter mode keyword.
PARAM_MODE_PL: dict[str, str] = {"in": "IN", "out": "OUT", "inout": "IN OUT"}


# Each error category gets a distinct SQLCODE range; @retry uses the range
# to decide whether a raised error is panic-class (re-raise immediately, no
# retry) or normal (count an attempt, sleep, try again). The ranges are
# disjoint so a single integer test classifies the error.
SQLCODE_BASE: dict[str, int] = {
    "propagate": 20100,  # -20100 .. -20199
    "skip":      20200,  # -20200 .. -20299
    "panic":     20300,  # -20300 .. -20399
}
SQLCODE_PANIC_LO = -(SQLCODE_BASE["panic"] + 99)  # -20399
SQLCODE_PANIC_HI = -(SQLCODE_BASE["panic"] +  0)  # -20300

# Oracle built-ins that should be classified as panic — invariant
# violations, programming bugs, infrastructure failures that retry won't
# fix. Anything not in this set defaults to propagate (retryable).
ORACLE_PANIC_SQLCODES: tuple[int, ...] = (
    -1476,   # ZERO_DIVIDE
    -6502,   # VALUE_ERROR — type/conversion bug
    -6592,   # CASE_NOT_FOUND
    -4068,   # existing state of packages has been discarded
    -1410,   # invalid ROWID
    -1483,   # invalid LENGTH for DATE/NUMBER bind
    -6500,   # PL/SQL: storage error
    -6501,   # PL/SQL: program error
    -6504,   # PL/SQL: cursor variables in mismatch
    -7445,   # core dump
)


# Method-style aliases — each lowers `recv.method(args...)` to a fixed SQL
# fragment. The receiver is rendered exactly once; arguments are rendered
# left-to-right. These are dispatched AFTER object/method dispatch on
# user-defined types (so `user_type.contains(x)` still calls the user's
# method if defined) and AFTER list-method handling.
_METHOD_ALIASES: dict[str, tuple[int, str]] = {
    # String predicates
    "contains":    (1, "(INSTR({recv}, {arg0}) > 0)"),
    "starts_with": (1, "({recv} LIKE {arg0} || '%')"),
    "ends_with":   (1, "({recv} LIKE '%' || {arg0})"),
    "is_empty":    (0, "({recv} IS NULL OR LENGTH({recv}) = 0)"),
    # Date / timestamp components
    "year":        (0, "EXTRACT(YEAR FROM {recv})"),
    "month":       (0, "EXTRACT(MONTH FROM {recv})"),
    "day":         (0, "EXTRACT(DAY FROM {recv})"),
    "hour":        (0, "EXTRACT(HOUR FROM {recv})"),
    "minute":      (0, "EXTRACT(MINUTE FROM {recv})"),
    "second":      (0, "EXTRACT(SECOND FROM {recv})"),
    # Date arithmetic: omitted — `d + n` already works via BinOp.
}


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
        if t.base == "rowtype" and t.params:
            # rowtype<table_or_view> → table_or_view%ROWTYPE.
            # The argument's name is used verbatim (case-folded to lower);
            # pell does no validation — Oracle resolves the table at compile
            # time. LSP can squiggle missing/typo'd field accesses against
            # the schema snapshot when that lands.
            inner = t.params[0]
            if isinstance(inner, A.NamedType):
                return f"{inner.name.lower()}%ROWTYPE"
            raise ValueError(
                f"rowtype<...> must take a table or view name, got {type(inner).__name__}"
            )
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
        if t.base == "cursor":
            # `cursor<T>` outside @pipelined contexts lowers to a weakly-typed
            # SYS_REFCURSOR. Pipelined fns get their own param-type lowering
            # (strong REF CURSOR when @parallel partition= is set).
            return "SYS_REFCURSOR"
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
    def __init__(self, module: A.Module, target: str = "23", *,
                 source_text: Optional[str] = None,
                 source_path: Optional[str] = None,
                 reproducible: bool = False):
        if target not in SUPPORTED_TARGETS:
            raise ValueError(
                f"unsupported target {target!r}; must be one of {SUPPORTED_TARGETS}"
            )
        self.module = module
        self.target = target
        self.source_text = source_text          # for SHA-256 in the preamble
        self.source_path = source_path or (module.loc.file if module.loc else None)
        # When True, omit volatile preamble fields (build timestamp, uncommitted
        # working-tree hash) so the emitted SQL is byte-stable across runs from
        # the same source + commit. Used for golden-snapshot tests.
        self.reproducible = reproducible
        # Schema/package split — first dotted node of the module name becomes the
        # PL/SQL schema; the rest is mangled into the package name. Single-node
        # modules (`module foo;`) get no schema qualifier (backwards compat).
        parts = module.name.split(".")
        if len(parts) >= 2:
            self.schema = parts[0]
            self.pkg = "_".join(parts[1:])
        else:
            self.schema = None
            self.pkg = parts[0]
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
        self._types: list[A.TypeDef] = [i for i in module.items if isinstance(i, A.TypeDef)]
        self._sealed_types: list[A.SealedTypeDef] = [i for i in module.items if isinstance(i, A.SealedTypeDef)]
        self._aggregates: list[A.AggregateDef] = [i for i in module.items if isinstance(i, A.AggregateDef)]
        self._sequences: list[A.SequenceDef] = [i for i in module.items if isinstance(i, A.SequenceDef)]
        # Sequence names — used by _lower_ident to skip the `l_` local prefix
        # and by type inference to type `<seq>.nextval` / `<seq>.currval` as NUMBER.
        self._seq_names: set[str] = {s.name for s in self._sequences}
        # Enums — map `<EnumName>::<VARIANT>` to the lowered text literal.
        self._enums: list[A.EnumDef] = [i for i in module.items if isinstance(i, A.EnumDef)]
        self._enum_variants: dict[tuple[str, str], str] = {}
        for e in self._enums:
            for v in e.variants:
                lit = v.value if v.value is not None else v.name
                self._enum_variants[(e.name, v.name)] = lit
        # Names of declared enums for reference in type positions.
        self._enum_names: set[str] = {e.name for e in self._enums}
        # Per-category SQLCODE assignment for declared errors. Each error gets
        # a unique negative integer within its category's range. Computed once
        # in __init__ so any helper (RAISE emit, runtime section, etc.) can
        # ask "what code goes with `NotFound`?" without re-walking.
        self._error_sqlcodes: dict[str, int] = {}
        _counters = {"propagate": 0, "skip": 0, "panic": 0}
        for e in self._errors:
            cat = getattr(e, "category", "propagate")
            if cat not in SQLCODE_BASE:
                raise EmitError(
                    f"error {e.name!r}: unknown category {cat!r} "
                    "(expected skip / propagate / panic)",
                    e.loc,
                )
            idx = _counters[cat]
            if idx >= 99:
                raise EmitError(
                    f"too many {cat}-category errors in module {module.name} "
                    f"(SQLCODE range exhausted)",
                    e.loc,
                )
            _counters[cat] += 1
            self._error_sqlcodes[e.name] = -(SQLCODE_BASE[cat] + idx)
        # all module-private and module-public fn names
        self._fn_names: set[str] = {f.name for f in self._fns}
        # names of declared types (so StructLit on a type emits an OBJECT constructor,
        # not a record-style field-by-field assignment)
        self._type_names: set[str] = {t.name for t in self._types} | {
            c.name for st in self._sealed_types for c in st.cases
        } | {st.name for st in self._sealed_types}
        # method-emission state — set by _emit_method, used by _lower_ident
        self._in_method_type: Optional[str] = None  # type name being emitted (for `self`)
        # True while emitting an aggregate's `finish()` body — return X gets
        # rewritten to `returnValue := X; RETURN ODCIConst.Success;`.
        self._in_aggregate_terminate: bool = False
        # Set when any `.split()` call is emitted — triggers a per-module
        # private helper function `pell_split_text` in the package body.
        self._needs_split_helper: bool = False
        # Set when any `@retry` annotation is emitted — triggers a per-module
        # private helper `pell_is_panic(p_code NUMBER) RETURN BOOLEAN` so the
        # retry handler can decide whether to re-raise without retry.
        self._needs_panic_helper: bool = False
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
        # §5.2 / §5.3 — types, sealed hierarchies, aggregates emit schema-level
        # CREATE TYPE / CREATE TYPE BODY / CREATE FUNCTION ... AGGREGATE USING.
        for t in self._types:
            self._emit_user_type(t)
        for st in self._sealed_types:
            self._emit_sealed_type(st)
        for ag in self._aggregates:
            self._emit_aggregate(ag)
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
                f"CREATE OR REPLACE TYPE {self._q(obj_name)} AS OBJECT (\n"
                + ",\n".join(field_lines)
                + "\n);\n/"
            )
            self._obj_emitted.add(obj_name)
        if with_table:
            nt_name = f"{self.pkg}_{rec.name.lower()}_nt"
            nt_key = f"NT:{nt_name}"
            if nt_key not in self._obj_emitted:
                self._schema_types.append(
                    f"CREATE OR REPLACE TYPE {self._q(nt_name)} AS TABLE OF {obj_name};\n/"
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
        from . import __version__ as _PELL_VERSION
        import hashlib, datetime, subprocess, os
        lines: list[str] = []
        lines.append("-- " + "=" * 70)
        lines.append(f"-- Generated by pell {_PELL_VERSION} from module {self.module.name}")
        if self.source_path:
            import os
            disp = self.source_path
            if self.reproducible:
                # Relative path so snapshots are portable across machines.
                try:
                    disp = os.path.relpath(self.source_path)
                except ValueError:
                    pass
            lines.append(f"--   Source:     {disp}")
        if self.source_text is not None:
            sha = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
            lines.append(f"--   SHA-256:    {sha}")
        # Best-effort git provenance. Failures (no git, no .git) silently skip.
        # In reproducible mode we keep the commit hash but drop the
        # uncommitted-tree hash — otherwise every edit causes snapshot churn.
        git_info = self._git_info(omit_dirty=self.reproducible)
        if git_info:
            lines.append(f"--   pell git:   {git_info}")
        lines.append(f"--   Target:     Oracle {self.target}")
        lines.append(f"--   Schema:     {self.schema or '(none — unqualified)'}")
        if not self.reproducible:
            lines.append(f"--   Built at:   {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append("-- DO NOT EDIT — regenerate with `pell build`")
        # Dependency manifest — what this module touches. Static info,
        # extracted shallowly from sql!{} bodies + pell-visible decls.
        from . import deps as _deps
        manifest = _deps.collect_module_deps(self.module)
        if any(manifest.values()):
            lines.append("--")
            lines.append("-- Dependencies (extracted from pell source):")
            for kind in ("tables", "sequences", "packages", "dblinks"):
                vals = manifest.get(kind, [])
                if vals:
                    label = kind + " (incl. views/synonyms)" if kind == "tables" else kind
                    lines.append(f"--   {label}:")
                    for v in vals:
                        lines.append(f"--     {v}")
        lines.append("-- " + "=" * 70 + "\n")
        return "\n".join(lines)

    def _q(self, name: str) -> str:
        """Schema-qualify a top-level object name for CREATE statements.

        Returns `<schema>.<name>` when the module declared a schema (first
        node of the dotted module path); otherwise returns `name` unchanged
        for backwards compat with single-node modules.

        Used only for CREATE / DROP statements. References to objects within
        the same package body remain unqualified — Oracle resolves them via
        the current schema, and a single-schema deploy is the common case.
        Cross-schema references aren't supported in v1.
        """
        return f"{self.schema}.{name}" if self.schema else name

    def _git_info(self, *, omit_dirty: bool = False) -> Optional[str]:
        """Return a `<short-hash>[ + uncommitted: <patch-sha-prefix>]` string,
        or None if we can't get git info. Runs git in the source file's
        directory (or cwd if no source path).

        `omit_dirty=True` suppresses the uncommitted-tree hash even when
        the working tree is dirty — used for reproducible snapshot output.
        """
        import subprocess, hashlib, os
        cwd = os.path.dirname(self.source_path) if self.source_path else None
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=cwd, timeout=2,
            )
            if commit.returncode != 0:
                return None
            sha = commit.stdout.strip()
            if omit_dirty:
                return sha
            diff = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True, text=True, cwd=cwd, timeout=2,
            )
            if diff.returncode == 0 and diff.stdout.strip():
                dirty = hashlib.sha256(diff.stdout.encode("utf-8")).hexdigest()[:8]
                return f"{sha} + uncommitted:{dirty}"
            return f"{sha} (clean)"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

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
            code = self._error_sqlcodes[e.name]
            cat = getattr(e, "category", "propagate")
            lines.append(f"--   {exn} EXCEPTION;  -- {cat}")
            lines.append(f"--   PRAGMA EXCEPTION_INIT({exn}, {code});")
        lines.append("")
        return "\n".join(lines)

    def _exception_name(self, err_name: str) -> str:
        # Exceptions land in the global pell_runtime package, so they have to
        # be globally unique. Use the FULL mangled module name (including the
        # schema prefix) so `hr.employees::NotFound` and `acct.employees::NotFound`
        # don't collide as `employees_notfound` in pell_runtime.
        full = self.module.name.replace(".", "_")
        return f"{full}_{err_name}".lower()

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
        # Pre-walk @pipelined fns with @parallel(partition=…) — those need a
        # strongly-typed REF CURSOR declaration before the fn signature
        # (Oracle PLS-00627 if SYS_REFCURSOR is used).
        strong_cursor_decls: list[str] = []
        seen_cursors: set[str] = set()
        for fn in self._fns:
            if not (any(a.name == "pipelined" for a in fn.annotations)):
                continue
            if not self._fn_has_parallel_partition(fn):
                continue
            for p in fn.params:
                if isinstance(p.type_ref, A.GenericType) and p.type_ref.base == "cursor" and p.type_ref.params:
                    inner = p.type_ref.params[0]
                    if isinstance(inner, A.NamedType):
                        cur_name = f"t_{inner.name.lower()}_cur"
                        if cur_name in seen_cursors:
                            continue
                        seen_cursors.add(cur_name)
                        rec_type = _record_type_name(inner.name)
                        strong_cursor_decls.append(
                            f"  TYPE {cur_name} IS REF CURSOR RETURN {rec_type};"
                        )
        out: list[str] = []
        out.append(f"CREATE OR REPLACE PACKAGE {self._q(self.pkg)} AS")
        # Enum constants — emitted before records so they can be referenced
        # from record field defaults (future) and from fn bodies.
        for e in self._enums:
            if not e.is_pub:
                continue
            out.append(f"  -- enum {e.name}")
            for v in e.variants:
                lit = v.value if v.value is not None else v.name
                out.append(
                    f"  {e.name.lower()}_{v.name.lower()} CONSTANT VARCHAR2(200) := "
                    f"{_sql_string(lit)};"
                )
        # records (declare types public so callers can reference them)
        for rec in self._records:
            if rec.is_pub:
                out.append(self._render_record_type(rec, indent="  "))
        # strongly-typed cursors for parallel pipelined fns (must follow records).
        for line in strong_cursor_decls:
            out.append(line)
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
        out.append(f"CREATE OR REPLACE PACKAGE BODY {self._q(self.pkg)} AS")
        # private record types
        for rec in self._records:
            if not rec.is_pub:
                out.append(self._render_record_type(rec, indent="  "))
        # collected list types (assoc array INDEX BY PLS_INTEGER) — those
        # referenced by public fn signatures were already emitted in the spec.
        for decl in self._list_type_decls:
            out.append(decl)
        # Generated helpers
        if self._needs_split_helper:
            out.append("")
            out.append(self._split_helper_source())
        if self._needs_panic_helper:
            out.append("")
            out.append(self._panic_helper_source())
        # Pinning cursors for @touches dependencies from unsafe fns. Never
        # called; the cursor declarations make Oracle's ALL_DEPENDENCIES
        # track refs that would otherwise be invisible inside EXECUTE
        # IMMEDIATE strings.
        dyn_touches = self._collect_dyn_touches()
        if dyn_touches:
            out.append("")
            out.append("  -- Pinning declarations for dynamic-SQL @touches. Never invoked;")
            out.append("  -- exists so Oracle ALL_DEPENDENCIES tracks references hidden")
            out.append("  -- in EXECUTE IMMEDIATE strings.")
            out.append("  PROCEDURE pell_dep_pinning IS")
            for t in dyn_touches:
                slug = t.replace(".", "_").replace("@", "_at_")
                out.append(f"    CURSOR pin_{slug} IS SELECT 1 FROM {t} WHERE 1=0;")
            out.append("  BEGIN NULL; END pell_dep_pinning;")
        # all fns
        for chunk in fn_chunks:
            out.append("")
            out.append(chunk)
        out.append(f"END {self.pkg};")
        out.append("/")
        return "\n".join(out)

    def _panic_helper_source(self) -> str:
        """A package-private helper for `@retry`. Returns TRUE for SQLCODEs
        classified as `panic` — those should never be retried.

        Includes pell's own panic-category range (-20300 .. -20399) plus
        known Oracle built-in panic codes (ZERO_DIVIDE, VALUE_ERROR,
        package-state-discarded, etc.). Unknown codes return FALSE — the
        retry loop will treat them as retryable.
        """
        oracle_codes = ", ".join(str(c) for c in ORACLE_PANIC_SQLCODES)
        return (
            "  FUNCTION pell_is_panic(p_code IN NUMBER) RETURN BOOLEAN IS\n"
            "  BEGIN\n"
            f"    IF p_code BETWEEN {SQLCODE_PANIC_LO} AND {SQLCODE_PANIC_HI} THEN RETURN TRUE; END IF;\n"
            f"    IF p_code IN ({oracle_codes}) THEN RETURN TRUE; END IF;\n"
            "    RETURN FALSE;\n"
            "  END pell_is_panic;"
        )

    def _split_helper_source(self) -> str:
        """A package-private helper for `.split(delim)`. Splits using regexp."""
        return (
            "  FUNCTION pell_split_text(p_s VARCHAR2, p_delim VARCHAR2) RETURN t_text_list IS\n"
            "    l_result t_text_list;\n"
            "    l_idx PLS_INTEGER := 0;\n"
            "  BEGIN\n"
            "    IF p_s IS NULL OR p_delim IS NULL THEN RETURN l_result; END IF;\n"
            "    FOR rec IN (\n"
            "      SELECT REGEXP_SUBSTR(p_s, '[^' || p_delim || ']+', 1, LEVEL) AS part\n"
            "      FROM dual\n"
            "      CONNECT BY LEVEL <= REGEXP_COUNT(p_s, p_delim) + 1\n"
            "    ) LOOP\n"
            "      IF rec.part IS NOT NULL THEN\n"
            "        l_idx := l_idx + 1;\n"
            "        l_result(l_idx) := rec.part;\n"
            "      END IF;\n"
            "    END LOOP;\n"
            "    RETURN l_result;\n"
            "  END pell_split_text;"
        )

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
                f"{param_name(p.name)} IN {self._pipelined_param_type(p.type_ref, fn)}"
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
            sig += self._pipelined_parallel_clauses(fn)
            return sig
        params = ", ".join(
            f"{param_name(p.name)} {PARAM_MODE_PL[p.mode]} {self._lt(p.type_ref, param=True)}"
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

    def _pipelined_parallel_clauses(self, fn: A.FnDef) -> str:
        """If the @pipelined fn carries @parallel(...), render the
        PARALLEL_ENABLE / PARTITION BY / ORDER BY / CLUSTER BY clauses.

        Supported partition forms:
          partition = any                  → PARTITION p BY ANY
          partition = hash(col1, col2, …)  → PARTITION p BY HASH(col1, col2, …)
          partition = range(col)           → PARTITION p BY RANGE(col)

        ORDER and CLUSTER each take a column tuple; specifying both is a
        compile-time error (Oracle accepts only one).
        """
        ann = next((a for a in fn.annotations if a.name == "parallel"), None)
        if ann is None:
            return ""
        # find the cursor parameter — that's what PARTITION/ORDER/CLUSTER reference
        cursor_param = next(
            (p for p in fn.params
             if isinstance(p.type_ref, A.GenericType) and p.type_ref.base == "cursor"),
            None,
        )
        if cursor_param is None:
            raise EmitError(
                f"@parallel on @pipelined fn {fn.name!r}: function must have a `cursor<T>` parameter",
                ann.loc,
            )
        cursor_pl = param_name(cursor_param.name)
        partition = ann.kwargs.get("partition")
        order = ann.kwargs.get("order")
        cluster = ann.kwargs.get("cluster")
        if partition is None and (order is not None or cluster is not None):
            raise EmitError(
                f"@parallel on @pipelined fn {fn.name!r}: order/cluster require a partition= clause",
                ann.loc,
            )
        if order is not None and cluster is not None:
            raise EmitError(
                f"@parallel on @pipelined fn {fn.name!r}: cannot specify both order and cluster",
                ann.loc,
            )
        # Allow `@parallel` with no kwargs to mean "PARALLEL_ENABLE with no
        # partition clause" — useful for stateless PTFs that Oracle can split
        # freely. Equivalent to partition=any.
        if partition is None and not ann.kwargs:
            return " PARALLEL_ENABLE"
        if partition is None:
            return ""
        partition_str = self._render_partition_spec(partition, ann)
        out = f" PARALLEL_ENABLE(PARTITION {cursor_pl} BY {partition_str})"
        if order is not None:
            cols = self._render_col_tuple(order, ann, "order")
            out += f"\n    ORDER {cursor_pl} BY ({cols})"
        elif cluster is not None:
            cols = self._render_col_tuple(cluster, ann, "cluster")
            out += f"\n    CLUSTER {cursor_pl} BY ({cols})"
        return out

    def _render_partition_spec(self, expr: A.Expr, ann: A.Annotation) -> str:
        """Render a partition= value as the Oracle BY-clause body."""
        if isinstance(expr, A.Ident) and expr.name.lower() == "any":
            return "ANY"
        if isinstance(expr, A.Call) and isinstance(expr.callee, A.Ident):
            kind = expr.callee.name.lower()
            if kind in ("hash", "range"):
                if not expr.args:
                    raise EmitError(
                        f"@parallel partition={kind}(...) requires at least one column",
                        ann.loc,
                    )
                if kind == "range" and len(expr.args) != 1:
                    raise EmitError(
                        "@parallel partition=range(...) takes exactly one column",
                        ann.loc,
                    )
                cols = ", ".join(self._render_col_name(a, ann) for a in expr.args)
                return f"{kind.upper()}({cols})"
        raise EmitError(
            f"@parallel partition= must be `any`, `hash(col, …)`, or `range(col)`; got {type(expr).__name__}",
            ann.loc,
        )

    def _render_col_tuple(self, expr: A.Expr, ann: A.Annotation, label: str) -> str:
        """Render a column tuple expression `(c1, c2, …)` as a comma list."""
        if isinstance(expr, A.TupleLit):
            return ", ".join(self._render_col_name(e, ann) for e in expr.elements)
        if isinstance(expr, A.Ident):
            # A single column needn't be a tuple; accept `order = col`.
            return self._render_col_name(expr, ann)
        raise EmitError(
            f"@parallel {label}= must be a column name or tuple of column names",
            ann.loc,
        )

    def _render_col_name(self, expr: A.Expr, ann: A.Annotation) -> str:
        if isinstance(expr, A.Ident) and "::" not in expr.name:
            return expr.name.lower()
        raise EmitError(
            "@parallel column references must be plain identifiers",
            ann.loc,
        )

    def _pipelined_param_type(self, t: A.TypeRef, fn: Optional[A.FnDef] = None) -> str:
        """Lower a parameter type when inside a pipelined fn.

        cursor<T> -> SYS_REFCURSOR by default; if the fn has `@parallel(partition=…)`,
        Oracle requires a STRONGLY-TYPED ref cursor (PLS-00627), so we emit
        `t_<T>_cur` which is declared in the package spec.
        """
        if isinstance(t, A.GenericType) and t.base == "cursor":
            if fn is not None and self._fn_has_parallel_partition(fn):
                inner = t.params[0] if t.params else None
                if isinstance(inner, A.NamedType):
                    return f"t_{inner.name.lower()}_cur"
            return "SYS_REFCURSOR"
        return self._lt(t, param=True)

    def _fn_has_parallel_partition(self, fn: A.FnDef) -> bool:
        """True if fn has @parallel(...) with a `partition=` clause."""
        ann = next((a for a in fn.annotations if a.name == "parallel"), None)
        return ann is not None and "partition" in ann.kwargs

    def _fn_body_pragmas(self, fn: A.FnDef) -> list[str]:
        """Body-level pragmas: PRAGMA UDF, PRAGMA AUTONOMOUS_TRANSACTION."""
        ann_names = {a.name for a in fn.annotations}
        out: list[str] = []
        if "autonomous" in ann_names:
            out.append("PRAGMA AUTONOMOUS_TRANSACTION;")
        if "udf" in ann_names:
            out.append("PRAGMA UDF;")
        return out

    def _retry_for(self, fn: A.FnDef) -> Optional[dict]:
        """If `fn` has a `@retry(...)` annotation, return its parsed params as
        a dict. Returns None when the annotation isn't present.

        Accepted shape:
            @retry(N)
            @retry(N, backoff_ms = X)
            @retry(N, backoff_ms = X, exponential = true)
            @retry(N, backoff_ms = X, jitter = true)
            @retry(N, backoff_ms = X, cap_ms = Y, exponential = true, jitter = true)

        N is a required positional int >= 1.
        """
        ann = next((a for a in fn.annotations if a.name == "retry"), None)
        if ann is None:
            return None
        if not ann.args or not isinstance(ann.args[0], A.NumberLit):
            raise EmitError(
                f"@retry on fn {fn.name!r}: first argument must be a positive integer literal "
                "(max attempts)",
                ann.loc,
            )
        n = int(ann.args[0].value)
        if n < 1:
            raise EmitError(f"@retry({n}) requires n >= 1", ann.loc)

        def _int_kw(name: str) -> Optional[int]:
            v = ann.kwargs.get(name)
            if v is None:
                return None
            if not isinstance(v, A.NumberLit):
                raise EmitError(
                    f"@retry({name}=...): expected a number literal", ann.loc,
                )
            return int(v.value)

        def _bool_kw(name: str) -> bool:
            v = ann.kwargs.get(name)
            if v is None:
                return False
            if not isinstance(v, A.BoolLit):
                raise EmitError(
                    f"@retry({name}=...): expected true/false", ann.loc,
                )
            return v.value

        return {
            "n":           n,
            "backoff_ms":  _int_kw("backoff_ms") or 0,
            "exponential": _bool_kw("exponential"),
            "jitter":      _bool_kw("jitter"),
            "cap_ms":      _int_kw("cap_ms"),
            "loc":         ann.loc,
        }

    def _retry_sleep_stmt(self, retry: dict) -> Optional[str]:
        """Build the DBMS_SESSION.SLEEP statement for a retry policy, or
        None if no delay is configured."""
        if not retry["backoff_ms"]:
            return None
        expr = f"({retry['backoff_ms']} / 1000)"
        if retry["exponential"]:
            expr = f"({expr} * POWER(2, l_pell_attempt - 1))"
        if retry["jitter"]:
            expr = f"({expr} * (0.75 + DBMS_RANDOM.VALUE * 0.5))"
        if retry["cap_ms"] is not None:
            expr = f"LEAST(({retry['cap_ms']} / 1000), {expr})"
        return f"DBMS_SESSION.SLEEP({expr});"

    def _collect_dyn_touches(self) -> list[str]:
        """All `@touches` table names from unsafe fns in this module, sorted.
        Drives pinning-cursor emission so ALL_DEPENDENCIES sees the refs."""
        names: set[str] = set()
        for fn in self._fns:
            if not getattr(fn, "is_unsafe", False):
                continue
            for ann in fn.annotations:
                if ann.name == "touches":
                    for arg in ann.args:
                        if isinstance(arg, A.Ident):
                            names.add(arg.name.lower())
        return sorted(names)

    def _emit_exec_dyn(self, target: Optional[str], call: A.Call, indent: str) -> list[str]:
        """Lower `exec_dyn(<sql_string>)` to `EXECUTE IMMEDIATE`.

        `target` is the PL/SQL identifier receiving a scalar result, or None
        for DML / bare-statement form.

        Requirements (compile-time error otherwise):
        - Must be called inside an `unsafe fn`.
        - exec_dyn takes exactly one argument — the SQL string expression.
        - USING clause derived from the fn's `@binds(...)` annotation, in
          declaration order. Each name resolves to the matching pell
          variable in scope (parameter, local, or loop variable).
        """
        fn = self._current_fn
        if fn is None or not getattr(fn, "is_unsafe", False):
            raise EmitError(
                "exec_dyn(...) can only be called inside an `unsafe fn`",
                call.loc,
            )
        if len(call.args) != 1:
            raise EmitError(
                f"exec_dyn takes exactly 1 argument (the SQL string), got {len(call.args)}",
                call.loc,
            )
        sql_code = self._emit_expr(call.args[0])
        binds = self._fn_binds(fn)
        using = ", ".join(self._lower_ident(b) for b in binds)
        parts = [f"EXECUTE IMMEDIATE {sql_code}"]
        if target is not None:
            parts.append(f"INTO {target}")
        if using:
            parts.append(f"USING {using}")
        return [f"{indent}{' '.join(parts)};"]

    def _fn_touches(self, fn: A.FnDef) -> list[str]:
        """Return the list of table names from `@touches(t1, t2, ...)` on a fn.
        Each arg must be a bare Ident (no qualification). Empty list if absent.
        """
        ann = next((a for a in fn.annotations if a.name == "touches"), None)
        if ann is None:
            return []
        names: list[str] = []
        for arg in ann.args:
            if isinstance(arg, A.Ident) and "::" not in arg.name:
                names.append(arg.name.lower())
            else:
                raise EmitError(
                    f"@touches argument must be a bare table identifier, got {type(arg).__name__}",
                    ann.loc,
                )
        return names

    def _fn_binds(self, fn: A.FnDef) -> list[str]:
        """Return the list of `:bind_name` variables for a fn's @binds(...)
        annotation. Order is significant — that's the order pell hands values
        to EXECUTE IMMEDIATE's USING clause. The user is responsible for
        aligning these with the `:name` placeholders in the SQL string.

        Each arg must be a bare identifier. v1 doesn't validate types — pell
        variables with the same names in scope provide the values.
        """
        ann = next((a for a in fn.annotations if a.name == "binds"), None)
        if ann is None:
            return []
        names: list[str] = []
        for arg in ann.args:
            if isinstance(arg, A.Ident) and "::" not in arg.name:
                names.append(arg.name)
            else:
                raise EmitError(
                    f"@binds argument must be a bare identifier, got {type(arg).__name__}",
                    ann.loc,
                )
        return names

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
        retry = self._retry_for(fn)
        if retry is not None:
            if has_finally:
                raise EmitError(
                    f"@retry on fn {fn.name!r}: not yet supported with `finally` blocks",
                    retry["loc"],
                )
            if "autonomous" in {a.name for a in fn.annotations}:
                raise EmitError(
                    f"@retry on fn {fn.name!r}: not yet supported with @autonomous",
                    retry["loc"],
                )
            if "pipelined" in {a.name for a in fn.annotations}:
                raise EmitError(
                    f"@retry on fn {fn.name!r}: not supported on @pipelined fns",
                    retry["loc"],
                )
            self._needs_panic_helper = True
            self._decl("l_pell_attempt PLS_INTEGER := 0;")

        body_stmt_lines: list[str] = []
        # @retry adds an extra two indent levels (LOOP → inner BEGIN); finally
        # adds one (outer BEGIN → inner BEGIN). Otherwise body lives directly
        # under the function's BEGIN.
        if retry is not None:
            body_indent = "        "
        elif has_finally:
            body_indent = "      "
        else:
            body_indent = "    "
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
        if retry is not None:
            sleep_stmt = self._retry_sleep_stmt(retry)
            retry_prov = self._loc_comment(retry["loc"])
            out.append("    LOOP")
            out.append(f"      SAVEPOINT pell_attempt;{retry_prov}")
            out.append("      BEGIN")
            if body_stmt_lines:
                out.extend(body_stmt_lines)
            else:
                out.append("        NULL;")
            out.append("        EXIT;")  # success path — unreachable for FUNCTIONs (RETURN exits first)
            out.append("      EXCEPTION")
            out.append("        WHEN OTHERS THEN")
            out.append("          IF pell_is_panic(SQLCODE) THEN RAISE; END IF;")
            out.append("          l_pell_attempt := l_pell_attempt + 1;")
            out.append(f"          ROLLBACK TO pell_attempt;{retry_prov}")
            out.append(f"          IF l_pell_attempt >= {retry['n']} THEN RAISE; END IF;")
            if sleep_stmt is not None:
                out.append(f"          {sleep_stmt}")
            out.append("      END;")
            out.append("    END LOOP;")
        elif has_finally:
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
        if isinstance(expr, A.PipelineExpr):
            expr = self._reduce_pipeline(expr)
        # `exec_dyn(<sql_string>)` (optionally wrapped in `?`) → EXECUTE
        # IMMEDIATE … INTO target USING … . Only legal inside an unsafe fn.
        inner_for_dyn = expr.inner if isinstance(expr, A.QuestionMark) else expr
        if (isinstance(inner_for_dyn, A.Call)
                and isinstance(inner_for_dyn.callee, A.Ident)
                and inner_for_dyn.callee.name == "exec_dyn"):
            return self._emit_exec_dyn(target, inner_for_dyn, indent)
        if isinstance(expr, A.QuestionMark):
            inner = expr.inner
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
        # `<seq>.nextval` / `<seq>.currval` → NUMBER.
        if (
            isinstance(value, A.MemberAccess)
            and isinstance(value.obj, A.Ident)
            and value.obj.name in self._seq_names
            and value.field in ("nextval", "currval")
        ):
            return "NUMBER"
        return None

    def _infer_call_type(self, call: A.Call) -> Optional[str]:
        # .one() / .first() / .one_or_none() on a sql!{} (possibly wrapped in lock modifiers)
        if isinstance(call.callee, A.MemberAccess):
            method = call.callee.field
            # Method-style aliases that return scalars.
            if method in ("contains", "starts_with", "ends_with", "is_empty"):
                return "BOOLEAN"
            if method in ("year", "month", "day", "hour", "minute", "second"):
                return "NUMBER"
            if method == "split":
                return "t_text_list"
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
        # Match the explicit chain form: sql!{}.one()[.if_empty(X)][.if_many(Y)]
        # (each .if_* is optional, order-independent). When matched, the
        # WHEN handlers raise the user-specified typed errors instead of the
        # prelude / name-matched ones.
        base, if_empty_arg, if_many_arg = self._peel_select_chain(inner_call)
        if base is not None:
            recv, sql = self._strip_lock_modifiers(base.callee.obj)
            if sql is not None:
                return self._emit_select_into(
                    target, sql, indent,
                    expect_exactly_one=True,
                    if_empty_arg=if_empty_arg,
                    if_many_arg=if_many_arg,
                )
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

    def _peel_select_chain(
        self, call: A.Call,
    ) -> tuple[Optional[A.Call], Optional[A.StructLit], Optional[A.StructLit]]:
        """Walk inward through `.if_empty(X)` / `.if_many(Y)` method calls and
        return `(one_call, if_empty_arg, if_many_arg)` if the chain bottoms out
        at a `.one()` call on some receiver. Otherwise `(None, None, None)`.

        Each `.if_*` call may appear at most once; order doesn't matter.
        """
        if_empty: Optional[A.StructLit] = None
        if_many:  Optional[A.StructLit] = None
        cur: A.Expr = call
        # peel at most 2 layers, then expect .one()
        for _ in range(3):
            if not (isinstance(cur, A.Call) and isinstance(cur.callee, A.MemberAccess)):
                return (None, None, None)
            method = cur.callee.field
            if method == "if_empty" and if_empty is None and len(cur.args) == 1:
                if not isinstance(cur.args[0], A.StructLit):
                    return (None, None, None)
                if_empty = cur.args[0]
                cur = cur.callee.obj
            elif method == "if_many" and if_many is None and len(cur.args) == 1:
                if not isinstance(cur.args[0], A.StructLit):
                    return (None, None, None)
                if_many = cur.args[0]
                cur = cur.callee.obj
            elif method == "one" and not cur.args:
                # Found base .one() — return it (caller pulls receiver SQL out).
                return (cur, if_empty, if_many)
            else:
                return (None, None, None)
        return (None, None, None)

    def _emit_dyn_pivot_return(self, call: A.Call, indent: str) -> list[str]:
        """Lower `return pivot::sum_dyn(source=…, rows=…, col=…, value=…)`.

        Two-step at runtime:
          1. SELECT LISTAGG to compute the dynamic `IN (...)` column list.
          2. Build the PIVOT SQL via string concat, then OPEN a SYS_REFCURSOR
             via EXECUTE IMMEDIATE.

        Locals (`l_pell_pivot_cols`, `l_pell_pivot_sql`, `l_pell_pivot_cur`)
        are added to the fn's declares. Only legal inside `unsafe fn`.
        """
        fn = self._current_fn
        if fn is None or not getattr(fn, "is_unsafe", False):
            raise EmitError(
                "pivot::sum_dyn(...) requires the enclosing fn to be `unsafe fn` "
                "— dynamic SQL is the underlying mechanism",
                call.loc,
            )
        kw = call.kwargs
        for key in ("source", "rows", "col", "value"):
            if key not in kw:
                raise EmitError(
                    f"pivot::sum_dyn: missing required kwarg `{key}=`",
                    call.loc,
                )
        source = kw["source"]
        if not isinstance(source, A.SqlBlock):
            raise EmitError(
                "pivot::sum_dyn: `source=` must be a sql!{ … } block", call.loc,
            )
        rows  = self._pivot_col_ref("rows",  kw["rows"],  call.loc)
        col   = self._pivot_col_ref("col",   kw["col"],   call.loc)
        value = self._pivot_col_ref("value", kw["value"], call.loc)
        source_sql = source.sql.strip().rstrip(";")

        # Register locals on the enclosing fn's declares.
        self._decl("l_pell_pivot_cols VARCHAR2(4000);")
        self._decl("l_pell_pivot_sql  VARCHAR2(32767);")
        self._decl("l_pell_pivot_cur  SYS_REFCURSOR;")

        # The source SQL goes verbatim — but we need it as a quoted literal
        # for the dynamic SQL string. Escape any single quotes.
        source_quoted = source_sql.replace("'", "''")
        return [
            # Step 1: discover columns.
            f"{indent}SELECT LISTAGG('''' || {col} || ''' AS \"' || {col} || '\"', ', ')",
            f"{indent}         WITHIN GROUP (ORDER BY {col})",
            f"{indent}  INTO l_pell_pivot_cols",
            f"{indent}  FROM (SELECT DISTINCT {col} FROM (",
            f"{indent}{source_sql}",
            f"{indent}  )) WHERE {col} IS NOT NULL;",
            # Step 2: construct the PIVOT SQL string. We embed the source SQL
            # inline (single-quoted) so the resulting text is a single
            # standalone SELECT … FROM (…) PIVOT (… IN (cols)) statement.
            f"{indent}l_pell_pivot_sql :=",
            f"{indent}  'SELECT * FROM (' ||",
            f"{indent}  '{source_quoted}' ||",
            f"{indent}  ') PIVOT (SUM({value}) FOR {col} IN (' ||",
            f"{indent}  l_pell_pivot_cols ||",
            f"{indent}  '))';",
            # Step 3: open + return.
            f"{indent}OPEN l_pell_pivot_cur FOR l_pell_pivot_sql;",
            f"{indent}RETURN l_pell_pivot_cur;",
        ]

    def _lower_typed_pivot(self, call: A.Call) -> A.SqlBlock:
        """Lower `pivot::sum(source=sql!{...}, rows=col, col=col, over=Enum, value=col)`
        to a SqlBlock containing Oracle's PIVOT clause with the enum's
        variants as the static `FOR <col> IN (...)` list. Result composes
        with .collect() / .one() chains via _strip_lock_modifiers.
        """
        kw = call.kwargs
        required = ("source", "rows", "col", "over", "value")
        for key in required:
            if key not in kw:
                raise EmitError(
                    f"pivot::sum: missing required kwarg `{key}=` "
                    f"(need: {', '.join(required)})",
                    call.loc,
                )
        source = kw["source"]
        if not isinstance(source, A.SqlBlock):
            raise EmitError(
                "pivot::sum: `source=` must be a sql!{ … } block", call.loc,
            )
        rows  = self._pivot_col_ref("rows",  kw["rows"],  call.loc)
        col   = self._pivot_col_ref("col",   kw["col"],   call.loc)
        value = self._pivot_col_ref("value", kw["value"], call.loc)
        over = kw["over"]
        if not isinstance(over, A.Ident) or "::" in over.name:
            raise EmitError(
                "pivot::sum: `over=` must reference a pell enum by name",
                call.loc,
            )
        enum_def = next((e for e in self._enums if e.name == over.name), None)
        if enum_def is None:
            raise EmitError(
                f"pivot::sum: enum {over.name!r} is not declared in this module "
                "(typed pivot needs a `pub enum` to enumerate the columns)",
                call.loc,
            )
        cols_in = ",\n      ".join(
            f"{_sql_string(v.value or v.name)} AS \"{v.name}\""
            for v in enum_def.variants
        )
        pivot_sql = (
            f"SELECT * FROM (\n"
            f"  {source.sql.strip()}\n"
            f") PIVOT (\n"
            f"  SUM({value}) FOR {col} IN (\n      {cols_in}\n  )\n"
            f")"
        )
        return A.SqlBlock(
            loc=call.loc,
            sql=pivot_sql,
            binds=source.binds,
            is_dml=False,
            has_returning=False,
        )

    def _pivot_col_ref(self, role: str, expr: A.Expr, loc: A.Loc) -> str:
        """Validate that a pivot kwarg is a bare-identifier column reference
        and return its SQL spelling (lowercased)."""
        if isinstance(expr, A.Ident) and "::" not in expr.name:
            return expr.name.lower()
        raise EmitError(
            f"pivot::sum: `{role}=` must be a bare column identifier",
            loc,
        )

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
        # `pivot::sum(...)` synthesizes a SqlBlock at lowering time. Once
        # rewritten, downstream `.collect()` / `.one()` chains pick it up
        # the same way they would a literal `sql!{}`.
        if isinstance(cur, A.Call) and isinstance(cur.callee, A.Ident) and cur.callee.name == "pivot::sum":
            cur = self._lower_typed_pivot(cur)
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

    def _emit_select_into(
        self,
        target: str,
        sql: A.SqlBlock,
        indent: str,
        expect_exactly_one: bool,
        *,
        if_empty_arg: Optional[A.StructLit] = None,
        if_many_arg: Optional[A.StructLit] = None,
    ) -> list[str]:
        """Lower a single-row SELECT INTO.

        PL/SQL requires INTO between the SELECT list and FROM, so we splice
        it in at that boundary.

        Error-handler dispatch order:
          1. `if_empty_arg` / `if_many_arg` (explicit chain) take precedence.
          2. Else, NO_DATA_FOUND maps to the fn's NotFound-named variant if
             present (legacy name-matching).
          3. Else, NO_DATA_FOUND and TOO_MANY_ROWS re-raise as-is.
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
        # NO_DATA_FOUND handler
        if if_empty_arg is not None:
            exn = self._exception_name(if_empty_arg.type_name)
            payload = self._struct_lit_to_json(if_empty_arg)
            nf_handler = (
                f"{indent}  WHEN NO_DATA_FOUND THEN\n"
                f"{indent}    pell_runtime.set_err('{exn}:1', {payload});\n"
                f"{indent}    RAISE pell_runtime.{exn};"
            )
        else:
            nf_variant = self._notfound_variant_for_current_fn()
            if nf_variant is not None:
                exn = self._exception_name(nf_variant)
                nf_handler = (
                    f"{indent}  WHEN NO_DATA_FOUND THEN\n"
                    f"{indent}    pell_runtime.set_err('{exn}:1', '{{}}');\n"
                    f"{indent}    RAISE pell_runtime.{exn};"
                )
            else:
                nf_handler = f"{indent}  WHEN NO_DATA_FOUND THEN RAISE;"
        # TOO_MANY_ROWS handler
        if if_many_arg is not None:
            exn = self._exception_name(if_many_arg.type_name)
            payload = self._struct_lit_to_json(if_many_arg)
            tm_handler = (
                f"{indent}  WHEN TOO_MANY_ROWS THEN\n"
                f"{indent}    pell_runtime.set_err('{exn}:1', {payload});\n"
                f"{indent}    RAISE pell_runtime.{exn};"
            )
        else:
            tm_handler = f"{indent}  WHEN TOO_MANY_ROWS THEN RAISE;"
        return [
            f"{indent}BEGIN",
            f"{indent}  {spliced.strip()};",
            f"{indent}EXCEPTION",
            nf_handler,
            tm_handler,
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
        # Inside an aggregate's `finish()`, return must assign to the OUT
        # parameter `returnValue` and yield ODCIConst.Success — the function
        # signature is dictated by Oracle's ODCIAggregate contract, not the
        # pell `-> T` declaration.
        if self._in_aggregate_terminate and s.value is not None:
            val = self._emit_expr(s.value)
            return [
                f"{indent}returnValue := {val};",
                f"{indent}RETURN ODCIConst.Success;",
            ]
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
        # `return pivot::sum_dyn(...)` — dynamic pivot, opens a SYS_REFCURSOR.
        if (isinstance(s.value, A.Call)
                and isinstance(s.value.callee, A.Ident)
                and s.value.callee.name == "pivot::sum_dyn"):
            return prefix + self._emit_dyn_pivot_return(s.value, indent)
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
        """Lower `return Err(<variant>)` to: set SYS_CONTEXT payload + RAISE.

        Payload is encoded as a JSON object so the catch side can read
        individual fields back via JSON_VALUE without string parsing.
        """
        if isinstance(payload_expr, A.StructLit):
            err_name = payload_expr.type_name
            exn = self._exception_name(err_name)
            payload = self._struct_lit_to_json(payload_expr)
            return [
                f"{indent}pell_runtime.set_err('{exn}:1', {payload});",
                f"{indent}RAISE pell_runtime.{exn};",
            ]
        if isinstance(payload_expr, A.Ident):
            # zero-payload error variant
            exn = self._exception_name(payload_expr.name)
            return [f"{indent}RAISE pell_runtime.{exn};"]
        return [f"{indent}-- TODO: Err({type(payload_expr).__name__})"]

    def _struct_lit_to_json(self, sl: A.StructLit) -> str:
        """Render a pell struct literal as a PL/SQL JSON_OBJECT(...) call.

        Each field becomes `'name' VALUE <emitted-expr>`. Zero-field structs
        render as `'{}'` (literal JSON empty object).
        """
        if not sl.fields:
            return "'{}'"
        args = ", ".join(
            f"'{f.name}' VALUE {self._emit_expr(f.value)}"
            for f in sl.fields
        )
        return f"JSON_OBJECT({args})"

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

        Special-case: when every arm pattern is a `case` of the same sealed type,
        lower to a CASE expression that dispatches via `IS OF (<case_t>)`.
        For arms that bind a value (`Circle(c) =>` / `Circle { radius } =>`), we
        introduce a local typed as the case child and assign via TREAT().
        """
        scrut_code = self._emit_expr(s.scrutinee)
        if self._is_sealed_match(s):
            return self._emit_sealed_match(s, scrut_code, indent)
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

    def _is_sealed_match(self, s: A.MatchStmt) -> bool:
        """True if every arm pattern names a sealed-type case."""
        if not s.arms:
            return False
        case_names = {c.name for st in self._sealed_types for c in st.cases}
        for a in s.arms:
            p = a.pattern
            if isinstance(p, A.VariantPattern) and p.name in case_names:
                continue
            if isinstance(p, A.WildcardPattern):
                continue
            return False
        return True

    def _emit_sealed_match(self, s: A.MatchStmt, scrut_code: str, indent: str) -> list[str]:
        """Lower a `match shape { Circle(c) => ..., Rectangle(r) => ... }` to:

            IF (shape) IS OF (t_circle) THEN
              DECLARE c t_circle := TREAT((shape) AS t_circle);
              BEGIN <body> END;
            ELSIF (shape) IS OF (t_rectangle) THEN
              ...
            END IF;
        """
        out: list[str] = []
        case_names = {c.name: (st, c) for st in self._sealed_types for c in st.cases}
        for i, arm in enumerate(s.arms):
            pat = arm.pattern
            if isinstance(pat, A.WildcardPattern):
                kw = "ELSE" if i > 0 else "IF TRUE THEN"
                out.append(f"{indent}{kw}")
                self._emit_arm_body(arm.body, out, indent + "  ")
                continue
            assert isinstance(pat, A.VariantPattern)
            st_case = case_names[pat.name]
            _, case = st_case
            case_pl = _record_type_name(case.name)
            kw = "IF" if i == 0 else "ELSIF"
            out.append(f"{indent}{kw} ({scrut_code}) IS OF ({case_pl}) THEN")
            # Bind value: VariantPattern.args (positional) gives bind name(s);
            # VariantPattern.fields (struct-form) lets us project fields.
            bind_name: Optional[str] = None
            if pat.args and isinstance(pat.args[0], A.BindingPattern):
                bind_name = pat.args[0].name
            # Struct-form pattern (`Circle { radius }`) — bind each named field as a local.
            field_binds: list[tuple[str, str]] = []  # (pell field name, local var name)
            if pat.fields:
                for fp in pat.fields:
                    field_binds.append((fp.name, local_name(fp.name)))
            inner_indent = indent + "  "
            if bind_name is not None or field_binds:
                out.append(f"{inner_indent}DECLARE")
                if bind_name is not None:
                    out.append(f"{inner_indent}  {local_name(bind_name)} {case_pl};")
                for pell_name, pl_name in field_binds:
                    # Field types come from the case (or its parent's fields).
                    ftype = self._case_field_type(case, pell_name)
                    if ftype is None:
                        raise EmitError(
                            f"case {case.name} has no field {pell_name!r}",
                            arm.loc,
                        )
                    out.append(f"{inner_indent}  {pl_name} {ftype};")
                out.append(f"{inner_indent}BEGIN")
                if bind_name is not None:
                    out.append(f"{inner_indent}  {local_name(bind_name)} := TREAT(({scrut_code}) AS {case_pl});")
                for pell_name, pl_name in field_binds:
                    out.append(
                        f"{inner_indent}  {pl_name} := TREAT(({scrut_code}) AS {case_pl}).{pell_name.lower()};"
                    )
                # Inside the bound block, register the binding so identifier lookup works.
                save_params = self._params
                self._params = set(self._params) | ({bind_name} if bind_name else set()) | {n for n, _ in field_binds}
                try:
                    self._emit_arm_body(arm.body, out, inner_indent + "  ")
                finally:
                    self._params = save_params
                out.append(f"{inner_indent}END;")
            else:
                self._emit_arm_body(arm.body, out, inner_indent)
        out.append(f"{indent}END IF;")
        return out

    def _emit_arm_body(self, body, out: list[str], indent: str) -> None:
        if isinstance(body, list):
            for stmt in body:
                out.extend(self._emit_stmt(stmt, indent))
        else:
            # Expression-form arm: emit as a NULL statement holding the value.
            # Match-as-expression isn't lowered yet; for now we just evaluate.
            out.append(f"{indent}NULL;  -- expr arm value: {self._emit_expr(body)}")

    def _case_field_type(self, case: A.CaseDef, name: str) -> Optional[str]:
        """Return the PL/SQL type name for the named field in a case, walking
        the parent's fields too."""
        for f in case.fields:
            if f.name == name:
                return self._lt(f.type_ref, sql_context=True)
        # Walk parent fields too
        for st in self._sealed_types:
            if case in st.cases:
                for f in st.fields:
                    if f.name == name:
                        return self._lt(f.type_ref, sql_context=True)
        return None

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
        prov = self._loc_comment(s.loc)
        out = [
            f"{indent}DECLARE",
            f"{indent}  {committed_flag} BOOLEAN := FALSE;",
            f"{indent}BEGIN",
            f"{indent}  SAVEPOINT {sp};{prov}",
        ]
        self._tx_stack.append(committed_flag)
        for stmt in s.body:
            out.extend(self._emit_stmt(stmt, indent + "  "))
        self._tx_stack.pop()
        body_ends_in_return = bool(s.body) and isinstance(s.body[-1], A.ReturnStmt)
        if not body_ends_in_return:
            out.append(f"{indent}  COMMIT;")
            out.append(f"{indent}  {committed_flag} := TRUE;")
        out += [
            f"{indent}EXCEPTION",
            f"{indent}  WHEN OTHERS THEN",
            f"{indent}    IF NOT {committed_flag} THEN ROLLBACK TO {sp};{prov} END IF;",
            f"{indent}    RAISE;",
            f"{indent}END;",
        ]
        return out

    def _loc_comment(self, loc: Optional[A.Loc]) -> str:
        """Inline trailing PL/SQL comment pointing at the pell source line that
        produced this emission. Used at SAVEPOINT / ROLLBACK / pell_finally_body
        sites so a DBA reading the generated PL/SQL can `grep -n` the pell
        file. Returns the empty string when loc is unavailable."""
        if loc is None or not loc.file:
            return ""
        import os
        # Strip absolute path noise — show just the basename so the comment
        # stays readable even on long paths. Users can grep -r if needed.
        base = os.path.basename(loc.file)
        return f"  -- @ {base}:{loc.line}"

    def _emit_expr_stmt(self, s: A.ExprStmt, indent: str) -> list[str]:
        e = s.expr
        # sql!{} write as a bare statement
        if isinstance(e, A.SqlBlock):
            sql_text = self._rewrite_binds(e.sql).strip().rstrip(";")
            lines = [f"{indent}{line}" for line in (sql_text + ";").splitlines()]
            return lines
        # `exec_dyn(<sql>);` as a bare statement → EXECUTE IMMEDIATE with no
        # INTO clause (DML or PL/SQL block).
        if (isinstance(e, A.Call)
                and isinstance(e.callee, A.Ident)
                and e.callee.name == "exec_dyn"):
            return self._emit_exec_dyn(None, e, indent)
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
            # A StructLit on a known `type` lowers to its OBJECT constructor.
            if e.type_name in self._type_names:
                return self._emit_obj_constructor(e)
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
            # List-typed OBJECT attribute: `self.vals.len()` → `SELF.vals.COUNT`
            if self._is_list_member_access(recv):
                list_ref = self._emit_expr(recv)
                if method == "len" and not e.args:
                    return f"{list_ref}.COUNT"
                if method == "first" and not e.args:
                    return f"{list_ref}.FIRST"
                if method == "last" and not e.args:
                    return f"{list_ref}.LAST"
                if method == "at" and len(e.args) == 1:
                    return f"{list_ref}({self._emit_expr(e.args[0])})"
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
            # Object dispatch — when the receiver is an OBJECT instance, Oracle
            # uses `recv.method(args)` (not free-function style).
            if self._receiver_is_object_typed(recv):
                args_code = [self._emit_expr(a) for a in e.args]
                return f"{self._emit_expr(recv)}.{method.lower()}({', '.join(args_code)})"
            # Method-style aliases: .contains, .starts_with, .year, .add_days, …
            if method in _METHOD_ALIASES:
                arity, template = _METHOD_ALIASES[method]
                if len(e.args) != arity:
                    raise EmitError(
                        f"method .{method}() takes {arity} argument(s); got {len(e.args)}",
                        e.loc,
                    )
                placeholders: dict[str, str] = {"recv": self._emit_expr(recv)}
                for i, a in enumerate(e.args):
                    placeholders[f"arg{i}"] = self._emit_expr(a)
                return template.format(**placeholders)
            # .split(delim) — emits a per-package helper that returns list<text>.
            if method == "split" and len(e.args) == 1:
                self._needs_split_helper = True
                # Register `t_text_list` so the package body declares it.
                if "t_text_list" not in self._list_types_emitted:
                    self._list_type_decls.append(
                        "  TYPE t_text_list IS TABLE OF VARCHAR2(4000) INDEX BY PLS_INTEGER;"
                    )
                    self._list_types_emitted.add("t_text_list")
                recv_code = self._emit_expr(recv)
                delim_code = self._emit_expr(e.args[0])
                return f"pell_split_text({recv_code}, {delim_code})"
            # generic method call → free-function style with receiver as first arg
            args_code = [self._emit_expr(recv)] + [self._emit_expr(a) for a in e.args]
            return f"{method}({', '.join(args_code)})"
        # plain function call — if the callee is a bare Ident that isn't a
        # known pell binding, treat it as an Oracle/PL/SQL builtin name
        # (length, substr, bitxor, ora_hash, …) and emit it verbatim rather
        # than prefixing with `l_`.
        if isinstance(e.callee, A.Ident) and "::" not in e.callee.name:
            name = e.callee.name
            if (
                name not in self._fn_names
                and name not in self._params
                and not any(name in scope for scope in self._loop_vars)
                and name not in self._list_locals
                and name not in self._local_types
            ):
                args_code = [self._emit_expr(a) for a in e.args]
                return f"{name.lower()}({', '.join(args_code)})"
        callee = self._emit_expr(e.callee)
        args_code = [self._emit_expr(a) for a in e.args]
        return f"{callee}({', '.join(args_code)})"

    # ---- §5.2 / §5.3 — types, sealed hierarchies, aggregates -----------

    def _emit_user_type(self, td: A.TypeDef) -> None:
        """Lower `pub type T { ... }` to schema-level CREATE TYPE + CREATE TYPE BODY."""
        type_name = _record_type_name(td.name)
        # Spec: attributes + member function signatures
        attr_lines = [
            f"  {f.name.lower()} {self._lt(f.type_ref, sql_context=True)}"
            for f in td.fields
        ]
        method_sigs = [self._method_signature(m, type_name) for m in td.methods]
        spec_lines = attr_lines + method_sigs
        spec = (
            f"CREATE OR REPLACE TYPE {self._q(type_name)} AS OBJECT (\n"
            + ",\n".join(spec_lines)
            + "\n);\n/"
        )
        self._schema_types.append(spec)
        # Body: concrete method implementations
        if any(not m.is_abstract for m in td.methods):
            body_chunks = [
                self._method_body(m, td.name, type_name)
                for m in td.methods
                if not m.is_abstract
            ]
            body = (
                f"CREATE OR REPLACE TYPE BODY {self._q(type_name)} AS\n"
                + "\n\n".join(body_chunks)
                + f"\nEND;\n/"
            )
            self._schema_types.append(body)

    def _emit_sealed_type(self, st: A.SealedTypeDef) -> None:
        """Lower `pub sealed type T { ... case C ... }` to NOT FINAL parent + UNDER children."""
        parent_name = _record_type_name(st.name)
        attr_lines = [
            f"  {f.name.lower()} {self._lt(f.type_ref, sql_context=True)}"
            for f in st.fields
        ]
        # Build parent method signatures; abstract methods get a NOT INSTANTIABLE prefix.
        parent_method_sigs: list[str] = []
        any_abstract = False
        for m in st.methods:
            sig = self._method_signature(m, parent_name)
            if m.is_abstract:
                # Oracle puts the NOT INSTANTIABLE modifier *before* MEMBER FUNCTION.
                # We insert it into the spec line at that position.
                sig = sig.replace("  MEMBER", "  NOT INSTANTIABLE MEMBER", 1)
                any_abstract = True
            parent_method_sigs.append(sig)
        spec_lines = attr_lines + parent_method_sigs
        if not attr_lines:
            # Oracle (PLS-00589) requires at least one attribute in an OBJECT type.
            # When the sealed parent has no fields, emit a hidden placeholder.
            spec_lines = ["  sys_tag_ NUMBER"] + parent_method_sigs
        suffix = "NOT FINAL"
        if any_abstract:
            suffix = "NOT INSTANTIABLE " + suffix
        parent_spec = (
            f"CREATE OR REPLACE TYPE {self._q(parent_name)} AS OBJECT (\n"
            + ",\n".join(spec_lines)
            + f"\n) {suffix};\n/"
        )
        self._schema_types.append(parent_spec)
        # Parent body — concrete (non-abstract) methods.
        concrete_parent_methods = [m for m in st.methods if not m.is_abstract]
        if concrete_parent_methods:
            body_chunks = [
                self._method_body(m, st.name, parent_name)
                for m in concrete_parent_methods
            ]
            parent_body = (
                f"CREATE OR REPLACE TYPE BODY {self._q(parent_name)} AS\n"
                + "\n\n".join(body_chunks)
                + f"\nEND;\n/"
            )
            self._schema_types.append(parent_body)
        # Each case: CREATE TYPE child UNDER parent (with case-specific fields and overrides)
        for case in st.cases:
            case_name = _record_type_name(case.name)
            case_attr_lines = [
                f"  {f.name.lower()} {self._lt(f.type_ref, sql_context=True)}"
                for f in case.fields
            ]
            case_method_sigs: list[str] = []
            for m in case.methods:
                # If the parent had a method by this name, mark this as OVERRIDING.
                is_override = any(pm.name == m.name for pm in st.methods)
                sig = self._method_signature(m, case_name, overriding=is_override)
                case_method_sigs.append(sig)
            case_spec_lines = case_attr_lines + case_method_sigs
            if not case_spec_lines:
                # An empty case still needs at least one declaration — emit a dummy
                # zero-byte attribute (Oracle requires at least one attribute in a
                # subtype that adds nothing). We use a single PLS-compatible value.
                case_spec_lines = ["  dummy_ NUMBER"]
            case_spec = (
                f"CREATE OR REPLACE TYPE {self._q(case_name)} UNDER {parent_name} (\n"
                + ",\n".join(case_spec_lines)
                + "\n);\n/"
            )
            self._schema_types.append(case_spec)
            # Case body
            if case.methods:
                case_body_chunks = [
                    self._method_body(m, case.name, case_name,
                                      is_override=any(pm.name == m.name for pm in st.methods))
                    for m in case.methods
                ]
                case_body = (
                    f"CREATE OR REPLACE TYPE BODY {self._q(case_name)} AS\n"
                    + "\n\n".join(case_body_chunks)
                    + f"\nEND;\n/"
                )
                self._schema_types.append(case_body)

    def _emit_aggregate(self, ag: A.AggregateDef) -> None:
        """Lower an aggregate to ODCIAggregate object type + CREATE FUNCTION AGGREGATE USING."""
        ann_names = {a.name for a in ag.annotations}
        is_parallel = "parallel" in ann_names
        if is_parallel and ag.merge_body is None:
            raise EmitError(
                f"aggregate {ag.name!r}: @parallel requires a `merge` block — "
                "Oracle cannot split iteration without a merge function",
                ag.loc,
            )
        type_name = f"{ag.name.lower()}_agg_t"
        if ag.return_type is None:
            raise EmitError(f"aggregate {ag.name!r}: missing `-> T` return type", ag.loc)
        ret_sql = self._lt(ag.return_type)
        ret_sql_param = self._lt(ag.return_type, param=True)
        # Oracle's ODCIAggregate interface takes a single iterate input — even
        # in 23ai, true multi-arg ODCI aggregates aren't supported (the wrapper
        # function fails ORA-29925, and SQL_MACRO wrapping produces ORA-00600).
        # For multi-arg aggregates we auto-generate an OBJECT tuple type, take
        # that tuple as iterate's single input, and unpack it back to the
        # user's step parameter names at the top of the iterate body.
        if not ag.step_params:
            raise EmitError(
                f"aggregate {ag.name!r}: step must take at least one parameter",
                ag.loc,
            )
        if len(ag.step_params) != len(ag.params):
            raise EmitError(
                f"aggregate {ag.name!r}: step has {len(ag.step_params)} parameter(s) "
                f"but the aggregate signature has {len(ag.params)} — they must match",
                ag.loc,
            )
        is_multi_arg = len(ag.step_params) > 1
        if is_multi_arg:
            tuple_type_name = f"{ag.name.lower()}_args_t"
            # Emit the tuple OBJECT type — one attribute per step param, in order.
            tuple_attrs = ",\n".join(
                f"  {p.name.lower()} {self._lt(p.type_ref, sql_context=True)}"
                for p in ag.step_params
            )
            self._schema_types.append(
                f"CREATE OR REPLACE TYPE {self._q(tuple_type_name)} AS OBJECT (\n{tuple_attrs}\n);\n/"
            )
            # The iterate signature takes the tuple as its single input.
            tuple_param_name = "p_args"
            step_params_pl = f"{tuple_param_name} IN {tuple_type_name}"
        else:
            step_params_pl = ", ".join(
                f"{param_name(p.name)} IN {self._lt(p.type_ref, param=True)}"
                for p in ag.step_params
            )
        # Pre-register any list types referenced by state fields so we can reference
        # them as schema-level types. v1: only `list<primitive>` is supported, which
        # we lower to a nested table at the schema level (one TYPE per element type).
        state_attr_lines: list[str] = []
        for f in ag.state_fields:
            attr_type = self._aggregate_state_attr_type(f.type_ref, ag.loc)
            state_attr_lines.append(f"  {f.name.lower()} {attr_type}")
        # Oracle requires ALL four ODCI routines on the type even when
        # the aggregate is single-threaded (ORA-29925 otherwise). When the
        # user didn't supply `merge`, we still declare ODCIAggregateMerge and
        # give it a body that raises — that way the aggregate works serially
        # but a future @parallel-enable can't accidentally produce wrong
        # results.
        spec_lines = state_attr_lines + [
            f"  STATIC FUNCTION ODCIAggregateInitialize(sctx IN OUT {type_name}) RETURN NUMBER",
            f"  MEMBER FUNCTION ODCIAggregateIterate(self IN OUT {type_name}, {step_params_pl}) RETURN NUMBER",
            f"  MEMBER FUNCTION ODCIAggregateMerge(self IN OUT {type_name}, ctx2 IN {type_name}) RETURN NUMBER",
            f"  MEMBER FUNCTION ODCIAggregateTerminate(self IN OUT {type_name}, returnValue OUT {ret_sql_param}, flags IN NUMBER) RETURN NUMBER",
        ]
        spec = (
            f"CREATE OR REPLACE TYPE {self._q(type_name)} AS OBJECT (\n"
            + ",\n".join(spec_lines)
            + "\n);\n/"
        )
        self._schema_types.append(spec)
        # Body
        # Initialize: build sctx from state defaults (type-aware: list<T> default
        # `= []` becomes the empty nested-table constructor t_<T>_nt()).
        init_args = ", ".join(
            self._emit_state_default_typed(f.type_ref, e)
            for f, e in zip(ag.state_fields, ag.state_defaults)
        )
        init_body = [
            f"  STATIC FUNCTION ODCIAggregateInitialize(sctx IN OUT {type_name}) RETURN NUMBER IS",
            f"  BEGIN",
            f"    sctx := {type_name}({init_args});",
            f"    RETURN ODCIConst.Success;",
            f"  END;",
        ]
        iterate_inner, iterate_decls = self._emit_aggregate_block(ag.step_body, ag.step_params, ag, indent="    ")
        # Multi-arg: prepend "p_<name> := p_args.<name>;" assigns so the body
        # references its declared step parameter names naturally.
        unpack_lines: list[str] = []
        unpack_decls: list[str] = []
        if is_multi_arg:
            for p in ag.step_params:
                # Local declarations need sized types (param=False).
                unpack_decls.append(f"{param_name(p.name)} {self._lt(p.type_ref)};")
                unpack_lines.append(f"    {param_name(p.name)} := p_args.{p.name.lower()};")
        iterate_body = [
            f"  MEMBER FUNCTION ODCIAggregateIterate(self IN OUT {type_name}, {step_params_pl}) RETURN NUMBER IS",
        ] + [f"    {d}" for d in unpack_decls + iterate_decls] + [
            f"  BEGIN",
        ] + unpack_lines + iterate_inner + [
            f"    RETURN ODCIConst.Success;",
            f"  END;",
        ]
        chunks = ["\n".join(init_body), "\n".join(iterate_body)]
        if ag.merge_body is not None:
            other_pl_name = param_name(ag.merge_other_name)
            merge_inner, merge_decls = self._emit_aggregate_block_merge(
                ag.merge_body, ag, ag.merge_other_name, other_pl_name, indent="    "
            )
            merge_body_lines = [
                f"  MEMBER FUNCTION ODCIAggregateMerge(self IN OUT {type_name}, ctx2 IN {type_name}) RETURN NUMBER IS",
                f"    {other_pl_name} {type_name} := ctx2;",
            ] + [f"    {d}" for d in merge_decls] + [
                f"  BEGIN",
            ] + merge_inner + [
                f"    RETURN ODCIConst.Success;",
                f"  END;",
            ]
            chunks.append("\n".join(merge_body_lines))
        else:
            # No user-supplied merge — emit a stub that raises so an accidental
            # parallel execution surfaces a clear error instead of producing
            # silently-wrong results. (Single-threaded execution never invokes
            # this routine; ORA-29925 forces it to exist in the spec.)
            merge_stub = [
                f"  MEMBER FUNCTION ODCIAggregateMerge(self IN OUT {type_name}, ctx2 IN {type_name}) RETURN NUMBER IS",
                f"  BEGIN",
                f"    RAISE_APPLICATION_ERROR(-20100,",
                f"      'pell aggregate {ag.name} has no merge block; cannot run in parallel');",
                f"    RETURN ODCIConst.Error;",
                f"  END;",
            ]
            chunks.append("\n".join(merge_stub))
        terminate_inner, terminate_decls = self._emit_aggregate_terminate(ag, indent="    ")
        # Only emit a trailing RETURN ODCIConst.Success if the body doesn't
        # already end with one (avoids a dead statement that Oracle's compiler
        # would flag if PL/SCOPE is on).
        trailing_return: list[str] = []
        if not (terminate_inner and terminate_inner[-1].strip().startswith("RETURN ODCIConst")):
            trailing_return = [f"    RETURN ODCIConst.Success;"]
        terminate_body = [
            f"  MEMBER FUNCTION ODCIAggregateTerminate(self IN OUT {type_name}, returnValue OUT {ret_sql_param}, flags IN NUMBER) RETURN NUMBER IS",
        ] + [f"    {d}" for d in terminate_decls] + [
            f"  BEGIN",
        ] + terminate_inner + trailing_return + [
            f"  END;",
        ]
        chunks.append("\n".join(terminate_body))
        body = (
            f"CREATE OR REPLACE TYPE BODY {self._q(type_name)} AS\n"
            + "\n\n".join(chunks)
            + f"\nEND;\n/"
        )
        self._schema_types.append(body)
        # The CREATE FUNCTION user-facing wrapper. For multi-arg aggregates,
        # the signature takes the auto-generated tuple type (single param);
        # callers must construct the tuple at call sites:
        #   SELECT argmax(argmax_args_t(name, salary)) FROM employees;
        if is_multi_arg:
            outer_params = f"p_args IN {tuple_type_name}"
        else:
            outer_params = ", ".join(
                f"{param_name(p.name)} IN {self._lt(p.type_ref, param=True)}"
                for p in ag.params
            )
        parallel_clause = " PARALLEL_ENABLE" if is_parallel else ""
        fn_decl = (
            f"CREATE OR REPLACE FUNCTION {self._q(ag.name.lower())}({outer_params}) "
            f"RETURN {ret_sql_param}{parallel_clause} AGGREGATE USING {type_name};\n/"
        )
        self._schema_types.append(fn_decl)

    def _aggregate_state_attr_type(self, t: A.TypeRef, loc: A.Loc) -> str:
        """For a `state { foo: T = default; }` field, return the SQL type expression
        usable as an OBJECT attribute. `list<P>` becomes a schema-level nested table type."""
        if isinstance(t, A.GenericType) and t.base == "list" and t.params:
            elem_t = t.params[0]
            elem_sql = self._lt(elem_t, sql_context=True)
            nt_name = f"t_{_safe(_render_type(elem_t))}_nt"
            key = f"NT:{nt_name}"
            if key not in self._obj_emitted:
                self._schema_types.insert(0,
                    f"CREATE OR REPLACE TYPE {self._q(nt_name)} AS TABLE OF {elem_sql};\n/"
                )
                self._obj_emitted.add(key)
            return nt_name
        if isinstance(t, A.PrimType):
            return self._lt(t, sql_context=True)
        if isinstance(t, A.NamedType):
            return _record_type_name(t.name)
        raise EmitError(
            f"aggregate state field has unsupported type: {type(t).__name__}",
            loc,
        )

    def _emit_state_default_typed(self, t: A.TypeRef, e: A.Expr) -> str:
        """Render an aggregate-state initial value, knowing the declared type.

        - `list<T> = []`           → `t_<T>_nt()` (empty nested-table ctor)
        - `list<T> = [v1, v2]`     → `t_<T>_nt(v1, v2)` (populated nested-table)
        - any non-list type        → emit the expression directly
        """
        if isinstance(t, A.GenericType) and t.base == "list" and t.params:
            elem_t = t.params[0]
            nt_name = f"t_{_safe(_render_type(elem_t))}_nt"
            if isinstance(e, A.ListLit):
                if not e.elements:
                    return f"{nt_name}()"
                args = ", ".join(self._emit_expr(el) for el in e.elements)
                return f"{nt_name}({args})"
        return self._emit_expr(e)

    def _emit_aggregate_block(
        self, body: list[A.Stmt], step_params: list[A.Param], ag: A.AggregateDef, indent: str
    ) -> tuple[list[str], list[str]]:
        """Emit the step body. Returns (body_lines, declares)."""
        save_params = self._params
        save_in_method = self._in_method_type
        save_declares = self._declares
        save_decl_seen = self._decl_seen
        save_fn = self._current_fn
        self._params = {p.name for p in step_params}
        # Register a synthetic current_fn so type-aware features (parameter type
        # lookups, etc.) work inside the iterate body.
        synth = A.FnDef(loc=ag.loc, name=ag.name, params=step_params, return_type=None, body=body)
        self._in_method_type = ag.name  # so `self` lowers to SELF
        self._declares = []
        self._decl_seen = set()
        self._current_fn = synth
        try:
            lines: list[str] = []
            for s in body:
                lines.extend(self._emit_aggregate_stmt(s, indent))
            return lines, list(self._declares)
        finally:
            self._params = save_params
            self._in_method_type = save_in_method
            self._declares = save_declares
            self._decl_seen = save_decl_seen
            self._current_fn = save_fn

    def _emit_aggregate_block_merge(
        self, body: list[A.Stmt], ag: A.AggregateDef, other_name: str, other_pl: str, indent: str
    ) -> tuple[list[str], list[str]]:
        save_params = self._params
        save_in_method = self._in_method_type
        save_declares = self._declares
        save_decl_seen = self._decl_seen
        self._params = {other_name}
        self._in_method_type = ag.name
        self._declares = []
        self._decl_seen = set()
        try:
            lines: list[str] = []
            for s in body:
                lines.extend(self._emit_aggregate_stmt(s, indent))
            return lines, list(self._declares)
        finally:
            self._params = save_params
            self._in_method_type = save_in_method
            self._declares = save_declares
            self._decl_seen = save_decl_seen

    def _emit_aggregate_terminate(self, ag: A.AggregateDef, indent: str) -> tuple[list[str], list[str]]:
        """Emit the finish body — every `return X` (top-level or inside an IF)
        becomes `returnValue := X; RETURN ODCIConst.Success;`."""
        save_params = self._params
        save_in_method = self._in_method_type
        save_declares = self._declares
        save_decl_seen = self._decl_seen
        save_fn = self._current_fn
        save_in_terminate = self._in_aggregate_terminate
        self._params = set()
        self._in_method_type = ag.name
        self._declares = []
        self._decl_seen = set()
        self._current_fn = None
        self._in_aggregate_terminate = True
        try:
            lines: list[str] = []
            for s in ag.finish_body:
                lines.extend(self._emit_aggregate_stmt(s, indent))
            return lines, list(self._declares)
        finally:
            self._params = save_params
            self._in_method_type = save_in_method
            self._declares = save_declares
            self._decl_seen = save_decl_seen
            self._current_fn = save_fn
            self._in_aggregate_terminate = save_in_terminate

    def _emit_aggregate_stmt(self, s: A.Stmt, indent: str) -> list[str]:
        """Statement emission inside an aggregate body — handles `.append()` / `.extend()`
        on state lists specially since the surrounding emitter assumes packaged contexts."""
        if isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call):
            call = s.expr
            # self.<field>.append(v)
            if (
                isinstance(call.callee, A.MemberAccess)
                and call.callee.field == "append"
                and isinstance(call.callee.obj, A.MemberAccess)
                and len(call.args) == 1
            ):
                target_obj = self._emit_expr(call.callee.obj)
                val = self._emit_expr(call.args[0])
                return [
                    f"{indent}{target_obj}.EXTEND;",
                    f"{indent}{target_obj}({target_obj}.LAST) := {val};",
                ]
            # self.<field>.extend(other.<field>) — bulk concatenation
            if (
                isinstance(call.callee, A.MemberAccess)
                and call.callee.field == "extend"
                and isinstance(call.callee.obj, A.MemberAccess)
                and len(call.args) == 1
            ):
                target = self._emit_expr(call.callee.obj)
                source = self._emit_expr(call.args[0])
                idx = self._sql_var_counter
                self._sql_var_counter += 1
                return [
                    f"{indent}IF {source} IS NOT NULL AND {source}.COUNT > 0 THEN",
                    f"{indent}  FOR i_{idx} IN {source}.FIRST .. {source}.LAST LOOP",
                    f"{indent}    {target}.EXTEND;",
                    f"{indent}    {target}({target}.LAST) := {source}(i_{idx});",
                    f"{indent}  END LOOP;",
                    f"{indent}END IF;",
                ]
        # Fall back to standard statement emission.
        return self._emit_stmt(s, indent)

    # ---- method signature & body helpers (for `type` and `sealed type`) -

    def _method_signature(self, m: A.MethodDef, type_name: str, *, overriding: bool = False) -> str:
        """Build the spec-level method signature for inclusion in a TYPE declaration."""
        prefix_parts: list[str] = []
        if overriding:
            prefix_parts.append("OVERRIDING")
        if m.is_constructor:
            # `new` => CONSTRUCTOR FUNCTION T(p ...) RETURN SELF AS RESULT
            prefix_parts.append("CONSTRUCTOR FUNCTION")
            params = ", ".join(
                f"{param_name(p.name)} {self._lt(p.type_ref, param=True)}"
                for p in m.params
            )
            sig = f"  {' '.join(prefix_parts)} {type_name}"
            if params:
                sig += f"({params})"
            sig += " RETURN SELF AS RESULT"
            return sig
        if m.is_map:
            # MAP MEMBER FUNCTION foo RETURN NUMBER (no params allowed by Oracle)
            prefix_parts.append("MAP MEMBER FUNCTION")
            if m.params:
                raise EmitError(
                    f"map fn {m.name!r}: must take no parameters", m.loc,
                )
            ret = self._lt(m.return_type, param=True) if m.return_type else "NUMBER"
            return f"  {' '.join(prefix_parts)} {m.name.lower()} RETURN {ret}"
        prefix_parts.append("MEMBER")
        # function vs procedure based on return type
        if m.return_type is None or _is_unit_like(m.return_type):
            prefix_parts.append("PROCEDURE")
        else:
            prefix_parts.append("FUNCTION")
        params = ", ".join(
            f"{param_name(p.name)} {PARAM_MODE_PL[p.mode]} {self._lt(p.type_ref, param=True)}"
            for p in m.params
        )
        sig = f"  {' '.join(prefix_parts)} {m.name.lower()}"
        if params:
            sig += f"({params})"
        if m.return_type is not None and not _is_unit_like(m.return_type):
            sig += f" RETURN {self._lt(m.return_type, param=True)}"
        return sig

    def _method_body(self, m: A.MethodDef, type_logical_name: str, type_pl_name: str, *, is_override: bool = False) -> str:
        """Emit the TYPE BODY entry for a concrete method."""
        sig = self._method_signature(m, type_pl_name, overriding=is_override).lstrip()
        # method body emission shares the statement emitter with regular fns
        save_params = self._params
        save_in_method = self._in_method_type
        save_declares = self._declares
        save_decl_seen = self._decl_seen
        save_fn = self._current_fn
        self._params = {p.name for p in m.params}
        self._in_method_type = type_logical_name
        self._declares = []
        self._decl_seen = set()
        # Set _current_fn to a synthetic FnDef so _emit_return etc. work.
        synth_fn = A.FnDef(
            loc=m.loc, name=m.name, params=m.params,
            return_type=m.return_type, body=m.body,
        )
        self._current_fn = synth_fn
        try:
            body_lines: list[str] = []
            for s in m.body:
                body_lines.extend(self._emit_stmt(s, indent="    "))
            decls = self._declares[:]
        finally:
            self._params = save_params
            self._in_method_type = save_in_method
            self._declares = save_declares
            self._decl_seen = save_decl_seen
            self._current_fn = save_fn
        out: list[str] = []
        out.append(f"  {sig} IS")
        for d in decls:
            out.append(f"    {d}")
        out.append(f"  BEGIN")
        if body_lines:
            out.extend(body_lines)
        else:
            out.append("    NULL;")
        out.append(f"  END;")
        return "\n".join(out)

    def _is_list_member_access(self, recv: A.Expr) -> bool:
        """True if `recv` is a `self.<field>` or `<param>.<field>` where field is list-typed."""
        if not isinstance(recv, A.MemberAccess):
            return False
        ft = self._resolve_member_type(recv)
        return isinstance(ft, A.GenericType) and ft.base == "list"

    def _resolve_member_type(self, ma: A.MemberAccess) -> Optional[A.TypeRef]:
        """Best-effort: figure out the pell TypeRef of `obj.field`."""
        obj = ma.obj
        owner_fields: Optional[list[A.FieldDef]] = None
        # Case 1: obj is `self` inside a method body
        if isinstance(obj, A.Ident) and obj.name == "self" and self._in_method_type is not None:
            owner_fields = self._fields_of_logical_type(self._in_method_type)
        # Case 2: obj is a parameter whose declared type is a known named type
        elif isinstance(obj, A.Ident) and obj.name in self._params and self._current_fn is not None:
            for p in self._current_fn.params:
                if p.name == obj.name and isinstance(p.type_ref, A.NamedType):
                    owner_fields = self._fields_of_logical_type(p.type_ref.name)
        if owner_fields is None:
            return None
        for f in owner_fields:
            if f.name == ma.field:
                return f.type_ref
        return None

    def _fields_of_logical_type(self, name: str) -> Optional[list[A.FieldDef]]:
        """Look up the field list for a TypeDef, sealed parent, sealed case, or aggregate."""
        td = self._lookup_type(name)
        if td is not None:
            return td.fields
        for st in self._sealed_types:
            if st.name == name:
                return st.fields
            for c in st.cases:
                if c.name == name:
                    return list(st.fields) + list(c.fields)
        for ag in self._aggregates:
            if ag.name == name:
                return ag.state_fields
        return None

    def _receiver_is_object_typed(self, recv: A.Expr) -> bool:
        """Heuristic: does `recv` evaluate to an instance of a user-defined OBJECT type?

        - `self` inside a method body → yes (the enclosing type).
        - A parameter whose declared type is a known type → yes.
        - A MemberAccess whose final field is declared as a known type → yes.
        Otherwise no (caller falls back to free-function style).
        """
        if isinstance(recv, A.Ident):
            if recv.name == "self" and self._in_method_type is not None:
                return True
            if recv.name in self._params and self._current_fn is not None:
                for p in self._current_fn.params:
                    if p.name == recv.name and isinstance(p.type_ref, A.NamedType):
                        return p.type_ref.name in self._type_names
        # MemberAccess chain (e.g., self.next.method()) — peek at the field's declared type.
        if isinstance(recv, A.MemberAccess) and self._in_method_type is not None:
            field_type = self._field_type_for_member(recv)
            if field_type is not None and field_type in self._type_names:
                return True
        return False

    def _field_type_for_member(self, ma: A.MemberAccess) -> Optional[str]:
        """If `ma.obj` is `self` inside a known type and `ma.field` is a typed field,
        return the field's named-type name (else None)."""
        if not (isinstance(ma.obj, A.Ident) and ma.obj.name == "self"):
            return None
        if self._in_method_type is None:
            return None
        td = self._lookup_type(self._in_method_type)
        if td is None:
            ck = self._lookup_case(self._in_method_type)
            if ck is None:
                return None
            _, case = ck
            fields = case.fields
        else:
            fields = td.fields
        for f in fields:
            if f.name == ma.field and isinstance(f.type_ref, A.NamedType):
                return f.type_ref.name
        return None

    def _emit_obj_constructor(self, sl: A.StructLit) -> str:
        """`Money { amount: x, currency: y }` → `t_money(x, y)` (positional).

        For sealed-case constructors, the synthetic parent placeholder (when
        the parent had no real fields) gets a NULL prefix.
        """
        type_pl = _record_type_name(sl.type_name)
        ordered_field_names: list[str] = []
        prefix_nulls = 0
        td = self._lookup_type(sl.type_name)
        if td is not None:
            ordered_field_names = [f.name for f in td.fields]
        else:
            ck = self._lookup_case(sl.type_name)
            if ck is not None:
                st, case = ck
                ordered_field_names = [f.name for f in st.fields] + [f.name for f in case.fields]
                if not st.fields:
                    # Parent has a synthetic placeholder attribute; pass NULL.
                    prefix_nulls = 1
        provided = {f.name: f.value for f in sl.fields}
        missing = [n for n in ordered_field_names if n not in provided]
        if missing:
            raise EmitError(
                f"{sl.type_name} {{ ... }}: missing fields {missing}",
                sl.loc,
            )
        args_list = (["NULL"] * prefix_nulls) + [self._emit_expr(provided[n]) for n in ordered_field_names]
        return f"{type_pl}({', '.join(args_list)})"

    def _lookup_type(self, name: str) -> Optional[A.TypeDef]:
        for t in self._types:
            if t.name == name:
                return t
        return None

    def _lookup_case(self, name: str) -> Optional[tuple[A.SealedTypeDef, A.CaseDef]]:
        for st in self._sealed_types:
            for c in st.cases:
                if c.name == name:
                    return (st, c)
        return None

    def _lower_ident(self, name: str) -> str:
        """Map a pell identifier (possibly qualified with ::) to PL/SQL."""
        if name == "self" and self._in_method_type is not None:
            # Inside a member-fn body, `self` is Oracle's implicit SELF parameter.
            return "SELF"
        # Enum variant references — `EnumName::VARIANT` lowers to the text literal.
        if "::" in name:
            head, _, tail = name.rpartition("::")
            if (head, tail) in self._enum_variants:
                return _sql_string(self._enum_variants[(head, tail)])
        # Declared sequence references — emit verbatim (with `::` → `.` for schemas).
        if name in self._seq_names:
            if "::" in name:
                parts = name.split("::")
                return ".".join(p.lower() for p in parts)
            return name.lower()
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


def emit(module: A.Module, target: str = "23", *,
         source_text: Optional[str] = None,
         source_path: Optional[str] = None,
         reproducible: bool = False) -> str:
    return Emitter(
        module, target=target,
        source_text=source_text, source_path=source_path,
        reproducible=reproducible,
    ).emit()
