"""PL/SQL emitter for pell.

Walks a Module AST and emits a CREATE OR REPLACE PACKAGE + PACKAGE BODY
pair. Also emits a one-time pell_runtime stub if errors are declared.

This is the v0 emitter: it covers a useful subset and emits sensible
PL/SQL for the patterns shown in design.md. Sections marked `-- TODO`
in the output indicate constructs that aren't fully lowered yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import ast as A


class EmitError(Exception):
    def __init__(self, msg: str, loc: A.Loc, code: str = ""):
        super().__init__(f"{loc}: {msg}")
        self.loc = loc
        self.msg = msg
        # Stable identifier for tooling (LSP code actions, IDE quickfixes).
        # Empty string when the error isn't auto-fixable; named codes like
        # "pell.unused-return" or "pell.unused-value" let the LSP server
        # offer a precise WorkspaceEdit without parsing the message text.
        self.code = code


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

PRIM_MAP = {
    "number":    "NUMBER",
    "int":       "PLS_INTEGER",
    "text":      "VARCHAR2(4000)",
    # bigtext is `text` for input/output purposes, but lowers to CLOB so
    # payloads can exceed 32K. Use it for params/columns that may hold
    # large strings (file contents, log entries, JSON-as-text, etc.).
    "bigtext":   "CLOB",
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
    "bigtext":   "CLOB",
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


# Oracle's hard-reserved words (V$RESERVED_WORDS where RESERVED='Y').
# These can never name a schema or package — CREATE fails with ORA-04050
# or a syntax error, so we reject them at compile time instead. Discovered
# the hard way twice: `module audit::…` and `module signups::validate`.
_ORACLE_RESERVED = frozenset({
    "access", "add", "all", "alter", "and", "any", "as", "asc", "audit",
    "between", "by", "char", "check", "cluster", "column", "comment",
    "compress", "connect", "create", "current", "date", "decimal",
    "default", "delete", "desc", "distinct", "drop", "else", "exclusive",
    "exists", "file", "float", "for", "from", "grant", "group", "having",
    "identified", "immediate", "in", "increment", "index", "initial",
    "insert", "integer", "intersect", "into", "is", "level", "like",
    "lock", "long", "maxextents", "minus", "mlslabel", "mode", "modify",
    "noaudit", "nocompress", "not", "nowait", "null", "number", "of",
    "offline", "on", "online", "option", "or", "order", "pctfree",
    "prior", "public", "raw", "rename", "resource", "revoke", "row",
    "rowid", "rownum", "rows", "select", "session", "set", "share",
    "size", "smallint", "start", "successful", "synonym", "sysdate",
    "table", "then", "to", "trigger", "uid", "union", "unique", "update",
    "user", "validate", "values", "varchar", "varchar2", "view",
    "whenever", "where", "with",
})


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
    # `.to_text()` is handled specially (not a fixed template) because
    # the conversion depends on the receiver's inferred type — see
    # _emit_call_expr's to_text handling below.
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


# Sentinel prefix used by _infer_expr_type to mark list types, so
# _auto_stringify can route them to the list-to-text helper without
# matching on `T_*_LIST` substrings (which could collide with a
# user-defined record named e.g. `User_List`). The full sentinel value
# is "__PELL_LIST__<elem>" where <elem> is the pell-surface element
# type name ("text", "number", etc.).
_LIST_SENTINEL = "__PELL_LIST__"


# Pell-surface type name → the PL/SQL spelling that _infer_expr_type
# returns. Used when looking up the type of a loop variable, whose
# binding stores the pell name ("json", "text", ...) rather than the
# emitter's PL/SQL form.
_PELL_TO_PLSQL_TYPE: dict[str, str] = {
    "json":      "JSON",
    "text":      "VARCHAR2(4000)",
    "number":    "NUMBER",
    "bool":      "BOOLEAN",
    "date":      "DATE",
    "timestamp": "TIMESTAMP",
    "bytes":     "RAW(32767)",
    "bigtext":   "CLOB",
}


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
                 reproducible: bool = False,
                 debug_markers: bool = False):
        # When True, every emitted statement carries a trailing
        # `-- @pell:<line>` marker naming its pell source line. The
        # debugger derives the pell↔PL/SQL line map from these (see
        # pell/srcmap.py) — no sidecar files, and the map survives in
        # the deployed source itself (ALL_SOURCE).
        self.debug_markers = debug_markers
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
        # Schema + package come from the new explicit model: the schema
        # is the `schema::` prefix (or None = connected schema); the
        # package name is the whole dotted module path mangled with
        # underscores. (No dotted segment is implicitly a schema now.)
        self.schema = module.schema
        self.pkg = module.package_name
        # Oracle rejects reserved words as object names (ORA-04050 / syntax
        # errors at CREATE time) — fail here with a pointable loc instead.
        if module.name != "_pell_anon":
            if self.schema and self.schema.lower() in _ORACLE_RESERVED:
                raise EmitError(
                    f"`{self.schema}` is an Oracle reserved word and can't "
                    f"be a schema name. Pick another name (e.g. "
                    f"`{self.schema}ing`, `{self.schema}s`).",
                    module.loc,
                    code="pell.reserved-name",
                )
            if self.pkg.lower() in _ORACLE_RESERVED:
                raise EmitError(
                    f"module path `{module.name}` lowers to package "
                    f"`{self.pkg}`, an Oracle reserved word — CREATE "
                    f"PACKAGE would fail with ORA-04050. Rename the "
                    f"module (e.g. `{module.name}_pkg` or a longer "
                    f"dotted path).",
                    module.loc,
                    code="pell.reserved-name",
                )
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
        # Loop variable → element type's pell-surface name (e.g. "text",
        # "number", "ColumnInfo"). Lets `_emit_expr` validate field
        # access on the loop variable at compile time: if the element
        # is a primitive, `i.name` is a clear user error (text has no
        # fields) — raise an EmitError with the actual issue instead of
        # letting Oracle throw PLS-00487 at runtime.
        self._loop_var_types: dict[str, str] = {}
        # Element type for the *current* jq!{}-receiving target (set by the
        # surrounding let-binding before recursing into _emit_assign_to /
        # _emit_call_assignment, restored after). For scalar list<T>, this is T.
        # For a record-typed target (.one()), it's the record type itself.
        self._pending_jq_elem_type: Optional[A.TypeRef] = None
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
        # Names of `re::find_all` / `re::split` adapters needed by this
        # package. Adapters copy pell_re's foreign collection type into
        # the package's own t_text_list (PL/SQL nominal typing for
        # collections leaves no other route).
        self._needs_re_adapter: set[str] = set()
        # Set when any `@retry` annotation is emitted — triggers a per-module
        # private helper `pell_is_panic(p_code NUMBER) RETURN BOOLEAN` so the
        # retry handler can decide whether to re-raise without retry.
        self._needs_panic_helper: bool = False
        # List-to-text helper requests. Each entry is (pkg, elem) where
        # `pkg` is `None` for the local `t_<elem>_list` type, or the
        # foreign package name (e.g. "hello") for `<pkg>.t_<elem>_list`.
        # Generates one helper per distinct (pkg, elem) combo so the
        # signature matches the list value being passed in.
        self._needs_list_to_text: set[tuple[Optional[str], str]] = set()
        # Structural list-type tracking: maps local/param name → the
        # element type's pell-surface name ("text", "number", etc.).
        # Auto-stringify checks this dict BEFORE string-pattern matching
        # so a user-defined record named e.g. `User_List` (which would
        # otherwise lower to T_USER_LIST and accidentally match the
        # T_*_LIST suffix) doesn't get confused with a `list<user>`.
        self._list_locals: dict[str, str] = {}
        # Cross-package fn signatures registered by the REPL (or by any
        # caller wiring up sibling .pell files). Keyed by "<pkg>::<fn>".
        # Used by `_infer_call_type` for cross-module return-type
        # inference and by `_emit_let` to declare locals with the
        # FOREIGN collection type (avoids the PLS-00382 nominal-type
        # clash on cross-package list returns).
        self._project_signatures: dict[str, A.TypeRef] = {}
        # Cross-package record registry. Populated externally by the
        # build/exec/repl entry points so the emitter can lower
        # `pkg::Record { … }` to a per-field assign on a foreign-typed
        # temp local. Two indices:
        #   _project_records[("pkg", "Record")] -> RecordDef
        #   _project_records_by_name["Record"] -> list[("pkg", RecordDef)]
        # Bare-name lookups (`let a: Summary` without qualifier) succeed
        # when exactly one project module exposes that name; ambiguous
        # cases raise a helpful EmitError pointing at the candidates.
        self._project_records: dict[tuple[str, str], A.RecordDef] = {}
        self._project_records_by_name: dict[str, list[tuple[str, A.RecordDef]]] = {}
        # Project module registry (package/short name -> ModuleInfo).
        # Drives ACCESSIBLE BY emission + the compile-time access check.
        # Populated externally via emit(project_modules=...).
        self._project_modules: dict[str, "ModuleInfo"] = {}
        # Packages currently in scope for cross-package references. Always
        # includes the local module so self-qualified refs work. Each
        # import contributes BOTH the fully-qualified form (`std_logger`)
        # and the short form (`logger`) since pell registers both shapes
        # in scan_project_signatures. Cross-pkg refs to packages not in
        # this set raise pell.missing-import.
        self._imported_packages: set[str] = set()
        self._imported_packages.add(module.package_name)
        self._imported_packages.add(module.short_name)
        for _it in module.items:
            if isinstance(_it, A.ImportStmt):
                # `import [schema::] a.b.c;` — strip the optional schema
                # prefix, then register the package name (dotted path
                # with underscores) and the short name (last segment).
                _path = _it.path.partition("::")[2] if "::" in _it.path else _it.path
                self._imported_packages.add(_path.replace(".", "_"))
                self._imported_packages.add(_path.split(".")[-1])
        # convenience wrapper that threads self.target through type lowering
        # AND resolves cross-package record references so they emit as
        # `<pkg>.t_<record>` instead of the unqualified `t_<record>` that
        # would refer to a local-only type.
        def _lt(t, *, param: bool = False, sql_context: bool = False) -> str:
            if isinstance(t, A.NamedType):
                # Qualified `pkg::Type` — verify the package is imported.
                # The error fires here so the squiggle lands on the type
                # annotation token, not on whatever statement happened to
                # be doing the lookup.
                if "::" in t.name:
                    pkg = t.name.rsplit("::", 1)[0]
                    pkg_key = pkg.replace("::", "_").replace(".", "_")
                    self._check_pkg_imported(pkg_key, t.loc)
                rec, pkg = self._lookup_record_with_pkg(t.name)
                if rec is not None and pkg is not None:
                    return f"{pkg}.{_record_type_name(rec.name)}"
            return lower_type(
                t, param=param, target=self.target, sql_context=sql_context,
            )
        self._lt = _lt
        self._pipelined_fn_names: set[str] = {
            f.name for f in self._fns
            if any(a.name == "pipelined" for a in f.annotations)
        }

    # ---- entry point ----------------------------------------------------

    def emit(self) -> str:
        # `@stub` modules — every pub fn is a stub, no real implementation
        # to lower. These exist purely to feed signatures + completion into
        # the project registries; deploying them would `CREATE OR REPLACE
        # PACKAGE x AS ...` and clobber the real Oracle SYS package. Emit
        # an empty-output marker so CLI knows to skip file generation.
        if self._is_stub_module():
            return ""
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

    def _is_stub_fn(self, fn: A.FnDef) -> bool:
        """True iff `fn` is a stub. Either the fn itself is annotated
        `@stub`, or the enclosing module is annotated `@stub` (one
        marker for the whole file instead of one per fn). Used for the
        SYS-package shims in runtime/stubs/."""
        if self._is_stub_module():
            return True
        return any(a.name == "stub" for a in fn.annotations)

    def _is_stub_module(self) -> bool:
        """True iff `@stub` annotates the module itself (preferred:
        one marker, applies to every item) OR every pub fn is
        individually `@stub`-annotated (legacy). A module with mixed
        stub + real fns emits normally with stubs skipped."""
        if any(a.name == "stub" for a in self.module.annotations):
            return True
        if not self._fns:
            return False
        return all(
            any(a.name == "stub" for a in fn.annotations)
            for fn in self._fns
        )

    def emit_anon(self) -> str:
        """Emit the module as a self-contained DECLARE/BEGIN/END block.

        Caller is responsible for synthesizing a `pell_anon_main` fn from the
        cell's top-level statements; this method just nests every fn + record
        in the DECLARE section and calls `pell_anon_main` from BEGIN.
        """
        # OBJECT-TYPE-requiring items have no place in an anon block —
        # CREATE TYPE is schema-level only.
        if self._types:
            t = self._types[0]
            raise EmitError(
                "anon-block mode: `type` declarations need CREATE TYPE — "
                "use `pell build` for the module instead.",
                t.loc,
            )
        if self._sealed_types:
            st = self._sealed_types[0]
            raise EmitError(
                "anon-block mode: `sealed type` needs CREATE TYPE.",
                st.loc,
            )
        if self._aggregates:
            ag = self._aggregates[0]
            raise EmitError(
                "anon-block mode: `aggregate` needs CREATE TYPE + AGGREGATE USING.",
                ag.loc,
            )
        for fn in self._fns:
            if any(a.name == "pipelined" for a in fn.annotations):
                raise EmitError(
                    "anon-block mode: @pipelined fns need schema-level OBJECT TYPES.",
                    fn.loc,
                )
        if self._errors:
            err = self._errors[0]
            raise EmitError(
                "anon-block mode: pell `error` decls need the runtime package — "
                "install it once with `pell runtime`, then use `pell build`.",
                err.loc,
            )

        # Drive per-fn body emit so list_type_decls and helper flags populate.
        fn_bodies = [self._fn_body(fn) for fn in self._fns]

        out: list[str] = []
        out.append("DECLARE")
        for rec in self._records:
            out.append(self._render_record_type(rec, indent="  "))
        for decl in self._list_type_decls:
            out.append(decl)
        if self._needs_split_helper:
            out.append("")
            out.append(self._split_helper_source())
        if self._needs_panic_helper:
            out.append("")
            out.append(self._panic_helper_source())
        for pkg, elem in sorted(self._needs_list_to_text, key=lambda x: (x[0] or "", x[1])):
            out.append("")
            out.append(self._list_to_text_helper_source(pkg, elem))
        for chunk in fn_bodies:
            out.append("")
            out.append(chunk)
        out.append("BEGIN")
        out.append("  pell_anon_main;")
        out.append("END;")
        out.append("/")
        return "\n".join(out)

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
                _safe_drop_type(self._q(obj_name))
                + f"CREATE OR REPLACE TYPE {self._q(obj_name)} AS OBJECT (\n"
                + ",\n".join(field_lines)
                + "\n);\n/"
            )
            self._obj_emitted.add(obj_name)
        if with_table:
            nt_name = f"{self.pkg}_{rec.name.lower()}_nt"
            nt_key = f"NT:{nt_name}"
            if nt_key not in self._obj_emitted:
                self._schema_types.append(
                    _safe_drop_type(self._q(nt_name))
                    + f"CREATE OR REPLACE TYPE {self._q(nt_name)} AS TABLE OF {obj_name};\n/"
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
        """Resolve a record reference. Handles three call sites:

          * Bare local: `Summary` matches a record in this module.
          * Qualified foreign: `collections_linked_list::Summary` looks
            up in the project registry, regardless of imports.
          * Bare cross-package: `Summary` falls back to the registry
            when it's unique across all scanned modules.

        Returns the RecordDef or None. Use _lookup_record_with_pkg when
        the caller also needs the owning PL/SQL package name (e.g. to
        qualify the lowered type as `<pkg>.t_<record>`).
        """
        rec, _ = self._lookup_record_with_pkg(name)
        return rec

    def _lookup_record_with_pkg(
        self, name: str,
    ) -> tuple[Optional[A.RecordDef], Optional[str]]:
        """Same as _lookup_record but also returns the owning PL/SQL
        package name (None when the record is local). The returned
        package name is already the lowered identifier — e.g.
        `collections_linked_list`, not `collections.linked_list`.

        Import gating is enforced by callers via _check_pkg_imported;
        the lookup itself returns whatever it finds in the registry,
        even if the package isn't currently imported. This separation
        lets callers attach a precise source loc to the resulting
        pell.missing-import error.
        """
        # Qualified path: `pkg::Record` — split, look up directly.
        if "::" in name:
            pkg, _, rec_name = name.rpartition("::")
            pkg = pkg.replace("::", "_").replace(".", "_")
            rec = self._project_records.get((pkg, rec_name))
            if rec is not None:
                return rec, pkg
            return None, None
        # Local module records take priority.
        for r in self._records:
            if r.name == name:
                return r, None
        # Fall back to project registry by bare name, filtered to the
        # set of imported packages. Bare `Summary` only matches when
        # `import collections_linked_list;` is in effect.
        matches = self._project_records_by_name.get(name, [])
        imported = [
            (pkg, rec) for (pkg, rec) in matches
            if pkg in self._imported_packages
        ]
        if len(imported) == 1:
            pkg, rec = imported[0]
            return rec, pkg
        return None, None

    # Intrinsic / built-in namespaces that don't require an `import`
    # because they aren't real pell modules — they lower to inline
    # PL/SQL or Oracle-stdlib pass-throughs. Anything listed here
    # bypasses the pkg-import check in _emit_call_expr.
    _INTRINSIC_NAMESPACES = frozenset({
        "json", "jq", "re", "sql", "pell", "pivot", "bulk",
        "std", "self",
    })

    def _is_intrinsic_call(self, callee_name: str) -> bool:
        """True when the leading `::`-segment is one of the emitter's
        synthetic namespaces. `json::get_text(...)` and friends never
        need an `import json;` line — they're macro-flavoured surface
        the emitter handles directly."""
        first = callee_name.split("::", 1)[0]
        return first in self._INTRINSIC_NAMESPACES

    def _resolve_struct_lit_record(
        self, sl: A.StructLit,
    ) -> Optional[A.RecordDef]:
        """Look up the record for a struct literal, gating on imports.
        For qualified type names (`pkg::Summary`), raises pell.missing-
        import if pkg isn't imported. For bare names, the lookup is
        already import-filtered (see _lookup_record_with_pkg).

        Centralizes what every StructLit emit site otherwise has to do
        twice — call sites become a single `rec = self._resolve_struct
        _lit_record(sl)`.
        """
        if "::" in sl.type_name:
            pkg = sl.type_name.rsplit("::", 1)[0]
            pkg_key = pkg.replace("::", "_").replace(".", "_")
            self._check_pkg_imported(pkg_key, sl.loc)
        return self._lookup_record(sl.type_name)

    def _check_pkg_accessible(self, pkg: str, loc: A.Loc) -> None:
        """Mirror Oracle's ACCESSIBLE BY enforcement at pell compile
        time: a call to a GUARDED submodule from outside its access
        domain is an error here, with a precise loc, instead of a
        PLS-00904 at install time (or worse, an unguarded mistake).

        A target is guarded iff the emitted spec carries an
        ACCESSIBLE BY clause — non-root, not @open, and its domain has
        at least one other member (the exact emission conditions in
        _accessible_by_clause). Unknown packages (Oracle stdlib,
        external schemas) are never checked.
        """
        target = self._project_modules.get(pkg)
        if target is None or target.is_root or target.is_open:
            return
        # Domain has other members? (Dedupe registry rows — both the
        # package name and the short name map to the same ModuleInfo.)
        domain_pkgs = {
            info.package_name
            for info in self._project_modules.values()
            if info.domain_key == target.domain_key
        }
        if len(domain_pkgs) <= 1:
            return  # standalone — no clause was emitted, stays public
        caller = self.module
        if caller.name == "_pell_anon":
            raise EmitError(
                f"module `{target.path}` is a guarded submodule — its "
                f"package is ACCESSIBLE BY the `{target.domain}` domain "
                f"only, so anonymous blocks (REPL / pell exec) can't "
                f"call it. Call through the domain's root module "
                f"`{target.domain}`, or mark the module `@open` to lift "
                f"the wall.",
                loc,
                code="pell.access-violation",
            )
        caller_key = (caller.schema or "", caller.access_domain)
        if caller_key != target.domain_key:
            raise EmitError(
                f"module `{caller.name}` (domain `{caller.access_domain}`) "
                f"can't call into `{target.path}` — that package is "
                f"ACCESSIBLE BY the `{target.domain}` domain only. Call "
                f"its root module `{target.domain}` instead, or mark "
                f"`{target.path}` as `@open`.",
                loc,
                code="pell.access-violation",
            )

    def _check_pkg_imported(self, pkg: str, loc: A.Loc) -> None:
        """Raise pell.missing-import if `pkg` is not in the local
        module's import set. Callers pass the source loc of the
        cross-package reference so the squiggle lands precisely on
        the qualifier (e.g. on `catalog` in `catalog::list_tables()`).
        Pure no-op when the package IS imported.
        """
        if pkg in self._imported_packages:
            return
        raise EmitError(
            f"package `{pkg}` is not imported. Add `import {pkg};` "
            "to the top of this file.",
            loc,
            code="pell.missing-import",
        )

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

    def _accessible_by_clause(self) -> Optional[str]:
        """Build the ACCESSIBLE BY (...) clause for this module, or None
        when the package should stay public.

        Access model (full lineage + siblings — i.e., the whole domain):
          * The access DOMAIN is (schema, top path segment). Everything
            under `demo::app.*` can call everything else under it.
          * ROOT modules (`demo::app` — no dotted sub-path) are the
            public application interface: no clause, callable by anyone.
          * NON-ROOT modules (`demo::app.parsing`) get
            `ACCESSIBLE BY (PACKAGE demo.app, PACKAGE demo.app_validation, ...)`
            listing every OTHER package in the domain. Oracle then
            rejects calls from outside the domain at compile time of
            the caller (PLS-00904) — real runtime-adjacent enforcement,
            not a pell-only fiction.
          * `@open` on the module opts out (debugging / REPL access).
          * No project context (single-file build) or a single-member
            domain → no clause. The walls go up once the domain has
            other members.
        """
        if self.module.is_root_module:
            return None
        if any(a.name == "open" for a in self.module.annotations):
            return None
        if not self._project_modules:
            return None
        my_key = (self.module.schema or "", self.module.access_domain)
        members = {
            info.qualified_name: info
            for info in self._project_modules.values()
            if info.domain_key == my_key
            and info.package_name != self.module.package_name
        }
        if not members:
            return None
        accessors = ", ".join(
            f"PACKAGE {qn}" for qn in sorted(members)
        )
        return f"ACCESSIBLE BY ({accessors})"

    def _exception_name(self, err_name: str) -> str:
        # Exceptions land in the global pell_runtime package, so they have
        # to be globally unique. Use the SCHEMA-qualified package name
        # (schema + package, mangled) so `hr::employees.NotFound` and
        # `acct::employees.NotFound` don't collide as `employees_notfound`.
        full = self.module.qualified_name.replace(".", "_")
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
        accessible_by = self._accessible_by_clause()
        if accessible_by:
            out.append(f"CREATE OR REPLACE PACKAGE {self._q(self.pkg)}")
            out.append(f"  {accessible_by}")
            out.append("AS")
        else:
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
        # public fn signatures (stub fns are scanner-visible but never
        # emitted into the package — see _is_stub_fn).
        for fn in self._fns:
            if fn.is_pub and not self._is_stub_fn(fn):
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
            if self._is_stub_fn(fn):
                continue
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
        for pkg, elem in sorted(self._needs_list_to_text, key=lambda x: (x[0] or "", x[1])):
            out.append("")
            out.append(self._list_to_text_helper_source(pkg, elem))
        for adapter in sorted(self._needs_re_adapter):
            out.append("")
            out.append(self._re_adapter_source(adapter))
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
        # Forward declarations for PRIVATE fns. Public fns are declared
        # in the package spec, so the body may call them in any order.
        # Private fns live only in the body — so calling one before its
        # definition is a forward reference (PLS-00313). Declaring every
        # private fn's signature up front lets them call each other (and
        # be called by earlier pub fns) regardless of source order.
        private_fwd = [
            fn for fn in self._fns
            if not fn.is_pub and not self._is_stub_fn(fn)
            and not any(a.name == "pipelined" for a in fn.annotations)
        ]
        if private_fwd:
            out.append("")
            out.append("  -- Forward declarations (private fns; lets them be")
            out.append("  -- called before their definition in this body).")
            for fn in private_fwd:
                out.append("  " + self._fn_signature(fn) + ";")
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

    def _re_adapter_source(self, op: str) -> str:
        """A package-private adapter for `re::find_all` / `re::split` that
        bridges pell_re's collection type into the package's t_text_list."""
        return (
            f"  FUNCTION pell_re_{op}(p_s VARCHAR2, p_pat VARCHAR2) RETURN t_text_list IS\n"
            f"    src pell_re.t_text_list := pell_re.{op}(p_s, p_pat);\n"
            f"    out t_text_list;\n"
            f"  BEGIN\n"
            f"    IF src.COUNT = 0 THEN RETURN out; END IF;\n"
            f"    FOR i IN src.FIRST .. src.LAST LOOP\n"
            f"      out(i) := src(i);\n"
            f"    END LOOP;\n"
            f"    RETURN out;\n"
            f"  END pell_re_{op};"
        )

    def _list_to_text_helper_source(self, pkg: Optional[str], elem: str) -> str:
        """A package-private helper that joins a list<T> into a single
        VARCHAR2 like `[a, b, c]`. One overload per (pkg, elem) combo —
        the param type matches what the caller is passing in (local
        `t_<elem>_list` or foreign `<pkg>.t_<elem>_list`).

        Elements are individually run through the matching to-text
        conversion so the formatting matches what `p()` / `print()`
        do for scalars (TO_CHAR with explicit format mask for dates,
        JSON_SERIALIZE for json, etc.).
        """
        list_type = f"{pkg}.t_{elem}_list" if pkg else f"t_{elem}_list"
        fn_name = (f"pell_list_to_text_{pkg}_{elem}" if pkg
                   else f"pell_list_to_text_{elem}")
        # Per-element conversion to text. Inlined into the loop body
        # using `p_xs(l_i)` as the element reference.
        elem_table = {
            "text":      ("p_xs(l_i)", False),
            "number":    ("TO_CHAR(p_xs(l_i))", False),
            "date":      ("TO_CHAR(p_xs(l_i), 'YYYY-MM-DD HH24:MI:SS')", False),
            "timestamp": ("TO_CHAR(p_xs(l_i), 'YYYY-MM-DD HH24:MI:SS.FF')", False),
            "bool":      ("CASE WHEN p_xs(l_i) THEN 'true' ELSE 'false' END", True),
            "json":      ("JSON_SERIALIZE(p_xs(l_i))", False),
            "bytes":     ("RAWTOHEX(p_xs(l_i))", False),
        }
        to_text, is_bool = elem_table.get(elem, ("TO_CHAR(p_xs(l_i))", False))
        # BOOLEAN can't be wrapped in NVL — NVL needs the same type
        # for both args, and there's no NULL→text path. For booleans,
        # we use COALESCE with a string fallback. Actually the CASE
        # already returns text, so just leave it bare.
        wrapped = to_text if is_bool else f"NVL({to_text}, 'NULL')"
        return (
            f"  FUNCTION {fn_name}(p_xs IN {list_type}) RETURN VARCHAR2 IS\n"
            f"    l_buf VARCHAR2(32767);\n"
            f"    l_i PLS_INTEGER;\n"
            f"  BEGIN\n"
            f"    IF p_xs.COUNT = 0 THEN RETURN '[]'; END IF;\n"
            f"    l_buf := '[';\n"
            f"    l_i := p_xs.FIRST;\n"
            f"    WHILE l_i IS NOT NULL LOOP\n"
            f"      IF l_i != p_xs.FIRST THEN l_buf := l_buf || ', '; END IF;\n"
            f"      l_buf := l_buf || {wrapped};\n"
            f"      l_i := p_xs.NEXT(l_i);\n"
            f"    END LOOP;\n"
            f"    RETURN l_buf || ']';\n"
            f"  END {fn_name};"
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
            # Track list-typed params structurally — record the element's
            # pell-surface name so auto-stringify can find a matching
            # `pell_list_to_text_<elem>` helper without string parsing.
            if (isinstance(p.type_ref, A.GenericType)
                    and p.type_ref.base == "list"
                    and p.type_ref.params
                    and isinstance(p.type_ref.params[0], A.PrimType)):
                self._list_locals[p.name] = p.type_ref.params[0].name

        self._check_annotation_conflicts(fn)
        # Static check: every path of a non-Unit fn must end in
        # `return`. PL/SQL accepts a function body that falls through
        # and then throws ORA-06503 at runtime ("function returned
        # without value"). Catch it at compile time instead.
        # Pipelined fns are excluded — they `yield` values, no `return`.
        # Stubs (annotated `@stub`) are signature-only declarations —
        # they never emit a body, so missing-return doesn't apply. Same
        # exemption for `@pipelined` (yields, not returns).
        if (fn.return_type is not None
                and not _is_unit_like(fn.return_type)
                and not self._is_stub_fn(fn)
                and not any(a.name == "pipelined" for a in fn.annotations)
                and not _body_always_returns(fn.body)):
            raise EmitError(
                f"function `{fn.name}` declared `-> "
                f"{_render_type(fn.return_type)}` has a path that "
                "doesn't return. PL/SQL would throw ORA-06503 at "
                "runtime. Add an explicit `return …;` at the missing "
                "path (or `raise <Error>;` if that path is "
                "unreachable in practice).",
                fn.loc,
                code="pell.missing-return",
            )

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
        out.append(f"  {sig} IS{self._dbg_loc_marker(fn.loc)}")
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

    def _dbg_loc_marker(self, loc: Optional[A.Loc]) -> str:
        """Trailing `-- @pell:N` marker for a declaration line, or ""
        when markers are off / the loc is unknown."""
        if not self.debug_markers or loc is None or not getattr(loc, "line", None):
            return ""
        return f"  -- @pell:{loc.line}"

    def _emit_stmt(self, s: A.Stmt, indent: str) -> list[str]:
        lines = self._emit_stmt_inner(s, indent)
        if self.debug_markers and lines:
            loc = getattr(s, "loc", None)
            line_no = getattr(loc, "line", None) if loc is not None else None
            first = lines[0]
            # Skip when the line already carries a marker (nested stmts
            # re-enter here) or when it ends inside a string literal
            # (odd quote count) — a trailing comment would corrupt it.
            if (line_no
                    and "-- @pell:" not in first
                    and first.count("'") % 2 == 0):
                lines[0] = f"{first}  -- @pell:{line_no}"
        return lines

    def _emit_stmt_inner(self, s: A.Stmt, indent: str) -> list[str]:
        if isinstance(s, A.LetStmt):
            return self._emit_let(s, indent)
        if isinstance(s, A.AssignStmt):
            # `a = catalog::list_tables();` where `a` is a *locally*-
            # typed list — same nominal mismatch as the return case.
            # Reuse the foreign-list adapter so the assignment becomes
            # a transit local + element copy.
            xpkg = self._cross_pkg_list_assign(s, indent)
            if xpkg is not None:
                return xpkg
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
        # No annotation but the RHS is a known cross-pkg list call —
        # route through the list-let path so the foreign type gets
        # declared AND _list_locals gets registered (so auto-stringify
        # and for-loop-over-list both find this var structurally).
        if (s.type_annot is None
                and isinstance(s.value, A.Call)
                and isinstance(s.value.callee, A.Ident)
                and s.value.callee.name in self._project_signatures):
            sig = self._project_signatures[s.value.callee.name]
            if (isinstance(sig, A.GenericType) and sig.base == "list"
                    and len(sig.params) == 1):
                # Synthesize the annotation in-place so the list-let path
                # has the elem type. (Doesn't mutate the user's source —
                # this LetStmt is a copy at this point.)
                s.type_annot = sig
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
        # If the user annotated a target type, expose it to jq!{} lowering so a
        # `.one()` chain on a record-typed target can shape the COLUMNS clause.
        prev_jq = self._pending_jq_elem_type
        if s.type_annot is not None:
            self._pending_jq_elem_type = s.type_annot
        try:
            return self._emit_assign_to(nm, s.value, indent)
        finally:
            self._pending_jq_elem_type = prev_jq

    def _emit_list_let_from_expr(self, s: A.LetStmt, nm: str, indent: str) -> list[str]:
        """`let xs: list<T> = <expr>;` for non-literal RHS (e.g. .collect())."""
        assert isinstance(s.type_annot, A.GenericType)
        elem_t = s.type_annot.params[0]
        elem_sql = self._lt(elem_t)
        elem_name = _render_type(elem_t)
        list_type = f"t_{_safe(elem_name)}_list"
        # Cross-module call returning `list<T>` — the foreign return
        # type is `<pkg>.t_<elem>_list`, which is a nominally-different
        # type from our local `t_<elem>_list` even though the shapes
        # match. If we have a registered signature for this fn, declare
        # the local with the FOREIGN type directly so the assignment
        # is type-identical (no adapter loop needed). Otherwise fall
        # back to the element-copy adapter.
        if (isinstance(s.value, A.Call)
                and isinstance(s.value.callee, A.Ident)
                and "::" in s.value.callee.name):
            segs = s.value.callee.name.split("::")
            pkg = ".".join(segs[:-1])
            if s.value.callee.name in self._project_signatures:
                foreign_type = f"{pkg}.{list_type}"
                self._decl(f"{nm} {foreign_type};")
                self._local_types[s.name] = foreign_type
                self._list_locals[s.name] = elem_name
                call_code = self._emit_expr(s.value)
                return [f"{indent}{nm} := {call_code};"]
            # No signature known — adapt by copying elements.
            if list_type not in self._list_types_emitted:
                self._list_type_decls.append(
                    f"  TYPE {list_type} IS TABLE OF {elem_sql} INDEX BY PLS_INTEGER;"
                )
                self._list_types_emitted.add(list_type)
            self._decl(f"{nm} {list_type};")
            self._local_types[s.name] = list_type
            self._list_locals[s.name] = elem_name
            return self._emit_cross_pkg_list_adapter(nm, s.value, list_type, indent)
        # Same-module / literal RHS — declare local type and assign directly.
        if list_type not in self._list_types_emitted:
            self._list_type_decls.append(
                f"  TYPE {list_type} IS TABLE OF {elem_sql} INDEX BY PLS_INTEGER;"
            )
            self._list_types_emitted.add(list_type)
        self._decl(f"{nm} {list_type};")
        self._local_types[s.name] = list_type
        self._list_locals[s.name] = elem_name
        if s.value is None:
            return []
        prev_jq = self._pending_jq_elem_type
        self._pending_jq_elem_type = elem_t
        try:
            return self._emit_assign_to(nm, s.value, indent)
        finally:
            self._pending_jq_elem_type = prev_jq

    def _emit_cross_pkg_list_adapter(
        self, target: str, call: A.Call, list_type: str, indent: str,
    ) -> list[str]:
        """Copy a foreign-package `list<T>` return into the local list.

        For `let xs: list<text> = hello::user_tables();`, emits:

            l_pell_listadapter_0 hello.t_text_list;
            l_pell_listadapter_0 := hello.user_tables();
            i := l_pell_listadapter_0.FIRST;
            WHILE i IS NOT NULL LOOP
              l_xs(i) := l_pell_listadapter_0(i);
              i := l_pell_listadapter_0.NEXT(i);
            END LOOP;

        Walks via FIRST / NEXT so sparse associative arrays are
        handled correctly. The whole thing is wrapped in a nested
        DECLARE so the iteration index is local.
        """
        assert isinstance(call.callee, A.Ident)
        segs = call.callee.name.split("::")
        pkg = ".".join(segs[:-1])
        foreign_type = f"{pkg}.{list_type}"
        tmp = f"l_pell_listadapter_{self._sql_var_counter}"
        self._sql_var_counter += 1
        self._decl(f"{tmp} {foreign_type};")
        call_code = self._emit_expr(call)
        return [
            f"{indent}{tmp} := {call_code};",
            f"{indent}DECLARE",
            f"{indent}  l_i PLS_INTEGER;",
            f"{indent}BEGIN",
            f"{indent}  l_i := {tmp}.FIRST;",
            f"{indent}  WHILE l_i IS NOT NULL LOOP",
            f"{indent}    {target}(l_i) := {tmp}(l_i);",
            f"{indent}    l_i := {tmp}.NEXT(l_i);",
            f"{indent}  END LOOP;",
            f"{indent}END;",
        ]

    def _cross_pkg_list_assign(
        self, s: A.AssignStmt, indent: str,
    ) -> Optional[list[str]]:
        """If `s` is `a = pkg::fn();` where `pkg::fn` returns a
        `list<T>` AND `a` is typed as the *local* `t_<T>_list`,
        adapter-copy the foreign result into `a` instead of emitting
        a raw assignment (which would fail with PLS-00382 nominal
        type clash). Returns None when the assignment doesn't match
        the pattern — caller falls through to the default emit.

        Mirrors the same lowering shipped for return-position in
        _emit_return; same shape, different caller site.
        """
        if not isinstance(s.target, A.Ident):
            return None
        if not isinstance(s.value, A.Call):
            return None
        if not isinstance(s.value.callee, A.Ident):
            return None
        fn_name = s.value.callee.name
        if "::" not in fn_name:
            return None
        sig = self._project_signatures.get(fn_name)
        if not (isinstance(sig, A.GenericType)
                and sig.base == "list"
                and len(sig.params) == 1):
            return None
        # Target must be a list-typed local. The local-types entry is
        # the PL/SQL form (e.g. `t_text_list`). If it already carries
        # a dot, it's foreign-typed and a direct assign works — bail.
        local_pl_type = self._local_types.get(s.target.name)
        if not local_pl_type or "." in local_pl_type:
            return None
        elem_name = _render_type(sig.params[0])
        if local_pl_type != f"t_{_safe(elem_name)}_list":
            return None
        list_type = local_pl_type
        target_pl = local_name(s.target.name)
        return self._emit_cross_pkg_list_adapter(
            target_pl, s.value, list_type, indent,
        )

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
        # Store the pell-surface element name (e.g. "text", "number")
        # so auto-stringify can pick the right helper without inferring
        # from the PL/SQL spelling.
        self._list_locals[s.name] = _render_type(elem_t)
        lines: list[str] = []
        for i, el in enumerate(s.value.elements, start=1):
            lines.append(f"{indent}{nm}({i}) := {self._emit_expr(el)};")
        return lines

    def _emit_assign_to(self, target: str, expr: A.Expr, indent: str) -> list[str]:
        """Emit one or more PL/SQL statements that assign `expr` to `target`."""
        if isinstance(expr, A.PipelineExpr):
            expr = self._reduce_pipeline(expr)
        # JSON dot-chain — `j.set(...).set(...).remove(...)` on a json-typed
        # receiver collapses to a single JSON_TRANSFORM. Must lower as
        # `SELECT ... INTO target FROM dual` because JSON_TRANSFORM's `SET`
        # keyword is SQL-only and the PL/SQL parser doesn't accept it.
        chain = self._maybe_emit_json_chain(target, expr, indent)
        if chain is not None:
            return chain
        # `exec_dyn(<sql_string>)` (optionally wrapped in `?`) → EXECUTE
        # IMMEDIATE … INTO target USING … . Only legal inside an unsafe fn.
        inner_for_dyn = expr.inner if isinstance(expr, A.QuestionMark) else expr
        if (isinstance(inner_for_dyn, A.Call)
                and isinstance(inner_for_dyn.callee, A.Ident)
                and inner_for_dyn.callee.name == "exec_dyn"):
            return self._emit_exec_dyn(target, inner_for_dyn, indent)
        # `json::into::<Record>(j_expr)` — multi-statement field-by-field
        # population of `target` from a JSON value via JSON_VALUE / JSON_QUERY.
        if (isinstance(inner_for_dyn, A.Call)
                and isinstance(inner_for_dyn.callee, A.Ident)
                and inner_for_dyn.callee.name == "json::into"
                and inner_for_dyn.type_args):
            return self._emit_json_into(target, inner_for_dyn, indent)
        # `re::capture::<Record>(s, pat)` — populate target's record fields
        # from named captures in the regex result.
        if (isinstance(inner_for_dyn, A.Call)
                and isinstance(inner_for_dyn.callee, A.Ident)
                and inner_for_dyn.callee.name == "re::capture"
                and inner_for_dyn.type_args):
            return self._emit_re_capture(target, inner_for_dyn, indent)
        if isinstance(expr, A.QuestionMark):
            inner = expr.inner
            if isinstance(inner, A.Call):
                return self._emit_questionmark_call(target, inner, indent)
        if isinstance(expr, A.Call):
            return self._emit_call_assignment(target, expr, indent)
        # sql!{}.one() (no question mark) — still emit the select-into
        if isinstance(expr, A.Call):
            return self._emit_call_assignment(target, expr, indent)
        # Record struct-lit: PL/SQL RECORD types have no constructor, so we
        # field-by-field assign to the (already-declared) target. OBJECT-type
        # struct-lits flow through _emit_expr → _emit_obj_constructor instead.
        if isinstance(expr, A.StructLit) and expr.type_name not in self._type_names:
            rec = self._resolve_struct_lit_record(expr)
            if rec is not None:
                provided = {f.name: f.value for f in expr.fields}
                lines: list[str] = []
                for f in rec.fields:
                    if f.name in provided:
                        lines += self._emit_record_field_assign(
                            f"{target}.{f.name.lower()}", provided[f.name], indent,
                        )
                return lines
        return [f"{indent}{target} := {self._emit_expr(expr)};"]

    def _emit_record_field_assign(
        self, field_target: str, value: A.Expr, indent: str,
    ) -> list[str]:
        """Assign `value` to a record field. List-literal values can't be
        assigned wholesale (PL/SQL has no list literal) — they become
        index-wise assigns into the field's collection. Everything else
        is a plain `field := <expr>;`."""
        if isinstance(value, A.ListLit):
            lines: list[str] = []
            for i, el in enumerate(value.elements, start=1):
                lines.append(
                    f"{indent}{field_target}({i}) := {self._emit_expr(el)};"
                )
            return lines
        return [f"{indent}{field_target} := {self._emit_expr(value)};"]

    def _suggest_record_def(self, sl: A.StructLit) -> str:
        """Build a copy-pasteable `record Foo { ... }` definition by
        inferring each field's pell-surface type from its value
        expression. Used by the "unknown type in struct literal" error
        so the user can fix the missing definition without leaving
        the source line.

        Fields whose value type can't be inferred fall back to `text`
        with a comment marker, so the user knows where to look.
        """
        parts: list[str] = []
        for fi in sl.fields:
            pl_type = self._infer_expr_type(fi.value)
            pell_type = self._pl_to_pell_type(pl_type, fi.value)
            parts.append(f"{fi.name}: {pell_type}")
        return f"record {sl.type_name} {{ {', '.join(parts)} }}"

    @staticmethod
    def _pl_to_pell_type(pl_type: Optional[str], value: A.Expr) -> str:
        """Reverse-map a PL/SQL type spelling to its pell-surface name.

        Falls back to a peek at the value AST for cases _infer_expr_type
        returns None (e.g., ListLit, nested StructLit).
        """
        if pl_type:
            t = pl_type.upper()
            if "NUMBER" in t or "PLS_INTEGER" in t or "INTEGER" in t:
                return "number"
            if t == "BOOLEAN":
                return "bool"
            if "JSON" in t:
                return "json"
            if "TIMESTAMP" in t:
                return "timestamp"
            if "DATE" in t:
                return "date"
            if "RAW" in t:
                return "bytes"
            if "CLOB" in t:
                return "bigtext"
            if "VARCHAR" in t or "CHAR" in t:
                return "text"
            # t_<name> means it's a record type — use the bare name
            if t.startswith("T_") and not t.endswith("_LIST"):
                return t[2:].lower().title().replace("_", "")
        # Couldn't infer from the type string — peek at the AST shape.
        if isinstance(value, A.ListLit):
            if value.elements:
                inner = Emitter._pl_to_pell_type(None, value.elements[0])
                return f"list<{inner}>"
            return "list<text>"
        if isinstance(value, A.StructLit):
            return value.type_name
        return "text  /* ??? — couldn't infer; please verify */"

    # -- auto-stringify ---------------------------------------------------

    def _infer_expr_type(self, e: A.Expr) -> Optional[str]:
        """Best-effort: what PL/SQL type does expression `e` evaluate to?

        Returns the PL/SQL spelling (e.g. "NUMBER", "VARCHAR2(4000)",
        "JSON", "BOOLEAN", "DATE") or None when we can't tell.
        Combines literal inference, local-type lookup, param-type lookup,
        and call-type inference.
        """
        if isinstance(e, A.NumberLit):
            return "NUMBER"
        if isinstance(e, A.TextLit):
            return "VARCHAR2(4000)"
        if isinstance(e, A.BoolLit):
            return "BOOLEAN"
        if isinstance(e, A.StructLit):
            return _record_type_name(e.type_name)
        if isinstance(e, A.Ident):
            # Structural list check first — if this Ident is known to be
            # a list<T>, return a sentinel that auto-stringify can
            # disambiguate without string parsing. Format:
            #   __PELL_LIST__<elem>           for local t_<elem>_list
            #   __PELL_LIST__<pkg>::<elem>    for foreign <pkg>.t_<elem>_list
            if e.name in self._list_locals:
                elem = self._list_locals[e.name]
                pl_type = self._local_types.get(e.name, "")
                if "." in pl_type and not pl_type.startswith("PLS_"):
                    pkg = pl_type.split(".", 1)[0]
                    return f"{_LIST_SENTINEL}{pkg}::{elem}"
                return f"{_LIST_SENTINEL}{elem}"
            # Check fn parameters first
            if self._current_fn is not None:
                for p in self._current_fn.params:
                    if p.name == e.name:
                        return self._lt(p.type_ref)
            # Check declared locals
            t = self._local_types.get(e.name)
            if t:
                return t
            # Check loop vars — `for row in cursor<dyn>(...)` binds row
            # to a JSON value; record-shaped FETCH binds to a typed
            # row local. The dict stores pell type names.
            pell_t = self._loop_var_types.get(e.name)
            if pell_t:
                return _PELL_TO_PLSQL_TYPE.get(pell_t, pell_t)
            return None
        if isinstance(e, A.Call):
            return self._infer_call_type(e)
        if isinstance(e, A.MemberAccess):
            if isinstance(e.obj, A.Ident) and e.obj.name in self._seq_names:
                return "NUMBER"
            return None
        if isinstance(e, A.BinOp):
            if e.op in ("+", "-", "*", "/", "%"):
                return "NUMBER"
            if e.op in ("==", "!=", "<", "<=", ">", ">=", "&&", "||"):
                return "BOOLEAN"
        if isinstance(e, A.QuestionMark):
            return self._infer_expr_type(e.inner)
        if isinstance(e, A.OkExpr):
            return self._infer_expr_type(e.inner)
        return None

    def _auto_stringify(self, expr_code: str, pl_type: Optional[str]) -> str:
        """Wrap `expr_code` in the appropriate Oracle to-text conversion
        based on `pl_type`. Returns `expr_code` unchanged if it's already
        text-typed or the type is unknown.

        Used by string interpolation (`"{x}"`) and by known text-only
        call targets (`logger::info`, `dbms_output::put_line`) so the user
        can pass any typed expression and it Just Works.
        """
        if pl_type is None:
            return expr_code
        # Structural list-type tag from _infer_expr_type. Format:
        # "__PELL_LIST__<elem>" where <elem> is the pell-surface
        # element name. Checked FIRST so it can't be confused with
        # any PL/SQL type spelling.
        if pl_type.startswith(_LIST_SENTINEL):
            rest = pl_type[len(_LIST_SENTINEL):]
            if "::" in rest:
                pkg, elem = rest.split("::", 1)
            else:
                pkg, elem = None, rest
            self._needs_list_to_text.add((pkg, elem))
            helper = (f"pell_list_to_text_{pkg}_{elem}" if pkg
                      else f"pell_list_to_text_{elem}")
            return f"{helper}({expr_code})"
        t = pl_type.upper()
        if "VARCHAR" in t or "CLOB" in t or "CHAR" in t:
            return expr_code
        if "NUMBER" in t or "PLS_INTEGER" in t or "INTEGER" in t:
            return f"TO_CHAR({expr_code})"
        if t == "BOOLEAN":
            return f"CASE WHEN {expr_code} THEN 'true' ELSE 'false' END"
        if "JSON" in t:
            return f"JSON_SERIALIZE({expr_code})"
        if "TIMESTAMP" in t:
            return f"TO_CHAR({expr_code}, 'YYYY-MM-DD HH24:MI:SS.FF')"
        if "DATE" in t:
            return f"TO_CHAR({expr_code}, 'YYYY-MM-DD HH24:MI:SS')"
        if "RAW" in t:
            return f"RAWTOHEX({expr_code})"
        # Record types spelled as t_<name> — stringify field by field.
        # For v0 just TO_CHAR the whole thing (Oracle errors on non-scalars;
        # records need per-field handling which is a v2 feature).
        return f"TO_CHAR({expr_code})"

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
        # Cross-package call into a fn we have a registered signature
        # for. Returns the foreign collection type for list returns
        # (e.g. "hello.t_text_list") so the local can be declared with
        # the foreign type and the assignment is type-identical — no
        # PLS-00382 nominal clash. For scalar returns, returns the
        # standard lowered type.
        if (isinstance(call.callee, A.Ident)
                and "::" in call.callee.name
                and call.callee.name in self._project_signatures):
            ret = self._project_signatures[call.callee.name]
            # Package is everything except the last segment; pell's `::`
            # lowers to PL/SQL `.` so `pell_test::hello::fn` →
            # `pell_test.hello.fn` and the foreign type lives at
            # `pell_test.hello.t_<elem>_list`.
            segs = call.callee.name.split("::")
            pkg = ".".join(segs[:-1])
            if (isinstance(ret, A.GenericType) and ret.base == "list"
                    and len(ret.params) == 1):
                elem = _render_type(ret.params[0])
                return f"{pkg}.t_{_safe(elem)}_list"
            return self._lt(ret)
        # json::* helpers — type by name (no fallthrough to free-fn lookup).
        if isinstance(call.callee, A.Ident) and call.callee.name.startswith("json::"):
            n = call.callee.name[len("json::"):]
            if n in ("object", "array", "parse", "get_json", "from"):
                return "JSON"
            if n == "get_text" or n == "stringify":
                return "VARCHAR2(4000)"
            if n == "get_number":
                return "NUMBER"
            if n == "has":
                return "BOOLEAN"
        # re::* helpers — type by name. find_all/split return collections
        # and are handled in the let-binding path; here we type only the
        # scalar returners. capture::<T> returns T.
        if isinstance(call.callee, A.Ident) and call.callee.name.startswith("re::"):
            n = call.callee.name[len("re::"):]
            if n == "matches":
                return "BOOLEAN"
            if n in ("find", "replace_all"):
                return "VARCHAR2(4000)"
            if n in ("find_all", "split"):
                return "t_text_list"
            if n == "capture" and call.type_args:
                ta = call.type_args[0]
                if isinstance(ta, A.NamedType):
                    return _record_type_name(ta.name)
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
            if method in ("one", "first", "one_or_none", "scalar"):
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
            if method in ("one", "first", "one_or_none", "scalar") and isinstance(recv, A.Call):
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

    def _emit_re_capture(self, target: str, call: A.Call, indent: str) -> list[str]:
        """`re::capture::<RecordName>(s, pattern)` → one assignment per record
        field, each pulling its value out of pell_re's capture-by-name map.

        Lowering:
            l_pell_caps_N := pell_re.capture_by_name(s, pattern);
            IF l_pell_caps_N.EXISTS('field1') THEN
              target.field1 := l_pell_caps_N('field1').match_text;
            END IF;
            ... (one IF per record field)

        Fields whose names don't match a `(?<name>...)` group in the pattern
        are left at their default (NULL). v0 of capture extracts everything
        as VARCHAR2 — non-text record fields get an implicit TO_NUMBER /
        TO_DATE conversion via assignment, which raises if the captured
        text doesn't parse.
        """
        if len(call.args) != 2:
            raise EmitError(
                "re::capture takes (s, pattern): "
                "re::capture::<RecordName>(s, pat)", call.loc,
            )
        if len(call.type_args) != 1:
            raise EmitError(
                "re::capture needs a single record type argument: "
                "re::capture::<RecordName>(s, pat)", call.loc,
            )
        type_ref = call.type_args[0]
        if not isinstance(type_ref, A.NamedType):
            raise EmitError(
                "re::capture::<T>: T must be a record type", call.loc,
            )
        rec = next((r for r in self._records if r.name == type_ref.name), None)
        if rec is None:
            raise EmitError(
                f"re::capture::<{type_ref.name}>: record {type_ref.name!r} "
                "not declared in this module", call.loc,
            )
        s_expr   = self._emit_expr(call.args[0])
        pat_expr = self._emit_expr(call.args[1])
        tmp = f"l_pell_caps_{self._sql_var_counter}"
        self._sql_var_counter += 1
        self._decl(f"{tmp} pell_re.t_capture_map;")
        out: list[str] = [
            f"{indent}{tmp} := pell_re.capture_by_name({s_expr}, {pat_expr});",
        ]
        for f in rec.fields:
            key = _sql_string(f.name)
            out.extend([
                f"{indent}IF {tmp}.EXISTS({key}) THEN",
                f"{indent}  {target}.{f.name.lower()} := {tmp}({key}).match_text;",
                f"{indent}END IF;",
            ])
        return out

    def _emit_json_into(self, target: str, call: A.Call, indent: str) -> list[str]:
        """`json::into::<RecordName>(j_expr)` → one assignment per record field,
        each pulling its value out of the JSON with the appropriate accessor:

            target.text_field   := JSON_VALUE (j, '$.text_field');
            target.num_field    := JSON_VALUE (j, '$.num_field' RETURNING NUMBER);
            target.nested_field := JSON_QUERY (j, '$.nested_field');

        Missing paths produce NULL per Oracle's JSON_VALUE default.
        """
        if len(call.args) != 1:
            raise EmitError(
                "json::into takes one json value: json::into::<T>(j_expr)",
                call.loc,
            )
        if len(call.type_args) != 1:
            raise EmitError(
                "json::into needs a single record type argument: "
                "json::into::<RecordName>(j)",
                call.loc,
            )
        type_ref = call.type_args[0]
        if not isinstance(type_ref, A.NamedType):
            raise EmitError(
                "json::into::<T>: T must be a record type", call.loc,
            )
        rec = next((r for r in self._records if r.name == type_ref.name), None)
        if rec is None:
            raise EmitError(
                f"json::into::<{type_ref.name}>: record {type_ref.name!r} "
                "not declared in this module",
                call.loc,
            )
        # The json source expression is evaluated potentially many times if
        # the record has many fields. For an Ident that's cheap; for a
        # nested call we hoist it into a temp.
        src_expr = call.args[0]
        if isinstance(src_expr, A.Ident):
            src_code = self._emit_expr(src_expr)
        else:
            # Hoist: declare a temp local, assign once, then reference.
            tmp = f"l_pell_json_src_{self._sql_var_counter}"
            self._sql_var_counter += 1
            self._decl(f"{tmp} JSON;")
            return [
                f"{indent}{tmp} := {self._emit_expr(src_expr)};",
                *self._emit_json_into_assignments(target, tmp, rec, indent),
            ]
        return self._emit_json_into_assignments(target, src_code, rec, indent)

    def _emit_json_into_assignments(self, target: str, src: str,
                                     rec: A.RecordDef, indent: str) -> list[str]:
        out: list[str] = []
        for f in rec.fields:
            accessor = self._json_field_accessor(src, f)
            out.append(f"{indent}{target}.{f.name.lower()} := {accessor};")
        return out

    def _json_field_accessor(self, src: str, f: A.FieldDef) -> str:
        """Pick the right JSON_* function for a field type."""
        t = f.type_ref
        path = _sql_string(f"$.{f.name}")
        if isinstance(t, A.PrimType):
            if t.name == "number" or t.name == "int":
                return f"JSON_VALUE({src}, {path} RETURNING NUMBER)"
            if t.name == "json":
                return f"JSON_QUERY({src}, {path})"
            # text / bool / date / etc. all come back as VARCHAR2-ish via JSON_VALUE
            return f"JSON_VALUE({src}, {path})"
        if isinstance(t, A.NamedType):
            # Record-typed field — pell can't reconstruct a nested record
            # inline from JSON_QUERY (no constructor). v1 limitation:
            # nested records require manual unpacking.
            return f"JSON_QUERY({src}, {path})"
        return f"JSON_VALUE({src}, {path})"

    # The four methods that participate in a JSON dot-chain.
    _JSON_CHAIN_METHODS = {"set", "remove", "append"}

    def _is_json_typed(self, e: A.Expr) -> bool:
        """Best-effort: is `e` a json-typed expression?

        Recognizes json::* constructors, .set/.remove/.append chains on
        json bases, and Idents whose declared type is `json` (parameters
        or annotated locals). Conservative: returns False on ambiguity, in
        which case the .set/.remove/.append call falls through to the
        regular method dispatch.
        """
        if isinstance(e, A.Call):
            if isinstance(e.callee, A.Ident) and e.callee.name in (
                "json::object", "json::array", "json::parse",
                "json::from", "json::get_json",
            ):
                return True
            if (isinstance(e.callee, A.MemberAccess)
                    and e.callee.field in self._JSON_CHAIN_METHODS):
                return self._is_json_typed(e.callee.obj)
        if isinstance(e, A.Ident):
            # fn parameter typed json
            if self._current_fn is not None:
                for p in self._current_fn.params:
                    if (p.name == e.name
                            and isinstance(p.type_ref, A.PrimType)
                            and p.type_ref.name == "json"):
                        return True
            # let-local typed JSON via _local_types
            if self._local_types.get(e.name) == "JSON":
                return True
        return False

    def _collect_json_chain(self, e: A.Call) -> tuple[A.Expr, list[tuple[str, list[A.Expr], A.Loc]]]:
        """Walk a `.set/.remove/.append` chain outward-to-inward; return the
        innermost base expression plus the ops in *source* order."""
        ops: list[tuple[str, list[A.Expr], A.Loc]] = []
        cur: A.Expr = e
        while (isinstance(cur, A.Call)
               and isinstance(cur.callee, A.MemberAccess)
               and cur.callee.field in self._JSON_CHAIN_METHODS):
            ops.append((cur.callee.field, cur.args, cur.loc))
            cur = cur.callee.obj
        ops.reverse()    # restore source order
        return cur, ops

    def _maybe_emit_json_chain(self, target: str, expr: A.Expr,
                               indent: str) -> Optional[list[str]]:
        """If `expr` is a json dot-chain whose base is json-typed, emit
        PL/SQL-native JSON_OBJECT_T mutation calls.

        Uses Oracle's JSON_ELEMENT_T DOM API instead of SQL-only
        JSON_TRANSFORM — no `SELECT ... FROM dual` wrapper needed.
        """
        if not isinstance(expr, A.Call):
            return None
        if not (isinstance(expr.callee, A.MemberAccess)
                and expr.callee.field in self._JSON_CHAIN_METHODS):
            return None
        base, ops = self._collect_json_chain(expr)
        if not self._is_json_typed(base):
            return None
        base_sql = self._emit_expr(base)

        # Declare a temporary JSON_OBJECT_T to perform the mutations in
        # place, then convert back to the target's JSON type.
        tmp = f"l_pell_jobj_{self._sql_var_counter}"
        self._sql_var_counter += 1
        self._decl(f"{tmp} JSON_OBJECT_T;")

        lines = [f"{indent}{tmp} := JSON_OBJECT_T({base_sql});"]

        for method, args, loc in ops:
            if method == "set":
                if len(args) != 2:
                    raise EmitError(
                        f"json .set() takes (path, value), got {len(args)} args",
                        loc,
                    )
                path_text = self._json_chain_path_text(args[0], loc)
                val_sql = self._emit_expr(args[1])
                if "." in path_text and "[" not in path_text:
                    # Nested path: auto-vivify intermediate objects
                    lines.extend(self._emit_json_obj_nested_put(
                        tmp, path_text, val_sql, indent,
                    ))
                else:
                    lines.append(
                        f"{indent}{tmp}.put({_sql_string(path_text)}, {val_sql});"
                    )
            elif method == "remove":
                if len(args) != 1:
                    raise EmitError(
                        f"json .remove() takes (path), got {len(args)} args",
                        loc,
                    )
                path_text = self._json_chain_path_text(args[0], loc)
                lines.append(
                    f"{indent}{tmp}.remove({_sql_string(path_text)});"
                )
            elif method == "append":
                if len(args) != 2:
                    raise EmitError(
                        f"json .append() takes (path, value), got {len(args)} args",
                        loc,
                    )
                path_text = self._json_chain_path_text(args[0], loc)
                val_sql = self._emit_expr(args[1])
                # Get or create the array, then append to it.
                arr_tmp = f"l_pell_jarr_{self._sql_var_counter}"
                self._sql_var_counter += 1
                self._decl(f"{arr_tmp} JSON_ARRAY_T;")
                lines.extend([
                    f"{indent}IF {tmp}.has({_sql_string(path_text)}) THEN",
                    f"{indent}  {arr_tmp} := {tmp}.get_array({_sql_string(path_text)});",
                    f"{indent}ELSE",
                    f"{indent}  {arr_tmp} := JSON_ARRAY_T();",
                    f"{indent}END IF;",
                    f"{indent}{arr_tmp}.append({val_sql});",
                    f"{indent}{tmp}.put({_sql_string(path_text)}, {arr_tmp});",
                ])

        # Convert back to JSON type (23ai) or VARCHAR2 (19c).
        to_json = f"{tmp}.to_json()" if self.target == "23" else f"{tmp}.to_string()"
        lines.append(f"{indent}{target} := {to_json};")
        return lines

    def _emit_json_obj_nested_put(
        self, obj_var: str, path: str, val_sql: str, indent: str,
    ) -> list[str]:
        """Emit auto-vivifying `.put()` calls for a dotted path like
        `"user.address.city"`.

        Oracle's `JSON_OBJECT_T.get_object(key)` returns a value-typed
        copy of the inner object, not a reference. So
        `obj.get_object('a').put('b', v)` mutates a copy that's never
        written back — and Oracle catches that as PLS-00363 "cannot
        be used as an assignment target". The fix: step through the
        path with named temps, mutate the deepest one, then write
        each level back up to its parent in reverse order.

        Example for path "a.b.c" with value v:
            IF NOT obj.has('a') THEN obj.put('a', JSON_OBJECT_T()); END IF;
            l_pell_jobj_1 := obj.get_object('a');
            IF NOT l_pell_jobj_1.has('b') THEN
              l_pell_jobj_1.put('b', JSON_OBJECT_T());
            END IF;
            l_pell_jobj_2 := l_pell_jobj_1.get_object('b');
            l_pell_jobj_2.put('c', v);
            -- write back, deepest first
            l_pell_jobj_1.put('b', l_pell_jobj_2);
            obj.put('a', l_pell_jobj_1);
        """
        parts = path.split(".")
        # Single-segment paths don't need any nav — just put.
        if len(parts) == 1:
            return [f"{indent}{obj_var}.put({_sql_string(parts[0])}, {val_sql});"]

        lines: list[str] = []
        # Walk from the top down, allocating one temp per intermediate
        # level. Track (parent, key, child) for the writeback pass.
        cur_var = obj_var
        write_back: list[tuple[str, str, str]] = []
        for i in range(len(parts) - 1):
            key = _sql_string(parts[i])
            # Ensure the key exists at the current level
            lines.extend([
                f"{indent}IF NOT {cur_var}.has({key}) THEN",
                f"{indent}  {cur_var}.put({key}, JSON_OBJECT_T());",
                f"{indent}END IF;",
            ])
            # Fetch the child into a temp (by value — must write back later)
            tmp = f"l_pell_jobj_{self._sql_var_counter}"
            self._sql_var_counter += 1
            self._decl(f"{tmp} JSON_OBJECT_T;")
            lines.append(f"{indent}{tmp} := {cur_var}.get_object({key});")
            write_back.append((cur_var, key, tmp))
            cur_var = tmp
        # Put the leaf value on the deepest temp
        lines.append(
            f"{indent}{cur_var}.put({_sql_string(parts[-1])}, {val_sql});"
        )
        # Write back deepest-first so each parent gets the freshly-
        # mutated child reflected in its slot.
        for parent_var, key, child_var in reversed(write_back):
            lines.append(f"{indent}{parent_var}.put({key}, {child_var});")
        return lines

    def _json_chain_path_text(self, expr: A.Expr, loc: A.Loc) -> str:
        """Path arg must be a string literal — JSON_TRANSFORM path expressions
        are compile-time SQL strings in 23ai."""
        if isinstance(expr, A.TextLit):
            return expr.value
        raise EmitError(
            "json chain path must be a string literal — Oracle's "
            "JSON_TRANSFORM path is parsed at compile time, not runtime",
            loc,
        )

    def _emit_json_helper(self, call: A.Call) -> Optional[str]:
        """Lower `json::<helper>(...)` calls. Returns the PL/SQL expression
        text, or None when this isn't a recognized helper (caller falls
        through to the default `pkg.fn(args)` lowering).

        `json::from(record_var)` and `json::object(k = v)` use the record /
        kwargs to build a `JSON_OBJECT('k' VALUE v, ...)` literal. Missing
        path access (`json::get_text`, `json::get_number`, `json::get_json`)
        passes through Oracle's NULL-on-missing default.
        """
        name = call.callee.name[len("json::"):]   # after the prefix
        emit = self._emit_expr

        # --- construction ----------------------------------------------
        # On 23+ we ask JSON_OBJECT / JSON_ARRAY to return the native JSON
        # datatype (instead of the default VARCHAR2 text). 19c keeps the
        # default since the JSON datatype doesn't exist there.
        ret_clause = " RETURNING JSON" if self.target == "23" else ""

        if name == "object":
            # `json::object(name = "Alice", age = 30)` → JSON_OBJECT(...)
            if call.args:
                raise EmitError(
                    "json::object takes keyword arguments only — "
                    "use `json::object(field = value, ...)`",
                    call.loc,
                )
            if not call.kwargs:
                return f"JSON_OBJECT({ret_clause.lstrip()})" if ret_clause else "JSON_OBJECT()"
            parts = ", ".join(
                f"{_sql_string(k)} VALUE {emit(v)}"
                for k, v in call.kwargs.items()
            )
            return f"JSON_OBJECT({parts}{ret_clause})"

        if name == "array":
            # `json::array([a, b, c])` → JSON_ARRAY(a, b, c)
            if (len(call.args) != 1
                    or not isinstance(call.args[0], A.ListLit)):
                raise EmitError(
                    "json::array takes a single list literal argument: "
                    "`json::array([a, b, c])`",
                    call.loc,
                )
            els = call.args[0].elements
            if not els:
                return f"JSON_ARRAY({ret_clause.lstrip()})" if ret_clause else "JSON_ARRAY()"
            return f"JSON_ARRAY({', '.join(emit(e) for e in els)}{ret_clause})"

        # --- path access -----------------------------------------------
        if name == "get_text":
            self._check_json_get_args(call, name)
            return f"JSON_VALUE({emit(call.args[0])}, {emit(call.args[1])})"

        if name == "get_number":
            self._check_json_get_args(call, name)
            return (
                f"JSON_VALUE({emit(call.args[0])}, "
                f"{emit(call.args[1])} RETURNING NUMBER)"
            )

        if name == "get_json":
            self._check_json_get_args(call, name)
            return f"JSON_QUERY({emit(call.args[0])}, {emit(call.args[1])})"

        if name == "has":
            self._check_json_get_args(call, name)
            return f"JSON_EXISTS({emit(call.args[0])}, {emit(call.args[1])})"

        # --- parse / stringify -----------------------------------------
        if name == "parse":
            if len(call.args) != 1:
                raise EmitError("json::parse takes exactly 1 text argument", call.loc)
            return f"JSON({emit(call.args[0])})"

        if name == "stringify":
            if len(call.args) != 1:
                raise EmitError("json::stringify takes exactly 1 json argument", call.loc)
            return f"JSON_SERIALIZE({emit(call.args[0])})"

        # --- record → json ---------------------------------------------
        if name == "from":
            if len(call.args) != 1 or not isinstance(call.args[0], A.Ident):
                raise EmitError(
                    "json::from(<record_var>): argument must be a bare "
                    "identifier referencing a typed record local or parameter",
                    call.loc,
                )
            return self._emit_json_from_record(call.args[0], call.loc)

        return None  # unknown helper — let caller fall through

    def _check_json_get_args(self, call: A.Call, name: str) -> None:
        if len(call.args) != 2:
            raise EmitError(
                f"json::{name} takes (json_value, '$.path') — got {len(call.args)} args",
                call.loc,
            )

    def _emit_json_from_record(self, ident: A.Ident, loc: A.Loc) -> str:
        """Resolve the named identifier's record type and emit a
        `JSON_OBJECT(field1 VALUE recv.field1, ...)` literal."""
        # Where does the record type live? Three places to look:
        #   - current fn params (typed as NamedType referring to a record)
        #   - in-scope let locals (we track these in _local_types)
        # For v1 we only handle the fn-param case cleanly; let-locals
        # only carry the lowered PL/SQL type string, not the pell type,
        # so we'd need to thread record info through there. Punt with a
        # clear error if we can't resolve.
        rec_name: Optional[str] = None
        if self._current_fn is not None:
            for p in self._current_fn.params:
                if p.name == ident.name and isinstance(p.type_ref, A.NamedType):
                    rec_name = p.type_ref.name
                    break
        if rec_name is None:
            # Try the local-types map — it has PL/SQL type strings of the
            # form `t_<rec_name>`. Reverse-engineer the pell name.
            pl_type = self._local_types.get(ident.name)
            if pl_type is not None and pl_type.startswith("t_"):
                candidate = pl_type[2:]
                for rec in self._records:
                    if rec.name.lower() == candidate:
                        rec_name = rec.name
                        break
        if rec_name is None:
            raise EmitError(
                f"json::from({ident.name}): can't resolve a record type for "
                f"`{ident.name}`. Annotate the local with `let {ident.name}: "
                "<RecordName> = …` so pell knows the shape.",
                loc,
            )
        rec = next((r for r in self._records if r.name == rec_name), None)
        if rec is None:
            raise EmitError(
                f"json::from({ident.name}): record type {rec_name!r} not "
                "declared in this module",
                loc,
            )
        recv = self._lower_ident(ident.name)
        parts = ", ".join(
            f"{_sql_string(f.name)} VALUE {recv}.{f.name.lower()}"
            for f in rec.fields
        )
        ret_clause = " RETURNING JSON" if self.target == "23" else ""
        return f"JSON_OBJECT({parts}{ret_clause})"

    def _emit_re_helper(self, call: A.Call) -> Optional[str]:
        """Lower `re::<helper>(...)` calls to the pell_re runtime package.

        Scalar returners (matches/find/replace_all) pass through directly.
        Collection returners (find_all/split) route through a per-package
        adapter that copies the runtime's t_text_list into the package's
        own t_text_list — PL/SQL collections are nominally typed and
        assigning between two structurally-identical TABLE types is a
        PLS-00382, so the adapter is unavoidable.
        """
        name = call.callee.name[len("re::"):]
        emit = self._emit_expr

        if name == "matches":
            if len(call.args) != 2:
                raise EmitError(
                    "re::matches takes (s, pattern)", call.loc,
                )
            return f"pell_re.matches({emit(call.args[0])}, {emit(call.args[1])})"

        if name == "find":
            if len(call.args) not in (2, 3):
                raise EmitError(
                    "re::find takes (s, pattern [, start_pos])", call.loc,
                )
            args = ", ".join(emit(a) for a in call.args)
            return f"pell_re.find({args})"

        if name == "replace_all":
            if len(call.args) != 3:
                raise EmitError(
                    "re::replace_all takes (s, pattern, replacement)", call.loc,
                )
            args = ", ".join(emit(a) for a in call.args)
            return f"pell_re.replace_all({args})"

        if name in ("find_all", "split"):
            if len(call.args) != 2:
                raise EmitError(
                    f"re::{name} takes (s, pattern)", call.loc,
                )
            # Make sure the package-local t_text_list type exists by
            # pre-registering it through the same list-type machinery the
            # rest of the emitter uses for `list<text>`.
            text_type = A.PrimType(loc=call.loc, name="text")
            list_type = f"t_{_safe(_render_type(text_type))}_list"
            if list_type not in self._list_types_emitted:
                self._list_type_decls.append(
                    f"  TYPE {list_type} IS TABLE OF VARCHAR2(4000) INDEX BY PLS_INTEGER;"
                )
                self._list_types_emitted.add(list_type)
            # Mark we need the adapter; one per pattern-shape (find_all vs split)
            self._needs_re_adapter.add(name)
            args = ", ".join(emit(a) for a in call.args)
            return f"pell_re_{name}({args})"

        return None

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

    def _lower_jq_block(self, jq: A.JqBlock, elem_type: Optional[A.TypeRef]) -> A.SqlBlock:
        """Lower a `jq!{ ... }` macro to a SqlBlock.

        Two modes:

        1. **Array iteration** — path contains `[*]`. Lowers to a
           JSON_TABLE-backed SELECT (existing behavior). Used with
           `.collect()` / `.one()` / for-loops.

        2. **Scalar field access** — path has NO `[*]`. Lowers to a
           simple `SELECT JSON_VALUE(:src, 'path') FROM dual`. Used
           for `jq!{ j | .name }.one()` or `jq!{ j | .address.city }.one()`.
           This is the jq equivalent of `.name` on an object.

        The COLUMNS clause (mode 1) or RETURNING clause (mode 2) is
        shaped by `elem_type`, which the surrounding let-binding pushes
        onto `_pending_jq_elem_type`.
        """
        if elem_type is None:
            raise EmitError(
                "jq!{} requires a typed target — annotate the surrounding `let` "
                "with the element type (e.g. `let xs: list<text> = jq!{ ... }.collect();`).",
                jq.loc,
            )

        # -----------------------------------------------------------------
        # Mode 2: scalar field access (no [*] in path)
        # -----------------------------------------------------------------
        if "[*]" not in jq.path:
            return self._lower_jq_scalar(jq, elem_type)

        # Map each filter field to a column-alias inside JSON_TABLE so the
        # WHERE clause can compare against `jt.<alias>` instead of nested
        # JSON_VALUE calls. Filters on a field that's also projected reuse
        # that column.
        filter_cols: dict[str, tuple[str, str]] = {}  # path -> (alias, sql_type)
        if jq.projection is not None:
            elem_sql = self._lt(elem_type)
            col_parts = [f"v {elem_sql} PATH '$.{jq.projection}'"]
            select_list = "jt.v"
            # Add filter columns
            for f in jq.filters:
                if f.field_path in filter_cols:
                    continue
                if f.field_path == "$." + jq.projection:
                    filter_cols[f.field_path] = ("v", elem_sql)
                    continue
                alias = f"f{len(filter_cols)}"
                sql_type = _jq_literal_pl_type(f.literal)
                filter_cols[f.field_path] = (alias, sql_type)
                col_parts.append(f"{alias} {sql_type} PATH '{f.field_path}'")
            cols_sql = ", ".join(col_parts)
        else:
            rec = None
            if isinstance(elem_type, A.NamedType):
                rec = self._lookup_record(elem_type.name)
            if rec is None:
                raise EmitError(
                    "jq!{} without a trailing projection requires a record-typed "
                    f"target; got {_render_type(elem_type)}. Either add `| .field` "
                    "to project a scalar, or annotate the target with a `pub record`.",
                    jq.loc,
                )
            col_parts = []
            sel_parts: list[str] = []
            for f in rec.fields:
                col_sql = self._lt(f.type_ref)
                col_parts.append(f"{f.name} {col_sql} PATH '$.{f.name}'")
                sel_parts.append(f"jt.{f.name}")
                filter_cols["$." + f.name] = (f.name, col_sql)
            for f in jq.filters:
                if f.field_path in filter_cols:
                    continue
                alias = f"f{len(filter_cols) - len(rec.fields)}"
                sql_type = _jq_literal_pl_type(f.literal)
                filter_cols[f.field_path] = (alias, sql_type)
                col_parts.append(f"{alias} {sql_type} PATH '{f.field_path}'")
            cols_sql = ", ".join(col_parts)
            select_list = ", ".join(sel_parts)

        where_sql = self._jq_filters_to_where(jq.filters, filter_cols)
        sql_lines = [
            f"SELECT {select_list}",
            f"  FROM JSON_TABLE(:{jq.source}, '{jq.path}'",
            f"    COLUMNS ({cols_sql})) jt",
        ]
        if where_sql:
            sql_lines.append(f"  WHERE {where_sql}")
        return A.SqlBlock(
            loc=jq.loc,
            sql="\n".join(sql_lines),
            binds=[jq.source],
            is_dml=False,
            has_returning=False,
        )

    def _lower_jq_scalar(self, jq: A.JqBlock, elem_type: A.TypeRef) -> A.SqlBlock:
        """Lower `jq!{ src | .field.sub }` (no `[*]`) to a simple
        `SELECT JSON_VALUE(:src, '$.field.sub' [RETURNING <type>]) FROM dual`.

        This is the jq equivalent of `.field` on an object — no array
        iteration, just path access.
        """
        elem_sql = self._lt(elem_type).upper()
        # Build the RETURNING clause for non-text types so Oracle
        # delivers the value in the target type directly.
        if "NUMBER" in elem_sql or "PLS_INTEGER" in elem_sql:
            returning = " RETURNING NUMBER"
        elif "JSON" in elem_sql:
            # Use JSON_QUERY for sub-document extraction
            sql = f"SELECT JSON_QUERY(:{jq.source}, '{jq.path}') FROM dual"
            return A.SqlBlock(loc=jq.loc, sql=sql, binds=[jq.source],
                              is_dml=False, has_returning=False)
        elif "DATE" in elem_sql and "TIMESTAMP" not in elem_sql:
            returning = " RETURNING DATE"
        elif "TIMESTAMP" in elem_sql:
            returning = " RETURNING TIMESTAMP"
        elif "BOOLEAN" in elem_sql:
            returning = ""  # JSON_VALUE doesn't support RETURNING BOOLEAN; get as text
        else:
            returning = ""  # VARCHAR2 is the default

        sql = f"SELECT JSON_VALUE(:{jq.source}, '{jq.path}'{returning}) FROM dual"
        return A.SqlBlock(loc=jq.loc, sql=sql, binds=[jq.source],
                          is_dml=False, has_returning=False)

    def _jq_filters_to_where(
        self,
        filters: list[A.JqFilter],
        filter_cols: dict[str, tuple[str, str]],
    ) -> str:
        """Render a `select(.a > 1 and .b == "x")` filter list as the WHERE
        clause for the JSON_TABLE-backed SELECT. Each `.field` reference
        resolves to the column alias provisioned in JSON_TABLE COLUMNS
        (always populated by the caller). Comparison operators map verbatim
        except `==`/`!=` which become `=`/`<>`."""
        if not filters:
            return ""
        op_map = {"==": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
        parts: list[str] = []
        for i, f in enumerate(filters):
            if i > 0:
                parts.append(f.join.upper() if f.join else "AND")
            alias, _ = filter_cols[f.field_path]
            lhs = f"jt.{alias}"
            op = op_map.get(f.op, f.op)
            if isinstance(f.literal, bool):
                rhs = "1" if f.literal else "0"
            elif isinstance(f.literal, (int, float)):
                rhs = str(f.literal)
            elif f.literal is None:
                rhs = "NULL"
            else:
                rhs = _sql_string(str(f.literal))
            parts.append(f"{lhs} {op} {rhs}")
        return " ".join(parts)

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
        # jq!{ ... } — lower to a JSON_TABLE-backed SqlBlock once we know the
        # target element type (pushed by the surrounding let/.collect()/.one()
        # so the COLUMNS clause and SELECT list can be shape-correct).
        if isinstance(cur, A.JqBlock):
            cur = self._lower_jq_block(cur, self._pending_jq_elem_type)
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

    def _lift_listlit_args(
        self, call: A.Call, indent: str,
    ) -> tuple[list[str], A.Call]:
        """Lift any ListLit argument to a pre-declared local with literal
        assigns. PL/SQL has no list-literal expression syntax; pell's
        `foo([1, 2, 3])` would otherwise fall through to the emitter's
        \"unhandled\" TODO. We synthesize:

            l_pell_arg_N t_number_list;
            ...
            l_pell_arg_N(1) := 1;
            l_pell_arg_N(2) := 2;
            l_pell_arg_N(3) := 3;
            target := foo(l_pell_arg_N);

        Returns (pre_statement_lines, call_with_ListLits_replaced_by_idents).
        When no ListLit args are present, returns ([], call_unchanged) so
        the caller can use this unconditionally.

        Element type is inferred from the literal values (NumberLit ->
        number, TextLit -> text, BoolLit -> bool); mixed or empty lists
        default to text.
        """
        def _liftable_struct(a: A.Expr) -> bool:
            return (isinstance(a, A.StructLit)
                    and a.type_name not in self._type_names
                    and self._resolve_struct_lit_record(a) is not None)

        if not any(isinstance(a, A.ListLit) or _liftable_struct(a)
                   for a in call.args):
            return [], call
        # Intrinsic callees (json::array, sql!{}, jq! macros, etc.)
        # *want* the raw ListLit — they have specialized handlers that
        # inspect the literal elements. Leave their args alone.
        if isinstance(call.callee, A.Ident) and self._is_intrinsic_call(
            call.callee.name,
        ):
            return [], call
        pre_lines: list[str] = []
        new_args: list[A.Expr] = []
        for arg in call.args:
            # Record struct-lit arg: PL/SQL records have no inline
            # constructor, so lift to a temp local of the record type,
            # populate field-by-field, and pass the temp.
            if _liftable_struct(arg):
                assert isinstance(arg, A.StructLit)
                rec = self._resolve_struct_lit_record(arg)
                assert rec is not None
                _, owning_pkg = self._lookup_record_with_pkg(arg.type_name)
                rec_type = (
                    f"{owning_pkg}.{_record_type_name(rec.name)}"
                    if owning_pkg is not None
                    else _record_type_name(rec.name)
                )
                self._sql_var_counter += 1
                tmp_name = f"pell_arg_{self._sql_var_counter}"
                tmp = local_name(tmp_name)
                self._decl(f"{tmp} {rec_type};")
                provided = {f.name: f.value for f in arg.fields}
                for fld in rec.fields:
                    if fld.name in provided:
                        pre_lines += self._emit_record_field_assign(
                            f"{tmp}.{fld.name.lower()}", provided[fld.name], indent,
                        )
                new_args.append(A.Ident(loc=arg.loc, name=tmp_name))
                continue
            if not isinstance(arg, A.ListLit):
                new_args.append(arg)
                continue
            elem_pell = self._infer_listlit_elem_type(arg)
            self._sql_var_counter += 1
            tmp_name = f"pell_arg_{self._sql_var_counter}"
            elem_sql = self._lt(A.PrimType(loc=arg.loc, name=elem_pell))
            list_type = f"t_{elem_pell}_list"
            if list_type not in self._list_types_emitted:
                self._list_type_decls.append(
                    f"  TYPE {list_type} IS TABLE OF {elem_sql} "
                    f"INDEX BY PLS_INTEGER;"
                )
                self._list_types_emitted.add(list_type)
            local_var = local_name(tmp_name)
            self._decl(f"{local_var} {list_type};")
            for i, el in enumerate(arg.elements, start=1):
                pre_lines.append(
                    f"{indent}{local_var}({i}) := {self._emit_expr(el)};"
                )
            new_args.append(A.Ident(loc=arg.loc, name=tmp_name))
        new_call = A.Call(
            loc=call.loc,
            callee=call.callee,
            args=new_args,
            type_args=list(call.type_args),
            kwargs=dict(call.kwargs),
        )
        return pre_lines, new_call

    @staticmethod
    def _infer_listlit_elem_type(lit: A.ListLit) -> str:
        if not lit.elements:
            return "text"
        first = lit.elements[0]
        if isinstance(first, A.NumberLit):
            return "number"
        if isinstance(first, A.TextLit):
            return "text"
        if isinstance(first, A.BoolLit):
            return "bool"
        return "text"

    def _emit_call_assignment(self, target: str, call: A.Call, indent: str) -> list[str]:
        # `.collect()` on a SELECT → BULK COLLECT INTO
        if isinstance(call.callee, A.MemberAccess) and call.callee.field == "collect":
            recv = call.callee.obj
            _, sql = self._strip_lock_modifiers(recv)
            if sql is not None and not sql.is_dml:
                return self._emit_bulk_collect_into(target, sql, indent)
        # .one() with no ? — same select-into but on NO_DATA_FOUND we raise a generic invariant? For v0, leave it as raise.
        # .scalar() — single-row, single-column SELECT INTO; the receiver SELECT
        # already projects exactly one column, so the same INTO splice as .one()
        # is correct (target is a scalar local, not a record).
        if isinstance(call.callee, A.MemberAccess) and call.callee.field in ("one", "first", "scalar"):
            recv = call.callee.obj
            _, sql_with_locks = self._strip_lock_modifiers(recv)
            if sql_with_locks is not None:
                if call.callee.field in ("one", "scalar"):
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
        pre_lines, call = self._lift_listlit_args(call, indent)
        return pre_lines + [f"{indent}{target} := {self._emit_expr(call)};"]

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
            inner = s.value.inner
            # `return Ok(Record { ... })` — same record-temp dance as below.
            if isinstance(inner, A.StructLit) and inner.type_name not in self._type_names:
                temp = self._record_return_temp(inner)
                if temp is not None:
                    temp_name, decl_type, assigns = temp
                    self._decl(f"{temp_name} {decl_type};")
                    return prefix + assigns + [f"{indent}RETURN {temp_name};"]
            return prefix + [f"{indent}RETURN {self._emit_expr(inner)};"]
        # `return Record { ... }` — RECORDs have no PL/SQL constructor, so
        # declare a temp local, field-by-field assign, and return the temp.
        if isinstance(s.value, A.StructLit) and s.value.type_name not in self._type_names:
            temp = self._record_return_temp(s.value)
            if temp is not None:
                temp_name, decl_type, assigns = temp
                self._decl(f"{temp_name} {decl_type};")
                return prefix + assigns + [f"{indent}RETURN {temp_name};"]
        # `return foreign_pkg::fn();` where the fn returns `list<T>` —
        # the foreign return type is `<pkg>.t_<T>_list`, which is
        # nominally a *different* VARRAY type from our local
        # `t_<T>_list` even though both are `VARRAY OF <elem>`. PL/SQL
        # rejects the direct return with PLS-00382. Adapt by declaring
        # a transit local of the foreign type and an element-copy loop
        # into a local-typed return temp.
        if (self._current_fn is not None
                and isinstance(self._current_fn.return_type, A.GenericType)
                and self._current_fn.return_type.base == "list"
                and isinstance(s.value, A.Call)
                and isinstance(s.value.callee, A.Ident)
                and "::" in s.value.callee.name
                and s.value.callee.name in self._project_signatures):
            sig = self._project_signatures[s.value.callee.name]
            if (isinstance(sig, A.GenericType)
                    and sig.base == "list"
                    and len(sig.params) == 1
                    and len(self._current_fn.return_type.params) == 1
                    and _render_type(sig.params[0])
                        == _render_type(self._current_fn.return_type.params[0])):
                elem_t = self._current_fn.return_type.params[0]
                elem_sql = self._lt(elem_t)
                elem_name = _render_type(elem_t)
                list_type = f"t_{_safe(elem_name)}_list"
                if list_type not in self._list_types_emitted:
                    self._list_type_decls.append(
                        f"  TYPE {list_type} IS TABLE OF {elem_sql} "
                        f"INDEX BY PLS_INTEGER;"
                    )
                    self._list_types_emitted.add(list_type)
                self._sql_var_counter += 1
                ret_local = f"l_pell_ret_{self._sql_var_counter}"
                self._decl(f"{ret_local} {list_type};")
                adapter = self._emit_cross_pkg_list_adapter(
                    ret_local, s.value, list_type, indent,
                )
                return prefix + adapter + [f"{indent}RETURN {ret_local};"]
        # `return sql!{...}.collect()` / `.one()` / `.first()` / `.scalar()` —
        # these patterns need a SELECT INTO / BULK COLLECT INTO temp; you
        # can't put them inline as expressions. Synthesize:
        #   let _ret_<n>: <fn_return_type> = <expr>;
        #   RETURN _ret_<n>;
        # and let the existing let-handling do the right thing.
        if (isinstance(s.value, A.Call)
                and isinstance(s.value.callee, A.MemberAccess)
                and isinstance(s.value.callee.obj, A.SqlBlock)
                and s.value.callee.field in ("collect", "one", "first", "scalar")
                and self._current_fn is not None
                and self._current_fn.return_type is not None):
            self._sql_var_counter += 1
            tmp_name = f"pell_ret_{self._sql_var_counter}"
            let_stmt = A.LetStmt(
                loc=s.loc,
                name=tmp_name,
                type_annot=self._current_fn.return_type,
                value=s.value,
            )
            let_lines = self._emit_let(let_stmt, indent)
            return prefix + let_lines + [f"{indent}RETURN {local_name(tmp_name)};"]
        # `return foo([1,2,3]);` — lift ListLit args to pre-declared
        # locals before emitting the call.
        if isinstance(s.value, A.Call):
            pre, call = self._lift_listlit_args(s.value, indent)
            if pre:
                return prefix + pre + [
                    f"{indent}RETURN {self._emit_expr(call)};"
                ]
        return prefix + [f"{indent}RETURN {self._emit_expr(s.value)};"]

    def _record_return_temp(self, sl: A.StructLit) -> Optional[tuple[str, str, list[str]]]:
        """Build (temp_name, decl_type, [assign_lines]) for a record StructLit
        used in a return position. Returns None if `sl.type_name` is not a
        known record (caller falls back to the default emit path).

        Routes through _resolve_struct_lit_record so the import check
        fires for `return pkg::Record { ... }` when pkg isn't imported.
        """
        rec = self._resolve_struct_lit_record(sl)
        if rec is None:
            return None
        provided = {f.name: f.value for f in sl.fields}
        self._sql_var_counter += 1
        temp_name = f"l_ret_{self._sql_var_counter}"
        # Qualify the PL/SQL record type when it's foreign so the
        # cross-package record path uses `<pkg>.t_<record>`.
        _, owning_pkg = self._lookup_record_with_pkg(sl.type_name)
        if owning_pkg is not None:
            decl_type = f"{owning_pkg}.{_record_type_name(rec.name)}"
        else:
            decl_type = _record_type_name(sl.type_name)
        lines: list[str] = []
        for f in rec.fields:
            if f.name in provided:
                lines += self._emit_record_field_assign(
                    f"{temp_name}.{f.name.lower()}", provided[f.name], "    ",
                )
        return temp_name, decl_type, lines

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
        # for k in json::get_keys(j): walk every key of a JSON object via
        # JSON_OBJECT_T.get_keys(). The loop var is `text`. Lets the user
        # discover the shape of a cursor<dyn> row at runtime.
        if (isinstance(s.iterable, A.Call)
                and isinstance(s.iterable.callee, A.Ident)
                and s.iterable.callee.name == "json::get_keys"):
            return self._emit_json_keys_iteration(s, indent)
        # for x in [a, b, c]: lift the list literal to a temp list local,
        # then iterate it through the list-variable path below. PL/SQL
        # can't iterate a literal directly, but a temp + the existing
        # assoc-array loop handles it transparently.
        if isinstance(s.iterable, A.ListLit):
            elem_pell = self._infer_listlit_elem_type(s.iterable)
            self._sql_var_counter += 1
            tmp_name = f"pell_iter_{self._sql_var_counter}"
            let_stmt = A.LetStmt(
                loc=s.loc, name=tmp_name,
                type_annot=A.GenericType(
                    loc=s.loc, base="list",
                    params=[A.PrimType(loc=s.loc, name=elem_pell)],
                ),
                value=s.iterable,
            )
            new_for = A.ForStmt(
                loc=s.loc, var_name=s.var_name,
                iterable=A.Ident(loc=s.loc, name=tmp_name),
                body=s.body,
            )
            return self._emit_let(let_stmt, indent) + self._emit_for(new_for, indent)
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
        # for x in <call>() — if the call returns list<T>, synthesize
        # a temp let and iterate that. Sugar for:
        #     let _tmp = <call>(); for x in _tmp { ... }
        # Works for any cross-package call in the project signature
        # registry, plus same-module fns. (No-arg or with-arg, doesn't
        # matter — we just unpack the return type.)
        if isinstance(s.iterable, A.Call) and isinstance(s.iterable.callee, A.Ident):
            fn_name = s.iterable.callee.name
            call_ret: Optional[A.TypeRef] = None
            if "::" in fn_name and fn_name in self._project_signatures:
                call_ret = self._project_signatures[fn_name]
            else:
                for fn in self._fns:
                    if fn.name == fn_name:
                        call_ret = fn.return_type
                        break
            if (call_ret is not None
                    and isinstance(call_ret, A.GenericType)
                    and call_ret.base == "list"
                    and len(call_ret.params) == 1):
                self._sql_var_counter += 1
                tmp_name = f"pell_iter_{self._sql_var_counter}"
                let_stmt = A.LetStmt(
                    loc=s.loc, name=tmp_name,
                    type_annot=call_ret, value=s.iterable,
                )
                new_for = A.ForStmt(
                    loc=s.loc, var_name=s.var_name,
                    iterable=A.Ident(loc=s.loc, name=tmp_name),
                    body=s.body,
                )
                return self._emit_let(let_stmt, indent) + self._emit_for(new_for, indent)
            # `for x in <call>()` where the call returns `cursor<T>` —
            # OPEN the cursor, fetch row-by-row into a typed local,
            # CLOSE on exit. Wrapped in an inner DECLARE so the cursor
            # and row temp are scoped to the loop. Works for
            # `cursor<text>`, `cursor<number>`, etc. — element type
            # comes from the registered return signature.
            if (call_ret is not None
                    and isinstance(call_ret, A.GenericType)
                    and call_ret.base == "cursor"
                    and len(call_ret.params) == 1):
                return self._emit_cursor_call_iteration(
                    s, call_ret.params[0], indent,
                )
        # for x in <list-typed local>: iterate via assoc-array FOR loop
        if isinstance(s.iterable, A.Ident) and s.iterable.name in self._list_locals:
            list_local = local_name(s.iterable.name)
            elem_pell = self._list_locals[s.iterable.name]  # pell-surface, e.g. "text" or "ColumnInfo"
            # Distinguish primitive vs record element types. Records
            # lower to t_<name>; primitives use their pell name. If the
            # list local is foreign-typed (e.g. catalog.t_columninfo_list),
            # the record element also lives in the foreign pkg.
            _prims = {"text", "number", "int", "bool", "date", "timestamp",
                      "bytes", "json", "bigtext", "Unit"}
            if elem_pell in _prims:
                elem_sql = self._lt(A.PrimType(loc=s.iterable.loc, name=elem_pell))
            else:
                # Record element. Check if the list itself is foreign-typed.
                list_pl_type = self._local_types.get(s.iterable.name, "")
                if "." in list_pl_type:
                    pkg = list_pl_type.split(".", 1)[0]
                    elem_sql = f"{pkg}.t_{_safe(elem_pell.lower())}"
                else:
                    elem_sql = self._lt(A.NamedType(loc=s.iterable.loc, name=elem_pell))
            # Walk via FIRST/NEXT so empty arrays (FIRST returns NULL)
            # and sparse arrays both work. The naive
            # `FOR i IN list.FIRST .. list.LAST LOOP` raises VALUE_ERROR
            # on an empty list because Oracle won't accept NULL bounds.
            idx = f"i_{s.var_name}"
            self._decl(f"{idx} PLS_INTEGER;")
            # Per-iteration element local, populated each loop pass.
            shadow = local_name(s.var_name) + "_iter"
            self._decl(f"{shadow} {elem_sql};")
            out = [
                f"{indent}{idx} := {list_local}.FIRST;",
                f"{indent}WHILE {idx} IS NOT NULL LOOP",
                f"{indent}  {shadow} := {list_local}({idx});",
            ]
            self._loop_vars.append({s.var_name})
            prev_override = self._loop_var_override.get(s.var_name)
            self._loop_var_override[s.var_name] = shadow
            prev_type = self._loop_var_types.get(s.var_name)
            self._loop_var_types[s.var_name] = elem_pell
            for stmt in s.body:
                out.extend(self._emit_stmt(stmt, indent + "  "))
            self._loop_vars.pop()
            if prev_override is None:
                del self._loop_var_override[s.var_name]
            else:
                self._loop_var_override[s.var_name] = prev_override
            if prev_type is None:
                self._loop_var_types.pop(s.var_name, None)
            else:
                self._loop_var_types[s.var_name] = prev_type
            out.append(f"{indent}  {idx} := {list_local}.NEXT({idx});")
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
        # Common typo: `for x in catalog.list_types()` (dot) instead of
        # `for x in catalog::list_types()` (colon-colon). If the receiver
        # isn't a known local but `<pkg>::<fn>` IS in the project
        # signature registry, point that out specifically.
        suggestion = self._suggest_cross_pkg_call(s.iterable)
        msg = (
            f"unsupported `for` iterable: {type(s.iterable).__name__}. "
            f"Use `for i in 1..=10`, `for row in sql!{{...}}`, or "
            f"`for x in <list-typed-variable>`."
        )
        code = ""
        if suggestion is not None:
            msg = (
                f"`{suggestion[0]}` looks like a cross-package call written "
                f"with `.` instead of `::`. did you mean `{suggestion[1]}`?"
            )
            code = "pell.dot-vs-colon"
        raise EmitError(msg, s.loc, code=code)

    # The set of pell-surface primitive type names. Field access on
    # any of these is a user error (text has no `.name`, number has no
    # `.x`, etc.) — except for `.length` and other text/list method
    # aliases that go through `_emit_call_expr`, not here.
    _PRIM_TYPE_NAMES = frozenset({
        "text", "number", "int", "bool", "date", "timestamp",
        "bytes", "json", "bigtext", "Unit",
    })

    def _check_field_on_primitive(self, e: "A.MemberAccess") -> None:
        """Raise a clear EmitError when the user reads `.field` on a
        primitive-typed receiver — e.g. `i.name` inside
        `for i in catalog::list_types() { ... }` where `i` is `text`.

        Only fires when we can confidently identify the receiver's type
        as a primitive — silent on unknown types so we don't false-
        positive on call chains we don't fully understand. Method calls
        (`.substr()`, `.contains()`, `.year()`) flow through
        `_emit_call_expr`, not here, so they aren't affected.
        """
        if not isinstance(e.obj, A.Ident):
            return
        name = e.obj.name
        # Loop variable in scope?
        elem_type = self._loop_var_types.get(name)
        if elem_type is None:
            # Maybe a plain let-local whose type we know is primitive.
            pl_type = self._local_types.get(name, "").upper()
            # Map PL/SQL type back to a pell surface name we recognize.
            if "VARCHAR" in pl_type or "CLOB" in pl_type or "CHAR" in pl_type:
                elem_type = "text"
            elif "NUMBER" in pl_type or "PLS_INTEGER" in pl_type or "INTEGER" in pl_type:
                elem_type = "number"
            elif pl_type == "BOOLEAN":
                elem_type = "bool"
            elif "JSON" in pl_type:
                # json IS a primitive in pell terms, but `.set/.remove`
                # method-chain dispatch lives in _emit_call_expr, not
                # here, so silent — and `j.some_field` on a json local
                # is genuinely ambiguous (could mean JSON_VALUE access).
                return
            elif "TIMESTAMP" in pl_type:
                elem_type = "timestamp"
            elif "DATE" in pl_type:
                elem_type = "date"
            elif "RAW" in pl_type:
                elem_type = "bytes"
            else:
                return
        if elem_type not in self._PRIM_TYPE_NAMES:
            return
        # Primitive! `.field` is invalid.
        hint = (
            f"`{name}` is `{elem_type}`, not a record. "
            f"`{elem_type}` has no `.{e.field}` field — "
        )
        if elem_type == "text":
            hint += (
                f"if you wanted the string itself, just use `{name}` "
                f"(it's already text)."
            )
        else:
            hint += (
                f"check the iterable's element type — if the call "
                f"returns `list<{elem_type}>`, each element is a "
                f"`{elem_type}`, not a record."
            )
        raise EmitError(hint, e.loc)

    def _suggest_cross_pkg_call(self, expr: "A.Expr") -> Optional[tuple[str, str]]:
        """If `expr` looks like a misspelled cross-package call (`pkg.fn(...)`
        where the receiver isn't a known local but `pkg::fn` is in the
        project signature registry), return a (wrong, right) pair for
        the suggestion. Otherwise None.

        Catches the common `.` vs `::` typo without lying about other
        member accesses on real records.
        """
        if not isinstance(expr, A.Call):
            return None
        callee = expr.callee
        if not isinstance(callee, A.MemberAccess):
            return None
        recv = callee.obj
        if not isinstance(recv, A.Ident):
            return None
        # The receiver is an Ident — is it actually a known local /
        # param / record / etc. in this scope? If yes, the call is a
        # legitimate method dispatch (or user typo we can't help with).
        if recv.name in self._local_types or recv.name in self._params:
            return None
        if recv.name in self._list_locals:
            return None
        # Is `<recv>::<field>` in the project signature registry?
        candidate = f"{recv.name}::{callee.field}"
        if candidate not in self._project_signatures:
            return None
        # Rebuild a printable form of the original and the fix.
        arg_text = ", ".join(self._emit_expr(a) for a in expr.args) if expr.args else ""
        wrong = f"{recv.name}.{callee.field}({arg_text})"
        right = f"{recv.name}::{callee.field}({arg_text})"
        return wrong, right

    def _emit_cursor_call_iteration(
        self, s: A.ForStmt, elem_type: A.TypeRef, indent: str,
    ) -> list[str]:
        """Lower `for row in cursor_returning_call() { ... }` into an
        OPEN / FETCH-loop / CLOSE block.

        Two flavors:

        * `cursor<dyn>` (the receiver's element is a NamedType "dyn")
          → DBMS_SQL.TO_CURSOR_NUMBER + DESCRIBE_COLUMNS +
          per-row formatted text. Handles any row shape — variable
          column count, mixed types — at the cost of a CHAR
          representation. Each row's `row_var` is built as
          `col1=val1 | col2=val2 | ...`. Use for dynamic pivots and
          anything where the shape isn't known at compile time.

        * Anything else (cursor<text>, cursor<number>, cursor<Record>,
          ...) → typed single-column FETCH INTO. Cheaper, but the
          declared element type must match the actual row shape or
          Oracle throws ORA-00932.
        """
        # Dynamic-shape path — uses DBMS_SQL to introspect the cursor.
        if (isinstance(elem_type, A.NamedType) and elem_type.name == "dyn"):
            return self._emit_dyn_cursor_iteration(s, indent)

        self._sql_var_counter += 1
        cur_var = f"l_pell_cur_{self._sql_var_counter}"
        row_var = f"l_pell_row_{self._sql_var_counter}"
        self._decl(f"{cur_var} SYS_REFCURSOR;")
        elem_sql = self._lt(elem_type)
        self._decl(f"{row_var} {elem_sql};")

        call_code = self._emit_expr(s.iterable)
        out = [
            f"{indent}{cur_var} := {call_code};",
            f"{indent}LOOP",
            f"{indent}  FETCH {cur_var} INTO {row_var};",
            f"{indent}  EXIT WHEN {cur_var}%NOTFOUND;",
        ]
        # Inside the body, `s.var_name` should refer to row_var. Use the
        # same shadow-override pattern as list iteration.
        self._loop_vars.append({s.var_name})
        prev_override = self._loop_var_override.get(s.var_name)
        self._loop_var_override[s.var_name] = row_var
        prev_type = self._loop_var_types.get(s.var_name)
        if isinstance(elem_type, A.PrimType):
            self._loop_var_types[s.var_name] = elem_type.name
        elif isinstance(elem_type, A.NamedType):
            self._loop_var_types[s.var_name] = elem_type.name
        for stmt in s.body:
            out.extend(self._emit_stmt(stmt, indent + "  "))
        self._loop_vars.pop()
        if prev_override is None:
            del self._loop_var_override[s.var_name]
        else:
            self._loop_var_override[s.var_name] = prev_override
        if prev_type is None:
            self._loop_var_types.pop(s.var_name, None)
        else:
            self._loop_var_types[s.var_name] = prev_type

        out.append(f"{indent}END LOOP;")
        out.append(f"{indent}CLOSE {cur_var};")
        return out

    def _emit_dyn_cursor_iteration(
        self, s: A.ForStmt, indent: str,
    ) -> list[str]:
        """Lower `for row in fn_returning_cursor_dyn() { ... }` into a
        DBMS_SQL.DESCRIBE_COLUMNS-based loop.

        Each row arrives in the loop body as a `json` value — a
        JSON object keyed by column name. Numbers preserve as JSON
        numbers; dates/timestamps round-trip as ISO 8601 strings;
        VARCHAR2 stays string. NULLs are represented as JSON null.

        Why JSON and not text:
          - Text loses column boundaries — you can't get individual
            cells back out without re-parsing a delimiter.
          - A synthesized record won't work either: the whole point
            of cursor<dyn> is the row shape isn't known at compile
            time. Even @relies_on can't enumerate the columns of a
            dynamic pivot (regions are discovered at runtime).
          - JSON is pell's existing dynamic-shape primitive. Users
            already have json::get_text / json::get_number /
            json::get_json / json::has, and `p(row)` auto-stringifies
            via JSON_SERIALIZE.

        Per-fetch we build a fresh JSON_OBJECT_T, populate it with
        typed put() calls (preserves number/date types), then
        serialize into the row var.

        Requires EXECUTE on DBMS_SQL — usually granted to the public
        role, but a DBA can lock it down. We surface the runtime
        error if so.
        """
        self._sql_var_counter += 1
        cur_var = f"l_pell_cur_{self._sql_var_counter}"
        dyn_cur = f"l_pell_dyn_cur_{self._sql_var_counter}"
        desc_var = f"l_pell_desc_{self._sql_var_counter}"
        col_cnt = f"l_pell_col_cnt_{self._sql_var_counter}"
        idx_var = f"l_pell_idx_{self._sql_var_counter}"
        row_var = f"l_pell_row_{self._sql_var_counter}"
        # Mutable JSON DOM used to build each row; serialized into row_var.
        jobj_var = f"l_pell_jobj_{self._sql_var_counter}"
        # Per-type fetch buffers — declared once, reused per column.
        buf_str = f"l_pell_buf_str_{self._sql_var_counter}"
        buf_num = f"l_pell_buf_num_{self._sql_var_counter}"
        buf_dt = f"l_pell_buf_dt_{self._sql_var_counter}"
        buf_ts = f"l_pell_buf_ts_{self._sql_var_counter}"
        col_name = f"l_pell_col_name_{self._sql_var_counter}"

        # On 23ai, `JSON` is a native datatype; on 19c, no such type,
        # so we fall back to VARCHAR2 holding JSON-shaped text (which
        # JSON_VALUE / JSON_QUERY happily accept).
        row_type = "JSON" if self.target == "23" else "VARCHAR2(32767)"

        self._decl(f"{cur_var} SYS_REFCURSOR;")
        self._decl(f"{dyn_cur} PLS_INTEGER;")
        self._decl(f"{col_cnt} PLS_INTEGER;")
        self._decl(f"{desc_var} DBMS_SQL.DESC_TAB;")
        self._decl(f"{idx_var} PLS_INTEGER;")
        self._decl(f"{row_var} {row_type};")
        self._decl(f"{jobj_var} JSON_OBJECT_T;")
        self._decl(f"{buf_str} VARCHAR2(4000);")
        self._decl(f"{buf_num} NUMBER;")
        self._decl(f"{buf_dt} DATE;")
        self._decl(f"{buf_ts} TIMESTAMP;")
        self._decl(f"{col_name} VARCHAR2(128);")

        # On 23ai we want a real JSON value, not a string. JSON_OBJECT_T.to_json()
        # returns JSON; on 19c we use to_string() (no JSON datatype).
        serialize_call = (
            f"{jobj_var}.to_json()" if self.target == "23"
            else f"{jobj_var}.to_string()"
        )

        call_code = self._emit_expr(s.iterable)
        out = [
            f"{indent}{cur_var} := {call_code};",
            f"{indent}{dyn_cur} := DBMS_SQL.TO_CURSOR_NUMBER({cur_var});",
            f"{indent}DBMS_SQL.DESCRIBE_COLUMNS({dyn_cur}, {col_cnt}, {desc_var});",
            # Define each column based on its native type.
            f"{indent}FOR {idx_var} IN 1 .. {col_cnt} LOOP",
            f"{indent}  IF {desc_var}({idx_var}).col_type = 2 THEN",
            f"{indent}    DBMS_SQL.DEFINE_COLUMN({dyn_cur}, {idx_var}, {buf_num});",
            f"{indent}  ELSIF {desc_var}({idx_var}).col_type = 12 THEN",
            f"{indent}    DBMS_SQL.DEFINE_COLUMN({dyn_cur}, {idx_var}, {buf_dt});",
            f"{indent}  ELSIF {desc_var}({idx_var}).col_type IN (180, 181, 231) THEN",
            f"{indent}    DBMS_SQL.DEFINE_COLUMN({dyn_cur}, {idx_var}, {buf_ts});",
            f"{indent}  ELSE",
            f"{indent}    DBMS_SQL.DEFINE_COLUMN({dyn_cur}, {idx_var}, {buf_str}, 4000);",
            f"{indent}  END IF;",
            f"{indent}END LOOP;",
            # Fetch loop: build a JSON_OBJECT_T per row, serialize at the end.
            f"{indent}WHILE DBMS_SQL.FETCH_ROWS({dyn_cur}) > 0 LOOP",
            f"{indent}  {jobj_var} := NEW JSON_OBJECT_T();",
            f"{indent}  FOR {idx_var} IN 1 .. {col_cnt} LOOP",
            f"{indent}    {col_name} := {desc_var}({idx_var}).col_name;",
            f"{indent}    IF {desc_var}({idx_var}).col_type = 2 THEN",
            f"{indent}      DBMS_SQL.COLUMN_VALUE({dyn_cur}, {idx_var}, {buf_num});",
            f"{indent}      IF {buf_num} IS NULL THEN {jobj_var}.put_null({col_name});",
            f"{indent}      ELSE {jobj_var}.put({col_name}, {buf_num}); END IF;",
            f"{indent}    ELSIF {desc_var}({idx_var}).col_type = 12 THEN",
            f"{indent}      DBMS_SQL.COLUMN_VALUE({dyn_cur}, {idx_var}, {buf_dt});",
            f"{indent}      IF {buf_dt} IS NULL THEN {jobj_var}.put_null({col_name});",
            f"{indent}      ELSE {jobj_var}.put({col_name}, TO_CHAR({buf_dt}, 'YYYY-MM-DD\"T\"HH24:MI:SS')); END IF;",
            f"{indent}    ELSIF {desc_var}({idx_var}).col_type IN (180, 181, 231) THEN",
            f"{indent}      DBMS_SQL.COLUMN_VALUE({dyn_cur}, {idx_var}, {buf_ts});",
            f"{indent}      IF {buf_ts} IS NULL THEN {jobj_var}.put_null({col_name});",
            f"{indent}      ELSE {jobj_var}.put({col_name}, TO_CHAR({buf_ts}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6')); END IF;",
            f"{indent}    ELSE",
            f"{indent}      DBMS_SQL.COLUMN_VALUE({dyn_cur}, {idx_var}, {buf_str});",
            f"{indent}      IF {buf_str} IS NULL THEN {jobj_var}.put_null({col_name});",
            f"{indent}      ELSE {jobj_var}.put({col_name}, {buf_str}); END IF;",
            f"{indent}    END IF;",
            f"{indent}  END LOOP;",
            f"{indent}  {row_var} := {serialize_call};",
        ]
        # Body — the user's `row` is now a pell `json` value.
        self._loop_vars.append({s.var_name})
        prev_override = self._loop_var_override.get(s.var_name)
        self._loop_var_override[s.var_name] = row_var
        prev_type = self._loop_var_types.get(s.var_name)
        self._loop_var_types[s.var_name] = "json"
        for stmt in s.body:
            out.extend(self._emit_stmt(stmt, indent + "  "))
        self._loop_vars.pop()
        if prev_override is None:
            del self._loop_var_override[s.var_name]
        else:
            self._loop_var_override[s.var_name] = prev_override
        if prev_type is None:
            self._loop_var_types.pop(s.var_name, None)
        else:
            self._loop_var_types[s.var_name] = prev_type

        out.append(f"{indent}END LOOP;")
        out.append(f"{indent}DBMS_SQL.CLOSE_CURSOR({dyn_cur});")
        return out

    def _emit_json_keys_iteration(
        self, s: A.ForStmt, indent: str,
    ) -> list[str]:
        """Lower `for k in json::get_keys(j) { ... }` to a JSON_OBJECT_T
        get_keys() + index loop.

        Useful for walking a `cursor<dyn>` row when you don't know the
        column set in advance:

            for row in some_dyn_cursor() {
                for k in json::get_keys(row) {
                    p("{k} = {json::get_text(row, \"$.\\{k\\}\")}");
                }
            }

        Note: get_keys() returns the keys in JSON_OBJECT_T's internal
        order, which is insertion order on 21c+ and undefined on earlier
        versions. Don't rely on the order.
        """
        call = s.iterable
        if len(call.args) != 1:
            raise EmitError(
                "json::get_keys takes one json value: "
                "json::get_keys(<json_value>)", s.loc,
            )
        self._sql_var_counter += 1
        jobj_var = f"l_pell_jkl_jobj_{self._sql_var_counter}"
        keys_var = f"l_pell_jkl_{self._sql_var_counter}"
        idx_var = f"l_pell_jkl_i_{self._sql_var_counter}"
        key_var = f"l_pell_jkl_key_{self._sql_var_counter}"

        self._decl(f"{jobj_var} JSON_OBJECT_T;")
        self._decl(f"{keys_var} JSON_KEY_LIST;")
        self._decl(f"{idx_var} PLS_INTEGER;")
        self._decl(f"{key_var} VARCHAR2(4000);")

        # JSON_OBJECT_T(j) is the universal constructor — accepts JSON,
        # VARCHAR2 (parses), CLOB, or JSON_ELEMENT_T.
        src = self._emit_expr(call.args[0])
        out = [
            f"{indent}{jobj_var} := JSON_OBJECT_T({src});",
            f"{indent}{keys_var} := {jobj_var}.get_keys();",
            f"{indent}FOR {idx_var} IN 1 .. {keys_var}.COUNT LOOP",
            f"{indent}  {key_var} := {keys_var}({idx_var});",
        ]
        # Body — `k` shadows to key_var, typed text.
        self._loop_vars.append({s.var_name})
        prev_override = self._loop_var_override.get(s.var_name)
        self._loop_var_override[s.var_name] = key_var
        prev_type = self._loop_var_types.get(s.var_name)
        self._loop_var_types[s.var_name] = "text"
        for stmt in s.body:
            out.extend(self._emit_stmt(stmt, indent + "  "))
        self._loop_vars.pop()
        if prev_override is None:
            del self._loop_var_override[s.var_name]
        else:
            self._loop_var_override[s.var_name] = prev_override
        if prev_type is None:
            self._loop_var_types.pop(s.var_name, None)
        else:
            self._loop_var_types[s.var_name] = prev_type
        out.append(f"{indent}END LOOP;")
        return out

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
            # Dispatch on the sys_tag_ ordinal, NOT `IS OF (t_case)` —
            # Oracle 23ai's default optimizer level miscompiles IS OF on
            # substitutable locals reassigned in loops (elements silently
            # vanish from a match-walk). A NUMBER compare is immune.
            tag = self._case_tag(case.name)
            # NOTE: no parens around the scrutinee — PL/SQL rejects
            # attribute access on a parenthesized expression
            # (`(x).attr` is SQL-only syntax; `x.attr` is required here).
            out.append(f"{indent}{kw} {scrut_code}.sys_tag_ = {tag} THEN")
            # Bind value: VariantPattern.args (positional) gives bind name(s);
            # VariantPattern.fields (struct-form) lets us project fields.
            bind_name: Optional[str] = None
            if pat.args and isinstance(pat.args[0], A.BindingPattern):
                bind_name = pat.args[0].name
            # Struct-form pattern. Two surface shapes:
            #   `Circle { radius }`     — bind `radius` (field-name = local-name)
            #   `Cons { head: h, tail: t }` — bind `h`, `t` (rename via inner pattern)
            # Stored as triples: (pell-side bind name, PL/SQL local name, source field name).
            field_binds: list[tuple[str, str, str]] = []
            if pat.fields:
                for fp in pat.fields:
                    bind_pell_name = fp.name  # shorthand
                    if isinstance(fp.pattern, A.BindingPattern):
                        bind_pell_name = fp.pattern.name
                    elif isinstance(fp.pattern, A.WildcardPattern):
                        continue  # `head: _` discards the field; no bind
                    field_binds.append((bind_pell_name, local_name(bind_pell_name), fp.name))
            inner_indent = indent + "  "
            if bind_name is not None or field_binds:
                out.append(f"{inner_indent}DECLARE")
                if bind_name is not None:
                    out.append(f"{inner_indent}  {local_name(bind_name)} {case_pl};")
                for bind_pell, pl_name, src_field in field_binds:
                    # Field types come from the case (or its parent's fields).
                    ftype = self._case_field_type(case, src_field)
                    if ftype is None:
                        raise EmitError(
                            f"case {case.name} has no field {src_field!r}",
                            arm.loc,
                        )
                    out.append(f"{inner_indent}  {pl_name} {ftype};")
                out.append(f"{inner_indent}BEGIN")
                if bind_name is not None:
                    out.append(f"{inner_indent}  {local_name(bind_name)} := TREAT(({scrut_code}) AS {case_pl});")
                for bind_pell, pl_name, src_field in field_binds:
                    out.append(
                        f"{inner_indent}  {pl_name} := TREAT(({scrut_code}) AS {case_pl}).{src_field.lower()};"
                    )
                # Inside the bound block, register the binding in _local_types
                # (not _params) so _lower_ident returns the `l_` prefix that
                # matches our DECLARE above. Storing in _params would yield
                # `p_<n>` references with no matching parameter.
                save_local_types = dict(self._local_types)
                if bind_name is not None:
                    self._local_types[bind_name] = case_pl
                for bind_pell, _, src_field in field_binds:
                    ftype = self._case_field_type(case, src_field)
                    if ftype is not None:
                        self._local_types[bind_pell] = ftype
                try:
                    self._emit_arm_body(arm.body, out, inner_indent + "  ")
                finally:
                    self._local_types = save_local_types
                out.append(f"{inner_indent}END;")
            else:
                self._emit_arm_body(arm.body, out, inner_indent)
        out.append(f"{indent}END IF;")
        return out

    def _emit_arm_body(self, body, out: list[str], indent: str) -> None:
        if isinstance(body, list):
            if not body:
                # Oracle requires at least one statement per IF/ELSIF branch.
                out.append(f"{indent}NULL;")
                return
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
        # Reject pure expressions as bare statements. PL/SQL has no
        # `<expr>;` statement form — only calls, assignments, SQL DML,
        # and control flow. Letting a NumberLit / BinOp / Ident through
        # produces invalid PL/SQL that Oracle quietly compiles to a
        # broken package body (USER_ERRORS catches it eventually but
        # the pell user sees a confusing PLS-00103 instead of a clear
        # "this statement has no effect" message).
        if not self._is_statement_shaped(e):
            kind = type(e).__name__
            raise EmitError(
                f"expression of type {kind} can't stand alone as a "
                "statement — its value is unused. Bind it with "
                "`let _ = ...` or use it inside a call/assignment.",
                s.loc,
                code="pell.unused-value",
            )
        # Discarded function return: `f();` where f has a non-Unit
        # return type is invalid PL/SQL (PLS-00221 — "is not a procedure
        # or is undefined"). Catch it at compile time when we can
        # resolve the callee. Cross-system calls (dbms_output.*, Oracle
        # built-ins, opaque OBJECT methods) stay permissive because we
        # don't have signatures for them.
        self._reject_unused_return(e, s.loc)
        # Side-effecting expression (Call / pipeline / try-call / etc.).
        # If it's a Call with ListLit args, lift them to pre-declared
        # locals so the emitter doesn't fall through to /* TODO: ListLit
        # unhandled */.
        if isinstance(e, A.Call):
            pre, e = self._lift_listlit_args(e, indent)
            return pre + [f"{indent}{self._emit_expr(e)};"]
        return [f"{indent}{self._emit_expr(e)};"]

    def _reject_unused_return(self, e: A.Expr, loc: Any) -> None:
        """Raise EmitError if `e` is a bare-statement Call whose callee
        we can resolve AND whose return type is non-Unit.

        Resolution sources, in order:
          - Same-module fns in `self._fns`
          - Cross-package fns in `self._project_signatures`

        Unresolvable callees (member access, unknown identifiers, Oracle
        built-ins, OBJECT methods) pass through — we'd rather miss a real
        error than reject legitimate code we can't type-check.
        """
        # Unwrap try-call / Ok / Some — the callee is on the inner.
        inner: A.Expr = e
        while isinstance(inner, (A.QuestionMark, A.OkExpr, A.SomeExpr)):
            inner = inner.inner
        if not isinstance(inner, A.Call):
            return
        if not isinstance(inner.callee, A.Ident):
            return
        fn_name = inner.callee.name
        # Side-effect-only builtins that don't need this check. Most
        # are already handled by earlier branches in _emit_expr_stmt,
        # but listing them here makes the intent explicit.
        if fn_name in ("exec_dyn", "p", "print", "println"):
            return
        ret_type = None
        # Same-module first.
        for fn in self._fns:
            if fn.name == fn_name:
                ret_type = fn.return_type
                break
        # Cross-package registry.
        if ret_type is None and "::" in fn_name:
            ret_type = self._project_signatures.get(fn_name)
        if ret_type is None:
            return  # unknown callee — be permissive
        if self._is_unit_return(ret_type):
            return
        raise EmitError(
            f"function `{fn_name}` returns a non-Unit value that's "
            "being discarded. PL/SQL rejects this (PLS-00221: not a "
            "procedure). Bind the result with `let _ = "
            f"{fn_name}(...)` or use it in an expression.",
            loc,
            code="pell.unused-return",
        )

    @staticmethod
    def _is_unit_return(t: A.TypeRef) -> bool:
        """A pell return type is Unit (PL/SQL PROCEDURE) when it's
        absent, the literal `Unit`, or the parser's placeholder for
        a bare `fn foo() { ... }` with no `-> T`."""
        if t is None:
            return True
        if isinstance(t, A.PrimType) and t.name == "Unit":
            return True
        if isinstance(t, A.NamedType) and t.name == "Unit":
            return True
        return False

    @staticmethod
    def _is_statement_shaped(e: A.Expr) -> bool:
        """A bare-statement expression must have a side effect. Calls
        (procedure or function-for-side-effect), pipelines that reduce
        to calls, and try-call (`call()?`) all qualify. Pure literals,
        identifiers, binops, and member accesses do not.

        Conservative: when in doubt, allow — better to let Oracle's
        own parser reject a weird edge case than to refuse legitimate
        code. The known-pure shapes below are the high-confidence
        rejects.
        """
        if isinstance(e, (A.Call, A.PipelineExpr)):
            return True
        # Try-call: `foo()?` — sugar for "raise if foo returned an err".
        # Still a call underneath.
        if isinstance(e, A.QuestionMark):
            return Emitter._is_statement_shaped(e.inner)
        if isinstance(e, A.OkExpr):
            return Emitter._is_statement_shaped(e.inner)
        if isinstance(e, A.SomeExpr):
            return Emitter._is_statement_shaped(e.inner)
        return False

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
            # Validate field access on primitive-typed receivers. The
            # common trip-up: `for i in catalog::list_types() { p(i.name); }`
            # where `i` is `text` (list_types returns list<text>), not a
            # record. Without this check the emit succeeds and Oracle
            # later throws PLS-00487 / PLS-00302 at runtime, pointing
            # at the lowered local-name shadow that the user can't read.
            self._check_field_on_primitive(e)
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
            # Record-typed StructLits are handled at assign-time
            # (_emit_assign_to does per-field copy). If we're here, the
            # type is neither a known record nor a known OBJECT type —
            # the user referenced an undefined type. Give them a clear
            # error with a *suggested* definition inferred from the
            # struct's field values, so they can copy-paste the fix.
            suggested = self._suggest_record_def(e)
            known_recs = ", ".join(r.name for r in self._records) or "(none)"
            known_types = ", ".join(self._type_names) or "(none)"
            raise EmitError(
                f"unknown type `{e.type_name}` in struct literal "
                f"`{e.type_name} {{ ... }}`.\n"
                f"  did you mean: {suggested}\n"
                f"  known records: {known_recs}; known types: {known_types}",
                e.loc,
                code="pell.unknown-struct-type",
            )
        if isinstance(e, A.SqlBlock):
            # TODO #1: bare sql block in expression position. The
            # proper fix is to synthesize a temp local, emit SELECT
            # INTO temp before the containing statement, and substitute
            # temp here. For now: emit a clear comment that at least
            # tells the user what went wrong (PL/SQL will still fail
            # but with line/col pointing here).
            return "/* TODO: bare sql block in expr position — see github issue */"
        return f"/* TODO: {type(e).__name__} — emitter unhandled */"

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

        Raw literals (backtick-delimited, `is_raw=True`) skip interpolation
        entirely — `{` and `}` are kept verbatim. This is what lets regex
        patterns like `\\d{3}` survive without doubling the braces.
        """
        import re
        from .lexer import tokenize
        from .parser import Parser, ParseError
        s = e.value
        if e.is_raw:
            return _sql_string(s)
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
                code = self._emit_expr(sub_expr)
                # Auto-stringify: the interpolation slot is a text
                # context (will be || concatenated). Wrap non-text
                # expressions so Oracle doesn't choke on JSON, BOOLEAN,
                # etc. NUMBER works via Oracle's implicit || coercion
                # but wrapping explicitly is harmless and consistent.
                inferred = self._infer_expr_type(sub_expr)
                code = self._auto_stringify(code, inferred)
                chunks.append(code)
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

    # Known call targets that accept a single text argument. When the
    # actual arg is non-text, the emitter auto-stringifies so the user
    # can write `log::info(x)` or `dbms_output::put_line(j)` for any
    # typed `x` without a manual TO_CHAR / JSON_SERIALIZE wrapper.
    _TEXT_CONSUMING_CALLS = frozenset({
        "logger::info", "logger::warn", "logger::err", "logger::debug", "logger::trace",
        "dbms_output::put_line",
    })

    # Compile-time aliases for `dbms_output.put_line`. Less typing in
    # the REPL and casual code; the emitter rewrites the call to
    # `dbms_output.put_line(<auto-stringified arg>)`. No PL/SQL
    # package is required at runtime.
    _PRINT_ALIASES: dict[str, str] = {
        "p":            "dbms_output.put_line",
        "print":        "dbms_output.put_line",
        "println":      "dbms_output.put_line",
        "std::p":       "dbms_output.put_line",
        "std::print":   "dbms_output.put_line",
        "std::println": "dbms_output.put_line",
    }

    def _emit_call_expr(self, e: A.Call) -> str:
        # Early check: the user wrote `pkg.fn(...)` (dot) when they
        # meant `pkg::fn(...)` (colon-colon). If pkg isn't a known
        # local/param but pkg::fn IS in the project signature
        # registry, that's almost certainly a typo — raise a
        # compile-time error with the fix rather than letting the
        # bogus `catalog.list_types()` reach Oracle and fail there.
        suggestion = self._suggest_cross_pkg_call(e)
        if suggestion is not None:
            wrong, right = suggestion
            raise EmitError(
                f"`{wrong}` looks like a cross-package call written "
                f"with `.` instead of `::`. did you mean `{right}`?",
                e.loc,
                code="pell.dot-vs-colon",
            )

        # Import gate for cross-package calls: `pkg::fn(...)` requires
        # the local module to `import pkg;`. The pell::* internal helpers
        # (json::*, re::*, jq::*, sql!, exec_dyn, etc.) are exempt — they
        # lower to inline PL/SQL or Oracle stdlib and aren't real pell
        # modules. The exemption list mirrors the macro/intrinsic surface
        # the emitter already special-cases.
        if (isinstance(e.callee, A.Ident)
                and "::" in e.callee.name
                and not self._is_intrinsic_call(e.callee.name)):
            pkg = e.callee.name.rsplit("::", 1)[0]
            pkg_key = pkg.replace("::", "_").replace(".", "_")
            self._check_pkg_imported(pkg_key, e.loc)
            self._check_pkg_accessible(pkg_key, e.loc)

        # Print aliases — rewrite to dbms_output.put_line, auto-stringify
        # the single argument so any type prints with the same format as
        # the auto-stringify table (TO_CHAR with explicit masks for
        # dates/timestamps, JSON_SERIALIZE for json, etc.).
        if (isinstance(e.callee, A.Ident)
                and e.callee.name in self._PRINT_ALIASES
                and len(e.args) == 1):
            arg = e.args[0]
            code = self._emit_expr(arg)
            inferred = self._infer_expr_type(arg)
            code = self._auto_stringify(code, inferred)
            return f"{self._PRINT_ALIASES[e.callee.name]}({code})"

        # Auto-stringify args for known text-only call targets.
        if (isinstance(e.callee, A.Ident)
                and e.callee.name in self._TEXT_CONSUMING_CALLS
                and len(e.args) == 1):
            arg = e.args[0]
            code = self._emit_expr(arg)
            inferred = self._infer_expr_type(arg)
            code = self._auto_stringify(code, inferred)
            fn_name = self._lower_ident(e.callee.name)
            return f"{fn_name}({code})"

        # JSON helper namespace — `json::object`, `json::array`, `json::get_text`,
        # `json::get_number`, `json::get_json`, `json::has`, `json::parse`,
        # `json::stringify`, `json::from`. (`json::into::<T>(...)?` is handled
        # at let-statement level since it produces a multi-statement
        # field-by-field record construction.)
        if isinstance(e.callee, A.Ident) and e.callee.name.startswith("json::"):
            result = self._emit_json_helper(e)
            if result is not None:
                return result
        # re::* helpers — route to the runtime pell_re package. Scalar
        # returners pass through directly; list-returners go through a
        # per-package adapter that copies pell_re.t_text_list into the
        # package's own t_text_list (PL/SQL uses nominal typing for
        # collections, so we can't just RETURN the foreign type).
        if isinstance(e.callee, A.Ident) and e.callee.name.startswith("re::"):
            result = self._emit_re_helper(e)
            if result is not None:
                return result
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
            # .to_text() — type-aware auto-stringify. Uses the same
            # conversion table as string interpolation and text-consuming
            # call targets.
            if method == "to_text" and len(e.args) == 0:
                recv_code = self._emit_expr(recv)
                inferred = self._infer_expr_type(recv)
                return self._auto_stringify(recv_code, inferred)
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
            _safe_drop_type(self._q(type_name))
            + f"CREATE OR REPLACE TYPE {self._q(type_name)} AS OBJECT (\n"
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
        # Every sealed parent carries a hidden sys_tag_ attribute holding
        # the constructing case's 1-based declaration ordinal. Match arms
        # dispatch on it (`x.sys_tag_ = k`) instead of `IS OF (t_case)`:
        # Oracle 23ai's PL/SQL optimizer (PLSQL_OPTIMIZE_LEVEL=2, the
        # default) miscompiles IS OF on substitutable locals reassigned
        # in loops — a match-walk over a sealed list silently dropped
        # elements; recompiling at level 1 fixed it. A plain NUMBER
        # compare is immune. (Also satisfies PLS-00589's at-least-one-
        # attribute rule for fieldless parents.)
        spec_lines = ["  sys_tag_ NUMBER"] + attr_lines + parent_method_sigs
        suffix = "NOT FINAL"
        if any_abstract:
            suffix = "NOT INSTANTIABLE " + suffix
        parent_spec = (
            _safe_drop_type(self._q(parent_name))
            + f"CREATE OR REPLACE TYPE {self._q(parent_name)} AS OBJECT (\n"
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
                _safe_drop_type(self._q(case_name))
                + f"CREATE OR REPLACE TYPE {self._q(case_name)} UNDER {parent_name} (\n"
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
                _safe_drop_type(self._q(tuple_type_name))
                + f"CREATE OR REPLACE TYPE {self._q(tuple_type_name)} AS OBJECT (\n{tuple_attrs}\n);\n/"
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
            _safe_drop_type(self._q(type_name))
            + f"CREATE OR REPLACE TYPE {self._q(type_name)} AS OBJECT (\n"
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
                    _safe_drop_type(self._q(nt_name))
                    + f"CREATE OR REPLACE TYPE {self._q(nt_name)} AS TABLE OF {elem_sql};\n/"
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
        out.append(f"  {sig} IS{self._dbg_loc_marker(m.loc)}")
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
        - A let-bound local whose declared PL/SQL type matches a known OBJECT type → yes.
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
            # Let-bound locals: their stored PL/SQL type matches `t_<typename>`
            # when the source-level type is one of our OBJECT types.
            if recv.name in self._local_types:
                lt = self._local_types[recv.name]
                for t_name in self._type_names:
                    if lt == _record_type_name(t_name):
                        return True
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
        prefix_args: list[str] = []
        td = self._lookup_type(sl.type_name)
        if td is not None:
            ordered_field_names = [f.name for f in td.fields]
        else:
            ck = self._lookup_case(sl.type_name)
            if ck is not None:
                st, case = ck
                ordered_field_names = [f.name for f in st.fields] + [f.name for f in case.fields]
                # Every sealed parent's first attribute is sys_tag_ — the
                # constructing case's ordinal, which match arms dispatch
                # on (see the sealed-parent spec emitter for why IS OF
                # was abandoned).
                prefix_args = [str(self._case_tag(sl.type_name))]
        provided = {f.name: f.value for f in sl.fields}
        missing = [n for n in ordered_field_names if n not in provided]
        if missing:
            raise EmitError(
                f"{sl.type_name} {{ ... }}: missing fields {missing}",
                sl.loc,
            )
        args_list = prefix_args + [self._emit_expr(provided[n]) for n in ordered_field_names]
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

    def _case_tag(self, case_name: str) -> int:
        """The 1-based declaration ordinal of a case within its sealed
        type — the value stored in sys_tag_ at construction and compared
        by match dispatch."""
        ck = self._lookup_case(case_name)
        assert ck is not None, f"not a sealed case: {case_name}"
        st, case = ck
        return st.cases.index(case) + 1

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
            head = ".".join(parts[:-1]).lower()
            # Resolve a module SHORT name to its real qualified package.
            # `parsing::tokenize` (module app.parsing) must lower to
            # `app_parsing.tokenize` — and a schema-qualified module
            # (`module demo::app.parsing`) to `demo.app_parsing.…`.
            # The local module is exempt (calls to self stay in-package
            # — though those are normally unqualified anyway).
            info = self._project_modules.get(head)
            if info is not None and info.package_name != self.pkg:
                head = info.qualified_name.lower()
            return head + "." + parts[-1].lower()
        # Parameters shadow same-named fn references — a fn's own param
        # always wins inside its body. (Was the other way around and caused
        # subtle aliasing when, e.g., a param `s` shadowed a sibling fn `s`.)
        if name in self._params:
            return param_name(name)
        if name in self._fn_names:
            return name.lower()
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


def _stmt_always_returns(s: A.Stmt) -> bool:
    """Does this statement guarantee that control leaves the enclosing
    fn (either via `return …;` or via raising)? Conservative: when in
    doubt, returns False. Used by the missing-return check, so a False
    negative just means we don't flag a fn that's actually fine — a
    False positive would refuse to compile a fn that's actually OK.

    Cases we recognize:
      * ReturnStmt          → True
      * IfStmt              → both then_body AND else_body always return
      * MatchStmt           → every arm body always returns
      * TransactionStmt     → inner block always returns
      * Bare ExprStmt where expr is a raise-style call (RAISE / RaiseStmt)
        — not handled; we lack a dedicated raise node in v0.

    Everything else (assignment, let, for/forall loops, bare calls,
    ExprStmt with QuestionMark, yield) returns False.
    """
    if isinstance(s, A.ReturnStmt):
        return True
    if isinstance(s, A.IfStmt):
        if not s.else_body:
            return False
        return _body_always_returns(s.then_body) \
            and _body_always_returns(s.else_body)
    if isinstance(s, A.MatchStmt):
        if not s.arms:
            return False
        return all(
            (_body_always_returns(arm.body)
             if isinstance(arm.body, list)
             else isinstance(arm.body, A.Expr) and False)
            for arm in s.arms
        )
    if isinstance(s, A.TransactionStmt):
        return _body_always_returns(s.body)
    return False


def _body_always_returns(body: list["A.Stmt"]) -> bool:
    """A statement list always returns iff its LAST stmt always returns.
    (Anything before the last is irrelevant — only the trailing
    fall-through matters.) Empty list → False.
    """
    if not body:
        return False
    return _stmt_always_returns(body[-1])


def _is_unit_like(t: A.TypeRef) -> bool:
    if isinstance(t, A.PrimType) and t.name in ("Unit", "Never"):
        return True
    if isinstance(t, A.GenericType) and t.base == "Result" and t.params:
        return _is_unit_like(t.params[0])
    return False


def _safe_drop_type(name: str) -> str:
    """An idempotent guard to put before `CREATE OR REPLACE TYPE`.

    Oracle blocks `CREATE OR REPLACE TYPE` when the target has
    dependents (most commonly a TABLE OF / nested-table type that
    references an OBJECT TYPE). Dropping the type with FORCE clears
    the path; wrapping in a BEGIN/EXCEPTION block keeps the first
    install idempotent (the DROP errors when the type doesn't exist
    yet — we swallow that case).
    """
    return (
        f"BEGIN\n"
        f"  EXECUTE IMMEDIATE 'DROP TYPE {name} FORCE';\n"
        f"EXCEPTION WHEN OTHERS THEN NULL;\n"
        f"END;\n"
        f"/\n"
    )


def _sql_string(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _jq_literal_pl_type(lit: object) -> str:
    """Infer the JSON_TABLE COLUMNS type for a synthetic filter column based
    on the literal value being compared against."""
    if isinstance(lit, bool):
        return "NUMBER(1)"
    if isinstance(lit, (int, float)):
        return "NUMBER"
    return "VARCHAR2(4000)"


def emit(module: A.Module, target: str = "23", *,
         source_text: Optional[str] = None,
         source_path: Optional[str] = None,
         reproducible: bool = False,
         debug: bool = False,
         project_signatures: Optional[dict[str, "A.TypeRef"]] = None,
         project_records: Optional[
             dict[tuple[str, str], "A.RecordDef"]
         ] = None,
         project_modules: Optional[dict[str, "ModuleInfo"]] = None) -> str:
    """Compile `module` to PL/SQL.

    `project_signatures` and `project_records` thread cross-package
    context — pub fn return types and pub record definitions from
    sibling .pell files. When provided, cross-package fn calls get
    real type inference and cross-package `pkg::Record { … }` struct
    literals lower to the foreign package's PL/SQL record type
    instead of falling through to broken `t_record` references.

    Callers that don't bother scanning (older callers, anon block
    paths that don't need it) can omit both — the emitter falls
    back to local-only resolution and prints the usual EmitError
    when a cross-package reference can't be resolved.
    """
    e = Emitter(
        module, target=target,
        source_text=source_text, source_path=source_path,
        reproducible=reproducible,
        debug_markers=debug,
    )
    if project_signatures:
        e._project_signatures = dict(project_signatures)
    if project_records:
        e._project_records = dict(project_records)
        for (pkg, name), rec in project_records.items():
            e._project_records_by_name.setdefault(name, []).append((pkg, rec))
    if project_modules:
        e._project_modules = dict(project_modules)
    return e.emit()


def scan_project_records(
    root: Optional["Path"] = None,
) -> dict[tuple[str, str], "A.RecordDef"]:
    """Walk `root` (default: cwd) + the pell runtime/ dir, parsing every
    *.pell file, and collect every `pub record` keyed by
    `(pkg_pl_sql_name, record_name)`. The package name is the lowered
    PL/SQL identifier — `collections.linked_list` becomes
    `collections_linked_list`. Identical-named records in two
    different modules both end up in the registry; bare-name
    resolution in the emitter rejects the ambiguity.

    Mirrors `scan_project_signatures` (which lives in repl.py for
    historical reasons) so the build path can do the same lookup.
    Best-effort: parse errors on individual files are ignored.
    """
    from pathlib import Path as _Path
    from .parser import parse as _parse
    records: dict[tuple[str, str], A.RecordDef] = {}
    seen: set[_Path] = set()

    # Build-output / vendor dirs to skip. Match only against path
    # components *relative to the scan root* — otherwise a repo
    # named e.g. "plsql" or "built" excludes itself.
    _SKIP = {"built", "out", ".git", "node_modules", "expected",
             "plsql", "dist", "build", ".venv", "venv"}

    def _walk(base: _Path) -> None:
        base = base.resolve()
        if not base.is_dir() or base in seen:
            return
        seen.add(base)
        for path in base.rglob("*.pell"):
            try:
                rel_parts = path.resolve().relative_to(base).parts
            except ValueError:
                rel_parts = path.parts
            # rel_parts ends with "<file>.pell" — only the *directory*
            # components matter for the skip check.
            if any(p in _SKIP for p in rel_parts[:-1]):
                continue
            try:
                mod = _parse(path.read_text(encoding="utf-8"), str(path))
            except Exception:
                continue
            pkg = mod.name.replace(".", "_").replace("::", "_")
            # For dotted modules (`pell_test.hello`), also register under
            # the short package name (`hello`) so callers that refer to
            # the package by its bare name — the natural form once
            # connected to that schema — resolve. Mirrors
            # scan_project_signatures' dual registration.
            short = mod.name.split(".")[-1].split("::")[-1]
            for item in mod.items:
                if isinstance(item, A.RecordDef) and item.is_pub:
                    records[(pkg, item.name)] = item
                    if short != pkg:
                        records[(short, item.name)] = item

    _walk(_Path(root) if root else _Path.cwd())
    runtime = _Path(__file__).resolve().parent.parent / "runtime"
    _walk(runtime)
    return records


@dataclass(frozen=True)
class ModuleInfo:
    """Project-scan record of one module declaration — everything the
    ACCESSIBLE BY emission and the compile-time access check need.

    `schema` is the explicit `schema::` prefix or None (connected
    schema). `path` is the dotted module path. `domain` is the top
    path segment — the ACCESSIBLE BY boundary. `is_open` marks an
    `@open` module (opts out of access control). `is_root` marks a
    top-level module (no dots): the public application interface.
    """
    schema: Optional[str]
    path: str               # e.g. "app.parsing.lex"
    package_name: str       # e.g. "app_parsing_lex"
    domain: str             # e.g. "app"
    is_open: bool
    is_root: bool

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.package_name}" if self.schema else self.package_name

    @property
    def domain_key(self) -> tuple[str, str]:
        return (self.schema or "", self.domain)


def scan_project_modules(
    root: Optional["Path"] = None,
) -> dict[str, "ModuleInfo"]:
    """Walk the project (same exclusion rules as scan_project_records)
    and index every module declaration by BOTH its package name
    (`app_parsing`) and its short name (`parsing`). Stub modules are
    skipped — they describe externally-owned packages (Oracle SYS etc.)
    and never participate in access domains.

    Feeds two consumers:
      * ACCESSIBLE BY emission — a non-root module lists every other
        package in its (schema, domain) as an accessor.
      * The compile-time access check — a cross-module call to a
        guarded submodule from outside its domain is a pell error
        before Oracle's PLS-00904 at install time.
    """
    from pathlib import Path as _Path
    from .parser import parse as _parse
    out: dict[str, ModuleInfo] = {}
    seen: set[_Path] = set()
    _SKIP = {"built", "out", ".git", "node_modules", "expected",
             "plsql", "dist", "build", ".venv", "venv"}

    def _walk(base: _Path) -> None:
        base = base.resolve()
        if not base.is_dir() or base in seen:
            return
        seen.add(base)
        for path in base.rglob("*.pell"):
            try:
                rel_parts = path.resolve().relative_to(base).parts
            except ValueError:
                rel_parts = path.parts
            if any(p in _SKIP for p in rel_parts[:-1]):
                continue
            try:
                mod = _parse(path.read_text(encoding="utf-8"), str(path))
            except Exception:
                continue
            if any(a.name == "stub" for a in mod.annotations):
                continue
            info = ModuleInfo(
                schema=mod.schema,
                path=mod.name,
                package_name=mod.package_name,
                domain=mod.access_domain,
                is_open=any(a.name == "open" for a in mod.annotations),
                is_root=mod.is_root_module,
            )
            out[mod.package_name] = info
            short = mod.short_name
            if short != mod.package_name:
                out.setdefault(short, info)

    _walk(_Path(root) if root else _Path.cwd())
    runtime = _Path(__file__).resolve().parent.parent / "runtime"
    _walk(runtime)
    return out


def _resolve_name_loc(
    item: "A.Item", src_lines: list[str],
) -> "A.Loc":
    """Given an Item with .loc pointing at the leading keyword (`fn`,
    `record`, `error`, `aggregate`), return a Loc pointing at the
    declaration's IDENT. Falls back to item.loc if the name can't
    be located on that line.

    Used by scan_project_definitions so goto-def lands cursor on the
    name rather than the keyword — required for subsequent Find
    Usages to look up the right symbol at the new cursor position.
    """
    import re as _re
    name = getattr(item, "name", None)
    if not name or item.loc.line - 1 >= len(src_lines):
        return item.loc
    line = src_lines[item.loc.line - 1]
    # The keyword sits at item.loc.col (1-based). Scan forward for
    # the IDENT matching `name` after that column.
    start_col = max(item.loc.col - 1, 0)
    m = _re.search(
        r"\b" + _re.escape(name) + r"\b",
        line[start_col:],
    )
    if m is None:
        return item.loc
    return A.Loc(
        file=item.loc.file,
        line=item.loc.line,
        col=start_col + m.start() + 1,  # back to 1-based
    )


def scan_project_definitions(
    root: Optional["Path"] = None,
) -> dict[tuple[str, str], "A.Loc"]:
    """Walk the project (same exclusion rules as scan_project_records)
    and produce a `(pkg, name) -> Loc` map covering every pub `fn`,
    `record`, `error`, and `aggregate` definition. The Loc is what the
    LSP uses to teleport to the source for cross-file goto-definition.

    Records are already available via scan_project_records (with the
    full AST node), but goto-def needs uniform navigation across all
    item kinds, so we collect everything here in one pass.
    """
    from pathlib import Path as _Path
    from .parser import parse as _parse
    out: dict[tuple[str, str], A.Loc] = {}
    seen: set[_Path] = set()
    _SKIP = {"built", "out", ".git", "node_modules", "expected",
             "plsql", "dist", "build", ".venv", "venv"}

    def _walk(base: _Path) -> None:
        base = base.resolve()
        if not base.is_dir() or base in seen:
            return
        seen.add(base)
        for path in base.rglob("*.pell"):
            try:
                rel_parts = path.resolve().relative_to(base).parts
            except ValueError:
                rel_parts = path.parts
            if any(p in _SKIP for p in rel_parts[:-1]):
                continue
            try:
                src = path.read_text(encoding="utf-8")
                mod = _parse(src, str(path))
            except Exception:
                continue
            src_lines = src.splitlines()
            pkg = mod.name.replace(".", "_").replace("::", "_")
            short = mod.name.split(".")[-1].split("::")[-1]
            for item in mod.items:
                kinds = (A.FnDef, A.RecordDef, A.ErrorDef, A.AggregateDef)
                if not isinstance(item, kinds):
                    continue
                if not getattr(item, "is_pub", False):
                    continue
                # Resolve the NAME's location, not the keyword's. Parser
                # stores item.loc on the `fn`/`record`/`error`/`aggregate`
                # keyword; for navigation we want the cursor to land on
                # the identifier so Find Usages at that position resolves
                # to the symbol (IDE jumps cursor to goto-def result, then
                # subsequent Find Usages searches the name at that position).
                name_loc = _resolve_name_loc(item, src_lines)
                out[(pkg, item.name)] = name_loc
                # Also under the short form for dotted modules so
                # `logger::info` from `std::logger` finds the same loc.
                if short != pkg:
                    out[(short, item.name)] = name_loc

    _walk(_Path(root) if root else _Path.cwd())
    runtime = _Path(__file__).resolve().parent.parent / "runtime"
    _walk(runtime)
    return out


def scan_project_references(
    root: Optional["Path"] = None,
) -> dict[tuple[Optional[str], str], list["A.Loc"]]:
    """Walk the project and collect reference sites for every project
    symbol. Two flavors of entries land in the same dict:

    * `(pkg, name)` — explicit qualified call site, like
      `catalog::list_tables(...)`. Always recorded.
    * `(None, name)` — bare call site, like `display_tables()` with no
      qualifier. on_references merges these in when the symbol is
      unique by name across the project; otherwise they're ignored
      (ambiguous bare calls can't be safely attributed).

    Returns `(pkg_or_None, name) -> [Loc, ...]`. Best-effort regex
    pass over source text; doesn't try to skip declaration sites
    (the caller deduplicates against the definitions index).
    """
    import re as _re
    from pathlib import Path as _Path
    refs: dict[tuple[Optional[str], str], list[A.Loc]] = {}
    seen: set[_Path] = set()
    _SKIP = {"built", "out", ".git", "node_modules", "expected",
             "plsql", "dist", "build", ".venv", "venv"}
    # Match `pkg::name`. The pkg can be a multi-segment qualifier
    # (`std::logger::info`); we record refs under the last segment
    # as pkg + `info` as name (matching how the project registries
    # normalize names).
    qual_pat = _re.compile(r"\b([A-Za-z_][\w]*)::([A-Za-z_][\w]*)")
    # Bare call-position identifier: `name(` not preceded by `::` or
    # `.` (would be a method call) or a word char (continuation of
    # a longer name). Captures fn calls + struct lits at any
    # location, including same-file usages of locally-defined fns.
    bare_pat = _re.compile(r"(?<![\w:.])([A-Za-z_][\w]*)\s*\(")

    def _walk(base: _Path) -> None:
        base = base.resolve()
        if not base.is_dir() or base in seen:
            return
        seen.add(base)
        for path in base.rglob("*.pell"):
            try:
                rel_parts = path.resolve().relative_to(base).parts
            except ValueError:
                rel_parts = path.parts
            if any(p in _SKIP for p in rel_parts[:-1]):
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except Exception:
                continue
            for line_no, line in enumerate(src.splitlines(), start=1):
                for m in qual_pat.finditer(line):
                    pkg, name = m.group(1), m.group(2)
                    loc = A.Loc(
                        file=str(path),
                        line=line_no,
                        col=m.start(2) + 1,
                    )
                    refs.setdefault((pkg, name), []).append(loc)
                for m in bare_pat.finditer(line):
                    name = m.group(1)
                    loc = A.Loc(
                        file=str(path),
                        line=line_no,
                        col=m.start(1) + 1,
                    )
                    refs.setdefault((None, name), []).append(loc)

    _walk(_Path(root) if root else _Path.cwd())
    runtime = _Path(__file__).resolve().parent.parent / "runtime"
    _walk(runtime)
    return refs


def emit_anon_block(
    items: list[A.Item],
    stmts: list[A.Stmt],
    target: str = "23",
    *,
    source_path: Optional[str] = None,
    debug: bool = False,
    project_signatures: Optional[dict[str, "A.TypeRef"]] = None,
    project_records: Optional[dict[tuple[str, str], "A.RecordDef"]] = None,
    project_modules: Optional[dict[str, "ModuleInfo"]] = None,
) -> str:
    """Emit a cell (items + top-level statements) as a self-contained
    anonymous PL/SQL block. Used by `pell exec` and the REPL.

    `record` and `fn` items are nested as DECLARE-local types and
    subprograms. `type` / `sealed` / `aggregate` items require schema-level
    OBJECT TYPE and raise EmitError — use `pell build` for those.

    `project_signatures` is an optional `<pkg>::<fn>` → return-type map
    of pub fns from sibling .pell files in the same project. Lets the
    emitter infer types of cross-package calls without the user
    annotating the let.

    `project_records` is the matching `(pkg, name) → RecordDef` map so
    cross-package record constructors (`pkg::Args { … }`, or a bare
    `Args { … }` when `import pkg;` is in the cell) resolve in the REPL
    and `pell exec` the same way they do in `pell build`.
    """
    loc = A.Loc(file=source_path or "<cell>", line=1, col=1)
    if stmts:
        loc = stmts[0].loc
    elif items:
        loc = items[0].loc
    main_fn = A.FnDef(
        loc=loc,
        annotations=[],
        is_pub=False,
        name="pell_anon_main",
        params=[],
        return_type=None,
        body=list(stmts),
        finally_body=None,
        is_unsafe=False,
    )
    module = A.Module(
        loc=loc,
        name="_pell_anon",
        items=list(items) + [main_fn],
    )
    emitter = Emitter(
        module, target=target, source_path=source_path, reproducible=True,
        debug_markers=debug,
    )
    if project_signatures:
        emitter._project_signatures = dict(project_signatures)
    if project_records:
        emitter._project_records = dict(project_records)
        for (pkg, name), rec in project_records.items():
            emitter._project_records_by_name.setdefault(name, []).append((pkg, rec))
    if project_modules:
        emitter._project_modules = dict(project_modules)
    return emitter.emit_anon()
