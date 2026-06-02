package dev.pell.intellij.codeInsight

import com.intellij.navigation.ChooseByNameContributor
import com.intellij.navigation.NavigationItem
import com.intellij.openapi.project.Project
import com.intellij.psi.PsiManager
import com.intellij.psi.search.FileTypeIndex
import com.intellij.psi.search.GlobalSearchScope
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.PellFileType
import dev.pell.intellij.psi.PellNamedElement

/**
 * Feeds every pell named declaration into the platform's Go To Symbol
 * popup (Cmd-Opt-O). Scans `.pell` files in the project; Phase 4-stub
 * conversion will trade scan-time for index-time without changing this
 * contract.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellGotoSymbolContributor : ChooseByNameContributor {

    override fun getNames(project: Project, includeNonProjectItems: Boolean): Array<String> {
        return allNamedElements(project).mapNotNull { it.name }.distinct().toTypedArray()
    }

    override fun getItemsByName(
        name: String,
        pattern: String,
        project: Project,
        includeNonProjectItems: Boolean,
    ): Array<NavigationItem> {
        return allNamedElements(project)
            .filter { it.name == name }
            .toTypedArray()
    }

    private fun allNamedElements(project: Project): List<PellNamedElement> {
        val manager = PsiManager.getInstance(project)
        val scope = GlobalSearchScope.allScope(project)
        return FileTypeIndex.getFiles(PellFileType, scope)
            .asSequence()
            .mapNotNull { manager.findFile(it) as? PellFile }
            .flatMap { PsiTreeUtil.findChildrenOfType(it, PellNamedElement::class.java).asSequence() }
            .toList()
    }
}
