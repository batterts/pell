package dev.pell.intellij.debugger

import com.intellij.execution.configurations.RunProfile
import com.intellij.execution.configurations.RunProfileState
import com.intellij.execution.configurations.RunnerSettings
import com.intellij.execution.executors.DefaultDebugExecutor
import com.intellij.execution.runners.ExecutionEnvironment
import com.intellij.execution.runners.GenericProgramRunner
import com.intellij.execution.ui.RunContentDescriptor
import com.intellij.xdebugger.XDebugProcess
import com.intellij.xdebugger.XDebugProcessStarter
import com.intellij.xdebugger.XDebugSession
import com.intellij.xdebugger.XDebuggerManager
import dev.pell.intellij.run.PellRunConfiguration

/**
 * Routes the Debug executor for pell run configurations into the
 * JDWP-based PellDebugProcess (Run keeps using PellCommandLineState).
 */
class PellDebugRunner : GenericProgramRunner<RunnerSettings>() {

    override fun getRunnerId(): String = "PellDebugRunner"

    override fun canRun(executorId: String, profile: RunProfile): Boolean =
        executorId == DefaultDebugExecutor.EXECUTOR_ID && profile is PellRunConfiguration

    override fun doExecute(state: RunProfileState, env: ExecutionEnvironment): RunContentDescriptor? {
        val config = env.runProfile as PellRunConfiguration
        // jdwp (default): the DB connects back to the IDE — richest
        //   experience (script breakpoints, exact frames).
        // sql: everything over outbound connections via DBMS_DEBUG —
        //   for tunnels/NAT/no-ACL instances. PELL_DEBUG_TRANSPORT=sql.
        val transport = System.getenv("PELL_DEBUG_TRANSPORT")?.lowercase() ?: "jdwp"
        val session = XDebuggerManager.getInstance(env.project).startSession(
            env,
            object : XDebugProcessStarter() {
                override fun start(session: XDebugSession): XDebugProcess =
                    if (transport == "sql") PellSqlDebugProcess(session, config)
                    else PellDebugProcess(session, config)
            },
        )
        return session.runContentDescriptor
    }
}
