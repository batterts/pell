"""Emitter tests."""
import pytest

from pell.parser import parse
from pell.emitter import emit


def compile_to_sql(src: str) -> str:
    return emit(parse(src))


def test_hello_world():
    sql = compile_to_sql("""
        module hello;
        pub fn greet(name: text) {
          log::info(name);
        }
    """)
    assert "CREATE OR REPLACE PACKAGE hello AS" in sql
    assert "PROCEDURE greet(p_name IN VARCHAR2);" in sql
    assert "CREATE OR REPLACE PACKAGE BODY hello AS" in sql
    assert "log.info(p_name);" in sql


def test_module_name_mangling():
    sql = compile_to_sql("""
        module hr.employees;
        pub fn ping() {}
    """)
    assert "PACKAGE hr_employees AS" in sql
    assert "PACKAGE BODY hr_employees AS" in sql


def test_record_emits_type_in_spec_when_pub():
    sql = compile_to_sql("""
        module m;
        pub record Foo {
          id: number,
          name: text,
        }
    """)
    assert "TYPE t_foo IS RECORD" in sql
    assert "id NUMBER" in sql
    assert "name VARCHAR2(4000)" in sql


def test_error_decl_emits_runtime_section():
    sql = compile_to_sql("""
        module m;
        pub error NotFound { id: number }
    """)
    assert "Additions to pell_runtime" in sql
    assert "m_notfound EXCEPTION" in sql
    assert "EXCEPTION_INIT(m_notfound" in sql


def test_function_with_record_return():
    sql = compile_to_sql("""
        module m;
        pub record Foo { id: number }
        pub fn get(x: number) -> Foo {
          return Foo { id: x };
        }
    """)
    assert "FUNCTION get(p_x IN NUMBER) RETURN t_foo" in sql


def test_select_into_one_question_mark():
    sql = compile_to_sql("""
        module m;
        pub record Foo { id: number, name: text }
        pub error NF { id: number }
        pub fn find(id: number) -> Result<Foo, NF> {
          let row = sql! {
            select id, name from t where id = :id
          }.one()?;
          return Ok(row);
        }
    """)
    # Should be a SELECT ... INTO l_row ... not a SELECT ... ; INTO l_row
    assert "INTO l_row" in sql
    # INTO should appear before FROM
    intro_pos = sql.index("INTO l_row")
    from_pos = sql.lower().index("from t", intro_pos - 200, intro_pos + 200)
    # FROM should be AFTER INTO in linear text
    assert from_pos > intro_pos


def test_transaction_emits_savepoint_and_handler():
    sql = compile_to_sql("""
        module m;
        pub fn tx() {
          transaction {
            sql! { update t set x = 1 };
          }
        }
    """)
    assert "SAVEPOINT pell_sp" in sql
    assert "COMMIT;" in sql
    assert "ROLLBACK TO pell_sp" in sql


def test_err_return_sets_payload_and_raises():
    sql = compile_to_sql("""
        module m;
        pub error Bad { reason: text }
        pub fn f() -> Result<Unit, Bad> {
          return Err(Bad { reason: "no" });
        }
    """)
    assert "pell_runtime.set_err('m_bad:1'" in sql
    assert "RAISE pell_runtime.m_bad" in sql


def test_dml_returning():
    sql = compile_to_sql("""
        module m;
        pub fn ins(code: text) -> number {
          let id = sql! {
            insert into t(code) values (:code) returning id
          }.returning::<number>().one();
          return id;
        }
    """)
    assert "INTO l_id" in sql
    assert "insert into t" in sql.lower()
    assert "returning id into l_id" in sql.lower()


def test_rowcount():
    sql = compile_to_sql("""
        module m;
        pub fn purge() -> number {
          let n = sql! { delete from t where stale = 1 }.rowcount();
          return n;
        }
    """)
    assert "SQL%ROWCOUNT" in sql
    assert "delete from t" in sql.lower()


def test_for_loop_over_sql():
    sql = compile_to_sql("""
        module m;
        pub fn run() {
          for r in sql! { select id from t } {
            log::info(r.id);
          }
        }
    """)
    assert "FOR r IN" in sql
    assert "log.info(r.id)" in sql


def test_if_else():
    sql = compile_to_sql("""
        module m;
        pub fn f(x: number) -> number {
          if x > 0 { return x; } else { return 0; }
        }
    """)
    assert "IF (p_x > 0) THEN" in sql
    assert "ELSE" in sql
    assert "END IF;" in sql


def test_no_size_on_param_types():
    sql = compile_to_sql("""
        module m;
        pub fn greet(name: text) {}
    """)
    # PL/SQL forbids size specifier on param types
    assert "IN VARCHAR2(4000)" not in sql
    assert "IN VARCHAR2" in sql


def test_for_update_modifier():
    sql = compile_to_sql("""
        module m;
        pub record A { id: number, x: number }
        pub error E;
        pub fn lock_it(id: number) -> Result<A, E> {
          transaction {
            let row = sql! {
              select id, x from t where id = :id
            }.for_update().one()?;
            return Ok(row);
          }
          return Err(E);
        }
    """)
    assert "FOR UPDATE" in sql


def test_for_update_nowait_skip_locked():
    sql = compile_to_sql("""
        module m;
        pub record A { id: number, x: number }
        pub error E;
        pub fn lock_first(id: number) -> Result<A, E> {
          transaction {
            let row = sql! {
              select id, x from t where id = :id
            }.for_update().nowait().one()?;
            return Ok(row);
          }
          return Err(E);
        }
    """)
    assert "FOR UPDATE NOWAIT" in sql


def test_string_interpolation():
    sql = compile_to_sql("""
        module m;
        pub fn greet(name: text) {
          log::info("hello {name}");
        }
    """)
    assert "'hello ' || p_name" in sql


def test_string_interpolation_field_access():
    sql = compile_to_sql("""
        module m;
        pub record P { id: number, name: text }
        pub fn show(p: P) {
          log::info("id={p.id}");
        }
    """)
    assert "'id=' || p_p.id" in sql


def test_string_no_interpolation_unchanged():
    sql = compile_to_sql("""
        module m;
        pub fn p() {
          log::info("no braces here");
        }
    """)
    assert "'no braces here'" in sql


def test_annotation_deterministic_result_cache():
    sql = compile_to_sql("""
        module m;
        @deterministic
        @result_cache
        pub fn lookup(code: text) -> text {
          let r = sql! { select name from t where code = :code }.one();
          return r;
        }
    """)
    assert "DETERMINISTIC RESULT_CACHE" in sql


def test_annotation_udf():
    sql = compile_to_sql("""
        module m;
        @udf
        pub fn pure_calc(x: number) -> number {
          return x;
        }
    """)
    assert "PRAGMA UDF;" in sql


def test_annotation_autonomous():
    sql = compile_to_sql("""
        module m;
        @autonomous
        pub fn audit(msg: text) {
          sql! { insert into audit(msg) values (:msg) };
        }
    """)
    assert "PRAGMA AUTONOMOUS_TRANSACTION;" in sql


def test_annotation_udf_autonomous_conflict():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            @udf
            @autonomous
            pub fn bad() {}
        """)


def test_procedure_return_without_value():
    sql = compile_to_sql("""
        module m;
        pub fn p() -> Result<Unit, Bad> {
          return Ok(());
        }
        pub error Bad;
    """)
    # In a Unit-returning fn, return Ok(()) becomes RETURN;
    assert "RETURN;" in sql
    assert "RETURN NULL" not in sql
