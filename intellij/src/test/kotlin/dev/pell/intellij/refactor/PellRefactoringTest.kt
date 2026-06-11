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

    /** A local declared in the selection but used after it must be
     *  RETURNED, and the call site rebinds it. (User-reported bug:
     *  `let kv = ...` got swallowed into the extracted fn, leaving
     *  `return kv;` referencing nothing.) */
    fun testExtractMethodWithOutput() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn parse_kv(s: text) -> KV {
                let kv: KV = re::capture::<KV>(s, /x/);
                return kv;
            }
            """.trimIndent(),
        )
        val text = myFixture.file.text
        val start = text.indexOf("    let kv")
        val end = text.indexOf(";", start) + 1
        myFixture.editor.selectionModel.setSelection(start, end)

        val file = myFixture.file as PellFile
        val analysis = ExtractMethodAnalyzer.analyze(file, myFixture.editor)
        assertTrue("extractable: ${analysis.rejectionReason}", analysis.isExtractable)
        assertEquals("one output (kv)", 1, analysis.outputs.size)
        assertEquals("kv", analysis.outputs.first().name)
        assertEquals("KV", analysis.outputs.first().typeText)

        PellExtractMethodHandler().applyExtract(
            project, file, myFixture.editor, analysis, "extract_kv", false,
        )
        val r = myFixture.file.text
        // Extracted fn returns KV and ends with `return kv;`.
        assertTrue("fn returns KV with typed param", r.contains("fn extract_kv(s: text) -> KV"))
        assertTrue("fn returns the output", r.contains("return kv;"))
        // Call site rebinds kv so `return kv;` in parse_kv still resolves.
        assertTrue("call rebinds kv", r.contains("let kv: KV = extract_kv(s);"))
    }

    /** Captured params carry their declared types — `any` never appears
     *  (it lowers to a non-existent t_any and fails at deploy). */
    fun testExtractMethodTypedParams() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn run(n: number) {
                let label: text = "x";
                p(label);
                p(n);
            }
            """.trimIndent(),
        )
        val text = myFixture.file.text
        val start = text.indexOf("    p(label);")
        val end = text.indexOf("p(n);") + "p(n);".length
        myFixture.editor.selectionModel.setSelection(start, end)
        val file = myFixture.file as PellFile
        val analysis = ExtractMethodAnalyzer.analyze(file, myFixture.editor)
        assertTrue("extractable: ${analysis.rejectionReason}", analysis.isExtractable)
        PellExtractMethodHandler().applyExtract(
            project, file, myFixture.editor, analysis, "printed", false,
        )
        val r = myFixture.file.text
        assertTrue("label typed text", r.contains("label: text"))
        assertTrue("n typed number", r.contains("n: number"))
        assertFalse("no any params", r.contains(": any"))
    }

    /** A whole for-loop is one statement of its parent block and must be
     *  extractable. (User-reported: the closing `}` of the loop body made
     *  the analyzer think the selection straddled two blocks.) */
    fun testExtractMethodWholeForLoop() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn fill(t: number, months: number) {
                for i in 1..months {
                    sql!{insert into cal values ((:t + i))};
                }
            }
            """.trimIndent(),
        )
        val text = myFixture.file.text
        val start = text.indexOf("for i in")
        val end = text.indexOf("}", text.indexOf("sql!")) + 1
        myFixture.editor.selectionModel.setSelection(start, end)
        val file = myFixture.file as PellFile
        val analysis = ExtractMethodAnalyzer.analyze(file, myFixture.editor)
        assertTrue("extractable: ${analysis.rejectionReason}", analysis.isExtractable)
        // `i` is the loop's own var — must NOT become a parameter.
        assertFalse(
            "loop var i is not captured",
            analysis.capturedParams.any { it.name == "i" },
        )
        PellExtractMethodHandler().applyExtract(
            project, file, myFixture.editor, analysis, "fill_months", false,
        )
        val r = myFixture.file.text
        assertTrue("new fn synthesized", r.contains("fn fill_months("))
        assertTrue("loop moved into it", r.contains("for i in 1..months"))
    }

    /** Extracting from INSIDE a loop body captures the loop var as a
     *  typed parameter (ranges iterate numbers). */
    fun testExtractMethodFromLoopBodyCapturesLoopVar() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn fill(months: number) {
                for i in 1..months {
                    p(i);
                }
            }
            """.trimIndent(),
        )
        val text = myFixture.file.text
        val start = text.indexOf("p(i);")
        val end = start + "p(i);".length
        myFixture.editor.selectionModel.setSelection(start, end)
        val file = myFixture.file as PellFile
        val analysis = ExtractMethodAnalyzer.analyze(file, myFixture.editor)
        assertTrue("extractable: ${analysis.rejectionReason}", analysis.isExtractable)
        val i = analysis.capturedParams.firstOrNull { it.name == "i" }
        assertNotNull("loop var i captured as param", i)
        assertEquals("range loop var is number", "number", i!!.typeText)
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

    /** Move rewrites references: qualified callers flip to the new
     *  module's qualifier (+ import added); bare uses in the old home
     *  get qualified. */
    fun testMoveSymbolRewritesReferences() {
        val dest = myFixture.addFileToProject(
            "dest2.pell",
            """
            module dest2;

            pub fn existing() -> number {
                return 0;
            }
            """.trimIndent(),
        ) as PellFile
        val caller = myFixture.addFileToProject(
            "caller2.pell",
            """
            module caller2;
            import src2;

            pub fn use_it() -> number {
                return src2::mover() + 1;
            }
            """.trimIndent(),
        )
        myFixture.configureByText(
            "src2.pell",
            """
            module src2;

            pub fn mover() -> number {
                return 99;
            }

            pub fn local_user() -> number {
                return mover() + 1;
            }
            """.trimIndent(),
        )
        val file = myFixture.file as PellFile
        val target = com.intellij.psi.util.PsiTreeUtil.findChildrenOfType(
            file, dev.pell.intellij.psi.PellFnDef::class.java,
        ).first { it.name == "mover" }
        dev.pell.intellij.refactor.move.PellMoveSymbolAction()
            .doMove(project, file, target, dest, "dest2")
        // Cross-file qualified caller flips qualifier + gains import.
        assertTrue("caller requalified", caller.text.contains("dest2::mover()"))
        assertTrue("caller imports dest2", caller.text.contains("import dest2;"))
        // Bare use in the old home gets qualified + import.
        val src = myFixture.file.text
        assertTrue("old-home bare use qualified", src.contains("dest2::mover()"))
        assertTrue("old home imports dest2", src.contains("import dest2;"))
        // The decl itself lives in dest now.
        assertTrue("decl moved", dest.text.contains("pub fn mover()"))
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
