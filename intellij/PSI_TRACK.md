# Pell PSI Track — Planning + Coordination

**Goal:** ship the IntelliJ refactoring features that need real PSI —
Extract Method, Parameters → Record, Move to Package, cross-file
Rename, Inline, Change Signature — plus the better autocomplete that
falls out of having a real PSI tree.

**Owner:** PSI track agent (this work).

**Out-of-scope for this track:** the Python compiler (`compiler/pell/`),
language features, examples, docs, anything CLI-side. That's the other
agent's territory.

---

## Why a track doc

There are multiple agents working in this repo concurrently. The
compiler agent is iterating on `compiler/pell/*.py` (lexer, parser,
emitter, jq, driver) and on `compiler/examples/`. The PSI track lives
entirely under `intellij/src/main/` in directories the compiler agent
has no reason to touch.

If a merge ever happens, the conflict surface is one file
(`intellij/build.gradle.kts`) and one file
(`intellij/src/main/resources/META-INF/plugin.xml`). Everything else is
new, disjoint, additive.

---

## Coordination rules

### Files this track will create (all new — no merge risk)

```
intellij/src/main/flex/Pell.flex                       JFlex lexer spec
intellij/src/main/grammar/Pell.bnf                     Grammar-Kit grammar
intellij/src/main/java/dev/pell/intellij/parser/       Generated lexer + parser (DON'T HAND-EDIT)
intellij/src/main/kotlin/dev/pell/intellij/psi/        PSI element classes + mixins
intellij/src/main/kotlin/dev/pell/intellij/refactor/   Extract, Move, Rename, Inline, Change Sig
intellij/src/main/kotlin/dev/pell/intellij/completion/ Completion contributors backed by PSI
intellij/src/main/kotlin/dev/pell/intellij/codeInsight/ Find Usages, Go To Symbol, Structure View
intellij/src/test/kotlin/dev/pell/intellij/...         Tests for everything above
intellij/PSI_TRACK.md                                  This document
```

### Files this track will modify (small, marked additions)

```
intellij/build.gradle.kts                              + Grammar-Kit plugin + JFlex plugin
intellij/src/main/resources/META-INF/plugin.xml        + new <psi.*>, <refactoring.*>, <completion.*> entries
intellij/src/main/kotlin/dev/pell/intellij/PellParserDefinition.kt
                                                       Replace trivial impl with the
                                                       generated parser. ~30 lines changed.
```

Any change in these files will be in a marked block:

```kotlin
// >>> PSI TRACK >>>
... my changes ...
// <<< PSI TRACK <<<
```

So merges with the compiler agent's possible (unlikely) edits to these
files stay surgical.

### Files this track WILL NOT touch

- Anything under `compiler/`
- Anything under `docs/`
- Anything under `runtime/`
- Anything under `lsp/`
- `intellij/src/main/kotlin/dev/pell/intellij/{preview,run,toolwindow,settings,newproject}/`
  — those are owned by the existing plugin features.
- `intellij/src/main/kotlin/dev/pell/intellij/{PellFileType,PellLanguage}.kt`
  — stable, no changes needed.

### Files the compiler agent owns; this track will read only

- `compiler/pell/parser.py` and `compiler/pell/lexer.py` are the
  canonical grammar reference. I'll port the surface they accept to
  the Grammar-Kit BNF. **Round-trip test**: every file in
  `compiler/examples/*.pell` must parse without error in both
  parsers. Drift between them is the project's main long-term risk;
  the test suite is the early-warning system.

---

## The fork: do we duplicate the parser?

We do. The Kotlin-side parser exists *only* for IDE features — it
never generates code, never gets called by the CLI. The Python parser
remains the source of truth for compilation. This is the same pattern
the JetBrains Rust, Python, Go, and Kotlin plugins use, for the same
reason: the IDE platform speaks JVM, the canonical compiler doesn't,
and porting the compiler costs more than maintaining two parsers in
parallel.

**Drift mitigation:** a test suite that parses every example file
through both parsers and asserts equivalent surface (node counts per
production, identifier sets per scope). Lives at
`intellij/src/test/kotlin/dev/pell/intellij/parser/RoundTripTest.kt`.
Runs in CI. A new pell language feature that the compiler agent ships
should produce a new example file; the round-trip test then either
passes (we already accept it) or fails (we need to extend the grammar
this side).

---

## Phased plan

Each phase is a self-contained chunk that can ship to the Marketplace
on its own. The version bumps after each phase let users see progress.

### Phase 0 — Bootstrap (Day 1–2)

Set up the Grammar-Kit toolchain. Smoke test with a trivial grammar.

- Add Grammar-Kit + JFlex Gradle plugins to `build.gradle.kts`.
- Create the directory skeleton.
- Verify `./gradlew generateLexer generateParser` runs.
- Plumb the generated outputs into the Java source set so they
  compile alongside the Kotlin.

**Definition of done:** a trivial 5-token grammar parses a trivial
test file. No platform-side wiring yet.

**Ship target:** none — this is internal scaffolding.

### Phase 1 — Lexer (Day 3–5)

Port `compiler/pell/lexer.py` to JFlex. The pell lexer is small —
keywords + identifiers + literals (string, raw string, regex, number)
+ punctuation + `sql!{}` / `jq!{}` raw blocks + comments.

Tricky bits to get right:
- **Regex literal disambiguation** (after value tokens it's division,
  otherwise it's `/regex/`). JFlex supports state-based lexing, which
  fits cleanly. Mirror `_VALUE_TOKEN_KINDS` from the Python lexer as
  a state transition table.
- **`sql!{...}` / `jq!{...}` block raw text capture** with
  brace-tracking that respects single-quoted strings + line comments.
- **String interpolation tokens** for `{name}` placeholders inside
  `"..."` strings.

**Definition of done:**
- `./gradlew test --tests "...PellLexerTest"` passes with at least one
  expected-token sequence test per non-trivial example file.
- Open a `.pell` file in IDEA via the runIde task → see token-level
  highlighting (keywords blue, strings green, comments grey) without
  any LSP4IJ involvement.

**Ship target:** can ship as a "PSI preview" if highlighting noticeably
improves over LSP-only, but probably bundles with Phase 2.

### Phase 2 — Grammar + Parser (Week 2)

Define `Pell.bnf`. Grammar-Kit emits the Java parser + PSI interface
hierarchy. Wire into `PellParserDefinition`.

The grammar is ~300–500 lines. Use `compiler/pell/parser.py` as
spec — every production there has a one-to-one Grammar-Kit rule. Map
the AST node names directly: `PellFnDef`, `PellRecordDef`,
`PellModuleDef`, `PellSqlBlock`, `PellJqBlock`, `PellMatchExpr`, etc.

**Definition of done:**
- Round-trip test: every `compiler/examples/*.pell` parses without
  error.
- Project-tree Structure panel (Cmd-7) shows the module's fns,
  records, errors as separate entries — derived from PSI, not LSP.

**Ship target:** v0.4.0. "Real PSI parser." Note in change-notes that
LSP4IJ is now redundant for structure view; it stays for diagnostics
+ hover.

### Phase 3 — PSI Implementation + Stubs (Week 3–4)

Grammar-Kit gives us generated PSI interfaces with no behavior. Now
add Kotlin mixin classes implementing `PsiNameIdentifierOwner`,
`PsiNamedElement`, etc.:

- `PellFnDef`: `getName()`, `setName()`, `getNameIdentifier()`,
  parameter list accessor, return-type accessor, body accessor.
- `PellRecordDef`: name + field list, with each field
  `PellNameIdentifierOwner` too.
- `PellModuleDef`: module name string.
- `PellQualifiedRef`: the `foo::bar::baz` references that resolution
  hangs off of.

Then add Stub Elements for everything cross-file-visible (`pub fn`,
`pub record`, `pub error`, `pub type`, `pub aggregate`, `pub enum`,
`pub seq`). Stubs let IntelliJ index the whole project without
parsing every file every time; mandatory for any cross-file feature
that scales.

- `PellFnStub`, `PellRecordStub`, etc. — minimal: name + qualified
  module path + a few flags (`is_pub`, `is_unsafe`).
- `PellStubIndex` exposing two indices: by name (for "Go To Symbol")
  and by qualified name (for resolution).
- `PellStubBuilder` reading the AST → producing stubs.

**Definition of done:**
- Cmd-O → "Go To Symbol" shows all `pub fn` / `pub record` in the
  project, navigates correctly.
- Cmd-Click on an identifier in the same file navigates to its
  declaration.

**Ship target:** v0.5.0. "Navigation across the project." Big visible
win.

### Phase 4 — Reference Resolution Across Files (Week 5)

Cross-file references: `hr::employees::Employee` in one file resolves
to the `pub record Employee` in `hr/employees.pell`.

- `PellReference` for identifier nodes — implements
  `PsiPolyVariantReference` so we can return multiple candidates
  (e.g., overloaded fns) and let IntelliJ disambiguate.
- `PellQualifiedNameResolver` — walks `module foo.bar;` declarations
  + `import` statements + the stub indices to resolve dotted refs.
- `FindUsagesProvider` for fn / record / module → enables Cmd-G
  ("Find Usages").

**Definition of done:**
- Find Usages on `pub fn promote` highlights every call site across
  every file.
- Cmd-Click on `hr::employees::Employee` in module `audit.charges`
  jumps to `hr/employees.pell`.
- Inspections panel shows "fn 'foo' is never used" warnings for
  module-private fns with no callers.

**Ship target:** v0.6.0. "Cross-file navigation and unused-symbol
detection."

### Phase 5 — Rename (Week 6)

First refactoring. Exercises the full PSI + reference infrastructure
in a focused way. Get it right, the others are easier.

- `RenameProcessor` implementation for pell-named symbols.
- `NamesValidator` — pell identifier rules (letter/underscore start,
  no Oracle reserved word collision, no `::` inside identifier).
- `RenameInputValidator` for record fields, fn params, etc.

**Definition of done:**
- Shift-F6 on `pub fn promote` updates every call site, including
  qualified references from other modules, atomically.
- "Find usages" preview window shows what's about to change.
- Test: a 5-module sample project, rename a record, every reference
  updates, project still parses.

**Ship target:** v0.7.0. "Rename across the project."

### Phase 6 — Extract Method (Week 7–8)

The big one. The feature you specifically named.

- Selection analysis: what locals does the selection read? Write?
  Return? Throw? Those determine the new fn's parameters, return
  type, and whether the call site needs `let x = ...` or just `;`.
- Synthesizing the new `pub fn` (or private — UI choice). Insert it
  after the current fn.
- Replacing the selection with the call.
- Dialog: name field, public/private toggle, return-type preview.

Tricky cases:
- Selection contains `return` — the new fn returns `Result<T, E>`
  with `Err(EarlyReturn { value })` and the call site dispatches.
- Selection contains `sql!{...}.one()?` with `?` propagating — the
  new fn must also return `Result<...>` so the `?` carries through.
- Selection captures a `for n in nums { ... break; }` — break can't
  cross a fn boundary; refuse with a clear message.

Tests are mandatory and substantial — the failure mode is "subtly
wrong code that compiles" which is worse than no feature.

**Ship target:** v0.8.0. "Extract Method." This is the one users mean
when they say "I want IntelliJ to feel natural."

### Phase 7 — Parameters → Record (Week 9)

Select a fn's parameter list (or a subset) → refactor to a single
record parameter.

- Synthesize the record: `pub record <FnName>Args { <each param> }`.
- Rewrite the fn signature: one param of the new record type.
- Rewrite the body: `<param>` → `<arg>.<param>`.
- Rewrite every call site: `foo(a, b, c)` →
  `foo(FnNameArgs { p1: a, p2: b, p3: c })`.
- UI: pick record name, public/private, position in module.

**Ship target:** v0.9.0. "Extract Parameters to Record."

### Phase 8 — Move + Inline + Change Signature (Week 10+)

The remaining refactorings, parallelizable once the infrastructure is
solid. Each ships in its own minor version.

- **Move Symbol**: F6 on a `pub fn` → choose target module → file
  move + signature stays + `import` statement injected in every
  module that referenced it.
- **Inline**: opposite of Extract. Substitute call sites with body.
- **Change Signature**: dialog → modify params, modes, return type
  → propagate to all call sites with type-safe rewrites.

**Ship target:** v1.0.0. "Refactoring complete." Plugin is now a
first-class language plugin.

---

## Build infrastructure

The `build.gradle.kts` changes for Phase 0:

```kotlin
plugins {
    java
    id("org.jetbrains.kotlin.jvm") version "2.0.21"
    id("org.jetbrains.intellij.platform") version "2.2.1"
    // >>> PSI TRACK >>>
    id("org.jetbrains.grammarkit") version "2022.3.2.2"
    // <<< PSI TRACK <<<
}

// >>> PSI TRACK >>>
sourceSets {
    main {
        java.srcDirs("src/main/gen")
    }
}

tasks {
    val generateLexer = generateLexer {
        sourceFile.set(file("src/main/flex/Pell.flex"))
        targetOutputDir.set(file("src/main/gen/dev/pell/intellij/parser"))
        purgeOldFiles.set(true)
    }
    val generateParser = generateParser {
        sourceFile.set(file("src/main/grammar/Pell.bnf"))
        targetRootOutputDir.set(file("src/main/gen"))
        pathToParser.set("dev/pell/intellij/parser/PellParser.java")
        pathToPsiRoot.set("dev/pell/intellij/psi")
        purgeOldFiles.set(true)
    }
    compileKotlin {
        dependsOn(generateLexer, generateParser)
    }
    compileJava {
        dependsOn(generateLexer, generateParser)
    }
}
// <<< PSI TRACK <<<
```

`src/main/gen/` is gitignored — generated code is reproducible from
the .flex + .bnf files, no need to commit it.

The `plugin.xml` changes for Phase 2 (replacing the trivial parser):

```xml
<!-- existing -->
<lang.parserDefinition
    language="pell"
    implementationClass="dev.pell.intellij.PellParserDefinition" />

<!-- >>> PSI TRACK additions below >>> -->
<stubElementTypeHolder class="dev.pell.intellij.psi.PellStubElementTypes" />
<stubIndex implementation="dev.pell.intellij.psi.stub.PellNameIndex" />
<stubIndex implementation="dev.pell.intellij.psi.stub.PellQualifiedNameIndex" />

<lang.findUsagesProvider language="pell"
    implementationClass="dev.pell.intellij.codeInsight.PellFindUsagesProvider" />
<lang.refactoringSupport language="pell"
    implementationClass="dev.pell.intellij.refactor.PellRefactoringSupport" />
<lang.namesValidator language="pell"
    implementationClass="dev.pell.intellij.refactor.PellNamesValidator" />

<refactoring.extractMethod implementation="dev.pell.intellij.refactor.PellExtractMethodHandler" />

<completion.contributor language="pell"
    implementationClass="dev.pell.intellij.completion.PellCompletionContributor" />

<gotoSymbolContributor implementation="dev.pell.intellij.codeInsight.PellGoToSymbolContributor" />
<!-- <<< PSI TRACK additions above <<< -->
```

---

## Testing strategy

Refactorings that produce subtly wrong code are worse than no
refactoring at all — they break trust permanently. Every refactoring
ships with:

1. **Parser round-trip**: every example file in
   `compiler/examples/*.pell` parses to the same surface in both the
   Python and Kotlin parsers. Catches grammar drift.
2. **Reference resolution**: a fixture project with N modules where
   every `pub` symbol is referenced from every other module; resolve
   each, assert correct.
3. **Refactoring round-trip**: apply the refactoring, then assert the
   result both (a) parses and (b) round-trips through the Python
   compiler producing equivalent PL/SQL output. This is the strongest
   guarantee — semantically identical lowered SQL means the
   refactoring preserved behavior.
4. **Negative tests**: refactorings that *should* refuse (e.g.,
   Extract Method on code containing `break` that would cross a
   boundary) produce the right error message and leave the source
   unchanged.

CI runs the test suite on every commit. A red test blocks the merge.

---

## Realistic effort

- **Phase 0–2 (Bootstrap + Lexer + Grammar):** ~2 weeks, mostly
  mechanical.
- **Phase 3–4 (PSI + cross-file refs):** ~2 weeks, conceptually
  harder but well-trodden territory in JetBrains-land.
- **Phase 5 (Rename):** ~1 week.
- **Phase 6 (Extract Method):** ~2 weeks. The hardest single feature.
- **Phase 7 (Parameters → Record):** ~1 week.
- **Phase 8 (Move + Inline + Change Sig):** ~2 weeks combined.

Total: **8–10 weeks of focused work**. Less if Phase 0–2 go faster
than expected (likely — they're mechanical port jobs).

---

## What to do *first*

Phase 0 (Bootstrap) is short and unlocks everything else. Start
there, ship to a feature branch, get the Grammar-Kit toolchain
producing a trivial test grammar, confirm `./gradlew buildPlugin`
still produces a working plugin. Then begin Phase 1.

After Phase 0 is in, ping the compiler agent: "PSI track is live in
`intellij/src/main/{flex,grammar,kotlin/dev/pell/intellij/{psi,refactor,completion,codeInsight}}/`,
plus marked blocks in `build.gradle.kts` and `plugin.xml`. No
coordination needed beyond not editing those marked blocks."
