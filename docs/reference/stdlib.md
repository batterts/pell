---
title: Standard library
parent: Reference
nav_order: 4
---

Pell exposes Oracle's PL/SQL builtins through two channels:

1. **Pass-through builtins** — call by name like any free function:
   `length(s)`, `substr(s, 1, 3)`, `sysdate()`, etc. The compiler
   doesn't `l_`-prefix unknown identifiers, so they go through as-is.
2. **Method aliases** — friendly dot-call versions of common
   operations: `s.contains("foo")`, `d.year()`, `xs.split(",")`. The
   compiler rewrites these to the appropriate Oracle builtin.

This page lists every method alias and the most commonly-used
builtins.

## Method aliases — strings

| Pell                          | PL/SQL lowering                      | Returns       |
|-------------------------------|---------------------------------------|---------------|
| `s.contains(t)`               | `INSTR(s, t) > 0`                     | `bool`        |
| `s.starts_with(t)`            | `s LIKE t \|\| '%'`                   | `bool`        |
| `s.ends_with(t)`              | `s LIKE '%' \|\| t`                   | `bool`        |
| `s.is_empty()`                | `s IS NULL OR LENGTH(s) = 0`          | `bool`        |
| `s.split(delim)`              | calls generated `pell_split_text`     | `list<text>`  |

`s.split(delim)` requires a per-package generated helper that uses a
recursive CONNECT BY split. For richer splitting, use `re::split(s,
/\s+/)`.

## Method aliases — dates / timestamps

| Pell                | PL/SQL lowering                       | Returns   |
|---------------------|----------------------------------------|-----------|
| `d.year()`          | `EXTRACT(YEAR FROM d)`                 | `number`  |
| `d.month()`         | `EXTRACT(MONTH FROM d)`                | `number`  |
| `d.day()`           | `EXTRACT(DAY FROM d)`                  | `number`  |
| `d.hour()`          | `EXTRACT(HOUR FROM d)`                 | `number`  |
| `d.minute()`        | `EXTRACT(MINUTE FROM d)`               | `number`  |
| `d.second()`        | `EXTRACT(SECOND FROM d)`               | `number`  |

## Common pass-through builtins

These work because pell doesn't prefix unknown identifiers — calls
go through unchanged.

### Strings

| Pell                            | Notes                                             |
|---------------------------------|---------------------------------------------------|
| `length(s)`                     | character count                                   |
| `substr(s, pos, len?)`          | 1-based; `len` optional                           |
| `instr(haystack, needle)`       | 1-based position, 0 if not found                  |
| `upper(s)` / `lower(s)`         |                                                   |
| `trim(s)` / `ltrim(s)` / `rtrim(s)` |                                              |
| `replace(s, from, to)`          |                                                   |
| `lpad(s, n, pad?)` / `rpad(...)`|                                                   |
| `concat(a, b)`                  | (or just use string interpolation `"a {b}"`)      |
| `chr(n)` / `ascii(s)`           |                                                   |
| `to_char(x)` / `to_char(d, fmt)`|                                                   |
| `to_number(s)`                  |                                                   |

### Dates

| Pell                              | Notes                                  |
|-----------------------------------|----------------------------------------|
| `sysdate()`                       | current date                           |
| `systimestamp()`                  | current timestamp                      |
| `current_date()` / `current_timestamp()` | session-local equivalents       |
| `add_months(d, n)`                |                                        |
| `months_between(a, b)`            |                                        |
| `trunc(d)` / `trunc(d, fmt)`      | start of day / month / year            |
| `to_date(s, fmt)`                 |                                        |
| `to_timestamp(s, fmt)`            |                                        |

### Numbers

| Pell                       |
|----------------------------|
| `abs(n)` / `sign(n)`       |
| `round(n)` / `round(n, d)` |
| `trunc(n)` / `trunc(n, d)` |
| `mod(a, b)`                |
| `power(b, e)`              |
| `sqrt(n)`                  |
| `ln(n)` / `log(b, n)` / `exp(n)` |
| `floor(n)` / `ceil(n)`     |
| `greatest(a, b, ...)` / `least(...)`|

### Conditional / NVL family

| Pell                          | Notes                                       |
|-------------------------------|----------------------------------------------|
| `nvl(x, default)`             | x if not NULL, else default                  |
| `coalesce(a, b, c, ...)`      | first non-NULL                               |
| `nullif(a, b)`                | NULL if a = b, else a                        |

**Watch out**: `decode(...)` and `nvl2(...)` are SQL-only — from PL/SQL
you'd wrap in `select ... from dual` or reach for `nvl` / `coalesce` /
a pell `if`. See [PL/SQL-disallowed functions](#pl-sql-disallowed-functions)
below.

`CASE` works in both SQL and PL/SQL. PL/SQL has three forms:

- **CASE expression** — `CASE x WHEN 1 THEN 'a' WHEN 2 THEN 'b' ELSE 'c' END`
  is a value expression usable anywhere a value is expected, in both
  SQL and PL/SQL.
- **CASE statement** — `CASE x WHEN 1 THEN stmts; WHEN 2 THEN stmts; ELSE stmts; END CASE;`
  is the PL/SQL flow-control form.
- **Searched CASE statement** — `CASE WHEN cond1 THEN stmts; WHEN cond2 THEN stmts; END CASE;`
  same as above but each branch has its own boolean condition.

Pell doesn't surface CASE directly — use `match` (for sealed types
and enum-style switching) or chained `if`/`else if` (for general
flow). When you want the *expression* form inside a `sql!{}` block,
just write it: `select case status when 'A' then 1 else 0 end from t`.

### Logging

| Pell                   | Notes                                                       |
|------------------------|--------------------------------------------------------------|
| `log::info(msg)`       | App-defined; you ship a `log` package with `info(msg)` etc. |
| `dbms_output::put_line(s)` | Oracle's stdout-style channel                          |

`log::*` isn't built into pell — you provide it. The convention is a
`log` package with `info`, `warn`, `error` procedures that route to
your logging table or `DBMS_OUTPUT`.

## SQL-only — wrap in `select ... from dual`

These work inside `sql!{}` blocks but NOT directly in PL/SQL context.
If you call them at the pell expression level, the emitter will pass
them through and you'll get a PL/SQL compile error like PLS-00204 or
PLS-00201.

Wrap them in a sql!{} block to get the SQL-context evaluation:

```pell
let n: number = sql! { select ora_hash(:s) from dual }.one();
let s: text   = sql! { select decode(:k, 1, 'a', 2, 'b', 'other') from dual }.one();
```

| Function          | Reason it's SQL-only                                        |
|-------------------|-------------------------------------------------------------|
| `decode(...)`     | Pure-SQL construct                                          |
| `nvl2(a, b, c)`   | SQL-only variant of NVL                                     |
| `json_table(...)` | SQL row source                                              |
| `ora_hash(s)`     | SQL function only; use `dbms_utility.get_hash_value` in PL/SQL |
| `vsize(x)`        | SQL function                                                |
| `dump(x)`         | SQL function                                                |
| `regexp_*`        | These actually work in PL/SQL too; just noted for completeness |
| Aggregates (`sum`, `count`, `max`, ...) | only valid in a SELECT context     |
| Analytic (`row_number()`, `lead()`, ...) | only valid in a SELECT context    |

`pell_re` (covered in [pell-re.md](./pell-re.md)) is PL/SQL-callable
and doesn't have this restriction.

## What pell adds on top of PL/SQL builtins

- **String interpolation** `"hello {name}"` — way better than `||`
  chains.
- **`re::` namespace** — see [pell_re reference](./pell-re.md).
- **`jq!{}` macro** — JSON_TABLE-backed iteration; see the [tutorial](../tutorial/12-jq.md).
- **`pivot::sum` / `pivot::sum_dyn`** — typed and dynamic PIVOT.
- **`json::*` family** — typed JSON construction and extraction.
- **`exec_dyn(...)`** — gated dynamic SQL with `unsafe fn`.

## Calling Oracle packages

Any installed PL/SQL package is callable as `pkg_name::fn(...)`. Pell
lowers `::` to `.` and otherwise leaves the call untouched:

```pell
dbms_lock::sleep(0.1);
utl_url::escape(url);
dbms_random::value(0, 1);
```

This makes the entire Oracle PL/SQL surface accessible by name.