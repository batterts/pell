package dev.pell.intellij

import com.intellij.lang.Language

/** The pell language registration. PSI features come from LSP4IJ. */
object PellLanguage : Language("pell") {
    private fun readResolve(): Any = PellLanguage
}
