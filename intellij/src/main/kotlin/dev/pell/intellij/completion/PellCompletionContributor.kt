package dev.pell.intellij.completion

import com.intellij.codeInsight.completion.CompletionContributor
import com.intellij.codeInsight.completion.CompletionParameters
import com.intellij.codeInsight.completion.CompletionProvider
import com.intellij.codeInsight.completion.CompletionResultSet
import com.intellij.codeInsight.completion.CompletionType
import com.intellij.codeInsight.lookup.LookupElementBuilder
import com.intellij.patterns.PlatformPatterns
import com.intellij.psi.util.PsiTreeUtil
import com.intellij.util.ProcessingContext
import dev.pell.intellij.PellFile
import dev.pell.intellij.PellLanguage
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellLetStmt
import dev.pell.intellij.psi.PellParam
import dev.pell.intellij.psi.PellSymbolScanner
import dev.pell.intellij.psi.PellVarStmt

/**
 * Backs Ctrl-Space autocomplete in `.pell` files with:
 *
 *   - All pell keywords (constant set).
 *   - Project-wide pub fn / record names (via PellSymbolScanner).
 *   - In-scope locals (params + lets + vars in the enclosing fn).
 *
 * LSP4IJ provides a similar but heavier completion path; this
 * contributor delivers fast inline results without round-tripping
 * through the LSP. Both feeds merge into the same lookup popup.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellCompletionContributor : CompletionContributor() {
    init {
        extend(
            CompletionType.BASIC,
            PlatformPatterns.psiElement().withLanguage(PellLanguage),
            PellCompletionProvider,
        )
    }
}

private object PellCompletionProvider : CompletionProvider<CompletionParameters>() {

    override fun addCompletions(
        parameters: CompletionParameters,
        context: ProcessingContext,
        result: CompletionResultSet,
    ) {
        val pos = parameters.position
        val file = pos.containingFile as? PellFile ?: return

        // 1. Keywords.
        KEYWORDS.forEach { kw ->
            result.addElement(LookupElementBuilder.create(kw).bold().withTypeText("keyword"))
        }

        // 2. Project-wide pub fns + records.
        for (fn in PellSymbolScanner.findPubFns(file.project)) {
            val name = fn.name ?: continue
            result.addElement(
                LookupElementBuilder.create(name)
                    .withIcon(com.intellij.icons.AllIcons.Nodes.Function)
                    .withTypeText("fn", true),
            )
        }
        for (rec in PellSymbolScanner.findPubRecords(file.project)) {
            val name = rec.name ?: continue
            result.addElement(
                LookupElementBuilder.create(name)
                    .withIcon(com.intellij.icons.AllIcons.Nodes.Record)
                    .withTypeText("record", true),
            )
        }

        // 3. In-scope locals — params + lets + vars in the enclosing fn.
        val enclosingFn = PsiTreeUtil.getParentOfType(pos, PellFnDef::class.java) ?: return
        for (p in PsiTreeUtil.findChildrenOfType(enclosingFn, PellParam::class.java)) {
            p.name?.let {
                result.addElement(LookupElementBuilder.create(it).withTypeText("param", true))
            }
        }
        for (l in PsiTreeUtil.findChildrenOfType(enclosingFn, PellLetStmt::class.java)) {
            // Only include lets that precede the caret.
            if (l.textRange.endOffset > pos.textRange.startOffset) continue
            l.name?.let {
                result.addElement(LookupElementBuilder.create(it).withTypeText("let", true))
            }
        }
        for (v in PsiTreeUtil.findChildrenOfType(enclosingFn, PellVarStmt::class.java)) {
            if (v.textRange.endOffset > pos.textRange.startOffset) continue
            v.name?.let {
                result.addElement(LookupElementBuilder.create(it).withTypeText("var", true))
            }
        }
    }

    private val KEYWORDS = listOf(
        "module", "import", "pub", "fn", "let", "var", "return", "yield",
        "if", "else", "for", "forall", "in", "match", "transaction",
        "record", "error", "true", "false", "Some", "None", "Ok", "Err",
        "unsafe", "finally", "and", "or", "not",
        "type", "sealed", "aggregate", "case", "self", "seq", "out", "inout", "enum",
    )
}
