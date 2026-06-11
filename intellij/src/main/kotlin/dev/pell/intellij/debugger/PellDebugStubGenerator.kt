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

    fun stubText(fn: PellFnDef): String {
        val file = fn.containingFile
        val moduleDecl = PsiTreeUtil.findChildOfType(file, PellModuleDecl::class.java)
        val module = moduleDecl?.let { shortModuleName(it) } ?: "unknown_module"
        val fnName = fn.name ?: "unknown_fn"
        val params = PsiTreeUtil.findChildrenOfType(fn, PellParam::class.java).toList()
        val args = params.joinToString(", ") { defaultArgFor(it) }
        val hasReturn = fn.text.substringBefore("{").contains("->")
        val call = "$module::$fnName($args)"
        return buildString {
            appendLine("// Debug stub for $module::$fnName — edit the arguments, set")
            appendLine("// breakpoints here or in ${file.name}, then Debug this file.")
            appendLine("import $module;")
            appendLine()
            if (hasReturn) {
                appendLine("let result = $call;")
                appendLine("p(\"{result}\");")
            } else {
                appendLine("$call;")
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
