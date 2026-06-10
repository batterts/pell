package dev.pell.intellij.parser

import com.intellij.psi.util.PsiTreeUtil
import com.intellij.psi.PsiErrorElement
import com.intellij.testFramework.fixtures.BasePlatformTestCase

/**
 * Regression: the Grammar-Kit grammar must accept range expressions
 * (`1..=10`, `0..5`) the same way the Python parser does, or the IDE
 * red-squiggles `for i in 1..=10` even though `pell build` accepts it.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellRangeParseTest : BasePlatformTestCase() {

    private fun assertParses(src: String) {
        myFixture.configureByText("a.pell", src)
        val errors = PsiTreeUtil.findChildrenOfType(
            myFixture.file, PsiErrorElement::class.java,
        )
        assertTrue(
            "unexpected parse errors: ${errors.joinToString { it.errorDescription }}",
            errors.isEmpty(),
        )
    }

    fun testInclusiveRange() = assertParses(
        "module a;\npub fn f() {\n  for i in 1..=10 { p(i); }\n}",
    )

    fun testExclusiveRangeSpaced() = assertParses(
        "module a;\npub fn f() {\n  for i in 1 .. 10 { p(i); }\n}",
    )

    fun testListLiteralFor() = assertParses(
        "module a;\npub fn f() {\n  for x in [1, 2, 3] { p(x); }\n}",
    )
}
