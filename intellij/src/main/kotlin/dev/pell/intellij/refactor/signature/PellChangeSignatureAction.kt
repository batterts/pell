package dev.pell.intellij.refactor.signature

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellBlock
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellMethodDef

/**
 * Change a fn's signature — params and/or return type. v0 surfaces the
 * current signature in a text editor, lets the user edit it freely,
 * and writes it back atomically. Call-site propagation is deferred
 * (the user gets to fix call sites in a separate Rename pass via the
 * inspector hints that fire on unresolved refs).
 *
 * Why text-edit and not a structured dialog: pell's type system is
 * rich (generics, optional, error unions, refs to user-defined records).
 * A structured editor for arbitrary types is its own project. The
 * free-form text approach lets users write any legal signature and
 * the parser validates on save.
 *
 * v0.x roadmap:
 *   - v0.9.x: structured editor for the common cases (add/remove/reorder
 *     params, change return type)
 *   - v1.x: type-safe call-site propagation when the new signature is
 *     a strict extension/permutation of the old one
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellChangeSignatureAction : AnAction() {

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabledAndVisible = e.getData(CommonDataKeys.PSI_FILE) is PellFile
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) as? PellFile ?: return

        val elemAtCaret = file.findElementAt(editor.caretModel.offset) ?: return
        val fn = PsiTreeUtil.getParentOfType(elemAtCaret, PellFnDef::class.java, PellMethodDef::class.java)
            ?: return Messages.showInfoMessage(project, "Place the caret in a fn or method.", "Change Signature")

        // The "signature" is everything between the fn keyword and the
        // opening `{` of the body (or trailing `;` for abstract methods).
        val body = PsiTreeUtil.findChildOfType(fn, PellBlock::class.java)
        val signatureEnd = body?.textRange?.startOffset ?: fn.textRange.endOffset
        val signatureStart = fn.textRange.startOffset
        val signatureText = fn.text.substring(0, signatureEnd - signatureStart).trimEnd()

        val newSignature = Messages.showMultilineInputDialog(
            project,
            "Edit the signature. The body is left untouched.",
            "Change Signature",
            signatureText,
            null,
            null,
        ) ?: return

        WriteCommandAction.runWriteCommandAction(project, "Change Signature", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val doc = pm.getDocument(file) ?: return@Runnable
            pm.commitDocument(doc)
            doc.replaceString(signatureStart, signatureEnd, newSignature + (if (body != null) " " else ""))
            pm.commitDocument(doc)
        })
    }
}
