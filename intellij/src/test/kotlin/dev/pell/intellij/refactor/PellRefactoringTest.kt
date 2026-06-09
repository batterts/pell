package dev.pell.intellij.refactor

import com.intellij.testFramework.fixtures.BasePlatformTestCase
import dev.pell.intellij.PellFile
import dev.pell.intellij.refactor.extract.ExtractMethodAnalyzer
import dev.pell.intellij.refactor.extract.PellExtractMethodHandler
import dev.pell.intellij.symbols.index.PellProjectIndex

/**
 * Headless refactoring tests. Each drives a refactoring's core
 * transformation (bypassing the modal dialogs that the UI actions
 * show) and asserts the resulting source.
 *
 * Run: ./gradlew test --tests "*PellRefactoringTest*"
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellRefactoringTest : BasePlatformTestCase() {

    override fun setUp() {
        super.setUp()
        PellProjectIndex.getInstance(project).resetForTest()
    }

    // ---- Rename --------------------------------------------------------

    /** Rename a fn renames its declaration AND every same-file call. */
    fun testRenameSameFile() {
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
        myFixture.renameElementAtCaret("renamed")
        val text = myFixture.file.text
        assertTrue("decl renamed", text.contains("pub fn renamed()"))
        assertFalse("no stale name remains", text.contains("helper"))
        // decl `renamed()` + two call sites `renamed()` = 3 occurrences.
        assertEquals("decl + 2 call sites", 3,
            Regex("\\brenamed\\(\\)").findAll(text).count())
    }

    /** Rename across files: declaration in one module, qualified call
     *  in another, both update. */
    fun testRenameCrossFile() {
        val consumer = myFixture.addFileToProject(
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
        myFixture.renameElementAtCaret("widget")
        assertTrue("decl renamed",
            myFixture.file.text.contains("pub fn widget()"))
        assertTrue("cross-file call renamed",
            consumer.text.contains("provider::widget()"))
    }

    // ---- Extract Method ------------------------------------------------

    /** Extract two statements into a new fn; selection becomes a call. */
    fun testExtractMethod() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn run() {
                let x = 1;
                p(x);
                p(x);
            }
            """.trimIndent(),
        )
        // Select the two p(x); statements.
        val text = myFixture.file.text
        val start = text.indexOf("    p(x);")
        val end = text.lastIndexOf("p(x);") + "p(x);".length
        myFixture.editor.selectionModel.setSelection(start, end)

        val file = myFixture.file as PellFile
        val analysis = ExtractMethodAnalyzer.analyze(file, myFixture.editor)
        assertTrue(
            "selection should be extractable: ${analysis.rejectionReason}",
            analysis.isExtractable,
        )
        PellExtractMethodHandler().applyExtract(
            project, file, myFixture.editor, analysis, "printed", true,
        )
        val result = myFixture.file.text
        assertTrue("new fn synthesized", result.contains("fn printed("))
        assertTrue("call site inserted", result.contains("printed("))
        // `x` is read inside the selection but bound outside → a param.
        assertTrue("captured x as param", result.contains("printed(x)"))
    }
}
