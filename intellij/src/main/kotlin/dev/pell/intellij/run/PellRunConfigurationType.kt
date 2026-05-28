package dev.pell.intellij.run

import com.intellij.execution.configurations.ConfigurationFactory
import com.intellij.execution.configurations.ConfigurationType
import com.intellij.icons.AllIcons
import javax.swing.Icon

/**
 * Configuration type ID `dev.pell.runConfiguration`. Surfaces one
 * factory ("pell") that produces runnable `PellRunConfiguration`
 * instances. The action (`build` / `exec` / etc.) lives in the
 * config's options, not in separate factories — keeps the
 * Run/Debug "+" menu uncluttered.
 */
class PellRunConfigurationType : ConfigurationType {
    override fun getDisplayName(): String = "pell"
    override fun getConfigurationTypeDescription(): String =
        "Run a .pell file via the pell CLI (build / exec / parse / tokens)."
    override fun getIcon(): Icon = AllIcons.RunConfigurations.Application
    override fun getId(): String = ID
    override fun getConfigurationFactories(): Array<ConfigurationFactory> =
        arrayOf(PellRunConfigurationFactory(this))

    companion object {
        const val ID = "dev.pell.runConfiguration"
        @JvmStatic
        val INSTANCE: PellRunConfigurationType by lazy { PellRunConfigurationType() }
    }
}
