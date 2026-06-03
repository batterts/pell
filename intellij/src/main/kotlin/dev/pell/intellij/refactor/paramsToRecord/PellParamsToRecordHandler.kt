package dev.pell.intellij.refactor.paramsToRecord

import com.intellij.openapi.actionSystem.DataContext
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFile
import com.intellij.psi.util.PsiTreeUtil
import com.intellij.refactoring.RefactoringActionHandler
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellCallOp
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellMethodDef
import dev.pell.intellij.psi.PellParam
import dev.pell.intellij.psi.PellSymbolScanner

/**
 * Extract a fn's parameter list into a new pub record. Rewrites the
 * signature, the body's parameter references, and every call site.
 *
 * Bound to the `Parameters to Record` action under Refactor This.
 *
 * Algorithm:
 *
 *   1. From the caret position, find the enclosing PellFnDef. Reject
 *      if there isn't one or it has zero params.
 *   2. Ask the user for: the record name (default <FnName>Args), the
 *      receiver param name in the rewritten signature (default `arg`).
 *   3. Inside a single WriteCommandAction:
 *      a. Insert `pub record <RecordName> { <fields> }` above the fn.
 *      b. Rewrite the param list to `<argName>: <RecordName>`.
 *      c. Walk the fn body; rewrite each free occurrence of an old
 *         param name to `<argName>.<paramName>`.
 *      d. Find every PellCallOp whose target resolves to this fn (via
 *         PellSymbolScanner — same-name lookup, type filter coming in
 *         Phase 8). Rewrite the args list to
 *         `<RecordName> { <field>: <orig_arg>, ... }`.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellParamsToRecordHandler : RefactoringActionHandler {

    override fun invoke(project: Project, editor: Editor?, file: PsiFile?, dataContext: DataContext?) {
        if (editor == null || file !is PellFile) return

        val elemAtCaret = file.findElementAt(editor.caretModel.offset) ?: return
        val fn = PsiTreeUtil.getParentOfType(elemAtCaret, PellFnDef::class.java, PellMethodDef::class.java)
            ?: return rejection(project, "place the caret inside a fn or method")
        val fnName = (fn as? PellFnDef)?.name ?: (fn as? PellMethodDef)?.name
            ?: return rejection(project, "fn has no name")
        val params = PsiTreeUtil.findChildrenOfType(fn, PellParam::class.java).toList()
        if (params.isEmpty()) {
            return rejection(project, "fn has no parameters")
        }

        val defaultRecordName = fnName.replaceFirstChar { it.uppercaseChar() } + "Args"
        val recordName = Messages.showInputDialog(
            project,
            "Record name:",
            "Parameters to Record",
            null,
            defaultRecordName,
            null,
        ) ?: return

        val argName = Messages.showInputDialog(
            project,
            "Receiver parameter name in the rewritten signature:",
            "Parameters to Record",
            null,
            "args",
            null,
        ) ?: return

        applyExtract(project, file, fn, fnName, params, recordName, argName)
    }

    override fun invoke(project: Project, elements: Array<out PsiElement>, dataContext: DataContext?) {
        // No-op: caret-driven only.
    }

    private fun applyExtract(
        project: Project,
        file: PellFile,
        fn: PsiElement,
        fnName: String,
        params: List<PellParam>,
        recordName: String,
        argName: String,
    ) {
        WriteCommandAction.runWriteCommandAction(project, "Parameters to Record '$recordName'", null, Runnable {
            val doc = PsiDocumentManager.getInstance(project).getDocument(file) ?: return@Runnable
            val pm = PsiDocumentManager.getInstance(project)
            pm.commitDocument(doc)

            // Synthesize the record fields from the original param decls.
            val paramSpecs = params.map { p ->
                val name = p.name ?: "_"
                val type = paramTypeText(p)
                ParamSpec(name, type)
            }

            // a. Insert record above the fn.
            val recordText = "pub record $recordName {\n" +
                paramSpecs.joinToString(",\n") { "    ${it.name}: ${it.type}" } +
                "\n}\n\n"
            doc.insertString(fn.textRange.startOffset, recordText)

            // Offsets shifted by recordText.length. Re-find the fn.
            pm.commitDocument(doc)

            val newFn = file.findElementAt(fn.textRange.startOffset + recordText.length)
                ?.let { PsiTreeUtil.getParentOfType(it, PellFnDef::class.java, PellMethodDef::class.java) } ?: return@Runnable

            // b. Rewrite the param list.
            val firstParam = PsiTreeUtil.findChildrenOfType(newFn, PellParam::class.java).first()
            val lastParam = PsiTreeUtil.findChildrenOfType(newFn, PellParam::class.java).last()
            doc.replaceString(
                firstParam.textRange.startOffset,
                lastParam.textRange.endOffset,
                "$argName: $recordName",
            )
            pm.commitDocument(doc)

            // c. Rewrite param refs inside the body.
            val refreshedFn = file.findElementAt(fn.textRange.startOffset + recordText.length)
                ?.let { PsiTreeUtil.getParentOfType(it, PellFnDef::class.java, PellMethodDef::class.java) } ?: return@Runnable

            // Walk the body for IDENTs matching original param names and rewrite
            // back-to-front so offsets don't drift.
            val body = PsiTreeUtil.findChildOfType(refreshedFn, dev.pell.intellij.psi.PellBlock::class.java) ?: return@Runnable
            val paramNames = paramSpecs.map { it.name }.toSet()
            val identsInBody = PsiTreeUtil.findChildrenOfType(body, PsiElement::class.java)
                .filter { it.node?.elementType == dev.pell.intellij.psi.PellElementTypes.IDENT && it.text in paramNames }
                .sortedByDescending { it.textRange.startOffset }
            for (ident in identsInBody) {
                doc.replaceString(
                    ident.textRange.startOffset,
                    ident.textRange.endOffset,
                    "$argName.${ident.text}",
                )
            }
            pm.commitDocument(doc)

            // d. Rewrite every call site of this fn.
            rewriteCallSites(project, fnName, recordName, paramSpecs)
        })
    }

    private fun rewriteCallSites(
        project: Project,
        fnName: String,
        recordName: String,
        paramSpecs: List<ParamSpec>,
    ) {
        // For each call expression in the project whose callee text equals
        // `fnName`, rewrite its args. PellSymbolScanner's name filter gives
        // the rough set; Phase 8 sharpens this with type-based filtering.
        // For v0, rewrite by textual match.
        val pm = PsiDocumentManager.getInstance(project)
        val callOps = PellSymbolScanner.findPubFns(project, fnName)
            .flatMap { fn ->
                // Find all PellCallOp's whose textual prefix matches `fnName(`.
                PsiTreeUtil.findChildrenOfType(fn.containingFile, PellCallOp::class.java)
                    .filter { it.prevSibling?.text == fnName }
            }
        for (call in callOps) {
            val doc = pm.getDocument(call.containingFile) ?: continue
            pm.commitDocument(doc)
            val origArgsText = call.text.removeSurrounding("(", ")")
            val origArgs = origArgsText.split(",").map { it.trim() }
            if (origArgs.size != paramSpecs.size) continue
            val newArgs = paramSpecs.zip(origArgs).joinToString(", ") { (p, a) -> "${p.name}: $a" }
            val replacement = "($recordName { $newArgs })"
            doc.replaceString(call.textRange.startOffset, call.textRange.endOffset, replacement)
            pm.commitDocument(doc)
        }
    }

    private fun paramTypeText(p: PellParam): String {
        // PellParam ::= paramMode? paramName COLON typeRef
        // Grab the typeRef child's text.
        val typeRef = PsiTreeUtil.findChildOfType(p, dev.pell.intellij.psi.PellTypeRef::class.java)
        return typeRef?.text ?: "any"
    }

    private fun rejection(project: Project, msg: String) {
        Messages.showInfoMessage(project, msg, "Parameters to Record")
    }

    private data class ParamSpec(val name: String, val type: String)
}
