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

    companion object {
        @JvmStatic
        fun getInstance(project: Project): PellSettings =
            project.getService(PellSettings::class.java)
    }
}
