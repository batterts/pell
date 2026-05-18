# Review integration summary

Five specialist reviewers ran in parallel against `main` at commit `194955a`
on 2026-05-14. Each made surgical edits to `design.md` in its own
`review/<slug>` branch and produced a structured critique. **All five
branches have been merged into `main`.** Branches retained for reference.

## Merge log

| Commit | Merged | Summary |
|---|---|---|
| `4783628` | `review/oracle-plsql` | Factual PL/SQL corrections (clean auto-merge) |
| `bbf9b50` | `review/type-system` | Type system coherence (clean auto-merge) |
| `92b22cd` | `review/readability` | Ergonomic cuts + warnings (1 conflict in §5 — kept both sides; complementary not contradictory) |
| `0caf6f5` | `review/dx-tooling` | Toolchain reality check (clean auto-merge) |
| `123d078` | `review/ide` | Editor tractability (1 conflict in §8 — kept both source-map perspectives; renumbered §10.x to avoid clash) |

## What each reviewer contributed

### Oracle PL/SQL expert (`review/oracle-plsql`)
Real factual bug fixes plus honest constraint surfacing:
- §4.5.1 fixed cursor sketch (`%rowtype` was wrong for narrow projections; now a holder record matching the actual select list)
- §4.5/§5.1.4 `TABLE(:xs)` requires **schema-level** nested-table types; element-type allowed/disallowed enumerated
- §9.2 **`RESULT_CACHE RELIES_ON` deprecated since 11.2** — removed from `@result_cache`
- §9.2 `PRAGMA INLINE` is statement-level, not function-level — `@inline` target was wrong
- §9.2 `@udf` + `@autonomous` mutually exclusive; `@serially_reusable` requires emission in both spec and body
- §6.5.1 "uncatchable" softened to "uncatchable in pell source"; 2048-byte cap added
- §6.6 sharpened with real numbers (1000-code band, 2KB message cap, session-not-thread)
- §9.3 `@error_code` collision detection via `build/error-codes.json`

### Type system / language designer (`review/type-system`)
Soundness pass closing coherence holes:
- §5 `T?` is sugar for `Option<T>`; `T??` is a parse error; `Option<Option<T>>` has a defined two-tag lowering
- §5 error unions formalized: structural, deduplicated, sorted; widening typer-inserted
- §5 record nominal vs structural pinned; empty-union `Result<T, !> = Infallible`
- §5 no user-written generics: built-ins are intrinsic monomorphization
- §5.1.3 map key-width overflow is an invariant panic, not silent truncation
- §5.1.5 (new) `.into` derived field-by-field, no user-written impl
- §5.1.6 (new) `derive Key` length-prefixed encoding; bans `list`/`map`/`set`/`Option`/`json` fields
- §6.5 `.expect` peel-one-layer table covering `Result<Option<T>, E>` and `Option<Result<T, E>>`
- §4.3 `?`-on-Option desugar via explicit `@from_none_of` mapping
- §4.4 recompilation hazard documented (callee adds variant → caller match breaks at compile time, intentionally)

### DX / tooling architect (`review/dx-tooling`)
Toolchain reality check:
- §8 honest build-time breakpoint at ~150–200 modules + two v1 mitigations
- §8 source-map drift caveat + `deploy.lock.json` per-package hash artifact
- §10 added `pell check`, `pell doc`, `pell.lock`; explicit deferred list
- §10.1 (new) `pell deploy` idempotency model
- §10.2 (new) `@test(db)` savepoint isolation + parallelism
- §10.3 (new) JetBrains/DataGrip caveat
- §10.4 (new) package manager v1 scope with manifest sample + lockfile policy
- §11.8 tiered SQL validation: offline parser v1 → `DBMS_SQL.PARSE` v1.1 → type resolution v2
- §12 M4/M5 reordered so packaging+deploy lands before LSP polish

### Readability / skeptical user (`review/readability`)
Ergonomic pass + foot-gun warnings:
- §1 added "not a goal: more concise" — brevity vs grep-ability tradeoff
- §3 cut weakest table row (Comments); rewrote Compiler hints row
- §4.5 **cut `sql!{} with(...)` clause** — `:status` resolves from lexical scope; trailing clause was ceremony
- §4.6 honest caveat on pipeline materialization perf
- §5 surface guidance: prefer `T?`, `pell fmt` rewrites `Option<T>` outside prelude
- §5.1.1 indexed-access tradeoff prose (don't iterate with `xs[i]`)
- §6.3 promoted finally foot-gun to a real lint (`finally_error_log`) with `@allow` opt-out — catches the WHEN OTHERS → finally migration mistake
- §6.5 reordered terminators with `.one()` first as the boring default
- §14 simplified naming section
- **Surfaced 3 missing M2-blocking features** (see open questions below)

### IDE / LSP / tree-sitter (`review/ide`)
Editor tractability:
- §4.5.2 (new) `sql!{}` language injection contract: bind inlay hints, GTD, span-level diagnostics, rename propagation, tree-sitter injection
- §8 source-map JSON given stable schema; DAP scoped to v2 explicitly
- §9 intro: closed annotation set called out as deliberate IDE-friendly design
- §10.5 (new) concrete LSP v1 capability list + 6 code actions
- §10.6 (new) parser error-recovery for unparseable buffers
- §10.7 (new) tree-sitter ambiguity table (7 disambiguation points: `|`, `?`, `{`, `:`, `..`, `@`, struct-literal-vs-block)

## Conflicts encountered and how they were resolved

**§5 `T?` vs `Option<T>` framing** (readability vs type-system).
Resolved by keeping type-system's rigorous "canonical `Option<T>`, `T?` is sugar"
treatment *and* readability's "prefer `T?` in surface code, `pell fmt`
rewrites" guidance. Not contradictory — one is the type-system fact, the
other is the surface convention.

**§8 source-map and incremental compilation** (dx-tooling vs ide).
Resolved by keeping both source-map perspectives (ide's stable JSON schema
+ DAP-in-v2 framing; dx-tooling's hash-mismatch caveat + `deploy.lock.json`)
and dx-tooling's richer incremental-compilation paragraph.

**§10 subsection numbering clash** (dx-tooling §10.1–§10.4 vs ide §10.1–§10.3
with different content). Resolved by renumbering ide's three to §10.5/§10.6/§10.7.
The current sequence (deploy → test → LSP-reality → package → LSP-capabilities
→ parser → grammar) is suboptimal thematically; consider resequencing in a
follow-up if you want LSP-capabilities-then-reality adjacency.

## Open questions for Shaun (consolidated)

### Blocking M2 (readability — surfaced by a real 200-line module sketch)

These three are PL/SQL surfaces that the design doc does not currently
expose and that a real `pell` module would need on day one:

- **`RETURNING INTO`** — proposed `.returning::<T>()` on an `sql!{}` insert/update
- **`SQL%ROWCOUNT`** — proposed `.rowcount()` on the iterator
- **`SELECT … FOR UPDATE`** — locking surface

All three should be addressed before M2 lands. They affect §4.5 and likely
add a §4.5.3+.

### Type system (4)
- `Option<T>` lowering default — bare nullable slot or always tag-and-val?
- `?` on `Option`: explicit `@from_none_of` (current) vs an inferred `.ok_or`?
- Error-union widening as subtyping vs typer-inserted (subtyping was rejected; revisit?)
- Allow `Option`-bearing keys in `derive Key`?

### DX / tooling (6)
- Cost of an offline SQL parser vs reliance on DB validation
- Where does the deploy state table live (per-schema, per-project, system)?
- `@test(db, commits)` for tests that genuinely need to commit (DDL) — opt-in tag?
- JetBrains plugin priority (native vs LSP wrapper)
- Source-map JSON schema details
- Lockfile semantics for path-based deps

### IDE (5)
- Source-of-truth for schema snapshots used in `sql!{}` completion
- Bind shadowing rules across nested `sql!{}` blocks
- SQL-identifier highlighting source (introspection vs static config)
- Block-scope binds inside `sql!{}` — are `let`s after the bind visible?
- JetBrains plugin depth

(`relies_on` completion question is moot — `RELIES_ON` is deprecated.)

## Worktree cleanup (when ready)

The branches `review/<slug>` are preserved in git for reference. To clean
up the on-disk worktrees once you've stopped consulting them:

```
for slug in oracle-plsql type-system dx-tooling readability ide; do
  git worktree remove /tmp/pell-reviews/$slug
done
rmdir /tmp/pell-reviews
```

To also delete the merged branches (after the merge commits make their
content redundant):

```
for slug in oracle-plsql type-system dx-tooling readability ide; do
  git branch -d review/$slug
done
```

Individual `reviews/<role>.md` files in this directory are the original
critiques and are now under version control via the merge commits.
