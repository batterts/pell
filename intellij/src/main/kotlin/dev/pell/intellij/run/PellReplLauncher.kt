package dev.pell.intellij.run

import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project

/**
 * Shared helper for "open the pell REPL in IntelliJ's terminal."
 *
 * Used by:
 *   - PellToolWindow's "Open pell REPL" toolbar button (no file context)
 *   - PellRunLineMarkerContributor's gutter action (with file → `\load`s
 *     the current .pell so its defs are in the running session)
 *
 * The pell REPL needs a real TTY because prompt_toolkit's multi-line
 * input doesn't work in IntelliJ's Run console. IntelliJ's Terminal
 * plugin (bundled with every IDE) gives us one.
 */
object PellReplLauncher {

    private val LOG = logger<PellReplLauncher>()

    /** Open a fresh terminal tab in the project root and start the
     *  pell REPL. If [loadFile] is non-null, also `\load` it after the
     *  REPL prompt appears so its defs are available immediately.
     *
     *  Returns true on success; false if the terminal couldn't be
     *  opened (e.g. the Terminal plugin is missing — shouldn't happen
     *  since our plugin.xml depends on it, but defensive). */
    fun launch(project: Project, loadFile: String? = null): Boolean {
        val cwd = project.basePath ?: return false
        return try {
            val mgr = org.jetbrains.plugins.terminal.TerminalToolWindowManager.getInstance(project)
            // Use the non-deprecated createShellWidget variant.
            // Params: workingDirectory, tabName, requestFocus,
            //         deferSessionStartUntilUiShown.
            val widget = mgr.createShellWidget(cwd, "pell repl", true, true)
            // First command: launch the REPL.
            widget.sendCommandToExecute("./pell repl")
            // Second command: pre-load the file's defs. The REPL is a
            // TTY consumer, so backslash-commands at start-of-line are
            // parsed by the REPL itself.
            if (loadFile != null) {
                widget.sendCommandToExecute("\\load $loadFile")
            }
            true
        } catch (e: Throwable) {
            LOG.warn("Failed to open pell REPL terminal", e)
            false
        }
    }
}
