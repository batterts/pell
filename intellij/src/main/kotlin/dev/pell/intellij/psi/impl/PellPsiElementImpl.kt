package dev.pell.intellij.psi.impl

import com.intellij.extapi.psi.ASTWrapperPsiElement
import com.intellij.lang.ASTNode
import dev.pell.intellij.psi.PellPsiElement

/**
 * Base class for every pell PSI element. Grammar-Kit's
 * `extends(".*") = "..."` mapping makes every generated PSI impl class
 * extend this. Inherits ASTWrapperPsiElement (the standard
 * IElementType-backed implementation) and tags us as a PellPsiElement.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
open class PellPsiElementImpl(node: ASTNode) : ASTWrapperPsiElement(node), PellPsiElement
