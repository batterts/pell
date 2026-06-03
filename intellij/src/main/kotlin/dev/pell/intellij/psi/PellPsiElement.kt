package dev.pell.intellij.psi

import com.intellij.psi.PsiElement

/**
 * Common marker interface for every PSI element in the pell language.
 * Grammar-Kit's `implements(".*") = "..."` mapping makes every
 * generated PSI interface extend this one.
 *
 * Lets refactoring code do `is PellPsiElement` checks instead of
 * walking every concrete subtype, and gives us a single place to add
 * pell-wide convenience methods later (containing module, file-scope
 * imports, etc.).
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
interface PellPsiElement : PsiElement
