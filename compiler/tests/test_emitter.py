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
    # First node of the module name is the schema; remainder is the package.
    assert "CREATE OR REPLACE PACKAGE hr.employees AS" in sql
    assert "CREATE OR REPLACE PACKAGE BODY hr.employees AS" in sql


def test_single_node_module_no_schema_qualifier():
    sql = compile_to_sql("""
        module standalone;
        pub fn ping() {}
    """)
    # No dot → no schema; bare package name (backwards compat).
    assert "CREATE OR REPLACE PACKAGE standalone AS" in sql
    assert "CREATE OR REPLACE PACKAGE BODY standalone AS" in sql
    # No spurious schema prefix.
    assert "CREATE OR REPLACE PACKAGE standalone." not in sql


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


def test_rowtype_lowers_to_percent_rowtype():
    sql = compile_to_sql("""
        module m;
        pub fn fetch_one() -> rowtype<accounts> {
          let r: rowtype<accounts> = sql! {
            select * from accounts where id = 1
          }.one();
          return r;
        }
    """)
    assert "RETURN accounts%ROWTYPE" in sql
    assert "l_r accounts%ROWTYPE" in sql


def test_rowtype_in_param_position():
    sql = compile_to_sql("""
        module m;
        pub fn process(r: rowtype<accounts>) {
          log::info(r.id);
        }
    """)
    assert "p_r IN accounts%ROWTYPE" in sql


def test_target_19c_lowers_bool_in_object_to_number_one():
    from pell.emitter import emit
    from pell.parser import parse
    src = """
        module m;
        pub record Src { id: number, active: bool }
        pub record Out { id: number, ok: bool }
        @pipelined
        pub fn pipe(rows: cursor<Src>) -> stream<Out> {
          for s in rows { yield Out { id: s.id, ok: s.active }; }
        }
    """
    sql_23 = emit(parse(src), target="23")
    sql_19c = emit(parse(src), target="19c")
    # 23: OBJECT attributes use BOOLEAN (only Out is emitted at schema level)
    assert "ok BOOLEAN" in sql_23
    # 19c: OBJECT attributes use NUMBER(1)
    assert "ok NUMBER(1)" in sql_19c


def test_target_19c_lowers_json_to_varchar2():
    from pell.emitter import emit
    from pell.parser import parse
    src = """
        module m;
        pub record Doc { id: number, payload: json }
        pub fn get_doc(id: number) -> Doc {
          let row: Doc = sql! { select id, payload from docs where id = :id }.one();
          return row;
        }
    """
    sql_23 = emit(parse(src), target="23")
    sql_19c = emit(parse(src), target="19c")
    # 23: native JSON type for the payload field
    assert "payload JSON" in sql_23
    # 19c: VARCHAR2(32767) for the payload field
    assert "payload VARCHAR2(32767)" in sql_19c
    # the 19c output should not contain bare JSON as a slot type
    assert "payload JSON" not in sql_19c


def test_target_19c_preserves_pipelined_streaming():
    """All the streaming machinery is already 19c-compatible — verify it
    still works at --target 19c."""
    from pell.emitter import emit
    from pell.parser import parse
    src = """
        module m;
        pub record Src { id: number }
        pub record Out { id: number }
        @pipelined
        pub fn pipe(rows: cursor<Src>) -> stream<Out> {
          for s in rows { yield Out { id: s.id }; }
        }
    """
    sql = emit(parse(src), target="19c")
    assert "PIPELINED" in sql
    assert "PIPE ROW" in sql
    assert "BULK COLLECT INTO" in sql


def test_target_unknown_rejected():
    from pell.emitter import emit, EmitError
    from pell.parser import parse
    import pytest as pt
    src = "module m; pub fn f() {}"
    with pt.raises(ValueError):
        emit(parse(src), target="21c")


def test_pipelined_emits_schema_types():
    sql = compile_to_sql("""
        module m;
        pub record Src { id: number, n: number }
        pub record Out { id: number, n: number }
        @pipelined
        pub fn pipe(rows: cursor<Src>) -> stream<Out> {
          for s in rows {
            yield Out { id: s.id, n: s.n * 2 };
          }
        }
    """)
    # OBJECT type for Out (yield element) — needed for PIPE ROW
    assert "CREATE OR REPLACE TYPE m_out_obj AS OBJECT" in sql
    # Nested table for the return type
    assert "CREATE OR REPLACE TYPE m_out_nt AS TABLE OF m_out_obj" in sql
    # Cursor element is a package-private RECORD type, not a schema OBJECT —
    # BULK COLLECT INTO won't accept a table-of-OBJECT from a multi-column cursor
    assert "TYPE t_src IS RECORD" in sql
    assert "m_src_obj" not in sql


def test_pipelined_fn_signature():
    sql = compile_to_sql("""
        module m;
        pub record Src { id: number }
        pub record Out { id: number }
        @pipelined
        pub fn pipe(rows: cursor<Src>) -> stream<Out> {
          for s in rows {
            yield Out { id: s.id };
          }
        }
    """)
    assert "FUNCTION pipe(p_rows IN SYS_REFCURSOR) RETURN m_out_nt PIPELINED" in sql


def test_pipelined_body_pipe_row_and_fetch():
    sql = compile_to_sql("""
        module m;
        pub record Src { x: number }
        pub record Out { y: number }
        @pipelined
        pub fn pipe(rows: cursor<Src>) -> stream<Out> {
          for s in rows {
            yield Out { y: s.x * 3 };
          }
        }
    """)
    assert "FETCH p_rows BULK COLLECT INTO" in sql
    assert "LIMIT 100" in sql
    assert "PIPE ROW(m_out_obj(" in sql
    assert "CLOSE p_rows" in sql
    assert "RETURN;" in sql


def test_pipeline_pipe_operator():
    sql = compile_to_sql("""
        module m;
        pub record Src { id: number }
        pub record Out { id: number }
        @pipelined
        pub fn pipe(rows: cursor<Src>) -> stream<Out> {
          for s in rows { yield Out { id: s.id }; }
        }
        pub fn caller() -> number {
          let xs: list<Out> = sql! { select id from t } |> pipe |> collect();
          return xs.len();
        }
    """)
    assert "TABLE(PIPE(CURSOR(SELECT ID FROM T)))" in sql.upper()
    assert "BULK COLLECT INTO l_xs" in sql


def test_bulk_rowcount_and_total():
    sql = compile_to_sql("""
        module m;
        pub fn run() {
          let xs: list<number> = [1, 2, 3];
          forall x in xs {
            sql! { insert into t(n) values (:x) };
          }
          for i in xs.indices() {
            log::info("rows: {bulk.rowcount(i)} total {bulk.total()}");
          }
        }
    """)
    assert "SQL%BULK_ROWCOUNT(i)" in sql
    assert "SQL%ROWCOUNT" in sql


def test_list_accessor_methods():
    sql = compile_to_sql("""
        module m;
        pub fn run() -> number {
          let xs: list<number> = [10, 20, 30];
          let n = xs.len();
          let first = xs.first();
          let last = xs.last();
          let third = xs.at(3);
          return n + first + last + third;
        }
    """)
    assert "l_xs.COUNT" in sql
    assert "l_xs.FIRST" in sql
    assert "l_xs.LAST" in sql
    assert "l_xs(3)" in sql


def test_for_indices_loop():
    sql = compile_to_sql("""
        module m;
        pub fn run() {
          let xs: list<number> = [1, 2, 3];
          for i in xs.indices() {
            log::info("at {i}");
          }
        }
    """)
    assert "FOR i IN l_xs.FIRST .. l_xs.LAST LOOP" in sql


def test_interpolation_supports_method_calls():
    sql = compile_to_sql("""
        module m;
        pub fn run() {
          let xs: list<number> = [1];
          log::info("len is {xs.len()}");
        }
    """)
    assert "'len is ' || l_xs.COUNT" in sql


def test_forall_bulk_insert():
    sql = compile_to_sql("""
        module m;
        pub fn run() {
          let xs: list<number> = [1, 2, 3];
          forall x in xs {
            sql! { insert into t(n) values (:x) };
          }
        }
    """)
    assert "FORALL i_x IN l_xs.FIRST .. l_xs.LAST" in sql
    assert "insert into t(n) values (l_xs(i_x))" in sql


def test_forall_rejects_non_dml_body():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub fn run() {
              let xs: list<number> = [1, 2];
              forall x in xs {
                log::info(x);
              }
            }
        """)


def test_bulk_collect_into():
    sql = compile_to_sql("""
        module m;
        pub fn run() -> number {
          let rows: list<number> = sql! { select n from t order by n }.collect();
          return 0;
        }
    """)
    assert "TYPE t_number_list IS TABLE OF NUMBER" in sql
    assert "l_rows t_number_list" in sql
    assert "BULK COLLECT INTO l_rows" in sql


def test_list_literal_and_loop():
    sql = compile_to_sql("""
        module m;
        pub fn run() {
          let xs: list<number> = [1, 3, 5];
          for x in xs {
            sql! { insert into t(n) values (:x) };
          }
        }
    """)
    assert "TYPE t_number_list IS TABLE OF NUMBER INDEX BY PLS_INTEGER" in sql
    assert "l_xs(1) := 1;" in sql
    assert "l_xs(2) := 3;" in sql
    assert "l_xs(3) := 5;" in sql
    assert "FOR i_x IN l_xs.FIRST .. l_xs.LAST LOOP" in sql
    assert "insert into t(n) values (l_x_iter)" in sql


def test_finally_block_emitted():
    sql = compile_to_sql("""
        module m;
        pub fn charge(id: number) {
          sql! { insert into audit(id) values (:id) };
        } finally {
          log::info("done");
        }
    """)
    assert "PROCEDURE pell_finally_body IS" in sql
    assert "log.info('done')" in sql
    assert "EXCEPTION" in sql
    # The finally should be called in both the success path AND
    # the WHEN OTHERS handler path (then re-raise).
    body_calls = sql.count("pell_finally_body")
    assert body_calls >= 4  # decl, the IS line, error-path, success-path


def test_no_finally_no_wrapping():
    sql = compile_to_sql("""
        module m;
        pub fn simple() { log::info("hi"); }
    """)
    assert "pell_finally_body" not in sql


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


# ---------------------------------------------------------------------------
# §5.2 / §5.3 — type, sealed type, aggregate emission
# ---------------------------------------------------------------------------


def test_type_basic_emission():
    sql = compile_to_sql("""
        module m;
        pub type Money {
            amount: number;
            currency: text;
            fn add(other: Money) -> Money {
                return Money { amount: self.amount + other.amount, currency: self.currency };
            }
            map fn rank() -> number { return self.amount; }
        }
    """)
    assert "CREATE OR REPLACE TYPE t_money AS OBJECT (" in sql
    assert "amount NUMBER" in sql
    assert "MEMBER FUNCTION add(p_other IN t_money) RETURN t_money" in sql
    assert "MAP MEMBER FUNCTION rank RETURN NUMBER" in sql
    assert "CREATE OR REPLACE TYPE BODY t_money AS" in sql
    assert "t_money((SELF.amount + p_other.amount), SELF.currency)" in sql
    assert "RETURN SELF.amount;" in sql


def test_sealed_type_emission_has_under_and_overriding():
    sql = compile_to_sql("""
        module m;
        pub sealed type Shape {
            fn area() -> number;
            case Circle { radius: number } {
                fn area() -> number { return 3.14 * self.radius * self.radius; }
            }
            case Rectangle { width: number; height: number } {
                fn area() -> number { return self.width * self.height; }
            }
        }
    """)
    assert "NOT INSTANTIABLE NOT FINAL" in sql
    assert "NOT INSTANTIABLE MEMBER FUNCTION area RETURN NUMBER" in sql
    assert "CREATE OR REPLACE TYPE t_circle UNDER t_shape (" in sql
    assert "CREATE OR REPLACE TYPE t_rectangle UNDER t_shape (" in sql
    assert "OVERRIDING MEMBER FUNCTION area RETURN NUMBER" in sql


def test_match_on_sealed_lowers_to_is_of_treat():
    sql = compile_to_sql("""
        module m;
        pub sealed type Shape {
            fn area() -> number;
            case Circle { radius: number } { fn area() -> number { return 1; } }
            case Square { side: number } { fn area() -> number { return 2; } }
        }
        pub fn kind(s: Shape) -> text {
            var k: text = "";
            match s {
                Circle(c) -> { k = "round"; }
                Square(_) -> { k = "boxy"; }
            }
            return k;
        }
    """)
    assert "IS OF (t_circle)" in sql
    assert "IS OF (t_square)" in sql
    assert "TREAT((p_s) AS t_circle)" in sql


def test_aggregate_emits_odci_machinery():
    sql = compile_to_sql("""
        module m;
        pub aggregate counter(x: number) -> number {
            state { n: number = 0; }
            step(v: number) { self.n = self.n + 1; }
            finish() -> number { return self.n; }
        }
    """)
    assert "STATIC FUNCTION ODCIAggregateInitialize(sctx IN OUT counter_agg_t)" in sql
    assert "MEMBER FUNCTION ODCIAggregateIterate(self IN OUT counter_agg_t" in sql
    assert "MEMBER FUNCTION ODCIAggregateTerminate(self IN OUT counter_agg_t" in sql
    # ODCIAggregateMerge is always declared (ORA-29925 requires it); body
    # is a stub when user didn't supply `merge`.
    assert "ODCIAggregateMerge" in sql
    assert "cannot run in parallel" in sql
    assert "CREATE OR REPLACE FUNCTION counter(p_x IN NUMBER) RETURN NUMBER AGGREGATE USING counter_agg_t" in sql


def test_aggregate_parallel_requires_merge():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            @parallel
            pub aggregate bad(x: number) -> number {
                state { n: number = 0; }
                step(v: number) { self.n = self.n + 1; }
                finish() -> number { return self.n; }
            }
        """)


def test_aggregate_with_merge_and_parallel_emits_clause():
    sql = compile_to_sql("""
        module m;
        @parallel
        pub aggregate sum2(x: number) -> number {
            state { acc: number = 0; }
            step(v: number) { self.acc = self.acc + v; }
            merge(o: Self) { self.acc = self.acc + o.acc; }
            finish() -> number { return self.acc; }
        }
    """)
    assert "ODCIAggregateMerge" in sql
    assert "PARALLEL_ENABLE AGGREGATE USING sum2_agg_t" in sql


def test_aggregate_list_state_lowers_to_nested_table():
    sql = compile_to_sql("""
        module m;
        pub aggregate sample(x: number) -> number {
            state { vals: list<number> = []; }
            step(v: number) { self.vals.append(v); }
            finish() -> number { return self.vals.len(); }
        }
    """)
    assert "CREATE OR REPLACE TYPE t_number_nt AS TABLE OF NUMBER" in sql
    assert "vals t_number_nt" in sql
    assert "SELF.vals.EXTEND" in sql
    assert "SELF.vals.COUNT" in sql


def test_aggregate_step_lets_emit_declarations():
    """Step/merge/finish bodies that declare locals must emit DECLARE-style
    decls between IS and BEGIN."""
    sql = compile_to_sql("""
        module m;
        pub aggregate h(s: text) -> number {
            state { acc: number = 0; }
            step(v: text) {
                let local: number = 42;
                self.acc = self.acc + local;
            }
            finish() -> number { return self.acc; }
        }
    """)
    assert "l_local NUMBER;" in sql


def test_aggregate_finish_return_inside_if_assigns_returnvalue():
    """Returns nested inside `if` blocks in finish() must still rewrite to
    `returnValue := ...; RETURN ODCIConst.Success;`."""
    sql = compile_to_sql("""
        module m;
        pub aggregate pick(s: text) -> text {
            state { val: text = ""; conflict: number = 0; }
            step(v: text) { self.val = v; }
            finish() -> text {
                if self.conflict == 1 { return ""; }
                return self.val;
            }
        }
    """)
    # The nested-in-IF return is the one most likely to regress.
    # We expect both arms to assign returnValue, not bare RETURN <value>.
    assert "RETURN SELF.val;" not in sql
    assert "returnValue := SELF.val;" in sql
    assert "returnValue := '';" in sql


def test_multi_arg_aggregate_emits_tuple_type():
    """Aggregates with >1 step parameter auto-emit a tuple OBJECT type;
    iterate takes that tuple as its single arg and the body unpacks back
    to the user's named parameters."""
    sql = compile_to_sql("""
        module m;
        pub aggregate argmax(val: text, key: number) -> text {
            state {
                best_val: text = "";
                best_key: number = 0;
                seen: number = 0;
            }
            step(v: text, k: number) {
                if self.seen == 0 { self.best_val = v; self.best_key = k; self.seen = 1; }
                else if k > self.best_key { self.best_val = v; self.best_key = k; }
            }
            finish() -> text { return self.best_val; }
        }
    """)
    # Tuple type emitted with one attribute per step param, in source order.
    assert "CREATE OR REPLACE TYPE argmax_args_t AS OBJECT" in sql
    assert "v VARCHAR2(4000)" in sql
    assert "k NUMBER" in sql
    # Iterate takes the tuple, not separate args.
    assert "ODCIAggregateIterate(self IN OUT argmax_agg_t, p_args IN argmax_args_t)" in sql
    # Body unpacks tuple back to user's named params.
    assert "p_v := p_args.v;" in sql
    assert "p_k := p_args.k;" in sql
    # The wrapper function has signature (p_args IN argmax_args_t).
    assert "FUNCTION argmax(p_args IN argmax_args_t) RETURN VARCHAR2" in sql


def test_pipelined_parallel_emits_partition_and_order_clauses():
    """@parallel(partition=hash(col), order=(cols)) on a @pipelined fn
    surfaces PARALLEL_ENABLE / PARTITION BY / ORDER BY in the signature,
    and uses a strongly-typed REF CURSOR (Oracle PLS-00627)."""
    sql = compile_to_sql("""
        module m;
        pub record Txn { country: text, ts: timestamp, amount: number }
        pub record Out { country: text, ts: timestamp, balance: number }
        @pipelined
        @parallel(partition = hash(country), order = (country, ts))
        pub fn rolling(rows: cursor<Txn>) -> stream<Out> {
            var first: number = 1;
            for r in rows {
                yield Out { country: r.country, ts: r.ts, balance: r.amount };
            }
        }
    """)
    # Strong cursor declaration in the spec.
    assert "TYPE t_txn_cur IS REF CURSOR RETURN t_txn;" in sql
    # Signature uses the strong cursor.
    assert "p_rows IN t_txn_cur" in sql
    assert "SYS_REFCURSOR" not in sql  # must NOT be used for parallel pipelined
    # Parallel + partition + order clauses present.
    assert "PARALLEL_ENABLE(PARTITION p_rows BY HASH(country))" in sql
    assert "ORDER p_rows BY (country, ts)" in sql


def test_pipelined_parallel_cluster_clause():
    """cluster= renders CLUSTER BY (...) instead of ORDER BY."""
    sql = compile_to_sql("""
        module m;
        pub record Txn { k: text, v: number }
        pub record Out { k: text, n: number }
        @pipelined
        @parallel(partition = hash(k), cluster = (k))
        pub fn pf(rows: cursor<Txn>) -> stream<Out> {
            for r in rows { yield Out { k: r.k, n: r.v }; }
        }
    """)
    assert "CLUSTER p_rows BY (k)" in sql
    assert "ORDER p_rows BY" not in sql


def test_pipelined_parallel_order_and_cluster_conflict_errors():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub record T { c: text }
            pub record O { c: text }
            @pipelined
            @parallel(partition = hash(c), order = (c), cluster = (c))
            pub fn pf(rows: cursor<T>) -> stream<O> {
                for r in rows { yield O { c: r.c }; }
            }
        """)


def test_enum_emits_constants_in_spec():
    sql = compile_to_sql("""
        module m;
        pub enum Region { NORTH, SOUTH, EAST, WEST }
        pub enum Status { OPEN = "open", CLOSED = "closed" }
    """)
    # Constants for each variant in the spec
    assert "region_north CONSTANT VARCHAR2(200) := 'NORTH';" in sql
    assert "region_west CONSTANT VARCHAR2(200) := 'WEST';" in sql
    assert "status_open CONSTANT VARCHAR2(200) := 'open';" in sql
    assert "status_closed CONSTANT VARCHAR2(200) := 'closed';" in sql


def test_enum_variant_references_lower_to_literal():
    sql = compile_to_sql("""
        module m;
        pub enum Region { NORTH, SOUTH }
        pub fn label(r: text) -> text {
            if r == Region::NORTH { return "northern"; }
            return "other";
        }
    """)
    # The reference becomes the literal text — no `pkg.region_north` lookup
    # needed at the call site (constants exist for cross-module use).
    assert "(p_r = 'NORTH')" in sql


def test_enum_does_not_pollute_packages_manifest():
    """`Region::NORTH` looks like `pkg::member` to the dep walker but enums
    are compile-time literals, not packages — must not show up."""
    sql = compile_to_sql("""
        module m;
        pub enum Region { NORTH }
        pub fn f() -> text { return Region::NORTH; }
    """)
    # The manifest section is bounded by the preamble lines; check there's
    # no `region` package listed.
    assert "packages:\n--     region" not in sql


def test_sequence_nextval_emits_bare_reference():
    """`pub seq name;` + `name.nextval` lowers to a bare PL/SQL reference,
    not the l_-prefixed local."""
    sql = compile_to_sql("""
        module m;
        pub seq emp_id_seq;
        pub fn next_id() -> number {
            let id = emp_id_seq.nextval;
            return id;
        }
    """)
    assert "l_id NUMBER;" in sql
    assert "l_id := emp_id_seq.nextval;" in sql
    # The seq name itself must NOT be `l_`-prefixed.
    assert "l_emp_id_seq" not in sql


def test_sequence_qualified_name():
    """Schema-qualified seq name lowers `::` to `.`"""
    sql = compile_to_sql("""
        module m;
        pub seq hr::emp_seq;
        pub fn n() -> number { return hr::emp_seq.nextval; }
    """)
    assert "hr.emp_seq.nextval" in sql


def test_string_method_aliases():
    sql = compile_to_sql("""
        module m;
        pub fn f(s: text) -> bool {
            if s.is_empty() { return false; }
            if s.starts_with("DEBUG") { return true; }
            if s.contains("ERROR") { return true; }
            return s.ends_with("!");
        }
    """)
    assert "(p_s IS NULL OR LENGTH(p_s) = 0)" in sql
    assert "(p_s LIKE 'DEBUG' || '%')" in sql
    assert "(INSTR(p_s, 'ERROR') > 0)" in sql
    assert "(p_s LIKE '%' || '!')" in sql


def test_date_extract_aliases():
    sql = compile_to_sql("""
        module m;
        pub fn parts(d: date) -> number {
            return d.year() * 10000 + d.month() * 100 + d.day();
        }
    """)
    assert "EXTRACT(YEAR FROM p_d)" in sql
    assert "EXTRACT(MONTH FROM p_d)" in sql
    assert "EXTRACT(DAY FROM p_d)" in sql


def test_split_emits_helper_and_list_type():
    sql = compile_to_sql("""
        module m;
        pub fn parse(s: text) -> number {
            let parts: list<text> = s.split(",");
            return parts.len();
        }
    """)
    assert "TYPE t_text_list IS TABLE OF VARCHAR2(4000)" in sql
    assert "FUNCTION pell_split_text(p_s VARCHAR2, p_delim VARCHAR2) RETURN t_text_list" in sql
    assert "pell_split_text(p_s, ',')" in sql


def test_split_helper_only_when_used():
    """The helper should NOT be emitted in modules that don't call .split()."""
    sql = compile_to_sql("""
        module m;
        pub fn f(s: text) -> number { return s.length(); }
    """)
    assert "pell_split_text" not in sql


def test_method_alias_wrong_arity_errors():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub fn f(s: text) -> bool { return s.contains(); }
        """)


def test_error_categories_get_distinct_sqlcode_ranges():
    sql = compile_to_sql("""
        module m;
        @propagate pub error A { x: number }
        @propagate pub error B { x: number }
        @skip      pub error C { x: number }
        @panic     pub error D { x: number }
    """)
    # propagate range -20100..-20199, skip -20200..-20299, panic -20300..-20399
    assert "PRAGMA EXCEPTION_INIT(m_a, -20100)" in sql
    assert "PRAGMA EXCEPTION_INIT(m_b, -20101)" in sql
    assert "PRAGMA EXCEPTION_INIT(m_c, -20200)" in sql
    assert "PRAGMA EXCEPTION_INIT(m_d, -20300)" in sql


def test_retry_wraps_body_in_loop_and_savepoint():
    sql = compile_to_sql("""
        module m;
        @propagate pub error Failed { reason: text }
        @retry(3, backoff_ms = 100)
        pub fn flaky() -> Result<Unit, Failed> {
            sql! { insert into events (data) values ('ok') };
            return Ok(());
        }
    """)
    assert "FUNCTION pell_is_panic(p_code IN NUMBER) RETURN BOOLEAN" in sql
    assert "l_pell_attempt PLS_INTEGER := 0;" in sql
    assert "SAVEPOINT pell_attempt;" in sql
    assert "IF pell_is_panic(SQLCODE) THEN RAISE; END IF;" in sql
    assert "ROLLBACK TO pell_attempt;" in sql
    assert "IF l_pell_attempt >= 3 THEN RAISE; END IF;" in sql
    assert "DBMS_SESSION.SLEEP((100 / 1000));" in sql


def test_retry_with_exponential_and_jitter_and_cap():
    sql = compile_to_sql("""
        module m;
        @retry(5, backoff_ms = 100, exponential = true, jitter = true, cap_ms = 5000)
        pub fn f() -> number { return 1; }
    """)
    # nested sleep expression with cap, exponential, and jitter factors
    assert "LEAST((5000 / 1000)" in sql
    assert "POWER(2, l_pell_attempt - 1)" in sql
    assert "DBMS_RANDOM.VALUE" in sql


def test_retry_without_backoff_emits_no_sleep():
    sql = compile_to_sql("""
        module m;
        @retry(3)
        pub fn f() -> number { return 1; }
    """)
    assert "l_pell_attempt PLS_INTEGER := 0;" in sql
    assert "DBMS_SESSION.SLEEP" not in sql


def test_retry_rejects_pipelined():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub record R { x: number }
            @pipelined
            @retry(3)
            pub fn p(rows: cursor<R>) -> stream<R> {
                for r in rows { yield R { x: r.x }; }
            }
        """)


def test_retry_panic_helper_only_when_used():
    """No @retry → no helper."""
    sql = compile_to_sql("""
        module m;
        pub fn f() -> number { return 1; }
    """)
    assert "pell_is_panic" not in sql


def test_out_inout_param_modes_emit_correctly():
    sql = compile_to_sql("""
        module m;
        pub fn split_name(full: text, out firstname: text, out lastname: text) {
            firstname = full;
            lastname = full;
        }
        pub fn bump(inout n: number) { n = n + 1; }
    """)
    assert "p_full IN VARCHAR2" in sql
    assert "p_firstname OUT VARCHAR2" in sql
    assert "p_lastname OUT VARCHAR2" in sql
    assert "p_n IN OUT NUMBER" in sql
    # Body should ASSIGN to the OUT param (not declare a local for it).
    assert "p_firstname := p_full;" in sql
    assert "p_n := (p_n + 1);" in sql


def test_caller_passes_locals_as_out_args():
    """When a pell fn calls another fn with OUT params, the local variable
    references lower correctly as `l_<name>` so PL/SQL accepts them as OUT
    bind targets."""
    sql = compile_to_sql("""
        module m;
        pub fn split(s: text, out a: text, out b: text) { a = s; b = s; }
        pub fn demo() -> text {
            var x: text = "";
            var y: text = "";
            split("hi", x, y);
            return x;
        }
    """)
    assert "split('hi', l_x, l_y);" in sql


def test_sequence_currval_inferred_as_number():
    sql = compile_to_sql("""
        module m;
        pub seq s;
        pub fn cur() -> number {
            let n = s.currval;
            return n;
        }
    """)
    assert "l_n NUMBER;" in sql
    assert "l_n := s.currval;" in sql


def test_pipelined_parallel_any_partition():
    sql = compile_to_sql("""
        module m;
        pub record T { x: number }
        pub record O { y: number }
        @pipelined
        @parallel(partition = any)
        pub fn pf(rows: cursor<T>) -> stream<O> {
            for r in rows { yield O { y: r.x }; }
        }
    """)
    assert "PARALLEL_ENABLE(PARTITION p_rows BY ANY)" in sql


def test_multi_arg_aggregate_step_count_mismatch_errors():
    """Step's parameter count must match the aggregate's outer signature."""
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub aggregate bad(a: number, b: number) -> number {
                state { n: number = 0; }
                step(only_one: number) { self.n = self.n + only_one; }
                finish() -> number { return self.n; }
            }
        """)


def test_unknown_callee_passes_through_as_builtin():
    """Bare-identifier function calls (Oracle/PL/SQL builtins) should not
    be l_-prefixed."""
    sql = compile_to_sql("""
        module m;
        pub aggregate h(n: number) -> number {
            state { acc: number = 0; }
            step(v: number) { self.acc = bitand(self.acc, v); }
            finish() -> number { return self.acc; }
        }
    """)
    assert "bitand(SELF.acc, p_v)" in sql
    assert "l_bitand" not in sql
