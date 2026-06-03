package dev.pell.intellij.refactor

import com.intellij.psi.PsiElement
import com.intellij.refactoring.rename.RenamePsiElementProcessor
import dev.pell.intellij.psi.PellModuleDecl
import dev.pell.intellij.psi.PellNamedElement

/**
 * Custom rename processor for pell. Most of the work is handled by the
 * platform's default flow (PsiNameIdentifierOwner → setName, references
 * via [com.intellij.psi.PsiReference.handleElementRename] using each
 * reference's range). This subclass exists for the per-symbol edge
 * cases.
 *
 * Phase 5 scope (file-local + project-wide reference rewriting) is
 * handled fully by the default flow. This class:
 *
 *   1. Claims ownership of every [PellNamedElement] so the platform
 *      uses our [PellNamesValidator] for new-name checks.
 *   2. Disables in-place rename for [PellModuleDecl] — the popup-style
 *      rename in the editor doesn't make sense for a dotted name like
 *      `hr.employees`. The full Refactor → Rename dialog still works.
 *
 * Future phases (file/directory rename for module symbols, smart
 * rename of associated PL/SQL package files in plsql/) hang off this
 * class.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellRenameProcessor : RenamePsiElementProcessor() {

    override fun canProcessElement(element: PsiElement): Boolean = element is PellNamedElement

    override fun isInplaceRenameSupported(): Boolean = true

    /** The dialog-based rename always works; this only governs the popup
     *  in-place flow that fires on `Refactor → Rename` from a single
     *  IDENT inside the editor. Module names are dotted so the in-place
     *  editor would mis-render the trailing segments. */
    override fun forcesShowPreview(): Boolean = false
}
