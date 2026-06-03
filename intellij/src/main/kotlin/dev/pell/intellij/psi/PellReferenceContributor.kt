package dev.pell.intellij.psi

import com.intellij.openapi.util.TextRange
import com.intellij.patterns.PlatformPatterns
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiElementResolveResult
import com.intellij.psi.PsiPolyVariantReferenceBase
import com.intellij.psi.PsiReference
import com.intellij.psi.PsiReferenceContributor
import com.intellij.psi.PsiReferenceProvider
import com.intellij.psi.PsiReferenceRegistrar
import com.intellij.psi.ResolveResult
import com.intellij.util.ProcessingContext

/**
 * Wires PsiReferences onto qualified-expression nodes and type-reference
 * nodes. The platform then routes Go To Definition (Cmd-Click), Find
 * Usages (Cmd-G), and Rename (Shift-F6) through them.
 *
 * Phase 4 implementation:
 *   - PellQualifiedExpr (foo::bar::baz in expression position) →
 *     resolves to the last-segment named declaration.
 *   - PellTypeRef (named types in declarations) → resolves to the
 *     matching record / type / sealed type / enum / error.
 *
 * Resolution uses [PellSymbolScanner]; will switch to stub-backed
 * lookup once stubs land (no API change required).
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
class PellReferenceContributor : PsiReferenceContributor() {
    override fun registerReferenceProviders(registrar: PsiReferenceRegistrar) {
        registrar.registerReferenceProvider(
            PlatformPatterns.psiElement(PellQualifiedExpr::class.java),
            QualifiedProvider,
        )
        registrar.registerReferenceProvider(
            PlatformPatterns.psiElement(PellTypeRef::class.java),
            TypeRefProvider,
        )
    }

    private object QualifiedProvider : PsiReferenceProvider() {
        override fun getReferencesByElement(element: PsiElement, context: ProcessingContext): Array<PsiReference> {
            val qe = element as? PellQualifiedExpr ?: return PsiReference.EMPTY_ARRAY
            // For Rename: the reference range covers only the LAST segment
            // of `foo::bar::baz`. Renaming `baz` therefore rewrites just the
            // tail and leaves the qualifier untouched.
            val pathText = qe.text.substringBefore('{').trimEnd()
            val lastSep = pathText.lastIndexOf("::")
            val lastStart = if (lastSep >= 0) lastSep + 2 else 0
            val range = TextRange(lastStart, pathText.length)
            return arrayOf(PellQualifiedReference(qe, range))
        }
    }

    private object TypeRefProvider : PsiReferenceProvider() {
        override fun getReferencesByElement(element: PsiElement, context: ProcessingContext): Array<PsiReference> {
            val tr = element as? PellTypeRef ?: return PsiReference.EMPTY_ARRAY
            // Phase 4 simplification: resolve the whole typeRef text against
            // named record/type/enum/error decls. Phase 8 will split this
            // into per-atom references for generics like `list<Employee>`.
            val text = tr.text
            val range = TextRange(0, text.length)
            return arrayOf(PellTypeReference(tr, range))
        }
    }
}

/** Reference for a `foo::bar::baz` qualified expression. */
class PellQualifiedReference(
    element: PellQualifiedExpr,
    range: TextRange,
) : PsiPolyVariantReferenceBase<PellQualifiedExpr>(element, range, /*soft*/ true) {

    override fun multiResolve(incompleteCode: Boolean): Array<ResolveResult> {
        val path = element.text.substringBefore('{').trim()
        val lastSegment = path.substringAfterLast("::").substringAfterLast('.')
        return PellSymbolScanner.findAllByName(element.project, lastSegment)
            .map { PsiElementResolveResult(it) }
            .toTypedArray()
    }

    override fun getVariants(): Array<Any> = emptyArray()
}

/** Reference for a `Foo` (or `list<Foo>`) type reference. */
class PellTypeReference(
    element: PellTypeRef,
    range: TextRange,
) : PsiPolyVariantReferenceBase<PellTypeRef>(element, range, /*soft*/ true) {

    override fun multiResolve(incompleteCode: Boolean): Array<ResolveResult> {
        val name = element.text.substringBefore('<').substringAfterLast("::").trim()
        if (name.isEmpty()) return ResolveResult.EMPTY_ARRAY
        return PellSymbolScanner.findAllByName(element.project, name)
            .map { PsiElementResolveResult(it) }
            .toTypedArray()
    }

    override fun getVariants(): Array<Any> = emptyArray()
}
