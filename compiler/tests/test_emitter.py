"""Emitter tests."""
import pytest

from pell.parser import parse
from pell.emitter import emit


def compile_to_sql(src: str) -> str:
    return emit(parse(src))


def test_hello_world():
    sql = compile_to_sql("""
        module hello;
        import std::logger;
        pub fn greet(name: text) {
          logger::info(name);
        }
    """)
    assert "CREATE OR REPLACE PACKAGE hello AS" in sql
    assert "PROCEDURE greet(p_name IN VARCHAR2);" in sql
    assert "CREATE OR REPLACE PACKAGE BODY hello AS" in sql
    assert "logger.info(p_name);" in sql


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
        import std::logger;
        pub fn run() {
          for r in sql! { select id from t } {
            logger::info(r.id);
          }
        }
    """)
    assert "FOR r IN" in sql
    assert "logger.info(r.id)" in sql


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
        import std::logger;
        pub fn process(r: rowtype<accounts>) {
          logger::info(r.id);
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
        import std::logger;
        pub fn run() {
          let xs: list<number> = [1, 2, 3];
          forall x in xs {
            sql! { insert into t(n) values (:x) };
          }
          for i in xs.indices() {
            logger::info("rows: {bulk.rowcount(i)} total {bulk.total()}");
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
        import std::logger;
        pub fn run() {
          let xs: list<number> = [1, 2, 3];
          for i in xs.indices() {
            logger::info("at {i}");
          }
        }
    """)
    assert "FOR i IN l_xs.FIRST .. l_xs.LAST LOOP" in sql


def test_interpolation_supports_method_calls():
    sql = compile_to_sql("""
        module m;
        import std::logger;
        pub fn run() {
          let xs: list<number> = [1];
          logger::info("len is {xs.len()}");
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
        import std::logger;
            pub fn run() {
              let xs: list<number> = [1, 2];
              forall x in xs {
                logger::info(x);
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
    # Iteration uses FIRST/NEXT (safe on empty + sparse arrays) — the
    # FOR..FIRST..LAST form raises VALUE_ERROR when the list is empty
    # because Oracle won't accept NULL bounds.
    assert "i_x := l_xs.FIRST;" in sql
    assert "WHILE i_x IS NOT NULL LOOP" in sql
    assert "i_x := l_xs.NEXT(i_x);" in sql
    assert "insert into t(n) values (l_x_iter)" in sql


def test_finally_block_emitted():
    sql = compile_to_sql("""
        module m;
        import std::logger;
        pub fn charge(id: number) {
          sql! { insert into audit(id) values (:id) };
        } finally {
          logger::info("done");
        }
    """)
    assert "PROCEDURE pell_finally_body IS" in sql
    assert "logger.info('done')" in sql
    assert "EXCEPTION" in sql
    # The finally should be called in both the success path AND
    # the WHEN OTHERS handler path (then re-raise).
    body_calls = sql.count("pell_finally_body")
    assert body_calls >= 4  # decl, the IS line, error-path, success-path


def test_no_finally_no_wrapping():
    sql = compile_to_sql("""
        module m;
        import std::logger;
        pub fn simple() { logger::info("hi"); }
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
        import std::logger;
        pub fn greet(name: text) {
          logger::info("hello {name}");
        }
    """)
    assert "'hello ' || p_name" in sql


def test_string_interpolation_field_access():
    sql = compile_to_sql("""
        module m;
        import std::logger;
        pub record P { id: number, name: text }
        pub fn show(p: P) {
          logger::info("id={p.id}");
        }
    """)
    assert "'id=' || p_p.id" in sql


def test_string_no_interpolation_unchanged():
    sql = compile_to_sql("""
        module m;
        import std::logger;
        pub fn p() {
          logger::info("no braces here");
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


def test_json_chain_simple_set_uses_json_object_t():
    """Dot-chain .set() lowers to JSON_OBJECT_T.put() — no SELECT FROM dual."""
    sql = compile_to_sql("""
        module m;
        pub fn build() -> json {
            let j: json = json::object()
                .set("name", "Alice")
                .set("age", 30);
            return j;
        }
    """)
    assert "JSON_OBJECT_T" in sql
    assert ".put('name', 'Alice')" in sql
    assert ".put('age', 30)" in sql
    assert ".to_json()" in sql
    assert "JSON_TRANSFORM" not in sql  # no SQL wrapper
    assert "FROM dual" not in sql


def test_json_chain_autovivifies_intermediate_paths():
    sql = compile_to_sql("""
        module m;
        pub fn build() -> json {
            let j: json = json::object()
                .set("user.name", "Bob")
                .set("user.address.city", "NYC");
            return j;
        }
    """)
    # Intermediate auto-vivify via has() + put():
    assert ".has('user')" in sql
    assert ".put('user', JSON_OBJECT_T())" in sql
    assert ".has('address')" in sql
    # leaf puts:
    assert ".put('name', 'Bob')" in sql
    assert ".put('city', 'NYC')" in sql


def test_json_chain_dedupes_intermediate_creates():
    """Two SETs with the same prefix both check .has() — idempotent."""
    sql = compile_to_sql("""
        module m;
        pub fn f() -> json {
            let j: json = json::object()
                .set("u.a", 1)
                .set("u.b", 2);
            return j;
        }
    """)
    assert ".put('a', 1)" in sql
    assert ".put('b', 2)" in sql


def test_json_chain_remove_and_append():
    sql = compile_to_sql("""
        module m;
        pub fn f(j: json) -> json {
            let r: json = j.remove("draft").append("history", "edited");
            return r;
        }
    """)
    assert ".remove('draft')" in sql
    assert "JSON_ARRAY_T" in sql
    assert ".append('edited')" in sql
    assert ".put('history'," in sql


def test_json_chain_on_non_json_falls_through():
    """When the receiver isn't json-typed, .set() reverts to a regular method
    dispatch (so user-defined `.set()` on other types still works)."""
    sql = compile_to_sql("""
        module m;
        pub type Counter {
            n: number;
            fn set(value: number) { self.n = value; }
        }
        pub fn f(c: Counter) { c.set(5); }
    """)
    assert "p_c.set(5)" in sql       # dispatch hit the user's method
    assert "JSON_OBJECT_T" not in sql


def test_json_chain_runtime_path_errors():
    """Path must be a string literal — runtime paths are rejected at compile time."""
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub fn f(p: text) -> json {
                let j: json = json::object().set(p, "x");
                return j;
            }
        """)


def test_json_object_kwargs_become_json_object():
    sql = compile_to_sql("""
        module m;
        pub fn make() -> json {
            return json::object(name = "Alice", age = 30, role = "admin");
        }
    """)
    assert "JSON_OBJECT('name' VALUE 'Alice', 'age' VALUE 30, 'role' VALUE 'admin' RETURNING JSON)" in sql


def test_json_array_lowers_to_json_array():
    sql = compile_to_sql("""
        module m;
        pub fn xs() -> json { return json::array([1, 2, 3]); }
    """)
    assert "JSON_ARRAY(1, 2, 3 RETURNING JSON)" in sql


def test_json_path_access_helpers():
    sql = compile_to_sql("""
        module m;
        pub fn t(j: json) -> text { return json::get_text(j, "$.name"); }
        pub fn n(j: json) -> number { return json::get_number(j, "$.age"); }
        pub fn q(j: json) -> json { return json::get_json(j, "$.addr"); }
        pub fn h(j: json) -> bool { return json::has(j, "$.email"); }
    """)
    assert "JSON_VALUE(p_j, '$.name')" in sql
    assert "JSON_VALUE(p_j, '$.age' RETURNING NUMBER)" in sql
    assert "JSON_QUERY(p_j, '$.addr')" in sql
    assert "JSON_EXISTS(p_j, '$.email')" in sql


def test_json_parse_and_stringify():
    sql = compile_to_sql("""
        module m;
        pub fn p(s: text) -> json { return json::parse(s); }
        pub fn s(j: json) -> text { return json::stringify(j); }
    """)
    assert "JSON(p_s)" in sql
    assert "JSON_SERIALIZE(p_j)" in sql


def test_json_from_record_emits_json_object():
    sql = compile_to_sql("""
        module m;
        pub record User { name: text, age: number }
        pub fn pack(u: User) -> json { return json::from(u); }
    """)
    assert "JSON_OBJECT('name' VALUE p_u.name, 'age' VALUE p_u.age RETURNING JSON)" in sql


def test_json_into_emits_field_by_field_extraction():
    sql = compile_to_sql("""
        module m;
        pub record User { name: text, age: number }
        pub fn unpack(j: json) -> text {
            let u: User = json::into::<User>(j);
            return u.name;
        }
    """)
    assert "l_u.name := JSON_VALUE(p_j, '$.name');" in sql
    assert "l_u.age := JSON_VALUE(p_j, '$.age' RETURNING NUMBER);" in sql


def test_json_into_unknown_record_errors():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub fn f(j: json) -> text {
                let u: NotARecord = json::into::<NotARecord>(j);
                return "x";
            }
        """)


def test_call_kwargs_parse_in_position():
    """`fn(name = value, other = expr)` parses as Call with kwargs."""
    from pell.parser import parse
    m = parse("""
        module m;
        pub fn f() -> number {
            let n = foo(a = 1, b = 2);
            return n;
        }
    """)
    from pell import ast as A
    call = m.items[0].body[0].value
    assert isinstance(call, A.Call)
    assert "a" in call.kwargs and "b" in call.kwargs


def test_typed_pivot_emits_static_pivot_clause():
    sql = compile_to_sql("""
        module m;
        pub enum Region { NORTH, SOUTH, EAST, WEST }
        pub record Totals { product: text, NORTH: number, SOUTH: number, EAST: number, WEST: number }
        pub fn totals() -> list<Totals> {
            let rows: list<Totals> = pivot::sum(
                source = sql!{ select product, region, sales from orders },
                rows = product, col = region, over = Region, value = sales,
            ).collect();
            return rows;
        }
    """)
    assert "PIVOT (" in sql
    assert "SUM(sales) FOR region IN" in sql
    assert "'NORTH' AS \"NORTH\"" in sql
    assert "'WEST' AS \"WEST\"" in sql
    assert "BULK COLLECT INTO l_rows" in sql


def test_typed_pivot_requires_known_enum():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub fn f() -> number {
                let _ = pivot::sum(
                    source = sql!{ select 1 from dual }, rows = a, col = b, over = NotAnEnum, value = c,
                ).collect();
                return 1;
            }
        """)


def test_dyn_pivot_emits_listagg_and_open_for():
    sql = compile_to_sql("""
        module m;
        @touches(orders)
        pub unsafe fn region_sales_dyn() -> cursor<text> {
            return pivot::sum_dyn(
                source = sql!{ select product, region, sales from orders },
                rows = product, col = region, value = sales,
            );
        }
    """)
    assert "LISTAGG" in sql
    assert "WITHIN GROUP (ORDER BY region)" in sql
    assert "OPEN l_pell_pivot_cur FOR l_pell_pivot_sql;" in sql
    assert "RETURN l_pell_pivot_cur;" in sql
    # Pinning cursor from @touches
    assert "CURSOR pin_orders IS SELECT 1 FROM orders" in sql


def test_dyn_pivot_requires_unsafe_fn():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub fn f() -> cursor<text> {
                return pivot::sum_dyn(
                    source = sql!{ select 1 from dual }, rows = a, col = b, value = c,
                );
            }
        """)


def test_exec_dyn_emits_execute_immediate():
    sql = compile_to_sql("""
        module m;
        @touches(orders)
        @binds(st)
        pub unsafe fn count_in(tname: text, st: text) -> number {
            let n: number = exec_dyn("select count(*) from {tname} where status = :st")?;
            return n;
        }
    """)
    # EXECUTE IMMEDIATE with INTO target and USING the @binds.
    assert "EXECUTE IMMEDIATE" in sql
    assert "INTO l_n" in sql
    assert "USING p_st" in sql
    # tname is interpolated, not a bind:
    assert "|| p_tname ||" in sql


def test_exec_dyn_in_non_unsafe_fn_errors():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub fn bad() -> number {
                let n: number = exec_dyn("select 1 from dual")?;
                return n;
            }
        """)


def test_unsafe_fn_emits_pinning_cursors():
    sql = compile_to_sql("""
        module m;
        @touches(orders, archive_orders)
        @binds(id)
        pub unsafe fn fetch(id: number) -> number {
            let n: number = exec_dyn("select count(*) from orders where id = :id")?;
            return n;
        }
    """)
    assert "PROCEDURE pell_dep_pinning IS" in sql
    assert "CURSOR pin_orders IS SELECT 1 FROM orders WHERE 1=0;" in sql
    assert "CURSOR pin_archive_orders IS SELECT 1 FROM archive_orders WHERE 1=0;" in sql


def test_touches_feeds_dep_manifest():
    sql = compile_to_sql("""
        module m;
        @touches(orders, archive_orders)
        @binds(id)
        pub unsafe fn fetch(id: number) -> number {
            let n: number = exec_dyn("select 1 from orders")?;
            return n;
        }
    """)
    # tables (incl. views/synonyms) list must contain the @touches.
    assert "archive_orders" in sql
    assert "orders" in sql


def test_exec_dyn_dml_statement_no_into():
    sql = compile_to_sql("""
        module m;
        @touches(audit_log)
        @binds(action)
        pub unsafe fn log(action: text) {
            exec_dyn("insert into audit_log (action, ts) values (:action, systimestamp)");
        }
    """)
    # No INTO clause for DML.
    assert "EXECUTE IMMEDIATE" in sql
    assert "USING p_action" in sql
    # Check that no INTO appears in the same statement
    exec_idx = sql.find("EXECUTE IMMEDIATE")
    semi_idx = sql.find(";", exec_idx)
    assert "INTO " not in sql[exec_idx:semi_idx]


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


# ---------------------------------------------------------------------------
# jq!{...} macro — JSON_TABLE-backed iteration
# ---------------------------------------------------------------------------


def test_jq_scalar_projection_lowers_to_json_table():
    sql = compile_to_sql("""
        module m;
        pub fn names(j: json) -> list<text> {
            let xs: list<text> = jq!{ j | .users[] | .name }.collect();
            return xs;
        }
    """)
    assert "JSON_TABLE(p_j, '$.users[*]'" in sql
    assert "v VARCHAR2(4000) PATH '$.name'" in sql
    assert "BULK COLLECT INTO l_xs" in sql


def test_jq_select_filter_adds_where():
    sql = compile_to_sql("""
        module m;
        pub fn adults(j: json) -> list<text> {
            let xs: list<text> = jq!{ j | .users[] | select(.age >= 18) | .name }
                .collect();
            return xs;
        }
    """)
    assert "f0 NUMBER PATH '$.age'" in sql
    assert "WHERE jt.f0 >= 18" in sql


def test_jq_and_or_combinator():
    sql = compile_to_sql("""
        module m;
        pub fn picks(j: json) -> list<text> {
            let xs: list<text> = jq!{
                j | .users[] | select(.age >= 18 and .age <= 65) | .name
            }.collect();
            return xs;
        }
    """)
    assert "AND" in sql
    assert ">= 18" in sql and "<= 65" in sql


def test_jq_record_projection_emits_per_field_columns():
    sql = compile_to_sql("""
        module m;
        pub record User { name: text, age: number }
        pub fn all_users(j: json) -> list<User> {
            let xs: list<User> = jq!{ j | .users[] }.collect();
            return xs;
        }
    """)
    assert "name VARCHAR2(4000) PATH '$.name'" in sql
    assert "age NUMBER PATH '$.age'" in sql
    assert "jt.name, jt.age" in sql


def test_jq_top_level_array_iterator():
    sql = compile_to_sql("""
        module m;
        pub fn names(j: json) -> list<text> {
            let xs: list<text> = jq!{ j | .[] | .name }.collect();
            return xs;
        }
    """)
    assert "JSON_TABLE(p_j, '$[*]'" in sql


def test_jq_without_target_type_errors():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql("""
            module m;
            pub fn names(j: json) -> json {
                let xs = jq!{ j | .users[] }.collect();
                return xs;
            }
        """)


def test_jq_string_literal_filter():
    sql = compile_to_sql("""
        module m;
        pub fn pick(j: json) -> list<text> {
            let xs: list<text> = jq!{ j | .users[] | select(.role == "admin") | .name }
                .collect();
            return xs;
        }
    """)
    assert "f0 VARCHAR2(4000) PATH '$.role'" in sql
    assert "jt.f0 = 'admin'" in sql


def test_bigtext_lowers_to_clob():
    sql = compile_to_sql("""
        module m;
        pub fn store(payload: bigtext) -> number {
            return length(payload);
        }
        pub fn echo_back(payload: bigtext) -> bigtext {
            return payload;
        }
    """)
    assert "FUNCTION store(p_payload IN CLOB) RETURN NUMBER" in sql
    assert "FUNCTION echo_back(p_payload IN CLOB) RETURN CLOB" in sql


def test_bigtext_in_record_field_lowers_to_clob():
    sql = compile_to_sql("""
        module m;
        pub record Doc {
            id: number,
            body: bigtext,
        }
    """)
    assert "body CLOB" in sql


# ---------------------------------------------------------------------------
# emit_anon_block — anonymous PL/SQL for `pell exec` / REPL
# ---------------------------------------------------------------------------


def test_emit_anon_block_wraps_stmts_in_pell_anon_main():
    from pell.parser import parse_cell
    from pell.emitter import emit_anon_block
    items, stmts = parse_cell("""
        let x: number = 21;
        let y: number = x * 2;
    """)
    block = emit_anon_block(items, stmts)
    assert block.startswith("DECLARE")
    assert "PROCEDURE pell_anon_main IS" in block
    assert "l_x NUMBER" in block and "l_y NUMBER" in block
    assert "BEGIN\n  pell_anon_main;\nEND;" in block


def test_emit_anon_block_nests_fns_and_records():
    from pell.parser import parse_cell
    from pell.emitter import emit_anon_block
    items, stmts = parse_cell("""
        fn square(n: number) -> number { return n * n; }
        record Pair { a: number, b: number }
        let p: number = square(7);
    """)
    block = emit_anon_block(items, stmts)
    assert "FUNCTION square(p_n IN NUMBER) RETURN NUMBER IS" in block
    assert "TYPE t_pair IS RECORD" in block
    assert "l_p := square(7);" in block


def test_emit_anon_block_rejects_schema_level_items():
    from pell.parser import parse_cell
    from pell.emitter import emit_anon_block, EmitError
    items, stmts = parse_cell("""
        pub type Animal { name: text; }
    """)
    with pytest.raises(EmitError):
        emit_anon_block(items, stmts)


# ---------------------------------------------------------------------------
# re:: surface — pell_re package bindings
# ---------------------------------------------------------------------------


def test_re_matches_lowers_to_pell_re_call():
    sql = compile_to_sql(r"""
        module m;
        pub fn ok(s: text) -> bool {
            return re::matches(s, "\d+");
        }
    """)
    assert "pell_re.matches(p_s, '\\d+')" in sql


def test_re_find_lowers_to_pell_re_call():
    sql = compile_to_sql(r"""
        module m;
        pub fn first(s: text) -> text {
            return re::find(s, "\w+");
        }
    """)
    assert "pell_re.find(p_s, '\\w+')" in sql


def test_re_find_all_emits_adapter():
    sql = compile_to_sql(r"""
        module m;
        pub fn nums(s: text) -> list<text> {
            return re::find_all(s, "\d+");
        }
    """)
    assert "FUNCTION pell_re_find_all" in sql
    assert "pell_re.find_all(p_s, p_pat)" in sql
    assert "FOR i IN src.FIRST .. src.LAST" in sql


def test_re_split_emits_adapter():
    sql = compile_to_sql(r"""
        module m;
        pub fn words(s: text) -> list<text> {
            return re::split(s, "\s+");
        }
    """)
    assert "FUNCTION pell_re_split" in sql
    assert "pell_re.split(p_s, p_pat)" in sql


def test_string_escape_preserves_unknown_sequences():
    """Lexer keeps `\\d` `\\w` `\\s` etc. so regex patterns survive."""
    sql = compile_to_sql(r"""
        module m;
        pub fn f() -> text { return "\d\w\s\b"; }
    """)
    assert "'\\d\\w\\s\\b'" in sql


def test_string_escape_still_handles_known_sequences():
    sql = compile_to_sql(r"""
        module m;
        pub fn f() -> text { return "line1\nline2"; }
    """)
    # `\n` becomes a real newline in the PL/SQL literal
    assert "line1\nline2'" in sql


# ---------------------------------------------------------------------------
# re::capture::<Record> + raw-string literals (backticks)
# ---------------------------------------------------------------------------


def test_re_capture_emits_field_by_field_assignment():
    sql = compile_to_sql(r"""
        module m;
        pub record Phone { area: text, prefix: text, line: text }
        pub fn parse(s: text) -> Phone {
            let p: Phone = re::capture::<Phone>(s, `(?<area>\d{3})-(?<prefix>\d{3})-(?<line>\d{4})`);
            return p;
        }
    """)
    assert "pell_re.t_capture_map" in sql
    assert "pell_re.capture_by_name(p_s," in sql
    assert "EXISTS('area')" in sql
    assert "l_p.area := " in sql
    assert ".match_text" in sql


def test_backtick_raw_strings_preserve_braces_and_backslashes():
    sql = compile_to_sql(r"""
        module m;
        pub fn f() -> text { return `\d{3}`; }
    """)
    assert "'\\d{3}'" in sql


def test_backtick_raw_strings_skip_interpolation():
    sql = compile_to_sql(r"""
        module m;
        pub fn f() -> text { return `{name}`; }
    """)
    # `{name}` is preserved verbatim — no interpolation
    assert "'{name}'" in sql


def test_re_capture_requires_record_type_arg():
    from pell.emitter import EmitError
    with pytest.raises(EmitError):
        compile_to_sql(r"""
            module m;
            pub fn f(s: text) -> text {
                let p: text = re::capture::<text>(s, `(?<a>\d+)`);
                return p;
            }
        """)


# ---------------------------------------------------------------------------
# /pattern/ regex literals
# ---------------------------------------------------------------------------


def test_regex_literal_after_comma_lowers_to_string():
    sql = compile_to_sql(r"""
        module m;
        pub fn f(s: text) -> bool {
            return re::matches(s, /\d+/);
        }
    """)
    assert "pell_re.matches(p_s, '\\d+')" in sql


def test_regex_literal_with_quantifier_braces():
    sql = compile_to_sql(r"""
        module m;
        pub fn f(s: text) -> text {
            return re::find(s, /\d{3,5}/);
        }
    """)
    assert "pell_re.find(p_s, '\\d{3,5}')" in sql


def test_division_after_identifier_still_works():
    """The lexer must NOT mistake `n / 2` for a regex literal."""
    sql = compile_to_sql("""
        module m;
        pub fn half(n: number) -> number {
            return n / 2;
        }
    """)
    assert "(p_n / 2)" in sql


def test_division_after_rparen_still_works():
    sql = compile_to_sql("""
        module m;
        pub fn f(xs: list<number>) -> number {
            return xs(1) / 2;
        }
    """)
    assert "(p_xs(1) / 2)" in sql


def test_regex_then_division_in_same_fn():
    sql = compile_to_sql(r"""
        module m;
        pub fn f(s: text, n: number) -> number {
            let ok: bool = re::matches(s, /\w+/);
            let half: number = n / 2;
            return half;
        }
    """)
    assert "pell_re.matches(p_s, '\\w+')" in sql
    assert "(p_n / 2)" in sql


def test_regex_literal_after_return():
    sql = compile_to_sql(r"""
        module m;
        pub fn f(s: text) -> bool {
            return re::matches(s, /^foo$/);
        }
    """)
    assert "'^foo$'" in sql


def test_regex_named_capture_via_literal():
    sql = compile_to_sql(r"""
        module m;
        pub record P { area: text }
        pub fn parse(s: text) -> P {
            let p: P = re::capture::<P>(s, /(?<area>\d{3})/);
            return p;
        }
    """)
    assert "pell_re.capture_by_name(p_s, '(?<area>\\d{3})')" in sql
    assert "EXISTS('area')" in sql


def test_regex_flags_not_yet_supported():
    from pell.lexer import LexError
    with pytest.raises(LexError):
        compile_to_sql(r"""
            module m;
            pub fn f(s: text) -> bool {
                return re::matches(s, /foo/i);
            }
        """)


# ---------------------------------------------------------------------------
# Auto-stringify — non-text args in text-expecting positions
# ---------------------------------------------------------------------------


def test_auto_stringify_number_in_interpolation():
    sql = compile_to_sql("""
        module m;
        pub fn f(n: number) -> text { return "val={n}"; }
    """)
    assert "TO_CHAR(p_n)" in sql


def test_auto_stringify_json_in_interpolation():
    sql = compile_to_sql("""
        module m;
        pub fn f(j: json) -> text { return "data={j}"; }
    """)
    assert "JSON_SERIALIZE(p_j)" in sql


def test_auto_stringify_bool_in_interpolation():
    sql = compile_to_sql("""
        module m;
        pub fn f(flag: bool) -> text { return "flag={flag}"; }
    """)
    assert "CASE WHEN p_flag THEN 'true' ELSE 'false' END" in sql


def test_auto_stringify_date_in_interpolation():
    sql = compile_to_sql("""
        module m;
        pub fn f(d: date) -> text { return "when={d}"; }
    """)
    assert "TO_CHAR(p_d, 'YYYY-MM-DD HH24:MI:SS')" in sql


def test_auto_stringify_log_info_number():
    sql = compile_to_sql("""
        module m;
        import std::logger;
        pub fn f(n: number) { logger::info(n); }
    """)
    assert "logger.info(TO_CHAR(p_n))" in sql


def test_auto_stringify_log_info_json():
    sql = compile_to_sql("""
        module m;
        import std::logger;
        pub fn f(j: json) { logger::info(j); }
    """)
    assert "logger.info(JSON_SERIALIZE(p_j))" in sql


def test_auto_stringify_dbms_output_put_line_number():
    sql = compile_to_sql("""
        module m;
        import dbms_output;
        pub fn f(n: number) { dbms_output::put_line(n); }
    """)
    assert "dbms_output.put_line(TO_CHAR(p_n))" in sql


def test_auto_stringify_text_passthrough():
    """text args should NOT be wrapped."""
    sql = compile_to_sql("""
        module m;
        import std::logger;
        pub fn f(s: text) { logger::info(s); }
    """)
    assert "logger.info(p_s)" in sql
    assert "TO_CHAR" not in sql


def test_to_text_method_alias():
    sql = compile_to_sql("""
        module m;
        pub fn f(n: number) -> text { return n.to_text(); }
    """)
    assert "TO_CHAR(p_n)" in sql


def test_to_text_on_json():
    sql = compile_to_sql("""
        module m;
        pub fn f(j: json) -> text { return j.to_text(); }
    """)
    assert "JSON_SERIALIZE(p_j)" in sql


# ---------------------------------------------------------------------------
# jq!{} scalar field access (no [*] iterator)
# ---------------------------------------------------------------------------


def test_jq_scalar_field_access():
    """jq!{ j | .name } without [] lowers to JSON_VALUE, not JSON_TABLE."""
    sql = compile_to_sql("""
        module m;
        pub fn f(j: json) -> text {
            let name: text = jq!{ j | .name }.one();
            return name;
        }
    """)
    assert "JSON_VALUE(p_j, '$.name')" in sql
    assert "JSON_TABLE" not in sql


def test_jq_nested_scalar_field():
    sql = compile_to_sql("""
        module m;
        pub fn f(j: json) -> text {
            let city: text = jq!{ j | .address.city }.one();
            return city;
        }
    """)
    assert "JSON_VALUE(p_j, '$.address.city')" in sql


def test_jq_scalar_number_returning():
    sql = compile_to_sql("""
        module m;
        pub fn f(j: json) -> number {
            let age: number = jq!{ j | .age }.one();
            return age;
        }
    """)
    assert "JSON_VALUE(p_j, '$.age' RETURNING NUMBER)" in sql


def test_jq_array_iteration_still_works():
    """Existing [*] array iteration is unaffected by the scalar path."""
    sql = compile_to_sql("""
        module m;
        pub fn f(j: json) -> list<text> {
            let xs: list<text> = jq!{ j | .users[] | .name }.collect();
            return xs;
        }
    """)
    assert "JSON_TABLE(p_j, '$.users[*]'" in sql
    assert "JSON_VALUE" not in sql or "PATH" in sql


def test_undefined_record_type_raises_clear_error():
    """StructLit referencing an unknown type → clear EmitError, not
    silent fall-through to a TODO comment that crashes Oracle."""
    from pell.emitter import EmitError
    with pytest.raises(EmitError) as exc_info:
        compile_to_sql("""
            module m;
            pub fn f() {
                let alice: Employee = Employee { id: 1, name: "Alice", level: 5 };
            }
        """)
    msg = str(exc_info.value)
    assert "unknown type `Employee`" in msg
    # Suggested record def with field types inferred from values:
    assert "record Employee { id: number, name: text, level: number }" in msg


def test_undefined_record_type_suggestion_handles_bool_and_json():
    """The suggestion infers bool, json, and other non-trivial types."""
    from pell.emitter import EmitError
    with pytest.raises(EmitError) as exc_info:
        compile_to_sql("""
            module m;
            pub fn f() {
                let c: Config = Config { enabled: true, count: 3 };
            }
        """)
    msg = str(exc_info.value)
    assert "record Config { enabled: bool, count: number }" in msg


# ---------------------------------------------------------------------------
# Print aliases — p() / print() / println() / std::print() etc.
# ---------------------------------------------------------------------------


def test_p_alias_lowers_to_put_line():
    sql = compile_to_sql("""
        module m;
        pub fn f(n: number) { p(n); }
    """)
    assert "dbms_output.put_line(TO_CHAR(p_n))" in sql


def test_print_alias_with_date():
    sql = compile_to_sql("""
        module m;
        pub fn f(d: date) { print(d); }
    """)
    assert "dbms_output.put_line(TO_CHAR(p_d, 'YYYY-MM-DD HH24:MI:SS'))" in sql


def test_println_alias_with_json():
    sql = compile_to_sql("""
        module m;
        pub fn f(j: json) { println(j); }
    """)
    assert "dbms_output.put_line(JSON_SERIALIZE(p_j))" in sql


def test_std_print_alias():
    sql = compile_to_sql("""
        module m;
        pub fn f() { std::print("hello"); }
    """)
    assert "dbms_output.put_line('hello')" in sql


# ---------------------------------------------------------------------------
# Auto-stringify for list<T> in text-expecting positions
# ---------------------------------------------------------------------------


def test_list_text_in_interpolation_emits_helper():
    """`"{xs}"` where xs is list<text> emits a pell_list_to_text_text helper
    and calls it inline. No raw TO_CHAR on the collection."""
    sql = compile_to_sql("""
        module m;
        pub fn f() -> text {
            let xs: list<text> = sql!{ select dummy from dual }.collect();
            return "got {xs}";
        }
    """)
    assert "FUNCTION pell_list_to_text_text" in sql
    assert "pell_list_to_text_text(l_xs)" in sql
    assert "TO_CHAR(l_xs)" not in sql  # the broken old behavior


def test_list_number_uses_to_char_per_element():
    sql = compile_to_sql("""
        module m;
        pub fn f() -> text {
            let ns: list<number> = sql!{ select 1 as n from dual }.collect();
            return "got {ns}";
        }
    """)
    assert "FUNCTION pell_list_to_text_number" in sql
    assert "TO_CHAR(p_xs(l_i))" in sql
    assert "pell_list_to_text_number(l_ns)" in sql


def test_list_in_print_alias_works():
    sql = compile_to_sql("""
        module m;
        pub fn f() {
            let xs: list<text> = sql!{ select dummy from dual }.collect();
            p(xs);
        }
    """)
    assert "dbms_output.put_line(pell_list_to_text_text(l_xs))" in sql


def test_record_named_like_list_does_not_call_list_helper():
    """A user-defined record whose lowered PL/SQL name ends in `_list`
    (e.g. `User_List` → `t_user_list`) must NOT be misidentified as a
    list<T>. The structural sentinel from _infer_expr_type protects
    against this."""
    sql = compile_to_sql("""
        module m;
        pub record User_List { id: number, name: text }
        pub fn f() -> text {
            let u: User_List = User_List { id: 1, name: "shaun" };
            return "user: {u.name}";
        }
    """)
    # Record renders correctly:
    assert "t_user_list IS RECORD" in sql
    # NO list helper got triggered:
    assert "pell_list_to_text" not in sql
