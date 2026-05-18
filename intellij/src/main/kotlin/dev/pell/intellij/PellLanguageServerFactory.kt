package dev.pell.intellij

import com.intellij.openapi.project.Project
import com.redhat.devtools.lsp4ij.LanguageServerFactory
import com.redhat.devtools.lsp4ij.server.ProcessStreamConnectionProvider
import com.redhat.devtools.lsp4ij.server.StreamConnectionProvider

/**
 * Spawns the pell-lsp server process. The path to the launcher script
 * defaults to the conventional location (`<repo>/lsp/run.sh`); users can
 * override it via the LSP4IJ language-server settings UI.
 */
class PellLanguageServerFactory : LanguageServerFactory {

    override fun createConnectionProvider(project: Project): StreamConnectionProvider {
        // Resolution order:
        //   1. PELL_LSP environment variable (absolute path to run.sh / executable)
        //   2. ./lsp/run.sh relative to the project root
        //   3. ~/.local/bin/pell-lsp (future: installable binary)
        val envOverride = System.getenv("PELL_LSP")
        val projectBased = project.basePath?.let { "$it/lsp/run.sh" }
        val candidates = listOfNotNull(envOverride, projectBased, "${System.getProperty("user.home")}/.local/bin/pell-lsp")
        val launcher = candidates.firstOrNull { java.io.File(it).canExecute() } ?: candidates.first()

        return ProcessStreamConnectionProvider(
            listOf(launcher),
            project.basePath  // working directory
        )
    }

    override fun getServerInterface(): Class<*> = org.eclipse.lsp4j.services.LanguageServer::class.java
}
