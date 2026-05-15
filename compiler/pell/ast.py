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
class MatchStmt(Stmt):
    scrutinee: Expr
    arms: list[MatchArm] = field(default_factory=list)


@dataclass
class TransactionStmt(Stmt):
    body: list[Stmt] = field(default_factory=list)


@dataclass
class ExprStmt(Stmt):
    expr: Expr


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


@dataclass
class ImportStmt(Item):
    path: str = ""  # e.g. std::log


# ---------------------------------------------------------------------------
# Module (the root)
# ---------------------------------------------------------------------------


@dataclass
class Module:
    loc: Loc
    name: str  # e.g. hr.employees
    items: list[Item] = field(default_factory=list)

    @property
    def package_name(self) -> str:
        """The PL/SQL package name (mangled): hr.employees -> hr_employees."""
        return self.name.replace(".", "_")
