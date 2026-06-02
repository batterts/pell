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
 * Alt-Enter intention: wrap an expression-statement in a `for ... in`
 * loop. Useful when the expression returns a list (e.g.
 * `catalog::tables()` returns `list<text>`).
 *
 *     user_tables();    →    for row in user_tables() {
 *                                /* TODO */
 *                            }
 *
 * Heuristic: always offered on a bare expression-statement; the user
 * decides whether it's actually a list-producing call. v0.x: type
 * inference will gate the offer to actual `list<T>` returns; v0 always
 * offers it.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellWrapInForLoopIntention : IntentionAction {

    override fun getText(): String = "Wrap in for-loop"
    override fun getFamilyName(): String = "Pell"
    override fun startInWriteAction(): Boolean = true

    override fun isAvailable(project: Project, editor: Editor?, file: PsiFile?): Boolean {
        if (file !is PellFile || editor == null) return false
        val elem = file.findElementAt(editor.caretModel.offset) ?: return false
        return PsiTreeUtil.getParentOfType(elem, PellExprOrAssignStmt::class.java) != null
    }

    override fun invoke(project: Project, editor: Editor?, file: PsiFile?) {
        if (file !is PellFile || editor == null) return
        val elem = file.findElementAt(editor.caretModel.offset) ?: return
        val stmt = PsiTreeUtil.getParentOfType(elem, PellExprOrAssignStmt::class.java) ?: return

        val doc = editor.document
        val pm = PsiDocumentManager.getInstance(project)
        pm.commitDocument(doc)

        val exprText = stmt.text.trimEnd(';', ' ', '\n')
        val replacement = "for row in $exprText {\n    /* TODO */\n}"
        doc.replaceString(stmt.textRange.startOffset, stmt.textRange.endOffset, replacement)
        pm.commitDocument(doc)

        // Select `row` so the user can rename it.
        val newOffset = stmt.textRange.startOffset + "for ".length
        editor.caretModel.moveToOffset(newOffset)
        editor.selectionModel.setSelection(newOffset, newOffset + "row".length)
    }
}
