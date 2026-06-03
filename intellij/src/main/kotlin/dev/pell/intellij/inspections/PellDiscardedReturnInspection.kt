package dev.pell.intellij.inspections

import com.intellij.codeInspection.LocalInspectionTool
import com.intellij.codeInspection.LocalQuickFix
import com.intellij.codeInspection.ProblemDescriptor
import com.intellij.codeInspection.ProblemHighlightType
import com.intellij.codeInspection.ProblemsHolder
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiElementVisitor
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.psi.PellCallOp
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellExprOrAssignStmt

/**
 * Warns when a bare expression-statement is a function call whose
 * return value is discarded:
 *
 *     user_tables();          // ← yellow squiggle
 *
 * Discarded function returns are a compile-time error in Oracle PL/SQL
 * (a function can't be called as a statement; that's procedure syntax).
 * The pell emitter currently lowers the bare call verbatim, so the
 * package body deploys with `PLS-00221: 'X' is not a procedure`. This
 * inspection catches the bug at edit time so the user can fix it before
 * deploy.
 *
 * Quick-fixes (Alt-Enter):
 *   - "Assign to local variable" → `let result = user_tables();`
 *   - "Wrap in for-loop"          → `for row in user_tables() { /* TODO */ }`
 *
 * Heuristic: any bare expression-statement that contains a [PellCallOp]
 * descendant. False positives are possible (the call really might be a
 * procedure-style fn whose return is intentionally ignored) but the
 * "Suppress for statement" Alt-Enter option handles those cases.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellDiscardedReturnInspection : LocalInspectionTool() {

    override fun getDisplayName(): String = "Discarded function return value"
    override fun getGroupDisplayName(): String = "Pell"
    override fun getShortName(): String = "PellDiscardedReturn"
    override fun isEnabledByDefault(): Boolean = true

    override fun buildVisitor(holder: ProblemsHolder, isOnTheFly: Boolean): PsiElementVisitor {
        return object : PsiElementVisitor() {
            override fun visitElement(element: PsiElement) {
                if (element !is PellExprOrAssignStmt) return
                // Skip assignment statements — they bind the value, no discard.
                if (element.node.findChildByType(PellElementTypes.EQ) != null) return
                // Skip if there's no call in the expression.
                PsiTreeUtil.findChildOfType(element, PellCallOp::class.java) ?: return

                holder.registerProblem(
                    element,
                    "Function call result is discarded — assign it or iterate it (Oracle PL/SQL can't call a function as a statement)",
                    ProblemHighlightType.WARNING,
                    AssignToLocalFix,
                    WrapInForLoopFix,
                )
            }
        }
    }
}

/**
 * Quick-fix: replace `expr;` with `let result = expr;` and leave the
 * caret on `result` for in-place rename.
 */
private object AssignToLocalFix : LocalQuickFix {
    override fun getFamilyName(): String = "Assign to local variable"

    override fun applyFix(project: Project, descriptor: ProblemDescriptor) {
        val stmt = descriptor.psiElement as? PellExprOrAssignStmt ?: return
        val file = stmt.containingFile
        val doc = PsiDocumentManager.getInstance(project).getDocument(file) ?: return
        val pm = PsiDocumentManager.getInstance(project)
        pm.commitDocument(doc)
        val originalText = stmt.text.trimEnd(';', ' ', '\n')
        doc.replaceString(
            stmt.textRange.startOffset,
            stmt.textRange.endOffset,
            "let result = $originalText;",
        )
        pm.commitDocument(doc)
    }
}

/**
 * Quick-fix: replace `expr;` with `for row in expr { /* TODO */ }`. Only
 * makes semantic sense if `expr` returns a list, but the user can pick
 * the other quick-fix if it doesn't.
 */
private object WrapInForLoopFix : LocalQuickFix {
    override fun getFamilyName(): String = "Wrap in for-loop"

    override fun applyFix(project: Project, descriptor: ProblemDescriptor) {
        val stmt = descriptor.psiElement as? PellExprOrAssignStmt ?: return
        val file = stmt.containingFile
        val doc = PsiDocumentManager.getInstance(project).getDocument(file) ?: return
        val pm = PsiDocumentManager.getInstance(project)
        pm.commitDocument(doc)
        val originalText = stmt.text.trimEnd(';', ' ', '\n')
        doc.replaceString(
            stmt.textRange.startOffset,
            stmt.textRange.endOffset,
            "for row in $originalText {\n    /* TODO */\n}",
        )
        pm.commitDocument(doc)
    }
}
