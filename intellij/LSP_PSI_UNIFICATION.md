# Pell IntelliJ — LSP + PSI Unification Plan

**Author:** PSI track agent
**Branch:** `psi-track` (worktree `~/code/plsql-psi/`)
**Status:** plan, awaiting user review
**Owns:** the IntelliJ plugin's PSI work and the LSP bridge surface
**Coordinates with:** the compiler agent (owns `compiler/pell/*.py` and
`lsp/pell_lsp/*.py`)

This document is the successor to `PSI_TRACK.md`. Where the PSI track
landed the IntelliJ-side AST, refactorings, intentions, and inspections
in isolation from the language server, this plan unifies the two so
they stop overlapping and start filling each other's gaps.

---

## 1. Current state

### 1.1 PSI side (what the PSI track shipped on this branch)

```
intellij/src/main/
  flex/Pell.flex                    JFlex lexer  (full pell port)
  grammar/Pell.bnf                  Grammar-Kit BNF  (full pell port)
  java/dev/pell/intellij/parser/    PellParserUtil  (external rules)
  kotlin/dev/pell/intellij/
    PellParserDefinition.kt         wires lexer + parser to platform
    psi/
      PellPsiElement.kt             marker interface
      PellNameIdentifier.kt         marker for *Name wrappers
      PellNamedElement.kt           PsiNameIdentifierOwner
      impl/PellPsiElementImpl.kt    default base
      impl/PellNamedElementImpl.kt  getName / setName / nameIdentifier
      PellLexerAdapter.kt           FlexAdapter wrapper
      PellPsiFactory.kt             synthesize subtrees from text
      PellSymbolScanner.kt          project-wide scan, PsiTreeUtil-based
      PellReferenceContributor.kt   PellQualifiedRef + PellTypeRef
    codeInsight/
      PellFindUsagesProvider.kt
      PellGotoSymbolContributor.kt
    refactor/
      PellNamesValidator.kt
      PellRenameProcessor.kt
      extract/PellExtractMethodHandler.kt + ExtractMethodAnalysis
      paramsToRecord/PellParamsToRecordHandler.kt + Action
      move/PellMoveSymbolAction.kt
      inline/PellInlineAction.kt
      signature/PellChangeSignatureAction.kt
    intentions/
      PellIntroduceVariableIntention.kt
      PellAssignToLocalVariableIntention.kt
      PellWrapInForLoopIntention.kt
    inspections/
      PellDiscardedReturnInspection.kt
    generate/
      PellGenerateFieldAction.kt
      PellGenerateCrudAction.kt
      PellGenerateAggregateAction.kt
    completion/
      PellCompletionContributor.kt  keywords + primitives + project syms + locals
```

### 1.2 LSP side (already shipping, owned by compiler agent)

`lsp/pell_lsp/server.py` exposes these LSP methods today:

| LSP method | Notes |
|---|---|
| `textDocument/didOpen` `didChange` `didSave` | Lifecycle |
| `textDocument/documentSymbol` | Outline |
| `textDocument/hover` | Type info on hover |
| `textDocument/definition` | Go-To-Definition |
| `textDocument/codeAction` | Quick fixes |
| `textDocument/completion` | Completion |
| `textDocument/semanticTokens/full` | Semantic colouring |

Plus diagnostics streamed via `textDocument/publishDiagnostics`.

What it does *not* expose:

- `workspace/symbol` (Go-To-Symbol across files)
- `textDocument/signatureHelp` (parameter-info popup)
- `textDocument/references` (find-usages from LSP side)
- `textDocument/prepareRename` `rename`
- `textDocument/inlayHint`
- Custom: a way for PSI to ask "what's the type of expr at offset X?"
- Custom: a way for PSI to enumerate the stdlib symbol catalog
  (`catalog::*`, `dbms_output::*`, `p`, runtime helpers)

### 1.3 The overlap pain

Today, both stacks compete in two places and miss each other in three:

**Competing:**

1. **Completion.** LSP4IJ provides one CompletionContributor, my
   PellCompletionContributor provides another. Both populate the
   lookup popup. On Ctrl-Space the user sees duplicates (or items in
   one but not the other, depending on order).

2. **Document symbol / Structure View.** LSP4IJ produces a Structure
   View from `textDocument/documentSymbol`; my Grammar-Kit parser
   produces another from the PSI tree. Both render if both are wired.

**Missing — neither side has it:**

3. **Stdlib symbol catalog visible from PSI.** The discarded-return
   inspection skips `catalog::tables()` because my PellSymbolScanner
   only sees project source. The LSP knows the type but can't push it
   to the inspection.

4. **Type-aware refactorings.** Extract Method types captured params
   as `any`. Parameters-to-Record can't fill in field types. Change
   Signature can't validate the new return type. All because the PSI
   has no type inference and doesn't ask the LSP either.

5. **Member completion after `.`** Type `e.<Ctrl-Space>` on a
   `let e: Employee = ...` — neither side suggests `id`, `name`,
   `level`. The PSI doesn't know `e`'s type; the LSP doesn't (yet)
   expose completion items keyed by an expression position.

---

## 2. Target architecture

### 2.1 Ownership split

```
                          IntelliJ
                          Platform
                              |
                  +-----------+-----------+
                  |                       |
       +----------v---------+    +--------v---------+
       |        PSI         |    |   pell-lsp       |
       |  (Kotlin, on JVM)  |    |  (Python proc)   |
       +----------+---------+    +--------+---------+
                  |                       |
                  |  PellSymbolService    |
                  |  <----- bridge ------>|
                  |                       |
                  v                       v
            structure ops          semantic ops
            - AST                  - type inference
            - lex/parse            - diagnostics
            - refactor             - effects
            - rename               - sql/jq analysis
            - navigation           - stdlib catalog
            - file-level scan      - workspace-wide query
```

**Hard rule:** every PSI consumer (refactoring, inspection,
completion, inlay hint) goes through `PellSymbolService`. Direct calls
to `PellSymbolScanner` or `PsiTreeUtil` scans become an internal
implementation detail.

**Soft rule:** when the LSP and PSI both *can* answer a query, the
faster one wins. Local-to-this-file structural questions go to PSI
(in-memory tree). Project-wide and type-keyed questions go to LSP
(maintains its own symbol table).

### 2.2 New service layer: `PellSymbolService`

A Kotlin project-level service (annotated `@Service(Service.Level.PROJECT)`)
with this surface:

```kotlin
interface PellSymbolService {
    // Identifier-keyed lookup.
    fun findSymbol(name: String): List<PellSymbolInfo>
    fun findQualified(path: String): PellSymbolInfo?

    // Type-keyed queries (asks LSP if PSI can't infer).
    fun typeOf(expr: PellExpr): PellType?
    fun fieldsOf(type: PellType): List<PellFieldInfo>
    fun methodsOf(type: PellType): List<PellSymbolInfo>

    // Module/import resolution.
    fun resolveModule(dottedName: String): PellModuleInfo?
    fun importsOf(file: PellFile): List<PellModuleInfo>

    // Stdlib catalog (cached from LSP at workspace open).
    fun stdlibSymbols(): List<PellSymbolInfo>

    // Effect tracking — does this sql! block write data?
    fun effectsOf(sqlBlock: PellSqlBlockExpr): SqlEffects
}

data class PellSymbolInfo(
    val name: String,
    val qualifiedName: String,
    val kind: SymbolKind,             // FN, RECORD, ERROR, ENUM, TYPE, ...
    val type: PellType?,              // return type for fns, the type itself for records
    val params: List<PellParamInfo>,  // empty for non-callable
    val source: SymbolSource,         // LOCAL_FILE, PROJECT, STDLIB, ORACLE
    val location: PsiLocation?,       // null for stdlib
    val isPub: Boolean,
)

sealed class PellType {
    object Number : PellType()
    object Text : PellType()
    object Bool : PellType()
    object Date : PellType()
    object Timestamp : PellType()
    object Json : PellType()
    object Bytes : PellType()
    object Unit : PellType()
    data class Named(val qualifiedName: String) : PellType()
    data class List(val element: PellType) : PellType()
    data class Optional(val inner: PellType) : PellType()
    data class Result(val ok: PellType, val err: PellType) : PellType()
    data class Generic(val base: String, val args: kotlin.collections.List<PellType>) : PellType()
    object Unknown : PellType()
}
```

The `PellSymbolService` is the only thing in the PSI track that knows
about LSP — every refactoring, inspection, and intention talks to the
service, not to LSP directly.

### 2.3 LSP extensions (compiler agent territory)

To make this work, `lsp/pell_lsp/server.py` needs these additions.
They're additive — existing LSP4IJ behaviour stays the same.

**Standard LSP additions:**

```
workspace/symbol            Project-wide GTSymbol
textDocument/signatureHelp  Parameter info popup
textDocument/references     Find usages from LSP
textDocument/prepareRename  Rename validation
textDocument/inlayHint      Inline type annotations
```

**Custom LSP additions (pell-specific):**

```
pell/symbolCatalog          { source: STDLIB|ORACLE, ... } -> [PellSymbolInfo]
                            Used by PSI to learn about catalog::*,
                            dbms_output::*, runtime helpers without
                            walking source.

pell/typeOf                 { uri, position } -> PellType
                            What type does the expression at this
                            position have? Used by Extract Method,
                            Parameters-to-Record, type-aware
                            inspections.

pell/effectsOf              { uri, sqlBlockOffset } -> SqlEffects
                            Does this sql!{} write? What tables?
                            What columns? Used by inspections that
                            flag write-from-pure-fn patterns.
```

The custom methods don't conflict with LSP-spec methods because they
live under the `pell/` namespace.

### 2.4 Stubs

Convert `PellSymbolScanner` from PSI-walk to StubIndex-backed:

```
PellFnStub        name, qualifiedName, isPub, returnTypeText
PellRecordStub    name, qualifiedName, isPub, fieldNames+types
PellErrorStub     name, qualifiedName, isPub, fieldNames+types
PellTypeStub      name, qualifiedName, isPub, caseNames
PellEnumStub      name, qualifiedName, isPub, variantNames
PellSeqStub       name, qualifiedName
PellAggregateStub name, qualifiedName, returnTypeText
PellMethodStub    name, ownerName, returnTypeText
PellModuleStub    dottedName
```

Indices:

```
PellNameIndex             keyByName(name) -> [PellNamedElement]
PellQualifiedNameIndex    keyByQName(qname) -> [PellNamedElement]
PellModuleNameIndex       keyByQName(module) -> [PellModuleDecl]
PellMethodOwnerIndex      keyByOwner(typeName) -> [PellMethodDef]
```

Wire into the generated PSI by:

1. Switching the generated `*Impl` classes from
   `ASTWrapperPsiElement` to `StubBasedPsiElementBase<StubT>`.
   Grammar-Kit supports this via `stubClass` and `mixin` annotations
   in the BNF.
2. Registering each `StubElementType` in `PellElementTypes`.
3. Implementing `StubBuilder` that walks the AST and emits stubs.
4. Wire StubIndex registration in `plugin.xml`.

Once stubs exist, `PellSymbolService.findSymbol(name)` becomes O(1)
via the StubIndex instead of O(project files) via PsiTreeUtil.

### 2.5 Type inference layer

`PellTypeInferencer` — visitor that walks PSI expressions and returns
`PellType`. Lives in `intellij/src/main/kotlin/dev/pell/intellij/types/`.

Strategy:

1. **Literals**: cheap — NUMBER → `Number`, STRING/RAWSTRING → `Text`,
   REGEX → `Text` (per the AST node), KW_TRUE/FALSE → `Bool`.
2. **Identifier**: resolve via `PellSymbolService.findSymbol`, return
   the symbol's type (for a let-bound var, the let's annotation or the
   inferred type of its initialiser).
3. **Member access** `obj.field`: infer `obj`'s type; if it's
   `Named(R)` and R is a record, look up the field via
   `PellSymbolService.fieldsOf`.
4. **Call** `callee(args)`: infer callee's symbol; return its return
   type.
5. **Binary ops**: rules per the operator (`+` on `text` → `text`; on
   `number` → `number`; etc.).
6. **`?` (try operator)**: peel one layer of `Result<T,E>` → T.
7. **`sql!{}.one()`/`.collect()`/`.first()`**: handled by LSP via
   `pell/typeOf` since the type depends on the SQL block's projection
   columns, which the LSP already understands.
8. **`if`/`match` expressions**: unify branch types; if branches
   disagree, return `Unknown`.
9. **Struct lit**: return `Named(typeName)`.

Cache: per-expression, invalidated on PSI change. Use IntelliJ's
`CachedValuesManager` keyed on the expression's PSI element.

### 2.6 Eliminating duplicate completion

Three rules:

1. **Keywords + primitives**: PSI provides. They never depend on
   semantic info and a tiny PSI list is faster than an LSP round-trip.

2. **Project + stdlib symbols**: PSI asks `PellSymbolService` which
   merges the local Stub-backed scan with the cached stdlib catalog
   from LSP. One unified list — no duplicates.

3. **Member-access completion (`obj.<Ctrl-Space>`)**: PSI infers
   `obj`'s type, then asks `PellSymbolService.fieldsOf` and
   `methodsOf`. Falls back to LSP if PSI inference returns `Unknown`.

LSP4IJ's contributor stays registered but its completion is filtered
to only contribute items the PSI hasn't already added (we'll dedupe
on `lookupString`).

---

## 3. Phase plan

Each phase ships independently. Conservative time estimates assume
sequential work with test coverage. Marketplace shipping decisions
follow `SHIPPING.md`'s phase-to-version mapping.

### Phase L0: LSP capability survey (1 day)

**Deliverable:** `intellij/LSP_CAPABILITY_MATRIX.md` listing every LSP
method currently exposed by `lsp/pell_lsp/server.py`, every method
this plan needs, and the per-method gap.

**Coordination:** read-only on `compiler/pell/` and `lsp/pell_lsp/`.

**Risk:** if the LSP server's existing methods don't return enough
info (e.g. `documentSymbol` doesn't carry type info), the gap list
grows. Surface this to the compiler agent before Phase L3.

### Phase L1: PellSymbolService facade (3 days)

**Deliverables:**

- `intellij/src/main/kotlin/dev/pell/intellij/symbols/`
  - `PellSymbolService.kt` — interface + default impl
  - `PellSymbolInfo.kt` — DTOs
  - `PellType.kt` — sealed type hierarchy
  - `PellSymbolSource.kt` — enum
- `plugin.xml` registers the project service.
- `PellSymbolScanner` becomes private — only the facade calls it.
- One refactoring (Rename, lowest risk) migrated to use the facade
  end-to-end as a smoke test.

**Risk:** the facade design has to anticipate Phase L3+ LSP additions.
Easier to iterate after Phase L0 lands the capability matrix.

### Phase L2: PSI stubs (5 days)

**Deliverables:**

- Stub classes per the §2.4 list.
- Generated PSI classes extend `StubBasedPsiElementBase`.
- StubIndex registered in plugin.xml.
- `PellSymbolService` swaps internal `PsiTreeUtil` scans for
  StubIndex lookups.
- Existing refactorings + inspections continue to pass.

**Risk:** Grammar-Kit's stub support has edge cases — generated
classes occasionally need manual mixin overrides. Plan one
day of buffer.

### Phase L3: Stdlib catalog bridge (3 days, blocks on compiler agent)

**Deliverables:**

- Compiler agent: extend `lsp/pell_lsp/server.py` with custom method
  `pell/symbolCatalog`. Returns the stdlib symbol list (catalog::*,
  dbms_output::*, runtime helpers, `p`).
- PSI side: `PellLspClient.kt` — minimal JSON-RPC client over the
  LSP4IJ-managed process.
- `PellSymbolService.stdlibSymbols()` calls the new method on
  workspace open, caches the result for the project lifetime.
- The discarded-return inspection now flags `catalog::tables()` etc.
  because the catalog tells it `catalog::tables()` returns
  `list<text>`.

**Coordination:** **blocked** on the compiler agent adding the
custom LSP method. If they decline, fall back to:

- Hard-code the stdlib catalog as a Kotlin resource (less canonical
  but unblocks PSI inspections immediately).

### Phase L4: Type inferencer (7 days)

**Deliverables:**

- `intellij/src/main/kotlin/dev/pell/intellij/types/`
  - `PellTypeInferencer.kt` — visitor + per-expr cache
  - `PellTypeContext.kt` — scope-aware type lookup
  - `PellTypeUnification.kt` — for if/match branch unification
- `PellSymbolService.typeOf(expr)` delegates here.
- Falls back to LSP's `pell/typeOf` for sql!{} terminators.
- Fixture-driven tests against every expression form in
  `compiler/examples/*.pell`.

**Coordination:** the sql!{} pathway needs the compiler agent to add
`pell/typeOf` (Phase L3 territory). Until then, sql!{} returns
`Unknown` and refactorings that touch it skip safely.

**Risk:** type inference for closure-captured variables and
match-arm bindings is fiddly. Conservative bound for unknown cases
is `Unknown`.

### Phase L5: Inspection upgrade (2 days)

**Deliverables:** new inspections that the type inferencer unlocks:

- `PellUnknownIdentifierInspection` — IDENT that doesn't resolve to
  any project + stdlib symbol → red squiggle.
- `PellTypeMismatchInspection` — assign `text` to a `number`-typed
  let, etc. → red squiggle.
- `PellUnusedLetInspection` — `let x =` followed by no read → gray
  squiggle.
- `PellMissingMatchArmInspection` — match on a sealed type missing a
  case → yellow squiggle, quick-fix "add missing arms".
- `PellDiscardedReturnInspection` upgraded: works for stdlib
  functions (catalog::tables, etc.) too.

### Phase L6: Refactoring upgrade (3 days)

**Deliverables:**

- Extract Method: captured params get their real types via
  `PellSymbolService.typeOf`, not `any`.
- Parameters-to-Record: record field types from the same source.
- Change Signature: validates the new return type by parsing it
  through the Grammar-Kit parser.
- Inline fn: type-checks the substituted body in caller context;
  refuses when the types disagree.

### Phase L7: Completion + member access + parameter info (5 days)

**Deliverables:**

- `PellCompletionContributor` rewrites to:
  - Always: keywords + primitives.
  - At type position (after `:` or `->`): types first, weighted.
  - At expression position: symbols + locals.
  - After `.`: members of the receiver type (via type inferencer
    + `PellSymbolService.fieldsOf`/`methodsOf`).
  - After `::`: items under the qualifier module.
- `PellParameterInfoHandler.kt` — implements
  `LanguageParameterInfoProvider` so call sites show parameter
  hints below the cursor (Cmd-P).
- LSP4IJ completion deduplication: `PellLspCompletionFilter` removes
  items already added by PSI.

### Phase L8: Semantic highlighting + inlay hints (3 days)

**Deliverables:**

- `PellInlayHintsProvider.kt` — shows inferred types inline
  for `let` statements without explicit annotation.
- `PellSemanticHighlightingFactory.kt` — bridges LSP's
  `textDocument/semanticTokens/full` colours into IntelliJ.
- Code lens above pub fns: "N call sites" (uses `PellSymbolService`
  for the count).

### Phase L9: Eliminate duplicates (2 days)

**Deliverables:**

- Audit every overlap from §1.3:
  - Completion: deduplication wired in Phase L7.
  - Structure View: LSP4IJ disabled for pell, PSI provides
    (faster, type-aware).
  - Hover: PSI provides type info for project symbols; LSP
    provides for stdlib + Oracle types; merged in a single hover
    panel.
- Document the final ownership matrix in
  `intellij/LSP_PSI_ARCHITECTURE.md`.

### Phase L10: Tests + docs (3 days)

**Deliverables:**

- ParsingTestCase fixtures expanded to every
  `compiler/examples/*.pell` file.
- Refactoring round-trip tests: apply each refactoring, assert the
  result re-parses and the compiler's lowered PL/SQL is equivalent.
- Inspection fixtures: one `.pell` file per inspection with expected
  squiggle ranges.
- `intellij/LSP_PSI_ARCHITECTURE.md` final form.
- `intellij/CONTRIBUTING.md` for future-track agents.

---

## 4. Coordination matrix

| What | Owner | Phase | Notes |
|---|---|---|---|
| Grammar-Kit BNF | PSI track | L2 (stub annotations) | Already shipped |
| JFlex lexer | PSI track | — | Already shipped |
| `PellSymbolService` | PSI track | L1 | New |
| Stub classes | PSI track | L2 | New |
| Type inferencer | PSI track | L4 | New |
| Refactorings | PSI track | L0, L6 | Polish only |
| Inspections | PSI track | L5 | New ones |
| Completion contributor | PSI track | L7 | Rewrite |
| Inlay hints | PSI track | L8 | New |
| `lsp/pell_lsp/server.py` core | compiler agent | — | No change |
| `pell/symbolCatalog` LSP method | compiler agent | L3 | **Blocker for Phase L3** |
| `pell/typeOf` LSP method | compiler agent | L4 | **Blocker for sql!{}-typing** |
| `pell/effectsOf` LSP method | compiler agent | L5 | Optional, enables one inspection |
| Standard `workspace/symbol` | compiler agent | L1 | Nice-to-have |
| Standard `textDocument/signatureHelp` | compiler agent | L7 | PSI can substitute its own |
| `compiler/pell/emitter.py` discard-return | compiler agent | — | The bug behind the PSI inspection |

**Three asks of the compiler agent**, in order of impact:

1. **Add `pell/symbolCatalog`.** Unblocks every inspection that needs
   to know about stdlib. Small Python method that returns a static
   list (the stdlib doesn't change at runtime).

2. **Add `pell/typeOf`.** Unblocks accurate sql!{} typing in
   refactorings and inspections. Reuses the typer the compiler already
   has — just exposes it over JSON-RPC.

3. **Fix the discarded-return bug in `emitter.py`.** This is the bug
   that prompted PellDiscardedReturnInspection. The PSI inspection is
   the *defense*; the compiler fix is the *cure*. Either reject
   `expr;` at typing time or auto-lower to `l_unused := expr;`.

Each ask is small in isolation; together they make the LSP + PSI
story complete.

---

## 5. Test strategy

The bar for shipping each phase is the same as the PSI track:

1. **Compile gate.** `./gradlew buildPlugin` succeeds.
2. **Smoke gate.** Install the produced .zip in IDEA, open
   `compiler/examples/02_employees.pell`, verify the phase's headline
   feature works.
3. **Regression gate.** Every prior phase's feature still works
   (structure view, navigation, refactoring, intentions, inspections).
4. **Round-trip gate.** Every `compiler/examples/*.pell` parses
   identically in both the Python parser and the Grammar-Kit parser
   (the PSI_TRACK.md round-trip test, expanded each phase).
5. **Per-phase fixtures.** Each phase ships fixture .pell files +
   expected outcomes (parser trees, refactoring results, inspection
   ranges).

CI runs all five on every commit. A red gate blocks the merge.

---

## 6. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Compiler agent declines custom LSP methods | Medium | High | Fall back to hard-coded stdlib catalog in Kotlin |
| Grammar-Kit stub generation has edge cases | Medium | Medium | Buffer day in Phase L2; manual mixin overrides ready |
| Type inferencer mishandles a corner | High | Low | Return `Unknown` for any expression we can't fully type; downstream code already handles it |
| LSP4IJ behaviour changes in 2026.x | Low | Medium | Pinned IntelliJ 2024.3 in build.gradle.kts; revisit when 2027 ships |
| Duplicate completion not actually dedupe-able | Low | Medium | Worst case: disable LSP4IJ completion for pell, rely on PSI alone |
| Performance regression from type cache | Low | Low | Caching keyed on PSI element; invalidated automatically |

---

## 7. Open questions for user review

These need a decision before Phase L0 starts. None block writing
this document — but execution stalls until they're resolved.

1. **Do I have authorisation to extend `lsp/pell_lsp/server.py`** if
   the compiler agent is slow? The three custom LSP methods are
   additive (existing behaviour unchanged) — but they cross into the
   `lsp/` package which has been the compiler agent's territory
   historically.

2. **Marketplace version cadence.** This plan ships across many
   versions; per `SHIPPING.md` each phase gets one. Stay on monthly
   cadence (Phase per month) or burst-release as phases complete?

3. **Do you want stubs (Phase L2) at all?** The PsiTreeUtil scan is
   working for projects ≤ 100 files. Stubs are the right long-term
   answer but they're 5 days of work for a perf-only win. Defer to a
   v1.1 if your projects stay small.

4. **Real type inference (Phase L4) — bake in PSI, or delegate to
   LSP?** PSI inferencer is faster and works offline; LSP delegation
   keeps the type system single-sourced. Tradeoff.

5. **Replace existing intentions with type-aware variants** in Phase
   L5/L6, or keep both? The existing intentions work without type
   info; the new ones will be smarter. Could ship both and let the
   smarter one win at presentation time.

---

## 8. What I'm doing while you sleep

Strictly **planning** — this document. No code changes beyond
committing the plan to the worktree.

If you'd like me to start executing autonomously, reply with a
specific phase (e.g. "do Phase L1 and L2 overnight, stop before L3
which needs the compiler agent"). The PSI track precedent shows I can
execute multi-phase work end-to-end overnight; this plan's phases are
sized to the same cadence.

Default if you don't reply: I sit on this plan, you review when you
wake up, we pick a phase together.

---

## Appendix A — File-level map

When this plan completes, the IntelliJ plugin's source tree looks
like:

```
intellij/src/main/
  flex/                            unchanged
  grammar/                         unchanged
  java/dev/pell/intellij/parser/   unchanged
  kotlin/dev/pell/intellij/
    PellParserDefinition.kt        unchanged
    PellFileType.kt                unchanged
    PellLanguage.kt                unchanged
    PellFile.kt                    unchanged
    psi/                           (PSI track — current state)
    codeInsight/                   (PSI track — current state)
    refactor/                      (PSI track — polished in L6)
    intentions/                    (PSI track — type-aware in L6)
    inspections/                   (PSI track — expanded in L5)
    generate/                      (PSI track — unchanged)
    completion/                    (rewritten in L7)
    symbols/                       NEW (L1)
    types/                         NEW (L4)
    lsp/                           NEW (L3) — JSON-RPC client + DTOs
    stubs/                         NEW (L2) — stub classes + indices
    inlay/                         NEW (L8)
    parameterInfo/                 NEW (L7)
```

## Appendix B — Estimated total

| Phase | Days |
|---|---|
| L0 Capability survey | 1 |
| L1 PellSymbolService | 3 |
| L2 Stubs | 5 |
| L3 Stdlib catalog bridge | 3 |
| L4 Type inferencer | 7 |
| L5 Inspections | 2 |
| L6 Refactoring upgrade | 3 |
| L7 Completion + member access + parameter info | 5 |
| L8 Semantic highlight + inlay | 3 |
| L9 Eliminate duplicates | 2 |
| L10 Tests + docs | 3 |
| **Total** | **37 days** ≈ **7–8 weeks** focused |

Compressed via overnight autonomous runs (à la the PSI track which did
8 phases in one session): probably **2–3 nights** of focused autonomous
work to land L0–L2 and L4–L7, with L3 + L5's stdlib pieces blocked on
compiler-agent coordination.
