# 3. Records and types

This chapter covers pell's type system: primitive types, records,
optional values, lists, and the type-spelling conventions used by the
emitter.

## Primitive types

| pell           | PL/SQL (default)        | Notes                                  |
|----------------|-------------------------|----------------------------------------|
| `number`       | `NUMBER`                |                                        |
| `int`          | `PLS_INTEGER`           | integer arithmetic, ~10× faster        |
| `text`         | `VARCHAR2(4000)`        | the everyday string type               |
| `bigtext`      | `CLOB`                  | for strings that may exceed 32K        |
| `bool`         | `BOOLEAN`               | NUMBER(1) inside OBJECT TYPE on 19c    |
| `date`         | `DATE`                  |                                        |
| `timestamp`    | `TIMESTAMP`             |                                        |
| `interval`     | `INTERVAL DAY TO SECOND`|                                        |
| `bytes`        | `RAW(2000)`             |                                        |
| `json`         | `JSON` (CLOB on 19c)    | native JSON datatype                   |
| `Unit`         | (none)                  | "no return value" — used for procs     |

Literal syntax:

```pell
let n: number = 42;
let pi: number = 3.14;
let name: text = "Alice";
let raw_pattern: text = `(\d{3}) - (\d{4})`;
let on: bool = true;
let now: date = sysdate();
```

For very large literals or contents that aren't easily expressed inline,
read them from a table or pass them as a parameter.

### `bigtext` vs `text`

`text` lowers to `VARCHAR2(4000)`. That's plenty for most identifiers,
labels, short messages, and codes. Once your strings might exceed 32K
(file contents, large XML/JSON payloads, log lines), use `bigtext`:

```pell
pub fn store(payload: bigtext) -> number {
    return length(payload);
}
```

`bigtext` is `text` semantically — same operators, same builtins —
but it lowers to `CLOB`, which Oracle handles via LOB locators
internally. The `pell exec` and `pell repl` driver auto-promotes
Python `str` binds > 4000 bytes to CLOB binds, so you can pass big
payloads from the client without writing LOB code yourself.

## Optional values — `Option<T>`

`Option<T>` represents "T or absent." It lowers to `T` directly (no
wrapper), with `NULL` representing `None`:

```pell
pub fn find_email(user_id: number) -> Option<text> {
    let row: Option<text> = sql! {
        select email from users where id = :user_id
    }.one_or_none();
    return row;
}
```

If the SELECT returns no rows, `.one_or_none()` returns `None`; the
caller can check with `if let Some(e) = row { ... }` (covered in the
chapter on match/if-let patterns).

## Records

Records are PL/SQL `RECORD` types with named fields:

```pell
pub record Employee {
    id:    number,
    name:  text,
    level: number,
}
```

lowers to:

```sql
CREATE OR REPLACE PACKAGE BODY <pkg> AS
  TYPE t_employee IS RECORD (
    id NUMBER,
    name VARCHAR2(4000),
    level NUMBER
  );
  ...
```

`pub record` exposes the type in the package spec; without `pub`, it's
private to the package body.

### Constructing records

Two ways:

```pell
// Struct-literal syntax — most readable
let e: Employee = Employee { id: 1, name: "Alice", level: 5 };

// Field-by-field — useful when constructing in steps
let e: Employee;
e.id = 1;
e.name = "Alice";
e.level = 5;
```

The first form is preferred. Order doesn't matter; fields by name.

### Accessing fields

Dot access:

```pell
log::info("{e.name} is level {e.level}");
```

## Records and SQL

Records are the natural target for `sql!{...}.collect()` and
`sql!{...}.one()` when the query returns multiple columns. Pell
matches the SELECT list to record fields by position:

```pell
pub record EmpRow { id: number, name: text }

pub fn list_active() -> list<EmpRow> {
    let xs: list<EmpRow> = sql! {
        select id, name from employees where active = 1
    }.collect();
    return xs;
}
```

The column count in the SELECT must equal the field count in the
record. Column names don't have to match — only the positional order
matters.

## Lists — `list<T>`

`list<T>` lowers to an associative array — `TABLE OF T INDEX BY
PLS_INTEGER`:

```pell
let xs: list<number> = [1, 2, 3, 4, 5];
let names: list<text> = ["Alice", "Bob"];
let rows: list<EmpRow> = sql! { select id, name from employees }.collect();
```

The list literal `[1, 2, 3]` expands to per-index assignments at
compile time:

```sql
l_xs(1) := 1;
l_xs(2) := 2;
l_xs(3) := 3;
l_xs(4) := 4;
l_xs(5) := 5;
```

The corresponding type declaration is generated once per element type
at the package level:

```sql
TYPE t_number_list IS TABLE OF NUMBER INDEX BY PLS_INTEGER;
```

Iteration is covered in [Iteration](./05-iteration.md).

## Type aliases via records

There's no `type alias = T` syntax. If you want a named alias for a
type, declare a single-field record:

```pell
pub record EmployeeId { v: number }
```

This is the idiomatic way to make IDs of different entities
type-incompatible.

## Generic types in the surface

A handful of types take type parameters:

- `Option<T>` — value-or-NULL
- `Result<T, E>` — value-or-error (see [Errors](./06-errors-and-retry.md))
- `list<T>` — associative array, 1-based
- `map<K, V>` — associative array keyed by `K`
- `set<T>` — associative array of `T` with sentinel values
- `cursor<T>` — REF CURSOR, T is the row type
- `stream<T>` — pipelined function row stream
- `rowtype<table_or_view>` — Oracle `%ROWTYPE`

Use `rowtype` to match a table's column shape without redeclaring it:

```pell
pub fn first_employee() -> rowtype<employees> {
    return sql! { select * from employees fetch first 1 rows only }.one();
}
```

## What about `null` / `nil`?

Pell uses `Option<T>` for "may be absent." Bare `NULL` (Oracle's NULL)
appears in SQL contexts via `Option<T>` returning None. There's no
standalone `nil` keyword in the surface — that's a deliberate choice
to avoid making every variable nullable by default.

A practical gotcha: in Oracle, the empty string `''` **is** NULL. So:

```pell
let s: text = "";
if s == "" {  // FALSE — '' is NULL
    log::info("empty");
}
```

That comparison never matches because both sides are NULL, and `NULL =
NULL` is unknown (not true). When you need to track "was this set?",
use an explicit flag:

```pell
let seen: number = 0;
let value: text;
if some_condition {
    value = "got it";
    seen = 1;
}
if seen = 1 {
    log::info("value is {value}");
}
```

## Where to go next

- [SQL blocks](./04-sql-blocks.md) — `sql!{}` queries that return
  records, lists of records, and scalars.
- [Iteration](./05-iteration.md) — `for n in xs`, `forall` for bulk
  DML.
- [Types, sealed types, aggregates](./07-types-and-aggregates.md) —
  pell's OO surface and user-defined aggregates.
