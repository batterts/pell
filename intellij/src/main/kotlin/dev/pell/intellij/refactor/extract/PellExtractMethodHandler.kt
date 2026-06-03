package dev.pell.intellij.refactor.extract

import com.intellij.openapi.actionSystem.DataContext
import com.intellij.openapi.application.runWriteAction
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.refactoring.RefactoringActionHandler
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellPsiFactory

/**
 * Extract Method handler — the user-facing entry point bound to the
 * `Refactor ▸ Extract Method...` action (Cmd-Opt-M) for pell files.
 *
 * Workflow:
 *   1. Run [ExtractMethodAnalyzer.analyze] on the current selection.
 *   2. If rejected, surface the reason as a balloon and stop.
 *   3. Otherwise show a dialog asking for: name, public/private toggle.
 *   4. Synthesize a `pub fn <name>(<captured>) -> <inferred>?` after
 *      the enclosing fn.
 *   5. Replace the selected stmts with a call to the new fn.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellExtractMethodHandler : RefactoringActionHandler {

    override fun invoke(project: Project, editor: Editor?, file: PsiFile?, dataContext: DataContext?) {
        if (editor == null || file !is PellFile) return
        val analysis = ExtractMethodAnalyzer.analyze(file, editor)
        if (!analysis.isExtractable) {
            balloon(project, "Cannot extract: ${analysis.rejectionReason}")
            return
        }

        val name = Messages.showInputDialog(
            project,
            "Method name:",
            "Extract Method",
            null,
            "extracted",
            PellMethodNameValidator,
        ) ?: return

        val isPub = Messages.showYesNoDialog(
            project,
            "Make the new method `pub`?",
            "Extract Method",
            "pub fn",
            "fn",
            null,
        ) == Messages.YES

        applyExtract(project, file, editor, analysis, name, isPub)
    }

    override fun invoke(project: Project, elements: Array<out PsiElement>, dataContext: DataContext?) {
        // No-op: Extract Method is editor-driven.
    }

    private fun applyExtract(
        project: Project,
        file: PellFile,
        editor: Editor,
        analysis: ExtractMethodAnalysis,
        name: String,
        isPub: Boolean,
    ) {
        WriteCommandAction.runWriteCommandAction(project, "Extract Method '$name'", null, Runnable {
            val doc = editor.document
            val pm = PsiDocumentManager.getInstance(project)
            pm.commitDocument(doc)

            // Build the extracted fn text from the captured params + selected stmts.
            val paramList = analysis.capturedParams.joinToString(", ") {
                // Phase 6 v0: every captured param is typed `any` (will be
                // inferred by the compiler). Phase-final type-inference pass
                // can fill in concrete types per the original locals.
                "${it.name}: any"
            }
            val visibility = if (isPub) "pub fn" else "fn"
            val bodyText = analysis.selectedStmts.joinToString("\n    ") { it.text }
            val fnText = "$visibility $name($paramList) {\n    $bodyText\n}\n"

            // Build the call site that replaces the selection.
            val argList = analysis.capturedParams.joinToString(", ") { it.name }
            val callText = "$name($argList);"

            // Replace the selected stmt range with the call site.
            doc.replaceString(
                analysis.selectionRange.startOffset,
                analysis.selectionRange.endOffset,
                callText,
            )

            // Insert the extracted fn after the enclosing fn declaration.
            val enclosingRange = analysis.enclosingFnOrMethod.textRange
            doc.insertString(enclosingRange.endOffset, "\n\n$fnText")

            pm.commitDocument(doc)
        })
    }

    private fun balloon(project: Project, msg: String) {
        Messages.showInfoMessage(project, msg, "Extract Method")
    }
}

private object PellMethodNameValidator : com.intellij.openapi.ui.InputValidator {
    private val NAME_RE = Regex("[a-zA-Z_][a-zA-Z0-9_]*")
    override fun checkInput(inputString: String?): Boolean =
        inputString != null && NAME_RE.matches(inputString)
    override fun canClose(inputString: String?): Boolean = checkInput(inputString)
}
