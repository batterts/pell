# 4. SQL blocks

The `sql!{}` macro is how pell programs talk to the database. Inside
the braces you write Oracle SQL exactly as you would in any other
client; pell handles bind extraction, terminator splicing, and result
shape. This chapter covers every common pattern.

## The basic shape

```pell
sql! { select id, name from employees where active = 1 }
```

That on its own is a *cursor expression* — useful as the iterable of a
`for` loop, but you can't read its rows yet. You attach a *terminator*
to say what you want done with the rows.

| Terminator           | Behavior                                              |
|----------------------|-------------------------------------------------------|
| `.one()`             | Exactly one row. NO_DATA_FOUND / TOO_MANY_ROWS error. |
| `.one_or_none()`     | Zero or one row. Returns `Option<T>`.                 |
| `.first()`           | First row only. NULL if no rows.                      |
| `.collect()`         | All rows into a `list<T>`.                            |
| `.rowcount()`        | For DML — returns number of rows affected.            |
| (none, in `for`)     | Streams via cursor FOR loop.                          |

## Reading a single row

```pell
pub fn lookup_name(id: number) -> text {
    let name: text = sql! {
        select name from employees where id = :id
    }.one();
    return name;
}
```

lowers to:

```sql
FUNCTION lookup_name(p_id IN NUMBER) RETURN VARCHAR2 IS
  l_name VARCHAR2(4000);
BEGIN
  BEGIN
    SELECT name
      INTO l_name
      FROM employees WHERE id = p_id;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN RAISE;
    WHEN TOO_MANY_ROWS THEN RAISE;
  END;
  RETURN l_name;
END lookup_name;
```

The `:id` inside the SQL is a bind reference to the pell variable
`id`. Pell extracts binds at compile time and rewrites them to the
PL/SQL local name (`p_id` because it's a parameter, `l_id` for locals).

### Multi-column rows → records

If your SELECT returns more than one column, the receiving local must
be a record type and the SELECT list must match in count and order:

```pell
pub record EmpRow { id: number, name: text }

pub fn lookup(id: number) -> EmpRow {
    let row: EmpRow = sql! {
        select id, name from employees where id = :id
    }.one();
    return row;
}
```

lowers to:

```sql
BEGIN
  SELECT id, name
    INTO l_row.id, l_row.name
    FROM employees WHERE id = p_id;
EXCEPTION
  WHEN NO_DATA_FOUND THEN RAISE;
  WHEN TOO_MANY_ROWS THEN RAISE;
END;
```

### Custom NotFound exceptions

If you want a domain-specific error rather than a bare NO_DATA_FOUND,
declare it and pell will wire it up:

```pell
pub error EmployeeNotFound { id: number }

pub fn lookup(id: number) -> Result<EmpRow, EmployeeNotFound> {
    let row: EmpRow = sql! {
        select id, name from employees where id = :id
    }.one();
    return Ok(row);
}
```

Pell sees the `Result<EmpRow, EmployeeNotFound>` return type and
rewires the NO_DATA_FOUND handler to raise the typed error. See
[Errors](./06-errors-and-retry.md).

## Reading multiple rows

```pell
pub fn list_active() -> list<EmpRow> {
    let xs: list<EmpRow> = sql! {
        select id, name from employees where active = 1
    }.collect();
    return xs;
}
```

lowers to:

```sql
FUNCTION list_active RETURN t_emp_row_list IS
  l_xs t_emp_row_list;
BEGIN
  SELECT id, name
    BULK COLLECT INTO l_xs
    FROM employees WHERE active = 1;
  RETURN l_xs;
END list_active;
```

`.collect()` becomes BULK COLLECT — a single round-trip that loads all
rows. For very large result sets, prefer streaming via a `for` loop
(below) so you don't materialize everything in memory.

## Streaming with `for`

When you want to process rows one at a time without loading them all:

```pell
pub record EmpRow { id: number, name: text }

pub fn print_active() {
    for row in sql! {
        select id, name from employees where active = 1
    } {
        log::info("emp {row.id}: {row.name}");
    }
}
```

lowers to:

```sql
PROCEDURE print_active IS
BEGIN
  FOR row IN (
    SELECT id, name FROM employees WHERE active = 1
  ) LOOP
    log.info('emp ' || row.id || ': ' || row.name);
  END LOOP;
END print_active;
```

A PL/SQL cursor FOR loop. Oracle automatically opens, fetches, and
closes the cursor; rows are pulled one (or a batch) at a time.

## DML

INSERT / UPDATE / DELETE / MERGE work the same way. Use `.rowcount()`
to get the affected-row count:

```pell
pub fn deactivate(id: number) -> number {
    let n: number = sql! {
        update employees set active = 0 where id = :id
    }.rowcount();
    return n;
}
```

lowers to:

```sql
FUNCTION deactivate(p_id IN NUMBER) RETURN NUMBER IS
  l_n NUMBER;
BEGIN
  UPDATE employees SET active = 0 WHERE id = p_id;
  l_n := SQL%ROWCOUNT;
  RETURN l_n;
END deactivate;
```

If you don't need the count, drop the `.rowcount()` and assign nothing:

```pell
pub fn deactivate(id: number) {
    sql! { update employees set active = 0 where id = :id };
}
```

## INSERT ... RETURNING

When you want the new row's ID (or other generated columns) back from
an INSERT:

```pell
pub fn add_employee(name: text) -> number {
    let id: number = sql! {
        insert into employees (id, name)
          values (employees_seq.nextval, :name)
          returning id
    }.returning::<number>().one();
    return id;
}
```

lowers to:

```sql
FUNCTION add_employee(p_name IN VARCHAR2) RETURN NUMBER IS
  l_id NUMBER;
BEGIN
  INSERT INTO employees (id, name)
    VALUES (employees_seq.nextval, p_name)
    RETURNING id INTO l_id;
  RETURN l_id;
END add_employee;
```

The `.returning::<T>().one()` chain tells pell what type to expect back
and asserts exactly one row.

## Locking modifiers

After a SELECT, chain locking methods:

```pell
pub fn lock_employee(id: number) -> EmpRow {
    let row: EmpRow = sql! {
        select id, name from employees where id = :id
    }.for_update().nowait().one();
    return row;
}
```

lowers to:

```sql
SELECT id, name
  INTO l_row.id, l_row.name
  FROM employees WHERE id = p_id
  FOR UPDATE NOWAIT;
```

Available locking methods: `.for_update()`, `.nowait()`,
`.skip_locked()`, `.wait(n)`, `.for_update_of(col1, col2)`.

## Bind safety

Pell rewrites `:name` references to the local PL/SQL name (`p_name`,
`l_name`, etc.), so the value is always passed as a bind parameter —
not concatenated into the SQL string. This means:

- No SQL injection risk for normal queries.
- Oracle's shared-pool cache hits on the rewritten SQL, regardless of
  the parameter value.
- Date/timestamp parameters use the right binding, no `TO_DATE`
  hoops.

For dynamic SQL where you need to splice an identifier (a table name,
a column name) you have to opt into `unsafe fn` + `exec_dyn`. See
[Dynamic SQL](./09-dynamic-sql.md).

## Multi-line SQL and whitespace

Pell preserves whitespace inside `sql!{}` blocks. Write your SQL with
the indentation that reads best:

```pell
sql! {
    select e.id,
           e.name,
           d.name as department
      from employees e
      join departments d on d.id = e.dept_id
     where e.active = 1
       and e.salary > :min_salary
     order by e.salary desc
}
```

Comments inside `sql!{}` work normally — both `--` line comments and
`/* ... */` blocks pass through unchanged.

## Where to go next

- [Iteration](./05-iteration.md) — `forall` for bulk DML
- [Errors and @retry](./06-errors-and-retry.md) — Custom error types
  + automatic retry
- [Pipelined and parallel](./08-pipelined-and-parallel.md) —
  table-function streaming
