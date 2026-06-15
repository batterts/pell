package dev.pell.intellij.symbols

/**
 * Sealed hierarchy describing every type the IntelliJ side can reason
 * about. The PSI side fills this in by walking declarations (lossy);
 * the LSP side fills it in via the future `pell/typeOf` custom method
 * (authoritative).
 *
 * Where a particular consumer can't determine a type (a sql!{} return
 * before the LSP catalog ships, a member access through an Unknown
 * receiver, etc.), the consumer uses [Unknown] and downstream code
 * decides whether to skip the action or push the user toward a manual
 * type annotation.
 *
 * Owned by L1 of LSP_PSI_UNIFICATION.md — `intellij/LSP_PSI_UNIFICATION.md`.
 */
sealed class PellType {
    abstract fun displayName(): String

    // --- primitives (mirrors PRIMITIVES in compiler/pell/parser.py) ---
    object Number    : PellType() { override fun displayName() = "number" }
    object Int       : PellType() { override fun displayName() = "int" }
    object Text      : PellType() { override fun displayName() = "text" }
    object BigText   : PellType() { override fun displayName() = "bigtext" }
    object Bool      : PellType() { override fun displayName() = "bool" }
    object Date      : PellType() { override fun displayName() = "date" }
    object Timestamp : PellType() { override fun displayName() = "timestamp" }
    object Interval  : PellType() { override fun displayName() = "interval" }
    object Bytes     : PellType() { override fun displayName() = "bytes" }
    object Json      : PellType() { override fun displayName() = "json" }
    object Unit      : PellType() { override fun displayName() = "Unit" }

    // --- user-defined / composite ---
    data class Named(val qualifiedName: String) : PellType() {
        override fun displayName() = qualifiedName
    }

    data class Optional(val inner: PellType) : PellType() {
        override fun displayName() = "${inner.displayName()}?"
    }

    data class List(val element: PellType) : PellType() {
        override fun displayName() = "list<${element.displayName()}>"
    }

    data class Result(val ok: PellType, val err: PellType) : PellType() {
        override fun displayName() = "Result<${ok.displayName()}, ${err.displayName()}>"
    }

    /** Generic invocation like `Some<text>` or any user generic. */
    data class Generic(val base: String, val args: kotlin.collections.List<PellType>) : PellType() {
        override fun displayName() = "$base<${args.joinToString(", ") { it.displayName() }}>"
    }

    /** Sum-of-types — used for error unions `T | U` in fn return position. */
    data class Union(val variants: kotlin.collections.List<PellType>) : PellType() {
        override fun displayName() = variants.joinToString(" | ") { it.displayName() }
    }

    /** Sentinel for "we don't know" — downstream code treats this as
     *  permission to give up safely. Distinct from `Unit` (which is a
     *  real type: the procedural fn return). */
    object Unknown : PellType() { override fun displayName() = "?" }

    companion object {
        /** Parse the surface-text of a `-> T` clause or field type
         *  declaration into a structured [PellType]. Conservative: any
         *  pattern we can't pinpoint becomes [Unknown]. */
        fun parse(text: String): PellType {
            val trimmed = text.trim().trim(';', ',')
            if (trimmed.isEmpty()) return Unknown

            // Trailing `?` means optional.
            if (trimmed.endsWith("?")) {
                return Optional(parse(trimmed.dropLast(1)))
            }

            // Generic shapes: list<T>, Result<T, E>, Some<T>, etc.
            val ltIdx = trimmed.indexOf('<')
            val gtIdx = trimmed.lastIndexOf('>')
            if (ltIdx in 1 until gtIdx) {
                val base = trimmed.substring(0, ltIdx).trim()
                val inner = trimmed.substring(ltIdx + 1, gtIdx)
                val args = splitTopLevelCommas(inner).map { parse(it.trim()) }
                return when (base) {
                    "list"   -> if (args.size == 1) List(args[0]) else Unknown
                    "Result" -> if (args.size == 2) Result(args[0], args[1]) else Unknown
                    else     -> Generic(base, args)
                }
            }

            // Union `T | U | V` — only at top level.
            if (" | " in trimmed) {
                return Union(trimmed.split(" | ").map { parse(it.trim()) })
            }

            return when (trimmed) {
                "number"    -> Number
                "int"       -> Int
                "text"      -> Text
                "bigtext"   -> BigText
                "bool"      -> Bool
                "date"      -> Date
                "timestamp" -> Timestamp
                "interval"  -> Interval
                "bytes"     -> Bytes
                "json"      -> Json
                "Unit"      -> Unit
                ""          -> Unknown
                else        -> Named(trimmed)
            }
        }

        /** Split a string on top-level commas (not inside nested `< >`). */
        private fun splitTopLevelCommas(s: String): kotlin.collections.List<String> {
            val out = mutableListOf<String>()
            var depth = 0
            var start = 0
            for ((i, c) in s.withIndex()) {
                when (c) {
                    '<' -> depth++
                    '>' -> depth--
                    ',' -> if (depth == 0) {
                        out += s.substring(start, i)
                        start = i + 1
                    }
                }
            }
            out += s.substring(start)
            return out
        }
    }
}

/** Whether a call to a fn of this return type yields a value that must
 *  be used — the predicate the discarded-return inspection uses to flag a
 *  bare-call statement. False for [PellType.Unit], [PellType.Unknown],
 *  and `Result<Unit, E>`: the latter lowers to a PL/SQL PROCEDURE (the
 *  `Ok` carries no value), so `f()?;` as a statement is valid — matching
 *  the emitter's `_is_unit_return`. */
fun PellType.producesValue(): Boolean = when {
    this is PellType.Unit -> false
    this is PellType.Unknown -> false
    this is PellType.Result && this.ok is PellType.Unit -> false
    else -> true
}
