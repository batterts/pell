package dev.pell.intellij.toolwindow

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project
import com.intellij.openapi.roots.ProjectRootManager
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VfsUtilCore
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.openapi.vfs.VirtualFileManager
import com.intellij.openapi.vfs.VirtualFileVisitor
import com.intellij.openapi.vfs.newvfs.BulkFileListener
import com.intellij.openapi.vfs.newvfs.events.VFileContentChangeEvent
import com.intellij.openapi.vfs.newvfs.events.VFileCreateEvent
import com.intellij.openapi.vfs.newvfs.events.VFileDeleteEvent
import com.intellij.openapi.vfs.newvfs.events.VFileEvent
import com.intellij.openapi.vfs.newvfs.events.VFileMoveEvent
import dev.pell.intellij.PellFileType
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Project-scoped service that owns the list of pell modules in the
 * project and their current build/deploy state. The tool window
 * subscribes to changes via the `listeners` list to refresh its UI.
 *
 * Listens to the VFS so newly-added .pell / .sql files appear in the
 * tree without the user clicking Refresh — important for the
 * New-Project wizard flow, where the scaffold writes files after the
 * tool window's initial scan has already finished.
 *
 * "Module" here means "one .pell file" — we don't currently track
 * cross-file aggregations.
 */
@Service(Service.Level.PROJECT)
class PellModuleService(private val project: Project) {

    private val modulesByPath = mutableMapOf<String, PellModule>()
    private val listeners = CopyOnWriteArrayList<() -> Unit>()

    init {
        // VFS subscription — auto-rescan when relevant files appear or
        // disappear under the project's content roots.
        val bus = project.messageBus.connect()
        bus.subscribe(
            VirtualFileManager.VFS_CHANGES,
            object : BulkFileListener {
                override fun after(events: List<VFileEvent>) {
                    if (events.any { interesting(it) }) {
                        ApplicationManager.getApplication().invokeLater { rescan() }
                    }
                }
            },
        )
        // Re-scan when the project's content roots change (added/removed/
        // reordered). Catches the post-wizard case where files exist but
        // the root model wasn't populated yet at tool-window init time.
        bus.subscribe(
            com.intellij.openapi.roots.ModuleRootListener.TOPIC,
            object : com.intellij.openapi.roots.ModuleRootListener {
                override fun rootsChanged(event: com.intellij.openapi.roots.ModuleRootEvent) {
                    ApplicationManager.getApplication().invokeLater { rescan() }
                }
            },
        )
    }

    private fun interesting(event: VFileEvent): Boolean {
        // We care about: create / delete / move of .pell or .sql files.
        // Content changes (typing in an open .pell) don't change the
        // module list — skip them so we don't rescan on every keystroke.
        val path = when (event) {
            is VFileCreateEvent -> event.path
            is VFileDeleteEvent -> event.file.path
            is VFileMoveEvent -> event.file.path
            else -> return false
        }
        val ext = path.substringAfterLast('.', missingDelimiterValue = "").lowercase()
        return ext == "pell" || ext == "sql"
    }

    fun modules(): List<PellModule> = synchronized(modulesByPath) {
        modulesByPath.values.sortedBy { it.source.path }
    }

    fun addListener(l: () -> Unit) { listeners.add(l) }
    fun removeListener(l: () -> Unit) { listeners.remove(l) }

    private fun notifyChanged() = listeners.forEach { it.invoke() }

    /** Walk the project's content roots, find every .pell file, fold
     *  into the in-memory module table. Preserves state across calls
     *  so a refresh doesn't clobber Deployed/BuildFailed markers.
     *
     *  Falls back to scanning `project.basePath` when there are no
     *  content roots — that's the common case for brand-new IDEA
     *  projects created via our wizard (no module / no `.idea/modules.xml`
     *  → `ProjectRootManager.contentRoots` returns an empty array). */
    fun rescan() {
        val rm = ProjectRootManager.getInstance(project)
        val contentRoots = rm.contentRoots
        val roots: Array<VirtualFile> = if (contentRoots.isNotEmpty()) {
            contentRoots
        } else {
            val base = project.basePath?.let {
                LocalFileSystem.getInstance().findFileByPath(it)
            }
            if (base != null) arrayOf(base) else emptyArray()
        }
        val found = mutableMapOf<String, PellModule>()
        for (root in roots) {
            VfsUtilCore.visitChildrenRecursively(root, object : VirtualFileVisitor<Void>() {
                override fun visitFile(file: VirtualFile): Boolean {
                    if (file.isDirectory) {
                        // Skip the things developers don't want crawled
                        val name = file.name
                        if (name == ".git" || name == "node_modules" || name == ".venv"
                            || name == "build" || name == "out" || name == "target"
                            || name == "__pycache__" || name == "expected") {
                            return false
                        }
                        return true
                    }
                    if (file.extension?.lowercase() == "pell") {
                        val existing = synchronized(modulesByPath) { modulesByPath[file.path] }
                        val mod = existing?.copy(source = file)
                            ?: PellModule(source = file, moduleName = parseModuleName(file))
                        // Look for a matching built .sql under the
                        // configured target dir (Settings → Tools → pell,
                        // default `plsql`). Falls back to the legacy
                        // `built/` location for projects that haven't
                        // migrated yet.
                        if (mod.built == null || !mod.built!!.exists()) {
                            val projectBase = project.basePath
                            if (projectBase != null) {
                                val targetDir = dev.pell.intellij.settings
                                    .PellSettings.getInstance(project).targetDirName
                                val lfs = LocalFileSystem.getInstance()
                                val candidates = listOf(
                                    "$projectBase/$targetDir/${file.nameWithoutExtension}.sql",
                                    "$projectBase/built/${file.nameWithoutExtension}.sql",
                                )
                                val hit = candidates.firstNotNullOfOrNull { p ->
                                    lfs.findFileByPath(p)?.takeIf { it.exists() }
                                }
                                if (hit != null) {
                                    mod.built = hit
                                    if (mod.state == PellModule.State.Unknown) {
                                        mod.state = PellModule.State.BuildOk
                                    }
                                }
                            }
                        }
                        found[file.path] = mod
                    }
                    return true
                }
            })
        }
        synchronized(modulesByPath) {
            modulesByPath.clear()
            modulesByPath.putAll(found)
        }
        notifyChanged()
    }

    /** True when the source `.pell` was modified after its built `.sql`
     *  (so the .sql is stale and needs rebuilding before deploy). */
    fun isStale(module: PellModule): Boolean {
        val src = module.source
        val built = module.built ?: return true
        return src.timeStamp > built.timeStamp
    }

    fun setState(file: VirtualFile, state: PellModule.State, message: String = "") {
        synchronized(modulesByPath) {
            modulesByPath[file.path]?.also {
                it.state = state
                it.lastMessage = message
            }
        }
        notifyChanged()
    }

    fun setBuilt(file: VirtualFile, builtPath: String) {
        val vf = LocalFileSystem.getInstance().findFileByPath(builtPath) ?: return
        synchronized(modulesByPath) {
            modulesByPath[file.path]?.also { it.built = vf }
        }
        notifyChanged()
    }

    /** Cheap-and-cheerful header parser: looks for `module foo.bar;`. */
    private fun parseModuleName(file: VirtualFile): String {
        return try {
            val text = String(file.contentsToByteArray(), Charsets.UTF_8)
            val match = Regex("""(?m)^\s*module\s+([\w.]+)\s*;""").find(text)
            match?.groupValues?.get(1) ?: file.nameWithoutExtension
        } catch (_: Exception) {
            file.nameWithoutExtension
        }
    }

    companion object {
        @JvmStatic
        fun getInstance(project: Project): PellModuleService =
            project.getService(PellModuleService::class.java)
    }
}
