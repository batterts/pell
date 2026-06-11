package dev.pell.intellij.debugger

import com.intellij.psi.util.PsiTreeUtil
import com.intellij.testFramework.fixtures.BasePlatformTestCase
import dev.pell.intellij.psi.PellFnDef

class PellDebugStubGeneratorTest : BasePlatformTestCase() {

    fun testStubForFnWithParamsAndReturn() {
        myFixture.configureByText(
            "billing.pell",
            """
            module acme::billing.charges;

            pub fn apply_charge(account: text, amount: number, dry_run: bool) -> number {
                return amount;
            }
            """.trimIndent(),
        )
        val fn = PsiTreeUtil.findChildrenOfType(myFixture.file, PellFnDef::class.java)
            .first { it.name == "apply_charge" }
        val stub = PellDebugStubGenerator.stubText(fn)
        // Short module name = last dotted segment, schema stripped.
        assertTrue("imports short module name:\n$stub", stub.contains("import charges;"))
        assertTrue("calls with placeholder args:\n$stub",
                   stub.contains("charges::apply_charge(\"x\", 1, true)"))
        assertTrue("captures + prints the result:\n$stub", stub.contains("let result ="))
        assertTrue(stub.contains("p(\"{result}\");"))
    }

    fun testStubForVoidFn() {
        myFixture.configureByText(
            "jobs.pell",
            """
            module jobs;

            pub fn run_nightly() {
                p("running");
            }
            """.trimIndent(),
        )
        val fn = PsiTreeUtil.findChildrenOfType(myFixture.file, PellFnDef::class.java)
            .first { it.name == "run_nightly" }
        val stub = PellDebugStubGenerator.stubText(fn)
        assertTrue(stub.contains("import jobs;"))
        assertTrue(stub.contains("jobs::run_nightly();"))
        assertFalse("void fn: no result binding", stub.contains("let result"))
    }
}
