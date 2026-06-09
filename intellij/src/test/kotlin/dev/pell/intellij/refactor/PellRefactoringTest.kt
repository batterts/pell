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

    // ---- Parameters to Record ------------------------------------------

    fun testParamsToRecord() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn greet(name: text, age: number) -> text {
                return name;
            }
            """.trimIndent(),
        )
        val file = myFixture.file as PellFile
        val fn = com.intellij.psi.util.PsiTreeUtil.findChildOfType(
            file, dev.pell.intellij.psi.PellFnDef::class.java,
        )!!
        val params = com.intellij.psi.util.PsiTreeUtil.findChildrenOfType(
            fn, dev.pell.intellij.psi.PellParam::class.java,
        ).toList()
        dev.pell.intellij.refactor.paramsToRecord.PellParamsToRecordHandler()
            .applyExtract(project, file, fn, "greet", params, "GreetArgs", "args")
        val r = myFixture.file.text
        assertTrue("record synthesized", r.contains("pub record GreetArgs {"))
        assertTrue("field name", r.contains("name: text"))
        assertTrue("field age", r.contains("age: number"))
        assertTrue("signature rewritten", r.contains("args: GreetArgs"))
        assertTrue("body ref rewritten", r.contains("return args.name"))
    }

    // ---- Inline --------------------------------------------------------

    fun testInlineFn() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn one() -> number {
                return 1;
            }

            pub fn use_it() -> number {
                return one() + one();
            }
            """.trimIndent(),
        )
        val file = myFixture.file as PellFile
        val fn = com.intellij.psi.util.PsiTreeUtil.findChildrenOfType(
            file, dev.pell.intellij.psi.PellFnDef::class.java,
        ).first { it.name == "one" }
        dev.pell.intellij.refactor.inline.PellInlineAction()
            .doInlineFn(project, file, fn, "one", "return 1")
        val r = myFixture.file.text
        assertFalse("declaration removed", r.contains("pub fn one()"))
        assertTrue("call sites inlined", r.contains("(return 1)"))
    }

    // ---- Move ----------------------------------------------------------

    fun testMoveSymbol() {
        val dest = myFixture.addFileToProject(
            "dest.pell",
            """
            module dest;

            pub fn existing() -> number {
                return 0;
            }
            """.trimIndent(),
        ) as PellFile
        myFixture.configureByText(
            "src.pell",
            """
            module src;

            pub fn mover() -> number {
                return 99;
            }
            """.trimIndent(),
        )
        val file = myFixture.file as PellFile
        val target = com.intellij.psi.util.PsiTreeUtil.findChildOfType(
            file, dev.pell.intellij.psi.PellFnDef::class.java,
        )!!
        dev.pell.intellij.refactor.move.PellMoveSymbolAction()
            .doMove(project, file, target, dest, "dest")
        assertFalse("removed from source", myFixture.file.text.contains("mover"))
        assertTrue("added to dest", dest.text.contains("pub fn mover()"))
    }

    // ---- Change Signature ----------------------------------------------

    fun testChangeSignature() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn greet(name: text) -> text {
                return name;
            }
            """.trimIndent(),
        )
        val file = myFixture.file as PellFile
        val fn = com.intellij.psi.util.PsiTreeUtil.findChildOfType(
            file, dev.pell.intellij.psi.PellFnDef::class.java,
        )!!
        val body = com.intellij.psi.util.PsiTreeUtil.findChildOfType(
            fn, dev.pell.intellij.psi.PellBlock::class.java,
        )!!
        val sigStart = fn.textRange.startOffset
        val sigEnd = body.textRange.startOffset
        dev.pell.intellij.refactor.signature.PellChangeSignatureAction()
            .doChangeSignature(
                project, file, sigStart, sigEnd,
                "pub fn greet(name: text, loud: bool) -> text", true,
            )
        val r = myFixture.file.text
        assertTrue("new param added", r.contains("loud: bool"))
        assertTrue("body untouched", r.contains("return name;"))
    }
}
