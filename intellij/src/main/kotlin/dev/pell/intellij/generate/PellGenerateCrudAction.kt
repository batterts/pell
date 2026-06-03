package dev.pell.intellij.generate

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellFieldDef
import dev.pell.intellij.psi.PellRecordDef
import dev.pell.intellij.psi.PellTypeRef

/**
 * Cmd-N ▸ "CRUD for record" — given a `pub record`, generate the four
 * standard table operations:
 *
 *   pub fn select_<record>(id) -> Result<Record, NotFound>
 *   pub fn insert_<record>(r: Record)
 *   pub fn update_<record>(r: Record)
 *   pub fn delete_<record>(id)
 *
 * Each body uses sql!{ } against a user-chosen table name (default:
 * lowercase record name + "s") and the user-chosen primary key field
 * (default: first field whose name starts with "id").
 *
 * If the caret is inside a record body, that's the target. Otherwise
 * the user picks from a chooser of records in the file.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellGenerateCrudAction : AnAction() {

    override fun update(e: AnActionEvent) {
        val file = e.getData(CommonDataKeys.PSI_FILE)
        e.presentation.isEnabledAndVisible = file is PellFile
        e.presentation.text = "CRUD for record"
        e.presentation.description = "Generate insert/select/update/delete fns for a record"
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) as? PellFile ?: return

        val elem = file.findElementAt(editor.caretModel.offset)
        val recordAtCaret = elem?.let { PsiTreeUtil.getParentOfType(it, PellRecordDef::class.java) }
        val record = recordAtCaret ?: pickRecord(project, file) ?: return

        val recordName = record.name ?: return
        val fields = PsiTreeUtil.findChildrenOfType(record, PellFieldDef::class.java).toList()
        if (fields.isEmpty()) {
            Messages.showInfoMessage(project, "Record `$recordName` has no fields yet — add at least one before generating CRUD.", "Generate CRUD")
            return
        }

        // Heuristics for table name + primary key. Both editable in the dialog.
        val defaultTable = recordName.lowercase() + "s"
        val table = Messages.showInputDialog(project, "Table name:", "Generate CRUD", null, defaultTable, null) ?: return
        val defaultPk = fields.firstOrNull { it.name?.startsWith("id", ignoreCase = true) == true }?.name ?: fields.first().name ?: "id"
        val pk = Messages.showInputDialog(project, "Primary key field:", "Generate CRUD", null, defaultPk, null) ?: return
        val pkType = fields.firstOrNull { it.name == pk }?.let { fieldTypeText(it) } ?: "number"

        val nonPkFields = fields.filter { it.name != pk }
        val allColumns = fields.mapNotNull { it.name }
        val crud = buildCrud(recordName, table, pk, pkType, allColumns, nonPkFields.mapNotNull { it.name })

        WriteCommandAction.runWriteCommandAction(project, "Generate CRUD for `$recordName`", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val doc = pm.getDocument(file) ?: return@Runnable
            pm.commitDocument(doc)
            // Insert after the record declaration.
            val insertOffset = record.textRange.endOffset
            doc.insertString(insertOffset, "\n\n$crud")
            pm.commitDocument(doc)
        })
    }

    private fun buildCrud(
        recordName: String,
        table: String,
        pk: String,
        pkType: String,
        allColumns: List<String>,
        nonPkFields: List<String>,
    ): String {
        val recordLowered = recordName.lowercase()
        val columnList = allColumns.joinToString(", ")
        val placeholders = allColumns.joinToString(", ") { ":r.$it" }
        val setClauses = nonPkFields.joinToString(",\n               ") { "$it = :r.$it" }
        return """
            |pub error ${recordName}NotFound { $pk: $pkType }
            |
            |pub fn select_${recordLowered}($pk: $pkType) -> Result<$recordName, ${recordName}NotFound> {
            |    let row: $recordName = sql! {
            |        select $columnList from $table where $pk = :$pk
            |    }.one()?;
            |    return Ok(row);
            |}
            |
            |pub fn insert_${recordLowered}(r: $recordName) {
            |    sql! {
            |        insert into $table ($columnList) values ($placeholders)
            |    }.execute();
            |}
            |
            |pub fn update_${recordLowered}(r: $recordName) {
            |    sql! {
            |        update $table
            |           set $setClauses
            |         where $pk = :r.$pk
            |    }.execute();
            |}
            |
            |pub fn delete_${recordLowered}($pk: $pkType) {
            |    sql! {
            |        delete from $table where $pk = :$pk
            |    }.execute();
            |}
        """.trimMargin()
    }

    private fun pickRecord(project: Project, file: PellFile): PellRecordDef? {
        val records = PsiTreeUtil.findChildrenOfType(file, PellRecordDef::class.java).toList()
        if (records.isEmpty()) {
            Messages.showInfoMessage(project, "No `pub record` declarations in this file.", "Generate CRUD")
            return null
        }
        if (records.size == 1) return records[0]
        val names = records.map { it.name ?: "(unnamed)" }.toTypedArray()
        val chosen = Messages.showEditableChooseDialog(
            "Generate CRUD for which record?",
            "Generate CRUD",
            null,
            names,
            names.first(),
            null,
        ) ?: return null
        return records.firstOrNull { it.name == chosen }
    }

    private fun fieldTypeText(f: PellFieldDef): String =
        PsiTreeUtil.findChildOfType(f, PellTypeRef::class.java)?.text ?: "any"
}
