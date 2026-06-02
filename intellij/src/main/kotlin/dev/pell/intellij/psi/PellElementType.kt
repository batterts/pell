package dev.pell.intellij.psi

import com.intellij.psi.tree.IElementType
import dev.pell.intellij.PellLanguage
import org.jetbrains.annotations.NonNls

/**
 * Marker IElementType subclass for every pell *non-terminal* AST node
 * produced by the parser. Grammar-Kit's `elementTypeClass` in Pell.bnf
 * points here.
 *
 * Counterpart for lexer tokens lives in PellTokenType.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellElementType(@NonNls debugName: String) : IElementType(debugName, PellLanguage)
