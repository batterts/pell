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
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellExprOrAssignStmt
import dev.pell.intellij.symbols.PellSymbolInfo
import dev.pell.intellij.symbols.PellSymbolService
import dev.pell.intellij.symbols.producesValue

/**
 * Warns when a bare expression-statement is a function call whose
 * return value is discarded:
 *
 *     user_tables();          // ← yellow squiggle
 *
 * Discarded function returns are a compile-time error in Oracle PL/SQL
 * (a function can't be called as a statement; that's procedure syntax).
 *
 * Quick-fixes (Alt-Enter):
 *   - "Assign to local variable" → `let result = user_tables();`
 *   - "Wrap in for-loop"          → `for row in user_tables() { /* TODO */ }`
 *
 * Resolution routes entirely through [PellSymbolService] as of L1 — the
 * inspection itself no longer carries qualifier-resolution logic. The
 * facade handles same-file-vs-cross-file scoping, qualified paths,
 * and (in L3+) the stdlib catalog.
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
                if (element.node.findChildByType(PellElementTypes.EQ) != null) return

                val text = element.text.trimEnd(';', ' ', '\n').trim()
                val match = LEADING_CALL_RE.find(text) ?: return
                val fullCallee = match.groupValues[1]

                // Skip member-access calls — needs type inference (L4).
                if ('.' in fullCallee) return

                val callerFile = element.containingFile as? PellFile ?: return
                val service = PellSymbolService.getInstance(element.project)
                val target = resolve(service, callerFile, fullCallee) ?: return

                // Function vs procedure: producesValue() returns true exactly
                // when the symbol's type is non-Unit and non-Unknown.
                val ret = target.type ?: return
                if (!ret.producesValue()) return

                holder.registerProblem(
                    element,
                    "Function `$fullCallee` returns `${ret.displayName()}` — discarding it lowers to PLS-00221 at deploy (Oracle PL/SQL can't call a function as a statement)",
                    ProblemHighlightType.WARNING,
                    AssignToLocalFix,
                    WrapInForLoopFix,
                )
            }

            private fun resolve(
                service: PellSymbolService,
                callerFile: PellFile,
                fullCallee: String,
            ): PellSymbolInfo? {
                if ("::" in fullCallee) {
                    // Qualified path: the facade does qualifier-tail matching.
                    return service.findQualified(fullCallee)
                }
                // Unqualified: must be in the caller's file.
                return service.findPubFns(fullCallee).firstOrNull { sym ->
                    sym.location?.containingFile == callerFile
                }
            }
        }
    }

    companion object {
        private val LEADING_CALL_RE = Regex("""^([A-Za-z_][A-Za-z0-9_.:]*)\s*\(""")
    }
}

private object AssignToLocalFix : LocalQuickFix {
    override fun getFamilyName(): String = "Assign to local variable"

    override fun applyFix(project: Project, descriptor: ProblemDescriptor) {
        val stmt = descriptor.psiElement as? PellExprOrAssignStmt ?: return
        val file = stmt.containingFile
        val doc = PsiDocumentManager.getInstance(project).getDocument(file) ?: return
        val pm = PsiDocumentManager.getInstance(project)
        pm.commitDocument(doc)
        val originalText = stmt.text.trimEnd(';', ' ', '\n')
        doc.replaceString(stmt.textRange.startOffset, stmt.textRange.endOffset, "let result = $originalText;")
        pm.commitDocument(doc)
    }
}

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
