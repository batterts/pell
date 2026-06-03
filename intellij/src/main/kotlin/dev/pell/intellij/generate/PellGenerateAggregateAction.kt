package dev.pell.intellij.generate

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import dev.pell.intellij.PellFile

/**
 * Cmd-N ▸ "Aggregate" — insert a full pell aggregate skeleton with
 * `state {} step() {} merge() {} finish() -> T {}` at the caret. The
 * user picks a name, an input element type, and a result type;
 * everything else is filled with sensible idiomatic stubs.
 *
 * The template lowers cleanly through the compiler — `pell build`
 * produces a working PL/SQL ODCI aggregate from it, so the user can
 * run an `sql!{}` query against `<name>(col)` immediately after a
 * couple of edits.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellGenerateAggregateAction : AnAction() {

    override fun update(e: AnActionEvent) {
        val file = e.getData(CommonDataKeys.PSI_FILE)
        e.presentation.isEnabledAndVisible = file is PellFile
        e.presentation.text = "Aggregate"
        e.presentation.description = "Insert a pell aggregate skeleton (state / step / merge / finish)"
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) as? PellFile ?: return

        val name = Messages.showInputDialog(project, "Aggregate name (snake_case):", "Generate Aggregate", null, "my_agg", null) ?: return
        val elemType = Messages.showInputDialog(project, "Input element type:", "Generate Aggregate", null, "number", null) ?: return
        val resultType = Messages.showInputDialog(project, "Result type:", "Generate Aggregate", null, "number", null) ?: return

        val skeleton = """
            |pub aggregate $name(value: $elemType) -> $resultType {
            |    state {
            |        accum: $resultType = 0,
            |        count: number = 0,
            |    }
            |
            |    step(value: $elemType) {
            |        self.accum = self.accum + value;
            |        self.count = self.count + 1;
            |    }
            |
            |    merge(other: Self) {
            |        self.accum = self.accum + other.accum;
            |        self.count = self.count + other.count;
            |    }
            |
            |    finish() -> $resultType {
            |        return self.accum;
            |    }
            |}
        """.trimMargin()

        WriteCommandAction.runWriteCommandAction(project, "Generate Aggregate '$name'", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val doc = pm.getDocument(file) ?: return@Runnable
            pm.commitDocument(doc)
            val insertOffset = editor.caretModel.offset
            doc.insertString(insertOffset, "\n$skeleton\n")
            pm.commitDocument(doc)
        })
    }
}
