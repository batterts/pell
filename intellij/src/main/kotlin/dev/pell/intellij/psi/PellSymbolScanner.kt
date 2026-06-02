package dev.pell.intellij.psi

import com.intellij.openapi.project.Project
import com.intellij.psi.PsiManager
import com.intellij.psi.search.FileTypeIndex
import com.intellij.psi.search.GlobalSearchScope
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.PellFileType

/**
 * Project-wide lookup for pell named declarations.
 *
 * Phase 3 implementation: walks every `.pell` file in the project and
 * collects [PellNamedElement]s by name. O(project size) per lookup —
 * fine for projects up to ~1k files. Phase 4 will replace the
 * implementation with a stub-backed [com.intellij.psi.stubs.StubIndex]
 * for O(1) lookup; the public API on this object will not change.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
object PellSymbolScanner {

    /** All pell files in the project's source scope. */
    private fun allFiles(project: Project): Sequence<PellFile> {
        val manager = PsiManager.getInstance(project)
        val scope = GlobalSearchScope.allScope(project)
        return FileTypeIndex.getFiles(PellFileType, scope)
            .asSequence()
            .mapNotNull { manager.findFile(it) as? PellFile }
    }

    /** All public functions visible at module scope, optionally filtered
     *  by name. */
    fun findPubFns(project: Project, name: String? = null): List<PellFnDef> =
        allFiles(project)
            .flatMap { PsiTreeUtil.getChildrenOfTypeAsList(it, PellFnDef::class.java).asSequence() }
            .filter { /* TODO: is_pub flag once Phase 4 adds a child accessor */ true }
            .filter { name == null || it.name == name }
            .toList()

    /** All public records project-wide, optionally filtered by name. */
    fun findPubRecords(project: Project, name: String? = null): List<PellRecordDef> =
        allFiles(project)
            .flatMap { PsiTreeUtil.getChildrenOfTypeAsList(it, PellRecordDef::class.java).asSequence() }
            .filter { name == null || it.name == name }
            .toList()

    /** All module declarations project-wide, keyed by their `module foo.bar`
     *  name. Used by import resolution. */
    fun findModule(project: Project, dottedName: String): PellModuleDecl? =
        allFiles(project)
            .mapNotNull { PsiTreeUtil.getChildOfType(it, PellModuleDecl::class.java) }
            .firstOrNull { it.name == dottedName }

    /** All [PellNamedElement]s in the project matching the given name,
     *  regardless of declaration kind. Used by Find Usages and Rename
     *  to enumerate candidates before per-reference filtering. */
    fun findAllByName(project: Project, name: String): List<PellNamedElement> =
        allFiles(project)
            .flatMap { file ->
                PsiTreeUtil.findChildrenOfType(file, PellNamedElement::class.java).asSequence()
            }
            .filter { it.name == name }
            .toList()
}
