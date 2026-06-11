package dev.pell.intellij.debugger

import com.intellij.execution.ExecutionManager
import com.intellij.execution.RunManager
import com.intellij.execution.executors.DefaultDebugExecutor
import com.intellij.execution.executors.DefaultRunExecutor
import com.intellij.execution.lineMarker.RunLineMarkerContributor
import com.intellij.execution.runners.ExecutionEnvironmentBuilder
import com.intellij.icons.AllIcons
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.psi.PsiElement
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFileType
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.run.PellRunConfiguration
import dev.pell.intellij.run.PellRunConfigurationType
import java.io.File

/**
 * Run/Debug gutter icons on every `pub fn`. Both actions generate (or
 * reuse) a debug stub — a small exec script in `.pell-debug/` that
 * imports the module and calls the fn with placeholder args — open it
 * for editing, and launch it:
 *
 *   ▶ Run    — `pell exec` of the stub (plain run, console output)
 *   🐞 Debug — full debugger: deploys the module with --debug, steps
 *              through the stub AND the fn (and into plain PL/SQL
 *              where pell source runs out)
 */
class PellDebugFnLineMarkerContributor : RunLineMarkerContributor() {

    override fun getInfo(element: PsiElement): Info? {
        val file = element.containingFile ?: return null
        if (file.fileType !is PellFileType) return null
        if (element.node?.elementType != PellElementTypes.IDENT) return null
        val fn = element.parent as? PellFnDef
            ?: PsiTreeUtil.getParentOfType(element, PellFnDef::class.java)?.takeIf {
                it.name == element.text
            } ?: return null
        if (fn.name != element.text) return null
        // pub fns only — private fns aren't callable from an anon stub.
        if (!fn.text.trimStart().startsWith("pub")) return null
        // Only the fn's own name ident (not idents in the body).
        val nameOffset = fn.text.indexOf(element.text)
        if (element.textOffset != fn.textOffset + nameOffset) return null

        return Info(
            AllIcons.Actions.Execute,
            arrayOf(StubAction(fn, debug = false), StubAction(fn, debug = true)),
        ) { "Run/Debug ${fn.name} via a generated stub" }
    }

    private class StubAction(private val fn: PellFnDef, private val debug: Boolean) : AnAction(
        if (debug) "Debug ${fn.name} (pell debugger)" else "Run ${fn.name} (generated stub)",
        if (debug) "Generate a stub calling ${fn.name} and step through it"
        else "Generate a stub calling ${fn.name} and execute it",
        if (debug) AllIcons.Actions.StartDebugger else AllIcons.Actions.Execute,
    ) {
        override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

        override fun actionPerformed(e: AnActionEvent) {
            val project = e.project ?: return
            val root = project.basePath?.let { File(it) } ?: return
            val moduleFile = fn.containingFile.virtualFile?.path ?: return

            val stub = PellDebugStubGenerator.stubFile(root, fn)
            LocalFileSystem.getInstance().refreshAndFindFileByIoFile(stub)?.let { vf ->
                FileEditorManager.getInstance(project).openFile(vf, true)
            }

            val runManager = RunManager.getInstance(project)
            val name = "pell debug: ${fn.name}"
            val settings = runManager.allSettings.firstOrNull {
                it.name == name && it.configuration is PellRunConfiguration
            } ?: runManager.createConfiguration(name, PellRunConfigurationType.INSTANCE.configurationFactories[0]).also {
                runManager.addConfiguration(it)
            }
            (settings.configuration as PellRunConfiguration).apply {
                filePath = stub.absolutePath
                action = "exec"
                options.deployPath = moduleFile
            }
            runManager.selectedConfiguration = settings

            val executor = if (debug) DefaultDebugExecutor.getDebugExecutorInstance()
                           else DefaultRunExecutor.getRunExecutorInstance()
            val env = ExecutionEnvironmentBuilder.create(executor, settings).build()
            ExecutionManager.getInstance(project).restartRunProfile(env)
        }
    }
}
