---
title: Stream rows with a pipelined function
parent: Cookbook
nav_order: 5
---

## Problem

You want a function that returns "a stream of rows" — usable as the
source of a SELECT query, with no intermediate materialization.

## Solution

Declare a `@pipelined` function that returns `stream<Record>`. Use
`yield` to emit each row.

```pell
module text_utils;

pub record Slice { idx: number, value: text }

@pipelined
pub fn split_chars(s: text) -> stream<Slice> {
    for i in 1 .. length(s) + 1 {
        yield Slice { idx: i, value: substr(s, i, 1) };
    }
}
```

Use it from SQL:

```sql
SELECT * FROM TABLE(text_utils.split_chars('hello'));
```

```
IDX  VALUE
  1  h
  2  e
  3  l
  4  l
  5  o
```

## How it lowers

The pell compiler generates the OBJECT TYPE + nested-table TYPE at
schema level, then the package fn:

```sql
-- Schema-level (emitted before the package)
CREATE OR REPLACE TYPE text_utils_slice_obj AS OBJECT (
  idx NUMBER,
  value VARCHAR2(4000)
);
/
CREATE OR REPLACE TYPE text_utils_slice_nt AS TABLE OF text_utils_slice_obj;
/

-- In the package
FUNCTION split_chars(p_s IN VARCHAR2) RETURN text_utils_slice_nt PIPELINED IS
BEGIN
  FOR i IN 1 .. LENGTH(p_s) + 1 LOOP
    PIPE ROW(text_utils_slice_obj(i, SUBSTR(p_s, i, 1)));
  END LOOP;
  RETURN;
END split_chars;
```

`yield` becomes `PIPE ROW(...)` plus a constructor call to the
OBJECT type. The terminating `RETURN;` is added automatically.

## Consuming a cursor input

Pipelined functions often *transform* an input row stream. Take a
cursor parameter to consume one:

```pell
pub record Score { id: number, score: number }
pub record Decile { id: number, decile: number }

@pipelined
pub fn deciles(rows: cursor<Score>) -> stream<Decile> {
    for r in rows {
        yield Decile { id: r.id, decile: floor(r.score / 10) };
    }
}
```

Use it:

```sql
SELECT *
  FROM TABLE(text_utils.deciles(CURSOR(
    SELECT id, score FROM raw_scores
  )));
```

## Parallel execution

Add `@parallel(partition = hash(col))` for Oracle to distribute rows
across slaves:

```pell
@pipelined
@parallel(partition = hash(id))
pub fn deciles(rows: cursor<Score>) -> stream<Decile> {
    for r in rows {
        yield Decile { id: r.id, decile: floor(r.score / 10) };
    }
}
```

The `partition = hash(id)` requires a strongly-typed REF CURSOR;
pell auto-emits the type alias in the package spec.

## When NOT to use a pipelined function

- **A regular SQL query would work.** Pipelined functions have
  per-row overhead that pure SQL doesn't.
- **You need to load all rows before processing.** Use `.collect()`
  + iteration.
- **Your transformation has side effects.** Pipelined functions
  should be referentially transparent within a query — Oracle may
  re-execute them.

When pipelined is the right tool:

- Generating row streams from non-relational sources (parsed CLOBs,
  external API results, computed sequences).
- Adding computed columns to a row stream where the computation is
  too complex for SQL expressions.
- Streaming through huge intermediate result sets where
  materialization is too expensive.

## See also

- [Pipelined and parallel functions](../tutorial/08-pipelined-and-parallel.md)
- [Pivot](../tutorial/13-pivot.md) — built-in alternative for
  row-to-column transformations.