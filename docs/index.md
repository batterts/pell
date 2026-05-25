---
title: Home
layout: home
nav_order: 1
description: "pell — a statically-typed language that compiles to Oracle PL/SQL 19c and 23ai."
permalink: /
---

# pell

A statically-typed surface language whose only backend target is Oracle
PL/SQL. It exists to fix the worst readability and ergonomics pain
points of PL/SQL without losing access to a real Oracle database.

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

The emitted PL/SQL is deployable as-is. You can paste either form into
your editor.

## Where to start

- **[Tutorial]({{ '/tutorial/' | relative_url }})** — a narrative walk-through
  from "hello world" to the advanced features. Read in order if you're
  learning the language.
- **[Reference]({{ '/reference/' | relative_url }})** — exhaustive
  coverage of every type, annotation, builtin, and runtime package. Use
  for lookups when you already know what you're searching for.
- **[Cookbook]({{ '/cookbook/' | relative_url }})** — task-oriented
  recipes for common problems. Each recipe is self-contained and shows
  the full pell source plus the PL/SQL it lowers to.

## Project meta

- **[Benchmarks]({{ '/benchmarks/' | relative_url }})** — compile-time
  and emit-quality measurements vs. hand-written PL/SQL.
- **[Reviews]({{ '/reviews/' | relative_url }})** — five-reviewer
  critique reports plus a SUMMARY of the design review.
- **[Source on GitHub](https://github.com/batterts/pell)** — the compiler,
  examples, and full spec.
