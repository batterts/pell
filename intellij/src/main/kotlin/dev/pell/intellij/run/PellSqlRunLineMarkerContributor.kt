package dev.pell.intellij.run

import com.intellij.execution.lineMarker.RunLineMarkerContributor
import com.intellij.icons.AllIcons
import com.intellij.psi.PsiElement

/**
 * Green-arrow gutter icon for plain `.sql` files. Lets you run the
 * file against PELL_DB_URL without paying for IntelliJ Ultimate's
 * Database plugin.
 *
 * Registered for `TEXT` *and* `SQL` languages in plugin.xml so it
 * fires in both Community (where .sql defaults to plain text) and
 * Ultimate (where the SQL plugin owns the file type). The contributor
 * filters by file extension to avoid lighting up the icon on every
 * text file in the project.
 *
 * Backed by `pell sql <file>` — splits on the SQL*Plus `/`
 * terminator, runs each statement, prints DBMS_OUTPUT as it goes.
 */
class PellSqlRunLineMarkerContributor : RunLineMarkerContributor() {

    private val sqlActions = arrayOf(
        PellRunAction(
            "sql",
            "Run SQL on PELL_DB_URL",
            "Execute this .sql file against the database (split on /, statement by statement)",
        ),
        PellRunAction(
            "sql --stop-on-error",
            "Run SQL (stop on error)",
            "Execute this .sql file against the database, stopping at the first failure",
        ),
    )

    override fun getInfo(element: PsiElement): Info? {
        val file = element.containingFile ?: return null
        val vfile = file.virtualFile ?: return null
        if (vfile.extension?.lowercase() != "sql") return null
        if (element.textOffset != 0) return null
        return Info(
            AllIcons.RunConfigurations.TestState.Run,
            sqlActions,
        ) { _ -> "Run this SQL on PELL_DB_URL" }
    }
}
