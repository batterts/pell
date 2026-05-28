package dev.pell.intellij.run

import com.intellij.execution.configurations.ConfigurationFactory
import com.intellij.execution.configurations.ConfigurationType
import com.intellij.execution.configurations.RunConfiguration
import com.intellij.openapi.project.Project

class PellRunConfigurationFactory(type: ConfigurationType) : ConfigurationFactory(type) {
    override fun getId(): String = "pell"
    override fun getName(): String = "pell"

    override fun createTemplateConfiguration(project: Project): RunConfiguration =
        PellRunConfiguration(project, this, "pell")

    override fun getOptionsClass(): Class<PellRunConfigurationOptions> =
        PellRunConfigurationOptions::class.java
}
