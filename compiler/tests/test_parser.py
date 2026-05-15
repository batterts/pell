"""Parser tests."""
import pytest

from pell import ast as A
from pell.parser import parse, ParseError


def test_module_only():
    m = parse("module foo;")
    assert m.name == "foo"
    assert m.items == []


def test_dotted_module():
    m = parse("module hr.employees;")
    assert m.name == "hr.employees"
    assert m.package_name == "hr_employees"


def test_simple_fn():
    m = parse("module m; fn greet(name: text) {}")
    assert len(m.items) == 1
    fn = m.items[0]
    assert isinstance(fn, A.FnDef)
    assert fn.name == "greet"
    assert len(fn.params) == 1
    assert fn.params[0].name == "name"


def test_record():
    m = parse("module m; record Foo { id: number, name: text }")
    rec = m.items[0]
    assert isinstance(rec, A.RecordDef)
    assert rec.name == "Foo"
    assert [f.name for f in rec.fields] == ["id", "name"]


def test_error_with_payload():
    m = parse("module m; error NotFound { entity: text, id: number }")
    e = m.items[0]
    assert isinstance(e, A.ErrorDef)
    assert e.name == "NotFound"
    assert len(e.fields) == 2


def test_error_no_payload():
    m = parse("module m; error Bare;")
    e = m.items[0]
    assert isinstance(e, A.ErrorDef)
    assert e.fields == []


def test_pub_modifier():
    m = parse("module m; pub fn foo() {}")
    assert m.items[0].is_pub is True


def test_import():
    m = parse("module m; import std::log;")
    assert isinstance(m.items[0], A.ImportStmt)
    assert m.items[0].path == "std::log"


def test_optional_type():
    m = parse("module m; record R { x: text? }")
    rec = m.items[0]
    assert isinstance(rec.fields[0].type_ref, A.OptionalType)


def test_generic_type():
    m = parse("module m; fn f() -> Result<Foo, NotFound> {}")
    fn = m.items[0]
    rt = fn.return_type
    assert isinstance(rt, A.GenericType)
    assert rt.base == "Result"


def test_error_union():
    m = parse("module m; fn f() -> Result<Foo, A | B | C> {}")
    fn = m.items[0]
    rt = fn.return_type
    assert isinstance(rt, A.GenericType)
    inner = rt.params[1]
    assert isinstance(inner, A.ErrorUnionType)
    assert len(inner.variants) == 3


def test_sql_block_in_let():
    m = parse('''
        module m;
        fn f() {
          let x = sql! { select 1 from dual };
        }
    ''')
    let_stmt = m.items[0].body[0]
    assert isinstance(let_stmt, A.LetStmt)
    sql = let_stmt.value
    assert isinstance(sql, A.SqlBlock)


def test_sql_block_with_one_question():
    m = parse('''
        module m;
        fn f() -> Result<Foo, X> {
          let x = sql!{select id from t where id = :id}.one()?;
          return Ok(x);
        }
    ''')
    let_stmt = m.items[0].body[0]
    qm = let_stmt.value
    assert isinstance(qm, A.QuestionMark)
    call = qm.inner
    assert isinstance(call, A.Call)
    # callee is MemberAccess(SqlBlock, 'one')
    assert isinstance(call.callee, A.MemberAccess)
    assert call.callee.field == "one"


def test_question_after_call():
    m = parse('''
        module m;
        fn f() -> Result<Foo, X> {
          let x = find()?;
          return Ok(x);
        }
    ''')
    let_stmt = m.items[0].body[0]
    qm = let_stmt.value
    assert isinstance(qm, A.QuestionMark)


def test_transaction():
    m = parse('''
        module m;
        fn f() {
          transaction {
            let x = 1;
          }
        }
    ''')
    tx = m.items[0].body[0]
    assert isinstance(tx, A.TransactionStmt)
    assert len(tx.body) == 1


def test_match_with_variants():
    m = parse('''
        module m;
        fn f() {
          match x {
            Some(v) -> log::info(v),
            None -> log::warn(0),
          }
        }
    ''')
    ms = m.items[0].body[0]
    assert isinstance(ms, A.MatchStmt)
    assert len(ms.arms) == 2


def test_struct_literal():
    m = parse('''
        module m;
        fn f() {
          return Err(NotFound { entity: "user", id: 42 });
        }
    ''')
    ret = m.items[0].body[0]
    assert isinstance(ret, A.ReturnStmt)
    assert isinstance(ret.value, A.ErrExpr)
    assert isinstance(ret.value.inner, A.StructLit)


def test_into_type_args():
    m = parse('''
        module m;
        fn f() {
          let x = row.into::<Employee>();
        }
    ''')
    let_stmt = m.items[0].body[0]
    call = let_stmt.value
    assert isinstance(call, A.Call)
    assert len(call.type_args) == 1


def test_for_loop_over_sql():
    m = parse('''
        module m;
        fn f() {
          for r in sql! { select id from t } {
            log::info(r.id);
          }
        }
    ''')
    fs = m.items[0].body[0]
    assert isinstance(fs, A.ForStmt)
    assert fs.var_name == "r"
    assert isinstance(fs.iterable, A.SqlBlock)


def test_parse_error_on_missing_semi():
    with pytest.raises(ParseError):
        parse('module m; fn f() { let x = 1 }')


def test_annotation_on_fn():
    m = parse('''
        module m;
        @deterministic
        @result_cache
        fn f() -> number { return 1; }
    ''')
    fn = m.items[0]
    assert [a.name for a in fn.annotations] == ["deterministic", "result_cache"]
