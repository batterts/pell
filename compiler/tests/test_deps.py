"""Tests for the shallow SQL dependency extractor and per-module manifest."""
import pytest

from pell.deps import extract_sql_deps, collect_module_deps
from pell.parser import parse


def test_extract_simple_select():
    t, d = extract_sql_deps("select id, name from employees where id = :x")
    assert t == {"employees"}
    assert d == set()


def test_extract_multiple_joins():
    t, _ = extract_sql_deps("""
        select e.id, d.name
          from employees e
          join departments d on e.dept_id = d.id
          left join locations l on d.loc_id = l.id
    """)
    assert t == {"employees", "departments", "locations"}


def test_extract_insert_into_distinguishes_from_select_into():
    """`INSERT INTO emp` should yield `emp`. `SELECT col INTO local FROM emp`
    must NOT yield `local` — INTO is only a table introducer after INSERT
    or MERGE."""
    t, _ = extract_sql_deps("insert into emp (id) values (1)")
    assert t == {"emp"}

    t, _ = extract_sql_deps("select id into l_local from emp where id = :x")
    assert "l_local" not in t
    assert t == {"emp"}


def test_extract_update_and_delete():
    t, _ = extract_sql_deps("update emp set salary = 1 where id = 2")
    assert t == {"emp"}
    t, _ = extract_sql_deps("delete from emp where id = 2")
    assert t == {"emp"}


def test_extract_merge_into():
    t, _ = extract_sql_deps("""
        merge into target_tbl t
        using source_tbl s on (t.id = s.id)
        when matched then update set t.v = s.v
    """)
    # `INTO target_tbl` is captured. `using source_tbl` is NOT (we don't
    # parse MERGE USING in v1) — documented limitation.
    assert "target_tbl" in t


def test_extract_dblink():
    t, d = extract_sql_deps("select * from emp_archive@prod_db")
    assert t == {"emp_archive"}
    assert d == {"prod_db"}


def test_extract_schema_qualified_name():
    t, _ = extract_sql_deps("select * from hr.employees")
    assert t == {"hr.employees"}


def test_extract_strings_dont_pollute():
    """A SQL string literal like `'FROM employees'` must NOT be parsed as a
    real FROM clause."""
    t, _ = extract_sql_deps(
        "select id, 'FROM employees' as note from real_table"
    )
    assert t == {"real_table"}


def test_extract_comments_dont_pollute():
    t, _ = extract_sql_deps("""
        -- from fake_table
        /* also from another_fake */
        select id from real_table
    """)
    assert t == {"real_table"}


def test_collect_packages_from_qualified_calls():
    from pell.deps import collect_module_deps
    m = parse("""
        module m;
        pub fn f(x: text) -> number {
            let h = dbms_utility::get_hash_value(x, 0, 1073741824);
            log::info("hashed");
            return h;
        }
    """)
    deps = collect_module_deps(m)
    assert "dbms_utility" in deps["packages"]
    assert "log" in deps["packages"]


def test_pell_runtime_in_packages_when_errors_declared():
    from pell.deps import collect_module_deps
    m = parse("""
        module m;
        pub error NotFound { id: number }
        pub fn nothing() {}
    """)
    deps = collect_module_deps(m)
    assert "pell_runtime" in deps["packages"]


def test_no_packages_when_no_qualified_refs():
    from pell.deps import collect_module_deps
    m = parse("""
        module m;
        pub fn double(x: number) -> number { return x * 2; }
    """)
    deps = collect_module_deps(m)
    assert deps["packages"] == []


def test_nested_qualifier_takes_everything_before_last():
    """`foo::bar::baz` → package `foo.bar`, member `baz`."""
    from pell.deps import _package_from_qualified
    assert _package_from_qualified("dbms_utility::get_hash_value") == "dbms_utility"
    assert _package_from_qualified("foo::bar::baz") == "foo.bar"
    assert _package_from_qualified("bare_name") is None


def test_collect_module_deps_aggregates_across_fns():
    m = parse("""
        module hr.employees;

        pub seq emp_id_seq;

        pub fn get_emp(id: number) -> number {
            let n = sql!{ select count(*) from employees where id = :id }.one()?;
            return n;
        }

        pub fn list_depts() -> number {
            let n = sql!{ select count(*) from departments }.one()?;
            return n;
        }
    """)
    deps = collect_module_deps(m)
    assert deps["tables"] == ["departments", "employees"]
    assert deps["sequences"] == ["emp_id_seq"]
    assert deps["dblinks"] == []


def test_emitter_emits_dependency_manifest():
    from pell.emitter import emit
    m = parse("""
        module hr.employees;
        pub fn f() -> number {
            return sql!{ select count(*) from employees }.one()?;
        }
    """)
    sql = emit(m)
    assert "Dependencies (extracted from pell source):" in sql
    assert "tables (incl. views/synonyms):" in sql
    assert "employees" in sql


def test_emitter_skips_manifest_when_no_deps():
    """A module with no SQL, no sequences, no DBLINKs shouldn't emit an
    empty Dependencies section."""
    from pell.emitter import emit
    m = parse("""
        module trivial;
        pub fn double(x: number) -> number { return x * 2; }
    """)
    sql = emit(m)
    assert "Dependencies (extracted from pell source):" not in sql
