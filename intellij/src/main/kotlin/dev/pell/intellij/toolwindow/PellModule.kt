package dev.pell.intellij.toolwindow

import com.intellij.openapi.vfs.VirtualFile

/**
 * One pell source file in the project, with derived build/deploy state.
 *
 * State flows: `Unknown` → `Building` → `BuildOk` / `BuildFailed` →
 * (on deploy) `Deploying` → `Deployed` / `DeployFailed`. State only
 * applies to the *most recent* attempt; we don't keep history here.
 *
 * Future: pull `Deployed` from Oracle's USER_OBJECTS table to verify
 * the installed object actually matches the local build (compare
 * SHA-256 in the file header).
 */
data class PellModule(
    val source: VirtualFile,
    val moduleName: String,
    var built: VirtualFile? = null,
    var state: State = State.Unknown,
    var lastMessage: String = "",
) {
    enum class State {
        Unknown,        // not yet built or deployed
        Building,       // build in progress
        BuildOk,        // built; not deployed (or unknown deploy state)
        BuildFailed,    // build error
        Deploying,      // deploy in progress
        Deployed,       // built + deployed against PELL_DB_URL
        DeployFailed,   // deploy error
    }
}
