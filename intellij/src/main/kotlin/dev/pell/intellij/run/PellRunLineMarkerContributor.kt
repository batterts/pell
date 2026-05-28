package dev.pell.intellij.run

import com.intellij.execution.lineMarker.RunLineMarkerContributor
import com.intellij.icons.AllIcons
import com.intellij.psi.PsiElement
import dev.pell.intellij.PellFileType

/**
 * Puts the green-arrow gutter icon at the top of every .pell file.
 * Clicking it opens a menu of the available pell subcommands so you
 * can pick the one you want without going through Edit Configurations.
 *
 *   ▶ Run pell exec              (the default — anonymous block + execute)
 *   ▶ Run pell exec (dry-run)    (show the block, don't connect)
 *   ▶ Run pell build             (compile to PL/SQL, stdout)
 *   ▶ Run pell build → file      (compile to <name>.sql next to source)
 *   ▶ Run pell parse             (print the AST)
 *   ▶ Run pell tokens            (print the token stream)
 *
 * Each entry creates / reuses a named run config (`pell <action>: <file>`),
 * runs it, and leaves it in the toolbar's run-config dropdown so the
 * next click on the toolbar Run button re-runs the same one.
 */
class PellRunLineMarkerContributor : RunLineMarkerContributor() {

    private val pellActions = arrayOf(
        PellRunAction("exec",            "Run pell exec",          "Compile to anon block and execute against PELL_DB_URL"),
        PellRunAction("exec --dry-run",  "Run pell exec (dry-run)","Compile to anon block; print it; don't connect"),
        PellRunAction("build",           "Run pell build",         "Compile to PL/SQL on stdout"),
        PellRunAction("build -o",        "Run pell build → file",  "Compile to <name>.sql next to the source file"),
        PellRunAction("parse",           "Run pell parse",         "Print the parsed AST"),
        PellRunAction("tokens",          "Run pell tokens",        "Print the lexer's token stream"),
        // Interactive — opens a terminal tab + auto `\load`s this file
        PellOpenReplAction(),
    )

    override fun getInfo(element: PsiElement): Info? {
        val file = element.containingFile ?: return null
        if (file.fileType !is PellFileType) return null
        // One marker per file — on the first PSI leaf (offset 0).
        if (element.textOffset != 0) return null
        return Info(
            AllIcons.RunConfigurations.TestState.Run,
            pellActions,
        ) { _ -> "Run pell on this file" }
    }
}
