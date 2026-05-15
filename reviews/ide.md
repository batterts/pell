# Review: IDE / LSP / tree-sitter expert

## Verdict

`pell` is unusually well-shaped for great editor support. The structural
choices that make it pleasant to *write* — closed annotation set, closed
error unions, exhaustive `match`, expression-iterator SQL, no implicit
`WHEN OTHERS`, one collection surface per role — also happen to be the
choices that make completion finite, hover honest, and rename safe. The
single nontrivial IDE risk is `sql!{}`: every modern language that does
inline DSL (TypeScript JSX, Rust `sqlx::query!`, Kotlin string templates)
discovers that the *injection* boundary is where features go to die — bind
resolution, schema-aware completion, and diagnostic granularity all need
explicit answers before M0, not after M4. The design now has those answers.
Tree-sitter wise, the grammar has half a dozen places where the same
token sequence means two things (`|`, `?`, `{`, `:`); each is resolvable
with positional rules but they need to be specified up front so we don't
fight the GLR table later. Net: this is tractable, and the M0 grammar work
is the right place to lock the disambiguations down.

## Strong points

- **Closed annotation set (§9)** is the single biggest IDE win in the doc.
  Completion after `@` is finite, hover is stable, and "did you mean…"
  quick-fixes are trivial. No plugin surface to babysit.
- **Closed error unions (§4.3, §6.1)** make completion after `Err(` finite
  and `Add variant to fn signature` a real, deterministic quick-fix.
- **No implicit cursors (§4.5.1)** means we don't have to model cursor state
  in semantic tokens or hover — `sql!{}` is just an iterator value.
- **Exhaustive match (§4.4)** powers a "missing arm" diagnostic with
  zero ambiguity.
- **One collection surface per role (§5.1)** keeps inlay hints terse and
  honest — `list<T>`, `map<K,V>`, `set<T>` and the LSP doesn't have to
  decide whether to show "associative array of …".
- **Source maps as a documented JSON format (§8)** lets a JetBrains plugin
  consume them without re-implementing the compiler.
- **`pell` -> backend mangling lives in the compiler (§7)** which means
  renames operate on the AST only; the package name re-derives.

## Concerns

- **§4.5 / §4.5.2 — language injection** was the largest gap in the original
  draft. Bind resolution, diagnostic spans inside the brace body, and the
  "completion inside SQL knows about schema" stories all needed an explicit
  position; the doc now has one.
- **§8 — debugger story.** "Source maps land DB errors on `.pell` lines"
  is the easy 80%. Step-debugging via DAP is harder than it looks because
  Oracle's debugger surface (DBMS_DEBUG_JDWP) is not a clean target for a
  DAP shim. Pushed to v2 explicitly; v1 ships the rewriter and the schema.
- **§10 — was a single table.** It now specifies an LSP capability list
  for v1 so M4 has something to build against, plus parser error recovery
  and tree-sitter ambiguity flags so M0 has something to build against.
- **`@result_cache(relies_on = [hr.employees])` (§9.2)** — the `relies_on`
  argument is a structured list of module references but the doc didn't
  say how the LSP completes them. Should behave like an `import` target;
  flagged in open questions below.
- **`:bind` go-to-def across files.** If a `let` is mutated and then used
  in `sql!{}` later, the inlay-hint type and the GTD target are still
  unambiguous (no aliasing in `pell`), but worth a test.
- **Rename across embedded SQL.** Renaming a `pell` `let` propagates to its
  `:bind` uses; renaming a SQL column does not (and shouldn't, in v1).
  The doc now says this; users will still try it.
- **Tree-sitter `|` ambiguity** between error unions and or-patterns is the
  one I'd actually test first in M0 — both can appear inside `match` arm
  RHS positions if a future feature lets a `match` arm return a
  `Result<T, E1 | E2>`. Positional resolution holds *today*.

## Proposed LSP v1 capability list

(this is the bulleted set landed in §10.1; reproduced here for the table)

- diagnostics with exact ranges, including ranges inside `sql!{}` bodies
- hover (types, declared error variants, bind resolution,
  `@deprecated`/`@panics`/`@must_use`)
- go-to-definition (incl. `:bind` → `let`, `into::<T>` → record)
- find-references / document-symbols / workspace-symbols
- completion: top-level, post-`.`, post-`@`, inside `sql!{}`
  (schema-aware), post-`Err(`
- signature help (including bind-parameter help inside `sql!{}`)
- semantic tokens (with dedicated types for `sql!{}` body, `:bind`,
  annotation, error variant, unsafe region)
- inlay hints (inferred `let` types, bind types, propagated error variants)
- code actions: `.expect → invariant const`, `.first() + match → .one() + ?`,
  `WHEN OTHERS → finally`, `add error variant`, `add missing match arm`,
  `add binding for :bind`
- formatting + range-formatting (via `pell fmt`)
- rename across module boundaries (AST-level; SQL identifiers excluded)
- document links (imports, module refs in annotation args)

Not in v1: call hierarchy, type hierarchy, monikers, semantic-token delta,
DAP step-debugging.

## Proposed edits (made in this worktree)

- **§4.5.2 (new) — Language injection inside `sql!{}`.** Specifies the
  tree-sitter injection point, the bind-resolution model (inlay hint,
  GTD, diagnostic at the bind token's span, rename propagation), what
  completion does inside the brace body, and where the schema snapshot
  comes from. This was the single biggest missing piece — without it,
  M2's SQL embedding has no editor story.
- **§8 — DAP scope.** Adds one paragraph clarifying the source-map JSON
  is a stable schema (so JetBrains can consume it), and that real DAP
  step-debugging is v2, not v1. Avoids overpromising.
- **§9 intro — IDE-friendliness of closed annotation set.** One paragraph
  explaining that the closedness is *the* reason completion after `@` is
  finite and hover is stable. Calling this out keeps a future "let's add
  user annotations" idea from being a one-line drive-by.
- **§10.1 — LSP capabilities for v1.** Concrete bulleted list anchoring
  M4. Replaces the previous one-row table mention with something the
  implementer can check items off of.
- **§10.2 — Parser error recovery.** Names the recovery strategy
  (statement-level boundary; resilient AST nodes; SQL sub-parser
  resilience) so the parser is built for editor use from day one, not
  retrofitted at M4.
- **§10.3 — Tree-sitter disambiguations.** Six-row table of grammar
  ambiguities (`|`, `?`, `{`, `:`, `..`, `@`) with their resolution.
  Locks in the M0 grammar contract.

## Open questions for Shaun

1. **Schema snapshot source.** For `sql!{}` completion of table/column
   names, the LSP needs a schema. Three plausible sources: (a) live
   query via `pell check --db`, (b) a checked-in `schema.json` snapshot
   generated by a CLI, (c) DDL files in the project. (b) is the most
   IDE-friendly (works offline, version-controlled, fast); is that the
   v1 answer?
2. **Should `:bind` resolution support shadowing?** If a fn parameter
   `status` is shadowed by a `let status = …` inside a block, and a
   `sql!{}` uses `:status`, the inner `let` wins by lexical scope —
   but it'd be easy to write code where the reader expects the
   parameter. Diagnostic on shadowed binds used in SQL?
3. **`@result_cache(relies_on = [hr.employees])` completion.** Should
   the LSP complete *any* in-workspace module name there, or only
   modules that actually expose tables / views? Latter is harder
   because we'd have to know which `sql!{}` reads from where.
4. **Rename of SQL identifiers.** Hard "no" in v1 is the right call,
   but should the LSP at least *highlight* matching column names
   in `sql!{}` (without rename support)? That's a small win for
   navigation that costs almost nothing.
5. **Multiple `sql!{}` per fn — block-scoped binds.** A `let foo = …`
   inside one branch shouldn't be visible to `:foo` in another branch.
   Spec is "lexical", which is what we want — but worth one
   end-to-end test in M2 to confirm the LSP agrees.
6. **JetBrains: thin LSP wrapper or a real plugin?** A thin LSP wrapper
   (via the official LSP4IJ) is cheap; a "real" plugin gets us better
   inspection/quick-fix UX. v1 should ship the wrapper; the plugin is
   M5/v2 work.
