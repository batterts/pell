package dev.pell.intellij.run

import com.intellij.execution.lineMarker.RunLineMarkerContributor
import com.intellij.icons.AllIcons
import com.intellij.psi.PsiElement
import dev.pell.intellij.PellFileType

/**
 * Puts a compile/hammer gutter icon at the top of every .pell file.
 * Clicking it opens a menu of the available pell subcommands.
 *
 * The icon is a hammer (not a play arrow) because the primary thing
 * a .pell file does is *compile* to PL/SQL — `pell build` is the
 * star action, not `pell exec`. The play-arrow icon stays reserved
 * for the side-by-side preview's "run this compiled SQL on
 * PELL_DB_URL" button, which is the actual execution surface.
 *
 *   ⚒ pell exec                   (compile + execute as anon block)
 *   ⚒ pell exec (dry-run)         (show the block, don't connect)
 *   ⚒ pell build                  (compile to PL/SQL on stdout)
 *   ⚒ pell build → file           (compile to <name>.sql)
 *   ⚒ pell parse                  (print the AST)
 *   ⚒ pell tokens                 (print the lexer stream)
 *
 * Each entry creates / reuses a named run config (`pell <action>: <file>`),
 * runs it, and leaves it in the toolbar's run-config dropdown so the
 * next click on the toolbar Run button re-runs the same one.
 */
class PellRunLineMarkerContributor : RunLineMarkerContributor() {

    // No `build` / `build → file` here — the split-editor preview
    // already shows the lowered PL/SQL live; the user doesn't need a
    // menu item to re-build the thing already on their screen.
    private val pellActions = arrayOf(
        PellRunAction("exec",            "Run pell exec",          "Compile to anon block and execute against PELL_DB_URL"),
        PellRunAction("exec --dry-run",  "Run pell exec (dry-run)","Compile to anon block; print it; don't connect"),
        // Interactive — opens a terminal tab + auto `\load`s this file
        PellOpenReplAction(),
        PellRunAction("parse",           "Show parsed AST",        "Print the parsed AST (debug)"),
        PellRunAction("tokens",          "Show token stream",      "Print the lexer's token stream (debug)"),
    )

    override fun getInfo(element: PsiElement): Info? {
        val file = element.containingFile ?: return null
        if (file.fileType !is PellFileType) return null
        // One marker per file — on the first PSI leaf (offset 0).
        if (element.textOffset != 0) return null
        return Info(
            AllIcons.Actions.Compile,
            pellActions,
        ) { _ -> "Compile this .pell file" }
    }
}
