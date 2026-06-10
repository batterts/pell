package dev.pell.intellij.psi.impl

import com.intellij.lang.ASTNode
import com.intellij.openapi.util.TextRange
import com.intellij.psi.PsiReference
import dev.pell.intellij.psi.PellQualifiedExpr
import dev.pell.intellij.psi.PellQualifiedReference

/**
 * Mixin base for the generated PellQualifiedExprImpl. Overrides
 * getReference() so a `foo::bar::baz` (or bare `foo`) expression
 * carries a hard PsiReference back to its declaration.
 *
 * Why a mixin instead of the PsiReferenceContributor: Grammar-Kit's
 * generated impls extend ASTWrapperPsiElement, which is NOT a
 * ContributedReferenceHost. IntelliJ's SharedPsiElementImplUtil only
 * consults the ReferenceProvidersRegistry (where contributors live)
 * for ContributedReferenceHost elements — so the contributor's
 * references were silently dropped, breaking Find Usages and PSI
 * Rename. A hard reference from getReference() is consulted
 * unconditionally by findReferenceAt() and ReferencesSearch.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
abstract class PellQualifiedExprMixin(node: ASTNode) :
    PellPsiElementImpl(node), PellQualifiedExpr {

    override fun getReference(): PsiReference? {
        // Range covers only the LAST segment of `foo::bar::baz`, so
        // rename/find-usages key on the tail identifier. Strip any
        // struct-literal body (`Foo { ... }`) from the path text first.
        val pathText = text.substringBefore('{').trimEnd()
        if (pathText.isEmpty()) return null
        val lastSep = pathText.lastIndexOf("::")
        val lastStart = if (lastSep >= 0) lastSep + 2 else 0
        return PellQualifiedReference(this, TextRange(lastStart, pathText.length))
    }

    override fun getReferences(): Array<PsiReference> {
        val ref = reference ?: return PsiReference.EMPTY_ARRAY
        return arrayOf(ref)
    }
}
