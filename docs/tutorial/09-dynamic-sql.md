# 9. Dynamic SQL: unsafe, exec_dyn, @touches, @binds

Sometimes the SQL you need to run can't be expressed at compile time:
the table name comes from configuration, the column list is built up
based on user input, the query has dozens of optional WHERE clauses.
Pell handles this through `unsafe fn` + `exec_dyn`, plus annotations
that re-create the dependency-tracking and bind safety that
compile-time SQL gets for free.

This chapter explains *why* dynamic SQL is gated, what the annotations
do, and how to write `unsafe fn` correctly.

## The problem

Static SQL has three properties that dynamic SQL loses:

1. **Bind safety** — params are bound, not concatenated. No injection.
2. **Dependency tracking** — Oracle's `ALL_DEPENDENCIES` knows your
   package uses `employees.salary`; if someone drops the column, your
   package becomes INVALID and Oracle warns you.
3. **Plan caching** — Oracle's shared pool caches the parsed SQL, so
   repeated calls don't re-parse.

Dynamic SQL is a string. Oracle sees it for the first time on each
EXECUTE IMMEDIATE call, has no idea what it touches, and re-parses
unless you carefully use bind variables.

Pell forces you to opt into dynamic SQL via `unsafe fn`, and asks you
to declare what tables/columns the dynamic SQL touches and what binds
it uses. The compiler then generates "pinning cursors" so
`ALL_DEPENDENCIES` works, and validates that you use the binds you
declared.

## Hello dynamic-SQL

```pell
unsafe fn count_in_table(tname: text) -> number
    @touches(employees, departments)
{
    let n: number = exec_dyn("select count(*) from {tname}").one();
    return n;
}
```

This:

- `unsafe fn` — required to use `exec_dyn`.
- `@touches(employees, departments)` — names the tables the dynamic
  SQL might reference. The compiler emits pinning cursors so Oracle
  knows. (If you forget a table that the dynamic SQL hits, the package
  still works — but `ALL_DEPENDENCIES` won't show the link, and you'll
  lose the "auto-invalidate on drop" behavior.)
- `{tname}` inside the string — pell interpolation, becomes `|| p_tname
  ||` in the emitted EXECUTE IMMEDIATE.

The lowering:

```sql
FUNCTION count_in_table(p_tname IN VARCHAR2) RETURN NUMBER IS
  l_n NUMBER;
BEGIN
  EXECUTE IMMEDIATE 'select count(*) from ' || p_tname
    INTO l_n;
  RETURN l_n;
END count_in_table;

-- Pinning declaration (generated, never called)
PROCEDURE pell_dep_pinning IS
  CURSOR pin_employees   IS SELECT 1 FROM employees   WHERE 1=0;
  CURSOR pin_departments IS SELECT 1 FROM departments WHERE 1=0;
BEGIN NULL; END pell_dep_pinning;
```

## `@binds` — declaring dynamic bind variables

When your dynamic SQL uses bind variables, declare them with `@binds`:

```pell
unsafe fn count_active_in_table(tname: text, status: text) -> number
    @touches(employees, departments)
    @binds(status)
{
    let n: number = exec_dyn(
        "select count(*) from {tname} where status = :status"
    ).one();
    return n;
}
```

`:status` in the SQL string refers to the pell parameter `status`.
The lowering uses `USING` to pass the bind:

```sql
EXECUTE IMMEDIATE 'select count(*) from ' || p_tname || ' where status = :status'
  INTO l_n
  USING p_status;
```

If you put a `:name` in the dynamic SQL that's NOT declared in
`@binds`, the compiler errors out. If you declare a bind in `@binds`
that's not used, you'll get a warning but no error.

## `exec_dyn` and the safe-vs-dynamic split

Inside `unsafe fn`, you can use both `sql!{}` (static, safe) AND
`exec_dyn(...)` (dynamic). The annotations only constrain `exec_dyn`
output:

```pell
unsafe fn audit_and_count(tname: text) -> number
    @touches(employees)
    @binds()
{
    // Static SQL — fully checked, no annotation impact
    sql! { insert into audit_log(event) values ('count_called') };

    // Dynamic SQL — gated by @touches + @binds
    let n: number = exec_dyn("select count(*) from {tname}").one();
    return n;
}
```

This pattern is common: most of the body is static, with one or two
dynamic spots for tables/columns picked at runtime.

## Why `unsafe fn` and not just opt-in per call?

The annotations need to be declared somewhere accessible to the
dependency-pinning generator. Putting them on the function lets the
compiler aggregate all dynamic uses in the package and emit one
pinning procedure. It also forces you to think about the *function*'s
contract: which tables can this function touch? That's a healthier
mental boundary than per-call.

If you find yourself wanting `unsafe fn` for many functions in a
module, that's often a smell — the dynamic part can usually be
isolated to one fn that the static fns call.

## Cases where you probably *don't* need dynamic SQL

- **Schema-qualified names**: use the module name. `module hr.employees`
  installs to `hr.employees` without any `exec_dyn`.
- **A few optional WHERE clauses**: use `WHERE (:filter IS NULL OR col
  = :filter)` patterns in static SQL. Oracle's optimizer handles
  these well.
- **List of values in an IN clause**: use `WHERE col IN (SELECT
  column_value FROM TABLE(...))` with a TABLE function fed by a list.
- **Different sort columns based on input**: use `ORDER BY DECODE(:sort_col,
  'name', name, 'id', TO_CHAR(id))` patterns or `dbms_assert.simple_sql_name`
  with safer static lookup.

When you truly need it (e.g., the column list is variable, table
sharding by tenant), `unsafe fn` + `exec_dyn` is the right tool.

## Where to go next

- [JSON](./10-json.md) — typed JSON without `exec_dyn` games.
- [Pivot](./13-pivot.md) — typed and dynamic pivot, which is one of
  the cases where dynamic SQL is the natural fit.
