package dev.pell.intellij.preview

import com.intellij.openapi.application.ApplicationManager
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * Connects debug sessions to the split-editor preview panes.
 *
 * While a pell debug session is active for a source file, its preview
 * compiles with `--debug` so the pane shows EXACTLY the deployed text
 * (markers, debug shadows) — which makes unit-line → file-line
 * conversion exact, and lets the debugger paint the current execution
 * line in the preview, paired with XDebugger's highlight in the pell
 * source on the left.
 */
object PellPreviewRegistry {

    private val previews = ConcurrentHashMap<String, PellPreviewEditor>()
    private val recompilers = ConcurrentHashMap<String, () -> Unit>()
    private val debugFiles = ConcurrentHashMap.newKeySet<String>()

    private fun key(path: String): String =
        runCatching { File(path).canonicalPath }.getOrDefault(path)

    fun register(path: String, preview: PellPreviewEditor, recompile: () -> Unit) {
        previews[key(path)] = preview
        recompilers[key(path)] = recompile
    }

    fun unregister(path: String) {
        previews.remove(key(path))
        recompilers.remove(key(path))
    }

    /** Is a debug session active for this source file? (The compile
     *  service checks this to add `--debug` to preview builds.) */
    fun isDebug(path: String): Boolean = key(path) in debugFiles

    fun beginDebug(path: String) {
        debugFiles.add(key(path))
        recompilers[key(path)]?.invoke()
    }

    fun endDebug(path: String) {
        debugFiles.remove(key(path))
        previews[key(path)]?.clearExecutionLine()
        recompilers[key(path)]?.invoke()
    }

    /** Paint the execution line (0-based, in the debug-built .sql text)
     *  in the preview pane for `path`. Returns false when no preview is
     *  open for that file (caller can fall back to a separate tab). */
    fun highlight(path: String, line0: Int): Boolean {
        val preview = previews[key(path)] ?: return false
        ApplicationManager.getApplication().invokeLater {
            preview.setExecutionLine(line0)
        }
        return true
    }

    fun clearHighlights() {
        previews.values.forEach { p ->
            ApplicationManager.getApplication().invokeLater { p.clearExecutionLine() }
        }
    }
}
