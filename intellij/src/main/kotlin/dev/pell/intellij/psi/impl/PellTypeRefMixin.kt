package dev.pell.intellij.psi.impl

import com.intellij.lang.ASTNode
import com.intellij.openapi.util.TextRange
import com.intellij.psi.PsiReference
import dev.pell.intellij.psi.PellTypeRef
import dev.pell.intellij.psi.PellTypeReference

/**
 * Mixin base for the generated PellTypeRefImpl. Overrides
 * getReference() so a named type reference (`Employee`,
 * `list<Employee>`, `pkg::Employee`) carries a hard PsiReference back
 * to the declaring record/type/enum/error.
 *
 * Same rationale as [PellQualifiedExprMixin] — the contributor path
 * is silently dropped for non-ContributedReferenceHost elements.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
abstract class PellTypeRefMixin(node: ASTNode) :
    PellPsiElementImpl(node), PellTypeRef {

    override fun getReference(): PsiReference? {
        if (text.isEmpty()) return null
        // Whole-text range; PellTypeReference resolves the bare name
        // (strips generics + qualifier internally).
        return PellTypeReference(this, TextRange(0, text.length))
    }

    override fun getReferences(): Array<PsiReference> {
        val ref = reference ?: return PsiReference.EMPTY_ARRAY
        return arrayOf(ref)
    }
}
