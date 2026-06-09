package dev.pell.intellij.psi

import com.intellij.openapi.util.TextRange
import com.intellij.psi.AbstractElementManipulator
import dev.pell.intellij.psi.impl.PellQualifiedExprMixin
import dev.pell.intellij.psi.impl.PellTypeRefMixin

/**
 * ElementManipulators tell the platform HOW to rewrite the text of a
 * reference-bearing element. Rename (and any other reference-edit)
 * calls reference.handleElementRename, whose default implementation
 * delegates to the manipulator's handleContentChange over the
 * reference's range. Without one registered, Rename throws
 * "No ElementManipulator instance registered for ...".
 *
 * These splice the new identifier into the element's existing text at
 * the reference range (the LAST segment for a qualified path, the
 * whole name for a type ref) and re-parse via PellPsiFactory.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellQualifiedExprManipulator :
    AbstractElementManipulator<PellQualifiedExprMixin>() {

    override fun handleContentChange(
        element: PellQualifiedExprMixin,
        range: TextRange,
        newContent: String,
    ): PellQualifiedExprMixin {
        val oldText = element.text
        val newText = range.replace(oldText, newContent)
        val replacement = PellPsiFactory.createQualifiedExpr(element.project, newText)
        return element.replace(replacement) as PellQualifiedExprMixin
    }
}

class PellTypeRefManipulator :
    AbstractElementManipulator<PellTypeRefMixin>() {

    override fun handleContentChange(
        element: PellTypeRefMixin,
        range: TextRange,
        newContent: String,
    ): PellTypeRefMixin {
        val oldText = element.text
        val newText = range.replace(oldText, newContent)
        val replacement = PellPsiFactory.createTypeRef(element.project, newText)
        return element.replace(replacement) as PellTypeRefMixin
    }
}
