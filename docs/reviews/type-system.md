---
title: Type system
parent: Reviews
nav_order: 2
---

## Verdict

The type story is *roughly* coherent but the draft hand-waves the most
load-bearing decision: whether `T?` and `Option<T>` are the same type or
two views of the same idea. The doc says "alias," then the lowerings imply
they are *not* the same (a nullable record slot is not a tagged variant),
which means the prose is wrong wherever it's checked carefully — including
the unique-rows triad in §6.5, the §5.1.1 `xs[i] -> Option<T>` story, and
anything that ever sees an `Option<Option<T>>`. Pick a story and commit:
either `T?` is sugar that desugars to `Option<T>` before type-checking
(my recommendation, and what I edited toward), *or* it's a genuinely
distinct nullable-slot type and `Option<T>` is the explicit form used
when nesting matters. The other rough edges — error-union composition,
`derive Key` for unbounded fields, `.expect` on stacked monads,
`map<text>` key-width overflow — are smaller and now spelled out.
Underlying instincts are good; the doc just needed the corner cases
nailed down before someone writes a checker against them.

## Strong points

- §6.5's "intent is encoded at the call site, not the catch site" is the
  best single design call in the doc. The `.first()` / `.one_or_none()` /
  `.one()` / `.expect()` quartet is genuinely better than what every other
  PL/SQL-targeting language has tried.
- §6.5.1's *uncatchable* invariant panics, coupled with `finally` (not
  `catch`) for log-and-rethrow, is exactly right and matches the user's
  stated preference.
- §5.1.4 forcing an explicit `.keys()`/`.values()` copy at the
  map-to-SQL boundary is the right ergonomic friction.
- §9's closed annotation set with target validation is the right move
  for a tightly-scoped v1.

## Concerns

- **§5: `T?` vs `Option<T>` identity.** Originally claimed an alias
  relationship that the rest of the doc quietly violated. `T??` was
  undefined. Now nailed down: `T?` is parse-time sugar for `Option<T>`,
  `T??` is a parse error, and the lowering distinguishes
  `Option<Option<T>>` cleanly via an `option_T(tag, val)` object type
  with an optional bare-nullable-slot optimization where provably safe.
- **§4.3 / §5: error-union structural identity.** The doc never said
  whether `A | B | A` and `B | A` are the same type, or whether
  `Result<T, A>` widens automatically to `Result<T, A | B>` at a call
  site. Now: structural, deduplicated, sorted; widening is inserted by
  the typer at the call site, not a subtype relation.
- **§4.3 `?` on `Option`.** The original parenthetical claimed
  `.first()` returns `Result<Row, NotFound>` (contradicting §5.1.1 and
  §6.5, which return `Option<Row>`). Reworked: `?` on an `Option`
  requires an explicit prelude `@from_none_of` mapping, so the
  conversion isn't implicit-from-whatever-variant-is-in-scope.
- **§4.4: exhaustive match against an open callee.** Adding an error
  variant to a callee silently turns every caller non-exhaustive on
  recompile. Documented as a deliberate hard-error (breaking change),
  with the explicit `Err(_) ->` escape hatch noted.
- **§5 `.into` was referenced but undefined.** Added §5.1.5: derived,
  field-subset, name+type matching, with `T → Option<T>` widening; no
  user-written `impl`.
- **§5.1.3 `derive Key` for nested records, lists, json.** The doc
  permitted `record` keys without saying what's encodable. Added §5.1.6:
  length-prefixed canonical encoding, type-identity-prefixed (so
  different records with identical fields don't collide), and an
  explicit ban on `list`/`map`/`set`/`Option`/`json` field types.
- **§5.1.3 `map<text>` keys > N.** Was completely undefined. Now an
  invariant panic, with rationale (truncation aliases keys, and a
  `Result<…, KeyTooLong>` on every map op would poison ergonomics).
- **§6.5 `.expect` on stacked types.** A user calling `.expect()` on a
  `Result<Option<T>, E>` would reasonably expect either `T` or a type
  error; the doc didn't say. Now: peels exactly one layer. Table added.
- **§9.4 `@must_use` and `Option<T>` / `T?`.** Result was defaulted to
  must-use; Option was unspecified. Now both default to must-use, with
  `let _ = …` as the explicit-discard form.
- **§5.1.1 `xs.first()` and generics.** §11 says "no generics in v1,"
  but `list<T>.first()` is generic on its face. Reframed as
  compiler-intrinsic monomorphization of a closed set of built-ins, with
  §11.3 updated to match — and an explicit note that adding user
  generics in v2 needs its own design pass because PL/SQL type-name
  explosion across packages is unbounded otherwise.

## Proposed edits (made in this worktree)

- **§5**: Replaced the "alias" claim with a precise rule: `T?` desugars
  to `Option<T>` before type-checking; `T??` is a parse error;
  `Option<Option<T>>` is legal and has a well-defined two-tag lowering;
  bare-nullable-slot lowering is an optional optimization. Why: every
  downstream §5/§5.1/§6.5 claim implicitly depends on this.
- **§5**: Pinned error unions as structural / deduplicated / sorted /
  not a subtype lattice; added empty-union (`Result<T, !>`). Why: the
  composition question ("A calls B and adds E2") had no documented
  answer.
- **§5**: Reframed "no generics except built-ins" as
  "built-ins are compiler-intrinsic monomorphization, not user
  generics." Why: the doc was internally contradictory.
- **§4.3**: Replaced the muddled `Option`-via-`?` parenthetical with an
  explicit prelude `@from_none_of(…)` rule. Why: implicit
  None-to-variant conversion based on what's-in-scope is a sharp edge.
- **§4.4**: Documented the exhaustive-match-vs-open-union hazard as
  intentional, with the escape hatch. Why: future callers will hit
  this and need to know it's not a bug.
- **§5.1.3**: Defined `map<text>` key-overflow as an invariant panic,
  not silent truncation. Why: previously undefined; truncation aliases
  keys, which defeats the whole point.
- **§5.1.5 (new)**: Defined `.into` concretely. Why: referenced but
  never specified.
- **§5.1.6 (new)**: Defined `derive Key` encoding, type-identity prefix,
  and the ban on unbounded field types. Why: previously hand-wavy;
  silent collisions are the worst possible failure mode for a map key.
- **§6.5**: Added the `.expect` peel-one-layer table covering
  `Result<Option<T>, E>` and `Option<Result<T, E>>`. Why: the most
  common stacked-monad case was undefined.
- **§9.4**: Extended `@must_use` default to `Option<T>` and `T?`. Why:
  the asymmetry with `Result` was unjustified.
- **§11.3**: Clarified that built-ins are intrinsic, not evidence of
  generics, and named the cross-package type-name explosion problem
  any v2 proposal must solve. Why: keeps the v2 conversation honest.

## Open questions for Shaun

- **`Option<T>` lowering shape.** I documented an `option_T(tag, val)`
  object type with an optimization to a bare-nullable slot when
  provably safe. Picking the *opposite* default — always lower to a
  bare nullable slot, and only promote to the object form when nesting
  is detected — gives smaller emitted SQL and friendlier `%TYPE`
  interop but makes `Option<Option<T>>` a special case. Strong
  preference?
- **`?` on `Option<T>`.** The `@from_none_of` rule is the cleanest
  story I could find, but it does mean the prelude has to enumerate
  every `Option`-bearing iterator method with its associated error
  variant. Alternative: forbid `?` on `Option<T>` outright, force
  callers to write `.ok_or(NotFound{…})?`. More verbose, no magic.
  Which?
- **Error-union widening.** I declared this typer-inserted at the call
  site, not a subtype relation. The subtype-relation version is simpler
  in a paper but causes inference surprises — calling
  `fn id<E>(r: Result<T, E>) -> Result<T, E>` on a `Result<T, A>` value
  inside a `Result<T, A | B>` context infers `E = A` or `E = A | B`?
  Worth a sentence in §5 if you want to pin it now, or leave to M1.
- **`derive Key` for records containing `Option<T>` fields.** I banned
  them. Real code (a `User` with an optional middle name used as a map
  key) might want this. Allowing it requires picking a `None`-sentinel
  byte that can't appear in any encoded value — doable but worth
  thinking about before M1 lands.
- **§4.5 `sql!{}` row type.** The doc treats `Row` as if it's a
  structural anonymous record, but §5 says all records are nominal.
  Is `Row` a third category? Or does each `sql!{}` synthesize a fresh
  nominal record at compile time? I didn't touch this because it
  arguably belongs to a SQL-embedding reviewer, but flagging it.