package dev.pell.intellij.codeInsight

import com.intellij.lang.HelpID
import com.intellij.lang.cacheBuilder.DefaultWordsScanner
import com.intellij.lang.cacheBuilder.WordsScanner
import com.intellij.lang.findUsages.FindUsagesProvider
import com.intellij.psi.PsiElement
import com.intellij.psi.tree.TokenSet
import dev.pell.intellij.PellParserDefinition
import dev.pell.intellij.psi.PellAggregateDef
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellEnumDef
import dev.pell.intellij.psi.PellErrorDef
import dev.pell.intellij.psi.PellFieldDef
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellLexerAdapter
import dev.pell.intellij.psi.PellMethodDef
import dev.pell.intellij.psi.PellModuleDecl
import dev.pell.intellij.psi.PellNamedElement
import dev.pell.intellij.psi.PellParam
import dev.pell.intellij.psi.PellRecordDef
import dev.pell.intellij.psi.PellSealedTypeDef
import dev.pell.intellij.psi.PellSeqDef
import dev.pell.intellij.psi.PellTypeDef

/**
 * Tells the platform's Find Usages machinery (Cmd-G) that pell named
 * elements are findable, and provides labels for the results popup.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellFindUsagesProvider : FindUsagesProvider {

    override fun canFindUsagesFor(element: PsiElement): Boolean = element is PellNamedElement

    override fun getHelpId(element: PsiElement): String? = HelpID.FIND_OTHER_USAGES

    override fun getType(element: PsiElement): String = when (element) {
        is PellFnDef           -> "function"
        is PellMethodDef       -> "method"
        is PellRecordDef       -> "record"
        is PellErrorDef        -> "error"
        is PellTypeDef         -> "type"
        is PellSealedTypeDef   -> "sealed type"
        is PellAggregateDef    -> "aggregate"
        is PellEnumDef         -> "enum"
        is PellSeqDef          -> "sequence"
        is PellFieldDef        -> "field"
        is PellParam           -> "parameter"
        is PellModuleDecl      -> "module"
        else                   -> "symbol"
    }

    override fun getDescriptiveName(element: PsiElement): String =
        (element as? PellNamedElement)?.name.orEmpty()

    override fun getNodeText(element: PsiElement, useFullName: Boolean): String =
        getDescriptiveName(element)

    override fun getWordsScanner(): WordsScanner = DefaultWordsScanner(
        PellLexerAdapter(),
        /* identifiers */     TokenSet.create(PellElementTypes.IDENT),
        /* comments */        PellParserDefinition.COMMENTS,
        /* literals */        PellParserDefinition.STRINGS,
    )
}
