# Shipping the PSI Track to the JetBrains Marketplace

The PSI track lands as **v0.4.0 → v1.0.0** in the public Marketplace
plugin. Each phase commit can ship on its own; users see incremental
capability as the version climbs. This document captures the workflow.

## Pre-release checklist

Before publishing **any** PSI-track version:

```bash
# Worktree → branch is `psi-track`; merge to main first.
cd /Users/shaun.batterton/code/plsql
git checkout main
git merge psi-track --no-ff       # preserve the 8-commit history
git push
```

Then bump the version + change-notes in `intellij/`:

- `intellij/build.gradle.kts` → `version = "0.4.0"` (or whatever)
- `intellij/src/main/resources/META-INF/plugin.xml` → `<version>` and
  prepend a `<h3>0.4.0</h3>` block to `<change-notes>`

(The version + description + change-notes blocks are the other agent's
territory — coordinate before bumping.)

## Phase → version mapping

| Phase                                | Version  | Marketplace headline                      |
|--------------------------------------|----------|-------------------------------------------|
| 0  Grammar-Kit + JFlex bootstrap     | —        | internal, no ship                         |
| 1  Lexer port                        | bundled  | with Phase 2                              |
| 2  Real PSI parser                   | v0.4.0   | "Real PSI parser — structure view"        |
| 3  PSI mixins                        | bundled  | with Phase 4                              |
| 4  Cross-file references + Find Usages | v0.5.0 | "Project-wide navigation"                 |
| 5  Rename                            | v0.6.0   | "Rename across the project"               |
| 6  Extract Method                    | v0.7.0   | "Extract Method"                          |
| 7  Parameters → Record               | v0.8.0   | "Parameters → Record"                     |
| 8  Move / Inline / Change Signature  | v0.9.0   | "Move, Inline, Change Signature"          |
| Final autocomplete + tests           | v1.0.0   | "Refactoring complete"                    |

## Build the .zip

```bash
cd intellij
JAVA_HOME=~/.local/jdk-21 ./gradlew clean buildPlugin
# Output: build/distributions/pell-intellij-0.4.0.zip
```

## Publish (DO NOT auto-publish — user review required)

```bash
# 1. Sanity check the zip in a clean IDE first:
#    Settings ▸ Plugins ▸ ⚙ ▸ Install Plugin from Disk ▸ pick the zip.
#    Open an example .pell file. Verify: structure view shows the
#    fn / record children, Cmd-Click on a name navigates, Find Usages
#    finds it, Rename across files works.
#
# 2. When happy:
JETBRAINS_MARKETPLACE_TOKEN=... \
  ./gradlew publishPlugin
```

The publish task signs the bundle and uploads via the Marketplace REST
API. New version appears on plugins.jetbrains.com within minutes.

## Things that need user attention before each ship

- **First-launch verification**: run the plugin against a real pell
  project (the `compiler/examples/` directory works as a smoke project).
- **Regression watch**: confirm the existing v0.3.13 features still
  work — preview pane, gutter actions, tool window, REPL launcher,
  New Project wizard. The PSI work is additive but plugin.xml has
  shared regions.
- **Rollback plan**: if a phase ships and immediately reports issues,
  Marketplace supports unpublishing the version. Users who already
  upgraded stay upgraded; the listing reverts to the prior public
  version for new installs.

## Why the user-review gate exists

The Marketplace listing is published under the user's personal
JetBrains account. Once a release goes out, every IDE in every JetBrains
install worldwide can pull it. Bugs at that scale are expensive in
trust. The PSI work hasn't been exercised in a live IDE yet — it
needs the user to sanity-check before shipping.
