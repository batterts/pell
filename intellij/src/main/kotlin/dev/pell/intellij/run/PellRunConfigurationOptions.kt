package dev.pell.intellij.run

import com.intellij.execution.configurations.LocatableRunConfigurationOptions

/**
 * Persisted options for a pell run configuration. Each config remembers
 * the file path, the action (`build` / `exec` / `parse` / `tokens`), and
 * any extra args to pass to the pell CLI.
 *
 * Extends `LocatableRunConfigurationOptions` so the platform's
 * "auto-named from context" machinery works correctly when the user
 * triggers the gutter icon.
 */
class PellRunConfigurationOptions : LocatableRunConfigurationOptions() {
    /** Absolute path to the .pell file to run. */
    var filePath: String? by string()

    /** "build", "exec", "parse", "tokens", or e.g. "build -o" / "exec --dry-run". */
    var action: String? by string("exec")

    /** Extra args appended to the pell command line (e.g. `--target 19c`). */
    var extraArgs: String? by string()

    /** Optional override for the pell repo (defaults to project root). */
    var pellHome: String? by string()

    /** What `pell deploy --debug` deploys before a debug session — the
     *  module file the debug stub calls into. Defaults to filePath. */
    var deployPath: String? by string()

    /** Environment variables for the spawned pell processes (exec,
     *  debug-target, debug-serve, deploy). Win over the IDE's
     *  inherited environment and the settings-page DB URL. */
    var envVars: MutableMap<String, String> by map()
}
