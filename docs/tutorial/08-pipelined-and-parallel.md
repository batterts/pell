# 8. Pipelined and parallel functions

A pipelined function streams rows back to a SQL caller — you write a
PL/SQL fn that yields rows one at a time, and `SELECT FROM TABLE(...)`
consumes them. Pell makes the syntax close to a generator and handles
the ceremony of OBJECT TYPES and nested-table types under the hood.

## Basic pipelined function

```pell
pub record Slice { idx: number, value: text }

@pipelined
pub fn split_letters(s: text) -> stream<Slice> {
    for i in 1 .. length(s) {
        yield Slice { idx: i, value: substr(s, i, 1) };
    }
}
```

Use it from SQL:

```sql
SELECT * FROM TABLE(split_letters('hello'));
-- IDX  VALUE
--   1  h
--   2  e
--   3  l
--   4  l
--   5  o
```

The pell lowering generates the OBJECT + nested-table + PIPELINED
function:

```sql
-- Schema-level OBJECT
CREATE OR REPLACE TYPE my_pkg_slice_obj AS OBJECT (
  idx NUMBER,
  value VARCHAR2(4000)
);
/
CREATE OR REPLACE TYPE my_pkg_slice_nt AS TABLE OF my_pkg_slice_obj;
/

-- The pipelined function inside the package
FUNCTION split_letters(p_s IN VARCHAR2) RETURN my_pkg_slice_nt PIPELINED IS
BEGIN
  FOR i IN 1 .. LENGTH(p_s) LOOP
    PIPE ROW(my_pkg_slice_obj(i, SUBSTR(p_s, i, 1)));
  END LOOP;
  RETURN;
END split_letters;
```

`yield` becomes `PIPE ROW(...)`. The terminating `RETURN;` is added by
pell automatically.

The return type must be `stream<RecordName>` where `RecordName` is a
record declared in the same module. The OBJECT and TABLE types are
generated once per record-name.

## Input streams via `cursor<T>`

To consume a stream of rows from SQL, declare a cursor input:

```pell
pub record Row { id: number, score: number }
pub record Scored { id: number, score: number, decile: number }

@pipelined
pub fn decile_classify(rows: cursor<Row>) -> stream<Scored> {
    for r in rows {
        let d: number = floor(r.score / 10);
        yield Scored { id: r.id, score: r.score, decile: d };
    }
}
```

Use it from SQL:

```sql
SELECT * FROM TABLE(decile_classify(CURSOR(
  SELECT id, score FROM raw_scores
)));
```

The lowering:

```sql
FUNCTION decile_classify(p_rows IN SYS_REFCURSOR) RETURN my_pkg_scored_nt PIPELINED IS
  l_row my_pkg_row_rec;
  l_d   NUMBER;
BEGIN
  LOOP
    FETCH p_rows INTO l_row;
    EXIT WHEN p_rows%NOTFOUND;
    l_d := FLOOR(l_row.score / 10);
    PIPE ROW(my_pkg_scored_obj(l_row.id, l_row.score, l_d));
  END LOOP;
  CLOSE p_rows;
  RETURN;
END decile_classify;
```

## `@parallel` — parallel pipelined execution

Add `@parallel(partition = hash(col))` to enable Oracle's parallel
query for the function. Each parallel slave gets a subset of rows
based on the partition strategy:

```pell
@pipelined
@parallel(partition = hash(id))
pub fn decile_classify(rows: cursor<Row>) -> stream<Scored> {
    for r in rows {
        yield Scored { id: r.id, score: r.score, decile: floor(r.score / 10) };
    }
}
```

lowers to:

```sql
FUNCTION decile_classify(p_rows IN t_row_cur)
  RETURN my_pkg_scored_nt PIPELINED
  PARALLEL_ENABLE(PARTITION p_rows BY HASH(id))
IS
  ...
```

Partition strategies:

| Pell                           | Oracle clause                            |
|--------------------------------|------------------------------------------|
| `partition = any`              | `PARTITION p_rows BY ANY`                |
| `partition = hash(col)`        | `PARTITION p_rows BY HASH(col)`          |
| `partition = range(col)`       | `PARTITION p_rows BY RANGE(col)`         |
| `order = asc(col)` etc.        | `ORDER p_rows BY (col)`                  |
| `cluster = col1, col2`         | `CLUSTER p_rows BY (col1, col2)`         |

`partition = hash(col)` requires a **strongly-typed REF CURSOR**.
Pell auto-emits the `TYPE t_<name>_cur IS REF CURSOR RETURN t_<name>;`
declaration in the package spec so Oracle can resolve the column
reference at compile time.

## When to use pipelined functions

- **Decorating a query**: add computed columns, filter, transform —
  the result is still a SQL row source so it composes with `WHERE`,
  `JOIN`, `GROUP BY`.
- **Streaming external data**: e.g. parse a CLOB or fetch from a
  remote service and present results as rows.
- **Large transformations** that would otherwise materialize a huge
  intermediate result.

Avoid pipelined functions when a regular SQL query would work — they
add per-row overhead that SQL alone doesn't have.

## Where to go next

- [Dynamic SQL](./09-dynamic-sql.md) — `unsafe fn` for cases where
  the table or column name isn't known at compile time.
- [Pivot](./13-pivot.md) — built-in support for Oracle's PIVOT
  clause (which is often what people reach for pipelined functions
  for).
