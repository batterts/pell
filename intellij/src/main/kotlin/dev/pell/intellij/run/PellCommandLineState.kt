package dev.pell.intellij.run

import com.intellij.execution.ExecutionException
import com.intellij.execution.configurations.CommandLineState
import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.execution.process.OSProcessHandler
import com.intellij.execution.process.ProcessHandler
import com.intellij.execution.process.ProcessTerminatedListener
import com.intellij.execution.runners.ExecutionEnvironment
import java.io.File

/**
 * Builds the actual command line for a pell run and starts the process.
 *
 * pell binary resolution order:
 *   1. `config.pellHome`              (per-config override)
 *   2. `<project_root>/pell`          (canonical layout)
 *   3. `$PATH`                         (if `pell` is on PATH)
 *
 * The selected action becomes the first argument; if the action
 * already contains a space (e.g. "build -o"), it's split on
 * whitespace so the option carries through correctly.
 */
class PellCommandLineState(
    private val config: PellRunConfiguration,
    environment: ExecutionEnvironment,
) : CommandLineState(environment) {

    override fun startProcess(): ProcessHandler {
        val pellExe = resolvePell()
            ?: throw ExecutionException(
                "Couldn't find the pell CLI. Set it on the run config " +
                "(\"pell home\") or place a `pell` script at the project root.",
            )

        val tokens = config.action.split(Regex("\\s+")).filter { it.isNotBlank() }
        val subcmd = tokens.firstOrNull() ?: throw ExecutionException("empty pell action")
        val flagsAfterSubcmd = tokens.drop(1)

        val args = mutableListOf<String>()
        args += subcmd

        if (subcmd == "test") {
            // `pell test <suite> [flags]` — the target is a utPLSQL suite
            // path (`ut.run(...)`), not a file. No file is read; the
            // command connects to the DB and runs the deployed package.
            val suite = config.suiteName?.takeIf { it.isNotBlank() }
                ?: throw ExecutionException(
                    "This test run configuration has no suite name set."
                )
            args += suite
            args += flagsAfterSubcmd
        } else {
            val filePath = config.filePath ?: throw ExecutionException(
                "This run configuration has no file path set."
            )
            if (!File(filePath).isFile) {
                throw ExecutionException("File not found: $filePath")
            }

            // pell's CLI shape is: `pell <subcmd> <file> [flags]`. Build the
            // arg list in that order so flags like `-o`, `--dry-run` land
            // after the input file, not between subcmd and file.
            args += filePath
            args += flagsAfterSubcmd

            // `build -o` is a shorthand: append the canonical output path
            // `<dir>/<name>.sql` (matches the External Tool variant).
            if (subcmd == "build" && "-o" in flagsAfterSubcmd) {
                val src = File(filePath)
                val out = File(src.parentFile, src.nameWithoutExtension + ".sql")
                args += out.absolutePath
            }
        }
        config.extraArgs?.let {
            args += it.split(Regex("\\s+")).filter { token -> token.isNotBlank() }
        }

        // Env: run-config vars win, then the settings-page DB URL +
        // utPLSQL schema, then the IDE's inherited environment.
        val extraEnv = LinkedHashMap<String, String>()
        val settings = dev.pell.intellij.settings.PellSettings.getInstance(config.project)
        settings.dbUrl.takeIf { it.isNotBlank() }?.let { extraEnv["PELL_DB_URL"] = it }
        // Only relevant to `pell test`; harmless on other subcommands.
        settings.utSchema.takeIf { it.isNotBlank() }?.let { extraEnv["PELL_UT_SCHEMA"] = it }
        extraEnv.putAll(config.envVars)
        val cmd = GeneralCommandLine(pellExe.absolutePath)
            .withParameters(args)
            .withWorkDirectory(pellExe.parentFile)
            .withEnvironment(extraEnv)
            .withCharset(Charsets.UTF_8)

        val handler = OSProcessHandler(cmd)
        ProcessTerminatedListener.attach(handler, environment.project)
        return handler
    }

    private fun resolvePell(): File? {
        val override = config.pellHome?.takeIf { it.isNotBlank() }
        if (override != null) {
            val asScript = File(override, "pell")
            if (asScript.canExecute()) return asScript
            val asExe = File(override)
            if (asExe.canExecute()) return asExe
        }
        val projectRoot = config.project.basePath
        if (projectRoot != null) {
            val candidate = File(projectRoot, "pell")
            if (candidate.canExecute()) return candidate
        }
        // $PATH lookup
        for (dir in (System.getenv("PATH") ?: "").split(File.pathSeparator)) {
            if (dir.isBlank()) continue
            val candidate = File(dir, "pell")
            if (candidate.canExecute()) return candidate
        }
        return null
    }
}
