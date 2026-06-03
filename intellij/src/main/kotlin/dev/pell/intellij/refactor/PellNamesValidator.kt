package dev.pell.intellij.refactor

import com.intellij.lang.refactoring.NamesValidator
import com.intellij.openapi.project.Project

/**
 * Validates names produced by Rename (Phase 5) and inspections.
 *
 * Rejects:
 *   - Empty / null
 *   - Non-IDENT-syntax (not [A-Za-z_][A-Za-z0-9_]*)
 *   - pell keywords (collides with parser)
 *
 * Does NOT reject Oracle reserved words — pell deliberately allows
 * them as user identifiers (the emitter quotes them at lowering time
 * when needed).
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellNamesValidator : NamesValidator {

    override fun isKeyword(name: String, project: Project?): Boolean = KEYWORDS.contains(name)

    override fun isIdentifier(name: String, project: Project?): Boolean =
        IDENT_RE.matches(name) && !KEYWORDS.contains(name)

    companion object {
        private val IDENT_RE = Regex("[A-Za-z_][A-Za-z0-9_]*")

        // Mirrors KEYWORDS in compiler/pell/lexer.py exactly.
        private val KEYWORDS = setOf(
            "module", "import", "pub", "fn", "let", "var", "return", "yield",
            "if", "else", "for", "forall", "in", "match", "transaction",
            "record", "error", "true", "false", "Some", "None", "Ok", "Err",
            "unsafe", "finally", "and", "or", "not",
            "type", "sealed", "aggregate", "case", "self",
            "seq",
            "out", "inout",
            "enum",
        )
    }
}
