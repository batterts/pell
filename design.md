# A Modern Language That Compiles to PL/SQL 23

> Working name: **`pell`** (placeholder — see "Naming" at the end).
> Status: draft 0.1, 2026-05-13. Everything here is up for revision.

## 1. Goals

Build a small, statically-typed surface language whose **only** backend target is
Oracle PL/SQL 23, with first-class tooling.

The three things that must be better than PL/SQL:

1. **Readability** — less ceremony (`DECLARE` / `BEGIN` / `END;` / package
   spec + body duplication / `IS` vs `AS`), modern keywords, expression-oriented
   where it doesn't fight SQL.
2. **Exception handling** — typed errors with structured payloads, `Result<T,E>`
   and `T?` for nullables, a `?` propagation operator, and *no implicit*
   `WHEN OTHERS THEN NULL` ever.
3. **Tooling** — formatter, LSP, test runner, and package manager from day one.
   The compiler must be usable without an Oracle install (it emits text);
   verifying generated PL/SQL against a real DB is a separate, optional step.

Non-goal of this goal list: "more concise." Brevity that hurts grep-ability
or stack-trace clarity is a regression even if it shortens the source.

## 2. Non-goals

- **Not** a SQL replacement. Embedded SQL stays SQL; we don't reinvent `SELECT`.
- **Not** a polyglot backend. No JS/Postgres/SQLite targets in v1. Trying to
  abstract over dialects is what kills these projects.
- **Not** a runtime. We emit PL/SQL text; we don't ship a VM, a stdlib loaded
  into the DB, or runtime helpers (unless one specific helper package becomes
  unavoidable — see §7).
- **Not** a full IDE. LSP + tree-sitter grammar; editors plug in.

## 3. Design choices at a glance

| Concern | PL/SQL today | `pell` |
|---|---|---|
| Block delimiters | `DECLARE … BEGIN … END;` | `{ }` |
| Procedure vs. function | Separate keywords | `fn`; returning `Unit` = procedure |
| Package spec + body | Two files, duplicated signatures | One `module`; compiler emits both |
| Nullability | Everything nullable, silent | `T?` opt-in, checked |
| Errors | `EXCEPTION` + `SQLCODE`/`SQLERRM` strings | Typed `error` decls + `Result<T,E>` |
| Log-and-rethrow | `WHEN OTHERS THEN log; RAISE;` | `finally { log; }` |
| Records | `TYPE ... IS RECORD (…)` | `record User { id: number, … }` |
| Collections | Nested tables, varrays, assoc. arrays | One `list<T>`, one `map<K,V>` (lower to assoc. arrays) |
| SQL embedding | Implicit | `sql!{ … }` block, params explicit |
| Cursors | `OPEN/FETCH/CLOSE` or `FOR…IN` | `for row in sql!{…} { }`, iterators |
| Transactions | Ambient + explicit `COMMIT` / `ROLLBACK` | `transaction { … }` block, auto-commit on exit, rollback on error, nested = savepoint (§4.8) |
| `RETURNING INTO` / `SQL%ROWCOUNT` | Implicit cursor attrs | `DmlResult` from a write `sql!{}` with `.returning::<T>()` and `.rowcount()` (§4.5.3) |
| `SELECT … FOR UPDATE` | In-SQL keyword | `.for_update()` modifier on the read iterator, requires `transaction { … }` (§4.5.4) |
| Boolean in SQL | Available in 23 — use it | Used natively |
| Compiler hints | `PRAGMA AUTONOMOUS_TRANSACTION;` / `PRAGMA INLINE(…)` / `PRAGMA UDF;` / `DETERMINISTIC` / `RESULT_CACHE` clauses, each with its own placement rules | Uniform `@name(args)` annotations, closed set, validated combinations (§9) |

## 4. Surface syntax — by example

### 4.1 Hello, module

```pell
module hr.employees;

import std::log;

fn greet(name: text) {
  log::info("hello, {name}");
}
```

Lowers to (sketch):

```plsql
create or replace package hr_employees as
  procedure greet(p_name in varchar2);
end hr_employees;
/
create or replace package body hr_employees as
  procedure greet(p_name in varchar2) is
  begin
    std_log.info('hello, ' || p_name);
  end greet;
end hr_employees;
/
```

Note the compiler handles the spec/body split, name-mangling, and string
interpolation lowering. Source maps are emitted so DB error stacks point back
to `.pell` lines (§8).

### 4.2 Records and nullability

```pell
record Employee {
  id:        number,
  name:      text,
  email:     text?,        // nullable
  hired_on:  date,
  manager:   Employee?,    // nullable self-reference
}
```

Nullable types use `?`. Non-`?` values are guaranteed non-null at the type
level; the compiler inserts `NOT NULL` constraints on lowered record fields and
rejects assigning a `T?` into a `T` slot without an unwrap.

### 4.3 Typed errors and `Result`

```pell
error NotFound        { entity: text, id: number }
error DuplicateEmail  { email: text }
error PolicyViolation { reason: text }

fn find_employee(id: number) -> Result<Employee, NotFound> {
  let row = sql! {
    select id, name, email, hired_on, manager_id
    from employees where id = :id
  }.first()?;                                      // Option<Row> -> Row or NotFound

  return Ok(row.into::<Employee>());
}

fn promote(id: number) -> Result<Unit, NotFound | PolicyViolation> {
  let e = find_employee(id)?;                      // bubbles NotFound
  if e.hired_on > today() - months(6) {
    return Err(PolicyViolation { reason: "tenure < 6mo" });
  }
  sql! { update employees set level = level + 1 where id = :id };
  return Ok(());
}
```

Key points:

- `Result<T, E>` and `Option<T>` are part of the prelude. `?` propagates on
  either: on `Result<T, E>`, the `E` must be assignable into the enclosing
  fn's declared error union. On `Option<T>`, the `?` desugars to
  `match { Some(v) -> v, None -> return Err(<from_none>) }` and requires the
  enclosing fn's error union to contain a variant marked
  `@from_none_of(<call site type>)`. In the prelude, `NotFound` is so marked
  for `Option<Row>` produced by `.first()`. (This avoids the surprise of `?`
  on a plain `Option<T>` silently converting to whatever single variant
  happens to be in scope.) In the example above, `find_employee`'s declared
  `NotFound` is what `?` propagates to — and only because `NotFound` is the
  prelude variant for the `Option<Row>` → `Result<Row, _>` conversion.
- Errors form a closed sum at each function boundary (`E1 | E2`); callers must
  handle or re-declare them. There is no implicit `WHEN OTHERS`.
- When emitted to PL/SQL, each `error` becomes a numbered `EXCEPTION` plus a
  shadow record type carrying the payload; we marshal via package-level globals
  scoped per-call (the alternative — `RAISE_APPLICATION_ERROR` with JSON
  payloads — is on the table; see §11).

### 4.4 `try` / `catch` with exhaustive patterns

```pell
match promote(emp_id) {
  Ok(())                         -> log::info("promoted"),
  Err(NotFound { id, .. })       -> log::warn("no such emp: {id}"),
  Err(PolicyViolation { reason }) -> log::warn("blocked: {reason}"),
}
```

Non-exhaustive matches are a compile error. There is no `_ -> ...` catch-all
unless explicitly written; if written, the compiler warns if all variants are
already covered.

**Recompilation hazard.** Exhaustiveness is checked against the *callee's
currently declared* error union. If a callee adds a new variant to its
signature (`Result<T, A | B>` → `Result<T, A | B | C>`), every caller's
exhaustive `match` becomes non-exhaustive on the next compile — which is a
hard error, not a silent fall-through. This is by design: adding an error
variant is a breaking change to the function's signature, and we want the
type system to surface it. The mitigation for callers who really want to
absorb future variants is to write an explicit `Err(_) -> …` arm and accept
the "all variants already covered" warning that arm currently produces;
that warning is downgraded to silence as soon as a new variant exists.

### 4.5 SQL embedding

`sql!{ … }` is an *expression* that yields an iterator over typed rows. Bound
variables are referenced by `:name` and resolve to in-scope `pell` identifiers
by ordinary lexical scope — no separate binding clause. Bind types are checked
against the SQL plan at compile time when a DB connection is configured,
otherwise at first run.

```pell
let status  = "ACTIVE";
let dept_id = my_dept;

let active = sql! {
  select id, name from employees
  where status = :status and dept_id = :dept_id
};

for row in active {
  log::info(row.name);
}
```

If a bind needs renaming, do it with an ordinary `let`. We considered a
trailing `with (status = "ACTIVE", dept_id = my_dept)` clause and dropped it:
in every realistic case the locals already exist with the right names, and
the clause was pure ceremony repeating them.

Bulk operations:

```pell
let ids: list<number> = [1, 2, 3, 4];
sql! {
  update employees set bonus = bonus * 1.1
  where id in (select column_value from table(:ids))
};
```

Lists lower to nested table types declared at module scope and reused. Note
that to use `TABLE(:xs)` in embedded SQL the element type must be SQL-
visible, which means the nested-table type itself has to be a **schema-
level** `CREATE TYPE … AS TABLE OF …`, not a package-local
`TYPE … IS TABLE OF …`. The compiler emits the schema types under a
configurable schema prefix (`pell_…`) and treats them as part of the
deploy artifact. See §5.1.4 for the element-type restrictions this
implies.

### 4.5.1 No explicit cursors

You never write `OPEN` / `FETCH` / `CLOSE` in `pell`. The `sql!{}` expression
is an iterator; consuming methods on it lower to one of:

- a cursor `FOR` loop (for streaming iteration and `.first()` / `.first_n()`),
- a `BULK COLLECT` with `FETCH FIRST n ROWS ONLY` (for `.collect()` on
  bounded result sets),
- a `count(*)` query (only for `.is_empty()` / `.count()` when no rows are
  needed).

In particular, **`.first()` does not lower to `SELECT INTO`** and never
involves `NO_DATA_FOUND`. It lowers to (the holder record matches the
*projection*, not the table — `employees%rowtype` would be wrong when the
select list is narrower):

```plsql
declare
  type t_proj is record (
    id   employees.id%type,
    name employees.name%type
  );
  l_result t_proj;
  l_found  boolean := false;
begin
  for r in (
    select id, name from employees
    where status = :status and dept_id = :dept_id
    fetch first 1 rows only
  ) loop
    l_result := r;
    l_found  := true;
    exit;          -- belt-and-braces; FETCH FIRST already bounds the cursor
  end loop;
  -- l_found drives Option::Some vs Option::None at the call site
end;
```

No exception machinery, no implicit `SELECT INTO`, no cursor variable in
scope. The cursor `FOR` loop *is* a cursor underneath — but it's the
implicit, scoped form, not the manual `OPEN`/`FETCH`/`CLOSE` form Shaun is
allergic to. If you ever need direct access to a `sys_refcursor` for FFI to
hand-written PL/SQL, that's `unsafe::cursor!{}` and stays out of normal code.

Optimizer note: `FETCH FIRST n ROWS ONLY` plans the same as `ROWNUM <= n`
in modern releases; it does not pessimize plans on indexed lookups. Where
the source query is itself bounded (PK lookup, unique index), the compiler
elides `FETCH FIRST 1` because the plan can't return more than one row
anyway — keeps the emitted SQL readable.

`.one()` lowers similarly but with `fetch first 2 rows only` so we can
distinguish "exactly one" from "more than one" without a second query — see
§6.5 for the full table.

### 4.5.2 Language injection inside `sql!{}`

`sql!{}` is a hard boundary in the grammar: the braces flip the editor into a
sub-context where Oracle SQL applies. The tree-sitter grammar declares the
body as an injection point (`injection.language = "sql"`), so a tree-sitter
SQL grammar (we'll target `tree-sitter-sql` with an Oracle dialect overlay)
renders highlighting, structural navigation, and folding inside the braces
exactly as a `.sql` buffer would.

LSP responsibilities at the injection boundary:

- **Completion**: identifier completion inside `sql!{}` is dispatched by the
  cursor's parent node. Inside the SQL body, completion sources are
  (1) keywords/builtins from the SQL grammar, (2) table/view names from a
  configured schema snapshot (loaded from `pell.toml` or live via
  `pell check --db`), (3) column names scoped to tables already named in the
  `FROM` clause, (4) bind names — see below.
- **Bind variables** (`:status`, `:dept_id`): these are *not* SQL identifiers;
  they cross back into `pell` scope. Each `:name` token resolves to a `pell`
  `let`/`var`/parameter in lexical scope at the `sql!{}` site. The LSP
  surfaces this as:
  - **Inlay hint** after `:status` showing the inferred bind type
    (`:status: text`).
  - **Go-to-definition** from `:status` jumps to the `let status = …`
    binding, *not* into the SQL grammar.
  - **Diagnostic** "no binding `:status` in scope" at the exact span of the
    bind token (not the whole block) when resolution fails.
  - **Find-references** for a `let` includes its uses as `:name` inside any
    `sql!{}` in scope.
- **Diagnostics granularity**: errors from our embedded SQL parser must point
  at the offending token (line/column within the brace body), not at the
  whole `sql!{}` span. The emitter stores the brace-body's source offset so
  inner spans map back to file positions cleanly.
- **Hover**: hovering a column name shows its declared type (when a schema
  snapshot is loaded); hovering a bind shows the resolved `pell` binding
  and its type.
- **Rename**: renaming a `pell` `let status` updates every `:status` use in
  enclosing `sql!{}` blocks. Renaming a SQL identifier (column, table) is
  *not* in v1 — it would require coordinating across `.pell` and the schema,
  and that's a footgun without a real refactor engine on the DB side.

### 4.5.3 Writes — `DmlResult`, `.rowcount()`, `.returning::<T>()`

A `sql!{}` whose top-level statement is `INSERT` / `UPDATE` / `DELETE` /
`MERGE` evaluates to a `DmlResult` instead of an iterator. The compiler
chooses the return type by parsing the SQL; reads stay reads, writes
become writes, no extra keyword. Reaching for `.first()` or `.one()` on a
`DmlResult` is a compile error; reaching for `.rowcount()` or
`.returning::<T>()` on a read iterator is a compile error.

```pell
// Fire-and-forget write — the DmlResult is discarded (and the compiler
// allows that here because DmlResult is *not* @must_use).
sql! { update users set active = 0 where id = :id };

// Need the row count.
let n = sql! { delete from sessions where expires_at < :cutoff }.rowcount();

// Need the generated PK back.
let new_id = sql! {
  insert into users(name, email) values (:name, :email)
  returning id
}
  .returning::<number>()
  .one()?;

// Bulk update returning every affected id.
let touched: list<number> = sql! {
  update orders set status = 'shipped'
  where status = 'ready' and ship_after <= :now
  returning id
}
  .returning::<number>()
  .collect();
```

Rules:

- The DML executes eagerly on `sql!{}` evaluation. `.rowcount()` and
  `.returning::<T>()` read already-materialized state and can be called
  any number of times in any order.
- `.returning::<T>()` on a statement without a `RETURNING` clause is a
  compile error caught by the SQL parser.
- The element type `T` is checked against the `RETURNING` projection at
  compile time (when a DB connection is configured; otherwise at first
  run). `T` may be a primitive, a `record`, or an anonymous row type.
- Multi-row `RETURNING` lowers to `BULK COLLECT INTO` a nested table of
  `T`'s lowered form; single-row `.one()` lowers to a scalar
  `RETURNING … INTO`.
- `.rowcount()` returns `int`, never `Option<int>` or `Result`. `0` means
  "no rows affected"; that's a legitimate outcome, not an error.

### 4.5.4 Row-level locking — `.for_update()`

`SELECT … FOR UPDATE` is expressed as a modifier on the read iterator:

```pell
transaction {
  let acct = sql! {
    select id, balance from accounts where id = :id
  }
    .for_update()
    .one()?;

  sql! {
    update accounts set balance = :new_balance where id = :acct.id
  };
}  // commit here; locks released
```

Variants:

| Call | Lowers to |
|---|---|
| `.for_update()` | `FOR UPDATE` |
| `.for_update().nowait()` | `FOR UPDATE NOWAIT` |
| `.for_update().wait(seconds)` | `FOR UPDATE WAIT N` |
| `.for_update().skip_locked()` | `FOR UPDATE SKIP LOCKED` |
| `.for_update_of(cols)` | `FOR UPDATE OF col1, col2` |

Constraints, enforced by the typer:

- `.for_update()` is **only legal inside an enclosing `transaction { … }`
  block** (§4.8). Outside one it's a compile error — autocommit would
  release the lock immediately, which is never what you meant.
- It can only attach to a read iterator. Calling it on a `DmlResult` is a
  compile error.
- Mutually exclusive with `.nowait()` / `.wait(N)` / `.skip_locked()` — the
  typer enforces "at most one wait policy."

### 4.6 Pipelines

```pell
let names = sql! { select id, name from employees }
  |> filter(|r| r.name.starts_with("A"))
  |> map(|r| r.name.upper())
  |> collect::<list<text>>();
```

Pipelines compile to either (a) plain loops with assoc. arrays when the input
is small/local, or (b) pure SQL when the operations are SQL-expressible
(early v1: only the latter when the user writes `|> sql` explicitly; auto-
fusion is a v2 idea).

Honest caveat: until auto-fusion lands, a pipeline over a `sql!{}` source
materializes the rows into PL/SQL collections and runs `filter`/`map` in PL/SQL.
That's fine for small result sets and a performance trap for large ones. If
you can express the work in SQL, write it in SQL — pipelines are not a free
abstraction over `WHERE`.

### 4.7 No `BEGIN`/`END`, no `DECLARE`

Locals are introduced with `let` (immutable) and `var` (mutable). Block scope
is curly braces. No forward declarations.

```pell
fn compound(p: number, r: number, n: number) -> number {
  var balance = p;
  for _ in 1..=n {
    balance = balance * (1 + r);
  }
  return balance;
}
```

### 4.8 Transactions

PL/SQL's "every statement participates in the ambient transaction, and you
call `COMMIT`/`ROLLBACK` when you mean it" model is the source of more
bugs than every cursor mistake combined. `pell` replaces it with a scoped
construct:

```pell
fn transfer(from: number, to: number, amt: number)
  -> Result<Unit, NotFound | Overdraft>
{
  transaction {
    let src = sql! {
      select id, balance from accounts where id = :from
    }.for_update().one()?;

    if src.balance < amt {
      return Err(Overdraft { account: from });   // rolls back
    }

    sql! { update accounts set balance = balance - :amt where id = :from };
    sql! { update accounts set balance = balance + :amt where id = :to };
  }  // commits here on normal exit
  return Ok(());
}
```

Semantics:

- Normal exit from the block → `COMMIT`.
- Any propagated error (`?`, explicit `return Err(…)` out of the block,
  invariant panic) → `ROLLBACK`.
- The block is an expression of type `Unit` (or `Result<Unit, …>` if any
  enclosed statement can fail); you can't return a value out of it
  without writing `let x = transaction { … x };`.
- **Nesting = `SAVEPOINT`.** An inner `transaction { … }` opens a savepoint
  on entry; normal exit releases it, error path rolls back to it without
  unwinding the outer transaction. This is the only legal way to write
  partial-failure logic.
- **Outside a `transaction { … }`, DML autocommits.** This is intentional
  for ergonomic one-shot scripts and tests, but `pell fmt` and the linter
  warn on any DML statement at fn-body scope that isn't either (a) the
  sole statement in the fn, or (b) inside a `transaction { … }`. The
  warning is `dml_outside_transaction`, opt out with `@allow(...)`.
- `@autonomous` (§9.2) opens a *separate* transaction context that does
  not nest into the outer one. An `@autonomous` block inside a
  `transaction { … }` is allowed; its commit/rollback doesn't affect the
  outer.
- `finally { … }` blocks run after the transaction's commit/rollback has
  been issued. A `finally` cannot rescue a transaction that has already
  rolled back, but it can log the outcome.

Why not implicit per-fn transactions? Because most real fns either don't
touch the database or want fine-grained control over what's in the unit
of work. Implicit per-fn is fine for trivial CRUD and a mess for anything
real.

Why not PL/SQL-style "ambient + explicit commit"? Because the structured
boundary is exactly what enables: (1) automatic rollback on `?`, (2) safe
`finally` semantics, (3) honest `FOR UPDATE` (§4.5.4), and (4) the
exception-safety guarantees the rest of the language leans on.

## 5. Type system (v1)

- Primitives: `number(p, s)`, `int`, `text`, `bool`, `date`, `timestamp`,
  `interval`, `bytes`, `json`.
- `T?` for nullable; **`Option<T>` is the canonical name and `T?` is sugar
  that desugars to it before type checking**. They are *the same type*,
  not two types that print the same way. In surface code prefer `T?`; the
  prelude is written in terms of `Option<T>` (`.first()` returns
  `Option<Row>`, etc.) and `pell fmt` rewrites `Option<T>` annotations to
  `T?` outside the prelude. Consequences:
    - `T??` is rejected at parse time; nesting `Option` requires the explicit
      `Option<Option<T>>` form (and is legal, since the inner and outer
      `None`/`Some(None)` are distinguishable at runtime — see lowering).
    - There is exactly one `None` constructor per type instantiation.
    - The PL/SQL lowering of an `Option<T>` field is **not** "a `T` slot with
      NULL allowed". It is an object type `option_T(tag pls_integer, val T)`,
      with `tag = 0` for `None` and `tag = 1` for `Some`. Plain non-`Option`
      fields lower with `NOT NULL` constraints as before; `Option<T>` fields
      do not (the tag distinguishes presence). This means `Option<Option<T>>`
      is representable: the outer tag and inner tag are independent.
    - Scalar `Option<T>` *parameters* and *locals* of a primitive `T` may, as
      an optimization, lower to a bare nullable `T` slot when the compiler
      can prove the `Option` never nests and never crosses a generic
      boundary. This optimization is invisible to the surface language.
- `Result<T, E>` where `E` may be a single error type or a `|` union of error
  types declared in scope. Error unions are **closed structural sums**,
  normalized by the typer to a canonical, deduplicated, sorted form:
  `A | B | A` and `B | A` are the *same* type. `Result<T, E1>` is *not* a
  subtype of `Result<T, E1 | E2>`; widening across a call boundary is
  inserted by the typer at the call site. `E` may be empty (`Result<T, !>`
  is the prelude `Infallible`); `?` on such a value is a no-op.
- `record { … }` — nominal: two records with identical fields but different
  names are different types. Structural conversion is opt-in via `.into`
  (see §5.1.5).
- `enum` — closed sum with payloads (lower to integer discriminator + per-arm
  record fields, or to a JSON tagged object when stored in a column).
- `list<T>`, `map<K,V>`, `set<T>` — see §5.1 for the surface API and the
  PL/SQL collection types each one lowers to.
- No user-written generics in v1. The built-ins `Option`, `Result`, `list`,
  `map`, `set` are compiler-intrinsic: each instantiation `Option<Employee>`,
  `list<number>`, etc. is monomorphized by the emitter into a concrete
  PL/SQL type with a mangled name. Methods on these intrinsics (`xs.first()`,
  `opt.expect(msg)`) are also intrinsic — they are *not* evidence of a
  general generic-method facility. See §11.3.
- No traits/interfaces in v1. Function overloading is also out.

### 5.1 Collections — one surface, three backing types

PL/SQL ships three collection types with very different semantics:

| PL/SQL type | Dense? | Bounded? | Key | Usable in SQL `TABLE()`? | Storable in column? |
|---|---|---|---|---|---|
| Associative array | sparse | unbounded | `PLS_INTEGER` or `VARCHAR2(N)` | **no** | no |
| Nested table | dense | unbounded | integer | yes | yes |
| Varray | dense | bounded | integer | yes | yes |

`pell` exposes one surface type per *role* and the compiler picks the backing
type:

| `pell` type | Role | Lowers to | Notes |
|---|---|---|---|
| `list<T>` | ordered sequence | nested table | usable in SQL via `TABLE(:xs)` *only* when `T` is SQL-visible (see §5.1.4); the type is emitted as a schema-level `CREATE TYPE` so SQL can see it |
| `map<K,V>` | keyed lookup | associative array | `K` must be `int`, `text`, or a primitive that has a derived `to_key()` |
| `set<T>` | unique unordered | associative array indexed by `T`'s key | `set<T>` is sugar for `map<T, unit>` |

There is no `varray<T, N>` in v1. If you need a bounded buffer, use `list<T>`
and assert the length. Reconsider if real code shows we need static bounds.

### 5.1.1 `list<T>` — surface

```pell
let xs: list<number> = [1, 2, 3, 4];

xs.push(5);
xs.len();                     // -> 5
xs[0];                        // -> Option<number>; out-of-bounds is None, not panic
xs[0].expect("non-empty");    // -> number, invariant panic on out-of-bounds
xs.first();                   // -> Option<number>
xs.last();                    // -> Option<number>

for x in xs {                 // by value
  log::info("{x}");
}

for (i, x) in xs.enumerate() {
  log::info("{i}: {x}");
}

// SQL bridging — lists pass directly into `TABLE(:xs)`:
sql! {
  delete from users
  where id in (select column_value from table(:xs))
};
```

Lowering of indexed access deliberately returns `T?` for `xs[i]`, not
a raw `T`. PL/SQL's nested-table indexing raises `SUBSCRIPT_BEYOND_COUNT` or
`SUBSCRIPT_OUTSIDE_LIMIT`; we catch those at the boundary the same way §6.5
handles `NO_DATA_FOUND` for `.first()`. If you want the panic-on-miss
semantics, write `.expect("…")`.

Tradeoff: this means a tight numeric loop over a list with `xs[i]` is more
verbose than the PL/SQL equivalent (every access has to handle `None`), and
the obvious workaround — iterate with `for x in xs` or `for (i, x) in
xs.enumerate()` — needs to be the documented norm. Indexed access is for
random access, not iteration.

### 5.1.2 `map<K, V>` — surface

```pell
let cache: map<text, User> = map::new();

cache.insert("alice", alice);
cache["bob"] = bob;                       // sugar for .insert

cache.get("alice");                       // -> Option<User>
cache["alice"];                           // sugar for .get; -> Option<User>
cache["alice"].expect("seeded user");     // -> User, invariant panic on miss

cache.contains_key("bob");                // -> bool
cache.remove("bob");                      // -> Option<User> (the removed value, if any)
cache.len();                              // -> int
cache.is_empty();                         // -> bool

// Iteration — order is unspecified (PL/SQL assoc arrays iterate by
// key order, but we don't promise that, in case we ever change backing types).
for (key, val) in cache {
  log::info("{key} -> {val.name}");
}

for key in cache.keys()    { … }          // .keys() -> list<K>
for val in cache.values()  { … }          // .values() -> list<V>

// Map literals
let counts: map<text, int> = {
  "apples":  3,
  "bananas": 7,
};
```

Lowering example for `map<text, User>`:

```plsql
-- Module-level type declaration (deduplicated per module)
type t_user_by_text is table of user_rec index by varchar2(4000);
```

`cache["alice"]` lowers to a generated helper:

```plsql
function user_by_text__get(p_map in t_user_by_text, p_key in varchar2)
  return option_user_rec
is
begin
  if p_map.exists(p_key) then
    return option_user_rec.some(p_map(p_key));
  else
    return option_user_rec.none;
  end if;
end;
```

Iteration lowers to PL/SQL's canonical assoc-array walk — fully hidden:

```plsql
declare
  l_key varchar2(4000) := cache.first;
begin
  while l_key is not null loop
    -- body using cache(l_key) and l_key
    l_key := cache.next(l_key);
  end loop;
end;
```

### 5.1.3 Key types and `to_key()`

PL/SQL associative arrays only index by `PLS_INTEGER` or `VARCHAR2(N)`.
`pell` accepts a broader set of key types and lowers them as follows:

| `K` | Backing index | Notes |
|---|---|---|
| `int` | `pls_integer` | direct |
| `text` | `varchar2(4000)` | default width; `map<text(N), V>` to override |
| `number(p,s)` | `varchar2(4000)` | via `to_char` canonicalized |
| `date` / `timestamp` | `varchar2(35)` | ISO-8601 canonical form |
| `record { … }` | `varchar2(4000)` | requires explicit `derive Key` on the record |
| `enum` | `pls_integer` | discriminator |

Anything else is a compile error in v1. The canonicalization rules live in
the prelude as `Key::to_key(&self) -> text` (or `pls_integer`); user records
opt in with `derive Key`, which generates a deterministic key from the
record's fields in declaration order (see §5.1.6). No `Hash` trait, no
rebalancing concerns — PL/SQL handles the hashing under the hood.

**Key-width overflow.** For `map<text, V>` (default `varchar2(4000)`) or
`map<text(N), V>`, attempting to `insert`, look up, or remove with a key
whose UTF-8 byte length exceeds the declared width is a **runtime invariant
panic** (§6.5.1), not a truncation and not a regular error. Rationale:
truncation aliases distinct keys, and surfacing a `Result<…, KeyTooLong>`
on every map operation would poison the ergonomics of the most common
collection. Callers who can produce unbounded keys must hash or truncate
*before* the map boundary — and pick the policy explicitly. The compiler
emits a static warning if it can prove the key expression's type is
`text(M)` with `M > N`.

### 5.1.4 SQL bridging

The collection type matters when SQL gets involved:

- `list<T>` is a nested table and works directly: `select … from table(:xs)`,
  but only when `T` is SQL-visible. Specifically:
  - **Allowed**: scalar SQL types (`number`, `text → varchar2`, `date`,
    `timestamp`, `bytes → raw`/`blob`, `json`), schema-level object types,
    and `list<U>` where `U` is itself allowed (nested-table-of-nested-table
    via wrapper object).
  - **Disallowed**: PL/SQL-only types in the element (`boolean` is SQL-
    visible in 23, so it's fine here; pre-23 it was not), `record` types
    that themselves contain nested records or assoc arrays, anonymous
    cursor types. The compiler refuses these at the `sql!{}` boundary and
    points the user at `.iter()` + a row-at-a-time path.
  - `interval` works but the schema-level type must spell out
    `INTERVAL DAY TO SECOND` / `INTERVAL YEAR TO MONTH` precision; the
    compiler emits one schema type per concrete precision.
- `map<K, V>` is an associative array and **cannot** be passed to SQL.
  Use `.keys()` or `.values()` to produce a `list<…>` first; the compiler
  does the nested-table copy:

  ```pell
  let stale_ids: list<number> = cache.keys();
  sql! { delete from sessions where id in (select column_value from table(:stale_ids)) };
  ```

- `set<T>` shares `map`'s SQL restriction; use `.to_list()` to bridge.

This is the only place the surface language exposes the difference between
the backing types, and it's intentional — silently copying a 10M-entry map
into a nested table to satisfy SQL would be a foot-gun worse than naming the
copy.

### 5.1.5 Record conversion: `.into`

`record` is nominal; `Row` returned from a `sql!{}` is its own anonymous
nominal type. `value.into::<Target>()` is the *only* way to cross record
identities. It is compiler-derived, not reflective:

- The typer accepts the conversion iff `Target`'s fields are a subset of the
  source's fields *by name and type* (or by name and `T → Option<T>`
  widening). Extra fields on the source are dropped; missing fields on the
  target are a compile error.
- There is no user-written `impl Into for X`. v1 ships exactly one form: the
  derived field-by-field copy. If you need a transformation, write a regular
  `fn`.
- `.into` between two declared `record` types still requires the same
  structural compatibility; the nominal distinction is preserved everywhere
  *except* at the explicit `.into` call.

### 5.1.6 `derive Key` — canonical encoding

A record annotated `derive Key` may be used as `K` in `map<K, V>`. The
generated `to_key()` produces a `varchar2` from the fields in declaration
order, using a length-prefixed encoding that is collision-free across all
field types currently allowed:

- each field is encoded as `<len>:<canonical>` where `<canonical>` is the
  type's own `to_key` form (ISO-8601 for dates, `to_char` with full
  precision for numbers, the bytes themselves quoted for `text`),
- fields are joined with an unambiguous separator (`\x1f`),
- the record's type identity is prefixed (`module.RecordName|`) so two
  records of different types with identical fields produce different keys.

**Restrictions enforced by the typer** (anything else is a compile error
in v1):

- All fields must themselves be `Key`-eligible primitives or nested
  `derive Key` records.
- **`list<T>`, `map<K,V>`, `set<T>`, `Option<T>`, and `json` fields cannot
  appear in a `derive Key` record.** Their canonical encoding is either
  undefined (`json`) or invites footguns (deep equality on a 10k-element
  list at every map probe). If you need them, write a regular `fn` that
  derives a key explicitly and use `map<text, V>` keyed by its output.

The total encoded length must fit in the map's key width
(default `varchar2(4000)`). Exceeding it is a **runtime panic** (§6.5.1),
not a silent truncation — silent truncation would cause aliasing of
distinct keys, which is exactly the bug `derive Key` exists to prevent.
See §5.1.3 for the parallel rule on `map<text(N), V>`.

## 6. Error model — deeper dive

This is the most opinionated part, so it gets its own section.

### 6.1 Rules

1. A function's signature lists every error variant it may return. No hidden
   exceptions.
2. `?` is the only implicit propagation. Everything else is `match` or
   `if let Ok(v) = …`.
3. `WHEN OTHERS` is **never** emitted by the compiler. The common PL/SQL
   "catch everything just to log it then re-raise" idiom is *not* exception
   handling — it's cleanup — and gets its own construct: `finally { }`
   (see §6.3). There is no `catch _`.
4. Unrecoverable PL/SQL conditions (e.g. `ORA-01403 no_data_found` in a
   context where we expected exactly one row) are mapped at the iterator
   boundary to typed errors (`NotFound`), not propagated as ORA codes.
5. Predefined Oracle exceptions (`DUP_VAL_ON_INDEX`, `VALUE_ERROR`,
   `INVALID_NUMBER`, etc.) are available as `oracle::DupValOnIndex`, etc.,
   and can be caught/converted at the SQL boundary:

   ```pell
   sql! { insert into users(email) values (:email) }
     .map_err(oracle::DupValOnIndex, |_| DuplicateEmail { email });
   ```

### 6.3 `finally` — cleanup, not handling

```pell
fn charge_account(id: number, amount: number) -> Result<Unit, ChargeError> {
  let span = log::span("charge", id, amount);

  sql! { update accounts set balance = balance - :amount where id = :id };
  sql! { insert into ledger(...) values (...) };
  return Ok(());
} finally {
  log::info("charge_account done in {span.elapsed()}ms");
}
```

Semantics: `finally` runs on **both** the success path and the error path.
On error, the error continues to propagate after the block runs — `finally`
cannot swallow or transform it. (That's what `match` is for, and it requires
naming the variants you handle.)

This subsumes the dominant PL/SQL pattern:

```plsql
-- Before
BEGIN
  do_stuff;
EXCEPTION
  WHEN OTHERS THEN
    log_error(SQLCODE, SQLERRM);
    RAISE;
END;
```

```pell
// After
do_stuff() finally {
  log::info("do_stuff done");   // runs on both success and error
}
```

**Foot-gun.** `finally` runs on the success path *and* the error path. The
PL/SQL `WHEN OTHERS THEN log; RAISE;` idiom looks superficially similar but
only ran on error. Mechanically swapping one for the other will start logging
on every successful call. The compiler emits a warning when a `finally` body
contains a string literal matching `/(?i)\b(fail|error|exception|abort|panic)\b/`
without also referencing the caught error — opt out with
`@allow(finally_error_log)`. This isn't elegant, but it catches the migration
mistake before it ships.

If you only want to run cleanup on the error path, write it explicitly:

```pell
match risky() {
  Ok(v)  -> v,
  Err(e) -> { log::error("risky failed: {e}"); return Err(e); }
}
```

We deliberately don't add a separate `on_error { }` block in v1 — `finally`
plus `match` covers the cases without inventing a third construct. Revisit if
real code shows the always-runs semantics is wrong more than rarely.

**Lowering** (sketch — uses a nested anonymous block + a local procedure so
the finally body isn't duplicated, even though PL/SQL has no native
`finally`):

```plsql
declare
  procedure finally_body is begin
    -- emitted finally code
  end;
begin
  begin
    -- emitted body
  exception
    when others then
      begin finally_body; exception when others then null; end;
      raise;                  -- preserves the original error stack
  end;
  finally_body;
end;
```

Edge cases the compiler has to handle and the sketch above does *not*:

- **Early `return` from the body.** PL/SQL has no labelled `return` out of
  a nested block, so the compiler rewrites early returns as
  `l_return_value := …; goto pell_finally;` and emits a single
  `<<pell_finally>>` label that calls `finally_body` then `return`s.
  (`goto` out of an inner block is legal as long as it doesn't cross a
  handler boundary; the compiler validates.)
- **`finally_body` itself raises.** PL/SQL's last-raised-wins rule means
  a naive call would mask the original exception. The wrapper above
  swallows secondary errors during error unwinding (logging them via the
  runtime helper) so the original cause survives; in the success path the
  finally body is allowed to raise normally.
- **Re-raise inside the body's handler.** Already handled — the inner
  `RAISE` (with no exception name) reraises the current one; the outer
  block has no handler so it propagates out of the wrapper unchanged.
- **Package initialization blocks.** A module-level `init { } finally { }`
  lowers into the package body's bottom `BEGIN … END;` initializer. Note
  that any error from package init poisons the package for the session
  (subsequent calls raise `ORA-04068` until reconnection); `finally`
  there can log but cannot rescue.
- **`@autonomous` on the enclosing fn.** The pragma stays at the
  outermost block; the finally wrapper is *inside* the autonomous scope,
  so cleanup runs in the autonomous transaction and any
  `commit`/`rollback` in `finally` applies there, not to the caller's
  transaction.

If the finally body is small (single statement, no locals), the compiler may
inline it directly into both arms to avoid the local procedure call — see
`@inline_finally` (§9.3).

### 6.5 "Catch and release" vs. "this shouldn't happen"

`NO_DATA_FOUND` (ORA-01403) is PL/SQL's worst design wart: a single condition
covers both *expected absence* ("the row may legitimately not exist") and
*invariant violation* ("the foreign key says this row MUST exist, so getting
no row means our database is corrupt"). Same exception code, completely
different intent, no way to tell at the catch site.

`pell` distinguishes these at the **API surface**, not the catch site. You
choose your meaning by which terminator you call on the iterator:

| Call site | Return type | 0 rows | 1 row | 2+ rows |
|---|---|---|---|---|
| `.one()` | `Result<Row, NotFound \| TooMany>` | `Err(NotFound)` | `Ok(r)` | `Err(TooMany)` |
| `.first()` | `Option<Row>` | `None` | `Some(r)` | `Some(first)` |
| `.one_or_none()` | `Result<Option<Row>, TooMany>` | `Ok(None)` | `Ok(Some(r))` | `Err(TooMany)` |
| `.expect("…")` (chained on any of the above) | `Row` | **invariant panic** | `r` | **invariant panic** |

`.one()` is the boring default: it forces the caller to deal with both
absence and duplicates, which is the safest assumption for code that names
"the row." Reach for `.first()` only when absence is genuinely a value (the
`/users/:id` rendering case), and `.one_or_none()` only when the query is
known-singleton by an index but the caller still wants to treat absence as
data rather than an error. If you find yourself reaching for `.one_or_none()`
more than once in a module, the query probably wanted `.one()` or `.first()`.

The three intents map cleanly:

```pell
// Intent 1: absence is a value (not an error). User looked up an ID that
// might not exist. Caller decides what to render.
let maybe_user = sql! { select * from users where id = :id }.first();
match maybe_user {
  Some(u) -> render(u),
  None    -> render_404(),
}

// Intent 2: absence is a *handled* error. The caller has a real recovery
// path and wants the type system to make them write it.
let user = sql! { select * from users where id = :id }.one()?;  // NotFound bubbles

// Intent 3: absence is a bug. There's a foreign key, a seed row, an
// invariant that says this row must exist. If it doesn't, we want to fail
// loud and uncatchably — not return a weird default, not silently log.
let admin = sql! { select * from users where username = 'admin' }
  .one()
  .expect("admin user must exist (seeded at install time)");
```

### 6.5.1 Invariant panics are uncatchable *in pell source*

`.expect(msg)` and the related `.unwrap()` are the "this shouldn't happen"
channel. They are **not** regular errors:

- They are not declared in any function's error signature.
- They cannot be caught by `match Err(...)` — there is no variant to match.
- `finally { }` still runs (it always runs), but it cannot stop the panic.
- They propagate to the top of the call stack and abort the request.

"Uncatchable" here is a *pell-source-level* property, not an Oracle-level
one. The lowering uses `RAISE_APPLICATION_ERROR`, which produces an
ordinary `ORA-20001` exception — hand-written PL/SQL elsewhere in the same
schema (or `WHEN OTHERS` in a stored procedure not generated by `pell`)
*can* still catch it. The compiler's job is only to refuse to emit such a
handler from `pell` source. Mention this in operator docs so people don't
treat "uncatchable" as a security property.

Lowering: a reserved error code in the `-20000..-20999` band (initial
choice: `-20001`, name `PELL_INVARIANT_VIOLATION`) raised via
`RAISE_APPLICATION_ERROR`. The compiler:

- refuses to let any user `error` declaration use that code,
- refuses to compile a `match` or `catch` that would catch it,
- includes the source location of the `.expect()` call and its message in
  the raised text, so the operator sees `pell invariant at hr/employees.pell:47: admin user must exist`.

The `RAISE_APPLICATION_ERROR` message argument is capped at **2048 bytes**
in Oracle 23 (unchanged since 10g). The compiler truncates long messages
with an ellipsis and emits the full text to a side channel (the runtime
log table, if `pell_runtime` is in use; otherwise `DBMS_OUTPUT` as a
fallback).

This gives Shaun's two cases distinct, syntactically obvious forms:

- *catch and release* → use `.one()` and `?`, or `match` on the `Result`.
- *this shouldn't happen* → use `.expect("invariant: …")`.

**Honesty caveat.** "Uncatchable" is a `pell`-surface claim, not a PL/SQL
runtime claim. The emitted `RAISE_APPLICATION_ERROR(-20001, …)` is a normal
Oracle exception and any hand-written PL/SQL upstream of the `pell` code can
catch it with `WHEN OTHERS`. We can keep `pell` itself from writing such a
catch, but if your call graph crosses a boundary into legacy PL/SQL, that
legacy code can swallow invariant panics. Document this; don't pretend
otherwise.

Generalization: `.expect` / `.unwrap` are also available on `Option<T>` and
`Result<T, E>` everywhere, not just on SQL terminators. Same semantics.

**Composition.** `.expect` and `.unwrap` peel exactly **one** layer. Their
return types are:

| Receiver | `.expect(msg)` returns |
|---|---|
| `Option<T>` | `T` (panics on `None`) |
| `Result<T, E>` | `T` (panics on any `Err`, message includes the variant) |
| `Result<Option<T>, E>` | `Option<T>` (panics on `Err`, *not* on `Ok(None)`) |
| `Option<Result<T, E>>` | `Result<T, E>` (panics on `None`) |

To collapse both layers, chain: `r.expect("e").expect("none")`. There is no
silent double-unwrap. The first form is the most common stumble — a caller
who writes `find_user(id).expect("must exist")` against a fn returning
`Result<Option<User>, DbError>` gets an `Option<User>` back and a type
error one line later, which is the right outcome.

### 6.6 Lowering strategy (errors)

Two candidates; we'll prototype both and pick based on stack-trace quality:

**(A) `RAISE_APPLICATION_ERROR` + JSON payload.** Each error variant gets a
stable code in the `-20000..-20999` band. Payload is a JSON string in the
message. Catching unmarshals back to the typed variant. *Pros*: one
mechanism, plays well with cross-package boundaries. *Cons*: the band has
exactly **1000 codes** and is shared across the entire schema with every
other tool, ORM, and hand-written package — for a non-trivial codebase this
runs out fast. The `RAISE_APPLICATION_ERROR` message is capped at **2048
bytes**, which is tight once payloads include strings or nested records;
JSON gets truncated silently if we don't pre-flight the size. Realistically
(A) needs a runtime helper anyway: a side table (or context) keyed by a
correlation id stuffed in the message, so payloads bigger than 2KB still
work. That collapses much of the "no runtime dep" advantage.

**(B) Sentinel `EXCEPTION` per variant + session-scoped payload register.**
Compiler emits a `pell_runtime` package whose package-level state holds a
stack of payload records keyed by `(error_identity, depth)`; `raise` pushes,
`catch` pops. PL/SQL package state is **session-scoped**, not thread-local
(there are no threads in the PL/SQL execution model — sessions serialize
through a single call stack), so the stack discipline is well-defined as
long as every raise is paired with either a catch or a top-of-stack
unwind-and-clear at the outermost generated entry point. *Pros*: no
message-size limits, real `EXCEPTION` per variant means `WHEN my_err THEN`
works in mixed `pell`/hand-PL/SQL code, payload typing survives across
package boundaries. *Cons*: introduces a runtime dep, the outermost
generated entry point must always clear residual state in a top-level
`finally` (otherwise the next call in the same session sees stale
payloads), and `@autonomous` blocks need their own nested stack frame
because they may raise into a different transaction context.

Initial bias: **(A)** for v0/M3 prototype on the strength of "no runtime
dep", but the 1000-code band and 2KB cap mean we should expect to migrate
to **(B)** by the time real codebases land. Worth structuring the emitter
so the choice is a single backend trait.

## 7. Module / package model

- One `module foo.bar.baz;` per file. Compiles to package `foo_bar_baz` by
  default (configurable in `pell.toml`).
- `pub fn` / `pub record` / `pub error` is exported; everything else is
  package-private and goes only in the body.
- Cross-module calls are fully qualified at the PL/SQL level
  (`foo_bar_baz.greet(...)`) — no synonyms emitted by default; the project
  manifest can opt into emitting synonyms.
- Single optional runtime package: `pell_runtime` (only if lowering strategy
  (B) wins; see §6.6). Contains payload-passing helpers and nothing else.

## 8. Compilation model

```
.pell sources ──► lexer ──► parser ──► AST
                                        │
                                        ▼
                                resolver / typer
                                        │
                                        ▼
                                  IR (typed)
                                        │
                                        ▼
                            PL/SQL emitter + source map
                                        │
                                        ▼
                                .sql files + .map files
```

- **Source maps**: per-line mapping from emitted `.sql` to source `.pell`.
  When the DB returns `ORA-06512 at "FOO_BAR_BAZ", line 47`, the `pell` CLI
  rewrites that to `hr/employees.pell:23`. The map file is a stable,
  documented JSON schema (so a DAP adapter and JetBrains plugin can both
  consume it without re-reading the compiler internals). v1 ships the
  rewriter as a CLI filter (`pell trace`) plus the map format; an actual
  DAP adapter (breakpoints, stepping) is v2 — Oracle's debugger surface is
  shaped enough that we should not promise step-debugging in v1.
  Caveat: maps live alongside the build output and key off a body hash;
  once a package has been re-extracted via `DBMS_METADATA.GET_DDL` and
  re-applied by hand, the hash diverges and we fall back to "unmapped,
  line N of body". Production debugging therefore requires that the
  `build/maps/` directory matches the deployed artifact — `pell deploy`
  writes a `deploy.lock.json` recording the per-package hash so the CLI
  can refuse to map against a mismatched build.
- **Incremental compilation**: not in v1. Full rebuild is fine at module #5,
  tolerable at module #50 (single-digit seconds), and the wall it hits is
  closer to #150–200 than "a few hundred", once the LSP also wants
  whole-program type info for hover. v1 ships with two mitigations instead
  of true incrementality: (a) parsing + typing are parallel per module, and
  (b) `pell-lsp` keeps a hot in-memory module graph and only re-types the
  affected SCC on edit. Real incremental codegen (cached emit per module,
  fingerprinted by interface hash) lands in v1.1; design the IR so the
  module boundary is a clean cut point.
- **Output layout**:
  ```
  build/
    sql/
      hr_employees.spec.sql
      hr_employees.body.sql
    maps/
      hr_employees.map.json
    deploy.sql           # ordered concat of specs first, then bodies
    deploy.lock.json     # per-package hashes; matched against DB on deploy
  ```

## 9. Annotations and compiler directives

PL/SQL ships a grab-bag of compiler controls — `PRAGMA AUTONOMOUS_TRANSACTION`,
`PRAGMA INLINE(...)`, `PRAGMA UDF`, `PRAGMA SERIALLY_REUSABLE`, the
`DETERMINISTIC` clause, `RESULT_CACHE` clause, `PRAGMA RESTRICT_REFERENCES`,
and more — each with its own syntax and placement rules. `pell` collapses
all of these, plus our own compiler controls, under one uniform surface:
**annotations**, written `@name` or `@name(args)`, attached to items
(`fn`, `record`, `error`, `module`), statements, or `{ }` blocks.

The set is **closed**: the compiler knows every legal annotation, validates
its target and arguments, and errors on unknown names. No silent typos.

This closedness is deliberately IDE-friendly. After `@` the LSP offers the
complete legal set filtered by target (annotations valid on the item the
cursor is on); hover on an annotation shows its target rules, args, and any
mutual-exclusion constraints; misspellings are a diagnostic with a
quick-fix to the nearest legal name. An open/user-extensible annotation
surface would make all three of those degrade to "best effort".

### 9.1 Syntax

```pell
@deterministic
@result_cache
fn lookup_country(code: text) -> Option<text> {
  return sql! {
    select name from countries where code = :code
  }.first();
}

@autonomous
fn audit_log(event: text) {
  sql! { insert into audit(event, ts) values (:event, sysdate) };
  commit;
}

@deprecated("use find_employee_by_email instead")
fn find_by_email(email: text) -> Option<Employee> { … }

@test
fn it_promotes_an_eligible_employee() { … }

@module(package_name = "HR_EMP", emit_synonym = true)
module hr.employees;
```

Annotations stack, one per line. Each annotation declares the targets it is
valid on; `@autonomous` on a record is a compile error, not a runtime
surprise.

### 9.2 The PL/SQL pragma bridge

These map one-to-one to PL/SQL constructs. Using the annotation is the
*only* way to set them — the surface language has no separate keyword for
each, which keeps the core small and the grab-bag organized.

| Annotation | Target | Lowers to | Notes |
|---|---|---|---|
| `@deterministic` | fn | `DETERMINISTIC` clause | Required for function-based indexes and for fns called from SQL with caching. Compiler refuses to apply this to fns with observable side effects detectable from their body (mutations, IO, `sql!{}` writes). |
| `@result_cache` | fn | `RESULT_CACHE` clause | PL/SQL function result cache. Compiler enforces: no side effects, no session state, all args primitive or `derive Key`. **`RELIES_ON` is deprecated** (since 11.2; parsed and ignored in modern Oracle, including 23 — the engine tracks dependencies automatically). The annotation accepts no `relies_on` arg; if users want explicit hints we surface them as documentation, not pragma. |
| `@autonomous` | fn or `{ }` block | `PRAGMA AUTONOMOUS_TRANSACTION;` | Independent transaction context. Block form opens a nested anonymous block with the pragma — saves writing a whole helper fn just to commit independently. Note: an autonomous block **must** end with `commit` or `rollback`; the compiler emits an implicit `rollback` on uncaught error paths so the autonomous tx doesn't leak. |
| `@inline` / `@no_inline` | call site (statement) | `PRAGMA INLINE(name, 'YES'/'NO')` | This pragma decorates the *next statement* in the caller, not the callee. Annotating a fn declaration with `@inline` is a compile error in `pell`; use it at the call site. |
| `@udf` | fn | `PRAGMA UDF;` | Fewer SQL ↔ PL/SQL context switches when called from SQL. Trades off the other direction: a UDF-pragma'd fn called from PL/SQL pays an extra context switch. Compiler-enforced conflicts: incompatible with `@autonomous` on the same fn (UDF assumes the fn participates in the calling SQL's transaction); incompatible with `OUT`/`IN OUT` params (UDF fns are pure-ish from SQL's view); silently coexists with `@deterministic` and `@result_cache` in 23. |
| `@serially_reusable` | module | `PRAGMA SERIALLY_REUSABLE;` | Whole module is session-stateless. Compiler refuses if the module declares package-level mutable state. PL/SQL requires this pragma in **both spec and body**; the emitter writes it to both. Note also: serially-reusable packages can't be used from database triggers, and their state resets between top-level server calls — flag in `pell doc`. |
| `@restrict_references(rnds, wnds, …)` | fn | `PRAGMA RESTRICT_REFERENCES(...)` | Legacy; deprecated since 8i, retained only for very old codebase interop. New code should rely on `@deterministic` / `@udf` instead. |

A worked example showing why this is better than raw PL/SQL — the canonical
"cache me a lookup" pattern:

```pell
@deterministic
@result_cache
@udf
fn country_name(code: text) -> Option<text> {
  return sql! {
    select name from countries where code = :code
  }.first();
}
```

Lowers to roughly:

```plsql
function country_name(p_code in varchar2)
  return option_varchar2
  deterministic
  result_cache
is
  pragma udf;
begin
  …
end country_name;
```

In PL/SQL you have to remember which of those go in the signature, which go
in the body as `PRAGMA`, and which can/can't coexist. In `pell` they're all
annotations on the function — same surface, compiler enforces the legal
combinations.

All three coexist legally in 23ai. The combination is exactly the one the
Oracle docs recommend for SQL-callable lookup functions: `DETERMINISTIC`
unlocks function-based-index and SQL-cache use, `RESULT_CACHE` memoizes
across sessions with automatic invalidation on referenced-table DML, and
`PRAGMA UDF` cuts the per-call SQL↔PL/SQL context-switch cost. The
compiler must still reject incompatible additions: `@autonomous` here would
be wrong (UDF + autonomous), and the moment the body grows a write `sql!{}`
the `@deterministic` and `@result_cache` checks fail.

### 9.3 Codegen control

| Annotation | Target | Effect |
|---|---|---|
| `@error_code(N)` | `error` decl | Pin to a specific code in `-20000..-20999`. Without it, the compiler assigns deterministically from the error's fully qualified name. `-20001` (the reserved `PELL_INVARIANT_VIOLATION`, §6.5.1) cannot be claimed. The band has 1000 codes total and is shared schema-wide; deterministic assignment can collide once a project exceeds ~few hundred error variants. The compiler emits a registry file (`build/error-codes.json`) and fails the build on collision; resolving requires either renaming the error or pinning a free code. See also §6.6, where lowering strategy (B) sidesteps the band entirely. |
| `@module(package_name = "X", emit_synonym = true)` | module | Override §7 defaults. |
| `@inline_finally` | `finally` block or fn | Force the inlining variant of §6.3 lowering even when the body is large. Useful for hot paths. |
| `@no_source_map` | fn | Skip source-map entries for generated/glue code. |

### 9.4 Semantic / API surface

These don't change emitted code much; they change what the type checker and
LSP do.

| Annotation | Target | Effect |
|---|---|---|
| `@deprecated("reason")` | fn, record, error | Warning at every call/use site; appears in `pell doc` output and LSP hover. |
| `@must_use` | fn | Caller must bind, `match`, or `?` the return value. **Default for any fn returning `Result<…>` or `Option<T>` (equivalently `T?`).** Opt-in elsewhere. Discarding the result of such a fn requires an explicit `let _ = …` to make the intent visible at the call site. |
| `@panics("when …")` | fn | Documents that the fn may raise an invariant violation (§6.5.1). Surfaces in LSP hover so callers can see it without reading the body. |
| `@unsafe` | fn | Calling it requires an `unsafe { … }` block. Used by `unsafe::cursor!{}` and any FFI. |

### 9.5 Tests, examples, lints

| Annotation | Target | Effect |
|---|---|---|
| `@test` | no-arg fn | Marks a unit test (`pell test`). |
| `@test(db)` | no-arg fn | Test requires a configured Oracle 23 connection; auto-skipped if absent. |
| `@ignore("reason")` | test fn | Skip with a reason. |
| `@example` | fn | Compiled (type-checked) but not run; keeps doc examples honest. |
| `@allow(rule)` / `@deny(rule)` | any item | Scoped lint control. |

### 9.6 What we deliberately don't have

- **No user-defined annotations.** v1 ships a closed set. No macros, no
  annotation processors, no plugin surface. Growing the language is preferred
  over shipping an extension API we'd later regret.
- **No expression-level annotations.** Targets are items, statements, and
  `{ }` blocks. `@inline (foo + bar)` is not a thing.
- **No magic strings.** Every annotation argument is typed and parsed.
  `@result_cache(relies_on = [hr.employees])` is a structured list of module
  references, not a free-form string the compiler shrugs at.

### 9.7 Implementation notes

Annotations live in the AST as nodes attached to items. The typer validates
them after name resolution, before lowering. PL/SQL emission consults the
annotation set to:

- prepend `PRAGMA …;` to the body where needed,
- adorn the signature line with `DETERMINISTIC` / `RESULT_CACHE …`,
- (for `@error_code`) bake the value into the error registry instead of
  generating one.

Conflict detection is the typer's job — e.g. `@deterministic` on a fn whose
body contains a write `sql!{}` is rejected at compile time, not at deploy
time.

## 10. Tooling — what ships with v1

| Tool | Purpose | Tech |
|---|---|---|
| `pell build` | Source → PL/SQL text | Rust (likely) or Go |
| `pell check` | Type-check + lint without emitting | Same as compiler |
| `pell fmt` | Canonical formatting (idempotent, no config knobs) | Same as compiler |
| `pell test` | Run unit tests; pure-`pell` tests run without a DB, `@test(db)` tests need a connection | Compiler + tiny harness |
| `pell deploy` | Apply build to a configured DB; idempotent, tracks state | sqlcl wrapper + state table |
| `pell doc` | Render module/fn/error docs from `///` comments → HTML + markdown | Compiler |
| `pell-lsp` | LSP server (diagnostics, hover, go-to-def, rename, find-refs) | Same crate, `tower-lsp` |
| `pell` tree-sitter grammar | Editor highlighting + structural navigation | tree-sitter |
| `pell.toml` | Manifest (name, modules, db connection profiles, deps) | toml |
| `pell.lock` | Resolved dependency versions + content hashes | toml |

Conspicuously **deferred** (and we name them so the deferral is a decision,
not an oversight):

- **Coverage** — instrumenting the lowered PL/SQL is feasible (insert
  per-statement counter increments into an autonomous tx) but the
  cost/value at v1 is bad. `pell test --coverage` is v1.1.
- **Configurable lint rules** — `@allow(rule)` / `@deny(rule)` scopes exist
  (§9.5), but v1 ships only the built-in rule set; no external rule
  plugins. A `[lints]` table in `pell.toml` flips severity.
- **Watch mode** — `pell build --watch` and `pell test --watch` are nice but
  not load-bearing for v1. `pell-lsp` covers the inner-loop case.
- **REPL** — out. PL/SQL has no meaningful REPL story; sqlcl is the answer.

### 10.1 `pell deploy` — idempotency model

The single hardest thing to get right. Plain "execute `deploy.sql`" is not
acceptable because packages have order dependencies (specs before bodies,
cross-package types), and re-running on partial failure must be safe.

v1 model — borrowed from Flyway/Liquibase but scoped to packages, types,
and triggers (per §11 (4), DDL stays out):

1. **State table**, owned by `pell` in the target schema:
   `pell_deploy_state(artifact_name, kind, content_hash, applied_at, applied_by)`.
   Created on first deploy.
2. **Plan phase** (offline): compare `deploy.lock.json` against
   `pell_deploy_state` to compute the set of artifacts whose hash changed.
   Spec changes drag their body and any downstream package whose interface
   hash they touch. `pell deploy --plan` prints the plan; CI gates on it.
3. **Apply phase**: for each changed artifact, in dependency order:
   specs first (all of them), then bodies. Each `CREATE OR REPLACE` is
   wrapped in an anonymous block that updates `pell_deploy_state` on
   success. PL/SQL package replacement is **not** transactional across
   multiple packages — we accept that and make re-runs idempotent instead.
4. **Rollback**: no automatic rollback; on failure, deploy halts and the
   state table reflects what landed. `pell deploy --resume` continues
   where it stopped. `pell deploy --to <git-ref>` rebuilds at a prior
   commit and re-applies (the "rollback" you actually want).

Liquibase/Flyway are not adopted directly because their changeset model
fights `CREATE OR REPLACE` — package bodies aren't migrations, they're
artifacts. The state table is `pell`-shaped on purpose; emitting a
Liquibase-changelog adapter is a v2 idea if there's demand.

### 10.2 `@test(db)` — execution model

- **Connection**: resolved from a `[test]` profile in `pell.toml`
  (`url`, `user`, `password_env`). `pell test` requires the profile to
  point at a *non-production* DB; the harness refuses if the schema's
  `pell_deploy_state.applied_by` is unset (i.e., it doesn't look like a
  pell-managed sandbox). Override with `--i-know-what-im-doing`.
- **Isolation**: each `@test(db)` runs inside a savepoint that's rolled
  back on completion, success or failure. Tests that issue `commit`
  (e.g., to exercise `@autonomous` paths) must declare `@test(db, commits)`,
  which switches to a per-test schema-truncate strategy and disables
  parallelism for those tests.
- **Parallelism**: pure-`pell` tests run on the host's CPU pool. `@test(db)`
  tests serialize by default in v1; opt into parallel pools by
  configuring `[test] db_pool = N` with N pre-provisioned connections.
- **Fixtures**: `@fixture fn seed_employees() { … }` declares a setup
  callable, attached to tests via `@test(db, fixture = seed_employees)`.
  Fixtures run inside the same savepoint as the test.

### 10.3 LSP and editor reality

`pell-lsp` is the editor integration surface. We ship official thin
wrappers for VS Code (a Code extension), Neovim (`lspconfig` entry), and
Helix (`languages.toml`). For **JetBrains** (DataGrip, IntelliJ): the
generic LSP plugin works for diagnostics/hover/go-to-def but does *not*
get inline SQL completion against the user's connected schema — that
requires DataGrip's native API and is out of v1. We document this
explicitly so DataGrip users aren't surprised. A native JetBrains plugin
is a v1.1 candidate if there's user demand.

### 10.4 Package manager — v1 scope

- **Manifest** (`pell.toml`):
  ```toml
  [package]
  name = "hr"
  version = "0.4.1"
  pell  = "^0.1"        # required compiler version

  [modules]
  root = "src"

  [dependencies]
  audit = { path = "../audit" }
  # registry deps deferred; format reserved:
  # logging = "^1.2"

  [deploy.dev]
  url = "jdbc:oracle:thin:@//localhost:1521/XEPDB1"
  user = "hr_dev"
  password_env = "HR_DEV_PW"

  [test]
  profile = "dev"
  db_pool = 4
  ```
- **Lockfile** (`pell.lock`): records resolved versions and content hashes
  for every direct and transitive dep. Path-only deps still write a hash;
  the lockfile detects drift between checkouts.
- **Versioning**: SemVer. Compiler version (`pell = "..."`) is part of the
  manifest; `pell build` refuses if the installed compiler is outside the
  declared range, with a one-line suggestion of the right `pellup` command.
- **Stdlib upgrades**: the prelude (`Option`, `Result`, `list`/`map`/`set`,
  `oracle::*`, `log::*`) is versioned with the compiler. v1 ships zero
  stdlib breaking changes; from v1.1 on, deprecations go through one minor
  before removal.
- **Out of v1**: registries (no `pell publish`), workspaces (multi-package
  monorepos beyond `path = "..."`), feature flags, build scripts.

Implementation-language pick: **Rust**, because it gives us a single static
binary, fast parsing, easy LSP via `tower-lsp`, and good string-manipulation
ergonomics. Go is a close second. Python is rejected for distribution reasons.

### 10.5 LSP capabilities (v1)

The server is a single binary, `pell-lsp`, sharing the parser/typer crate
with `pell build`. v1 ships:

- **Diagnostics** — push-based; whole-file and incremental on save. Diagnostics
  carry exact source ranges, including ranges *inside* `sql!{}` bodies
  (see §4.5.2). Severity: error/warning/info/hint.
- **Hover** — type of the symbol under the cursor; for fns, the full signature
  with declared error variants; for `:bind` tokens, the resolved `pell`
  binding and its inferred type; surfaces `@deprecated`, `@panics`,
  `@must_use` (§9.4).
- **Go-to-definition** — including `:bind` → `let`, `into::<T>` → `record T`,
  and cross-module across `import` boundaries.
- **Find-references / document-symbols / workspace-symbols**.
- **Completion** —
  - top-level: keywords, in-scope identifiers, imported module members,
  - after `.`: methods on the receiver's type, filtered by return type when
    used in `?` / `match` position,
  - after `@`: legal annotations for the target item (§9),
  - inside `sql!{}`: SQL keywords + schema-aware tables/columns + in-scope
    `:bind` candidates (see §4.5.2),
  - after `Err(`: in-scope error variants.
- **Signature help** — including bind-parameter help inside `sql!{}` (lists the
  binds the SQL refers to and what `pell` value each resolves to).
- **Semantic tokens** — full token classification including a dedicated token
  type for `sql!{}` body, `:bind` references, annotation names, error
  variants, and `unsafe` regions. Range-based delta is a v2 nice-to-have.
- **Inlay hints** —
  - inferred `let` types: `let row = …` → `: Option<{id: number, name: text}>`,
  - bind types inside `sql!{}`: `:status` → `text`,
  - inferred error union on `?`: shows the variant being propagated when the
    enclosing fn declares a union,
  - generated-name hints on the lowered side are *not* surfaced (they're
    backend noise).
- **Code actions** (v1 set, deliberately small):
  - `Extract .expect(msg) to invariant constant` — pulls the message into a
    named `pub const` for searchability and reuse.
  - `Convert .first() + match Some/None to .one() + ?` — when the enclosing
    fn declares `NotFound` (or can have it added with a follow-up action).
  - `Lift WHEN OTHERS THEN log; RAISE pattern to finally { log; }` — applied
    at function boundary; only offered when the existing handler does not
    transform the error.
  - `Add error variant to fn signature` — quick-fix for "error not declared".
  - `Add missing match arm` — for non-exhaustive `match` on a closed sum.
  - `Add binding for :bind` — quick-fix when a bind has no `let` in scope.
- **Formatting / range-formatting** — `pell fmt` exposed via LSP.
- **Rename** — across module boundaries. The compiler owns the source→generated
  name map (e.g. `module hr.employees` → `hr_employees`), and rename operates
  on the `pell` AST only; the package mangling re-derives. Renames of
  identifiers used as `:bind` inside `sql!{}` propagate to the bind sites.
  Renames of SQL identifiers (tables, columns) are out of v1.
- **Document links** — `import std::log` → the module's source file; module
  references in `@result_cache(relies_on = [hr.employees])` are clickable.

Not in v1: call hierarchy, type hierarchy, monikers, semantic-token delta,
DAP integration (see §8). Parked behind real demand from M4 dogfooding.

### 10.6 Parser error recovery

A real IDE buffer is unparseable most of the time. The parser is designed
around that:

- A **statement-level recovery boundary**: on a parse error inside a fn body,
  the parser skips to the next `;`, `}`, or top-level item keyword (`fn`,
  `record`, `error`, `module`, `import`, `@`) and resumes. This keeps the
  rest of the file analyzable for completion and hover.
- **Resilient AST nodes**: every block, expression, and arg list is allowed
  to be incomplete in the AST (the typer treats missing children as
  `<error>` of `Unknown` type, which suppresses cascading errors).
- `sql!{}` bodies parse with their own resilient SQL grammar; an unclosed
  identifier or trailing comma doesn't take down the outer `pell` parse.

### 10.7 Tree-sitter grammar — disambiguations

`pell` has several places where the same token sequence could mean two things.
The tree-sitter grammar (and the hand-written parser) need an explicit story
for each; flagging here so we don't discover them in M0:

| Construct | Ambiguity | Resolution |
|---|---|---|
| `E1 \| E2` in a fn return vs `Pat1 \| Pat2` in a match arm | `\|` is both error-union and or-pattern | Position-based: only an error-union after `Result<T,` or in an `error` decl context; only an or-pattern inside a `match` arm LHS. Grammar uses separate non-terminals. |
| `T?` (nullable type) vs `expr?` (propagation) | Trailing `?` is both | Type-context vs expr-context disambiguates; never overlaps in practice. Grammar uses two distinct production rules; reject `T?` in expr position with a targeted error. |
| `sql!{ … }` body | Outer parser must not try to parse SQL | Treat `sql!` as a macro-like prefix; the `{` after `sql!` opens a *raw-block* token that the SQL injection consumes. |
| `:name` bind | Easily confused with a label or a type ascription | Only valid inside `sql!{}` body. Outside, `:` is type ascription only; the lexer can hint based on the enclosing scope. |
| `@name` vs decorator on an expression | Annotations are item/stmt/block only (§9.6), not expression | Grammar production for annotation is fixed to item/stmt/block parents; rejects mid-expression. |
| `for x in 1..=n` | Range vs two consecutive expressions | `..` and `..=` are explicit range operators; grammar treats them as binary infix. |
| `record User { … }` literal vs block | `{` after a type name is either struct-literal or a block | After a bare type name in expression position, treat `{` as struct-literal; in statement position after `if`/`for`/`while`/etc., it's a block. Same rule Rust uses; users learn it once. |

The grammar ships with **injection queries**: `sql!{}` body → SQL grammar, and
interpolated string fragments (`"{name}"`) → `pell` expression grammar.
Both are tested as part of the M0 deliverable.

## 11. Open questions

1. **Error-payload lowering**: (A) vs (B) in §6.6. Need a prototype to compare
   stack-trace quality and cross-package behavior.
2. **SQL parsing depth**: do we parse SQL inside `sql!{}` ourselves (to catch
   bind-var typos and unknown columns at compile time), or treat it as an
   opaque string and rely on the DB to validate at deploy time? *Bias*: parse
   enough to extract binds and column names; let the DB validate semantics.
3. **Generics**: any in v1, or none? Leaning **none for user code**, because
   PL/SQL has no parametric polymorphism and monomorphizing across an open
   type universe is painful. Library code (`Option`, `Result`, `list`,
   `map`, `set`) is compiler-intrinsic: each used instantiation is
   monomorphized into a uniquely named PL/SQL type at emit time, with the
   set of instantiations closed over what the whole project actually uses.
   Methods on these intrinsics (`xs.first()`, `opt.expect(…)`, `r?`) are
   typed and lowered by the compiler per instantiation — this is *not*
   evidence of a general generic-method facility, and any future v2
   generics proposal needs its own design pass (especially: how to keep
   PL/SQL type-name explosion bounded when `pub fn foo<T>` can be
   instantiated by downstream packages).
4. **DDL**: in scope or out? *Bias*: out for v1. Tables/indexes/sequences stay
   in plain `.sql`. `pell` only generates packages, types, and triggers.
5. **Triggers**: support as a first-class construct (`trigger on employees
   before update { … }`) or out? *Bias*: support, because trigger ergonomics
   in PL/SQL are awful and this is high-value.
6. **Testing model**: assertion-style (`assert eq(...)`) plus fixtures? Or
   property-based? *Bias*: assertion + fixtures for v1.
7. **Concurrency / autonomous transactions**: explicit `autonomous fn`? Or
   block-level `autonomous { … }`? *Bias*: block-level.
8. **Compile-time SQL validation**: tiered.
   - *v1 (always-on)*: ship an offline SQL parser (lex + parse only — no
     semantics) sufficient to extract bind variables, detect obvious typos
     (`slect`, missing `from`), and reject use of constructs we refuse to
     lower (e.g. ref cursors outside `unsafe::cursor!{}`). Yes, we are
     parsing Oracle SQL ourselves; we restrict to a documented subset and
     pass unrecognized constructs through as opaque text with a warning.
     Aim for "catches 80% of finger-trouble bugs at edit time" via the LSP.
   - *v1.1*: `pell check --db` issues `DBMS_SQL.PARSE` (cheaper than
     `EXPLAIN PLAN`, doesn't touch the cost-based optimizer) to validate
     against a live schema. Opt-in, runs in CI.
   - *v2*: type-resolve columns, validate bind types, surface plan hints.

## 12. v1 milestones

Suggested ordering, each ~self-contained. **Note on order**: packaging
(manifest + deploy) lands *before* IDE polish, because a usable LSP is
worth little without a way to organize a multi-module project, and
because `pell deploy`'s state model influences source-map hashing (§8).

1. **M0 — grammar + parser**: tree-sitter grammar, lex + parse, no semantics.
   Deliverable: round-trip `.pell` → AST → `pell fmt` output.
2. **M1 — typer (no SQL)**: records, enums, `Result`, `Option`, functions,
   modules. Deliverable: compile a non-SQL `pell` program to a `.sql` file
   that runs and produces output.
3. **M2 — SQL embedding**: `sql!{}` blocks, iterators, binds, offline SQL
   parser for bind extraction and typo detection (§11.8 v1 tier).
   Deliverable: a working `find_employee` end-to-end against an Oracle 23
   sandbox.
4. **M3 — typed errors**: error decls, `?`, `match`, lowering strategy chosen
   between (A)/(B). Deliverable: stack-trace round-trip via source maps.
5. **M4 — packaging + deploy**: `pell.toml`, `pell.lock`, dependencies
   (paths first, registries later), `pell deploy` with the state-table
   model from §10.1. Deliverable: idempotent re-deploy of a two-module
   project; `pell deploy --plan` output gating CI.
6. **M5 — tooling polish**: `pell test` (incl. `@test(db)` with savepoint
   isolation, §10.2), `pell doc`, `pell-lsp` (diagnostics, hover,
   go-to-def, rename, find-refs). Deliverable: usable VS Code + Neovim
   extensions wrapping the LSP. JetBrains gets the generic LSP plugin
   path; a native plugin is post-v1 (§10.3).

Cut lines, in priority order, if M5 slips: `pell doc` → rename → find-refs →
JetBrains LSP wrapper docs → savepoint isolation for `@test(db)` (degrade to
"run against a freshly-created throwaway schema"). Hover, go-to-def, and
diagnostics are non-negotiable for v1 to be called shipped.

## 13. Prior art worth studying

- **PRQL** — pipelined SQL frontend; relevant for §4.6.
- **EdgeQL** — schema + query DSL; relevant for §4.5 column resolution.
- **Malloy** — semantic layer; less relevant but worth a skim.
- **TypeScript → JS** — the canonical "modern surface, legacy target" project.
- **Kotlin → JVM bytecode** — for nullability and exhaustive `when`.
- **Rescript / Reason** — for OCaml-flavored sums to a hostile target.
- **Liquibase / Flyway** — for the deploy model (we don't reinvent it).

## 14. Naming

`pell` is a placeholder; naming doesn't gate anything and gets decided last.

---

## Next steps

1. Lock the syntax-style decision (Rust/Kotlin-ish vs the alternatives shown
   earlier).
2. Pick error-lowering (A) vs (B) — or commit to prototyping both in M3.
3. Start M0: tree-sitter grammar + a handful of canonical `.pell` examples
   that exercise every construct in §4.
