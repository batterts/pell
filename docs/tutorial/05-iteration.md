---
title: Iteration: lists, for loops, FORALL
parent: Tutorial
nav_order: 5
---

This chapter covers every way pell iterates: cursor for-loops,
list-typed locals, range expressions, and the `forall` construct for
bulk DML.

## `for x in <list>` — iterate a list-typed local

```pell
let xs: list<number> = [1, 2, 3, 4, 5];
for n in xs {
    log::info("n = {n}");
}
```

lowers to:

```sql
DECLARE
  l_xs t_number_list;
  l_n_iter NUMBER;
BEGIN
  l_xs(1) := 1; l_xs(2) := 2; l_xs(3) := 3; l_xs(4) := 4; l_xs(5) := 5;
  FOR i_n IN l_xs.FIRST .. l_xs.LAST LOOP
    l_n_iter := l_xs(i_n);
    log.info('n = ' || l_n_iter);
  END LOOP;
END;
```

`l_n_iter` is a per-iteration shadow assigned from `l_xs(i_n)` —
that's so references to `n` inside the body resolve to a real local,
not a collection-index expression.

## `for ... in xs.indices()` — when you want the index

```pell
for i in xs.indices() {
    log::info("xs({i}) = {xs(i)}");
}
```

lowers to a bare integer-range loop:

```sql
FOR i IN l_xs.FIRST .. l_xs.LAST LOOP
  log.info('xs(' || i || ') = ' || l_xs(i));
END LOOP;
```

## `for row in sql!{...}` — cursor FOR loop

This is the most common iteration pattern: stream rows directly from a
query without materializing them in memory.

```pell
pub fn print_active() {
    for row in sql! {
        select id, name from employees where active = 1
    } {
        log::info("{row.id}: {row.name}");
    }
}
```

lowers to a PL/SQL cursor FOR loop. Oracle handles open/fetch/close
automatically. Use this for any result set you don't need to keep around.

If you need the whole result set as data, use `.collect()` (see
[SQL blocks](./04-sql-blocks.md)).

## `forall` — bulk DML

When you have a list of values and want to insert/update/delete one
row per value, use `forall`. This binds the entire list to a single
DML statement; Oracle drives the operation in a single context switch
instead of one per row, which is dramatically faster:

```pell
pub fn insert_all(ids: list<number>, names: list<text>) {
    forall i in ids.indices() {
        sql! {
            insert into employees (id, name)
              values (:ids(i), :names(i))
        }
    }
}
```

lowers to:

```sql
PROCEDURE insert_all(p_ids IN t_number_list, p_names IN t_text_list) IS
BEGIN
  FORALL i IN p_ids.FIRST .. p_ids.LAST
    INSERT INTO employees (id, name)
      VALUES (p_ids(i), p_names(i));
END insert_all;
```

The body of `forall` must be a **single DML statement**. That's a hard
restriction — Oracle's FORALL only accepts one statement. To do
multiple DMLs in bulk, use multiple FORALL blocks.

### Checking per-row results with `bulk.rowcount(i)`

After a `forall`, Oracle exposes `SQL%BULK_ROWCOUNT(i)` — the
rows-affected for the i-th iteration. Pell surfaces this as
`bulk.rowcount(i)`:

```pell
forall i in ids.indices() {
    sql! { update employees set active = 0 where id = :ids(i) }
}
for i in ids.indices() {
    log::info("row {i}: {bulk.rowcount(i)} update(s)");
}
```

`bulk.total()` returns the total rows affected (SQL%ROWCOUNT).

## Numeric range loops — `for i in a .. b`

Pell supports inclusive (`..=`) and exclusive (`..`) ranges:

```pell
for i in 0 .. 10 {       // 0, 1, ..., 9 (exclusive)
    ...
}

for i in 1 ..= 5 {       // 1, 2, 3, 4, 5 (inclusive)
    ...
}
```

lowers to:

```sql
FOR i IN 0 .. 9 LOOP    -- (10) - 1
  ...
END LOOP;

FOR i IN 1 .. 5 LOOP
  ...
END LOOP;
```

> **Note**: at present pell's parser accepts range expressions only
> in specific positions inside `for`. If you hit a parse error trying
> to use a range as a value, declare an intermediate list.

## Iterating records returned from a query into a list

Stream + collect patterns can be combined:

```pell
pub record EmpRow { id: number, name: text }

pub fn senior_employees(min_level: number) -> list<EmpRow> {
    let rows: list<EmpRow> = sql! {
        select id, name from employees where level >= :min_level
    }.collect();
    let seniors: list<EmpRow> = [];
    for r in rows {
        if r.level >= min_level + 2 {
            seniors.push(r);  // not yet implemented in v0 — see below
        }
    }
    return seniors;
}
```

> `list.push()` isn't in v0 yet. For now, filter in the SQL or build
> two list locals manually. The chapter on stdlib will be updated when
> push/extend land.

## Common pitfalls

### Loop variable shadowing

If your loop variable name collides with an outer local, the loop
variable wins inside the body. Don't reuse names:

```pell
let i: number = 100;
for i in 0 .. 5 {
    log::info("{i}");    // 0, 1, 2, 3, 4 — outer i is shadowed
}
log::info("outer = {i}");  // 100
```

### Forgetting `.collect()` for filtered iteration

```pell
// works — cursor for loop, no materialization
for row in sql! { select id from employees } {
    process(row.id);
}

// also works, but loads everything into memory first
let rows: list<EmpRow> = sql! { select id, name from employees }.collect();
for row in rows {
    process(row);
}
```

Prefer the cursor form unless you genuinely need the list (to pass to
another function, sort, or iterate twice).

### FORALL with non-DML body

```pell
forall i in ids.indices() {
    log::info("inserting {ids(i)}");      // ERROR — log::info isn't DML
    sql! { insert into ... };
}
```

FORALL only accepts a single DML statement. To log around it, use a
regular `for` loop (and lose the bulk-binding speedup), or do the
logging in batch before/after.

## Where to go next

- [Errors and @retry](./06-errors-and-retry.md) — what happens when a
  loop iteration fails
- [Pipelined functions](./08-pipelined-and-parallel.md) — for
  *producing* a stream of rows for SQL to consume