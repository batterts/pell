package dev.pell.intellij

import com.intellij.lang.ASTNode
import com.intellij.lang.ParserDefinition
import com.intellij.lang.PsiParser
import com.intellij.lexer.Lexer
import com.intellij.lexer.LexerBase
import com.intellij.openapi.project.Project
import com.intellij.extapi.psi.PsiFileBase
import com.intellij.psi.FileViewProvider
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.tree.IElementType
import com.intellij.psi.tree.IFileElementType
import com.intellij.psi.tree.TokenSet

/**
 * Minimum-viable parser definition. PSI structure is just a single
 * "FILE" node containing the raw text; structural features (folding,
 * brace matching, etc.) come from LSP4IJ / language server interactions.
 *
 * If we later want a real PSI tree (for IntelliJ-native refactoring,
 * structure view that doesn't depend on LSP, etc.), this is where we'd
 * grow a Grammar-Kit BNF.
 */
class PellParserDefinition : ParserDefinition {

    override fun createLexer(project: Project?): Lexer = PellTrivialLexer()
    override fun createParser(project: Project?): PsiParser = PellTrivialParser()
    override fun getFileNodeType(): IFileElementType = FILE
    override fun getCommentTokens(): TokenSet = TokenSet.EMPTY
    override fun getStringLiteralElements(): TokenSet = TokenSet.EMPTY
    override fun createElement(node: ASTNode): PsiElement {
        throw UnsupportedOperationException("pell PSI not implemented; using LSP instead")
    }
    override fun createFile(viewProvider: FileViewProvider): PsiFile = PellFile(viewProvider)

    companion object {
        val FILE = IFileElementType(PellLanguage)
        val SOURCE_TOKEN = IElementType("PELL_SOURCE", PellLanguage)
    }
}

class PellFile(viewProvider: FileViewProvider) : PsiFileBase(viewProvider, PellLanguage) {
    override fun getFileType() = PellFileType
}

/** Trivial single-token lexer — emits the whole buffer as one token. */
private class PellTrivialLexer : LexerBase() {
    private lateinit var buffer: CharSequence
    private var start = 0
    private var end = 0
    private var state = 0

    override fun start(buffer: CharSequence, startOffset: Int, endOffset: Int, initialState: Int) {
        this.buffer = buffer
        this.start = startOffset
        this.end = endOffset
        this.state = initialState
    }

    override fun getState(): Int = state
    override fun getTokenType(): IElementType? = if (start < end) PellParserDefinition.SOURCE_TOKEN else null
    override fun getTokenStart(): Int = start
    override fun getTokenEnd(): Int = end
    override fun advance() { start = end }
    override fun getBufferSequence(): CharSequence = buffer
    override fun getBufferEnd(): Int = end
}

/** Trivial parser — wraps the single source token in the file root. */
private class PellTrivialParser : PsiParser {
    override fun parse(root: IElementType, builder: com.intellij.lang.PsiBuilder): ASTNode {
        val marker = builder.mark()
        while (!builder.eof()) builder.advanceLexer()
        marker.done(root)
        return builder.treeBuilt
    }
}
