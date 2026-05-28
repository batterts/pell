package dev.pell.intellij.preview

import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.EditorFactory
import com.intellij.openapi.editor.ex.EditorEx
import com.intellij.openapi.editor.highlighter.EditorHighlighterFactory
import com.intellij.openapi.fileEditor.FileEditor
import com.intellij.openapi.fileEditor.FileEditorLocation
import com.intellij.openapi.fileEditor.FileEditorState
import com.intellij.openapi.fileTypes.FileTypeManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.UserDataHolderBase
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.testFramework.LightVirtualFile
import com.intellij.ui.JBColor
import java.beans.PropertyChangeListener
import javax.swing.JComponent

/**
 * The right-hand "preview" pane in the split editor. Owns a read-only
 * IntelliJ editor showing the PL/SQL output of `pell build` on the
 * left-hand source.
 *
 * Live state is driven by the parent [PellSplitEditor], which listens
 * to the source document and calls [setSql] / [setError] when a
 * compile completes.
 */
class PellPreviewEditor(
    private val project: Project,
    private val sourceFile: VirtualFile,
) : UserDataHolderBase(), FileEditor {

    private val editorFactory = EditorFactory.getInstance()
    private val document: Document = editorFactory.createDocument("// (compiling…)\n")
    private val viewer: EditorEx = editorFactory.createViewer(document, project) as EditorEx

    init {
        viewer.isViewer = true
        viewer.setCaretEnabled(false)
        viewer.settings.isLineNumbersShown = true
        viewer.settings.isLineMarkerAreaShown = false
        viewer.settings.isFoldingOutlineShown = true
        viewer.settings.isUseSoftWraps = false
        // Try to wire up SQL syntax highlighting via IntelliJ's
        // registered SQL file type. Falls back to plain text on
        // Community editions where the SQL highlighter isn't bundled.
        try {
            val sqlType = FileTypeManager.getInstance().getFileTypeByExtension("sql")
            val tmpVf = LightVirtualFile("preview.sql", sqlType, "")
            val highlighter = EditorHighlighterFactory.getInstance()
                .createEditorHighlighter(project, tmpVf)
            viewer.highlighter = highlighter
        } catch (_: Throwable) {
            // No SQL highlighter available — plain text is fine.
        }
    }

    /** Replace the preview contents with successfully-compiled PL/SQL. */
    fun setSql(sql: String) {
        com.intellij.openapi.application.ApplicationManager.getApplication().runWriteAction {
            document.setText(sql.ifEmpty { "// (no output)\n" })
        }
        viewer.backgroundColor = JBColor.namedColor("EditorPane.background", JBColor.WHITE)
    }

    /** Show a compile error in the preview pane (header + stderr). */
    fun setError(message: String) {
        val text = buildString {
            append("-- =============================================================\n")
            append("--  pell compile error\n")
            append("-- =============================================================\n\n")
            append(message.trimEnd())
            append("\n")
        }
        com.intellij.openapi.application.ApplicationManager.getApplication().runWriteAction {
            document.setText(text)
        }
    }

    // -- FileEditor interface ------------------------------------------------

    override fun getComponent(): JComponent = viewer.component
    override fun getPreferredFocusedComponent(): JComponent = viewer.contentComponent
    override fun getName(): String = "PL/SQL preview"
    override fun setState(state: FileEditorState) {}
    override fun isModified(): Boolean = false
    override fun isValid(): Boolean = sourceFile.isValid
    override fun addPropertyChangeListener(listener: PropertyChangeListener) {}
    override fun removePropertyChangeListener(listener: PropertyChangeListener) {}
    override fun getCurrentLocation(): FileEditorLocation? = null
    override fun getFile(): VirtualFile = sourceFile
    override fun dispose() {
        editorFactory.releaseEditor(viewer)
    }
}
