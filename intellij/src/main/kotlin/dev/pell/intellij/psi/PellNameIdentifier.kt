package dev.pell.intellij.psi

/**
 * Marker for the *Name wrapper rules in Pell.bnf (fnName, recordName,
 * paramName, ...). Each wraps a single IDENT token whose text is the
 * declared name; for dotted/qualified names (moduleName, seqName) the
 * wrapper's `.text` is the full dotted/qualified path.
 *
 * The PellNamedElementImpl base walks children for the first
 * PellNameIdentifier and returns it from getNameIdentifier(). That's
 * what plumbs the platform's PsiNameIdentifierOwner contract into
 * every pell declaration without per-rule boilerplate.
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
interface PellNameIdentifier : PellPsiElement
