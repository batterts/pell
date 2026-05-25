---
title: DX tooling
parent: Reviews
nav_order: 6
---

## Verdict

The v1 toolchain plan in §10 is directionally right but underspecified in
the places that historically kill compiler projects: deploy idempotency,
LSP performance under no-incremental-codegen, package-manager schema, and
the JetBrains story. The closed-set annotation surface in §9 is the
sharpest, most defensible part of the document — it's the thing most
likely to survive contact with real users. The milestone ordering in §12
was inverted (LSP polish before packaging), which would have produced a
nice IDE for projects nobody could organize. With the edits below the v1
target is achievable for a small team in 9–12 months.

## Strong points

- §9's closed-set annotation surface is genuinely better than the PL/SQL
  pragma grab-bag, and the typer-enforced legal combinations (e.g.
  `@deterministic` rejected on a function that writes via `sql!{}`) is
  the right kind of static guarantee.
- §9.5's `@test` / `@test(db)` split is the correct seam — a test runner
  that works offline is rare and valuable for a DB language.
- §8's source-map plan is honest about the round-trip problem, *once* you
  call out the `DBMS_METADATA.GET_DDL` hash-mismatch case (edit added).
- §10's "Rust + tower-lsp + tree-sitter" stack is the right call;
  resisting the urge to build a custom IDE is correct.
- §11 is unusually honest for an "open questions" section — most design
  docs hide their unsettled bits.

## Concerns

- **§8 incremental compilation**: "Acceptable up to a few hundred
  modules" overstates it once the LSP shares the type checker. The real
  wall is ~150–200 modules with sub-100ms hover-latency expectations.
  Edit reframes the breakpoint and proposes two mitigations short of
  full incrementality.
- **§10 missing pieces**: lockfile, `pell check`, `pell doc`, deploy
  idempotency model, JetBrains story, lint rule configuration, test
  isolation, parallelism. All addressed by added subsections §10.1–§10.4.
- **§10 `pell deploy`**: "sqlcl wrapper" is a one-liner for a problem
  that ate a year of Flyway's design budget. PL/SQL package replacement
  is not transactional across packages; re-runs on partial failure must
  be deterministic. Added a state-table model (§10.1) that's
  `pell`-shaped rather than borrowing Flyway/Liquibase wholesale.
- **§10 `@test(db)`**: connection config, savepoint isolation, parallel
  pool, fixture story all unaddressed. Added §10.2.
- **§10 JetBrains**: generic LSP plugin gives degraded experience
  against DataGrip's native completion; needed to be called out so
  Oracle-shop users aren't blindsided. Added §10.3.
- **§11 SQL validation**: "out of v1" leaves the most common bug class
  (typo in a column name) caught only at deploy. Tiered the answer in
  §11.8 — offline parser in v1, `DBMS_SQL.PARSE`-based check in v1.1.
  An offline Oracle-SQL parser is non-trivial but a *restricted-subset*
  parser is feasible and high-leverage.
- **§12 milestone order**: M4 (LSP+IDE) before M5 (packaging) is
  backwards — a usable LSP needs the project model from M5 to be more
  than a single-file demo. Swapped; deploy moves to M4.
- **Source maps**: the doc said "DB error stacks point back to .pell
  lines" without acknowledging that the deployed body can drift from the
  build (manual fix in prod, `GET_DDL` round-trip). Added the
  `deploy.lock.json` hash mechanism.

## Proposed edits (made in this worktree)

- **§8** — replaced "no incremental compilation; acceptable up to a few
  hundred modules" with an honest breakpoint (~150–200 modules with LSP
  in the loop), two v1 mitigations (parallel parse/type, hot LSP module
  graph), and a v1.1 incremental-codegen plan. Added the source-map
  drift caveat and the `deploy.lock.json` hash artifact.
- **§10 table** — added `pell check`, `pell doc`, `pell.lock`; clarified
  `pell deploy` as state-tracking, not a sqlcl wrapper.
- **§10 (new "deferred" list)** — names coverage, configurable lints,
  watch mode, REPL as explicit deferrals so they're decisions not
  oversights.
- **§10.1 (new)** — `pell deploy` state-table idempotency model: plan
  phase, apply phase, no auto-rollback, `--resume` and `--to <git-ref>`
  semantics. Explains why Flyway/Liquibase aren't adopted directly.
- **§10.2 (new)** — `@test(db)` execution model: connection config,
  savepoint isolation, `@test(db, commits)` opt-out, parallel pool,
  fixtures.
- **§10.3 (new)** — LSP and editor reality, with explicit JetBrains
  caveat (LSP plugin works for diagnostics/hover/go-to-def but not for
  DataGrip-native schema-aware SQL completion).
- **§10.4 (new)** — package manager v1 scope: manifest schema sample,
  lockfile, SemVer, compiler-version pinning, stdlib upgrade policy,
  explicit out-of-v1 list (registries, workspaces, build scripts).
- **§11.8** — tiered SQL validation: offline parser in v1,
  `DBMS_SQL.PARSE` in v1.1, full type resolution in v2. Beats "out of
  v1, on the v2 wishlist".
- **§12** — reordered M4/M5 so packaging+deploy lands before LSP polish;
  added an explicit "cut lines" priority list for when M5 slips, with
  hover/go-to-def/diagnostics marked non-negotiable.

## Open questions for Shaun

- **Offline SQL parser scope**: lex + parse Oracle SQL ourselves is a
  real commitment. Is the cost (a few months, ongoing maintenance as
  Oracle adds syntax) worth it for catching column/keyword typos at
  edit time? Alternative: ship without it in v1 and bet on `pell-lsp`'s
  speed against a sandbox DB. I biased toward "ship the parser" because
  the LSP-against-sandbox UX is bad without warm connections.
- **Deploy state-table ownership**: should `pell_deploy_state` live in
  the same schema as the deployed code (simple, but pollutes the
  user's namespace), or in a dedicated `pell_meta` schema (cleaner, but
  needs cross-schema grants)? I sketched the former; the latter may be
  more enterprise-friendly.
- **`@test(db, commits)`**: is there an Oracle 23 feature (Flashback?
  Editions?) that gives us real per-test isolation without
  serializing? I assumed no and used per-test schema-truncate as the
  fallback, which is slow.
- **Native JetBrains plugin in v1.1 vs never**: if the target audience
  is Oracle shops, "never" is wrong; if the target is greenfield
  projects, "never" is fine. Need to know who you're optimizing for.
- **Source-map format**: did you want a `.map.json` per `.sql`
  (sketched), or one whole-project map file? Per-file is more
  rebuild-friendly; whole-project is easier to ship to ops.
- **Lockfile semantics for path deps**: I had `pell.lock` track a
  content hash even for `path = "..."` deps so checkouts catch drift.
  This is `cargo`'s behavior; some teams find it noisy. Worth a
  decision before M4.