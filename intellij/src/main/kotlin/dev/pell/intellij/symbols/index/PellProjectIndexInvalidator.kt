package dev.pell.intellij.symbols.index

import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.openapi.vfs.VirtualFileManager
import com.intellij.openapi.vfs.newvfs.BulkFileListener
import com.intellij.openapi.vfs.newvfs.events.VFileContentChangeEvent
import com.intellij.openapi.vfs.newvfs.events.VFileCopyEvent
import com.intellij.openapi.vfs.newvfs.events.VFileCreateEvent
import com.intellij.openapi.vfs.newvfs.events.VFileDeleteEvent
import com.intellij.openapi.vfs.newvfs.events.VFileEvent
import com.intellij.openapi.vfs.newvfs.events.VFileMoveEvent

/**
 * Keeps [PellProjectIndex] in sync with the filesystem. Subscribes to
 * the project's VFS, filters events down to `.pell` files, and dispatches
 * to the index on the EDT (the index's reindexFile does its own
 * ReadAction).
 *
 * Owned by L2 of LSP_PSI_UNIFICATION.md.
 */
class PellProjectIndexInvalidator : ProjectActivity {

    override suspend fun execute(project: Project) {
        val index = project.service<PellProjectIndex>()
        val connection = project.messageBus.connect()
        connection.subscribe(VirtualFileManager.VFS_CHANGES, object : BulkFileListener {
            override fun after(events: List<VFileEvent>) {
                for (e in events) {
                    when (e) {
                        is VFileContentChangeEvent -> reindex(index, e.file)
                        is VFileCreateEvent        -> e.file?.let { reindex(index, it) }
                        is VFileMoveEvent          -> reindex(index, e.file)
                        is VFileCopyEvent          -> e.newParent.findChild(e.newChildName)?.let { reindex(index, it) }
                        is VFileDeleteEvent        -> forget(index, e.file)
                        else                       -> {}
                    }
                }
            }
        })
    }

    private fun reindex(index: PellProjectIndex, file: VirtualFile) {
        if (file.extension != "pell") return
        index.reindexFile(file)
    }

    private fun forget(index: PellProjectIndex, file: VirtualFile) {
        if (file.extension != "pell") return
        index.forgetFile(file)
    }
}
