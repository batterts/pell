package dev.pell.intellij.refactor

import com.intellij.openapi.util.TextRange
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiWhiteSpace
import com.intellij.psi.PsiComment
import dev.pell.intellij.psi.PellElementTypes

/**
 * The full source range of a top-level declaration, INCLUDING any
 * leading `pub` modifier and `@annotation`s.
 *
 * In the grammar, `item ::= annotation* KW_PUB? itemBody` inlines
 * itemBody, so the `pub` keyword and annotations are PRECEDING SIBLINGS
 * of the PellFnDef / PellRecordDef node — not part of its textRange.
 * Refactorings that move or wrap a whole declaration (Move Symbol,
 * Parameters→Record) must account for them, or they orphan the `pub`
 * keyword (leaving `pub` behind on delete, or producing `pub pub` on
 * insert-before).
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
fun declarationRange(decl: PsiElement): TextRange {
    var anchor = decl
    var prev = decl.prevSibling
    while (prev != null) {
        if (prev is PsiWhiteSpace || prev is PsiComment) {
            prev = prev.prevSibling
            continue
        }
        val t = prev.node?.elementType
        if (t == PellElementTypes.KW_PUB || t == PellElementTypes.ANNOTATION) {
            anchor = prev
            prev = prev.prevSibling
        } else {
            break
        }
    }
    return TextRange(anchor.textRange.startOffset, decl.textRange.endOffset)
}

/** Start offset of the declaration including leading `pub`/annotations. */
fun declarationStartOffset(decl: PsiElement): Int = declarationRange(decl).startOffset
