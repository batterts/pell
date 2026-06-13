package dev.pell.intellij.run

import com.intellij.execution.ProgramRunnerUtil
import com.intellij.execution.RunManager
import com.intellij.execution.executors.DefaultRunExecutor
import com.intellij.execution.lineMarker.RunLineMarkerContributor
import com.intellij.icons.AllIcons
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.psi.PsiElement
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFileType
import dev.pell.intellij.psi.PellAnnotation
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellFnName
import dev.pell.intellij.psi.PellItem
import dev.pell.intellij.psi.PellModuleDecl

/**
 * utPLSQL "run test" gutter icons, mirroring the JUnit experience:
 *
 *   ▷ on the `module` keyword of an `@suite` module  → run the whole
 *     suite (`pell test <suite>`)
 *   ▷ on each `@test`-annotated `pub fn`             → run that one
 *     test (`pell test <suite>.<fn>`)
 *
 * Backed by `pell test`, which calls `ut.run(...)` against the deployed
 * package — it does NOT redeploy first, so the package must already be
 * installed (`pell deploy`). The suite name is derived from the module
 * declaration: `module hr::suite.x` → `hr.x`, plain `module foo` → `foo`.
 *
 * This stacks with the per-`pub fn` Run/Debug stub icons from
 * PellDebugFnLineMarkerContributor — a `@test pub fn` gets both, so you
 * can run it as a utPLSQL test or step through it via a stub.
 */
class PellTestRunLineMarkerContributor : RunLineMarkerContributor() {

    override fun getInfo(element: PsiElement): Info? {
        val file = element.containingFile ?: return null
        if (file.fileType !is PellFileType) return null

        // Whole-suite marker: the `module` keyword, when a file-level
        // `@suite` annotation is present.
        if (element.node?.elementType == PellElementTypes.KW_MODULE) {
            val decl = element.parent as? PellModuleDecl ?: return null
            if (!hasSuiteAnnotation(file)) return null
            val suite = suiteName(decl) ?: return null
            return Info(
                AllIcons.RunConfigurations.TestState.Run_run,
                arrayOf(RunSuiteAction(suite, test = null)),
            ) { "Run utPLSQL suite $suite" }
        }

        // Single-test marker: the fn name of a `@test pub fn`.
        if (element.node?.elementType == PellElementTypes.IDENT) {
            val nameNode = element.parent as? PellFnName ?: return null
            val fn = nameNode.parent as? PellFnDef ?: return null
            val item = fn.parent as? PellItem ?: return null
            if (item.annotationList.none { annotationName(it) == "test" }) return null
            val decl = PsiTreeUtil.findChildOfType(file, PellModuleDecl::class.java) ?: return null
            val suite = suiteName(decl) ?: return null
            val fnName = fn.name ?: return null
            return Info(
                AllIcons.RunConfigurations.TestState.Run,
                arrayOf(RunSuiteAction(suite, test = fnName)),
            ) { "Run utPLSQL test $suite.$fnName" }
        }
        return null
    }

    /** `@suite` may carry args (`@suite("desc", rollback = "manual")`)
     *  or stand bare; either way it sits at file level before the
     *  module declaration. */
    private fun hasSuiteAnnotation(file: PsiElement): Boolean =
        PsiTreeUtil.getChildrenOfType(file, PellAnnotation::class.java)
            ?.any { annotationName(it) == "suite" } == true

    private fun annotationName(a: PellAnnotation): String? = a.ident?.text

    /** Suite path as utPLSQL's `ut.run` sees it. `module schema::a.b.pkg`
     *  → `schema.pkg`; `module a.b.pkg` / `module pkg` → `pkg` (the
     *  package is the last dotted segment; the `::` prefix is the
     *  schema). */
    private fun suiteName(decl: PellModuleDecl): String? {
        val raw = decl.moduleName?.text?.trim() ?: return null
        val (schema, path) =
            if ("::" in raw) raw.substringBefore("::").trim() to raw.substringAfter("::").trim()
            else null to raw
        val pkg = path.substringAfterLast('.').trim()
        if (pkg.isEmpty()) return null
        return if (schema.isNullOrEmpty()) pkg else "$schema.$pkg"
    }

    /** Runs `pell test <suite>` — or `<suite>.<test>` for a single fn —
     *  via a reused, deterministically-named run configuration. */
    private class RunSuiteAction(
        private val suite: String,
        private val test: String?,
    ) : AnAction(
        if (test == null) "Run suite $suite (utPLSQL)" else "Run test $suite.$test (utPLSQL)",
        if (test == null) "Run the whole utPLSQL suite via `pell test`"
        else "Run this single utPLSQL test via `pell test`",
        AllIcons.RunConfigurations.TestState.Run,
    ) {
        override fun actionPerformed(e: AnActionEvent) {
            val project = e.project ?: return
            val target = if (test == null) suite else "$suite.$test"
            val runManager = RunManager.getInstance(project)
            val factory = PellRunConfigurationType.INSTANCE.configurationFactories.first()
            val name = "pell test: $target"
            val settings = runManager.findConfigurationByName(name)
                ?: runManager.createConfiguration(name, factory)
                    .also { runManager.addConfiguration(it) }
            (settings.configuration as PellRunConfiguration).apply {
                action = "test"
                suiteName = target
            }
            runManager.selectedConfiguration = settings
            ProgramRunnerUtil.executeConfiguration(
                settings, DefaultRunExecutor.getRunExecutorInstance(),
            )
        }
    }
}
