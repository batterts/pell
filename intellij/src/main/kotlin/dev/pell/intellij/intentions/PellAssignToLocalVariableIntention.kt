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
 * Alt-Enter intention: replace `expr;` with a typed binding:
 *
 *     user_tables();    →    let result: list<text> = user_tables();
 *
 * Same shape as IntroduceLocalVariable but emits the type annotation
 * placeholder so the user only edits the name. v0 hard-codes
 * `list<text>` for catalog::tables-style calls (heuristic) and `text`
 * otherwise; later phases swap in real type inference.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellAssignToLocalVariableIntention : IntentionAction {

    override fun getText(): String = "Assign to local variable"
    override fun getFamilyName(): String = "Pell"
    override fun startInWriteAction(): Boolean = true

    override fun isAvailable(project: Project, editor: Editor?, file: PsiFile?): Boolean {
        if (file !is PellFile || editor == null) return false
        val elem = file.findElementAt(editor.caretModel.offset) ?: return false
        val stmt = PsiTreeUtil.getParentOfType(elem, PellExprOrAssignStmt::class.java) ?: return false
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
        val inferredType = guessReturnType(exprText)
        val replacement = "let result: $inferredType = $exprText;"
        doc.replaceString(stmt.textRange.startOffset, stmt.textRange.endOffset, replacement)
        pm.commitDocument(doc)

        // Select the placeholder type so it's easy to fix.
        val typeStart = stmt.textRange.startOffset + "let result: ".length
        editor.caretModel.moveToOffset(typeStart)
        editor.selectionModel.setSelection(typeStart, typeStart + inferredType.length)
    }

    /** Cheap heuristic — no real inference yet. */
    private fun guessReturnType(exprText: String): String = when {
        "catalog::" in exprText && "()" in exprText -> "list<text>"
        exprText.endsWith("()") && exprText.startsWith(exprText.lowercase()) -> "text"
        else -> "text"
    }
}
