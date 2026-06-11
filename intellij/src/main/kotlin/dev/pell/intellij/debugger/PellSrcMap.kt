package dev.pell.intellij.debugger

import com.google.gson.JsonParser

/**
 * The pell↔PL/SQL line map for one compiled .pell file, parsed from
 * `pell srcmap [--anon]` JSON. Mirrors compiler/pell/srcmap.py:
 *
 *   unit line = JDWP line number = ALL_SOURCE line number
 *   (line 1 is the unit keyword line; for anon blocks, block line 1)
 *
 * One .pell file can produce several units (package body + type bodies
 * for sealed types). Breakpoint placement prefers the unit with an
 * exact pell-line hit, then the nearest following marker.
 */
class PellSrcMap(val units: List<Unit>) {

    class Unit(
        val kind: String,            // "PACKAGE BODY", "TYPE BODY", "BLOCK", ...
        val schema: String?,         // null = connected schema
        val name: String?,           // null for anon blocks
        val entries: List<Pair<Int, Int>>,  // (pellLine, unitLine), emission order
    ) {
        /** JDWP class name; for units with no explicit schema the
         *  debugger matches on suffix instead (see matches()). */
        fun jdwpKind(): String = when (kind) {
            "PACKAGE BODY" -> "PackageBody"
            "TYPE BODY" -> "TypeBody"
            "PACKAGE" -> "Package"
            "TYPE" -> "Type"
            "BLOCK" -> "Block"
            else -> kind.replace(" ", "")
        }

        /** Does the given JDWP class name refer to this unit?
         *  $Oracle.PackageBody.PELL_TEST.HELLO, $Oracle.Block.PELL_TEST.<hash> */
        fun matches(jdwpClassName: String): Boolean {
            val parts = jdwpClassName.split(".")
            if (parts.size < 2 || parts[0] != "\$Oracle") return false
            if (parts[1] != jdwpKind()) return false
            if (kind == "BLOCK") return true  // one anon block per session
            if (parts.size < 4) return false
            val schemaOk = schema == null || parts[2].equals(schema, ignoreCase = true)
            return schemaOk && parts[3].equals(name, ignoreCase = true)
        }

        fun unitLineForPell(pellLine: Int): Int? {
            entries.firstOrNull { it.first == pellLine }?.let { return it.second }
            return entries.filter { it.first > pellLine }
                .minByOrNull { it.first }?.second
        }

        fun pellLineForUnit(unitLine: Int): Int? =
            entries.filter { it.second <= unitLine }
                .maxByOrNull { it.second }?.first
    }

    /** Best (unit, unitLine) for a breakpoint at a pell source line. */
    fun resolveBreakpoint(pellLine: Int): Pair<Unit, Int>? {
        // Exact marker hit wins; otherwise smallest forward snap.
        var best: Pair<Unit, Int>? = null
        var bestDistance = Int.MAX_VALUE
        for (u in units) {
            val exact = u.entries.firstOrNull { it.first == pellLine }
            if (exact != null) return u to exact.second
            val next = u.entries.filter { it.first > pellLine }.minByOrNull { it.first }
            if (next != null && next.first - pellLine < bestDistance) {
                bestDistance = next.first - pellLine
                best = u to next.second
            }
        }
        return best
    }

    fun unitFor(jdwpClassName: String): Unit? = units.firstOrNull { it.matches(jdwpClassName) }

    companion object {
        fun parse(json: String): PellSrcMap {
            val root = JsonParser.parseString(json).asJsonObject
            val units = root.getAsJsonArray("units").map { el ->
                val o = el.asJsonObject
                Unit(
                    kind = o.get("kind").asString,
                    schema = o.get("schema")?.takeIf { !it.isJsonNull }?.asString,
                    name = o.get("name")?.takeIf { !it.isJsonNull }?.asString,
                    entries = o.getAsJsonArray("entries").map { e ->
                        val eo = e.asJsonObject
                        eo.get("pell_line").asInt to eo.get("unit_line").asInt
                    },
                )
            }
            return PellSrcMap(units)
        }
    }
}
