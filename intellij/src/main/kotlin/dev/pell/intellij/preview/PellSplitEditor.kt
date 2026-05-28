package dev.pell.intellij.preview

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.editor.event.DocumentEvent
import com.intellij.openapi.editor.event.DocumentListener
import com.intellij.openapi.fileEditor.TextEditor
import com.intellij.openapi.fileEditor.TextEditorWithPreview
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile

/**
 * Markdown-style split editor for `.pell` files:
 *
 *   ┌──────────────┬──────────────┐
 *   │ src/foo.pell │ PL/SQL       │
 *   │ (editable)   │ (live preview)│
 *   └──────────────┴──────────────┘
 *
 * IntelliJ's [TextEditorWithPreview] provides the top-right toggle
 * (text / split / preview). We wire up:
 *
 *   - A DocumentListener on the source that schedules a debounced
 *     `pell build` whenever the buffer changes.
 *   - The result feeds back into the right pane via the preview's
 *     `setSql` / `setError` methods.
 *
 * Initial compile fires from `selectNotify` so opening the file
 * populates the preview before the user types anything.
 */
class PellSplitEditor(
    private val project: Project,
    private val sourceFile: VirtualFile,
    textEditor: TextEditor,
    private val pellPreview: PellPreviewEditor,
) : TextEditorWithPreview(textEditor, pellPreview, "pell", Layout.SHOW_EDITOR_AND_PREVIEW) {

    private val service = PellCompileService.getInstance(project)
    private val documentListener: DocumentListener

    init {
        // Recompile on every meaningful edit. The service debounces, so
        // rapid keystrokes coalesce into a single compile.
        val doc = textEditor.editor.document
        documentListener = object : DocumentListener {
            override fun documentChanged(event: DocumentEvent) {
                triggerCompile()
            }
        }
        doc.addDocumentListener(documentListener, this)
        // Kick off the first compile as soon as the editor is shown.
        ApplicationManager.getApplication().invokeLater { triggerCompile(delayMs = 0) }
    }

    private fun triggerCompile(delayMs: Int = 400) {
        val content = textEditor.editor.document.text
        service.schedule(sourceFile.path, content, delayMs) { result ->
            ApplicationManager.getApplication().invokeLater {
                when (result) {
                    is CompileResult.Ok -> pellPreview.setSql(result.sql)
                    is CompileResult.Error -> pellPreview.setError(result.message)
                }
            }
        }
    }
}
