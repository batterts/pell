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
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellExprOrAssignStmt
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellModuleDecl
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
 *   1. The stmt starts with `<name>(` (unqualified) or `<a::b::c>(`
 *      (qualified) — i.e. its outermost expression IS a call. Member
 *      access `obj.method(` is skipped because resolution needs type
 *      inference we don't have yet.
 *   2. Resolution differs by qualification:
 *        - Unqualified `name(`: must resolve to a fn IN THE SAME FILE.
 *          Cross-file unqualified matches are ambiguous and the user
 *          would have to qualify them anyway, so we don't guess.
 *        - Qualified `mod::name(`: scan the project for a fn named
 *          `name` whose containing file's `module` declaration ends
 *          with `.mod` (matching pell's "module tail as qualifier"
 *          convention, e.g. `module pell_test.hello;` makes its fns
 *          callable as `hello::fn_name`).
 *   3. That fn declaration carries a `-> T` clause whose T is not
 *      `Unit` — i.e. it's a function, not a procedure.
 *
 * Procedures (no `-> T` or `-> Unit`), unresolved callees (`p`, stdlib
 * `dbms_output::*` / `catalog::*`, user-side stdlib modules like
 * `logger::info` not in the project), and member-access calls are
 * deliberately NOT flagged. Conservative stance — better to miss a
 * real function-discard than to nag on every legit call.
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

                // 1. Extract the leading callee from the stmt's text.
                val text = element.text.trimEnd(';', ' ', '\n').trim()
                val match = LEADING_CALL_RE.find(text) ?: return
                val fullCallee = match.groupValues[1]

                // Skip member-access calls (obj.method) — resolution needs
                // type inference we don't have yet.
                if ('.' in fullCallee) return

                // 2. Resolve.
                val callerFile = element.containingFile as? PellFile ?: return
                val target: PellFnDef = if ("::" in fullCallee) {
                    val parts = fullCallee.split("::")
                    val qualifier = parts.dropLast(1).joinToString(".")
                    val fnName = parts.last()
                    resolveQualified(element.project, qualifier, fnName) ?: return
                } else {
                    resolveInFile(callerFile, fullCallee) ?: return
                }

                // 3. Function vs procedure: a `-> T` child (PellTypeRef) means
                //    function. No PellTypeRef means procedure (no return value)
                //    so calling it as a stmt is fine.
                val returnType = PsiTreeUtil.findChildOfType(target, PellTypeRef::class.java) ?: return
                if (returnType.text.trim() == "Unit") return

                val displayName = fullCallee
                holder.registerProblem(
                    element,
                    "Function `$displayName` returns `${returnType.text.trim()}` — discarding it lowers to PLS-00221 at deploy (Oracle PL/SQL can't call a function as a statement)",
                    ProblemHighlightType.WARNING,
                    AssignToLocalFix,
                    WrapInForLoopFix,
                )
            }
        }
    }

    /** Resolve a bare `name(` against fns declared in the SAME file as
     *  the caller. Cross-file unqualified calls are ambiguous in pell
     *  and the user would have to qualify them anyway. */
    private fun resolveInFile(file: PellFile, fnName: String): PellFnDef? =
        PsiTreeUtil.findChildrenOfType(file, PellFnDef::class.java)
            .firstOrNull { it.name == fnName }

    /** Resolve `qualifier::name(` against the project. The qualifier
     *  matches a module if it equals the module's full dotted name
     *  (`pell_test.hello`) or its tail (`hello` matches the same
     *  module). Returns null if no project module matches — the call
     *  must be to a stdlib / external / runtime symbol. */
    private fun resolveQualified(project: Project, qualifier: String, fnName: String): PellFnDef? =
        PellSymbolScanner.findPubFns(project, fnName)
            .firstOrNull { fn ->
                val module = PsiTreeUtil.findChildOfType(fn.containingFile, PellModuleDecl::class.java)
                val moduleName = module?.name ?: return@firstOrNull false
                moduleName == qualifier || moduleName.endsWith(".$qualifier")
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
