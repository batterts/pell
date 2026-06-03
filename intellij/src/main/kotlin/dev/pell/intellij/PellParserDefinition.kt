package dev.pell.intellij

import com.intellij.lang.ASTNode
import com.intellij.lang.ParserDefinition
import com.intellij.lang.PsiParser
import com.intellij.lexer.Lexer
import com.intellij.openapi.project.Project
import com.intellij.extapi.psi.PsiFileBase
import com.intellij.psi.FileViewProvider
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.TokenType
import com.intellij.psi.tree.IFileElementType
import com.intellij.psi.tree.TokenSet
// >>> PSI TRACK >>>
import dev.pell.intellij.parser.PellParser
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellLexerAdapter
// <<< PSI TRACK <<<

/**
 * Parser definition for pell files. As of PSI track Phase 2 this wires
 * the JFlex lexer ([PellLexerAdapter]) and Grammar-Kit-generated parser
 * ([PellParser]) into the IntelliJ platform. The trivial single-token
 * lexer/parser that lived here before is gone.
 *
 * Grammar lives in:
 *   - intellij/src/main/flex/Pell.flex      (token rules)
 *   - intellij/src/main/grammar/Pell.bnf    (productions)
 *
 * Generated output lands in src/main/gen/ (gitignored).
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellParserDefinition : ParserDefinition {

    // >>> PSI TRACK >>>
    override fun createLexer(project: Project?): Lexer = PellLexerAdapter()
    override fun createParser(project: Project?): PsiParser = PellParser()
    override fun getFileNodeType(): IFileElementType = FILE
    override fun getCommentTokens(): TokenSet = COMMENTS
    override fun getStringLiteralElements(): TokenSet = STRINGS
    override fun createElement(node: ASTNode): PsiElement = PellElementTypes.Factory.createElement(node)
    // <<< PSI TRACK <<<

    override fun createFile(viewProvider: FileViewProvider): PsiFile = PellFile(viewProvider)

    companion object {
        // >>> PSI TRACK >>>
        val FILE: IFileElementType = IFileElementType("PELL_FILE", PellLanguage)

        val COMMENTS: TokenSet = TokenSet.create(
            PellElementTypes.LINE_COMMENT,
            PellElementTypes.BLOCK_COMMENT,
        )

        val WHITESPACES: TokenSet = TokenSet.create(TokenType.WHITE_SPACE)

        val STRINGS: TokenSet = TokenSet.create(
            PellElementTypes.STRING,
            PellElementTypes.RAWSTRING,
        )
        // <<< PSI TRACK <<<
    }
}

class PellFile(viewProvider: FileViewProvider) : PsiFileBase(viewProvider, PellLanguage) {
    override fun getFileType() = PellFileType
    override fun toString(): String = "Pell File"
}
