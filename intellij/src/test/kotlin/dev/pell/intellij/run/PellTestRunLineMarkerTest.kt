package dev.pell.intellij.run

import com.intellij.testFramework.fixtures.BasePlatformTestCase

/**
 * Headless checks for the utPLSQL "run test" gutter markers: the
 * contributor must light up the `module` keyword of an `@suite` module
 * and each `@test pub fn`, and stay dark elsewhere. Asserts via the
 * fixture's gutter-mark API (the same line markers the IDE renders).
 *
 * Run: ./gradlew test --tests "*PellTestRunLineMarkerTest*"
 */
class PellTestRunLineMarkerTest : BasePlatformTestCase() {

    private fun gutterTooltips(text: String): List<String> {
        myFixture.configureByText("ut_x.pell", text.trimIndent())
        myFixture.doHighlighting()
        return myFixture.findAllGutters().mapNotNull { it.tooltipText }
    }

    fun testSuiteAndTestMarkersAppear() {
        val tips = gutterTooltips(
            """
            @suite("vetting", rollback = "manual")
            module ut_examples;

            import ut;

            @test("greets")
            pub fn t_greet() {
                ut::expect(1).to_equal(1);
            }

            @test("adds")
            pub fn t_add() {
                ut::expect(2).to_equal(2);
            }
            """,
        )
        // Whole-suite marker on the module.
        assertTrue("expected a suite marker, got: $tips",
            tips.any { it.contains("suite ut_examples") })
        // One single-test marker per @test fn.
        assertTrue("expected t_greet test marker, got: $tips",
            tips.any { it.contains("ut_examples.t_greet") })
        assertTrue("expected t_add test marker, got: $tips",
            tips.any { it.contains("ut_examples.t_add") })
    }

    fun testNoSuiteMeansNoMarkers() {
        val tips = gutterTooltips(
            """
            module plain;

            pub fn helper() -> number {
                return 1;
            }
            """,
        )
        assertFalse("a non-suite module must not get utPLSQL markers: $tips",
            tips.any { it.contains("utPLSQL suite") || it.contains("utPLSQL test") })
    }

    fun testSchemaQualifiedSuiteName() {
        val tips = gutterTooltips(
            """
            @suite("q")
            module hr::a.b.checks;

            @test("x")
            pub fn t_x() { ut::expect(1).to_equal(1); }
            """,
        )
        // `module hr::a.b.checks` → suite path `hr.checks`.
        assertTrue("expected schema-qualified suite hr.checks, got: $tips",
            tips.any { it.contains("hr.checks") })
    }
}
