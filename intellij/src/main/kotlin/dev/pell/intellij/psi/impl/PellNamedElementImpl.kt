package dev.pell.intellij.psi.impl

import com.intellij.lang.ASTNode
import com.intellij.psi.PsiElement
import dev.pell.intellij.psi.PellNameIdentifier
import dev.pell.intellij.psi.PellNamedElement
import dev.pell.intellij.psi.PellPsiFactory

/**
 * Base class for every pell named declaration (fn, record, error,
 * type, sealedType, aggregate, seq, enum, method, case, enumVariant,
 * param, field, let, var, loopVar, module).
 *
 * Implements PsiNameIdentifierOwner by walking children for the first
 * [PellNameIdentifier] (the *Name wrapper) and returning it. That
 * lets the platform's Rename, Find Usages, and reference machinery
 * locate the name token without per-rule code in every declaration.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
abstract class PellNamedElementImpl(node: ASTNode) : PellPsiElementImpl(node), PellNamedElement {

    override fun getName(): String? = nameIdentifier?.text

    override fun setName(name: String): PsiElement {
        val newId = PellPsiFactory.createIdent(project, name)
            ?: return this
        nameIdentifier?.let { it.firstChild?.replace(newId) ?: it.replace(newId) }
        return this
    }

    override fun getNameIdentifier(): PsiElement? {
        var c: PsiElement? = firstChild
        while (c != null) {
            if (c is PellNameIdentifier) return c
            c = c.nextSibling
        }
        return null
    }

    override fun getTextOffset(): Int = nameIdentifier?.textOffset ?: super.getTextOffset()
}
