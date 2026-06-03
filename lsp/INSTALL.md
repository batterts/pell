# pell LSP + IntelliJ — install and smoke-test

Two paths. Pick the one that matches your patience.

## What's in the box

- **`lsp/`** — `pell-lsp`, the Language Server (Python, pygls 2.x). Wraps
  the existing pell parser to provide diagnostics, hover, document
  symbols, and basic completion over the Language Server Protocol.
- **`intellij/`** — `pell-intellij`, a dedicated IntelliJ plugin that
  bundles the LSP4IJ wiring so you don't have to configure it manually.
  Builds via Gradle to an installable `.zip`.
- **`intellij/pell-template.json`** — LSP4IJ "user-defined language
  server" template you can import without building the plugin.

## Path A — fastest: use LSP4IJ's user-defined-server UI

No build step. Works in any JetBrains IDE 2023.3+ (IntelliJ IDEA, DataGrip,
PyCharm, GoLand, …).

1. **Install LSP4IJ** from JetBrains Marketplace:
   - Settings → Plugins → Marketplace → search for "LSP4IJ" (by Red Hat)
   - Install and restart the IDE.

2. **Configure the pell server.** Two options:
   - **(a) Import the template.** Settings → Languages & Frameworks →
     Language Servers → "Import" → choose `intellij/pell-template.json`.
   - **(b) Add manually.** Settings → Languages & Frameworks →
     Language Servers → "+" → "New Language Server":
     - Name: `pell`
     - Command: absolute path to `lsp/run.sh` in your clone
     - Mappings → "Add File Name Patterns" → `*.pell`
     - Language ID: `pell`

3. **Open a `.pell` file.** Try `compiler/examples/02_employees.pell`. You
   should see:
   - Syntax errors as red squiggles
   - Hover on a `pub record` or `pub fn` name showing its declaration
   - "Structure" panel (Cmd-7 / Ctrl-7) showing the module's outline
   - `.` triggers a completion popup of available method names

## Path B — full plugin: build and install `pell-intellij`

Bundles the LSP4IJ wiring so end-users don't configure anything manually.

### Requirements

- JDK 17+
- LSP4IJ already installed in the target IDE (the plugin declares a
  runtime dependency on it)

### Build

```sh
cd intellij
./gradlew buildPlugin
# Output: build/distributions/pell-intellij-0.0.1.zip
```

### Install in IntelliJ

- Settings → Plugins → ⚙️ (gear icon) → "Install Plugin from Disk…"
- Pick the `.zip` from `intellij/build/distributions/`
- Restart the IDE

### Server resolution

The plugin spawns `pell-lsp` by resolving (in order):
1. `$PELL_LSP` env var (absolute path to a launcher script or binary)
2. `<project_root>/lsp/run.sh`
3. `~/.local/bin/pell-lsp` (future installable location)

If none of those are executable, the LSP4IJ status bar will show an
error and you can override the path via Settings → Languages &
Frameworks → Language Servers → pell → Command.

## Smoke test

Once installed (either path), open one of the example files:

| File | What to look for |
|---|---|
| `compiler/examples/02_employees.pell` | Hover on `Employee`, `find_employee`, `NotFound` shows record/fn/error declaration. Outline panel shows 3 items. |
| Make a typo: change `pub fn` → `pub fnn` | Red squiggle on `fnn` with the parser's error message |
| Add `import std::logger;` line | No error (imports are parsed) |
| Type `@` on a new line above a `pub fn` | Completion popup with `@deterministic`, `@result_cache`, `@udf`, `@autonomous`, etc. |
| Position cursor after `.` on `sql!{...}.` | Completion popup with `.one()`, `.first()`, `.collect()`, `.if_empty()`, `.if_many()`, etc. |

## Manual stdio test (no IntelliJ)

If you just want to verify the LSP server is working without involving the
IDE:

```sh
lsp/run.sh
# In another terminal, send LSP messages via stdin. See /tmp/lsp_smoke.py
# (committed) for a working example that does initialize → didOpen → hover →
# documentSymbol → completion → shutdown.
```

The smoke script's output should match:

```
--- initialize response: capabilities = ['completionProvider', 'definitionProvider', 'documentSymbolProvider', 'executeCommandProvider', 'hoverProvider', 'positionEncoding', 'textDocumentSync', 'workspace']
--- diagnostics (good): 0 issues
--- diagnostics (bad): 1 issues
    - expected IDENT, got ARROW ('->') @ 2:13
--- documentSymbol (good): 3 top-level symbols
    - Employee (record (2 fields))
    - NotFound (error (1 fields))
    - find (pub fn find(id: number) -> Result<Employee, NotFound>)
--- hover on 'Employee': True
--- server exit code: 0
```

## Known limitations (v0)

The LSP server is intentionally minimal. What's *not* implemented yet:

- **Cross-file go-to-definition** — works within a file only. Workspace
  indexing is the next big feature.
- **Find references** — same reason.
- **Rename refactoring** — needs cross-file edits.
- **Type-aware completion after `.`** — current completion returns a
  fixed menu of methods; doesn't filter by the receiver's actual type.
- **Schema-aware completion inside `sql!{}`** — needs the schema snapshot
  (M4) and tree-sitter language injection.
- **Semantic tokens / fine-grained highlighting** — IntelliJ's default
  highlighter colors based on plugin.xml file-type registration; we
  don't yet emit LSP semantic-token responses.
- **Code actions / quick fixes** — implemented for the
  `pell.unused-return` and `pell.unused-value` EmitError codes:
  Alt+Enter on a red squiggle offers
  *"Bind result with `let _ = `"*. Adding more is a matter of
  tagging the relevant `EmitError(..., code="pell.X")` and adding
  `pell.X` to `_QUICKFIX_CODES` in `pell_lsp/server.py`. The
  wishlist in `INSTRUMENTATION_WISHLIST.md` has six more concrete
  ideas.

## Troubleshooting

- **"Server not starting" or no diagnostics**: check that `lsp/run.sh`
  is executable and that the compiler venv exists at `compiler/.venv/`.
  Re-run `python3 -m venv compiler/.venv && compiler/.venv/bin/pip install pygls`
  if needed.
- **`ModuleNotFoundError: pell`**: the wrapper script sets `PYTHONPATH` to
  the compiler dir; if you're running the server outside the script,
  prepend `compiler/` to `PYTHONPATH`.
- **Diagnostics stop updating**: the server caches the last parsed module
  per URI. If the IDE doesn't notify the server of edits (rare), restart
  the LSP server via the LSP4IJ status bar.
- **Quickfix / code action lightbulb doesn't appear** even though the
  red squiggle is there: the running pell-lsp Python process is
  stale. After updating the pell repo (e.g. `git pull`), restart the
  LSP server via **View → Tool Windows → LSP Consoles** → right-click
  the *pell* entry → **Restart**. The new code action handlers are
  registered at server-startup time, so a hot reload of `server.py`
  doesn't propagate without a restart.
- **Build fails with "could not resolve plugin"**: `./gradlew buildPlugin`
  needs internet access on first run to download the IntelliJ Platform
  Gradle plugin and LSP4IJ jar.

## Next steps

In priority order if you want to keep going:

1. **Schema-aware completion inside `sql!{}`** — requires the M4
   schema-snapshot pipeline. Highest user-visible payoff.
2. **Workspace symbols** — index all `pub fn`/`record`/`error` across the
   project; powers cross-file navigation.
3. **Code actions** — "extract `.expect(msg)` to const", "convert
   `.first() + match` to `.one()?`", etc. See INSTRUMENTATION_WISHLIST §3.
4. **Semantic tokens** — proper highlighting of `:bind` variables inside
   `sql!{}`, annotation names, error variants.
5. **Native (non-LSP) plugin** — Grammar-Kit BNF + PSI; the heavy lift
   that gets you real IntelliJ refactoring with DataGrip's SQL grammar
   injection working inside `sql!{}`. ~3–6 months.
