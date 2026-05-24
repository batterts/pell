# 6. Errors and @retry

PL/SQL's error model — declarative exceptions, `SQLCODE`, `WHEN OTHERS`
— is one of its worst ergonomics warts. pell layers a small typed
surface on top that gives you:

- Named, typed error variants with payloads.
- `Result<T, E>` return types.
- Automatic `@retry` on transient failures.
- A `finally` block that always runs without swallowing the original
  error.

This chapter walks the full surface.

## Declaring errors

An error is a record-shaped declaration with a category:

```pell
pub error EmployeeNotFound { id: number }

@skip
pub error AlreadyDeactivated { id: number }

@panic
pub error InvariantViolation { msg: text }
```

Each error gets:

- A typed payload (the fields).
- A category — `propagate` (default), `skip`, or `panic` — which
  controls how `@retry` treats it.
- An assigned SQLCODE in a dedicated range.

| Category    | SQLCODE range     | `@retry` behavior |
|-------------|-------------------|--------------------|
| `@skip`     | -20100 to -20199  | Skipped — retry continues to next iteration. |
| (propagate) | -20200 to -20299  | Re-raised after retry budget exhausted. |
| `@panic`    | -20300 to -20399  | Re-raised immediately — no retry. |

Errors lower to PRAGMA EXCEPTION_INIT entries in a generated
`pell_runtime` package:

```sql
hr_employees_employeenotfound EXCEPTION;
PRAGMA EXCEPTION_INIT(hr_employees_employeenotfound, -20200);
```

To install the runtime package once per schema, run:

```sh
./pell runtime > pell_runtime.sql
# install pell_runtime.sql on your Oracle instance
```

## Result<T, E> — the return-type wrapper

A fallible function returns `Result<T, E>`:

```pell
pub error EmployeeNotFound { id: number }

pub fn lookup(id: number) -> Result<text, EmployeeNotFound> {
    let name: text = sql! {
        select name from employees where id = :id
    }.one();
    return Ok(name);
}
```

At the type level, `Result<T, E>` lowers to just `T`. The error path
uses PL/SQL's exception machinery — there's no Either-style wrapping
at runtime.

`Ok(value)` returns successfully. To return an error, use `Err(...)`:

```pell
pub fn promote(id: number, to_level: number)
    -> Result<Unit, EmployeeNotFound | PolicyViolation>
{
    let current: number = sql! {
        select level from employees where id = :id
    }.one();
    if to_level <= current {
        return Err(PolicyViolation { reason: "can't demote" });
    }
    sql! { update employees set level = :to_level where id = :id };
    return Ok(());
}
```

The error union `EmployeeNotFound | PolicyViolation` is just for type
checking; both kinds raise the same way at runtime.

### Propagating errors with `?`

The `?` operator on a `Result<T, E>` unwraps `Ok(v)` to `v`, or
re-raises the error:

```pell
pub fn handle(id: number) -> Result<Unit, EmployeeNotFound> {
    let name: text = lookup(id)?;     // unwrap or re-raise
    log::info("got {name}");
    return Ok(());
}
```

## Custom NotFound handling on `sql!{}.one()`

If your function returns `Result<T, E>` where `E` includes a
`Something*NotFound` variant, pell's `.one()` will automatically wire
the NO_DATA_FOUND handler to raise *your* error instead of the bare
Oracle one. The naming convention: `<RecordName>NotFound` or
`<EntityName>NotFound`.

```pell
pub record EmpRow { id: number, name: text }
pub error EmpRowNotFound { id: number }

pub fn lookup(id: number) -> Result<EmpRow, EmpRowNotFound> {
    let row: EmpRow = sql! {
        select id, name from employees where id = :id
    }.one();
    return Ok(row);
}
```

If the SELECT returns no rows, you get an `EmpRowNotFound` exception
with `id = <p_id>` populated, not NO_DATA_FOUND.

Override the auto-wiring with explicit chain:

```pell
let row: EmpRow = sql! { select ... }.one()
    .if_empty(EmpRowNotFound { id: id })
    .if_many(InvariantViolation { msg: "duplicate id" });
```

## `@retry` — automatic retry on transient failures

Apply `@retry` to retry the body of a function on transient errors:

```pell
@retry(n=3, backoff=100, jitter=50)
pub fn fetch_external_quote(symbol: text) -> text {
    return sql! {
        select rest_call('quotes/' || :symbol) from dual
    }.one();
}
```

lowers to a SAVEPOINT-rollback retry loop:

```sql
FUNCTION fetch_external_quote(p_symbol IN VARCHAR2) RETURN VARCHAR2 IS
  l_result VARCHAR2(4000);
  l_pell_attempt PLS_INTEGER := 0;
BEGIN
  LOOP
    SAVEPOINT pell_attempt;
    BEGIN
      SELECT rest_call('quotes/' || p_symbol)
        INTO l_result
        FROM dual;
      RETURN l_result;
    EXCEPTION
      WHEN OTHERS THEN
        IF pell_is_panic(SQLCODE) THEN RAISE; END IF;
        l_pell_attempt := l_pell_attempt + 1;
        ROLLBACK TO pell_attempt;
        IF l_pell_attempt >= 3 THEN RAISE; END IF;
        DBMS_SESSION.SLEEP(0.1 + DBMS_RANDOM.VALUE(0, 0.05));
    END;
  END LOOP;
END fetch_external_quote;
```

What `@retry` does:

- Wraps the body in a SAVEPOINT + ROLLBACK TO loop, so each retry sees
  the same pre-call DB state.
- Catches `WHEN OTHERS` to handle any transient error.
- Calls a generated `pell_is_panic(SQLCODE)` helper that returns TRUE
  for: any error in the panic SQLCODE range (-20300..-20399), plus
  known Oracle panic codes (ZERO_DIVIDE, VALUE_ERROR, etc.). On
  panic, the loop re-raises immediately without retrying.
- After the budget is exhausted, re-raises the last error.

Parameters:

| Parameter   | Default | Meaning                                              |
|-------------|---------|-------------------------------------------------------|
| `n`         | 3       | Max attempts (including the first).                  |
| `backoff`   | 0       | Base wait between attempts, in milliseconds.         |
| `jitter`    | 0       | Random jitter added to backoff, in ms.               |
| `exponential` | false | If true, backoff doubles each attempt.               |
| `cap`       | (none)  | Maximum total wait per attempt, ms.                  |

`@retry` is incompatible with `@autonomous` and `@pipelined` — the
compiler will reject those combinations with a clear error.

## `finally` — always run, even on error

When you need cleanup that must happen regardless of success or
failure, use a `finally` block on the function:

```pell
pub fn process_with_cleanup(id: number) -> number {
    let n: number = expensive_operation(id);
    return n;
} finally {
    log::info("operation complete for {id}");
    release_resource(id);
}
```

lowers to:

```sql
FUNCTION process_with_cleanup(p_id IN NUMBER) RETURN NUMBER IS
  l_n NUMBER;

  PROCEDURE pell_finally_body IS
  BEGIN
    log.info('operation complete for ' || p_id);
    release_resource(p_id);
  END pell_finally_body;
BEGIN
  BEGIN
    l_n := expensive_operation(p_id);
    pell_finally_body;
    RETURN l_n;
  EXCEPTION
    WHEN OTHERS THEN
      BEGIN pell_finally_body; EXCEPTION WHEN OTHERS THEN NULL; END;
      RAISE;
  END;
END process_with_cleanup;
```

Critically, the original error is *re-raised* — `finally` runs the
cleanup, but if it fails its own error is silently ignored so the
original propagates. This is the "log-and-rethrow" idiom done right;
do NOT write your own `WHEN OTHERS THEN log; raise;` block in pell —
use `finally`.

### Why not just write `WHEN OTHERS` yourself?

It's tempting to write:

```pell
// DON'T DO THIS
try {
    risky_op();
} catch _ {
    log::error("something went wrong");
}
```

This swallows the error. The next caller has no idea anything failed.
In pell, the *only* contexts where `WHEN OTHERS` is generated are:
1. Inside `@retry` (to decide whether to retry).
2. Inside `finally` (to ensure cleanup runs, then re-raise).

You never write a wildcard catch yourself. Always re-raise, always via
`finally`.

## Combining categories

Multiple error variants can have different categories:

```pell
@skip
pub error DuplicateSku { sku: text }

pub error InventoryNotFound { id: number }

@panic
pub error InvariantViolation { msg: text }

@retry(n=5, exponential=true, backoff=100, cap=2000)
pub fn import_one(sku: text) -> Result<Unit, DuplicateSku | InventoryNotFound> {
    ...
}
```

If `DuplicateSku` is raised, `@retry` skips it (logged once and
treated as "this one's done"). If `InventoryNotFound` is raised, it
re-raises after retries. If `InvariantViolation` is raised, it
re-raises immediately — no retry.

## Where to go next

- [Types, sealed types, aggregates](./07-types-and-aggregates.md) —
  pell's user-defined types for more structured domains.
- [Dynamic SQL](./09-dynamic-sql.md) — where `unsafe fn` opens up
  exec_dyn for cases that need it.
