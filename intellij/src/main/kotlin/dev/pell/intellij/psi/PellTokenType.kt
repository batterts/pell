package dev.pell.intellij.psi

import com.intellij.psi.tree.IElementType
import dev.pell.intellij.PellLanguage
import org.jetbrains.annotations.NonNls

/**
 * Marker IElementType subclass for every pell *terminal* token (one the
 * lexer emits). Grammar-Kit's `tokenTypeClass` in Pell.bnf points here.
 *
 * Counterpart for non-terminal AST nodes lives in PellElementType.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellTokenType(@NonNls debugName: String) : IElementType(debugName, PellLanguage) {
    override fun toString(): String = "PellTokenType.${super.toString()}"
}
