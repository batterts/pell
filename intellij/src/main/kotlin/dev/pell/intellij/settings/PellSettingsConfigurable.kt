package dev.pell.intellij.settings

import com.intellij.openapi.options.Configurable
import com.intellij.openapi.options.ConfigurationException
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.DialogPanel
import com.intellij.ui.components.JBTextField
import com.intellij.ui.dsl.builder.AlignX
import com.intellij.ui.dsl.builder.bindText
import com.intellij.ui.dsl.builder.panel
import javax.swing.JComponent

/**
 * Settings → Tools → pell — one knob: the directory name where
 * lowered PL/SQL is written (and read by `pell deploy`).
 *
 * Surfaced via the `<projectConfigurable>` extension in plugin.xml.
 * Reset / Apply lifecycle is handled by IntelliJ; we just bind the
 * text field to the settings field via the Kotlin UI DSL.
 */
class PellSettingsConfigurable(private val project: Project) : Configurable {

    private val settings = PellSettings.getInstance(project)
    private val targetDirField = JBTextField(settings.targetDirName, 20)
    private var dialogPanel: DialogPanel? = null

    override fun getDisplayName(): String = "pell"

    override fun createComponent(): JComponent {
        val p = panel {
            group("Build output") {
                row("Target directory:") {
                    cell(targetDirField).align(AlignX.LEFT)
                }.rowComment(
                    "Where the lowered PL/SQL is written (relative to the project root). " +
                    "The split editor writes <code>&lt;name&gt;.sql</code> here on save; " +
                    "<code>pell deploy</code> reads from here. Default: <code>plsql</code>."
                )
            }
        }
        dialogPanel = p
        return p
    }

    override fun isModified(): Boolean =
        targetDirField.text.trim() != settings.targetDirName

    @Throws(ConfigurationException::class)
    override fun apply() {
        val v = targetDirField.text.trim()
        // Basic sanity: not empty, not an absolute path, no path traversal.
        if (v.isBlank()) throw ConfigurationException("Target directory must not be blank")
        if (v.startsWith("/") || v.startsWith("\\"))
            throw ConfigurationException("Target directory must be relative to the project root")
        if (v.contains("..")) throw ConfigurationException("Target directory must not contain `..`")
        settings.targetDirName = v
    }

    override fun reset() {
        targetDirField.text = settings.targetDirName
    }
}
