package dev.pell.intellij.symbols

/**
 * What kind of declaration a [PellSymbolInfo] describes. Mirrors the
 * top-level item rules in Pell.bnf (`itemBody`) plus a few "nested
 * named decl" cases the refactorings care about (method, case,
 * enumVariant).
 *
 * Owned by L1 of LSP_PSI_UNIFICATION.md.
 */
enum class SymbolKind {
    FN,
    METHOD,
    RECORD,
    ERROR,
    TYPE,
    SEALED_TYPE,
    ENUM,
    ENUM_VARIANT,
    AGGREGATE,
    SEQUENCE,
    MODULE,
    CASE,
    FIELD,
    PARAM,
    LET,
    VAR,
    LOOP_VAR,
}
