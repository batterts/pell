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
- `T?` for nullable; **`Option<T>` is the canonical name and `T?` is sugar
  that desugars to it before type checking**. They are *the same type*,
  not two types that print the same way. Consequences:
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
  rewrites that to `hr/employees.pell:23`.
- **No incremental compilation** in v1; full project rebuild. Acceptable up to
  a few hundred modules.
- **Output layout**:
  ```
  build/
    sql/
      hr_employees.spec.sql
      hr_employees.body.sql
    maps/
      hr_employees.map.json
    deploy.sql           # ordered concat of specs first, then bodies
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
| `pell fmt` | Canonical formatting | Same as compiler |
| `pell test` | Run unit tests; pure-`pell` tests run without a DB, `@sql` tests need a connection | Compiler + tiny harness |
| `pell deploy` | Apply `build/deploy.sql` to a configured DB | sqlcl/sqlplus wrapper |
| `pell-lsp` | LSP server | Same crate as compiler |
| `pell` tree-sitter grammar | Editor highlighting + structural navigation | tree-sitter |
| `pell.toml` | Manifest (name, modules, db connection profiles, deps) | toml |

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
8. **Compile-time SQL validation**: optional `pell check --db` mode that
   actually issues `EXPLAIN PLAN` against a configured DB to validate binds
   and resolve types. Out of v1, on the v2 wishlist.

## 12. v1 milestones

Suggested ordering, each ~self-contained:

1. **M0 — grammar + parser**: tree-sitter grammar, lex + parse, no semantics.
   Deliverable: round-trip `.pell` → AST → `pell fmt` output.
2. **M1 — typer (no SQL)**: records, enums, `Result`, `Option`, functions,
   modules. Deliverable: compile a non-SQL `pell` program to a `.sql` file
   that runs and produces output.
3. **M2 — SQL embedding**: `sql!{}` blocks, iterators, binds. Deliverable: a
   working `find_employee` end-to-end against an Oracle 23 sandbox.
4. **M3 — typed errors**: error decls, `?`, `match`, lowering strategy chosen
   between (A)/(B). Deliverable: stack-trace round-trip via source maps.
5. **M4 — tooling**: `pell test`, `pell-lsp` (hover, go-to-def, diagnostics).
   Deliverable: usable VS Code + JetBrains extensions wrapping the LSP.
6. **M5 — packaging**: `pell.toml`, dependencies (paths first, registries
   later), `pell deploy`.

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
