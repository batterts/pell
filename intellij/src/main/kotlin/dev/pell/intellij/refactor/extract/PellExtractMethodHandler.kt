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

    // internal (not private) so headless tests can drive the
    // transformation directly, bypassing the modal name/visibility
    // dialogs that invoke() shows.
    internal fun applyExtract(
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
                // Param types come from the captured local's declared
                // annotation (let/var/param typeRef); "text" fallback for
                // unannotated bindings. Never `any` — that lowers to a
                // non-existent t_any and fails at deploy.
                "${it.name}: ${it.typeText}"
            }
            val visibility = if (isPub) "pub fn" else "fn"

            // Outputs: a local declared in the selection that's used after
            // it must be RETURNED, and the call site rebinds it. v0 handles
            // the single-output case (the analyzer rejects >1).
            val output = analysis.outputs.firstOrNull()
            val bodyText = analysis.selectedStmts.joinToString("\n    ") { it.text }
            val fnText = if (output != null) {
                "$visibility $name($paramList) -> ${output.typeText} {\n" +
                    "    $bodyText\n    return ${output.name};\n}\n"
            } else {
                "$visibility $name($paramList) {\n    $bodyText\n}\n"
            }

            // Build the call site that replaces the selection. With an
            // output, rebind it so code after the selection still sees it.
            val argList = analysis.capturedParams.joinToString(", ") { it.name }
            val callText = if (output != null) {
                "let ${output.name}: ${output.typeText} = $name($argList);"
            } else {
                "$name($argList);"
            }

            // Order matters: insert the extracted fn FIRST, at the end of
            // the enclosing fn (which is AFTER the selection), so the
            // selection offsets stay valid. Replacing the selection first
            // would shrink the document and invalidate enclosingRange.end
            // (the original IndexOutOfBoundsException).
            val enclosingRange = analysis.enclosingFnOrMethod.textRange
            doc.insertString(enclosingRange.endOffset, "\n\n$fnText")

            // Now replace the selected stmt range with the call site.
            doc.replaceString(
                analysis.selectionRange.startOffset,
                analysis.selectionRange.endOffset,
                callText,
            )

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
