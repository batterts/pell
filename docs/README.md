# pell documentation

pell is a small statically-typed language whose only backend target is
Oracle PL/SQL (19c and 23ai). It exists to fix the readability and
ergonomics pain points of PL/SQL without losing access to a real Oracle
database.

These docs are split into three sections:

- **[Tutorial](./tutorial/)** — a narrative walk-through, from "hello
  world" to the advanced features. Read these in order if you're
  learning the language.
- **[Reference](./reference/)** — exhaustive, alphabetical coverage of
  every type, annotation, builtin, and runtime package. Use these for
  lookups when you already know what you're searching for.
- **[Cookbook](./cookbook/)** — task-oriented recipes for common
  problems ("validate an email address", "bulk-insert with FORALL").
  Each recipe is self-contained and shows the full pell source + the
  PL/SQL it lowers to.

## How to read these docs

Every code example shows pell source on top and the emitted PL/SQL
below, like this:

```pell
module greet;

pub fn hello(name: text) -> text {
    return "hello, {name}";
}
```

lowers to:

```sql
CREATE OR REPLACE PACKAGE greet AS
  FUNCTION hello(p_name IN VARCHAR2) RETURN VARCHAR2;
END greet;
/

CREATE OR REPLACE PACKAGE BODY greet AS
  FUNCTION hello(p_name IN VARCHAR2) RETURN VARCHAR2 IS
  BEGIN
    RETURN ('hello, ' || p_name);
  END hello;
END greet;
/
```

You can paste either form into your own editor. The PL/SQL is
deployable as-is.

## Tutorial chapter list

1. [Getting started](./tutorial/01-getting-started.md)
2. [Modules and functions](./tutorial/02-modules-functions.md)
3. [Records and types](./tutorial/03-records-types.md)
4. [SQL blocks](./tutorial/04-sql-blocks.md)
5. [Iteration: lists, for loops, FORALL](./tutorial/05-iteration.md)
6. [Errors and @retry](./tutorial/06-errors-and-retry.md)
7. [Types, sealed types, aggregates](./tutorial/07-types-and-aggregates.md)
8. [Pipelined and parallel functions](./tutorial/08-pipelined-and-parallel.md)
9. [Dynamic SQL: unsafe, exec_dyn, @touches](./tutorial/09-dynamic-sql.md)
10. [JSON](./tutorial/10-json.md)
11. [Regex with re:: and /pattern/](./tutorial/11-regex.md)
12. [jq!{} for JSON queries](./tutorial/12-jq.md)
13. [Pivot — typed and dynamic](./tutorial/13-pivot.md)
14. [pell exec and pell repl](./tutorial/14-exec-and-repl.md)
15. [The compilation model](./tutorial/15-compilation-model.md)

## Reference pages

- [Syntax grammar](./reference/syntax.md)
- [Types](./reference/types.md)
- [Annotations](./reference/annotations.md)
- [Standard library and method aliases](./reference/stdlib.md)
- [pell_re — regex engine](./reference/pell-re.md)
- [pell_runtime — errors and context](./reference/runtime.md)

## Cookbook recipes

- [Validate an email address](./cookbook/validate-email-address.md)
- [Parse a phone number into a typed record](./cookbook/parse-phone-number.md)
- [Bulk-insert with FORALL](./cookbook/bulk-insert-with-forall.md)
- [Retry on deadlock](./cookbook/retry-on-deadlock.md)
- [Stream rows with a pipelined function](./cookbook/stream-rows-with-pipelined.md)
- [Upsert with MERGE](./cookbook/upsert-with-merge.md)
- [Extract typed fields from a JSON document](./cookbook/extract-fields-from-json.md)
