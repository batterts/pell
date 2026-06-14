package dev.pell.intellij.debugger

import com.intellij.psi.PsiFile
import com.intellij.psi.util.PsiTreeUtil
import dev.pell.intellij.psi.PellFieldDef
import dev.pell.intellij.psi.PellFnDef
import dev.pell.intellij.psi.PellModuleDecl
import dev.pell.intellij.psi.PellParam
import dev.pell.intellij.psi.PellRecordDef
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
        return defaultForType(ty)
    }

    /** A placeholder value of the right shape for a pell type. */
    private fun defaultForType(ty: String): String = when {
        ty == "number" || ty == "int" -> "1"
        ty == "text" || ty == "bigtext" -> "\"x\""
        ty == "bool" -> "true"
        ty == "json" -> "json::object()"
        ty.startsWith("list<") -> "[]"
        ty == "date" || ty == "timestamp" -> "now()"
        else -> "/* TODO: $ty */ \"\""
    }

    private fun recordDefInFile(file: PsiFile, typeName: String): PellRecordDef? =
        PsiTreeUtil.findChildrenOfType(file, PellRecordDef::class.java)
            .firstOrNull { it.name == typeName }

    /** A struct literal for `rec`, qualified by `module`, with each field
     *  filled by a shape-appropriate placeholder:
     *  `module::Rec { a: 1, b: "x", c: true }`. */
    private fun recordLiteral(module: String, rec: PellRecordDef): String {
        val fields = rec.fieldDefList?.fieldDefList.orEmpty()
        val parts = fields.joinToString(", ") { f ->
            val name = f.fieldName?.text ?: "field"
            val ty = f.typeRef?.text ?: "text"
            "$name: ${defaultForType(ty)}"
        }
        return "$module::${rec.name} { $parts }"
    }

    private fun paramName(param: PellParam): String =
        param.text.substringBefore(":").trim().ifBlank { "arg" }

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
        // Record-typed args can't be written inline — bind each to a local
        // first (`let <name> = pkg::Rec { ... };`) and pass the local, so the
        // stub is valid pell out of the box instead of a `/* TODO */ ""`.
        val prelude = StringBuilder()
        val args = params.joinToString(", ") { p ->
            val ty = PsiTreeUtil.findChildOfType(p, PellTypeRef::class.java)?.text ?: "text"
            val rec = recordDefInFile(file, ty)
            if (rec != null) {
                val nm = paramName(p)
                prelude.appendLine("let $nm = ${recordLiteral(module, rec)};")
                nm
            } else {
                defaultForType(ty)
            }
        }
        val ret = effectiveReturnType(fn)
        val call = "$module::$fnName($args)"
        return buildString {
            appendLine("// Debug stub for $module::$fnName — edit the arguments, set")
            appendLine("// breakpoints here or in ${file.name}, then Debug this file.")
            appendLine("import $module;")
            appendLine()
            if (prelude.isNotEmpty()) append(prelude)
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
