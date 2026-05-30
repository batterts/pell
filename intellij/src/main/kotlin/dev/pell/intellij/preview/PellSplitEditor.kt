package dev.pell.intellij.preview

import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.execution.process.OSProcessHandler
import com.intellij.execution.process.ProcessAdapter
import com.intellij.execution.process.ProcessEvent
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.editor.event.CaretEvent
import com.intellij.openapi.editor.event.CaretListener
import com.intellij.openapi.editor.event.DocumentEvent
import com.intellij.openapi.editor.event.DocumentListener
import com.intellij.openapi.fileEditor.TextEditor
import com.intellij.openapi.fileEditor.TextEditorWithPreview
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.openapi.vfs.VirtualFileManager
import com.intellij.openapi.vfs.newvfs.BulkFileListener
import com.intellij.openapi.vfs.newvfs.events.VFileContentChangeEvent
import com.intellij.openapi.vfs.newvfs.events.VFileEvent
import dev.pell.intellij.settings.PellSettings
import java.io.File

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
        // Sync the preview to wherever the caret lands in the source —
        // markdown-style. When the user clicks `pub fn greet(...)` we
        // scroll the preview to `FUNCTION greet`. Name-based lookup
        // (no source-map yet); good enough for ~95% of navigation.
        textEditor.editor.caretModel.addCaretListener(object : CaretListener {
            override fun caretPositionChanged(event: CaretEvent) {
                syncPreviewToCaret()
            }
        })
        // Flush the lowered PL/SQL to `<targetDir>/<name>.sql` whenever
        // the .pell source's on-disk content changes (Cmd-S, external
        // edit, git checkout, …). Keeps the committed artifact in sync
        // with the source you can git-blame.
        project.messageBus.connect(this).subscribe(
            VirtualFileManager.VFS_CHANGES,
            object : BulkFileListener {
                override fun after(events: List<VFileEvent>) {
                    for (e in events) {
                        if (e is VFileContentChangeEvent && e.file == sourceFile) {
                            flushToTargetDir()
                            break
                        }
                    }
                }
            },
        )
        // Kick off the first compile as soon as the editor is shown.
        ApplicationManager.getApplication().invokeLater { triggerCompile(delayMs = 0) }
    }

    /**
     * Shell out to `./pell build <source> -o <targetDir>/<name>.sql`
     * so the committed artifact stays in lockstep with the source.
     *
     * Async (no blocking on the save callback). On success, refreshes
     * VFS so the file pops into the project tree immediately. On
     * failure, silently leaves the previous file in place — the
     * preview pane already surfaces the compile error.
     */
    private fun flushToTargetDir() {
        val basePath = project.basePath ?: return
        val pellExe = File(basePath, "pell")
        if (!pellExe.canExecute()) return

        val targetDirName = PellSettings.getInstance(project).targetDirName
        val targetDir = File(basePath, targetDirName).apply { mkdirs() }
        val targetFile = File(targetDir, sourceFile.nameWithoutExtension + ".sql")

        val cmd = GeneralCommandLine(pellExe.absolutePath)
            .withParameters(
                "build",
                sourceFile.path,
                "-o", targetFile.absolutePath,
                "--reproducible",
            )
            .withWorkDirectory(basePath)
            .withCharset(Charsets.UTF_8)
            .withRedirectErrorStream(false)
        try {
            val handler = OSProcessHandler(cmd)
            handler.addProcessListener(object : ProcessAdapter() {
                override fun processTerminated(event: ProcessEvent) {
                    if (event.exitCode == 0) {
                        ApplicationManager.getApplication().invokeLater {
                            LocalFileSystem.getInstance()
                                .refreshAndFindFileByPath(targetFile.absolutePath)
                        }
                    }
                }
            })
            handler.startNotify()
        } catch (_: Throwable) {
            // No-op — the preview already shows compile errors.
        }
    }

    /**
     * Walk backward from the caret to find the nearest pell declaration
     * (`pub fn foo`, `fn foo`, `pub record Foo`, `pub type Foo`, etc.)
     * and ask the preview to scroll its viewer to the matching PL/SQL
     * line (`FUNCTION foo`, `PROCEDURE foo`, `TYPE t_foo`, …).
     */
    private fun syncPreviewToCaret() {
        val srcEditor = textEditor.editor
        val caret = srcEditor.caretModel.offset
        val text = srcEditor.document.text
        val (kind, name) = findEnclosingDecl(text, caret) ?: return
        pellPreview.scrollToDeclaration(kind, name)
    }

    /**
     * Returns (kind, name) for the declaration whose body contains
     * `caretOffset`, or null if none found. Tolerant of whitespace and
     * the `pub` modifier. Walks backward up to 4KB of source.
     */
    private fun findEnclosingDecl(text: String, caretOffset: Int): Pair<String, String>? {
        // Look in a window ending at caret. Most decls fit in 4KB.
        val start = (caretOffset - 4096).coerceAtLeast(0)
        val window = text.substring(start, caretOffset.coerceAtMost(text.length))
        // Regex catches the LAST matching declaration before caret.
        val re = Regex("""(?m)^\s*(?:pub\s+)?(fn|record|type|error|aggregate|enum|sealed)\s+(\w+)""")
        return re.findAll(window).lastOrNull()?.let { m ->
            m.groupValues[1] to m.groupValues[2]
        }
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
