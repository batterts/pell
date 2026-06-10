package dev.pell.intellij.refactor.move

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.psi.PsiDocumentManager
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.psi.PellModuleDecl
import dev.pell.intellij.psi.PellNamedElement
import dev.pell.intellij.symbols.index.PellProjectIndex

/**
 * Move a top-level pub declaration (fn / record / type / error /
 * aggregate / enum / seq) to another pell module.
 *
 * v0 algorithm:
 *
 *   1. Caret-resolve to the enclosing PellNamedElement; reject if it
 *      isn't a top-level item.
 *   2. List the project's modules (via PellSymbolScanner). Prompt the
 *      user with the list; they pick one.
 *   3. Inside a WriteCommandAction:
 *      a. Cut the declaration's text from the source file.
 *      b. Append it (with a leading blank line) to the destination
 *         module's file.
 *   4. Cross-file reference updates are deliberately deferred — pell
 *      qualifies by module path, so once the symbol moves, callers
 *      see a stale qualifier. The inspector flags the stale refs;
 *      Rename-after-move (Phase 5 covers it) is the recovery path.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellMoveSymbolAction : AnAction() {

    override fun getActionUpdateThread(): ActionUpdateThread =
        ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabledAndVisible = e.getData(CommonDataKeys.PSI_FILE) is PellFile
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val file = e.getData(CommonDataKeys.PSI_FILE) as? PellFile ?: return

        val elemAtCaret = file.findElementAt(editor.caretModel.offset) ?: return
        val target = PsiTreeUtil.getParentOfType(elemAtCaret, PellNamedElement::class.java) ?: return
        if (target.parent !is PellFile) {
            Messages.showInfoMessage(project, "Only top-level declarations can be moved.", "Move Symbol")
            return
        }

        val index = PellProjectIndex.getInstance(project)
        val modules = index.allOfKind { true }
            .mapNotNull { it.containingFile as? PellFile }
            .distinct()
            .mapNotNull { f -> PsiTreeUtil.findChildOfType(f, PellModuleDecl::class.java)?.let { it.name!! to f } }
        if (modules.isEmpty()) {
            Messages.showInfoMessage(project, "No destination modules found in this project.", "Move Symbol")
            return
        }

        val moduleNames = modules.map { it.first }.toTypedArray()
        val choice = Messages.showEditableChooseDialog(
            "Move `${target.name}` to module:",
            "Move Symbol",
            null,
            moduleNames,
            moduleNames.first(),
            null,
        ) ?: return

        val (_, destFile) = modules.firstOrNull { it.first == choice } ?: return
        doMove(project, file, target, destFile, choice)
    }

    // internal so headless tests drive the move without the
    // destination-module chooser dialog.
    internal fun doMove(
        project: Project,
        file: PellFile,
        target: PellNamedElement,
        destFile: PellFile,
        choice: String,
    ) {
        val symName = target.name ?: return
        val oldShort = moduleShortName(file)
        val newShort = moduleShortName(destFile)
        val newImportPath = moduleImportPath(destFile)

        WriteCommandAction.runWriteCommandAction(project, "Move `${target.name}` to module $choice", null, Runnable {
            val pm = PsiDocumentManager.getInstance(project)
            val sourceDoc = pm.getDocument(file) ?: return@Runnable
            val destDoc = pm.getDocument(destFile) ?: return@Runnable
            pm.commitDocument(sourceDoc)
            pm.commitDocument(destDoc)

            // Use the full declaration range so the leading `pub` /
            // annotations move with the body (target.textRange starts at
            // the `fn`/`record` keyword, after `pub`).
            val range = dev.pell.intellij.refactor.declarationRange(target)
            val movedText = sourceDoc.getText(
                com.intellij.openapi.util.TextRange(range.startOffset, range.endOffset)
            )
            // Cut from source.
            sourceDoc.deleteString(range.startOffset, range.endOffset)
            // Append to dest with a leading blank line.
            destDoc.insertString(destDoc.textLength, "\n\n$movedText")

            pm.commitDocument(sourceDoc)
            pm.commitDocument(destDoc)

            // ---- Reference rewriting across the project --------------
            // 1. Qualified refs `oldShort::sym` -> `newShort::sym` in
            //    every project pell file (incl. the source file).
            // 2. Bare refs `sym(` / `sym {` in the SOURCE file (which
            //    used to resolve locally) -> `newShort::sym...`.
            // 3. Any file that now references `newShort::` gets an
            //    `import <newImportPath>;` if it doesn't already have
            //    one. (Stale imports of the old module are left alone —
            //    harmless if other symbols are still used.)
            val touched = mutableSetOf<com.intellij.openapi.editor.Document>()
            val allFiles = com.intellij.psi.search.FileTypeIndex.getFiles(
                dev.pell.intellij.PellFileType,
                com.intellij.psi.search.GlobalSearchScope.allScope(project),
            ).mapNotNull {
                com.intellij.psi.PsiManager.getInstance(project).findFile(it) as? PellFile
            }
            val qualRe = Regex("\\b${Regex.escape(oldShort)}::${Regex.escape(symName)}\\b")
            for (pf in allFiles) {
                if (pf == destFile) continue
                val doc = pm.getDocument(pf) ?: continue
                var text = doc.text
                var changed = false
                if (qualRe.containsMatchIn(text)) {
                    text = qualRe.replace(text, "$newShort::$symName")
                    changed = true
                }
                if (pf == file) {
                    // Bare uses in the old home now need qualification.
                    val bareRe = Regex("(?<![\\w:.])${Regex.escape(symName)}(?=\\s*[({])")
                    if (bareRe.containsMatchIn(text)) {
                        text = bareRe.replace(text, "$newShort::$symName")
                        changed = true
                    }
                }
                if (!changed) continue
                if (text.contains("$newShort::") && newImportPath.isNotEmpty()
                    && !Regex("(?m)^\\s*import\\s+${Regex.escape(newImportPath)}\\s*;").containsMatchIn(text)
                ) {
                    // Insert the import right after the module decl line.
                    val lines = text.lines().toMutableList()
                    val modIdx = lines.indexOfFirst { it.trimStart().startsWith("module ") }
                    if (modIdx >= 0) {
                        lines.add(modIdx + 1, "import $newImportPath;")
                        text = lines.joinToString("\n")
                    }
                }
                doc.setText(text)
                pm.commitDocument(doc)
                touched.add(doc)
            }

            FileDocumentManager.getInstance().saveDocument(sourceDoc)
            FileDocumentManager.getInstance().saveDocument(destDoc)
            touched.forEach { FileDocumentManager.getInstance().saveDocument(it) }
        })
    }

    /** Last segment of a file's module path — the `short::` call
     *  qualifier. `module demo::app.parsing;` -> "parsing". */
    private fun moduleShortName(f: PellFile): String {
        val decl = PsiTreeUtil.findChildOfType(f, PellModuleDecl::class.java)
        val raw = decl?.name ?: return ""
        return raw.substringAfterLast("::").substringAfterLast(".")
    }

    /** The module path as written for an `import` line — the full decl
     *  text minus the keyword/semicolon. `module demo::app.parsing;`
     *  -> "demo::app.parsing". */
    private fun moduleImportPath(f: PellFile): String {
        val decl = PsiTreeUtil.findChildOfType(f, PellModuleDecl::class.java) ?: return ""
        return decl.text
            .removePrefix("module").trim()
            .removeSuffix(";").trim()
    }
}
