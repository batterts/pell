package dev.pell.intellij.preview

import com.intellij.lang.Language
import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.EditorFactory
import com.intellij.openapi.editor.colors.EditorColorsManager
import com.intellij.openapi.editor.ex.EditorEx
import com.intellij.openapi.editor.ex.util.LexerEditorHighlighter
import com.intellij.openapi.fileEditor.FileEditor
import com.intellij.openapi.fileEditor.FileEditorLocation
import com.intellij.openapi.fileEditor.FileEditorState
import com.intellij.openapi.fileTypes.SyntaxHighlighterFactory
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.UserDataHolderBase
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.ui.JBColor
import com.intellij.util.ui.JBUI
import java.awt.BorderLayout
import java.awt.Color
import java.beans.PropertyChangeListener
import javax.swing.JComponent
import javax.swing.JLabel
import javax.swing.JPanel
import javax.swing.SwingConstants

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
    // Set true once we've shown a successfully-compiled SQL at least
    // once. Until then, compile errors render in the document body
    // (no good content to fall back to); after, they surface only as
    // a header banner — the body keeps the last good SQL visible.
    private var hasGoodCompile: Boolean = false

    // Status banner above the viewer, used when the most recent compile
    // failed AND we have a prior good compile to keep showing. Removed
    // when a fresh compile succeeds.
    private val errorBanner: JLabel = JLabel("", SwingConstants.LEFT).apply {
        foreground = JBColor.RED
        background = JBColor(Color(0xFFEEEE), Color(0x5A3030))
        isOpaque = true
        border = JBUI.Borders.empty(4, 8)
    }
    private val errorBannerPanel: JPanel = JPanel(BorderLayout()).apply {
        add(errorBanner, BorderLayout.CENTER)
        isVisible = false
    }

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
        // SQL syntax highlighting via the public SyntaxHighlighterFactory
        // API. Avoids creating a LightVirtualFile (testFramework /
        // internal-tagged) just to get a file-typed highlighter.
        try {
            val sqlLang = Language.findLanguageByID("SQL")
            if (sqlLang != null) {
                val sh = SyntaxHighlighterFactory.getSyntaxHighlighter(sqlLang, project, null)
                if (sh != null) {
                    viewer.highlighter = LexerEditorHighlighter(
                        sh, EditorColorsManager.getInstance().globalScheme,
                    )
                }
            }
        } catch (_: Throwable) {
            // No SQL highlighter available — plain text is fine.
        }
        // Attach the error banner as the editor's permanent header so
        // we can toggle visibility without re-laying-out the editor.
        viewer.headerComponent = errorBannerPanel
    }

    /** Replace the preview contents with successfully-compiled PL/SQL.
     *  Clears the error banner — the new content IS the latest truth. */
    fun setSql(sql: String) {
        com.intellij.openapi.application.ApplicationManager.getApplication().runWriteAction {
            document.setText(sql.ifEmpty { "// (no output)\n" })
        }
        hasGoodCompile = true
        errorBannerPanel.isVisible = false
    }

    /** Surface a compile error WITHOUT clobbering the last successful
     *  preview. While the source has invalid syntax (mid-typing
     *  `pub fn foo(`), the SQL body stays frozen on the last good
     *  compile and a red banner above shows the error. Lets the user
     *  read the previous PL/SQL while they're still typing — no flicker.
     *
     *  Falls back to rendering the error in the document body only
     *  when there's no prior successful compile yet. */
    fun setError(message: String) {
        if (hasGoodCompile) {
            errorBanner.text = "<html>⚠ <b>compile error</b> — preview frozen at last good compile" +
                "<br/><span style='color:#888'>" + escapeHtml(firstLineOf(message)) + "</span></html>"
            errorBannerPanel.isVisible = true
            return
        }
        // First compile failed — render the error in the body so the
        // user has something to look at.
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

    private fun firstLineOf(s: String): String =
        s.lineSequence().firstOrNull { it.isNotBlank() }?.trim()?.take(200) ?: ""

    private fun escapeHtml(s: String): String =
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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
