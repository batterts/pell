package dev.pell.intellij.preview

import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.execution.process.OSProcessHandler
import com.intellij.execution.process.ProcessAdapter
import com.intellij.execution.process.ProcessEvent
import com.intellij.execution.process.ProcessListener
import com.intellij.openapi.components.Service
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Key
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicReference
import javax.swing.Timer

/**
 * Async, debounced `pell build` runner backing the side-by-side preview.
 *
 * Per-file state: each `.pell` source gets its own debounce timer and
 * running-process reference. When the user types, `schedule()` resets
 * the timer; when it fires, a fresh `pell build` is spawned and the
 * result handed to the registered callback.
 *
 * Cancellation: a new schedule() during an in-flight build cancels
 * the in-flight one (best-effort — `OSProcessHandler.destroyProcess`).
 *
 * We shell out to the project's `pell` shim rather than reimplementing
 * the compile in Kotlin. Cheap (~50–200ms cold) and keeps a single
 * source of truth for the lowering.
 */
@Service(Service.Level.PROJECT)
class PellCompileService(private val project: Project) {

    private val log = logger<PellCompileService>()
    private val timers = ConcurrentHashMap<String, Timer>()
    private val running = ConcurrentHashMap<String, OSProcessHandler>()

    /**
     * Debounced: requests a compile of `sourcePath`'s current content
     * (passed in [contentText] so we don't re-read a stale on-disk
     * buffer). After [delayMs] without further calls, runs `pell build`
     * on a temp file containing the content and calls [onResult].
     *
     * Cancels any pending timer or running process for this source.
     */
    fun schedule(
        sourcePath: String,
        contentText: String,
        delayMs: Int = 400,
        onResult: (CompileResult) -> Unit,
    ) {
        timers.remove(sourcePath)?.stop()
        running.remove(sourcePath)?.destroyProcess()

        val timer = Timer(delayMs) {
            runCompile(sourcePath, contentText, onResult)
        }
        timer.isRepeats = false
        timer.start()
        timers[sourcePath] = timer
    }

    private fun runCompile(
        sourcePath: String,
        contentText: String,
        onResult: (CompileResult) -> Unit,
    ) {
        val pellExe = File(project.basePath, "pell")
        if (!pellExe.canExecute()) {
            onResult(CompileResult.Error(
                "Can't find executable `pell` shim at ${pellExe.absolutePath}. " +
                "This editor expects to be opened inside a pell project."
            ))
            return
        }

        // Write the in-memory buffer to a temp file so pell sees the
        // current (unsaved) content, not what's on disk.
        val tmp = File.createTempFile("pell-preview-", ".pell").also {
            it.writeText(contentText)
            it.deleteOnExit()
        }

        val cmd = GeneralCommandLine(pellExe.absolutePath)
            .withParameters("build", tmp.absolutePath, "--reproducible")
            .withWorkDirectory(project.basePath)
            .withCharset(Charsets.UTF_8)
            .withRedirectErrorStream(false)

        val handler = try {
            OSProcessHandler(cmd)
        } catch (e: Exception) {
            onResult(CompileResult.Error("Failed to launch pell: ${e.message}"))
            return
        }

        running[sourcePath] = handler
        val stdout = StringBuilder()
        val stderr = StringBuilder()

        handler.addProcessListener(object : ProcessAdapter() {
            override fun onTextAvailable(event: ProcessEvent, outputType: Key<*>) {
                if (outputType.toString() == "stderr") {
                    stderr.append(event.text)
                } else {
                    stdout.append(event.text)
                }
            }
            override fun processTerminated(event: ProcessEvent) {
                running.remove(sourcePath, handler)
                tmp.delete()
                if (event.exitCode == 0) {
                    onResult(CompileResult.Ok(stdout.toString()))
                } else {
                    val errText = stderr.toString().trim().ifEmpty { stdout.toString().trim() }
                    onResult(CompileResult.Error(errText.ifEmpty { "(no error text)" }))
                }
            }
        })
        handler.startNotify()
    }

    companion object {
        fun getInstance(project: Project): PellCompileService =
            project.getService(PellCompileService::class.java)
    }
}

/** Result of a `pell build`. */
sealed class CompileResult {
    /** Successful compile — `sql` is the full emitted PL/SQL. */
    data class Ok(val sql: String) : CompileResult()
    /** Compile failed — `message` is the stderr / error text. */
    data class Error(val message: String) : CompileResult()
}
