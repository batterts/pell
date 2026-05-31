package dev.pell.intellij.settings

import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.execution.util.ExecUtil
import com.intellij.openapi.project.Project
import java.io.File

/**
 * Locates a working pell CLI in the user's environment, or reports
 * that none was found. Backs the first-launch notification and the
 * "Install pell" button in Settings → Tools → pell.
 *
 * Resolution order (matches the project shim's logic):
 *   1. `$PELL_HOME/pell`         (env override)
 *   2. `<projectBase>/pell`      (the wizard-scaffolded shim)
 *   3. `pell` on `$PATH`         (e.g. installed via `pip install pell`)
 */
object PellDetector {

    /** Result of a detection probe — null means no working pell found. */
    data class Result(val path: String, val version: String) {
        companion object {
            val NONE: Result? = null
        }
    }

    /**
     * Locate pell without running it. Cheap — just checks file
     * existence + executability. Use [probe] when you want the
     * version string too (slower; spawns the binary).
     */
    fun locate(project: Project?): String? {
        // 1. $PELL_HOME
        System.getenv("PELL_HOME")?.takeIf { it.isNotBlank() }?.let { home ->
            val candidate = File(home, "pell")
            if (candidate.canExecute()) return candidate.absolutePath
        }
        // 2. <projectBase>/pell shim
        project?.basePath?.let { base ->
            val candidate = File(base, "pell")
            if (candidate.canExecute()) return candidate.absolutePath
        }
        // 3. $PATH
        val pathEntries = System.getenv("PATH")?.split(File.pathSeparator).orEmpty()
        for (dir in pathEntries) {
            val candidate = File(dir, "pell")
            if (candidate.canExecute()) return candidate.absolutePath
        }
        return null
    }

    /**
     * Locate AND probe — runs `pell --version` so we know the binary
     * actually works. Returns null if not found or if the probe fails.
     */
    fun probe(project: Project?): Result? {
        val path = locate(project) ?: return null
        return try {
            val cmd = GeneralCommandLine(path, "--version")
                .withCharset(Charsets.UTF_8)
            val out = ExecUtil.execAndGetOutput(cmd, 5_000)
            if (out.exitCode != 0) return null
            Result(path = path, version = out.stdout.trim().ifEmpty { "unknown" })
        } catch (_: Throwable) {
            null
        }
    }
}
