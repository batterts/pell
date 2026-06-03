package dev.pell.intellij.symbols

/**
 * Where a symbol's declaration lives — drives both navigation
 * behaviour (can we jump to source?) and inspection conservatism
 * (do we trust the type information enough to flag misuse?).
 *
 * Owned by L1 of LSP_PSI_UNIFICATION.md.
 */
enum class SymbolSource {
    /** Declared in the same pell file as the caller. */
    LOCAL_FILE,

    /** Declared elsewhere in the user's project (another .pell file). */
    PROJECT,

    /** Pell stdlib (`catalog::*`, `logger::*`, `dbms_output::*`,
     *  runtime helpers like `p`). Populated from the future
     *  `pell/symbolCatalog` LSP method (L3); empty until then. */
    STDLIB,

    /** Oracle-side identifier (tables, sequences, packages on the
     *  connected database). Populated from LSP's data-dictionary
     *  introspection when that lands. */
    ORACLE,

    /** Unknown origin — used for unresolved references. */
    UNKNOWN,
}
