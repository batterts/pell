package dev.pell.intellij.debugger

import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellModuleDecl
import dev.pell.intellij.psi.PellParam
import dev.pell.intellij.psi.PellTypeRef
import java.io.File

/**
 * Generates the debug stub for a `pub fn` — a small pell exec script
 * that imports the fn's module and calls it with placeholder args.
 * The stub is a real editable file (project `.pell-debug/` dir): the
 * user fills in arguments, sets breakpoints IN THE STUB or in the
 * target fn, and debugging steps through both (the stub becomes the
 * anonymous block; the fn is in its deployed package).
 */
object PellDebugStubGenerator {

    /** Short module name as call sites use it: last dotted segment,
     *  schema prefix stripped (`billing::charges.core` -> `core`). */
    fun shortModuleName(decl: PellModuleDecl): String {
        val text = decl.text.removePrefix("module").trim().trimEnd(';').trim()
        return text.substringAfter("::").substringAfterLast('.').trim()
    }

    fun defaultArgFor(param: PellParam): String {
        val ty = PsiTreeUtil.findChildOfType(param, PellTypeRef::class.java)?.text ?: "text"
        return when {
            ty == "number" -> "1"
            ty == "text" || ty == "bigtext" -> "\"x\""
            ty == "bool" -> "true"
            ty.startsWith("list<") -> "[]"
            ty == "date" || ty == "timestamp" -> "now()"
            else -> "/* TODO: $ty */ \"\""
        }
    }

    /** The fn's return type with Result<T, ...> unwrapped to T, or
     *  null for procedures / Unit-like returns (which lower to PL/SQL
     *  PROCEDUREs — binding their "result" emits invalid code). */
    fun effectiveReturnType(fn: PellFnDef): String? {
        val header = fn.text.substringBefore("{")
        if (!header.contains("->")) return null
        var t = header.substringAfter("->").trim()
        if (t.startsWith("Result<")) {
            // first top-level type argument
            val inner = t.removePrefix("Result<").substringBeforeLast(">")
            var depth = 0
            var cut = inner.length
            for ((i, c) in inner.withIndex()) {
                when (c) {
                    '<' -> depth++
                    '>' -> depth--
                    ',' -> if (depth == 0) { cut = i; break }
                }
            }
            t = inner.substring(0, cut).trim()
        }
        return t.takeIf { it.isNotBlank() && it != "Unit" && it != "()" }
    }

    private val printable = setOf("text", "bigtext", "number", "bool",
                                  "date", "timestamp", "json")

    fun stubText(fn: PellFnDef): String {
        val file = fn.containingFile
        val moduleDecl = PsiTreeUtil.findChildOfType(file, PellModuleDecl::class.java)
        val module = moduleDecl?.let { shortModuleName(it) } ?: "unknown_module"
        val fnName = fn.name ?: "unknown_fn"
        val params = PsiTreeUtil.findChildrenOfType(fn, PellParam::class.java).toList()
        val args = params.joinToString(", ") { defaultArgFor(it) }
        val ret = effectiveReturnType(fn)
        val call = "$module::$fnName($args)"
        return buildString {
            appendLine("// Debug stub for $module::$fnName — edit the arguments, set")
            appendLine("// breakpoints here or in ${file.name}, then Debug this file.")
            appendLine("import $module;")
            appendLine()
            when {
                ret == null -> appendLine("$call;")
                ret in printable || ret.startsWith("json") -> {
                    appendLine("let result = $call;")
                    appendLine("p(\"{result}\");")
                }
                else -> {
                    // record / list returns: bind for the debugger's
                    // Variables view; printing them isn't supported.
                    appendLine("let result = $call;  // inspect `result` in the debugger")
                }
            }
        }
    }

    /** Create (or refresh nothing — keep the user's edits) the stub
     *  file for `fn` and return it. */
    fun stubFile(projectRoot: File, fn: PellFnDef): File {
        val moduleDecl = PsiTreeUtil.findChildOfType(fn.containingFile, PellModuleDecl::class.java)
        val module = moduleDecl?.let { shortModuleName(it) } ?: "module"
        val dir = File(projectRoot, ".pell-debug").apply { mkdirs() }
        val f = File(dir, "debug_${module}_${fn.name}.pell")
        if (!f.exists()) {
            f.writeText(stubText(fn))
        }
        return f
    }
}
