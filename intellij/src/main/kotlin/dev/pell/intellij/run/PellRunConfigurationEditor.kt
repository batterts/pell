package dev.pell.intellij.run

import com.intellij.openapi.options.SettingsEditor
import com.intellij.ui.components.JBTextField
import com.intellij.ui.dsl.builder.AlignX
import com.intellij.ui.dsl.builder.panel
import javax.swing.DefaultComboBoxModel
import javax.swing.JComboBox
import javax.swing.JComponent

/**
 * Per-config settings UI. Surfaces:
 *   - File path (the .pell file)
 *   - Action (build / exec / parse / tokens / build (to file))
 *   - Extra args
 *   - pell home override
 *
 * Built with IntelliJ's Kotlin DSL UI builder — same API the platform
 * uses for its own run configurations.
 */
class PellRunConfigurationEditor : SettingsEditor<PellRunConfiguration>() {

    private val filePathField = JBTextField()
    private val extraArgsField = JBTextField()
    private val pellHomeField = JBTextField()

    // Action dropdown — value is the actual CLI string to invoke.
    private val actions: Array<String> = arrayOf(
        "exec",
        "exec --dry-run",
        "build",
        "build -o",
        "parse",
        "tokens",
    )
    private val actionCombo: JComboBox<String> = JComboBox(DefaultComboBoxModel(actions))

    private val component: JComponent = panel {
        row("File path:") {
            cell(filePathField).align(AlignX.FILL)
        }.rowComment("Absolute path to the .pell file. Set automatically by the gutter icon.")

        row("Action:") {
            cell(actionCombo).align(AlignX.FILL)
        }.rowComment("Subcommand passed to the pell CLI (first positional arg).")

        row("Extra args:") {
            cell(extraArgsField).align(AlignX.FILL)
        }.rowComment("Appended to the command line. Example: --target 19c")

        row("pell home:") {
            cell(pellHomeField).align(AlignX.FILL)
        }.rowComment("Optional. Repo root holding the `pell` script. Defaults to the project root.")
    }

    override fun createEditor(): JComponent = component

    override fun resetEditorFrom(c: PellRunConfiguration) {
        filePathField.text = c.filePath.orEmpty()
        extraArgsField.text = c.extraArgs.orEmpty()
        pellHomeField.text = c.pellHome.orEmpty()
        actionCombo.selectedItem = c.action
    }

    override fun applyEditorTo(c: PellRunConfiguration) {
        c.filePath = filePathField.text.takeIf { it.isNotBlank() }
        c.extraArgs = extraArgsField.text.takeIf { it.isNotBlank() }
        c.pellHome = pellHomeField.text.takeIf { it.isNotBlank() }
        c.action = (actionCombo.selectedItem as? String) ?: "exec"
    }
}
