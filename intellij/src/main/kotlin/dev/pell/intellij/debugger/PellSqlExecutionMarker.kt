package dev.pell.intellij.debugger

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.editor.ex.MarkupModelEx
import com.intellij.openapi.editor.markup.HighlighterLayer
import com.intellij.openapi.editor.markup.HighlighterTargetArea
import com.intellij.openapi.editor.markup.RangeHighlighter
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.fileEditor.TextEditor
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile

/**
 * The paired "you are here" line marker in the deployed PL/SQL.
 *
 * XDebugger already paints the execution line in the pell source. This
 * mirrors it into the lowered `.sql` (read-only, fetched from
 * ALL_SOURCE) so you can watch both sides step in lock-step — the pell
 * statement on the left, the PL/SQL it became on the right.
 *
 * All editor access is on the EDT; the line uses the IDE's standard
 * execution-point color so it matches the debugger's own highlight.
 */
class PellSqlExecutionMarker(private val project: Project) {

    private var highlighter: RangeHighlighter? = null
    private var markedEditor: Editor? = null

    /** Open `vf` (without stealing focus from the pell editor) and mark
     *  `line` (0-based) as the current execution line. */
    fun show(vf: VirtualFile, line: Int) {
        ApplicationManager.getApplication().invokeLater {
            if (project.isDisposed) return@invokeLater
            clearOnEdt()
            val fem = FileEditorManager.getInstance(project)
            // focus=false: the pell editor keeps focus; the .sql opens
            // beside it (or reuses an existing tab).
            val descriptor = OpenFileDescriptor(project, vf, line, 0)
            val editor = fem.openFileEditor(descriptor, false)
                .filterIsInstance<TextEditor>().firstOrNull()?.editor
                ?: return@invokeLater
            if (line >= editor.document.lineCount) return@invokeLater
            val start = editor.document.getLineStartOffset(line)
            val end = editor.document.getLineEndOffset(line)
            // Theme execution-point colors; some schemes leave them
            // background-less, which renders invisibly — fall back to an
            // explicit band so the line is always apparent.
            var attrs = editor.colorsScheme.getAttributes(
                com.intellij.xdebugger.ui.DebuggerColors.EXECUTIONPOINT_ATTRIBUTES)
            if (attrs == null || attrs.backgroundColor == null) {
                attrs = com.intellij.openapi.editor.markup.TextAttributes().apply {
                    backgroundColor = com.intellij.ui.JBColor(0xCFE8FC, 0x2D4A5E)
                }
            }
            val hl = (editor.markupModel as MarkupModelEx).addRangeHighlighter(
                start, end, HighlighterLayer.SELECTION - 1,
                attrs, HighlighterTargetArea.LINES_IN_RANGE)
            hl.isGreedyToRight = true
            // Gutter arrow — unmistakable even when the band blends in.
            hl.gutterIconRenderer = object :
                com.intellij.openapi.editor.markup.GutterIconRenderer() {
                override fun getIcon() = com.intellij.icons.AllIcons.Debugger.NextStatement
                override fun getTooltipText() = "pell execution point (lowered PL/SQL)"
                override fun equals(other: Any?) = other === this
                override fun hashCode() = System.identityHashCode(this)
            }
            highlighter = hl
            markedEditor = editor
            editor.caretModel.moveToOffset(start)
            editor.scrollingModel.scrollToCaret(
                com.intellij.openapi.editor.ScrollType.CENTER)
        }
    }

    fun clear() {
        ApplicationManager.getApplication().invokeLater { clearOnEdt() }
    }

    private fun clearOnEdt() {
        highlighter?.let { hl -> runCatching { hl.dispose() } }
        highlighter = null
        markedEditor = null
    }
}
