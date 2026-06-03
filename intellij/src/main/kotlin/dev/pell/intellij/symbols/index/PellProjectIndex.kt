package dev.pell.intellij.symbols.index

import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.psi.PsiManager
import com.intellij.psi.search.FileTypeIndex
import com.intellij.psi.search.GlobalSearchScope
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.PellFileType
import dev.pell.intellij.psi.PellEnumDef
import dev.pell.intellij.psi.PellErrorDef
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellModuleDecl
import dev.pell.intellij.psi.PellNamedElement
import dev.pell.intellij.psi.PellRecordDef
import dev.pell.intellij.psi.PellSealedTypeDef
import dev.pell.intellij.psi.PellSeqDef
import dev.pell.intellij.psi.PellTypeDef
import java.util.concurrent.ConcurrentHashMap

/**
 * Project-level in-memory index of every named pell declaration —
 * the parsed-lookup the user asked about. Replaces
 * [dev.pell.intellij.psi.PellSymbolScanner]'s PsiTreeUtil walk with
 * O(1) HashMap lookups.
 *
 * **Trade vs. a real [com.intellij.psi.stubs.StubIndex]:** this index
 * doesn't persist to disk and doesn't get refreshed by IntelliJ's
 * native cache infrastructure — it rebuilds on project open and
 * invalidates per-file via [PellProjectIndexInvalidator] (VFS
 * listener). For the project sizes pell targets (≤ few thousand
 * files) the rebuild is cheap (one PSI walk per file). Disk-persistent
 * StubIndex is the v0.5.0 upgrade if the in-memory rebuild ever feels
 * heavy.
 *
 * Owned by L2 of LSP_PSI_UNIFICATION.md (revised — see
 * `intellij/LSP_PSI_UNIFICATION.md`).
 */
@Service(Service.Level.PROJECT)
class PellProjectIndex(private val project: Project) {

    /** name -> all matching named elements, project-wide. */
    private val byName = ConcurrentHashMap<String, MutableList<PellNamedElement>>()

    /** "module::name" -> elements matching that exact qualified path. */
    private val byQName = ConcurrentHashMap<String, MutableList<PellNamedElement>>()

    /** "module.foo.bar" -> the PellModuleDecl declaring it. */
    private val byModule = ConcurrentHashMap<String, PellModuleDecl>()

    /** VirtualFile -> what we indexed from it (so we can remove on
     *  change). One entry per .pell file. */
    private val perFile = ConcurrentHashMap<VirtualFile, FileEntry>()

    @Volatile private var initialized = false

    /** O(1) lookup by simple name (e.g. `info` matches both
     *  `logger::info` and `audit::info` if both exist). */
    fun findByName(name: String): List<PellNamedElement> {
        ensureInitialized()
        return byName[name]?.toList() ?: emptyList()
    }

    /** O(1) lookup by full qualified path (`logger::info`). */
    fun findByQualifiedName(qname: String): List<PellNamedElement> {
        ensureInitialized()
        return byQName[qname]?.toList() ?: emptyList()
    }

    /** Find the module declaration matching a dotted name
     *  (`pell_test.hello`). */
    fun findModule(dottedName: String): PellModuleDecl? {
        ensureInitialized()
        return byModule[dottedName]
    }

    /** All named elements with a given kind. Cheap iteration over the
     *  index; the cost is filtering, not scanning. */
    fun allOfKind(predicate: (PellNamedElement) -> Boolean): List<PellNamedElement> {
        ensureInitialized()
        return byName.values.flatten().filter(predicate)
    }

    /** Called by [PellProjectIndexInvalidator] when a `.pell` file is
     *  touched. Drops old entries, rebuilds new ones, all under one
     *  ReadAction. */
    fun reindexFile(file: VirtualFile) {
        ReadAction.run<RuntimeException> {
            // Drop old.
            perFile.remove(file)?.let { old -> removeEntry(old) }
            // Rebuild.
            val psi = PsiManager.getInstance(project).findFile(file) as? PellFile ?: return@run
            val entry = scanFile(psi)
            perFile[file] = entry
            addEntry(entry)
        }
    }

    /** Drop everything we have on a file — called by the listener on
     *  delete/move events. */
    fun forgetFile(file: VirtualFile) {
        perFile.remove(file)?.let { removeEntry(it) }
    }

    private fun ensureInitialized() {
        if (initialized) return
        synchronized(this) {
            if (initialized) return
            ReadAction.run<RuntimeException> {
                val scope = GlobalSearchScope.allScope(project)
                for (vf in FileTypeIndex.getFiles(PellFileType, scope)) {
                    val psi = PsiManager.getInstance(project).findFile(vf) as? PellFile
                        ?: continue
                    val entry = scanFile(psi)
                    perFile[vf] = entry
                    addEntry(entry)
                }
            }
            initialized = true
        }
    }

    /** Parse one file into a FileEntry. Reads the module decl and
     *  every named element via PsiTreeUtil walk — same work the old
     *  scanner did, but only once per file (not once per query). */
    private fun scanFile(file: PellFile): FileEntry {
        val module = PsiTreeUtil.findChildOfType(file, PellModuleDecl::class.java)
        val moduleName = module?.name
        val moduleTail = moduleName?.substringAfterLast('.')

        val symbols = mutableListOf<IndexedSymbol>()
        PsiTreeUtil.findChildrenOfType(file, PellNamedElement::class.java).forEach { named ->
            // Only index top-level decls and methods — not let/var/param/
            // loop-var locals, which the inspector doesn't need to find
            // across files.
            if (!isIndexedKind(named)) return@forEach
            val short = named.name ?: return@forEach
            val qname = if (moduleTail != null) "${moduleTail}::$short" else short
            symbols += IndexedSymbol(short, qname, named)
        }
        return FileEntry(moduleName, module, symbols)
    }

    private fun isIndexedKind(named: PellNamedElement): Boolean = when (named) {
        is PellFnDef, is PellRecordDef, is PellErrorDef,
        is PellTypeDef, is PellSealedTypeDef,
        is PellEnumDef, is PellSeqDef -> true
        else -> false
    }

    private fun addEntry(entry: FileEntry) {
        entry.moduleName?.let { name ->
            entry.moduleDecl?.let { byModule[name] = it }
        }
        for (sym in entry.symbols) {
            byName.computeIfAbsent(sym.name) { mutableListOf() }.add(sym.element)
            byQName.computeIfAbsent(sym.qname) { mutableListOf() }.add(sym.element)
        }
    }

    private fun removeEntry(entry: FileEntry) {
        entry.moduleName?.let { byModule.remove(it) }
        for (sym in entry.symbols) {
            byName[sym.name]?.remove(sym.element)
            if (byName[sym.name]?.isEmpty() == true) byName.remove(sym.name)
            byQName[sym.qname]?.remove(sym.element)
            if (byQName[sym.qname]?.isEmpty() == true) byQName.remove(sym.qname)
        }
    }

    private data class IndexedSymbol(
        val name: String,
        val qname: String,
        val element: PellNamedElement,
    )

    private data class FileEntry(
        val moduleName: String?,
        val moduleDecl: PellModuleDecl?,
        val symbols: List<IndexedSymbol>,
    )

    companion object {
        fun getInstance(project: Project): PellProjectIndex = project.service()
    }
}
