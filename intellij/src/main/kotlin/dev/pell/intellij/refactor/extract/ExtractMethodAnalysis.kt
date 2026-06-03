package dev.pell.intellij.refactor.extract

import com.intellij.openapi.editor.Editor
import com.intellij.openapi.util.TextRange
import com.intellij.psi.PsiElement
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellBlock
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellLetStmt
import dev.pell.intellij.psi.PellMethodDef
import dev.pell.intellij.psi.PellParam
import dev.pell.intellij.psi.PellReturnStmt
import dev.pell.intellij.psi.PellStmt
import dev.pell.intellij.psi.PellVarStmt

/**
 * Pure analysis of a selection range against the enclosing pell fn.
 * Decides:
 *
 *   - Is the selection extractable? (must be a contiguous sequence of
 *     stmts inside one block; can't straddle an arm/case boundary)
 *   - What free identifiers does it read? (→ extracted-fn parameters)
 *   - What does it return / yield / propagate?
 *
 * Pure code: no edits, no UI. The caller (PellExtractMethodHandler)
 * uses the result to drive synthesis + replacement.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
data class ExtractMethodAnalysis(
    /** The selection rewritten to a TextRange covering whole statements. */
    val selectionRange: TextRange,
    /** The enclosing block whose children are partially selected. */
    val enclosingBlock: PellBlock,
    /** The enclosing fn/method that the extracted target will live next to. */
    val enclosingFnOrMethod: PsiElement,
    /** The contiguous stmt list inside the block that's actually selected. */
    val selectedStmts: List<PellStmt>,
    /** Identifiers read inside the selection that resolve to locals
     *  declared OUTSIDE the selection — these become parameters. */
    val capturedParams: List<CapturedParam>,
    /** True if a `return` statement appears inside the selection. */
    val containsReturn: Boolean,
    /** True if a `?` operator appears inside the selection (propagation). */
    val containsQuestionMark: Boolean,
    /** Why the selection is unextractable, or null if it's fine. */
    val rejectionReason: String?,
) {
    data class CapturedParam(val name: String, val declaration: PsiElement)

    val isExtractable: Boolean get() = rejectionReason == null
}

object ExtractMethodAnalyzer {

    /**
     * Compute analysis for the current selection. Returns a result
     * whose `rejectionReason` is non-null if extraction can't proceed.
     */
    fun analyze(file: PellFile, editor: Editor): ExtractMethodAnalysis {
        val sel = TextRange(editor.selectionModel.selectionStart, editor.selectionModel.selectionEnd)
        if (sel.isEmpty) {
            return rejection(file, "no selection")
        }

        val startElem = file.findElementAt(sel.startOffset) ?: return rejection(file, "selection out of range")
        val endElem = file.findElementAt((sel.endOffset - 1).coerceAtLeast(0)) ?: return rejection(file, "selection out of range")

        // Both endpoints must live in the same PellBlock.
        val startBlock = PsiTreeUtil.getParentOfType(startElem, PellBlock::class.java)
        val endBlock = PsiTreeUtil.getParentOfType(endElem, PellBlock::class.java)
        if (startBlock == null || startBlock != endBlock) {
            return rejection(file, "selection must lie inside a single { ... } block")
        }

        // Enclosing fn or method (where the extracted target gets inserted).
        val enclosingFn = PsiTreeUtil.getParentOfType(
            startBlock, PellFnDef::class.java, PellMethodDef::class.java,
        ) ?: return rejection(file, "selection is not inside a fn or method")

        // Walk the block's children; pick the contiguous stmt subsequence
        // whose collective text range covers the selection.
        val allStmts = PsiTreeUtil.getChildrenOfTypeAsList(startBlock, PellStmt::class.java)
        val selectedStmts = allStmts.filter { it.textRange.intersects(sel) }
        if (selectedStmts.isEmpty()) {
            return rejection(file, "selection contains no complete statement")
        }

        val effectiveRange = TextRange(
            selectedStmts.first().textRange.startOffset,
            selectedStmts.last().textRange.endOffset,
        )

        // Identifier analysis: collect every IDENT in the selection that's
        // a free variable (not declared inside the selection).
        val captured = collectCapturedParams(selectedStmts)
        val hasReturn = selectedStmts.any { PsiTreeUtil.findChildOfType(it, PellReturnStmt::class.java) != null }
        val hasQuestion = selectedStmts.any {
            PsiTreeUtil.findChildrenOfType(it, PsiElement::class.java)
                .any { e -> e.node?.elementType == PellElementTypes.QUESTION }
        }

        // Phase 6 rejection rules — match the safety bar promised in the
        // PSI_TRACK plan. Smarter handling lands in v0.8.x.
        if (hasReturn && selectedStmts.size > 1) {
            return ExtractMethodAnalysis(
                effectiveRange, startBlock, enclosingFn, selectedStmts, captured,
                hasReturn, hasQuestion,
                rejectionReason = "selection contains `return` and other stmts — split the return into its own extraction",
            )
        }

        return ExtractMethodAnalysis(
            effectiveRange, startBlock, enclosingFn, selectedStmts, captured,
            hasReturn, hasQuestion,
            rejectionReason = null,
        )
    }

    private fun rejection(file: PellFile, msg: String): ExtractMethodAnalysis = ExtractMethodAnalysis(
        selectionRange = TextRange.EMPTY_RANGE,
        enclosingBlock = file.firstChild?.let { PsiTreeUtil.findChildOfType(it, PellBlock::class.java) }
            ?: PsiTreeUtil.findChildOfType(file, PellBlock::class.java)!!,
        enclosingFnOrMethod = file,
        selectedStmts = emptyList(),
        capturedParams = emptyList(),
        containsReturn = false,
        containsQuestionMark = false,
        rejectionReason = msg,
    )

    /**
     * Collect every IDENT inside the selection whose declaration lives
     * OUTSIDE the selection. Those become parameters of the extracted fn.
     *
     * Resolution is deliberately scoped to the enclosing fn — anything
     * declared at module level is a globally-visible symbol that the
     * extracted fn can reference without becoming a parameter.
     */
    private fun collectCapturedParams(stmts: List<PellStmt>): List<ExtractMethodAnalysis.CapturedParam> {
        val selectionRange = TextRange(
            stmts.first().textRange.startOffset,
            stmts.last().textRange.endOffset,
        )

        // Names declared INSIDE the selection (let/var/for-loop var).
        val internalDecls = mutableSetOf<String>()
        for (s in stmts) {
            PsiTreeUtil.findChildrenOfType(s, PellLetStmt::class.java).forEach { l ->
                l.name?.let { internalDecls.add(it) }
            }
            PsiTreeUtil.findChildrenOfType(s, PellVarStmt::class.java).forEach { v ->
                v.name?.let { internalDecls.add(it) }
            }
        }

        // Reads inside the selection, in stable encounter order.
        val captured = LinkedHashMap<String, PsiElement>()
        for (s in stmts) {
            PsiTreeUtil.findChildrenOfType(s, PsiElement::class.java)
                .filter { it.node?.elementType == PellElementTypes.IDENT }
                .forEach { ident ->
                    val name = ident.text
                    if (name in internalDecls) return@forEach
                    if (name in captured) return@forEach
                    // Look for a declaration of `name` outside the selection but
                    // inside the enclosing fn body. If found, that's a captured
                    // local; record it. If not found, it's either a module-scope
                    // symbol or a builtin — leave it alone.
                    val decl = findEnclosingDeclaration(ident, name, selectionRange)
                    if (decl != null) {
                        captured[name] = decl
                    }
                }
        }
        return captured.map { (n, d) -> ExtractMethodAnalysis.CapturedParam(n, d) }
    }

    /**
     * Search the enclosing fn body for a let/var/param/for-loop var named
     * `name` that lives OUTSIDE the selection range. Returns the
     * declaration if found, else null.
     */
    private fun findEnclosingDeclaration(ident: PsiElement, name: String, selectionRange: TextRange): PsiElement? {
        val enclosingFn = PsiTreeUtil.getParentOfType(ident, PellFnDef::class.java, PellMethodDef::class.java) ?: return null

        // Params of the enclosing fn.
        PsiTreeUtil.findChildrenOfType(enclosingFn, PellParam::class.java)
            .firstOrNull { it.name == name }
            ?.let { return it }

        // let/var declarations that precede the selection.
        for (let in PsiTreeUtil.findChildrenOfType(enclosingFn, PellLetStmt::class.java)) {
            if (let.name == name && !selectionRange.contains(let.textRange)) return let
        }
        for (v in PsiTreeUtil.findChildrenOfType(enclosingFn, PellVarStmt::class.java)) {
            if (v.name == name && !selectionRange.contains(v.textRange)) return v
        }
        return null
    }
}
