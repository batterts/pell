package dev.pell.intellij.intentions

import com.intellij.codeInsight.intention.IntentionAction
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiFile
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellExprOrAssignStmt

/**
 * Alt-Enter intention: wrap an expression-statement in a `let` binding.
 *
 *     user_tables();    →    let result = user_tables();
 *
 * Available when the caret is on a [PellExprOrAssignStmt] whose
 * statement is a bare expression (not already an assignment). Doesn't
 * fire on `let` / `var` statements — they're already named.
 *
 * After invocation, leaves the caret on `result` so the user can rename
 * in-place. Phase-final type inference will add the `: T` annotation;
 * v0 omits the type and lets the compiler infer.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellIntroduceVariableIntention : IntentionAction {

    override fun getText(): String = "Introduce local variable"
    override fun getFamilyName(): String = "Pell"
    override fun startInWriteAction(): Boolean = true

    override fun isAvailable(project: Project, editor: Editor?, file: PsiFile?): Boolean {
        if (file !is PellFile || editor == null) return false
        val elem = file.findElementAt(editor.caretModel.offset) ?: return false
        val stmt = PsiTreeUtil.getParentOfType(elem, PellExprOrAssignStmt::class.java) ?: return false
        // Skip if it's already an assignment (contains an `=` token).
        val hasAssign = stmt.children.any { it.text == "=" }
        return !hasAssign
    }

    override fun invoke(project: Project, editor: Editor?, file: PsiFile?) {
        if (file !is PellFile || editor == null) return
        val elem = file.findElementAt(editor.caretModel.offset) ?: return
        val stmt = PsiTreeUtil.getParentOfType(elem, PellExprOrAssignStmt::class.java) ?: return

        val doc = editor.document
        val pm = PsiDocumentManager.getInstance(project)
        pm.commitDocument(doc)

        val exprText = stmt.text.trimEnd(';', ' ', '\n')
        val replacement = "let result = $exprText;"
        doc.replaceString(stmt.textRange.startOffset, stmt.textRange.endOffset, replacement)
        pm.commitDocument(doc)

        // Position the caret on `result` so the user can rename in-place.
        val newOffset = stmt.textRange.startOffset + "let ".length
        editor.caretModel.moveToOffset(newOffset)
        editor.selectionModel.setSelection(newOffset, newOffset + "result".length)
    }
}
