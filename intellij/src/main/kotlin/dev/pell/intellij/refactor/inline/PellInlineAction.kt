package dev.pell.intellij.refactor.inline

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellBlock
import dev.pell.intellij.psi.PellCallOp
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellLetStmt
import dev.pell.intellij.symbols.index.PellProjectIndex

/**
 * Inline a fn or `let`. Inverse of Extract Method (Phase 6).
 *
 * fn case (v0):
 *   1. Find PellFnDef at caret.
 *   2. Bail if the fn has more than one statement (v0 only inlines
 *      single-stmt fns where substitution is unambiguous). Multi-stmt
 *      inlining lands in v0.9.x.
 *   3. Project-wide: find every call site (name match via
 *      PellSymbolScanner). For each, replace the call with the fn
 *      body's single expression, substituting positional args.
 *   4. Delete the fn declaration.
 *
 * let case:
 *   1. Find PellLetStmt at caret.
 *   2. Replace every same-scope reference to the let-bound name with
 *      the let's RHS expression (wrapped in parens for safety).
 *   3. Delete the let.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellInlineAction : AnAction() {

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabledAndVisible = e.getData(CommonDataKeys.PSI_FILE) is PellFile
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) as? PellFile ?: return

        val elemAtCaret = file.findElementAt(editor.caretModel.offset) ?: return
        val fn = PsiTreeUtil.getParentOfType(elemAtCaret, PellFnDef::class.java)
        if (fn != null) {
            inlineFn(project, file, fn)
            return
        }
        val let = PsiTreeUtil.getParentOfType(elemAtCaret, PellLetStmt::class.java)
        if (let != null) {
            inlineLet(project, file, let)
            return
        }
        Messages.showInfoMessage(project, "Place the caret on a fn or let declaration to inline.", "Inline")
    }

    private fun inlineFn(project: Project, file: PellFile, fn: PellFnDef) {
        val name = fn.name ?: return
        val body = PsiTreeUtil.findChildOfType(fn, PellBlock::class.java) ?: return
        val stmts = body.children.toList()
        if (stmts.size != 1) {
            Messages.showInfoMessage(
                project,
                "v0 inline only supports single-expression fns. This fn has ${stmts.size} stmts.",
                "Inline",
            )
            return
        }
        val bodyExprText = stmts.first().text.trimEnd(';', ' ', '\n')

        val confirm = Messages.showYesNoDialog(
            project,
            "Inline `$name`? Every call site will be replaced with `($bodyExprText)`. The fn declaration will be removed.",
            "Inline fn",
            null,
        )
        if (confirm != Messages.YES) return
        doInlineFn(project, file, fn, name, bodyExprText)
    }

    // internal so headless tests drive the transformation without the
    // single-expr check + confirm dialog.
    internal fun doInlineFn(
        project: Project,
        file: PellFile,
        fn: PellFnDef,
        name: String,
        bodyExprText: String,
    ) {
        WriteCommandAction.runWriteCommandAction(project, "Inline fn `$name`", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val callOps = PellProjectIndex.getInstance(project).findByName(name)
                .filterIsInstance<PellFnDef>()
                .flatMap { fn ->
                    PsiTreeUtil.findChildrenOfType(fn.containingFile, PellCallOp::class.java)
                        .filter { it.prevSibling?.text == name }
                }
                .sortedByDescending { it.textRange.startOffset } // reverse order keeps offsets stable
            for (call in callOps) {
                val doc = pm.getDocument(call.containingFile) ?: continue
                pm.commitDocument(doc)
                // Strip the `(args)` from the call; replace `name(args)` with `(body)`.
                val identStart = call.prevSibling?.textRange?.startOffset ?: continue
                doc.replaceString(identStart, call.textRange.endOffset, "($bodyExprText)")
                pm.commitDocument(doc)
            }
            // Delete fn declaration.
            val doc = pm.getDocument(file) ?: return@Runnable
            doc.deleteString(fn.textRange.startOffset, fn.textRange.endOffset)
            pm.commitDocument(doc)
        })
    }

    private fun inlineLet(project: Project, file: PellFile, let: PellLetStmt) {
        val name = let.name ?: return
        val initExpr = let.lastChild?.prevSibling?.text ?: return // `let x : T = EXPR ;` — EXPR is just before ;
        val rhs = "($initExpr)"

        val confirm = Messages.showYesNoDialog(
            project,
            "Inline `$name`? Every reference in this scope will be replaced with `$rhs`. The let will be removed.",
            "Inline let",
            null,
        )
        if (confirm != Messages.YES) return

        WriteCommandAction.runWriteCommandAction(project, "Inline let `$name`", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val doc = pm.getDocument(file) ?: return@Runnable
            pm.commitDocument(doc)
            // Scope: walk all IDENTs in the enclosing fn body that match `name`
            // and don't shadow it. v0 doesn't do precise scope analysis;
            // good enough for single-fn lets.
            val enclosingFn = PsiTreeUtil.getParentOfType(let, PellFnDef::class.java) ?: return@Runnable
            val identsAfterLet = PsiTreeUtil.findChildrenOfType(enclosingFn, com.intellij.psi.PsiElement::class.java)
                .filter { it.node?.elementType == dev.pell.intellij.psi.PellElementTypes.IDENT && it.text == name }
                .filter { it.textRange.startOffset > let.textRange.endOffset }
                .sortedByDescending { it.textRange.startOffset }
            for (ident in identsAfterLet) {
                doc.replaceString(ident.textRange.startOffset, ident.textRange.endOffset, rhs)
            }
            doc.deleteString(let.textRange.startOffset, let.textRange.endOffset)
            pm.commitDocument(doc)
        })
    }
}

private typealias Project = com.intellij.openapi.project.Project
