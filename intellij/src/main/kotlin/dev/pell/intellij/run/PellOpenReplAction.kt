package dev.pell.intellij.run

import com.intellij.icons.AllIcons
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys

/**
 * Gutter-popup action: "Open pell REPL" with the current `.pell` file
 * pre-loaded. Same effect as opening a terminal, running `./pell repl`,
 * then typing `\load <currentfile>` — but in one click.
 *
 * Distinct from [PellRunAction] because the REPL needs a real TTY,
 * which only the Terminal tool window provides. PellRunAction creates
 * RunConfigurations, which use IntelliJ's Run console (no TTY → prompt
 * toolkit doesn't render properly).
 */
class PellOpenReplAction : AnAction(
    "Open pell REPL (load this file)",
    "Open the pell REPL in a terminal tab and \\load this .pell file's defs into the session",
    AllIcons.Debugger.Console,
) {
    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val vfile = e.getData(CommonDataKeys.VIRTUAL_FILE) ?: return
        PellReplLauncher.launch(project, loadFile = vfile.path)
    }
}
