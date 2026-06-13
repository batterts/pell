package dev.pell.intellij.settings

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.project.Project

/**
 * Per-project pell settings, persisted to `.idea/pell.xml`.
 *
 * Access via:  `PellSettings.getInstance(project)`
 *
 * Why project-scoped (not application-scoped): different projects
 * may have different conventions for where the lowered PL/SQL lives
 * — one might call it `sql/`, another `plsql/`, another wants the
 * legacy `built/`. The choice is a project-level concern, not a
 * per-user preference.
 */
@Service(Service.Level.PROJECT)
@State(
    name = "PellSettings",
    storages = [Storage("pell.xml")],
)
class PellSettings : PersistentStateComponent<PellSettings.State> {

    /** The on-disk state that gets serialized to pell.xml. */
    data class State(
        /**
         * Directory (relative to the project root) where the lowered
         * PL/SQL is written. The IntelliJ preview pane writes here on
         * save, and `pell deploy` reads from here. Default: `plsql`.
         */
        var targetDirName: String = "plsql",
        /**
         * Debugger (jdwp transport): fixed listener port — needed when
         * the connect-back arrives through a reverse SSH tunnel
         * (`ssh -R 9040:...`). Blank = ephemeral port.
         */
        var debugJdwpPort: String = "",
        /**
         * Debugger (jdwp transport): the address the DATABASE dials to
         * reach the IDE. Blank = auto (host.docker.internal for a local
         * container, the route-interface IP otherwise). For a reverse
         * tunnel: 127.0.0.1.
         */
        var debugCallbackHost: String = "",
        /** Debugger transport: "jdwp" (default) or "sql" (outbound-only
         *  via DBMS_DEBUG — for tunnels/NAT/no-ACL instances). */
        var debugTransport: String = "jdwp",
        /** Default PELL_DB_URL for spawned pell processes. Blank =
         *  inherit from the IDE's environment. Per-run-config env
         *  vars override this. */
        var dbUrl: String = "",
        /** Schema utPLSQL is installed in, when it isn't reachable via
         *  public synonym (the default install creates them, so this
         *  stays blank for most setups). Passed to `pell test` as
         *  PELL_UT_SCHEMA so `ut` / `ut_*` get qualified. */
        var utSchema: String = "",
    )

    private var state = State()

    override fun getState(): State = state
    override fun loadState(s: State) {
        state = s
    }

    /** The current target directory name (e.g. "plsql", "sql", "built"). */
    var targetDirName: String
        get() = state.targetDirName.ifBlank { "plsql" }
        set(value) {
            state.targetDirName = value.trim().ifBlank { "plsql" }
        }

    var debugJdwpPort: String
        get() = state.debugJdwpPort.trim()
        set(value) { state.debugJdwpPort = value.trim() }

    var debugCallbackHost: String
        get() = state.debugCallbackHost.trim()
        set(value) { state.debugCallbackHost = value.trim() }

    var debugTransport: String
        get() = state.debugTransport.trim().ifBlank { "jdwp" }
        set(value) { state.debugTransport = value.trim().lowercase().ifBlank { "jdwp" } }

    var dbUrl: String
        get() = state.dbUrl.trim()
        set(value) { state.dbUrl = value.trim() }

    var utSchema: String
        get() = state.utSchema.trim()
        set(value) { state.utSchema = value.trim() }

    companion object {
        @JvmStatic
        fun getInstance(project: Project): PellSettings =
            project.getService(PellSettings::class.java)
    }
}
