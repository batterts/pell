package dev.pell.intellij.debugger

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PellSrcMapTest {

    private val json = """
    {
      "pell_file": "examples/01_hello.pell",
      "units": [
        {"kind": "PACKAGE", "schema": null, "name": "HELLO",
         "jdwp_class": "${'$'}Oracle.Package..HELLO", "entries": []},
        {"kind": "PACKAGE BODY", "schema": null, "name": "HELLO",
         "jdwp_class": "${'$'}Oracle.PackageBody..HELLO",
         "file_line": 21,
         "entries": [
            {"pell_line": 9,  "unit_line": 3},
            {"pell_line": 10, "unit_line": 5},
            {"pell_line": 11, "unit_line": 6}
         ]}
      ]
    }
    """.trimIndent()

    @Test
    fun parseAndMatch() {
        val map = PellSrcMap.parse(json)
        assertEquals(2, map.units.size)
        val body = map.unitFor("\$Oracle.PackageBody.PELL_TEST.HELLO")!!
        assertEquals("PACKAGE BODY", body.kind)
        // schema-less map matches any schema; wrong name/kind don't.
        assertTrue(body.matches("\$Oracle.PackageBody.OTHER_SCHEMA.HELLO"))
        assertFalse(body.matches("\$Oracle.PackageBody.PELL_TEST.LOGGER"))
        assertFalse(body.matches("\$Oracle.Package.PELL_TEST.HELLO"))
    }

    @Test
    fun breakpointResolution() {
        val map = PellSrcMap.parse(json)
        // exact hit
        assertEquals(5, map.resolveBreakpoint(10)!!.second)
        // blank/comment line snaps forward to the next statement
        assertEquals(3, map.resolveBreakpoint(8)!!.second)
        // past the last statement: nothing to arm
        assertNull(map.resolveBreakpoint(99))
    }

    @Test
    fun frameMapping() {
        val body = PellSrcMap.parse(json).unitFor("\$Oracle.PackageBody.X.HELLO")!!
        assertEquals(10, body.pellLineForUnit(5))
        // unit line between markers maps to nearest marker above
        assertEquals(9, body.pellLineForUnit(4))
        // before any marker: unmapped
        assertNull(body.pellLineForUnit(1))
    }

    @Test
    fun fileLineAnchorsUnitToFileConversion() {
        val body = PellSrcMap.parse(json).unitFor("${'$'}Oracle.PackageBody.X.HELLO")!!
        // unit line 1 IS the CREATE line: file line = fileLine + unit - 1.
        assertEquals(21, body.fileLine)
        assertEquals(25, body.fileLine + 5 - 1)
        // absent file_line defaults to 1 (older maps)
        val spec = PellSrcMap.parse(json).units.first { it.kind == "PACKAGE" }
        assertEquals(1, spec.fileLine)
    }

    @Test
    fun anonBlockMatchesAnyBlockClass() {
        val anon = PellSrcMap.parse(
            """
            {"pell_file": "stub.pell", "units": [
              {"kind": "BLOCK", "schema": null, "name": null,
               "jdwp_class": "${'$'}Oracle.Block",
               "entries": [{"pell_line": 2, "unit_line": 7}]}
            ]}
            """.trimIndent(),
        )
        val block = anon.unitFor("\$Oracle.Block.PELL_TEST.BUgTvvNzmd0qCVjbOZGm1Xw")!!
        assertEquals(2, block.pellLineForUnit(7))
    }
}
