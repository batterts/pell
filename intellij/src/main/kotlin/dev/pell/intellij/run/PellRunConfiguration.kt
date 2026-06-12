package dev.pell.intellij.run

import com.intellij.execution.Executor
import com.intellij.execution.configurations.ConfigurationFactory
import com.intellij.execution.configurations.LocatableConfigurationBase
import com.intellij.execution.configurations.RunConfiguration
import com.intellij.execution.configurations.RunProfileState
import com.intellij.execution.runners.ExecutionEnvironment
import com.intellij.openapi.options.SettingsEditor
import com.intellij.openapi.project.Project

/**
 * A pell run configuration: knows which file to compile/exec and which
 * subcommand to invoke. Persisted via `PellRunConfigurationOptions`.
 *
 * Extends `LocatableConfigurationBase` so IntelliJ correctly handles
 * "auto" run configs produced by `PellRunConfigurationProducer` from a
 * source-file context — those are temporary and stick to the file
 * rather than getting saved with a generic name.
 */
class PellRunConfiguration(
    project: Project,
    factory: ConfigurationFactory,
    name: String,
) : LocatableConfigurationBase<PellRunConfigurationOptions>(project, factory, name) {

    public override fun getOptions(): PellRunConfigurationOptions =
        super.getOptions() as PellRunConfigurationOptions

    var filePath: String?
        get() = options.filePath
        set(v) { options.filePath = v }

    /** "build", "exec", "parse", or "tokens". */
    var action: String
        get() = options.action ?: "exec"
        set(v) { options.action = v }

    var extraArgs: String?
        get() = options.extraArgs
        set(v) { options.extraArgs = v }

    var pellHome: String?
        get() = options.pellHome
        set(v) { options.pellHome = v }

    var envVars: Map<String, String>
        get() = options.envVars
        set(v) {
            options.envVars.clear()
            options.envVars.putAll(v)
        }

    override fun getConfigurationEditor(): SettingsEditor<out RunConfiguration> =
        PellRunConfigurationEditor()

    override fun getState(executor: Executor, environment: ExecutionEnvironment): RunProfileState? =
        PellCommandLineState(this, environment)

    override fun suggestedName(): String? {
        val f = filePath ?: return null
        val base = java.io.File(f).nameWithoutExtension
        return "pell $action: $base"
    }
}
