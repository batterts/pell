package dev.pell.intellij.generate

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellTypeDef
import dev.pell.intellij.psi.PellSealedTypeDef

/**
 * Cmd-N ▸ "Method" — inserts a `pub fn <name>(...) -> <ret> { }` skeleton
 * at the appropriate position:
 *
 *   - Inside a `type` / `sealed type` body: a method declaration
 *   - At top level: a free fn
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellGenerateMethodAction : AnAction() {

    override fun update(e: AnActionEvent) {
        val file = e.getData(CommonDataKeys.PSI_FILE) ?: return
        if (file !is PellFile) {
            e.presentation.isEnabledAndVisible = false
            return
        }
        e.presentation.isEnabledAndVisible = true
        e.presentation.text = "Method"
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) as? PellFile ?: return
        val elem = file.findElementAt(editor.caretModel.offset) ?: return

        val name = Messages.showInputDialog(project, "Method name:", "Generate Method", null, "do_thing", null) ?: return
        val params = Messages.showInputDialog(project, "Params (e.g. `id: number, name: text`):", "Generate Method", null, "", null) ?: return
        val ret = Messages.showInputDialog(project, "Return type (blank for none):", "Generate Method", null, "text", null) ?: return

        val returnClause = if (ret.isBlank()) "" else " -> $ret"
        val skeleton = "pub fn $name($params)$returnClause {\n    /* TODO */\n}\n"

        WriteCommandAction.runWriteCommandAction(project, "Generate Method '$name'", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val doc = pm.getDocument(file) ?: return@Runnable
            pm.commitDocument(doc)

            // Pick the insertion point. Inside a (sealed) type body — insert
            // before its closing `}`. Otherwise — after the enclosing fn,
            // or at end of file.
            val typeBody = PsiTreeUtil.getParentOfType(elem, PellSealedTypeDef::class.java, PellTypeDef::class.java)
            val enclosingFn = PsiTreeUtil.getParentOfType(elem, PellFnDef::class.java)
            val insertOffset = when {
                typeBody != null -> typeBody.textRange.startOffset + typeBody.text.lastIndexOf('}')
                enclosingFn != null -> enclosingFn.textRange.endOffset
                else -> doc.textLength
            }
            doc.insertString(insertOffset, "\n$skeleton\n")
            pm.commitDocument(doc)
        })
    }
}
