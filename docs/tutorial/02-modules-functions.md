---
title: Modules and functions
parent: Tutorial
nav_order: 2
---

Every pell file declares a module, then a list of items. Functions are
the most common item. This chapter covers the full surface of fn
declarations: parameters, modes, return types, public vs private,
annotations.

## Modules

A pell file starts with `module <name>;`:

```pell
module billing.charges;
```

The dotted name maps directly to a PL/SQL package name. The **first
segment** is treated as the schema; subsequent segments join with `_`
to form the package name:

| pell module name      | PL/SQL                       |
|-----------------------|------------------------------|
| `module hello;`       | `hello`                      |
| `module hr.employees;`| `hr.employees`               |
| `module a.b.c;`       | `a.b_c`                      |

So `module hr.employees;` deploys to `hr.employees` (package
`employees` in schema `hr`), and `module a.b.c;` deploys to `a.b_c`
(package `b_c` in schema `a`).

If you don't want a schema prefix, use a single-segment module name —
the package will install into the user's current schema.

## Functions

The basic shape:

```pell
fn name(param: type, ...) -> return_type {
    // body
}
```

- `fn` (no `pub`) — package-body-private.
- `pub fn` — exposed in the package spec.
- The return type is optional. Omit it for procedures (no return value).

Examples:

```pell
module math;

pub fn double(n: number) -> number {
    return n * 2;
}

// no return type → PROCEDURE
pub fn log_event(event: text) {
    log::info(event);
}

// package-body-private helper
fn _shifted(n: number) -> number {
    return n + 10;
}
```

lowers to:

```sql
CREATE OR REPLACE PACKAGE math AS
  FUNCTION double(p_n IN NUMBER) RETURN NUMBER;
  PROCEDURE log_event(p_event IN VARCHAR2);
END math;
/

CREATE OR REPLACE PACKAGE BODY math AS
  FUNCTION _shifted(p_n IN NUMBER) RETURN NUMBER IS
  BEGIN
    RETURN (p_n + 10);
  END _shifted;

  FUNCTION double(p_n IN NUMBER) RETURN NUMBER IS
  BEGIN
    RETURN (p_n * 2);
  END double;
  ...
END math;
/
```

Things to notice:

- `pub fn` items appear in the package spec; private fns are only in
  the body.
- Parameters get a `p_` prefix.
- Local variables (declared with `let`, covered in
  [Records and types](./03-records-types.md)) get an `l_` prefix.

## Parameter modes — `in`, `out`, `inout`

The default mode is `in` (read-only inside the function). To return a
value via a parameter, mark it `out`. To read and write, mark it
`inout`:

```pell
pub fn split_full_name(full: text, out first: text, out last: text) {
    // populate `first` and `last`
}

pub fn increment(inout counter: number) {
    counter = counter + 1;
}
```

lowers to:

```sql
PROCEDURE split_full_name(
  p_full IN VARCHAR2,
  p_first OUT VARCHAR2,
  p_last OUT VARCHAR2
) IS
BEGIN
  ...
END split_full_name;

PROCEDURE increment(p_counter IN OUT NUMBER) IS
BEGIN
  p_counter := p_counter + 1;
END increment;
```

Pell uses keyword modifiers (not sigils) so the modes read fluently:
`out first: text` reads as "out param `first`, type `text`."

## Return types and `Result<T, E>`

A fn with a return type returns a value:

```pell
pub fn add(a: number, b: number) -> number {
    return a + b;
}
```

For fallible operations, return a `Result<T, E>`:

```pell
pub error NotFound { id: number }

pub fn lookup(id: number) -> Result<text, NotFound> {
    let row: text = sql! {
        select name from people where id = :id
    }.one();
    return Ok(row);
}
```

`Result<T, E>` lowers to just `T` at the type level — pell uses
exceptions (via the `pell_runtime` package) to propagate errors. See
[Errors and @retry](./06-errors-and-retry.md) for the full story.

## Calling functions

Within the same module, just call by name:

```pell
pub fn quadruple(n: number) -> number {
    return double(double(n));
}
```

Across modules, use `module_name::fn` notation:

```pell
import math;

pub fn ten_times(n: number) -> number {
    return math::double(n * 5);
}
```

`math::double(...)` lowers to `math.double(...)` in PL/SQL.

## Annotations

Pell uses `@name` annotations to attach behavior to functions. The most
common ones:

```pell
@deterministic
pub fn double(n: number) -> number { return n * 2; }

@autonomous
pub fn write_audit_log(event: text) {
    sql! { insert into audit_log(event, ts) values (:event, sysdate) };
}

@retry(n=3, backoff=100)
pub fn poll_external_system() -> text {
    ...
}
```

| Annotation         | What it does                                              |
|--------------------|-----------------------------------------------------------|
| `@deterministic`   | Marks the function pure — Oracle can cache results.      |
| `@autonomous`      | Function runs in its own transaction.                    |
| `@retry(n=, ...)`  | Wrap the body in a retry loop on transient failures.    |
| `@udf`             | UDF-eligible (no DML, no autonomous, etc.).             |
| `@pipelined`       | Streaming function (table function in SQL).             |
| `@parallel(...)`   | Pipelined function eligible for parallel execution.      |
| `@touches(...)`    | (unsafe only) declare dynamic-SQL deps to ALL_DEPENDENCIES. |
| `@binds(...)`      | (unsafe only) declare bind parameters used in exec_dyn.  |

Full reference: [Annotations](../reference/annotations.md).

## String interpolation

Pell strings support `{name}` placeholders that interpolate any pell
expression:

```pell
pub fn greet(name: text, count: number) -> text {
    return "hello {name}, you have {count} messages";
}
```

lowers to:

```sql
FUNCTION greet(p_name IN VARCHAR2, p_count IN NUMBER) RETURN VARCHAR2 IS
BEGIN
  RETURN ('hello ' || p_name || ', you have ' || p_count || ' messages');
END greet;
```

Inside `{ ... }`, you can put any pell expression: identifiers, member
access (`{u.name}`), method calls (`{bulk.rowcount(i)}`), arithmetic
(`{a + b}`).

To write a literal `{` or `}`, double it: `"uses {{x, y}} syntax"` →
`'uses {x, y} syntax'`.

For content that should be preserved verbatim (regex patterns, file
paths), use **backtick raw strings**:

```pell
let pattern: text = `(\d{3}) - (\d{4})`;
```

Backticks skip both escape processing and interpolation. See
[Regex](./11-regex.md) for the recommended `/pattern/` literal syntax.

## Where to go next

- [Records and types](./03-records-types.md) — types beyond
  text/number, including records and lists.
- [SQL blocks](./04-sql-blocks.md) — talking to the database with
  `sql!{}`.
- [Errors and @retry](./06-errors-and-retry.md) — the full Result/error
  story.