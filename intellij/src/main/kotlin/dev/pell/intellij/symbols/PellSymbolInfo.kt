package dev.pell.intellij.symbols

import com.intellij.psi.PsiElement

/**
 * What every PSI consumer (inspection, refactoring, completion, inlay
 * hint) knows about a pell symbol after asking [PellSymbolService].
 *
 * Designed to be cheap to construct: PSI-walking impls fill it in
 * directly; the future StubIndex backing populates it from stub bytes.
 *
 * Owned by L1 of LSP_PSI_UNIFICATION.md.
 */
data class PellSymbolInfo(
    val name: String,
    val qualifiedName: String,
    val kind: SymbolKind,
    /** Fn return type, record's own named-type form, etc. Null when the
     *  symbol doesn't carry a meaningful type (modules, fields are
     *  carried separately in [fields]). */
    val type: PellType?,
    /** Parameters for [SymbolKind.FN], [SymbolKind.METHOD],
     *  [SymbolKind.AGGREGATE]; empty for non-callable kinds. */
    val params: List<PellParamInfo>,
    /** Fields for [SymbolKind.RECORD], [SymbolKind.ERROR],
     *  [SymbolKind.TYPE], [SymbolKind.SEALED_TYPE]; empty otherwise. */
    val fields: List<PellFieldInfo>,
    val source: SymbolSource,
    /** PSI navigation target for [SymbolSource.LOCAL_FILE] /
     *  [SymbolSource.PROJECT] symbols. Null for stdlib / Oracle
     *  symbols whose source isn't accessible from the IDE. */
    val location: PsiElement?,
    val isPub: Boolean,
)

data class PellParamInfo(
    val name: String,
    val type: PellType,
    val mode: ParamMode = ParamMode.IN,
)

enum class ParamMode { IN, OUT, INOUT }

data class PellFieldInfo(
    val name: String,
    val type: PellType,
)

data class PellModuleInfo(
    /** The full dotted module path, e.g. `pell_test.hello`. */
    val dottedName: String,
    /** PSI element of the `module foo.bar;` declaration. */
    val location: PsiElement?,
)

/**
 * Effects a sql!{...} block has on the database. Filled in by the
 * future `pell/effectsOf` LSP method (L5); a default-empty instance
 * is returned by the L1 PSI-only backing.
 */
data class SqlEffects(
    val isDml: Boolean = false,
    val hasReturning: Boolean = false,
    val tablesRead: List<String> = emptyList(),
    val tablesWritten: List<String> = emptyList(),
) {
    companion object {
        val NONE = SqlEffects()
    }
}
