package dev.pell.intellij.run

import com.intellij.execution.ProgramRunnerUtil
import com.intellij.execution.RunManager
import com.intellij.execution.executors.DefaultRunExecutor
import com.intellij.icons.AllIcons
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys

/**
 * One menu entry shown on the gutter-icon popup. Each instance is bound
 * to a single pell subcommand (`exec`, `build`, `parse`, …); clicking it
 * creates (or reuses) a `PellRunConfiguration` for the current file
 * with that action and runs it via the standard Run executor.
 *
 * The configuration is auto-saved with a deterministic name
 * (`pell <action>: <file>`) so a follow-up run from the toolbar
 * dropdown or Run menu picks up the same one — no proliferation of
 * temporary configs cluttering the dropdown.
 */
class PellRunAction(
    private val pellAction: String,
    label: String,
    description: String,
) : AnAction(label, description, AllIcons.RunConfigurations.TestState.Run) {

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val virtualFile = e.getData(CommonDataKeys.VIRTUAL_FILE) ?: return

        val runManager = RunManager.getInstance(project)
        val factory = PellRunConfigurationType.INSTANCE.configurationFactories.first()
        val name = "pell $pellAction: ${virtualFile.nameWithoutExtension}"

        val settings = runManager.findConfigurationByName(name)
            ?: runManager.createConfiguration(name, factory).also { runManager.addConfiguration(it) }

        (settings.configuration as PellRunConfiguration).apply {
            filePath = virtualFile.path
            action = pellAction
        }
        runManager.selectedConfiguration = settings

        ProgramRunnerUtil.executeConfiguration(settings, DefaultRunExecutor.getRunExecutorInstance())
    }
}
