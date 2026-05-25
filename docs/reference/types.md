---
title: Types
parent: Reference
nav_order: 2
---

## Primitives

### `number`

Floating-point numeric.

- PL/SQL: `NUMBER`
- Literal: `42`, `3.14`, `-1`
- Operators: `+ - * / % == != < <= > >=`

### `int`

Integer-only numeric. Lowers to `PLS_INTEGER`, which is roughly 10×
faster than `NUMBER` for integer arithmetic but bounded to ±2³¹.

- PL/SQL: `PLS_INTEGER`
- Literal: shared with `number`; coerce with explicit type annotation
  `let i: int = 42;`

### `text`

The everyday string type.

- PL/SQL: `VARCHAR2(4000)`
- Literal: `"hello"`, with `{name}` interpolation
- Operators: `==`, `!=`; concatenation via interpolation `"a {b}"`
- Empty string `""` is NULL in Oracle — be careful with `== ""`

### `bigtext`

`text` with CLOB storage. Use for strings that may exceed 32K.

- PL/SQL: `CLOB`
- Same operators as `text`; the driver auto-promotes Python str binds
  over 4000 bytes to CLOB
- See [bigtext vs text](../tutorial/03-records-types.md#bigtext-vs-text)

### `bool`

Boolean true/false.

- PL/SQL: `BOOLEAN`
- On 19c, lowers to `NUMBER(1)` inside OBJECT TYPE / table columns
  because PL/SQL BOOLEAN isn't a SQL-visible type until 23ai
- Literals: `true`, `false`
- Operators: `&& || !`

### `date`

Calendar date with time-of-day.

- PL/SQL: `DATE`
- Method aliases: `.year()`, `.month()`, `.day()`, `.hour()`,
  `.minute()`, `.second()` — each lowers to `EXTRACT(... FROM ...)`
- Common builtin: `sysdate()` for the current date

### `timestamp`

Higher-resolution timestamp (fractional seconds).

- PL/SQL: `TIMESTAMP`
- Same method aliases as `date`
- Builtin: `systimestamp()` for current timestamp

### `interval`

Time interval.

- PL/SQL: `INTERVAL DAY TO SECOND`
- Arithmetic: `date + interval`, `date - date` produces interval

### `bytes`

Raw bytes.

- PL/SQL: `RAW(2000)`
- Literal: `bytes::hex("DEADBEEF")` or `bytes::base64("...")`
- Operators: limited; use `UTL_RAW` builtins for manipulation

### `json`

Native JSON.

- PL/SQL: `JSON` on 23ai, `VARCHAR2(32767)` on 19c
- Construction: `json::object(...)`, `json::array(...)`, `json::parse(...)`
- Access: `json::get_text`, `json::get_number`, `json::has`
- Conversion: `json::from(record_var)`, `json::into::<R>(j)`
- Builder: `.set(path, v)`, `.remove(path)`, `.append(path, v)`
- See [JSON tutorial](../tutorial/10-json.md)

### `Unit`

The "no return value" type. Lowers to PL/SQL `PROCEDURE` for fns
returning `Unit`. The literal is `()`.

### `Never`

A type-level signal that a fn does not return. Reserved for future
use; currently lowers like `Unit`.

## Generics

### `Option<T>`

T or absent. Lowers to `T` directly, with NULL meaning None.

- Construction: `Some(value)`, `None`
- Inspection: `if let Some(v) = opt { ... }` (in match arms)

### `Result<T, E>`

Value-or-error. Lowers to `T` at the type level; errors propagate
via PL/SQL exceptions through the `pell_runtime` package.

- Construction: `Ok(value)`, `Err(error_record)`
- Unwrap: `let v = fallible()?;`

### `list<T>`

Associative array of `T` indexed by 1-based `PLS_INTEGER`.

- PL/SQL: `TYPE t_<T>_list IS TABLE OF <T> INDEX BY PLS_INTEGER`
- Construction: `[v1, v2, v3]` literal, or from `sql!{...}.collect()`
- Iteration: `for x in xs { ... }`, `for i in xs.indices() { ... }`
- Index access: `xs(1)`, `xs(i)`
- Methods: `.first()`, `.last()`, `.count()`

### `map<K, V>`

Associative array of `V` keyed by `K`.

- PL/SQL: `TYPE t_<K>_to_<V> IS TABLE OF <V> INDEX BY <K>`
- Access: `m(k)`
- `m.EXISTS(k)` to test for presence (use the PL/SQL idiom)

### `set<T>`

Associative array of `T` with sentinel values.

- Less common; usually `map<T, bool>` is clearer

### `cursor<T>`

Strongly-typed REF CURSOR.

- PL/SQL: `TYPE t_<T>_cur IS REF CURSOR RETURN <T>`
- Used as a parameter type for pipelined functions:
  `fn fancy(rows: cursor<Row>) -> stream<Out>`
- Outside `@pipelined`, `cursor<T>` lowers to weakly-typed
  `SYS_REFCURSOR`

### `stream<T>`

Row stream produced by a pipelined function. Required as the return
type of `@pipelined` fns.

- PL/SQL: `TYPE <pkg>_<T>_nt IS TABLE OF <pkg>_<T>_obj`
- One OBJECT TYPE + one TABLE TYPE per record name, schema-level

### `rowtype<table_or_view>`

Mirrors a table or view's column shape via PL/SQL `%ROWTYPE`.

- PL/SQL: `table_or_view%ROWTYPE`
- Useful for "I want all the columns, don't make me redeclare them"

## Record types

```pell
pub record Employee {
    id:    number,
    name:  text,
    level: number,
}
```

- PL/SQL: `TYPE t_employee IS RECORD (id NUMBER, name VARCHAR2(4000),
  level NUMBER)`
- Struct-lit construction: `Employee { id: 1, name: "Alice", level: 5 }`
- Field-by-field: `let e: Employee; e.id = 1; ...`
- Access: `e.name`

`pub record` exposes the type in the package spec; non-pub keeps it
private to the body.

## Error types

```pell
pub error NotFound { id: number }

@skip
pub error AlreadyExists { id: number }

@panic
pub error InvariantViolation { msg: text }
```

- Each error gets an EXCEPTION_INIT mapping in `pell_runtime`
- Category controls `@retry` behavior; see [Errors](../tutorial/06-errors-and-retry.md)

## OBJECT types

`pub type` (with methods) and `pub sealed type` (closed hierarchies)
lower to schema-level OBJECT TYPE — see [Types and aggregates](../tutorial/07-types-and-aggregates.md).

## Enum types

```pell
pub enum Status {
    active     = "active",
    leave      = "leave",
    terminated = "terminated",
}
```

Compile-time inlined as text constants. Used by `pivot::sum` for
typed pivots. The PL/SQL has no enum type — the literal string is
inlined wherever an enum variant is referenced.

## Type aliases

There is no `type alias = T` syntax. Use a single-field record:

```pell
pub record EmployeeId { v: number }
```

This makes `EmployeeId` and `DepartmentId` distinct nominal types
even though both wrap `number`.