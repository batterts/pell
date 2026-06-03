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
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellExprOrAssignStmt
import dev.pell.intellij.psi.PellSymbolScanner
import dev.pell.intellij.psi.PellTypeRef

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
 * Detection rule: only flag a bare expression-statement when we can
 * prove the callee returns a value. Specifically:
 *
 *   1. The stmt starts with `<name>(` or `<qualified::name>(` /
 *      `<obj.method>(` — i.e. its outermost expression IS a call.
 *   2. The callee name resolves to a [dev.pell.intellij.psi.PellFnDef]
 *      somewhere in the project.
 *   3. That fn declaration carries a `-> T` clause whose T is not
 *      `Unit` — i.e. it's a function, not a procedure.
 *
 * Procedures (no `-> T` or `-> Unit`) and unresolved callees (built-ins
 * like `p(...)`, `dbms_output::put_line(...)`, `catalog::*` that aren't
 * in the user's project) are deliberately NOT flagged. Conservative
 * stance — better to miss a real function-discard than to nag on every
 * procedure call.
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

                // 1. Extract the leading callee name from the stmt's text.
                //    Matches `name(`, `path::name(`, `obj.method(` at the start.
                val text = element.text.trimEnd(';', ' ', '\n').trim()
                val match = LEADING_CALL_RE.find(text) ?: return
                val fullCallee = match.groupValues[1]
                val calleeName = fullCallee.substringAfterLast("::").substringAfterLast(".")

                // 2. Resolve to a fn in the project. Built-ins (p, catalog::*,
                //    dbms_output::*, anything outside the user's pell sources)
                //    won't resolve — skip them rather than guess.
                val target = PellSymbolScanner.findPubFns(element.project, calleeName).firstOrNull() ?: return

                // 3. Function vs procedure: a `-> T` child (PellTypeRef) means
                //    function. No PellTypeRef means procedure (no return value)
                //    so calling it as a stmt is fine.
                val returnType = PsiTreeUtil.findChildOfType(target, PellTypeRef::class.java) ?: return
                if (returnType.text.trim() == "Unit") return

                holder.registerProblem(
                    element,
                    "Function `$calleeName` returns `${returnType.text.trim()}` — discarding it lowers to PLS-00221 at deploy (Oracle PL/SQL can't call a function as a statement)",
                    ProblemHighlightType.WARNING,
                    AssignToLocalFix,
                    WrapInForLoopFix,
                )
            }
        }
    }

    companion object {
        private val LEADING_CALL_RE = Regex("""^([A-Za-z_][A-Za-z0-9_.:]*)\s*\(""")
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
