package dev.pell.intellij.generate

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellRecordDef

/**
 * Cmd-N ▸ "Field" — inside a `pub record { ... }`, insert a new
 * `<name>: <type>,` line right before the closing brace.
 *
 *     pub record Employee {
 *         id: number,
 *         name: text,        ← caret here
 *     }
 *     Cmd-N → Field → "salary" : "number"  →
 *     pub record Employee {
 *         id: number,
 *         name: text,
 *         salary: number,
 *     }
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellGenerateFieldAction : AnAction() {

    override fun update(e: AnActionEvent) {
        val file = e.getData(CommonDataKeys.PSI_FILE) ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        if (file !is PellFile) {
            e.presentation.isEnabledAndVisible = false
            return
        }
        val elem = file.findElementAt(editor.caretModel.offset)
        val inRecord = PsiTreeUtil.getParentOfType(elem, PellRecordDef::class.java) != null
        e.presentation.isEnabledAndVisible = inRecord
        e.presentation.text = "Field"
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) as? PellFile ?: return
        val elem = file.findElementAt(editor.caretModel.offset) ?: return
        val record = PsiTreeUtil.getParentOfType(elem, PellRecordDef::class.java) ?: return

        val fieldName = Messages.showInputDialog(project, "Field name:", "Generate Field", null, "new_field", null) ?: return
        val fieldType = Messages.showInputDialog(project, "Field type:", "Generate Field", null, "text", null) ?: return

        WriteCommandAction.runWriteCommandAction(project, "Generate Field '$fieldName'", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val doc = pm.getDocument(file) ?: return@Runnable
            pm.commitDocument(doc)
            // Find the closing `}` of the record body.
            val recordText = record.text
            val closeBraceOffset = record.textRange.startOffset + recordText.lastIndexOf('}')
            // Insert before the `}` preserving indentation.
            doc.insertString(closeBraceOffset, "    $fieldName: $fieldType,\n")
            pm.commitDocument(doc)
        })
    }
}
