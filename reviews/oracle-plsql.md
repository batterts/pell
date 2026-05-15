# Review: Oracle PL/SQL implementation expert

## Verdict

The lowering story is viable in spirit, with two material factual errors that
would have caused embarrassment at first PL/SQL emission (the cursor FOR loop
sketch's wrong `%ROWTYPE`, and `RESULT_CACHE RELIES_ON` being treated as a
live clause when it has been deprecated/ignored since 11.2). The error model
is conceptually sound — the §6.5 surface API for distinguishing absence vs
invariant is the strongest contribution of the doc — but the §6.6 strategy
comparison is overoptimistic about (A): the `-20000..-20999` band is 1000
codes shared schema-wide and `RAISE_APPLICATION_ERROR` is capped at 2048
bytes, both of which (A) bumps into in any non-trivial codebase. §6.3
finally lowering needs to address early `return`, nested raises, and
package-init blocks. Pragma compatibility claims in §9.2 are mostly correct
for 23ai but `PRAGMA INLINE` targets a call-site statement, not a function
definition, so the annotation surface needs to reflect that. Net: small set
of corrections, not a redesign.

## Strong points

- §6.5 / §6.5.1 distinction between `Option`/`Result`/`expect` is genuinely
  better than PL/SQL's overloaded `NO_DATA_FOUND` and worth shipping.
- §4.5.1 commitment to never emit `OPEN`/`FETCH`/`CLOSE` and to keep
  `SELECT INTO` out of the lowering avoids two well-known PL/SQL footguns.
- §9 uniform `@name` annotation surface for the pragma grab-bag is the
  right call; PL/SQL's mix of signature clauses vs body pragmas is a
  consistent source of bugs.
- §5.1.4 acknowledging that `map<K,V>` cannot be passed to SQL is honest
  and correct.
- §3 table is a good elevator pitch.

## Concerns

- **§4.5.1** the cursor FOR loop sketch declared `l_result employees%rowtype`
  while the cursor projects only `id, name` — that won't compile. The
  holder record must match the projection, not the table. Also worth adding
  the optimizer-plan note for `FETCH FIRST n` and elision when the source
  is already PK-bounded.
- **§4.5 / §5.1** the claim that `list<T>` works in `TABLE(:xs)` "for all
  element types" is too broad. SQL-visible nested tables must be **schema-
  level** (`CREATE TYPE … AS TABLE OF …`), not package-local PL/SQL
  collections, and the element type must itself be SQL-visible. Records of
  records, assoc arrays as elements, and certain interval-precision
  combinations break this.
- **§6.3** the finally lowering sketch doesn't address: early `return`
  from inside the body, a raise inside the finally body itself masking
  the original exception, package-initializer `finally`, or how
  `@autonomous` interacts with the wrapper.
- **§6.5.1** "uncatchable" overstates the property — `RAISE_APPLICATION_ERROR`
  is a normal exception that arbitrary hand-written PL/SQL can catch with
  `WHEN OTHERS` or via `PRAGMA EXCEPTION_INIT`. The property is "the pell
  compiler refuses to emit a handler", not a database-enforced guarantee.
- **§6.6(A)** the `-20000..-20999` band is 1000 codes total, shared
  schema-wide with every other tool/ORM/hand-package. For a real codebase
  this needs management; the cons bullet undersells it. Also the
  2048-byte message cap on `RAISE_APPLICATION_ERROR` is not mentioned.
- **§6.6(B)** "thread-local payload" is the wrong PL/SQL primitive — there
  are no threads; package state is session-scoped. The mechanism still
  works but the description should say session-scoped.
- **§9.2 `@result_cache(relies_on = …)`** the `RELIES_ON` clause was
  deprecated in 11.2 and is parsed-but-ignored in modern Oracle including
  23. Treating it as a live dependency hint is factually wrong.
- **§9.2 `@inline`** lowers to `PRAGMA INLINE(name, 'YES')`, which is a
  **statement-level** pragma at the *call site*, not a function-level
  decoration. The "target: fn or call site" entry is incorrect for fn.
- **§9.2 `@udf` + `@autonomous`** the note says "mutually exclusive with
  some `@autonomous` settings" — make this a hard rule, since UDF
  semantics assume participation in the caller's SQL transaction.
- **§9.2 `@serially_reusable`** must be emitted in **both** package spec
  and body; doc doesn't say so, and missing one is a common bug. Also
  worth noting that serially-reusable packages can't be referenced from
  triggers.

## Proposed edits (made in this worktree)

- **§4.5.1** fixed the `%ROWTYPE` mismatch in the `.first()` sketch by
  declaring a holder record matching the projection, added an `exit;` for
  symmetry, and added an optimizer-plan note plus an elision rule for
  PK-bounded source queries.
- **§4.5 / §5.1 (table row) / §5.1.4** corrected the nested-table claim:
  `list<T>` is only `TABLE(:xs)`-usable when the element type is
  SQL-visible and the nested-table type lives at schema scope, not
  package scope. Enumerated the disallowed element shapes (record-of-
  record, assoc-array element, etc.) and called out interval-precision.
- **§6.3** rewrote the lowering sketch to drop the redundant `l_failed`
  flag (it wasn't used) and added an explicit list of edge cases the
  compiler must handle: early `return`, secondary raises from inside the
  finally body, package-init blocks, and `@autonomous`-wrapped fns.
- **§6.5.1** softened "uncatchable" to "uncatchable in pell source",
  explained that the ORA-20001 emission is catchable in hand-written
  PL/SQL, and called out the 2048-byte `RAISE_APPLICATION_ERROR` message
  cap plus the truncation strategy.
- **§6.6** expanded both (A) and (B) with concrete sizing — 1000-code band
  and 2KB message cap for (A); reframed (B)'s "thread-local" as
  "session-scoped payload register" and noted the residual-state-cleanup
  obligation at outermost entry points and the `@autonomous` nested-frame
  requirement. Bias still (A) but flagged migration to (B) as expected.
- **§9.2** corrected the pragma table: dropped `RELIES_ON` as deprecated;
  flagged `@inline` as call-site-only and made fn-level a compile error;
  hardened `@udf` + `@autonomous` mutual exclusion; noted
  `@serially_reusable` must be emitted in both spec and body. Updated the
  worked example to drop `relies_on` and added a paragraph confirming
  `DETERMINISTIC` + `RESULT_CACHE` + `PRAGMA UDF` all coexist legally in
  23ai with the compiler-enforced incompatibilities listed.
- **§9.3** expanded the `@error_code` note to acknowledge the 1000-code
  band reality, the deterministic-assignment collision risk, and the
  registry-file collision check.

## Open questions for Shaun

- For strategy (A) error lowering, do you want the compiler to maintain a
  *project-local* code registry (one band per project, allocated up front)
  or a *schema-global* one (shared across pell projects in the same DB)?
  The latter is more conservative but requires a shared deployment step.
- Source maps for the `RAISE_APPLICATION_ERROR` message under (A) — do we
  prefix the message with a correlation id (so the bulk of the payload
  goes to a side table) when it would otherwise exceed 2KB, or hard-fail
  the compile? I'd lean correlation-id-with-side-table; it's the only
  thing that scales.
- `@result_cache` without `RELIES_ON`: the doc accepted `relies_on` as a
  "hint". Now that the clause is dead, do you want the annotation to
  accept an "intended dependencies" list anyway for `pell doc` purposes
  even though it doesn't lower to anything?
- For `list<T>` schema-level types: are you OK with the compiler emitting
  `CREATE TYPE` DDL as part of `deploy.sql`, given §11 question 4 leans
  "no DDL in v1"? Either we relax that for collection types, or we
  declare them as a build-time prerequisite the user runs by hand. I'd
  document them as compiler-generated DDL that's still considered
  "infrastructure types", not "user DDL".
- §6.5.1 `.expect` panic: do you want `finally` blocks above the panic
  site to be able to *log* the panic message (read-only access to it)
  before it propagates? Otherwise operators only see the
  `RAISE_APPLICATION_ERROR` text. Cheap to add via a runtime helper.
