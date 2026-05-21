"""AST node definitions for pell.

Every node carries a `loc` (source location) for diagnostics. Nodes are
plain dataclasses; the parser builds them, the emitter consumes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass(frozen=True)
class Loc:
    file: str
    line: int
    col: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class TypeRef:
    loc: Loc


@dataclass
class PrimType(TypeRef):
    name: str  # number, int, text, bool, date, timestamp, bytes, json, Unit


@dataclass
class NamedType(TypeRef):
    name: str  # qualified or unqualified record/error name


@dataclass
class OptionalType(TypeRef):
    inner: TypeRef  # T? -> Optional(T)


@dataclass
class GenericType(TypeRef):
    base: str  # Option, Result, list, map, set
    params: list[TypeRef] = field(default_factory=list)


@dataclass
class ErrorUnionType(TypeRef):
    variants: list[TypeRef] = field(default_factory=list)  # E1 | E2 | E3


# ---------------------------------------------------------------------------
# Patterns (for `match`)
# ---------------------------------------------------------------------------


@dataclass
class Pattern:
    loc: Loc


@dataclass
class WildcardPattern(Pattern):
    pass  # _


@dataclass
class BindingPattern(Pattern):
    name: str  # x


@dataclass
class LiteralPattern(Pattern):
    value: object  # int/str/bool


@dataclass
class VariantPattern(Pattern):
    """Some(x), None, Err(NotFound { id, .. }), Ok(v), etc."""
    name: str
    args: list[Pattern] = field(default_factory=list)
    fields: list["FieldPattern"] = field(default_factory=list)  # for struct-like
    rest: bool = False  # .. captured


@dataclass
class FieldPattern:
    loc: Loc
    name: str
    pattern: Optional[Pattern] = None  # None means shorthand `{ id }` ≡ `{ id: id }`


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass
class Expr:
    loc: Loc


@dataclass
class NumberLit(Expr):
    value: str  # keep as string to preserve precision; emitter passes through


@dataclass
class TextLit(Expr):
    value: str  # already stripped of quotes; interpolation parts kept in `parts` if any
    parts: list["InterpPart"] = field(default_factory=list)


@dataclass
class InterpPart:
    """A piece of an interpolated string: either literal text or an embedded expression."""
    text: Optional[str] = None  # if not None, this is literal text
    expr: Optional[Expr] = None  # if not None, this is `{expr}`


@dataclass
class BoolLit(Expr):
    value: bool


@dataclass
class UnitLit(Expr):
    pass  # ()


@dataclass
class Ident(Expr):
    name: str  # may include `::` segments (foo::bar::baz)


@dataclass
class MemberAccess(Expr):
    obj: Expr
    field: str


@dataclass
class Call(Expr):
    callee: Expr
    args: list[Expr] = field(default_factory=list)
    type_args: list[TypeRef] = field(default_factory=list)  # for .into::<T>(), .returning::<T>()
    # Keyword arguments — `name = expr` inside the call's parens. Same shape
    # as annotation kwargs. Used by built-ins like `pivot::sum(source=...,
    # rows=..., col=..., over=..., value=...)` where positional order would
    # be brittle.
    kwargs: dict[str, Expr] = field(default_factory=dict)


@dataclass
class BinOp(Expr):
    op: str  # +, -, *, /, ==, !=, <, <=, >, >=, &&, ||, %
    left: Expr
    right: Expr


@dataclass
class UnaryOp(Expr):
    op: str  # -, !
    operand: Expr


@dataclass
class QuestionMark(Expr):
    """expr? — propagation operator"""
    inner: Expr


@dataclass
class StructLit(Expr):
    """Foo { a: 1, b: 2 } — record literal."""
    type_name: str
    fields: list["FieldInit"] = field(default_factory=list)


@dataclass
class FieldInit:
    loc: Loc
    name: str
    value: Expr


@dataclass
class SqlBlock(Expr):
    """sql!{ ... } — raw SQL text with extracted bind names."""
    sql: str  # the raw SQL text between the braces
    binds: list[str] = field(default_factory=list)  # the :name binds referenced
    is_dml: bool = False  # write (insert/update/delete/merge) vs read (select)
    has_returning: bool = False


@dataclass
class IfExpr(Expr):
    cond: Expr
    then_body: list["Stmt"] = field(default_factory=list)
    else_body: Optional[list["Stmt"]] = None


@dataclass
class MatchExpr(Expr):
    scrutinee: Expr
    arms: list["MatchArm"] = field(default_factory=list)


@dataclass
class MatchArm:
    loc: Loc
    pattern: Pattern
    body: Union[Expr, list["Stmt"]]


@dataclass
class OkExpr(Expr):
    inner: Expr  # Ok(x)


@dataclass
class ErrExpr(Expr):
    inner: Expr  # Err(x)


@dataclass
class SomeExpr(Expr):
    inner: Expr  # Some(x)


@dataclass
class NoneExpr(Expr):
    pass  # None


@dataclass
class ListLit(Expr):
    """A list literal `[e1, e2, ...]`. Element type is taken from context
    (the `let` annotation or the surrounding expression's expected type)."""
    elements: list[Expr] = field(default_factory=list)


@dataclass
class TupleLit(Expr):
    """A parenthesized tuple `(a, b, c)`. Only meaningful in annotation
    keyword-argument position today — e.g., `@parallel(order = (col1, col2))`.
    A single `(x)` is parsed as a grouped expression, not a 1-tuple."""
    elements: list[Expr] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass
class Stmt:
    loc: Loc


@dataclass
class LetStmt(Stmt):
    name: str
    type_annot: Optional[TypeRef] = None
    value: Optional[Expr] = None
    mutable: bool = False  # var vs let


@dataclass
class AssignStmt(Stmt):
    target: Expr  # Ident or MemberAccess
    value: Expr


@dataclass
class ReturnStmt(Stmt):
    value: Optional[Expr] = None


@dataclass
class IfStmt(Stmt):
    cond: Expr
    then_body: list[Stmt] = field(default_factory=list)
    else_body: Optional[list[Stmt]] = None


@dataclass
class ForStmt(Stmt):
    var_name: str
    iterable: Expr
    body: list[Stmt] = field(default_factory=list)


@dataclass
class ForallStmt(Stmt):
    """`forall n in nums { sql!{ ... :n ... } }` — bulk DML over a list.

    Body must contain exactly one DML `sql!{}` statement; lowers to PL/SQL
    `FORALL i IN list.FIRST .. list.LAST <dml>`.
    """
    var_name: str
    iterable: Expr
    body: list[Stmt] = field(default_factory=list)


@dataclass
class MatchStmt(Stmt):
    scrutinee: Expr
    arms: list[MatchArm] = field(default_factory=list)


@dataclass
class TransactionStmt(Stmt):
    body: list[Stmt] = field(default_factory=list)


@dataclass
class ExprStmt(Stmt):
    expr: Expr


@dataclass
class YieldStmt(Stmt):
    """`yield expr;` — only valid inside an @pipelined fn. Lowers to PL/SQL
    `PIPE ROW(<obj_constructor>(...));`."""
    value: Expr


@dataclass
class PipelineExpr(Expr):
    """`source |> target` — pipe a SQL source through a @pipelined fn.

    Lowers to `select * from table(<target>(cursor(<source>)))` (when source is
    sql!{}) and back to a SQL iterator. If target is itself a method call like
    `collect()`, it can apply to a previous pipeline stage.
    """
    source: Expr
    target: Expr


# ---------------------------------------------------------------------------
# Top-level items
# ---------------------------------------------------------------------------


@dataclass
class Annotation:
    loc: Loc
    name: str  # @name; for nested like @result_cache.this isn't supported in v0
    args: list[Expr] = field(default_factory=list)  # positional
    kwargs: dict[str, Expr] = field(default_factory=dict)  # named


@dataclass
class Param:
    loc: Loc
    name: str
    type_ref: TypeRef
    # Parameter mode: "in" (default — value is read-only inside the body),
    # "out" (caller variable is written), or "inout" (read-and-write).
    # Lowered to PL/SQL `IN` / `OUT` / `IN OUT` respectively.
    mode: str = "in"


@dataclass
class Item:
    loc: Loc
    annotations: list[Annotation] = field(default_factory=list)
    is_pub: bool = False


@dataclass
class FnDef(Item):
    name: str = ""
    params: list[Param] = field(default_factory=list)
    return_type: Optional[TypeRef] = None
    body: list[Stmt] = field(default_factory=list)
    finally_body: Optional[list[Stmt]] = None  # `fn ... { ... } finally { ... }`
    # `unsafe fn` — gates dynamic-SQL features (exec_dyn, @touches, @binds).
    # A non-unsafe fn carrying those is a compile-time error.
    is_unsafe: bool = False


@dataclass
class RecordDef(Item):
    name: str = ""
    fields: list["FieldDef"] = field(default_factory=list)


@dataclass
class FieldDef:
    loc: Loc
    name: str
    type_ref: TypeRef


@dataclass
class ErrorDef(Item):
    name: str = ""
    fields: list[FieldDef] = field(default_factory=list)
    # Disposition category (set by @skip / @propagate / @panic annotation;
    # default propagate). Determines the SQLCODE range the error gets when
    # lowered, and whether `@retry` will catch it.
    #   propagate (default) — caller must handle via Result<T, E>
    #   skip                — best-effort; log + swallow when uncaught
    #   panic               — invariant violation; never caught by retry/skip
    category: str = "propagate"


@dataclass
class ImportStmt(Item):
    path: str = ""  # e.g. std::log


@dataclass
class EnumDef(Item):
    """`pub enum Foo { A, B = "b-val", C }` — a finite set of named text
    variants. Each variant has an implicit string value equal to its name
    (uppercase) unless overridden with `= "..."`.

    Lowers to a set of `CONSTANT VARCHAR2(...) := '<value>'` declarations
    in the package spec, one per variant. References as `Foo::A` lower to
    `pkg.foo_a` (the constant).
    """
    name: str = ""
    variants: list["EnumVariant"] = field(default_factory=list)


@dataclass
class EnumVariant:
    loc: Loc
    name: str
    value: Optional[str] = None  # explicit text value, or None → use name


@dataclass
class SequenceDef(Item):
    """`pub seq employee_id_seq;` — declares an external Oracle sequence.

    pell does not emit DDL for sequences (the user creates them via
    `CREATE SEQUENCE …`). The declaration simply registers the name so
    that `name.nextval` / `name.currval` read as bare references in
    PL/SQL instead of getting the `l_` local-variable prefix.

    Qualified names are allowed (`hr::employees_seq`) and lower to
    `hr.employees_seq.nextval`.
    """
    name: str = ""


# ---------------------------------------------------------------------------
# `type` and `sealed type` (§5.2)
# ---------------------------------------------------------------------------


@dataclass
class MethodDef:
    """Member function inside a `type` or `sealed type`.

    `is_abstract=True` means the method has only a signature (body is empty)
    and every case in a sealed hierarchy must implement it.
    `is_map=True` means this is the type's MAP method (`map fn rank()`); at
    most one per type, must return a primitive comparable type.
    `is_constructor=True` means the method is named `new` and acts as a
    smart constructor (returns `Self` or `Result<Self, _>`).
    """
    loc: Loc
    name: str
    params: list[Param] = field(default_factory=list)
    return_type: Optional[TypeRef] = None
    body: list[Stmt] = field(default_factory=list)
    is_abstract: bool = False
    is_map: bool = False
    is_constructor: bool = False
    is_overriding: bool = False  # set by emitter when method overrides a parent's
    annotations: list["Annotation"] = field(default_factory=list)


@dataclass
class TypeDef(Item):
    """`pub type T { fields; methods; }` — Oracle object type with member fns."""
    name: str = ""
    fields: list[FieldDef] = field(default_factory=list)
    methods: list[MethodDef] = field(default_factory=list)


@dataclass
class CaseDef:
    """A `case` inside a `sealed type`. Holds its own fields + method overrides."""
    loc: Loc
    name: str
    fields: list[FieldDef] = field(default_factory=list)
    methods: list[MethodDef] = field(default_factory=list)


@dataclass
class SealedTypeDef(Item):
    """`pub sealed type T { fields; methods; case C1 { ... } case C2 { ... } }`.

    Parent-level methods with bodies are inherited; with no body they are
    abstract and every case must implement them. Parent-level fields are
    inherited by every case.
    """
    name: str = ""
    fields: list[FieldDef] = field(default_factory=list)
    methods: list[MethodDef] = field(default_factory=list)
    cases: list[CaseDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# `aggregate` (§5.3)
# ---------------------------------------------------------------------------


@dataclass
class AggregateDef(Item):
    """`pub aggregate name(arg: T) -> R { state { ... } step ... merge ... finish ... }`.

    Compiles to an ODCIAggregate object type plus a CREATE FUNCTION ... AGGREGATE USING.
    `@parallel` annotation gates PARALLEL_ENABLE.
    """
    name: str = ""
    params: list[Param] = field(default_factory=list)  # arg(s) to step
    return_type: Optional[TypeRef] = None              # finish() return type
    state_fields: list[FieldDef] = field(default_factory=list)
    state_defaults: list[Expr] = field(default_factory=list)  # parallel to state_fields
    step_body: list[Stmt] = field(default_factory=list)
    step_params: list[Param] = field(default_factory=list)    # same as params for now
    merge_body: Optional[list[Stmt]] = None
    merge_other_name: str = "other"  # the name bound for the other Self
    finish_body: list[Stmt] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module (the root)
# ---------------------------------------------------------------------------


@dataclass
class Module:
    loc: Loc
    name: str  # e.g. hr.employees
    items: list[Item] = field(default_factory=list)

    @property
    def schema(self) -> Optional[str]:
        """Schema name: the first dotted node of the module path, or None for
        single-node modules.
        e.g.: `module hr_app.employees` → 'hr_app'
              `module foo`              → None
        """
        parts = self.name.split(".")
        return parts[0] if len(parts) >= 2 else None

    @property
    def package_name(self) -> str:
        """The PL/SQL package name *within its schema*. For multi-node module
        names the first node is the schema and is stripped; the remainder is
        mangled with underscores.
            `hr_app.employees`       → 'employees'
            `hr_app.shared.utils`    → 'shared_utils'
            `foo`                    → 'foo'
        """
        parts = self.name.split(".")
        if len(parts) >= 2:
            return "_".join(parts[1:])
        return parts[0]

    @property
    def qualified_name(self) -> str:
        """Schema-qualified form for use in PL/SQL CREATE statements.
            `hr_app.employees`  →  'hr_app.employees'
            `foo`               →  'foo'  (unqualified — current schema)
        """
        return f"{self.schema}.{self.package_name}" if self.schema else self.package_name
