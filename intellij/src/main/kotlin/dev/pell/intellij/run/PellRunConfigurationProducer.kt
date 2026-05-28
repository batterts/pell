package dev.pell.intellij.run

import com.intellij.execution.actions.ConfigurationContext
import com.intellij.execution.actions.LazyRunConfigurationProducer
import com.intellij.execution.configurations.ConfigurationFactory
import com.intellij.openapi.util.Ref
import com.intellij.psi.PsiElement
import dev.pell.intellij.PellFileType

/**
 * Translates a context (click in editor / click on gutter icon / right-click
 * → Run File) into a `PellRunConfiguration` pointed at the current file.
 *
 * `isConfigurationFromContext` lets IntelliJ reuse an existing config
 * when the user re-runs the same file, instead of producing a new
 * temporary every time.
 */
class PellRunConfigurationProducer : LazyRunConfigurationProducer<PellRunConfiguration>() {

    override fun getConfigurationFactory(): ConfigurationFactory =
        PellRunConfigurationType.INSTANCE.configurationFactories.first()

    override fun setupConfigurationFromContext(
        configuration: PellRunConfiguration,
        context: ConfigurationContext,
        sourceElement: Ref<PsiElement>,
    ): Boolean {
        val file = context.psiLocation?.containingFile ?: return false
        if (file.fileType !is PellFileType) return false
        val vfile = file.virtualFile ?: return false
        configuration.filePath = vfile.path
        configuration.action = configuration.action.ifBlank { "exec" }
        configuration.setName(configuration.suggestedName() ?: "pell exec: ${vfile.nameWithoutExtension}")
        return true
    }

    override fun isConfigurationFromContext(
        configuration: PellRunConfiguration,
        context: ConfigurationContext,
    ): Boolean {
        val file = context.psiLocation?.containingFile ?: return false
        if (file.fileType !is PellFileType) return false
        val vpath = file.virtualFile?.path ?: return false
        return configuration.filePath == vpath
    }
}
