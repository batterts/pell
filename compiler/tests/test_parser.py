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


# ---------------------------------------------------------------------------
# §5.2 / §5.3 — type, sealed type, aggregate
# ---------------------------------------------------------------------------


def test_type_with_methods():
    m = parse('''
        module m;
        pub type Money {
            amount: number;
            currency: text;
            fn add(other: Money) -> Money {
                return Money { amount: self.amount + other.amount, currency: self.currency };
            }
            map fn rank() -> number { return self.amount; }
        }
    ''')
    td = m.items[0]
    assert isinstance(td, A.TypeDef)
    assert td.name == "Money"
    assert td.is_pub
    assert [f.name for f in td.fields] == ["amount", "currency"]
    assert [me.name for me in td.methods] == ["add", "rank"]
    assert td.methods[1].is_map is True


def test_type_with_constructor():
    m = parse('''
        module m;
        pub type Email {
            addr: text;
            fn new(s: text) -> Email { return Email { addr: s }; }
        }
    ''')
    td = m.items[0]
    new_method = td.methods[0]
    assert new_method.name == "new"
    assert new_method.is_constructor is True


def test_sealed_type_with_cases():
    m = parse('''
        module m;
        pub sealed type Shape {
            fn area() -> number;
            case Circle { radius: number } {
                fn area() -> number { return 3.14; }
            }
            case Rectangle { width: number; height: number } {
                fn area() -> number { return 4; }
            }
        }
    ''')
    st = m.items[0]
    assert isinstance(st, A.SealedTypeDef)
    assert st.name == "Shape"
    assert [me.name for me in st.methods] == ["area"]
    assert st.methods[0].is_abstract is True
    assert [c.name for c in st.cases] == ["Circle", "Rectangle"]
    assert [f.name for f in st.cases[1].fields] == ["width", "height"]


def test_aggregate_minimal():
    m = parse('''
        module m;
        pub aggregate counter(x: number) -> number {
            state { n: number = 0; }
            step(v: number) { self.n = self.n + 1; }
            finish() -> number { return self.n; }
        }
    ''')
    ag = m.items[0]
    assert isinstance(ag, A.AggregateDef)
    assert ag.name == "counter"
    assert ag.merge_body is None
    assert [f.name for f in ag.state_fields] == ["n"]


def test_aggregate_with_merge_and_parallel():
    m = parse('''
        module m;
        @parallel
        pub aggregate avg2(x: number) -> number {
            state { sum: number = 0; n: number = 0; }
            step(v: number) {
                self.sum = self.sum + v;
                self.n = self.n + 1;
            }
            merge(o: Self) {
                self.sum = self.sum + o.sum;
                self.n = self.n + o.n;
            }
            finish() -> number { return self.sum / self.n; }
        }
    ''')
    ag = m.items[0]
    assert ag.merge_body is not None
    assert [a.name for a in ag.annotations] == ["parallel"]


def test_sequence_def_simple():
    m = parse("module m; pub seq emp_id_seq;")
    sd = m.items[0]
    assert isinstance(sd, A.SequenceDef)
    assert sd.name == "emp_id_seq"
    assert sd.is_pub


def test_fn_with_out_and_inout_params():
    m = parse("""
        module m;
        pub fn f(a: number, out b: text, inout c: number) {}
    """)
    fn = m.items[0]
    assert isinstance(fn, A.FnDef)
    assert [p.name for p in fn.params] == ["a", "b", "c"]
    assert [p.mode for p in fn.params] == ["in", "out", "inout"]


def test_method_param_modes():
    m = parse("""
        module m;
        pub type T {
            x: number;
            fn touch(out received: number) { received = self.x; }
        }
    """)
    td = m.items[0]
    method = td.methods[0]
    assert method.params[0].mode == "out"


def test_sequence_def_qualified():
    m = parse("module m; pub seq hr::emp_seq;")
    sd = m.items[0]
    assert isinstance(sd, A.SequenceDef)
    assert sd.name == "hr::emp_seq"


def test_aggregate_missing_state_errors():
    with pytest.raises(ParseError):
        parse('''
            module m;
            pub aggregate bad(x: number) -> number {
                step(v: number) { }
                finish() -> number { return 0; }
            }
        ''')
