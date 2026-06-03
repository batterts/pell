package dev.pell.intellij.symbols

import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.psi.PellExpr
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellModuleDecl
import dev.pell.intellij.psi.PellNamedElement
import dev.pell.intellij.psi.PellRecordDef
import dev.pell.intellij.psi.PellSqlBlockExpr
import dev.pell.intellij.symbols.index.PellProjectIndex

/**
 * Project-level facade that every PSI consumer (inspections,
 * refactorings, completion, intentions, code lens, etc.) talks to
 * when it needs symbol information.
 *
 * The facade hides the question of *who knows*: the PSI's
 * project-source scan, the LSP's stdlib catalog, or the LSP's
 * data-dictionary introspection. Today's L1 implementation answers
 * everything from the PSI side and returns empty for stdlib /
 * Oracle. L3 wires the LSP path; L2 swaps the PSI backing from
 * PsiTreeUtil to StubIndex (perf only, no API change).
 *
 * Owned by L1 of LSP_PSI_UNIFICATION.md.
 *
 * Access:
 *     val symbols = PellSymbolService.getInstance(project)
 */
interface PellSymbolService {

    /** Every project + stdlib symbol matching the given short name. */
    fun findSymbol(name: String): List<PellSymbolInfo>

    /** Symbol at the given qualified path (`foo::bar::baz` form).
     *  Null when unresolved. */
    fun findQualified(path: String): PellSymbolInfo?

    /** Static type of the expression at [expr]'s tree position.
     *  Returns [PellType.Unknown] when the inferencer can't decide
     *  — downstream code treats that as "don't act on this." */
    fun typeOf(expr: PellExpr): PellType

    /** Fields declared on the given record / type / sealed type.
     *  Empty for non-record types. */
    fun fieldsOf(type: PellType): List<PellFieldInfo>

    /** Methods declared on the given record / type / sealed type. */
    fun methodsOf(type: PellType): List<PellSymbolInfo>

    /** Resolve a `module foo.bar;` declaration by its dotted name. */
    fun resolveModule(dottedName: String): PellModuleInfo?

    /** All module declarations the given file imports + the module it
     *  itself declares — used by qualified-name resolution and
     *  completion. */
    fun importsOf(file: dev.pell.intellij.PellFile): List<PellModuleInfo>

    /** Pell stdlib symbols (`catalog::*`, `logger::*`, `p`, runtime
     *  helpers). Empty until L3 wires the `pell/symbolCatalog` LSP
     *  method; downstream code is built to tolerate that. */
    fun stdlibSymbols(): List<PellSymbolInfo>

    /** Effects of a sql!{...} block. Default-empty until L5. */
    fun effectsOf(sqlBlock: PellSqlBlockExpr): SqlEffects

    /** Convenience: pub fns with the given short name, project-wide. */
    fun findPubFns(name: String? = null): List<PellSymbolInfo>

    /** Convenience: pub records with the given short name, project-wide. */
    fun findPubRecords(name: String? = null): List<PellSymbolInfo>

    companion object {
        fun getInstance(project: Project): PellSymbolService = project.service()
    }
}

/**
 * L2 implementation — backed by [PellProjectIndex] (O(1) HashMap
 * lookups, refreshed by a VFS listener). Same API as the L1
 * implementation; consumers see no behavioural change beyond not
 * paying the project-wide PsiTreeUtil scan cost per lookup.
 */
@Service(Service.Level.PROJECT)
class PellSymbolServiceImpl(private val project: Project) : PellSymbolService {

    private val index get() = PellProjectIndex.getInstance(project)

    override fun findSymbol(name: String): List<PellSymbolInfo> =
        index.findByName(name).map(::toInfo)

    override fun findQualified(path: String): PellSymbolInfo? {
        // Fast path: an exact qualified-name match. Slow path: tail
        // matching (qualifier `hello` matches `module pell_test.hello`'s
        // tail).
        index.findByQualifiedName(path).firstOrNull()?.let { return toInfo(it) }

        val parts = path.split("::").filter { it.isNotEmpty() }
        if (parts.isEmpty()) return null
        val short = parts.last()
        val qualifier = if (parts.size > 1) parts.dropLast(1).joinToString(".") else null
        return findSymbol(short).firstOrNull { info ->
            if (qualifier == null) return@firstOrNull true
            val moduleName = info.qualifiedName.substringBeforeLast("::", missingDelimiterValue = "")
                .ifEmpty { return@firstOrNull false }
            moduleName == qualifier || moduleName.endsWith(".$qualifier")
        }
    }

    /** Best-effort type inference for L1: looks at the structural shape
     *  of the expression. The real inferencer ships in L4. */
    override fun typeOf(expr: PellExpr): PellType {
        // L1 placeholder — only handles the trivial "expr is a single
        // qualified-name that resolves to a known symbol" case, by
        // returning the symbol's declared type. Everything else returns
        // Unknown for downstream code to skip safely.
        val text = expr.text.trim()
        if (text.isEmpty()) return PellType.Unknown
        val sym = findSymbol(text.substringAfterLast("::"))
            .firstOrNull { it.qualifiedName == text || it.name == text }
        return sym?.type ?: PellType.Unknown
    }

    override fun fieldsOf(type: PellType): List<PellFieldInfo> {
        val name = (type as? PellType.Named)?.qualifiedName ?: return emptyList()
        val sym = findSymbol(name.substringAfterLast("::"))
            .firstOrNull { it.qualifiedName == name || it.name == name }
        return sym?.fields ?: emptyList()
    }

    override fun methodsOf(type: PellType): List<PellSymbolInfo> {
        // L1: methods aren't indexed yet; future work routes through here.
        return emptyList()
    }

    override fun resolveModule(dottedName: String): PellModuleInfo? {
        val decl = index.findModule(dottedName) ?: return null
        return PellModuleInfo(dottedName = decl.name ?: dottedName, location = decl)
    }

    override fun importsOf(file: dev.pell.intellij.PellFile): List<PellModuleInfo> {
        val module = PsiTreeUtil.findChildOfType(file, PellModuleDecl::class.java)
            ?: return emptyList()
        return listOf(PellModuleInfo(dottedName = module.name ?: "", location = module))
    }

    override fun stdlibSymbols(): List<PellSymbolInfo> = emptyList()

    override fun effectsOf(sqlBlock: PellSqlBlockExpr): SqlEffects = SqlEffects.NONE

    override fun findPubFns(name: String?): List<PellSymbolInfo> {
        val matches = if (name != null) {
            index.findByName(name).filterIsInstance<PellFnDef>()
        } else {
            index.allOfKind { it is PellFnDef }.filterIsInstance<PellFnDef>()
        }
        return matches.map(::toFnInfo)
    }

    override fun findPubRecords(name: String?): List<PellSymbolInfo> {
        val matches = if (name != null) {
            index.findByName(name).filterIsInstance<PellRecordDef>()
        } else {
            index.allOfKind { it is PellRecordDef }.filterIsInstance<PellRecordDef>()
        }
        return matches.map(::toRecordInfo)
    }

    // ---------- mapping PSI → DTO ----------

    private fun toInfo(named: PellNamedElement): PellSymbolInfo = when (named) {
        is PellFnDef     -> toFnInfo(named)
        is PellRecordDef -> toRecordInfo(named)
        else             -> PellSymbolInfo(
            name = named.name ?: "",
            qualifiedName = qualify(named),
            kind = kindOf(named),
            type = null,
            params = emptyList(),
            fields = emptyList(),
            source = SymbolSource.PROJECT,
            location = named,
            isPub = true,
        )
    }

    private fun toFnInfo(fn: PellFnDef): PellSymbolInfo {
        val returnTypeRef = PsiTreeUtil.getChildOfType(fn, dev.pell.intellij.psi.PellTypeRef::class.java)
        val returnType = returnTypeRef?.text?.let(PellType::parse) ?: PellType.Unit
        val params = PsiTreeUtil.findChildrenOfType(fn, dev.pell.intellij.psi.PellParam::class.java)
            .map { p ->
                PellParamInfo(
                    name = p.name ?: "",
                    type = PsiTreeUtil.getChildOfType(p, dev.pell.intellij.psi.PellTypeRef::class.java)
                        ?.text?.let(PellType::parse) ?: PellType.Unknown,
                )
            }
        return PellSymbolInfo(
            name = fn.name ?: "",
            qualifiedName = qualify(fn),
            kind = SymbolKind.FN,
            type = returnType,
            params = params,
            fields = emptyList(),
            source = SymbolSource.PROJECT,
            location = fn,
            isPub = true,
        )
    }

    private fun toRecordInfo(record: PellRecordDef): PellSymbolInfo {
        val fields = PsiTreeUtil.findChildrenOfType(record, dev.pell.intellij.psi.PellFieldDef::class.java)
            .map { f ->
                PellFieldInfo(
                    name = f.name ?: "",
                    type = PsiTreeUtil.getChildOfType(f, dev.pell.intellij.psi.PellTypeRef::class.java)
                        ?.text?.let(PellType::parse) ?: PellType.Unknown,
                )
            }
        return PellSymbolInfo(
            name = record.name ?: "",
            qualifiedName = qualify(record),
            kind = SymbolKind.RECORD,
            type = PellType.Named(qualify(record)),
            params = emptyList(),
            fields = fields,
            source = SymbolSource.PROJECT,
            location = record,
            isPub = true,
        )
    }

    /** Build `module::name`-style qualifier from the element's containing file. */
    private fun qualify(named: PellNamedElement): String {
        val module = named.containingFile?.let {
            PsiTreeUtil.findChildOfType(it, PellModuleDecl::class.java)
        }
        val moduleTail = module?.name?.substringAfterLast(".") ?: return named.name ?: ""
        return "${moduleTail}::${named.name ?: ""}"
    }

    private fun kindOf(named: PellNamedElement): SymbolKind = when (named) {
        is PellFnDef     -> SymbolKind.FN
        is PellRecordDef -> SymbolKind.RECORD
        is PellModuleDecl -> SymbolKind.MODULE
        is dev.pell.intellij.psi.PellErrorDef        -> SymbolKind.ERROR
        is dev.pell.intellij.psi.PellTypeDef         -> SymbolKind.TYPE
        is dev.pell.intellij.psi.PellSealedTypeDef   -> SymbolKind.SEALED_TYPE
        is dev.pell.intellij.psi.PellAggregateDef    -> SymbolKind.AGGREGATE
        is dev.pell.intellij.psi.PellEnumDef         -> SymbolKind.ENUM
        is dev.pell.intellij.psi.PellSeqDef          -> SymbolKind.SEQUENCE
        is dev.pell.intellij.psi.PellMethodDef       -> SymbolKind.METHOD
        is dev.pell.intellij.psi.PellEnumVariant     -> SymbolKind.ENUM_VARIANT
        is dev.pell.intellij.psi.PellParam           -> SymbolKind.PARAM
        is dev.pell.intellij.psi.PellFieldDef        -> SymbolKind.FIELD
        is dev.pell.intellij.psi.PellLetStmt         -> SymbolKind.LET
        is dev.pell.intellij.psi.PellVarStmt         -> SymbolKind.VAR
        else                                         -> SymbolKind.FN
    }
}
