package dev.pell.intellij.refactor.paramsToRecord

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import dev.pell.intellij.PellFile

/**
 * Action mounted under `Refactor This ▸ Parameters to Record`. Routes
 * to [PellParamsToRecordHandler] which does the work.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellParamsToRecordAction : AnAction() {

    override fun update(e: AnActionEvent) {
        val file = e.getData(CommonDataKeys.PSI_FILE)
        e.presentation.isEnabledAndVisible = file is PellFile
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) ?: return
        PellParamsToRecordHandler().invoke(project, editor, file, e.dataContext)
    }
}
