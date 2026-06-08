package dev.pell.intellij.findusages

import com.intellij.testFramework.fixtures.BasePlatformTestCase
import dev.pell.intellij.symbols.index.PellProjectIndex

/**
 * Headless Find Usages + reference-resolution tests. These give a
 * feedback loop the IDE UI can't: the fixture runs the full
 * ReferencesSearch + getReference() + WordsScanner pipeline against an
 * in-memory project and returns the usage count, so we can assert it
 * directly.
 *
 * Run: ./gradlew test --tests "*PellFindUsagesTest*"
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellFindUsagesTest : BasePlatformTestCase() {

    override fun setUp() {
        super.setUp()
        // The light project + project-level PellProjectIndex are shared
        // across test methods; reset so each test sees only its own
        // files (the once-only init would otherwise serve a prior
        // test's symbols).
        PellProjectIndex.getInstance(project).resetForTest()
    }

    /** A call-site reference resolves back to the declaration (the
     *  goto-def direction). */
    fun testReferenceResolves() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn helper() -> number {
                return 1;
            }

            pub fn caller() -> number {
                return hel<caret>per();
            }
            """.trimIndent(),
        )
        val ref = myFixture.file.findReferenceAt(myFixture.caretOffset)
        assertNotNull("bare call should carry a PsiReference", ref)
        assertNotNull(
            "call site should resolve to a declaration",
            ref!!.resolve(),
        )
    }

    /** Same-file: a fn declared and called within one module. */
    fun testSameFileUsage() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn hel<caret>per() -> number {
                return 1;
            }

            pub fn caller() -> number {
                return helper() + helper();
            }
            """.trimIndent(),
        )
        val usages = myFixture.findUsages(myFixture.elementAtCaret)
        assertEquals(2, usages.size)
    }

    /** Cross-file: declaration in one module, qualified call in another. */
    fun testCrossFileQualifiedUsage() {
        myFixture.addFileToProject(
            "consumer.pell",
            """
            module consumer;
            import provider;

            pub fn use_it() -> number {
                return provider::thing();
            }
            """.trimIndent(),
        )
        myFixture.configureByText(
            "provider.pell",
            """
            module provider;

            pub fn th<caret>ing() -> number {
                return 42;
            }
            """.trimIndent(),
        )
        val usages = myFixture.findUsages(myFixture.elementAtCaret)
        assertEquals(1, usages.size)
    }

    /** Type references: a record used as a field/param type counts as
     *  a usage. */
    fun testTypeReferenceUsage() {
        myFixture.configureByText(
            "t.pell",
            """
            module t;

            pub record Wid<caret>get {
                id: number,
            }

            pub fn make() -> Widget {
                return Widget { id: 1 };
            }
            """.trimIndent(),
        )
        val usages = myFixture.findUsages(myFixture.elementAtCaret)
        // The return-type `Widget` and the struct-literal `Widget { }`.
        assertTrue("expected >=1 type usage, got ${usages.size}", usages.size >= 1)
    }
}
