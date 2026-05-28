package dev.pell.intellij.newproject

import com.intellij.icons.AllIcons
import com.intellij.ide.util.projectWizard.WizardContext
import com.intellij.ide.wizard.AbstractNewProjectWizardStep
import com.intellij.ide.wizard.GeneratorNewProjectWizard
import com.intellij.ide.wizard.GeneratorNewProjectWizardBuilderAdapter
import com.intellij.ide.wizard.NewProjectWizardBaseData.Companion.baseData
import com.intellij.ide.wizard.NewProjectWizardBaseStep
import com.intellij.ide.wizard.NewProjectWizardChainStep.Companion.nextStep
import com.intellij.ide.wizard.NewProjectWizardStep
import com.intellij.ide.wizard.RootNewProjectWizardStep
import com.intellij.openapi.project.Project
import com.intellij.ui.dsl.builder.AlignX
import com.intellij.ui.dsl.builder.Panel
import com.intellij.ui.components.JBTextField
import java.io.File
import javax.swing.Icon

/**
 * The "pell" entry in IntelliJ IDEA's New Project dialog (under the
 * "Generators" section). IDEA reads this surface via a
 * `<moduleBuilder>` registration whose class wraps a
 * `GeneratorNewProjectWizard`. The actual wizard logic lives here;
 * the [PellNewProjectWizardBuilder] inner class is the adapter IDEA
 * needs to discover us.
 *
 * For PyCharm / WebStorm / RubyMine etc., the parallel surface is
 * `<directoryProjectGenerator>` — see `PellDirectoryProjectGenerator`
 * for that registration. Both keep the same scaffold logic in
 * `PellProjectScaffold`.
 */
class PellNewProjectWizard : GeneratorNewProjectWizard {
    override val id: String = "pell"
    override val name: String = "pell"
    override val icon: Icon = AllIcons.Actions.Compile
    override val description: String =
        "A statically-typed language that compiles to Oracle PL/SQL 23. " +
        "Scaffolds src/, tests/, built/, a pell wrapper script, and a " +
        "one-click Deploy run configuration."

    override fun createStep(context: WizardContext): NewProjectWizardStep =
        RootNewProjectWizardStep(context)
            .nextStep(::NewProjectWizardBaseStep)
            .nextStep(::PellOptionsStep)

    /** Adapter class IDEA's "Generators" wizard list picks up. The
     *  `<moduleBuilder>` extension in plugin.xml points at this — IDEA
     *  finds it, instantiates it, and shows the entry. */
    class PellNewProjectWizardBuilder :
        GeneratorNewProjectWizardBuilderAdapter(PellNewProjectWizard())
}

/** Per-project Pell options surfaced in the wizard UI. */
class PellOptionsStep(parent: NewProjectWizardStep) : AbstractNewProjectWizardStep(parent) {

    private val pellHomeField = JBTextField(detectPellHomeWizard())

    override fun setupUI(builder: Panel) {
        builder.group("Pell") {
            row("Pell home:") {
                cell(pellHomeField).align(AlignX.FILL)
            }.rowComment(
                "Path to the pell repo (the directory containing the <code>pell</code> script). " +
                "Defaults to <code>\$PELL_HOME</code> or your <code>~/code/plsql</code> checkout. " +
                "Baked into the generated <code>./pell</code> shim as a fallback."
            )
        }
    }

    override fun setupProject(project: Project) {
        val base = baseData ?: return
        val location = File(base.path, base.name)
        val pellHome = pellHomeField.text.trim().takeIf { it.isNotBlank() }
        PellProjectScaffold.create(location, base.name, pellHome)
    }
}

internal fun detectPellHomeWizard(): String =
    System.getenv("PELL_HOME")
        ?: listOf(
            System.getProperty("user.home") + "/code/plsql",
            System.getProperty("user.home") + "/code/pell",
            System.getProperty("user.home") + "/src/pell",
        ).firstOrNull { File("$it/pell").canExecute() }
        ?: ""
