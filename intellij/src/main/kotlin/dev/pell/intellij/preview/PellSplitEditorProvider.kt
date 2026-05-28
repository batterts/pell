package dev.pell.intellij.preview

import com.intellij.openapi.fileEditor.FileEditor
import com.intellij.openapi.fileEditor.FileEditorPolicy
import com.intellij.openapi.fileEditor.FileEditorProvider
import com.intellij.openapi.fileEditor.TextEditor
import com.intellij.openapi.fileEditor.impl.text.TextEditorProvider
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile

/**
 * Registers our split editor as the default for `.pell` files —
 * IntelliJ opens new pell sources directly into the split view.
 *
 * Policy is `HIDE_DEFAULT_EDITOR` so we replace (not augment) the
 * standard text editor. The user can still get text-only or
 * preview-only via the toggle buttons in the editor's top-right.
 *
 * Same surface Markdown uses (`org.intellij.plugins.markdown
 * .editor.MarkdownSplitEditorProvider`).
 */
class PellSplitEditorProvider : FileEditorProvider, DumbAware {

    override fun accept(project: Project, file: VirtualFile): Boolean =
        file.extension?.lowercase() == "pell"

    override fun createEditor(project: Project, file: VirtualFile): FileEditor {
        // Use the default TextEditorProvider — it's the PSI-aware one
        // for files with a registered language. Instantiating
        // PsiAwareTextEditorProvider directly is flagged as internal.
        val textEditor = TextEditorProvider.getInstance().createEditor(project, file) as TextEditor
        val preview = PellPreviewEditor(project, file)
        return PellSplitEditor(project, file, textEditor, preview)
    }

    override fun getEditorTypeId(): String = "pell-with-preview"

    override fun getPolicy(): FileEditorPolicy = FileEditorPolicy.HIDE_DEFAULT_EDITOR
}
