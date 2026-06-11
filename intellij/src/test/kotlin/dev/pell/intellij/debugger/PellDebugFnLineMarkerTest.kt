package dev.pell.intellij.debugger

import com.intellij.psi.util.PsiTreeUtil
import com.intellij.testFramework.fixtures.BasePlatformTestCase
import dev.pell.intellij.psi.PellElementTypes
import dev.pell.intellij.psi.PellFnDef

/** The per-fn run/debug gutter must appear on `pub fn` name idents
 *  (KW_PUB lives on the wrapping item node — regression: 0.7.0 looked
 *  for it inside fnDef and never showed the icon). */
class PellDebugFnLineMarkerTest : BasePlatformTestCase() {

    private val contributor = PellDebugFnLineMarkerContributor()

    private fun nameIdent(fnName: String): com.intellij.psi.PsiElement {
        val fn = PsiTreeUtil.findChildrenOfType(myFixture.file, PellFnDef::class.java)
            .first { it.name == fnName }
        return PsiTreeUtil.findChildrenOfType(fn, com.intellij.psi.PsiElement::class.java)
            .first { it.node?.elementType == PellElementTypes.IDENT && it.text == fnName }
    }

    fun testPubFnGetsGutterInfo() {
        myFixture.configureByText(
            "a.pell",
            """
            module a;

            pub fn greet(name: text) -> text {
                return name;
            }

            fn helper() {
                p("x");
            }
            """.trimIndent(),
        )
        assertNotNull("pub fn should get run/debug gutter", contributor.getInfo(nameIdent("greet")))
        assertNull("private fn should NOT get the gutter", contributor.getInfo(nameIdent("helper")))
    }
}
