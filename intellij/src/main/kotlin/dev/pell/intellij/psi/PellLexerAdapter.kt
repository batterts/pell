package dev.pell.intellij.psi

import com.intellij.lexer.FlexAdapter
import dev.pell.intellij.parser._PellLexer

/**
 * Adapts the JFlex-generated [_PellLexer] to IntelliJ's Lexer interface.
 * Wired into [dev.pell.intellij.PellParserDefinition] in Phase 2.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellLexerAdapter : FlexAdapter(_PellLexer(null))
