# Bulk-insert with FORALL

## Problem

You have a list of records you want to insert into a table. Doing
them one row at a time is too slow.

## Solution

Use `forall` to bind the entire list to one DML statement. Oracle
runs the operation in a single PL/SQL ↔ SQL context switch instead
of one per row.

```pell
module bulk_demo;

pub fn insert_employees(ids: list<number>, names: list<text>) {
    forall i in ids.indices() {
        sql! {
            insert into employees (id, name) values (:ids(i), :names(i))
        }
    }
}
```

## How it lowers

```sql
PROCEDURE insert_employees(p_ids IN t_number_list,
                           p_names IN t_text_list) IS
BEGIN
  FORALL i IN p_ids.FIRST .. p_ids.LAST
    INSERT INTO employees (id, name) VALUES (p_ids(i), p_names(i));
END insert_employees;
```

The PL/SQL FORALL statement makes Oracle execute the INSERT once with
a bound array, dramatically faster than a row-by-row loop.

## Performance

For 10,000 rows, expect roughly:
- Row-by-row INSERT (with `for`): 5–15 seconds.
- FORALL bulk INSERT: 50–500 ms — often 10–30× faster.

The speedup is bigger for larger batches because the per-row overhead
of a context switch dominates at small sizes.

## Checking per-row results

After a FORALL, `bulk.rowcount(i)` is the number of rows affected by
the i-th iteration (1 for inserts of one row, 0 if a constraint
violation occurred).

```pell
forall i in ids.indices() {
    sql! { update employees set status = 'X' where id = :ids(i) }
}
for i in ids.indices() {
    log::info("row {i}: updated {bulk.rowcount(i)} rows");
}
let total: number = bulk.total();   // sum across all iterations
```

## Restrictions

- The `forall` body must be **exactly one** DML statement. Oracle's
  FORALL doesn't accept multiple statements per iteration.
- The iterable must be a list-typed local variable, addressable by
  `.indices()`.
- The DML must use `:listname(i)` for the iteration variable —
  pell rewrites this to PL/SQL's expected form.

If you need multiple operations per iteration, run multiple FORALLs
in sequence:

```pell
forall i in ids.indices() {
    sql! { update employees set archived = 1 where id = :ids(i) }
}
forall i in ids.indices() {
    sql! { delete from employee_activity where employee_id = :ids(i) }
}
```

Each FORALL still gets the bulk-binding speedup.

## SAVE EXCEPTIONS — continue past errors

To collect errors instead of aborting on the first failure, use
Oracle's `SAVE EXCEPTIONS` clause inside a raw SQL block. Pell v0
doesn't yet expose this as a method; for now write it directly:

```pell
forall i in ids.indices() {
    sql! { insert into employees (id, name) values (:ids(i), :names(i)) }
    // SAVE EXCEPTIONS form not yet in surface; coming soon.
}
```

When that surface lands, it'll look like
`forall i in ids.indices() save_exceptions { ... }` and expose the
errored indices via `bulk.error(i)`.

## See also

- [Iteration](../tutorial/05-iteration.md)
- [SQL blocks](../tutorial/04-sql-blocks.md)
