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
 * Cmd-N ▸ "Field" — insert a `name: type,` line into a pell record.
 *
 * v0 behaviour (caret-only) was unfriendly — it hid the action unless
 * the caret was literally between `{` and `}` of a record body. v0.4
 * is always visible inside a pell file:
 *
 *   - Caret inside a record body → insert there.
 *   - Caret elsewhere → pop a chooser listing every record in the file
 *     so the user picks one.
 *   - No records in the file → show a friendly error.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellGenerateFieldAction : AnAction() {

    override fun update(e: AnActionEvent) {
        val file = e.getData(CommonDataKeys.PSI_FILE)
        e.presentation.isEnabledAndVisible = file is PellFile
        e.presentation.text = "Field"
        e.presentation.description = "Add a field to a record"
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) as? PellFile ?: return

        // 1. Pick the target record. Caret-first, then chooser fallback.
        val elem = file.findElementAt(editor.caretModel.offset)
        val recordAtCaret = elem?.let { PsiTreeUtil.getParentOfType(it, PellRecordDef::class.java) }
        val record = recordAtCaret ?: pickRecord(project, file) ?: return

        val fieldName = Messages.showInputDialog(project, "Field name:", "Generate Field", null, "new_field", null) ?: return
        val fieldType = Messages.showInputDialog(project, "Field type:", "Generate Field", null, "text", null) ?: return

        WriteCommandAction.runWriteCommandAction(project, "Generate Field '$fieldName'", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val doc = pm.getDocument(file) ?: return@Runnable
            pm.commitDocument(doc)
            val closeBraceOffset = record.textRange.startOffset + record.text.lastIndexOf('}')
            doc.insertString(closeBraceOffset, "    $fieldName: $fieldType,\n")
            pm.commitDocument(doc)
        })
    }

    private fun pickRecord(project: com.intellij.openapi.project.Project, file: PellFile): PellRecordDef? {
        val records = PsiTreeUtil.findChildrenOfType(file, PellRecordDef::class.java).toList()
        if (records.isEmpty()) {
            Messages.showInfoMessage(
                project,
                "No `pub record` declarations found in this file. Add a record first, then run Generate Field again.",
                "Generate Field",
            )
            return null
        }
        if (records.size == 1) return records[0]

        val names = records.map { it.name ?: "(unnamed)" }.toTypedArray()
        val chosen = Messages.showEditableChooseDialog(
            "Which record to add the field to?",
            "Generate Field",
            null,
            names,
            names.first(),
            null,
        ) ?: return null
        return records.firstOrNull { it.name == chosen }
    }
}
