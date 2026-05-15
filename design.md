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
   and `Option<T>` (a.k.a. `T?`), a `?` propagation operator, and *no implicit*
   `WHEN OTHERS THEN NULL` ever.
3. **Tooling** — formatter, LSP, test runner, and package manager from day one.
   The compiler must be usable without an Oracle install (it emits text);
   verifying generated PL/SQL against a real DB is a separate, optional step.

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
| Boolean in SQL | Available in 23 — use it | Used natively |
| Comments | `--` / `/* */` | `//` / `/* */` (kept familiar) |
| Compiler hints | `PRAGMA AUTONOMOUS_TRANSACTION;` / `PRAGMA INLINE(…)` / `PRAGMA UDF;` / `DETERMINISTIC` / `RESULT_CACHE` clauses | `@autonomous`, `@inline`, `@udf`, `@deterministic`, `@result_cache` — uniform `@name(args)` (§9) |

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
  either; for `Option`, it propagates the *current function's* declared error
  variant (`NotFound` is inferred for `find_employee` because the iterator
  helper `.first()` returns `Result<Row, NotFound>` when called on a SQL
  iterator — see §5).
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

### 4.5 SQL embedding

`sql!{ … }` is an *expression* that yields an iterator over typed rows. Bound
variables are referenced by `:name` and must resolve to in-scope `pell`
identifiers; bind types are checked against the SQL plan at compile time when
a DB connection is configured, otherwise at first run.

```pell
let active = sql! {
  select id, name from employees
  where status = :status and dept_id = :dept_id
} with (status = "ACTIVE", dept_id = my_dept);

for row in active {
  log::info(row.name);
}
```

Bulk operations:

```pell
let ids: list<number> = [1, 2, 3, 4];
sql! {
  update employees set bonus = bonus * 1.1
  where id in (select column_value from table(:ids))
};
```

Lists lower to nested table types declared at module scope and reused.

### 4.5.1 No explicit cursors

You never write `OPEN` / `FETCH` / `CLOSE` in `pell`. The `sql!{}` expression
is an iterator; consuming methods on it lower to one of:

- a cursor `FOR` loop (for streaming iteration and `.first()` / `.first_n()`),
- a `BULK COLLECT` with `FETCH FIRST n ROWS ONLY` (for `.collect()` on
  bounded result sets),
- a `count(*)` query (only for `.is_empty()` / `.count()` when no rows are
  needed).

In particular, **`.first()` does not lower to `SELECT INTO`** and never
involves `NO_DATA_FOUND`. It lowers to:

```plsql
declare
  l_result employees%rowtype;
  l_found  boolean := false;
begin
  for r in (
    select id, name from employees
    where status = :status and dept_id = :dept_id
    fetch first 1 rows only
  ) loop
    l_result := r;
    l_found  := true;
  end loop;
  -- l_found drives Option::Some vs Option::None at the call site
end;
```

No exception machinery, no implicit `SELECT INTO`, no cursor variable in
scope. The cursor `FOR` loop *is* a cursor underneath — but it's the
implicit, scoped form, not the manual `OPEN`/`FETCH`/`CLOSE` form Shaun is
allergic to. If you ever need direct access to a `sys_refcursor` for FFI to
hand-written PL/SQL, that's `unsafe::cursor!{}` and stays out of normal code.

`.one()` lowers similarly but with `fetch first 2 rows only` so we can
distinguish "exactly one" from "more than one" without a second query — see
§6.5 for the full table.

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

## 5. Type system (v1)

- Primitives: `number(p, s)`, `int`, `text`, `bool`, `date`, `timestamp`,
  `interval`, `bytes`, `json`.
- `T?` for nullable; `Option<T>` is a library alias.
- `Result<T, E>` where `E` may be a single error type or a `|` union of error
  types declared in scope.
- `record { … }` — nominal, structural conversion only via explicit `.into`.
- `enum` — closed sum with payloads (lower to integer discriminator + per-arm
  record fields, or to a JSON tagged object when stored in a column).
- `list<T>`, `map<K,V>`, `set<T>` — see §5.1 for the surface API and the
  PL/SQL collection types each one lowers to.
- No generics in v1 *except* for the built-ins above. Reconsider in v2 once we
  see real pain.
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
| `list<T>` | ordered sequence | nested table | gets a module-level `type t_T_list is table of T;` declaration; usable in SQL via `TABLE(:xs)` |
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

Lowering of indexed access deliberately returns `Option<T>` for `xs[i]`, not
a raw `T`. PL/SQL's nested-table indexing raises `SUBSCRIPT_BEYOND_COUNT` or
`SUBSCRIPT_OUTSIDE_LIMIT`; we catch those at the boundary the same way §6.5
handles `NO_DATA_FOUND` for `.first()`. If you want the panic-on-miss
semantics, write `.expect("…")`.

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
record's fields in declaration order. No `Hash` trait, no rebalancing
concerns — PL/SQL handles the hashing under the hood.

### 5.1.4 SQL bridging

The collection type matters when SQL gets involved:

- `list<T>` is a nested table and works directly: `select … from table(:xs)`.
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
  log::error("do_stuff failed");   // only logs on error? no — always logs.
}
```

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

**Lowering** (sketch — uses a nested anonymous block + a flag so the finally
body isn't duplicated, even though PL/SQL has no native `finally`):

```plsql
declare
  l_failed boolean := false;
  procedure finally_body is begin
    -- emitted finally code
  end;
begin
  begin
    -- emitted body
  exception
    when others then
      l_failed := true;
      finally_body();
      raise;
  end;
  finally_body();
end;
```

If the finally body is small (single statement, no locals), the compiler may
inline it to avoid the nested procedure.

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
| `.first()` | `Option<Row>` | `None` | `Some(r)` | `Some(first)` |
| `.one_or_none()` | `Result<Option<Row>, TooMany>` | `Ok(None)` | `Ok(Some(r))` | `Err(TooMany)` |
| `.one()` | `Result<Row, NotFound \| TooMany>` | `Err(NotFound)` | `Ok(r)` | `Err(TooMany)` |
| `.expect("…")` (chained on any of the above) | `Row` | **invariant panic** | `r` | **invariant panic** |

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

### 6.5.1 Invariant panics are uncatchable

`.expect(msg)` and the related `.unwrap()` are the "this shouldn't happen"
channel. They are **not** regular errors:

- They are not declared in any function's error signature.
- They cannot be caught by `match Err(...)` — there is no variant to match.
- `finally { }` still runs (it always runs), but it cannot stop the panic.
- They propagate to the top of the call stack and abort the request.

Lowering: a reserved error code in the `-20000..-20999` band (initial
choice: `-20001`, name `PELL_INVARIANT_VIOLATION`) raised via
`RAISE_APPLICATION_ERROR`. The compiler:

- refuses to let any user `error` declaration use that code,
- refuses to compile a `match` or `catch` that would catch it,
- includes the source location of the `.expect()` call and its message in
  the raised text, so the operator sees `pell invariant at hr/employees.pell:47: admin user must exist`.

This gives Shaun's two cases distinct, syntactically obvious forms:

- *catch and release* → use `.one()` and `?`, or `match` on the `Result`.
- *this shouldn't happen* → use `.expect("invariant: …")`.

Generalization: `.expect` / `.unwrap` are also available on `Option<T>` and
`Result<T, E>` everywhere, not just on SQL terminators. Same semantics.

### 6.6 Lowering strategy (errors)

Two candidates; we'll prototype both and pick based on stack-trace quality:

**(A) `RAISE_APPLICATION_ERROR` + JSON payload.** Each error variant gets a
stable code in the `-20000..-20999` band. Payload is a JSON string in the
message. Catching unmarshals back to the typed variant. *Pros*: one mechanism,
plays well with cross-package boundaries. *Cons*: `-20000..-20999` is small
and shared across the schema — needs coordination. Message size limits.

**(B) Sentinel `EXCEPTION` per variant + thread-local payload.** Compiler
emits a `pell_runtime` package with a stack of payload records keyed by error
identity; `raise` pushes, `catch` pops. *Pros*: no message-size limits, real
exception hierarchy. *Cons*: introduces a runtime dep, ordering across
nested calls is fiddly.

Initial bias: **(A)**, accept the code-band coordination cost, document it.

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
  rewrites that to `hr/employees.pell:23`. Caveat: maps live alongside the
  build output and key off a body hash; once a package has been re-extracted
  via `DBMS_METADATA.GET_DDL` and re-applied by hand, the hash diverges and
  we fall back to "unmapped, line N of body N". Production debugging
  therefore requires that the `build/maps/` directory matches the deployed
  artifact — `pell deploy` writes a `deploy.lock.json` recording the
  per-package hash so the CLI can refuse to map against a mismatched build.
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
| `@result_cache` | fn | `RESULT_CACHE` clause | PL/SQL function result cache. Compiler enforces: no side effects, no session state, all args primitive or `derive Key`. `@result_cache(relies_on = [hr.employees])` for dependency hints. |
| `@autonomous` | fn or `{ }` block | `PRAGMA AUTONOMOUS_TRANSACTION;` | Independent transaction context. Block form opens a nested anonymous block with the pragma — saves writing a whole helper fn just to commit independently. |
| `@inline` / `@no_inline` | fn or call site | `PRAGMA INLINE(name, 'YES'/'NO')` | Hints to the PL/SQL compiler. |
| `@udf` | fn | `PRAGMA UDF;` | Fewer SQL ↔ PL/SQL context switches when called from SQL. Mutually exclusive with some `@autonomous` settings; compiler enforces. |
| `@serially_reusable` | module | `PRAGMA SERIALLY_REUSABLE;` | Whole module is session-stateless. Compiler refuses if the module declares package-level mutable state. |
| `@restrict_references(rnds, wnds, …)` | fn | `PRAGMA RESTRICT_REFERENCES(...)` | Legacy; rarely needed past 11g. Included for old-codebase interop. |

A worked example showing why this is better than raw PL/SQL — the canonical
"cache me a lookup" pattern:

```pell
@deterministic
@result_cache(relies_on = [hr.countries])
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
  result_cache relies_on (hr_countries)
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

### 9.3 Codegen control

| Annotation | Target | Effect |
|---|---|---|
| `@error_code(N)` | `error` decl | Pin to a specific code in `-20000..-20999`. Without it, the compiler assigns deterministically from the error's fully qualified name. `-20001` (the reserved `PELL_INVARIANT_VIOLATION`, §6.5.1) cannot be claimed. |
| `@module(package_name = "X", emit_synonym = true)` | module | Override §7 defaults. |
| `@inline_finally` | `finally` block or fn | Force the inlining variant of §6.3 lowering even when the body is large. Useful for hot paths. |
| `@no_source_map` | fn | Skip source-map entries for generated/glue code. |

### 9.4 Semantic / API surface

These don't change emitted code much; they change what the type checker and
LSP do.

| Annotation | Target | Effect |
|---|---|---|
| `@deprecated("reason")` | fn, record, error | Warning at every call/use site; appears in `pell doc` output and LSP hover. |
| `@must_use` | fn | Caller must bind, `match`, or `?` the return value. Default for any fn returning `Result<…>`; opt-in elsewhere. |
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

## 11. Open questions

1. **Error-payload lowering**: (A) vs (B) in §6.6. Need a prototype to compare
   stack-trace quality and cross-package behavior.
2. **SQL parsing depth**: do we parse SQL inside `sql!{}` ourselves (to catch
   bind-var typos and unknown columns at compile time), or treat it as an
   opaque string and rely on the DB to validate at deploy time? *Bias*: parse
   enough to extract binds and column names; let the DB validate semantics.
3. **Generics**: any in v1, or none? Leaning **none**, because PL/SQL has no
   parametric polymorphism and monomorphizing across the type universe is
   painful. Library code (`Option`, `Result`, `list`) is compiler-blessed.
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

`pell` is a placeholder. Candidates:

| Name | Pros | Cons |
|---|---|---|
| **pell** | Short, unused, `.pell` extension is clean, CLI is `pell` | Means nothing; might collide later |
| **plux** | "pl" + "lux"; hints at PL/SQL ancestry | Sounds like a product |
| **oralite** | Says what it is | Tied to Oracle branding |
| **quill** | Writerly, clean | Heavily used elsewhere |
| **modus** | Modern + modus operandi | Existing Prolog-based language |

Pick last; doesn't gate anything.

---

## Next steps

Once this draft has been argued with for a round or two:

1. Lock the syntax-style decision (Rust/Kotlin-ish vs the alternatives shown
   earlier).
2. Pick error-lowering (A) vs (B) — or commit to prototyping both in M3.
3. Start M0: tree-sitter grammar + a handful of canonical `.pell` examples
   that exercise every construct in §4.

Argue with anything. The §3 table and §6 (errors) are the bits that will
shape every subsequent decision.
