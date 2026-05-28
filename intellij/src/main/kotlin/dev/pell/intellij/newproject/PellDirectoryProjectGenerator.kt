package dev.pell.intellij.newproject

import com.intellij.icons.AllIcons
import com.intellij.ide.util.projectWizard.WebProjectTemplate
import com.intellij.openapi.module.Module
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.platform.ProjectGeneratorPeer
import com.intellij.ui.components.JBTextField
import java.io.File
import javax.swing.Icon

/**
 * "pell" entry in File → New → Project. Uses the older
 * `DirectoryProjectGenerator` API (via `WebProjectTemplate`) which is
 * the surface the New Project wizard's left sidebar reads from.
 *
 * The newer `GeneratorNewProjectWizard` API does NOT auto-wire into
 * the sidebar — IntelliJ's modern wizard only shows entries that are
 * also registered via this older `directoryProjectGenerator` EP.
 *
 * Settings carried per-instance: `pellHome` — path to the pell repo
 * root, used to populate the generated `./pell` shim's default.
 */
class PellDirectoryProjectGenerator : WebProjectTemplate<PellSettings>() {

    override fun getName(): String = "pell"
    override fun getDescription(): String =
        "Statically-typed source that compiles to Oracle PL/SQL. " +
        "Scaffolds src/, tests/, built/, a pell wrapper script, and a one-click " +
        "Deploy run configuration."
    // `getIcon()` replaced the deprecated `getLogo()` in newer IDEs.
    override fun getIcon(): Icon = AllIcons.Actions.Compile

    override fun generateProject(
        project: Project,
        baseDir: VirtualFile,
        settings: PellSettings,
        module: Module,
    ) {
        // The wizard calls generateProject *after* the project's base
        // directory exists on disk — we can scaffold immediately
        // instead of deferring via the deprecated/internal
        // StartupManager.runAfterOpened.
        PellProjectScaffold.create(
            root = File(baseDir.path),
            projectName = project.name,
            pellHome = settings.pellHome.takeIf { it.isNotBlank() },
        )
        baseDir.refresh(true, true)
    }

    override fun createPeer(): ProjectGeneratorPeer<PellSettings> = PellGeneratorPeer()
}

/** Per-project settings — currently just the pell home override. */
class PellSettings {
    var pellHome: String = detectPellHome()
}

/**
 * The settings UI shown in the right-hand pane of the New Project
 * wizard when "pell" is selected. One row: Pell home.
 */
private class PellGeneratorPeer : ProjectGeneratorPeer<PellSettings> {

    private val settings = PellSettings()
    private val pellHomeField = JBTextField(settings.pellHome)

    // Note: we deliberately don't override `getComponent()` — that
    // method is deprecated. `buildUI(SettingsStep)` is the active
    // surface; IntelliJ's wizard wires the SettingsStep fields into
    // the right pane itself.
    override fun buildUI(settingsStep: com.intellij.ide.util.projectWizard.SettingsStep) {
        settingsStep.addSettingsField("Pell home:", pellHomeField)
    }
    override fun getSettings(): PellSettings {
        settings.pellHome = pellHomeField.text.trim()
        return settings
    }
    override fun validate(): com.intellij.openapi.ui.ValidationInfo? = null
    override fun isBackgroundJobRunning(): Boolean = false
    override fun addSettingsListener(listener: ProjectGeneratorPeer.SettingsListener) {}
}

private fun detectPellHome(): String =
    System.getenv("PELL_HOME")
        ?: listOf(
            System.getProperty("user.home") + "/code/plsql",
            System.getProperty("user.home") + "/code/pell",
            System.getProperty("user.home") + "/src/pell",
        ).firstOrNull { File("$it/pell").canExecute() }
        ?: ""
