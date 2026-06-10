package dev.pell.intellij.refactor

import com.intellij.lang.refactoring.RefactoringSupportProvider
import com.intellij.psi.PsiElement
import com.intellij.refactoring.RefactoringActionHandler
import dev.pell.intellij.psi.PellNamedElement
import dev.pell.intellij.refactor.extract.PellExtractMethodHandler

/**
 * Wires pell into the platform's refactoring machinery. The
 * `<refactoring.extractMethod>` extension point referenced earlier
 * doesn't exist — Extract Method (Refactor ▸ Extract Method… /
 * Cmd-Alt-M) is surfaced through getExtractMethodHandler() here, and
 * the platform's "Refactor This…" popup queries this provider.
 *
 * Registered via `<lang.refactoringSupport language="pell">`.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellRefactoringSupportProvider : RefactoringSupportProvider() {

    override fun getExtractMethodHandler(): RefactoringActionHandler =
        PellExtractMethodHandler()

    /** Members that can be safely renamed in place (drives the
     *  inline-rename UX vs. the dialog). */
    override fun isMemberInplaceRenameAvailable(
        element: PsiElement,
        context: PsiElement?,
    ): Boolean = element is PellNamedElement

    override fun isInplaceRenameAvailable(
        element: PsiElement,
        context: PsiElement?,
    ): Boolean = element is PellNamedElement
}
