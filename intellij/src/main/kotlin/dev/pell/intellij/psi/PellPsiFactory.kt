package dev.pell.intellij.psi

import com.intellij.openapi.project.Project
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiFileFactory
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.PellFile
import dev.pell.intellij.PellFileType
// Grammar-Kit-generated PSI interfaces (compiled from src/main/grammar/Pell.bnf
// into src/main/gen/dev/pell/intellij/psi/). Available after `./gradlew
// generateParser`.

/**
 * Helper for synthesising pell PSI nodes from text. Used by rename
 * (to produce a fresh IDENT for the new name), Extract Method (Phase
 * 6) for synthesising fn definitions and call sites, and the rest of
 * the refactoring stack downstream.
 *
 * Parses a throwaway file rooted at an arbitrary pell production, then
 * lifts the relevant subtree out. The throwaway file's lifetime ends
 * with the action.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
object PellPsiFactory {

    /** Build a pell file from arbitrary source text. The file is not
     *  added to the project model — caller harvests subtrees from it. */
    private fun fileFromText(project: Project, text: String): PellFile {
        return PsiFileFactory.getInstance(project)
            .createFileFromText("dummy.pell", PellFileType, text)
            as PellFile
    }

    /** Synthesise a bare IDENT token PSI element. Used by setName. */
    fun createIdent(project: Project, name: String): PsiElement? {
        // The cheapest container that holds an IDENT is a `let` statement
        // inside a top-level fn. We parse it and lift out the IDENT.
        val file = fileFromText(project, "module x; fn f() { let $name = 0; }")
        val let = PsiTreeUtil.findChildOfType(file, PellLetStmt::class.java) ?: return null
        return let.letName?.firstChild
    }

    /** Synthesise an empty `pub fn <name>() {}` declaration. Used by
     *  Extract Method (Phase 6). */
    fun createPubFn(project: Project, name: String): PellFnDef? {
        val file = fileFromText(project, "module x; pub fn $name() {}")
        return PsiTreeUtil.findChildOfType(file, PellFnDef::class.java)
    }

    /** Synthesise a struct-literal expression like `Args { a: x }`.
     *  Used by Parameters → Record (Phase 7). */
    fun createStructLit(project: Project, type: String, fields: List<Pair<String, String>>): PellQualifiedExpr? {
        val body = fields.joinToString(", ") { (k, v) -> "$k: $v" }
        val file = fileFromText(project, "module x; fn f() { let _ = $type { $body }; }")
        return PsiTreeUtil.findChildOfType(file, PellQualifiedExpr::class.java)
    }

    /** Parse a qualified expression (`foo`, `foo::bar::baz`) from text.
     *  Used by the rename ElementManipulator to rebuild a reference
     *  after splicing the new name into its text. */
    fun createQualifiedExpr(project: Project, exprText: String): PellQualifiedExpr {
        val file = fileFromText(project, "module x; fn f() { let _ = $exprText; }")
        return PsiTreeUtil.findChildOfType(file, PellQualifiedExpr::class.java)
            ?: error("could not parse qualified expr from '$exprText'")
    }

    /** Parse a type reference (`Foo`, `list<Foo>`, `pkg::Foo`) from
     *  text. Used by the rename ElementManipulator for type refs. */
    fun createTypeRef(project: Project, typeText: String): PellTypeRef {
        val file = fileFromText(project, "module x; fn f(p: $typeText) {}")
        return PsiTreeUtil.findChildOfType(file, PellTypeRef::class.java)
            ?: error("could not parse type ref from '$typeText'")
    }
}
