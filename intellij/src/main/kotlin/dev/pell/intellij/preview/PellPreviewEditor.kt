package dev.pell.intellij.preview

import com.intellij.icons.AllIcons
import com.intellij.lang.Language
import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.actionSystem.ActionPlaces
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.editor.Document
import com.intellij.openapi.editor.EditorFactory
import com.intellij.openapi.editor.colors.EditorColorsManager
import com.intellij.openapi.editor.ex.EditorEx
import com.intellij.openapi.editor.ex.util.LexerEditorHighlighter
import com.intellij.openapi.editor.markup.GutterIconRenderer
import com.intellij.openapi.editor.markup.HighlighterLayer
import com.intellij.openapi.editor.markup.HighlighterTargetArea
import com.intellij.openapi.editor.markup.RangeHighlighter
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
import java.io.File
import javax.swing.Icon
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

    // The gutter-icon highlighter that hangs off line 1 of a successful
    // preview. Click → write doc to a temp file → open a terminal tab →
    // run `./pell sql <tmpfile>` so the user sees the result inline.
    private var runHighlighter: RangeHighlighter? = null

    // Caret-sync line highlighter — the line the preview is currently
    // pinned to when the user clicks a decl in the source. One at a
    // time; the previous gets removed before the next is added.
    private var caretSyncHighlighter: RangeHighlighter? = null
    private var executionHighlighter: RangeHighlighter? = null

    init {
        viewer.isViewer = true
        viewer.setCaretEnabled(false)
        viewer.settings.isLineNumbersShown = true
        // Show the gutter marker area so our Run-on-PELL_DB_URL icon
        // has a visible home next to line 1.
        viewer.settings.isLineMarkerAreaShown = true
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
        // Clear any stale caret-sync highlight before the document
        // changes — its line offsets won't be meaningful in the new text.
        caretSyncHighlighter?.let { viewer.markupModel.removeHighlighter(it) }
        caretSyncHighlighter = null
        executionHighlighter?.let { viewer.markupModel.removeHighlighter(it) }
        executionHighlighter = null
        com.intellij.openapi.application.ApplicationManager.getApplication().runWriteAction {
            // IntelliJ documents must use \n-only separators. Lowered SQL
            // can carry a raw CR byte inside a string literal (e.g. the
            // CSV library's `"\r"` for CRLF handling); a bare \r would make
            // setText throw "Wrong line separators" and leave the preview
            // stuck at "(compiling…)". Normalize for display.
            document.setText(
                com.intellij.openapi.util.text.StringUtil
                    .convertLineSeparators(sql.ifEmpty { "// (no output)\n" }))
        }
        hasGoodCompile = true
        errorBannerPanel.isVisible = false
        installRunGutterIcon()
    }

    /** Paint the debugger's current execution line (0-based) — the
     *  paired highlight to XDebugger's band in the pell source. The
     *  preview must be showing the debug build (the registry forces
     *  that during a session) or the line would be off by the shadow
     *  declarations. */
    fun setExecutionLine(line0: Int) {
        clearExecutionLine()
        if (line0 < 0 || line0 >= document.lineCount) return
        val start = document.getLineStartOffset(line0)
        val end = document.getLineEndOffset(line0)
        var attrs = viewer.colorsScheme.getAttributes(
            com.intellij.xdebugger.ui.DebuggerColors.EXECUTIONPOINT_ATTRIBUTES)
        if (attrs == null || attrs.backgroundColor == null) {
            attrs = com.intellij.openapi.editor.markup.TextAttributes().apply {
                backgroundColor = com.intellij.ui.JBColor(0xCFE8FC, 0x2D4A5E)
            }
        }
        val hl = viewer.markupModel.addRangeHighlighter(
            start, end, HighlighterLayer.SELECTION - 1,
            attrs, HighlighterTargetArea.LINES_IN_RANGE)
        hl.gutterIconRenderer = object :
            com.intellij.openapi.editor.markup.GutterIconRenderer() {
            override fun getIcon() = com.intellij.icons.AllIcons.Debugger.NextStatement
            override fun getTooltipText() = "pell execution point"
            override fun equals(other: Any?) = other === this
            override fun hashCode() = System.identityHashCode(this)
        }
        executionHighlighter = hl
        viewer.caretModel.moveToOffset(start)
        viewer.scrollingModel.scrollToCaret(
            com.intellij.openapi.editor.ScrollType.CENTER)
    }

    fun clearExecutionLine() {
        executionHighlighter?.let { viewer.markupModel.removeHighlighter(it) }
        executionHighlighter = null
    }

    /** Pin a green-play gutter icon on line 1 of the preview. Click it
     *  to run the current PL/SQL against PELL_DB_URL via a terminal
     *  tab. Removes any prior icon first so we don't stack them. */
    private fun installRunGutterIcon() {
        val mm = viewer.markupModel
        runHighlighter?.let { mm.removeHighlighter(it) }
        if (document.lineCount == 0) return
        val end = document.getLineEndOffset(0)
        val hl = mm.addRangeHighlighter(
            0, end,
            HighlighterLayer.FIRST,
            null,
            HighlighterTargetArea.EXACT_RANGE,
        )
        hl.gutterIconRenderer = RunGutterRenderer(this)
        runHighlighter = hl
    }

    /**
     * Markdown-style caret sync: scroll the preview to the matching
     * PL/SQL declaration for the pell decl the source caret is on,
     * and highlight the line.
     *
     * For `fn`, we deliberately jump to the BODY (`FUNCTION foo IS`)
     * not the spec (`FUNCTION foo RETURN ...;`) — the body is where
     * the code lives. Search begins from the
     * `CREATE OR REPLACE PACKAGE BODY` marker if present.
     *
     * Mapping (best-effort, name-based):
     *   fn foo          → FUNCTION foo IS  |  PROCEDURE foo IS
     *   record Foo      → TYPE t_foo IS
     *   type Foo        → TYPE Foo AS OBJECT
     *   aggregate Foo   → TYPE Foo AS OBJECT
     *
     * Falls through silently if no match. Doesn't try to be exact —
     * just gets you close enough to read the rest.
     */
    fun scrollToDeclaration(kind: String, name: String) {
        val lcName = name.lowercase()
        val text = document.text
        // For fn lookups, skip past the PACKAGE spec so we land on the
        // FUNCTION foo IS body, not the spec's `FUNCTION foo RETURN ...;`.
        val bodyMarker = "CREATE OR REPLACE PACKAGE BODY"
        val searchFrom = if (kind == "fn") {
            val bodyAt = text.indexOf(bodyMarker)
            if (bodyAt >= 0) bodyAt else 0
        } else 0

        // Patterns include the body-marker shape (e.g. `FUNCTION foo IS`)
        // so we match the IS-prefixed body rather than a same-named call
        // somewhere inside another fn.
        val patterns = when (kind) {
            "fn"        -> listOf(
                "FUNCTION $lcName(", "FUNCTION $lcName ",
                "PROCEDURE $lcName(", "PROCEDURE $lcName ",
                "FUNCTION $name(", "PROCEDURE $name(",
            )
            "record"    -> listOf("TYPE t_$lcName ", "TYPE T_$lcName ", "TYPE t_$lcName\n")
            "type"      -> listOf("TYPE $name ", "TYPE t_$lcName ")
            "error"     -> listOf("EXCEPTION_INIT($lcName", "$lcName ")
            "aggregate" -> listOf("TYPE $name ", "TYPE t_$lcName ")
            "enum"      -> listOf("TYPE t_$lcName ", "$lcName ")
            "sealed"    -> listOf("TYPE $name ", "TYPE t_$lcName ")
            else        -> listOf(lcName)
        }
        val hit = patterns.firstNotNullOfOrNull { p ->
            val idx = text.indexOf(p, searchFrom)
            if (idx >= 0) idx else null
        } ?: return
        val line = document.getLineNumber(hit)

        // Scroll the line into view (centered, markdown-preview-style).
        viewer.scrollingModel.scrollTo(
            com.intellij.openapi.editor.LogicalPosition(line, 0),
            com.intellij.openapi.editor.ScrollType.CENTER,
        )
        highlightLine(line)
    }

    /** Paint a soft background on the given line of the preview so
     *  the user's eye lands on it after a caret-sync. Only one line
     *  is highlighted at a time — the previous mark is cleared. */
    private fun highlightLine(line: Int) {
        val mm = viewer.markupModel
        caretSyncHighlighter?.let { mm.removeHighlighter(it) }
        if (line < 0 || line >= document.lineCount) return
        val start = document.getLineStartOffset(line)
        val end = document.getLineEndOffset(line)

        // Reuse the IDE's "caret row" background color so the
        // highlight reads as native (light yellow on light theme,
        // dim gray on dark).
        val scheme = EditorColorsManager.getInstance().globalScheme
        val attrs = com.intellij.openapi.editor.markup.TextAttributes(
            null,
            scheme.getColor(com.intellij.openapi.editor.colors.EditorColors.CARET_ROW_COLOR)
                ?: JBColor(Color(0xFFF8C5), Color(0x3A3A2C)),
            null,
            null,
            java.awt.Font.PLAIN,
        )
        val hl = mm.addRangeHighlighter(
            start, end,
            HighlighterLayer.SELECTION - 1,  // under selection, over syntax
            attrs,
            HighlighterTargetArea.LINES_IN_RANGE,
        )
        caretSyncHighlighter = hl
    }

    /** True iff the preview is currently showing successfully-compiled
     *  PL/SQL (not the "(compiling…)" placeholder, not an error body). */
    fun isShowingGoodCompile(): Boolean = hasGoodCompile

    /** Shell-execute the current preview content as PL/SQL against
     *  PELL_DB_URL. Writes the in-memory buffer to a temp .sql file
     *  (no need to depend on disk save) and runs `./pell sql <tmp>` in
     *  a fresh terminal tab so the user sees row counts and any
     *  ORA-NNNNN errors next to the source. */
    fun runOnPellDbUrl() {
        val cwd = project.basePath ?: return
        // Refuse to run if the preview isn't showing real PL/SQL —
        // running the "(compiling…)" placeholder or the commented-out
        // error block against Oracle would either no-op (best case)
        // or throw ORA-00900 on whatever path strings happen to look
        // like SQL fragments (worst case).
        if (!hasGoodCompile) {
            com.intellij.notification.NotificationGroupManager.getInstance()
                .getNotificationGroup("pell")
                ?.createNotification(
                    "pell preview",
                    "The preview isn't a valid compile yet — fix the source first.",
                    com.intellij.notification.NotificationType.WARNING,
                )
                ?.notify(project)
            return
        }
        val tmp = File.createTempFile("pell-preview-", ".sql").also {
            it.writeText(document.text)
            it.deleteOnExit()
        }
        try {
            val mgr = org.jetbrains.plugins.terminal.TerminalToolWindowManager.getInstance(project)
            val tabName = "pell sql ${sourceFile.nameWithoutExtension}"
            val widget = mgr.createShellWidget(cwd, tabName, true, true)
            widget.sendCommandToExecute("./pell sql ${tmp.absolutePath}")
        } catch (e: Throwable) {
            // Terminal plugin unavailable — fall through silently.
            // (Plugin.xml depends on the Terminal plugin so this shouldn't
            // happen, but we don't want a stack trace in the UI.)
        }
    }

    /** Surface a compile error WITHOUT clobbering the last successful
     *  preview. While the source has invalid syntax (mid-typing
     *  `pub fn foo(`), the SQL body stays frozen on the last good
     *  compile and a red banner above shows the error. Lets the user
     *  read the previous PL/SQL while they're still typing — no flicker.
     *
     *  Falls back to rendering the error in the document body only
     *  when there's no prior successful compile yet. Every line of
     *  the error body is `-- `-prefixed so if the user runs it via
     *  the gutter (or shells `pell sql` on the temp file) Oracle just
     *  sees a comment block, not garbage that explodes with ORA-00900. */
    fun setError(message: String) {
        if (hasGoodCompile) {
            errorBanner.text = "<html>⚠ <b>compile error</b> — preview frozen at last good compile" +
                "<br/><span style='color:#888'>" + escapeHtml(firstLineOf(message)) + "</span></html>"
            errorBannerPanel.isVisible = true
            return
        }
        // First compile failed — render the error in the body so the
        // user has something to look at. Comment out every line so
        // accidentally running this against Oracle is a no-op.
        val text = buildString {
            append("-- =============================================================\n")
            append("--  pell compile error\n")
            append("-- =============================================================\n")
            for (line in message.trimEnd().lines()) {
                append("-- ")
                append(line)
                append("\n")
            }
        }
        com.intellij.openapi.application.ApplicationManager.getApplication().runWriteAction {
            // \n-only separators required (error text can echo a source
            // line carrying a CR) — see setSql.
            document.setText(
                com.intellij.openapi.util.text.StringUtil.convertLineSeparators(text))
        }
        // No good content → no Run button. Strip the gutter icon so
        // the user isn't tempted to click it on broken output.
        runHighlighter?.let { viewer.markupModel.removeHighlighter(it) }
        runHighlighter = null
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

/** Gutter renderer for the "Run this PL/SQL on PELL_DB_URL" action.
 *  Equality is identity-based so IntelliJ doesn't dedupe multiple
 *  renderer instances across edits. */
private class RunGutterRenderer(
    private val preview: PellPreviewEditor,
) : GutterIconRenderer() {
    override fun getIcon(): Icon = AllIcons.RunConfigurations.TestState.Run
    override fun getTooltipText(): String =
        "Run this PL/SQL on \$PELL_DB_URL"
    override fun isNavigateAction(): Boolean = true
    override fun getClickAction(): AnAction =
        object : AnAction("Run on \$PELL_DB_URL", null, AllIcons.RunConfigurations.TestState.Run) {
            override fun getActionUpdateThread() = ActionUpdateThread.BGT
            override fun actionPerformed(e: AnActionEvent) = preview.runOnPellDbUrl()
        }
    override fun equals(other: Any?): Boolean = other === this
    override fun hashCode(): Int = System.identityHashCode(this)
}
