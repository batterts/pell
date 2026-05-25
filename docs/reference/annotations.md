---
title: Annotations
parent: Reference
nav_order: 3
---

Annotations attach behavior to items (fns, errors, types). They use
`@name` or `@name(args, kwargs)` syntax. This page lists every
annotation in alphabetical order.

## `@autonomous`

```pell
@autonomous
pub fn write_audit_log(event: text) {
    sql! { insert into audit_log(event, ts) values (:event, sysdate) };
}
```

Wraps the body in PL/SQL's `PRAGMA AUTONOMOUS_TRANSACTION` semantics —
the fn runs in its own transaction, isolated from the caller. Useful
for audit logging that should commit even if the parent transaction
rolls back.

Lowering adds a `COMMIT` on success and `ROLLBACK + RAISE` on error
(autonomous transactions must close before returning, or Oracle
raises ORA-06519).

Incompatible with: `@udf`, `@retry`.

## `@binds(...)`

```pell
unsafe fn count_active(tname: text, status: text) -> number
    @touches(employees)
    @binds(status)
{
    return exec_dyn("select count(*) from {tname} where status = :status").one();
}
```

(Inside `unsafe fn` only.) Declares which pell parameters are referenced
as `:bind` variables inside `exec_dyn(...)` strings. The compiler
validates that every `:name` in the dynamic SQL is listed in `@binds`,
and emits the `USING` clause to pass them positionally.

If a bind is declared but not used, you get a warning. If a `:name` is
used but not declared, the compiler errors out.

## `@deterministic`

```pell
@deterministic
pub fn double(n: number) -> number { return n * 2; }
```

Lowers to PL/SQL's `DETERMINISTIC` clause. Tells Oracle the function
returns the same value for the same inputs, so it can cache calls
within a query.

Use for pure functions. Don't use for functions that read tables,
read SYS_CONTEXT, or call SYSDATE/RANDOM — they're not deterministic
in Oracle's sense.

## `@panic`

```pell
@panic
pub error InvariantViolation { msg: text }
```

(On `error` declarations.) Marks the error as "never retry." Assigns
a SQLCODE in the panic range (-20300..-20399); pell_re's `@retry`
loop calls `pell_is_panic(SQLCODE)` and re-raises immediately rather
than retrying when the code matches.

## `@parallel(...)`

Two distinct uses depending on where it appears:

### On a `@pipelined` fn

```pell
@pipelined
@parallel(partition = hash(id))
pub fn classify(rows: cursor<Row>) -> stream<Scored> { ... }
```

Enables parallel execution. Requires a partition strategy:

| Pell                          | Oracle PARALLEL_ENABLE clause             |
|-------------------------------|--------------------------------------------|
| `partition = any`             | `PARTITION p BY ANY`                       |
| `partition = hash(col)`       | `PARTITION p BY HASH(col)`                 |
| `partition = range(col)`      | `PARTITION p BY RANGE(col)`                |
| `order = asc(col)`            | `ORDER p BY (col)`                         |
| `order = desc(col)`           | `ORDER p BY (col) DESC`                    |
| `cluster = col1, col2`        | `CLUSTER p BY (col1, col2)`                |

`partition = hash(col)` requires a strongly-typed REF CURSOR
(`cursor<T>`) — pell auto-emits the type alias in the package spec.

### On an `aggregate`

```pell
@parallel
pub aggregate sum_squares(n: number) -> number { ... }
```

Marks the aggregate as parallel-safe. Requires a `merge(other: t)`
method on the aggregate so Oracle can combine sub-aggregates from
parallel slaves.

## `@pipelined`

```pell
@pipelined
pub fn split_letters(s: text) -> stream<Slice> {
    for i in 1 .. length(s) {
        yield Slice { idx: i, value: substr(s, i, 1) };
    }
}
```

Marks the fn as a PL/SQL `PIPELINED` function. Body uses `yield` to
emit rows; return type must be `stream<Record>`.

The lowering generates the supporting OBJECT TYPE and TABLE TYPE at
schema level, plus the `RETURN <nt_name> PIPELINED` signature.
Compose with `@parallel` for parallelism.

Incompatible with: `@retry`, `@autonomous`.

## `@propagate`

(Default for `error`; rarely written explicitly.) Marks the error as
"retry the body, but re-raise after the budget is exhausted."
Assigns a SQLCODE in the propagate range (-20200..-20299).

## `@retry(n=, ...)`

```pell
@retry(n=5, backoff=100, jitter=50, exponential=true, cap=2000)
pub fn fetch_external(url: text) -> text { ... }
```

Wraps the body in a SAVEPOINT-rollback retry loop on transient
failures.

| Parameter   | Default | Meaning                                                  |
|-------------|---------|-----------------------------------------------------------|
| `n`         | 3       | Max attempts (including the first).                     |
| `backoff`   | 0       | Base wait between attempts, ms.                         |
| `jitter`    | 0       | Random jitter added per attempt, ms.                    |
| `exponential` | false | If true, backoff doubles each attempt.                  |
| `cap`       | none    | Maximum total wait per attempt, ms (with exponential).  |

Errors in the `@panic` SQLCODE range bypass retry. Errors in `@skip`
range are noted as "this iteration succeeded" — the loop exits.

Incompatible with: `@autonomous`, `@pipelined`, `finally`.

## `@schema(...)`

(Planned, not in v0.) Will attach a JSON Schema validation
constraint to a record or JSON field, lowering to Oracle's `IS JSON
VALIDATING` constraint at the DDL level.

## `@skip`

```pell
@skip
pub error DuplicateSku { sku: text }
```

(On `error` declarations.) Marks the error as "this is recoverable
within the retry loop" — `@retry` exits normally on this error
without re-raising. Assigns a SQLCODE in the skip range
(-20100..-20199).

## `@touches(...)`

```pell
unsafe fn count_for(tname: text) -> number
    @touches(employees, departments, schedules.shifts)
{
    return exec_dyn("select count(*) from {tname}").one();
}
```

(Inside `unsafe fn` only.) Lists tables/views/packages referenced by
`exec_dyn(...)` strings. The compiler emits "pinning cursors" — never-
called CURSOR declarations with `WHERE 1=0` — so Oracle's
`ALL_DEPENDENCIES` sees the references.

Without `@touches`, dynamic SQL is invisible to Oracle's dependency
graph; dropping a referenced column won't INVALIDATE your package.

## `@udf`

```pell
@udf
pub fn sales_tax(amount: number, rate: number) -> number {
    return amount * rate;
}
```

Marks the fn as eligible for User-Defined Function execution mode,
which is much faster from SQL contexts than a regular PL/SQL function.

Restrictions: no DML, no autonomous transactions, no calls to
non-UDF functions, no I/O.

Incompatible with: `@autonomous`.