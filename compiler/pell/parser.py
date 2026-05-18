"""Recursive-descent parser for pell.

Consumes a token stream from `lexer.tokenize` and produces a Module AST.
"""

from __future__ import annotations

import re
from typing import Optional

from . import ast as A
from .lexer import Token, tokenize


class ParseError(Exception):
    def __init__(self, msg: str, loc: A.Loc):
        super().__init__(f"{loc}: {msg}")
        self.loc = loc
        self.msg = msg


# Bind extraction inside sql!{} text
_BIND_RE = re.compile(r"(?<![A-Za-z0-9_]):([A-Za-z_][A-Za-z0-9_]*)")


def _extract_binds_and_kind(sql_text: str) -> tuple[list[str], bool, bool]:
    """Strip SQL string literals and comments before scanning for :binds.

    Returns (binds, is_dml, has_returning).
    """
    stripped = []
    i = 0
    while i < len(sql_text):
        ch = sql_text[i]
        # line comment
        if sql_text.startswith("--", i):
            j = sql_text.find("\n", i)
            i = len(sql_text) if j == -1 else j
            continue
        # single-quoted string
        if ch == "'":
            i += 1
            while i < len(sql_text) and sql_text[i] != "'":
                if sql_text[i] == "\\":
                    i += 1
                i += 1
            if i < len(sql_text):
                i += 1
            stripped.append("''")
            continue
        # double-quoted identifier
        if ch == '"':
            i += 1
            while i < len(sql_text) and sql_text[i] != '"':
                i += 1
            if i < len(sql_text):
                i += 1
            stripped.append('""')
            continue
        stripped.append(ch)
        i += 1
    clean = "".join(stripped)
    binds = list(dict.fromkeys(m.group(1) for m in _BIND_RE.finditer(clean)))
    # detect kind
    first_word_match = re.match(r"\s*(\w+)", clean)
    kind_word = (first_word_match.group(1) if first_word_match else "").lower()
    is_dml = kind_word in {"insert", "update", "delete", "merge"}
    has_returning = bool(re.search(r"\breturning\b", clean, re.IGNORECASE)) if is_dml else False
    return binds, is_dml, has_returning


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.pos = 0

    # ---- token-level helpers --------------------------------------------

    def _peek(self, offset: int = 0) -> Token:
        i = self.pos + offset
        return self.toks[i] if i < len(self.toks) else self.toks[-1]

    def _at(self, *kinds: str) -> bool:
        return self._peek().kind in kinds

    def _eat(self, *kinds: str) -> Optional[Token]:
        if self._peek().kind in kinds:
            t = self._peek()
            self.pos += 1
            return t
        return None

    def _expect(self, kind: str, msg: Optional[str] = None) -> Token:
        t = self._eat(kind)
        if t is None:
            cur = self._peek()
            raise ParseError(
                msg or f"expected {kind}, got {cur.kind} ({cur.value!r})",
                cur.loc,
            )
        return t

    def _at_keyword(self, *kws: str) -> bool:
        cur = self._peek()
        return cur.kind.startswith("KW_") and cur.value in kws

    def _eat_keyword(self, *kws: str) -> Optional[Token]:
        if self._at_keyword(*kws):
            t = self._peek()
            self.pos += 1
            return t
        return None

    # ---- top-level ------------------------------------------------------

    def parse_module(self) -> A.Module:
        # module foo.bar;
        self._eat_keyword("module") or self._expect("KW_MODULE", "expected `module` at top of file")
        # Above is a little redundant; simpler form:
        first = self.toks[0]
        loc = first.loc
        name = self._parse_dotted_ident()
        self._expect("SEMI", "expected `;` after module name")
        items: list[A.Item] = []
        while not self._at("EOF"):
            items.append(self._parse_item())
        return A.Module(loc=loc, name=name, items=items)

    def _parse_dotted_ident(self) -> str:
        parts = [self._expect("IDENT").value]
        while self._eat("DOT"):
            parts.append(self._expect("IDENT").value)
        return ".".join(parts)

    def _parse_qualified_ident(self) -> str:
        """foo::bar::baz or just foo"""
        parts = [self._expect("IDENT").value]
        while self._eat("COLONCOLON"):
            parts.append(self._expect("IDENT").value)
        return "::".join(parts)

    def _parse_item(self) -> A.Item:
        annotations = self._parse_annotations()
        is_pub = bool(self._eat_keyword("pub"))
        cur = self._peek()
        if self._at_keyword("import"):
            return self._parse_import(annotations, is_pub)
        if self._at_keyword("fn"):
            return self._parse_fn_def(annotations, is_pub)
        if self._at_keyword("record"):
            return self._parse_record_def(annotations, is_pub)
        if self._at_keyword("error"):
            return self._parse_error_def(annotations, is_pub)
        if self._at_keyword("sealed"):
            return self._parse_sealed_type_def(annotations, is_pub)
        if self._at_keyword("type"):
            return self._parse_type_def(annotations, is_pub)
        if self._at_keyword("aggregate"):
            return self._parse_aggregate_def(annotations, is_pub)
        if self._at_keyword("seq"):
            return self._parse_seq_def(annotations, is_pub)
        raise ParseError(
            f"expected item (fn / record / error / import / type / sealed type / aggregate), got {cur.value!r}",
            cur.loc,
        )

    def _parse_annotations(self) -> list[A.Annotation]:
        out: list[A.Annotation] = []
        while self._at("AT"):
            at = self._eat("AT")
            assert at is not None
            name_tok = self._expect("IDENT", "expected annotation name after @")
            args: list[A.Expr] = []
            kwargs: dict[str, A.Expr] = {}
            if self._eat("LPAREN"):
                while not self._at("RPAREN"):
                    if (self._peek().kind == "IDENT"
                            and self._peek(1).kind == "EQ"):
                        kname = self._expect("IDENT").value
                        self._expect("EQ")
                        kwargs[kname] = self._parse_expr()
                    else:
                        args.append(self._parse_expr())
                    if not self._eat("COMMA"):
                        break
                self._expect("RPAREN")
            out.append(A.Annotation(loc=at.loc, name=name_tok.value, args=args, kwargs=kwargs))
        return out

    def _parse_seq_def(self, annotations: list[A.Annotation], is_pub: bool) -> A.SequenceDef:
        kw = self._expect("KW_SEQ")
        name = self._parse_qualified_ident()
        self._expect("SEMI")
        return A.SequenceDef(loc=kw.loc, annotations=annotations, is_pub=is_pub, name=name)

    def _parse_import(self, annotations: list[A.Annotation], is_pub: bool) -> A.ImportStmt:
        kw = self._expect("KW_IMPORT")
        path = self._parse_qualified_ident()
        self._expect("SEMI")
        return A.ImportStmt(loc=kw.loc, annotations=annotations, is_pub=is_pub, path=path)

    def _parse_fn_def(self, annotations: list[A.Annotation], is_pub: bool) -> A.FnDef:
        kw = self._expect("KW_FN")
        name = self._expect("IDENT").value
        self._expect("LPAREN")
        params: list[A.Param] = []
        while not self._at("RPAREN"):
            ploc = self._peek().loc
            pname = self._expect("IDENT").value
            self._expect("COLON")
            ptype = self._parse_type()
            params.append(A.Param(loc=ploc, name=pname, type_ref=ptype))
            if not self._eat("COMMA"):
                break
        self._expect("RPAREN")
        return_type: Optional[A.TypeRef] = None
        if self._eat("ARROW"):
            return_type = self._parse_type()
        body = self._parse_block()
        finally_body: Optional[list[A.Stmt]] = None
        if self._eat_keyword("finally"):
            finally_body = self._parse_block()
        return A.FnDef(
            loc=kw.loc,
            annotations=annotations,
            is_pub=is_pub,
            name=name,
            params=params,
            return_type=return_type,
            body=body,
            finally_body=finally_body,
        )

    def _parse_record_def(self, annotations: list[A.Annotation], is_pub: bool) -> A.RecordDef:
        kw = self._expect("KW_RECORD")
        name = self._expect("IDENT").value
        self._expect("LBRACE")
        fields = self._parse_field_defs()
        self._expect("RBRACE")
        return A.RecordDef(loc=kw.loc, annotations=annotations, is_pub=is_pub, name=name, fields=fields)

    def _parse_error_def(self, annotations: list[A.Annotation], is_pub: bool) -> A.ErrorDef:
        kw = self._expect("KW_ERROR")
        name = self._expect("IDENT").value
        fields: list[A.FieldDef] = []
        if self._eat("LBRACE"):
            fields = self._parse_field_defs()
            self._expect("RBRACE")
        else:
            # zero-payload error: `error Foo;`
            self._expect("SEMI")
        return A.ErrorDef(loc=kw.loc, annotations=annotations, is_pub=is_pub, name=name, fields=fields)

    # ---- type / sealed type / aggregate ---------------------------------

    def _parse_type_def(self, annotations: list[A.Annotation], is_pub: bool) -> A.TypeDef:
        kw = self._expect("KW_TYPE")
        name = self._expect("IDENT").value
        fields, methods = self._parse_type_body(allow_cases=False)
        return A.TypeDef(
            loc=kw.loc,
            annotations=annotations,
            is_pub=is_pub,
            name=name,
            fields=fields,
            methods=methods,
        )

    def _parse_sealed_type_def(self, annotations: list[A.Annotation], is_pub: bool) -> A.SealedTypeDef:
        kw = self._expect("KW_SEALED")
        self._expect("KW_TYPE", "expected `type` after `sealed`")
        name = self._expect("IDENT").value
        fields, methods, cases = self._parse_sealed_body()
        return A.SealedTypeDef(
            loc=kw.loc,
            annotations=annotations,
            is_pub=is_pub,
            name=name,
            fields=fields,
            methods=methods,
            cases=cases,
        )

    def _parse_type_body(self, *, allow_cases: bool) -> tuple[list[A.FieldDef], list[A.MethodDef]]:
        """Parse a `{ field; ... method ... }` body for `type`. Returns (fields, methods)."""
        self._expect("LBRACE")
        fields: list[A.FieldDef] = []
        methods: list[A.MethodDef] = []
        while not self._at("RBRACE", "EOF"):
            if self._at("AT") or self._at_keyword("fn") or self._is_at_map_fn():
                methods.append(self._parse_method_def())
                continue
            if allow_cases and self._at_keyword("case"):
                # Caller should have routed to _parse_sealed_body; reject here.
                raise ParseError("`case` only allowed inside `sealed type`", self._peek().loc)
            # otherwise: field declaration `IDENT : TYPE ;`
            floc = self._peek().loc
            fname = self._expect("IDENT").value
            self._expect("COLON", "expected `:` after field name")
            ftype = self._parse_type()
            self._expect("SEMI", "expected `;` after type-body field declaration")
            fields.append(A.FieldDef(loc=floc, name=fname, type_ref=ftype))
        self._expect("RBRACE")
        return fields, methods

    def _parse_sealed_body(self) -> tuple[list[A.FieldDef], list[A.MethodDef], list[A.CaseDef]]:
        self._expect("LBRACE")
        fields: list[A.FieldDef] = []
        methods: list[A.MethodDef] = []
        cases: list[A.CaseDef] = []
        while not self._at("RBRACE", "EOF"):
            if self._at_keyword("case"):
                cases.append(self._parse_case_def())
                continue
            if self._at("AT") or self._at_keyword("fn") or self._is_at_map_fn():
                methods.append(self._parse_method_def())
                continue
            floc = self._peek().loc
            fname = self._expect("IDENT").value
            self._expect("COLON", "expected `:` after field name")
            ftype = self._parse_type()
            self._expect("SEMI", "expected `;` after type-body field declaration")
            fields.append(A.FieldDef(loc=floc, name=fname, type_ref=ftype))
        self._expect("RBRACE")
        return fields, methods, cases

    def _parse_case_def(self) -> A.CaseDef:
        kw = self._expect("KW_CASE")
        name = self._expect("IDENT", "expected case name after `case`").value
        # Optional fields block `{ field; ... }` followed by either `;` or `{ methods }`.
        fields: list[A.FieldDef] = []
        methods: list[A.MethodDef] = []
        # Two forms:
        #   case Circle { radius: number };
        #   case Circle { radius: number } { fn area() -> number { ... } }
        # Detect by seeing if the first `{` body contains methods or is followed by another `{`.
        if self._eat("LBRACE"):
            while not self._at("RBRACE", "EOF"):
                # could be a field or — in a "method-only" case body — directly methods
                if self._at("AT") or self._at_keyword("fn") or self._is_at_map_fn():
                    methods.append(self._parse_method_def())
                    continue
                floc = self._peek().loc
                fname = self._expect("IDENT").value
                self._expect("COLON")
                ftype = self._parse_type()
                fields.append(A.FieldDef(loc=floc, name=fname, type_ref=ftype))
                # accept `;` or `,` between fields; trailing separator optional.
                if not (self._eat("SEMI") or self._eat("COMMA")):
                    break
            self._expect("RBRACE")
            # If methods are in a SEPARATE following block, parse them too.
            if self._eat("LBRACE"):
                while not self._at("RBRACE", "EOF"):
                    if self._at("AT") or self._at_keyword("fn") or self._is_at_map_fn():
                        methods.append(self._parse_method_def())
                    else:
                        cur = self._peek()
                        raise ParseError(
                            f"expected method in case `{name}` body, got {cur.value!r}",
                            cur.loc,
                        )
                self._expect("RBRACE")
        # Trailing `;` is optional; many cases will end at `}`.
        self._eat("SEMI")
        return A.CaseDef(loc=kw.loc, name=name, fields=fields, methods=methods)

    def _is_at_map_fn(self) -> bool:
        """True if current token is IDENT `map` and next is `fn` keyword."""
        return (
            self._peek().kind == "IDENT"
            and self._peek().value == "map"
            and self._peek(1).kind == "KW_FN"
        )

    def _parse_method_def(self) -> A.MethodDef:
        annotations = self._parse_annotations()
        is_map = False
        # `map fn` modifier
        if self._is_at_map_fn():
            self.pos += 1  # consume `map`
            is_map = True
        kw = self._expect("KW_FN", "expected `fn` to start method definition")
        name_tok = self._expect("IDENT", "expected method name")
        name = name_tok.value
        is_constructor = (name == "new")
        self._expect("LPAREN")
        params: list[A.Param] = []
        while not self._at("RPAREN"):
            ploc = self._peek().loc
            pname = self._expect("IDENT").value
            self._expect("COLON")
            ptype = self._parse_type()
            params.append(A.Param(loc=ploc, name=pname, type_ref=ptype))
            if not self._eat("COMMA"):
                break
        self._expect("RPAREN")
        return_type: Optional[A.TypeRef] = None
        if self._eat("ARROW"):
            return_type = self._parse_type()
        # Abstract methods end at `;`; concrete methods have a `{ ... }` body.
        body: list[A.Stmt] = []
        is_abstract = False
        if self._eat("SEMI"):
            is_abstract = True
        else:
            body = self._parse_block()
        return A.MethodDef(
            loc=kw.loc,
            name=name,
            params=params,
            return_type=return_type,
            body=body,
            is_abstract=is_abstract,
            is_map=is_map,
            is_constructor=is_constructor,
            annotations=annotations,
        )

    def _parse_aggregate_def(self, annotations: list[A.Annotation], is_pub: bool) -> A.AggregateDef:
        kw = self._expect("KW_AGGREGATE")
        name = self._expect("IDENT").value
        # signature: (param : T, ...) -> R
        self._expect("LPAREN")
        params: list[A.Param] = []
        while not self._at("RPAREN"):
            ploc = self._peek().loc
            pname = self._expect("IDENT").value
            self._expect("COLON")
            ptype = self._parse_type()
            params.append(A.Param(loc=ploc, name=pname, type_ref=ptype))
            if not self._eat("COMMA"):
                break
        self._expect("RPAREN")
        return_type: Optional[A.TypeRef] = None
        if self._eat("ARROW"):
            return_type = self._parse_type()
        # body: `state { ... } step(...) { ... } merge(...) { ... }? finish() -> T { ... }`
        self._expect("LBRACE", "expected `{` to start aggregate body")
        state_fields: list[A.FieldDef] = []
        state_defaults: list[A.Expr] = []
        step_body: list[A.Stmt] = []
        step_params: list[A.Param] = list(params)  # default: same as outer params
        merge_body: Optional[list[A.Stmt]] = None
        merge_other_name = "other"
        finish_body: list[A.Stmt] = []
        saw_state = saw_step = saw_finish = False
        while not self._at("RBRACE", "EOF"):
            cur = self._peek()
            if cur.kind == "IDENT" and cur.value == "state":
                if saw_state:
                    raise ParseError("duplicate `state` block in aggregate", cur.loc)
                saw_state = True
                self.pos += 1
                self._expect("LBRACE")
                while not self._at("RBRACE", "EOF"):
                    floc = self._peek().loc
                    fname = self._expect("IDENT").value
                    self._expect("COLON")
                    ftype = self._parse_type()
                    self._expect("EQ", "state field must have a `= default` expression")
                    fdefault = self._parse_expr()
                    self._expect("SEMI")
                    state_fields.append(A.FieldDef(loc=floc, name=fname, type_ref=ftype))
                    state_defaults.append(fdefault)
                self._expect("RBRACE")
                continue
            if cur.kind == "IDENT" and cur.value == "step":
                if saw_step:
                    raise ParseError("duplicate `step` block in aggregate", cur.loc)
                saw_step = True
                self.pos += 1
                # optional param list overrides outer params
                if self._eat("LPAREN"):
                    sp: list[A.Param] = []
                    while not self._at("RPAREN"):
                        ploc = self._peek().loc
                        pname = self._expect("IDENT").value
                        self._expect("COLON")
                        ptype = self._parse_type()
                        sp.append(A.Param(loc=ploc, name=pname, type_ref=ptype))
                        if not self._eat("COMMA"):
                            break
                    self._expect("RPAREN")
                    step_params = sp
                step_body = self._parse_block()
                continue
            if cur.kind == "IDENT" and cur.value == "merge":
                if merge_body is not None:
                    raise ParseError("duplicate `merge` block in aggregate", cur.loc)
                self.pos += 1
                self._expect("LPAREN")
                if not self._at("RPAREN"):
                    merge_other_name = self._expect("IDENT").value
                    self._expect("COLON")
                    _ = self._parse_type()  # must be Self; we accept whatever and trust the user
                self._expect("RPAREN")
                merge_body = self._parse_block()
                continue
            if cur.kind == "IDENT" and cur.value == "finish":
                if saw_finish:
                    raise ParseError("duplicate `finish` block in aggregate", cur.loc)
                saw_finish = True
                self.pos += 1
                self._expect("LPAREN")
                self._expect("RPAREN")
                if self._eat("ARROW"):
                    return_type = self._parse_type()  # may override outer
                finish_body = self._parse_block()
                continue
            raise ParseError(
                f"expected `state` / `step` / `merge` / `finish` in aggregate body, got {cur.value!r}",
                cur.loc,
            )
        self._expect("RBRACE")
        if not saw_state:
            raise ParseError("aggregate is missing a `state {}` block", kw.loc)
        if not saw_step:
            raise ParseError("aggregate is missing a `step(...) {}` block", kw.loc)
        if not saw_finish:
            raise ParseError("aggregate is missing a `finish() -> T {}` block", kw.loc)
        return A.AggregateDef(
            loc=kw.loc,
            annotations=annotations,
            is_pub=is_pub,
            name=name,
            params=params,
            return_type=return_type,
            state_fields=state_fields,
            state_defaults=state_defaults,
            step_body=step_body,
            step_params=step_params,
            merge_body=merge_body,
            merge_other_name=merge_other_name,
            finish_body=finish_body,
        )

    def _parse_field_defs(self) -> list[A.FieldDef]:
        out: list[A.FieldDef] = []
        while not self._at("RBRACE"):
            floc = self._peek().loc
            fname = self._expect("IDENT").value
            self._expect("COLON")
            ftype = self._parse_type()
            out.append(A.FieldDef(loc=floc, name=fname, type_ref=ftype))
            if not self._eat("COMMA"):
                break
        return out

    # ---- types ----------------------------------------------------------

    def _parse_type(self) -> A.TypeRef:
        """type := atom (`|` atom)*  — union when in Result<,> context."""
        first = self._parse_type_atom()
        if self._at("PIPE"):
            variants = [first]
            while self._eat("PIPE"):
                variants.append(self._parse_type_atom())
            return A.ErrorUnionType(loc=first.loc, variants=variants)
        return first

    def _parse_type_atom(self) -> A.TypeRef:
        cur = self._peek()
        if cur.kind == "IDENT":
            name = cur.value
            loc = cur.loc
            self.pos += 1
            base: A.TypeRef
            # generic?
            if self._eat("LT"):
                params = [self._parse_type()]
                while self._eat("COMMA"):
                    params.append(self._parse_type())
                self._expect("GT")
                base = A.GenericType(loc=loc, base=name, params=params)
            elif name in PRIMITIVES:
                base = A.PrimType(loc=loc, name=name)
            else:
                base = A.NamedType(loc=loc, name=name)
            # optional `?`
            while self._eat("QUESTION"):
                base = A.OptionalType(loc=loc, inner=base)
            return base
        if cur.kind == "LPAREN":
            self.pos += 1
            # `()` unit type
            if self._eat("RPAREN"):
                return A.PrimType(loc=cur.loc, name="Unit")
            # otherwise grouped — but we don't need this in v0
            inner = self._parse_type()
            self._expect("RPAREN")
            return inner
        if cur.kind == "BANG":
            # `!` empty/never type
            self.pos += 1
            return A.PrimType(loc=cur.loc, name="Never")
        raise ParseError(f"expected type, got {cur.kind} ({cur.value!r})", cur.loc)

    # ---- statements -----------------------------------------------------

    def _parse_block(self) -> list[A.Stmt]:
        self._expect("LBRACE")
        body: list[A.Stmt] = []
        while not self._at("RBRACE", "EOF"):
            body.append(self._parse_stmt())
        self._expect("RBRACE")
        return body

    def _parse_stmt(self) -> A.Stmt:
        if self._at_keyword("let"):
            return self._parse_let_var(mutable=False)
        if self._at_keyword("var"):
            return self._parse_let_var(mutable=True)
        if self._at_keyword("return"):
            return self._parse_return()
        if self._at_keyword("if"):
            return self._parse_if_stmt()
        if self._at_keyword("for"):
            return self._parse_for_stmt()
        if self._at_keyword("forall"):
            return self._parse_forall_stmt()
        if self._at_keyword("match"):
            return self._parse_match_stmt()
        if self._at_keyword("transaction"):
            return self._parse_transaction_stmt()
        if self._at_keyword("yield"):
            return self._parse_yield_stmt()
        # expression-statement or assignment
        expr = self._parse_expr()
        if self._eat("EQ"):
            rhs = self._parse_expr()
            self._expect("SEMI")
            return A.AssignStmt(loc=expr.loc, target=expr, value=rhs)
        self._expect("SEMI", "expected `;` after statement")
        return A.ExprStmt(loc=expr.loc, expr=expr)

    def _parse_let_var(self, mutable: bool) -> A.LetStmt:
        kw = self._peek()
        self.pos += 1
        name = self._expect("IDENT").value
        type_annot: Optional[A.TypeRef] = None
        if self._eat("COLON"):
            type_annot = self._parse_type()
        value: Optional[A.Expr] = None
        if self._eat("EQ"):
            value = self._parse_expr()
        self._expect("SEMI")
        return A.LetStmt(loc=kw.loc, name=name, type_annot=type_annot, value=value, mutable=mutable)

    def _parse_return(self) -> A.ReturnStmt:
        kw = self._expect("KW_RETURN")
        value: Optional[A.Expr] = None
        if not self._at("SEMI"):
            value = self._parse_expr()
        self._expect("SEMI")
        return A.ReturnStmt(loc=kw.loc, value=value)

    def _parse_if_stmt(self) -> A.IfStmt:
        kw = self._expect("KW_IF")
        cond = self._parse_expr()
        then_body = self._parse_block()
        else_body: Optional[list[A.Stmt]] = None
        if self._eat_keyword("else"):
            if self._at_keyword("if"):
                # else if -> nested IfStmt
                inner = self._parse_if_stmt()
                else_body = [inner]
            else:
                else_body = self._parse_block()
        return A.IfStmt(loc=kw.loc, cond=cond, then_body=then_body, else_body=else_body)

    def _parse_for_stmt(self) -> A.ForStmt:
        kw = self._expect("KW_FOR")
        name = self._expect("IDENT").value
        self._eat_keyword("in") or self._expect("KW_IN")
        iterable = self._parse_expr()
        body = self._parse_block()
        return A.ForStmt(loc=kw.loc, var_name=name, iterable=iterable, body=body)

    def _parse_forall_stmt(self) -> A.ForallStmt:
        kw = self._expect("KW_FORALL")
        name = self._expect("IDENT").value
        self._eat_keyword("in") or self._expect("KW_IN")
        iterable = self._parse_expr()
        body = self._parse_block()
        return A.ForallStmt(loc=kw.loc, var_name=name, iterable=iterable, body=body)

    def _parse_match_stmt(self) -> A.MatchStmt:
        kw = self._expect("KW_MATCH")
        scrutinee = self._parse_expr()
        self._expect("LBRACE")
        arms: list[A.MatchArm] = []
        while not self._at("RBRACE"):
            arms.append(self._parse_match_arm())
            self._eat("COMMA")
        self._expect("RBRACE")
        return A.MatchStmt(loc=kw.loc, scrutinee=scrutinee, arms=arms)

    def _parse_match_arm(self) -> A.MatchArm:
        loc = self._peek().loc
        pat = self._parse_pattern()
        self._expect("ARROW", "expected `->` in match arm")
        if self._at("LBRACE"):
            body = self._parse_block()
        else:
            body = self._parse_expr()
        return A.MatchArm(loc=loc, pattern=pat, body=body)

    def _parse_pattern(self) -> A.Pattern:
        cur = self._peek()
        if cur.kind == "IDENT" and cur.value == "_":
            self.pos += 1
            return A.WildcardPattern(loc=cur.loc)
        if self._at_keyword("None"):
            self.pos += 1
            return A.VariantPattern(loc=cur.loc, name="None")
        if self._at_keyword("Some", "Ok", "Err"):
            name = cur.value
            loc = cur.loc
            self.pos += 1
            args: list[A.Pattern] = []
            fields: list[A.FieldPattern] = []
            rest = False
            if self._eat("LPAREN"):
                # could be Some(x), Ok(v), Err(NotFound { id, .. })
                if not self._at("RPAREN"):
                    args.append(self._parse_pattern())
                    while self._eat("COMMA"):
                        args.append(self._parse_pattern())
                self._expect("RPAREN")
            return A.VariantPattern(loc=loc, name=name, args=args, fields=fields, rest=rest)
        if cur.kind == "IDENT":
            name = cur.value
            loc = cur.loc
            self.pos += 1
            # Type { field, ... } struct pattern, or Type(args), or binding?
            if self._eat("LBRACE"):
                fields: list[A.FieldPattern] = []
                rest = False
                while not self._at("RBRACE"):
                    if self._eat("DOTDOT"):
                        rest = True
                        break
                    floc = self._peek().loc
                    fname = self._expect("IDENT").value
                    fpat: Optional[A.Pattern] = None
                    if self._eat("COLON"):
                        fpat = self._parse_pattern()
                    fields.append(A.FieldPattern(loc=floc, name=fname, pattern=fpat))
                    if not self._eat("COMMA"):
                        break
                self._expect("RBRACE")
                return A.VariantPattern(loc=loc, name=name, fields=fields, rest=rest)
            if self._eat("LPAREN"):
                args = []
                if not self._at("RPAREN"):
                    args.append(self._parse_pattern())
                    while self._eat("COMMA"):
                        args.append(self._parse_pattern())
                self._expect("RPAREN")
                return A.VariantPattern(loc=loc, name=name, args=args)
            return A.BindingPattern(loc=loc, name=name)
        if cur.kind in ("NUMBER", "STRING"):
            self.pos += 1
            return A.LiteralPattern(loc=cur.loc, value=cur.value)
        if self._at_keyword("true", "false"):
            self.pos += 1
            return A.LiteralPattern(loc=cur.loc, value=(cur.value == "true"))
        raise ParseError(f"expected pattern, got {cur.kind} ({cur.value!r})", cur.loc)

    def _parse_yield_stmt(self) -> A.YieldStmt:
        kw = self._expect("KW_YIELD")
        value = self._parse_expr()
        self._expect("SEMI")
        return A.YieldStmt(loc=kw.loc, value=value)

    def _parse_transaction_stmt(self) -> A.TransactionStmt:
        kw = self._expect("KW_TRANSACTION")
        body = self._parse_block()
        return A.TransactionStmt(loc=kw.loc, body=body)

    # ---- expressions ----------------------------------------------------

    def _parse_expr(self) -> A.Expr:
        return self._parse_pipeline()

    def _parse_pipeline(self) -> A.Expr:
        left = self._parse_logical_or()
        while self._eat("PIPEGT"):
            right = self._parse_logical_or()
            left = A.PipelineExpr(loc=left.loc, source=left, target=right)
        return left

    def _parse_logical_or(self) -> A.Expr:
        left = self._parse_logical_and()
        while self._eat("PIPEPIPE"):
            right = self._parse_logical_and()
            left = A.BinOp(loc=left.loc, op="||", left=left, right=right)
        return left

    def _parse_logical_and(self) -> A.Expr:
        left = self._parse_equality()
        while self._eat("AMPAMP"):
            right = self._parse_equality()
            left = A.BinOp(loc=left.loc, op="&&", left=left, right=right)
        return left

    def _parse_equality(self) -> A.Expr:
        left = self._parse_comparison()
        while self._at("EQEQ", "BANGEQ"):
            op = self._peek().value
            self.pos += 1
            right = self._parse_comparison()
            left = A.BinOp(loc=left.loc, op=op, left=left, right=right)
        return left

    def _parse_comparison(self) -> A.Expr:
        left = self._parse_addition()
        while self._at("LT", "LE", "GT", "GE"):
            op = self._peek().value
            self.pos += 1
            right = self._parse_addition()
            left = A.BinOp(loc=left.loc, op=op, left=left, right=right)
        return left

    def _parse_addition(self) -> A.Expr:
        left = self._parse_multiplication()
        while self._at("PLUS", "MINUS"):
            op = self._peek().value
            self.pos += 1
            right = self._parse_multiplication()
            left = A.BinOp(loc=left.loc, op=op, left=left, right=right)
        return left

    def _parse_multiplication(self) -> A.Expr:
        left = self._parse_unary()
        while self._at("STAR", "SLASH", "PERCENT"):
            op = self._peek().value
            self.pos += 1
            right = self._parse_unary()
            left = A.BinOp(loc=left.loc, op=op, left=left, right=right)
        return left

    def _parse_unary(self) -> A.Expr:
        if self._at("MINUS", "BANG"):
            op_tok = self._peek()
            self.pos += 1
            operand = self._parse_unary()
            return A.UnaryOp(loc=op_tok.loc, op=op_tok.value, operand=operand)
        return self._parse_postfix()

    def _parse_postfix(self) -> A.Expr:
        expr = self._parse_primary()
        while True:
            if self._eat("DOT"):
                field = self._expect("IDENT").value
                expr = A.MemberAccess(loc=expr.loc, obj=expr, field=field)
                continue
            if self._eat("LPAREN"):
                args: list[A.Expr] = []
                if not self._at("RPAREN"):
                    args.append(self._parse_expr())
                    while self._eat("COMMA"):
                        args.append(self._parse_expr())
                self._expect("RPAREN")
                expr = A.Call(loc=expr.loc, callee=expr, args=args)
                continue
            # turbofish `::<T, ...>` for type args on methods like .into::<T>() and .returning::<T>()
            if self._at("COLONCOLON") and self._peek(1).kind == "LT":
                self._eat("COLONCOLON")
                self._eat("LT")
                type_args = [self._parse_type()]
                while self._eat("COMMA"):
                    type_args.append(self._parse_type())
                self._expect("GT")
                # the next thing must be a call
                self._expect("LPAREN")
                args = []
                if not self._at("RPAREN"):
                    args.append(self._parse_expr())
                    while self._eat("COMMA"):
                        args.append(self._parse_expr())
                self._expect("RPAREN")
                # if expr is MemberAccess, fold type-args into the Call
                expr = A.Call(loc=expr.loc, callee=expr, args=args, type_args=type_args)
                continue
            if self._eat("QUESTION"):
                expr = A.QuestionMark(loc=expr.loc, inner=expr)
                continue
            break
        return expr

    def _parse_primary(self) -> A.Expr:
        cur = self._peek()
        if cur.kind == "NUMBER":
            self.pos += 1
            return A.NumberLit(loc=cur.loc, value=cur.value)
        if cur.kind == "STRING":
            self.pos += 1
            return A.TextLit(loc=cur.loc, value=cur.value)
        if cur.kind == "KW_TRUE":
            self.pos += 1
            return A.BoolLit(loc=cur.loc, value=True)
        if cur.kind == "KW_FALSE":
            self.pos += 1
            return A.BoolLit(loc=cur.loc, value=False)
        if cur.kind == "KW_NONE":
            self.pos += 1
            return A.NoneExpr(loc=cur.loc)
        if cur.kind == "KW_SELF":
            # `self` reads as a regular identifier in expressions; the emitter
            # knows it refers to the method's receiver and renders it as the
            # PL/SQL `SELF` parameter (which Oracle implicitly binds).
            self.pos += 1
            return A.Ident(loc=cur.loc, name="self")
        if self._at_keyword("Some"):
            self.pos += 1
            self._expect("LPAREN")
            inner = self._parse_expr()
            self._expect("RPAREN")
            return A.SomeExpr(loc=cur.loc, inner=inner)
        if self._at_keyword("Ok"):
            self.pos += 1
            self._expect("LPAREN")
            inner = self._parse_expr() if not self._at("RPAREN") else A.UnitLit(loc=cur.loc)
            self._expect("RPAREN")
            return A.OkExpr(loc=cur.loc, inner=inner)
        if self._at_keyword("Err"):
            self.pos += 1
            self._expect("LPAREN")
            inner = self._parse_expr()
            self._expect("RPAREN")
            return A.ErrExpr(loc=cur.loc, inner=inner)
        if cur.kind == "SQL_BLOCK":
            self.pos += 1
            binds, is_dml, has_returning = _extract_binds_and_kind(cur.value)
            return A.SqlBlock(loc=cur.loc, sql=cur.value, binds=binds, is_dml=is_dml, has_returning=has_returning)
        if cur.kind == "LPAREN":
            self.pos += 1
            if self._eat("RPAREN"):
                return A.UnitLit(loc=cur.loc)
            e = self._parse_expr()
            # `(a, b, c)` → tuple literal. `(x)` alone is a grouped expression.
            if self._at("COMMA"):
                elements = [e]
                while self._eat("COMMA"):
                    if self._at("RPAREN"):
                        break  # trailing comma OK
                    elements.append(self._parse_expr())
                self._expect("RPAREN")
                return A.TupleLit(loc=cur.loc, elements=elements)
            self._expect("RPAREN")
            return e
        if cur.kind == "LBRACKET":
            self.pos += 1
            elements: list[A.Expr] = []
            if not self._at("RBRACKET"):
                elements.append(self._parse_expr())
                while self._eat("COMMA"):
                    if self._at("RBRACKET"):
                        break  # allow trailing comma
                    elements.append(self._parse_expr())
            self._expect("RBRACKET")
            return A.ListLit(loc=cur.loc, elements=elements)
        if cur.kind == "IDENT":
            # could be ident, qualified path, struct literal, or a call
            loc = cur.loc
            parts = [cur.value]
            self.pos += 1
            while self._eat("COLONCOLON"):
                parts.append(self._expect("IDENT").value)
            name = "::".join(parts)
            # struct literal? `Name { field: expr, ... }`
            if self._at("LBRACE") and _looks_like_struct_lit(self):
                self._eat("LBRACE")
                fields: list[A.FieldInit] = []
                while not self._at("RBRACE"):
                    floc = self._peek().loc
                    fname = self._expect("IDENT").value
                    fexpr: A.Expr
                    if self._eat("COLON"):
                        fexpr = self._parse_expr()
                    else:
                        # shorthand `{ x }` = `{ x: x }`
                        fexpr = A.Ident(loc=floc, name=fname)
                    fields.append(A.FieldInit(loc=floc, name=fname, value=fexpr))
                    if not self._eat("COMMA"):
                        break
                self._expect("RBRACE")
                return A.StructLit(loc=loc, type_name=name, fields=fields)
            return A.Ident(loc=loc, name=name)
        if self._at_keyword("if"):
            return self._parse_if_expr()
        if self._at_keyword("match"):
            return self._parse_match_expr()
        raise ParseError(f"expected expression, got {cur.kind} ({cur.value!r})", cur.loc)

    def _parse_if_expr(self) -> A.IfExpr:
        kw = self._expect("KW_IF")
        cond = self._parse_expr()
        then_body = self._parse_block()
        else_body: Optional[list[A.Stmt]] = None
        if self._eat_keyword("else"):
            else_body = self._parse_block()
        return A.IfExpr(loc=kw.loc, cond=cond, then_body=then_body, else_body=else_body)

    def _parse_match_expr(self) -> A.MatchExpr:
        kw = self._expect("KW_MATCH")
        scrut = self._parse_expr()
        self._expect("LBRACE")
        arms: list[A.MatchArm] = []
        while not self._at("RBRACE"):
            arms.append(self._parse_match_arm())
            self._eat("COMMA")
        self._expect("RBRACE")
        return A.MatchExpr(loc=kw.loc, scrutinee=scrut, arms=arms)


PRIMITIVES = {"number", "int", "text", "bool", "date", "timestamp", "interval", "bytes", "json", "Unit"}


def _looks_like_struct_lit(p: Parser) -> bool:
    """Disambiguate `Type { ... }` (struct lit) from a `{ ... }` block.

    Conservative heuristic: only treat as struct lit if the very next pair looks
    like `field :` or `field ,` or `}`. This avoids mis-parsing `if cond { ... }`
    bodies (`cond` is an Ident, then `{` starts the block — we're already past
    the Ident when we get here, so this heuristic kicks in only in expression
    position).
    """
    # peek (1) and (2) past the LBRACE
    if p._peek(1).kind == "RBRACE":  # empty struct lit
        return True
    if p._peek(1).kind == "IDENT" and p._peek(2).kind in ("COLON", "COMMA", "RBRACE"):
        return True
    return False


def parse(source: str, filename: str = "<input>") -> A.Module:
    toks = tokenize(source, filename)
    return Parser(toks).parse_module()
