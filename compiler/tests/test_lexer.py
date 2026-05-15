"""Lexer tests."""
import pytest
from pell.lexer import tokenize, LexError


def kinds(src):
    return [t.kind for t in tokenize(src) if t.kind != "EOF"]


def test_keywords_and_idents():
    assert kinds("module foo;") == ["KW_MODULE", "IDENT", "SEMI"]


def test_module_with_dotted_name():
    toks = tokenize("module hr.employees;")
    assert toks[0].kind == "KW_MODULE"
    assert toks[1].value == "hr"
    assert toks[2].kind == "DOT"
    assert toks[3].value == "employees"


def test_fn_signature():
    toks = tokenize("fn greet(name: text) -> Unit {}")
    kk = [t.kind for t in toks if t.kind != "EOF"]
    assert kk == [
        "KW_FN", "IDENT", "LPAREN", "IDENT", "COLON", "IDENT",
        "RPAREN", "ARROW", "IDENT", "LBRACE", "RBRACE",
    ]


def test_string_literal_basic():
    toks = tokenize('let s = "hello";')
    string_tok = next(t for t in toks if t.kind == "STRING")
    assert string_tok.value == "hello"


def test_string_escapes():
    toks = tokenize(r'let s = "a\nb\\c";')
    s = next(t for t in toks if t.kind == "STRING")
    assert s.value == "a\nb\\c"


def test_unterminated_string():
    with pytest.raises(LexError):
        tokenize('let s = "no end')


def test_number_literal():
    toks = tokenize("let x = 3.14;")
    n = next(t for t in toks if t.kind == "NUMBER")
    assert n.value == "3.14"


def test_compound_operators():
    toks = tokenize("a == b && c != d || e <= f")
    ops = [t.kind for t in toks if t.kind not in ("IDENT", "EOF")]
    assert ops == ["EQEQ", "AMPAMP", "BANGEQ", "PIPEPIPE", "LE"]


def test_colon_colon():
    toks = tokenize("std::log::info")
    kk = [t.kind for t in toks if t.kind != "EOF"]
    assert kk == ["IDENT", "COLONCOLON", "IDENT", "COLONCOLON", "IDENT"]


def test_sql_block_basic():
    toks = tokenize("sql! { select 1 from dual }")
    assert toks[0].kind == "SQL_BLOCK"
    assert "select 1 from dual" in toks[0].value


def test_sql_block_no_space():
    toks = tokenize("sql!{select 1 from dual}")
    assert toks[0].kind == "SQL_BLOCK"


def test_sql_block_with_braces_in_string():
    toks = tokenize("sql! { select '}}' from dual }")
    assert toks[0].kind == "SQL_BLOCK"
    assert "}}" in toks[0].value


def test_sql_block_with_binds():
    toks = tokenize("sql! { select * from t where id = :id and x = :y }")
    assert toks[0].kind == "SQL_BLOCK"
    assert ":id" in toks[0].value
    assert ":y" in toks[0].value


def test_line_comment_ignored():
    toks = tokenize("// hello\nfn f() {}")
    assert toks[0].kind == "KW_FN"


def test_block_comment_ignored():
    toks = tokenize("/* hello */ fn f() {}")
    assert toks[0].kind == "KW_FN"
