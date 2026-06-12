package dev.pell.intellij.settings

import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.execution.process.OSProcessHandler
import com.intellij.execution.process.ProcessAdapter
import com.intellij.execution.process.ProcessEvent
import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.options.Configurable
import com.intellij.openapi.options.ConfigurationException
import com.intellij.openapi.progress.ProgressIndicator
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.progress.Task
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.DialogPanel
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.util.Key
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextField
import com.intellij.ui.dsl.builder.AlignX
import com.intellij.ui.dsl.builder.bindText
import com.intellij.ui.dsl.builder.panel
import javax.swing.JButton
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
    private val jdwpPortField = JBTextField(settings.debugJdwpPort, 8)
    private val callbackHostField = JBTextField(settings.debugCallbackHost, 20)
    private val transportField = javax.swing.JComboBox(arrayOf("jdwp", "sql")).apply {
        selectedItem = settings.debugTransport
    }
    private val statusLabel = JBLabel("(checking…)")
    private val installButton = JButton("Install pell")
    private var dialogPanel: DialogPanel? = null

    override fun getDisplayName(): String = "pell"

    override fun createComponent(): JComponent {
        installButton.addActionListener { runPipInstall() }
        refreshStatus()
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
            group("Debugger") {
                row("Transport:") {
                    cell(transportField).align(AlignX.LEFT)
                }.rowComment(
                    "<code>jdwp</code> (default): the database connects back to the IDE — " +
                    "script breakpoints work; needs the JDWP network ACE. " +
                    "<code>sql</code>: outbound-only via DBMS_DEBUG — works through " +
                    "tunnels/NAT with just the DEBUG CONNECT SESSION grant."
                )
                row("JDWP listener port:") {
                    cell(jdwpPortField).align(AlignX.LEFT)
                }.rowComment(
                    "Fixed port for the IDE's listener — set when the connect-back " +
                    "arrives through a reverse SSH tunnel (<code>ssh -R 9040:…</code>). " +
                    "Blank = ephemeral."
                )
                row("Callback host:") {
                    cell(callbackHostField).align(AlignX.LEFT)
                }.rowComment(
                    "The address the DATABASE dials to reach the IDE. Blank = auto " +
                    "(<code>host.docker.internal</code> for a local container, the " +
                    "route-interface IP otherwise). Reverse tunnel: <code>127.0.0.1</code>."
                )
            }
            group("pell CLI") {
                row("Status:") {
                    cell(statusLabel).align(AlignX.LEFT)
                }
                row {
                    cell(installButton).align(AlignX.LEFT)
                }.rowComment(
                    "Runs <code>pip install pell</code> with the system Python " +
                    "to add the <code>pell</code> command to your PATH. " +
                    "If you'd rather, install manually: " +
                    "<code>pip install pell</code> (compiler core) or " +
                    "<code>pip install \"pell[repl]\"</code> (with Oracle driver + REPL)."
                )
            }
        }
        dialogPanel = p
        return p
    }

    /** Refresh the status label by probing for a working pell CLI. */
    private fun refreshStatus() {
        ApplicationManager.getApplication().executeOnPooledThread {
            val result = PellDetector.probe(project)
            ApplicationManager.getApplication().invokeLater {
                if (result != null) {
                    statusLabel.text = "<html>✓ found at <code>${result.path}</code><br/>" +
                        "<span style='color:#888'>${result.version}</span></html>"
                    installButton.text = "Reinstall pell"
                } else {
                    statusLabel.text = "<html>✗ not detected on PATH, <code>\$PELL_HOME</code>, " +
                        "or this project's root</html>"
                    installButton.text = "Install pell"
                }
            }
        }
    }

    /** Run `pip install pell` in a background task; report via notification. */
    private fun runPipInstall() {
        ProgressManager.getInstance().run(object : Task.Backgroundable(
            project, "Installing pell via pip", true,
        ) {
            override fun run(indicator: ProgressIndicator) {
                indicator.isIndeterminate = true
                indicator.text = "Locating pip…"
                val pip = locatePip() ?: run {
                    notify("Couldn't find pip on PATH. Install Python first " +
                           "(macOS: <code>brew install python</code>), then retry.",
                           NotificationType.WARNING)
                    return
                }
                indicator.text = "Running ${pip} install \"pell[repl]\"…"
                val cmd = GeneralCommandLine(pip, "install", "--user", "pell[repl]")
                    .withCharset(Charsets.UTF_8)
                val stderr = StringBuilder()
                val stdout = StringBuilder()
                val handler = OSProcessHandler(cmd)
                handler.addProcessListener(object : ProcessAdapter() {
                    override fun onTextAvailable(event: ProcessEvent, outputType: Key<*>) {
                        if (outputType == com.intellij.execution.process.ProcessOutputTypes.STDERR) {
                            stderr.append(event.text)
                        } else if (outputType == com.intellij.execution.process.ProcessOutputTypes.STDOUT) {
                            stdout.append(event.text)
                        }
                    }
                })
                handler.startNotify()
                handler.waitFor()
                if (handler.exitCode == 0) {
                    notify("Installed pell. Restart this IDE so the new PATH is picked up.",
                           NotificationType.INFORMATION)
                    ApplicationManager.getApplication().invokeLater { refreshStatus() }
                } else {
                    val err = stderr.toString().trim().ifEmpty { stdout.toString().trim() }
                    notify("pip install failed — try <code>pip install \"pell[repl]\"</code> " +
                           "in a terminal.<br/><pre>${err.take(500)}</pre>",
                           NotificationType.ERROR)
                }
            }
        })
    }

    /** Find the first `pip3` / `pip` on PATH. */
    private fun locatePip(): String? {
        val pathEntries = System.getenv("PATH")?.split(java.io.File.pathSeparator).orEmpty()
        for (name in listOf("pip3", "pip")) {
            for (dir in pathEntries) {
                val candidate = java.io.File(dir, name)
                if (candidate.canExecute()) return candidate.absolutePath
            }
        }
        return null
    }

    private fun notify(message: String, type: NotificationType) {
        ApplicationManager.getApplication().invokeLater {
            NotificationGroupManager.getInstance()
                .getNotificationGroup("pell")
                ?.createNotification("pell install", message, type)
                ?.notify(project)
        }
    }

    override fun isModified(): Boolean =
        targetDirField.text.trim() != settings.targetDirName ||
        jdwpPortField.text.trim() != settings.debugJdwpPort ||
        callbackHostField.text.trim() != settings.debugCallbackHost ||
        (transportField.selectedItem as String) != settings.debugTransport

    @Throws(ConfigurationException::class)
    override fun apply() {
        val v = targetDirField.text.trim()
        // Basic sanity: not empty, not an absolute path, no path traversal.
        if (v.isBlank()) throw ConfigurationException("Target directory must not be blank")
        if (v.startsWith("/") || v.startsWith("\\"))
            throw ConfigurationException("Target directory must be relative to the project root")
        if (v.contains("..")) throw ConfigurationException("Target directory must not contain `..`")
        settings.targetDirName = v
        val port = jdwpPortField.text.trim()
        if (port.isNotBlank() && port.toIntOrNull() !in 1..65535)
            throw ConfigurationException("JDWP listener port must be 1-65535 (or blank)")
        settings.debugJdwpPort = port
        settings.debugCallbackHost = callbackHostField.text.trim()
        settings.debugTransport = transportField.selectedItem as String
    }

    override fun reset() {
        targetDirField.text = settings.targetDirName
        jdwpPortField.text = settings.debugJdwpPort
        callbackHostField.text = settings.debugCallbackHost
        transportField.selectedItem = settings.debugTransport
    }
}
