---
title: Reference
nav_order: 3
has_children: true
permalink: /reference/
---

# Reference

Exhaustive, alphabetical coverage of every type, annotation, builtin, and
runtime package. Use these for lookups when you already know what you're
searching for.

## Pages

- [Syntax grammar](syntax.md) — EBNF for the whole surface language
- [Types](types.md) — primitives, generics, sealed types, the type lattice
- [Annotations](annotations.md) — `@deterministic`, `@result_cache`,
  `@autonomous`, `@pipelined`, `@parallel`, `@retry`, `@skip`,
  `@propagate`, `@panic`, `@touches`, `@binds`, `@udf`
- [Standard library and method aliases](stdlib.md) — what `log::`, `json::`,
  `pivot::`, and the method-style aliases (`.contains`, `.day()`, etc.) lower to
- [`pell_re` — regex engine](pell-re.md) — the pure-PL/SQL Thompson NFA engine
- [`pell_runtime` — errors and context](runtime.md) — the per-project
  runtime package
