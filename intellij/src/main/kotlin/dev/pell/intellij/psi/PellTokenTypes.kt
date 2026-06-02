package dev.pell.intellij.psi

/**
 * Hand-maintained token-type constants referenced by the JFlex lexer
 * (Pell.flex's `return IDENT;` etc.). Grammar-Kit's generated
 * PellElementTypes holds the AST-node element types; this file holds
 * the *terminal* token types separately so the JFlex spec doesn't have
 * to depend on a generated file (chicken-and-egg).
 *
 * Names here must match the `tokens = [ ... ]` block in Pell.bnf
 * exactly — Grammar-Kit verifies the cross-reference at generation
 * time.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
object PellTokenTypes {
    @JvmField val IDENT = PellTokenType("IDENT")
    @JvmField val LINE_COMMENT = PellTokenType("LINE_COMMENT")
    @JvmField val BLOCK_COMMENT = PellTokenType("BLOCK_COMMENT")

    // Phase 1 will add: keywords, string literal pieces, regex literal,
    // numbers, punctuation, sql!{}/jq!{} block tokens, operator family.
}
