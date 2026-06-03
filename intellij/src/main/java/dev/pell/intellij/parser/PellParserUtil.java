package dev.pell.intellij.parser;

import com.intellij.lang.PsiBuilder;
import com.intellij.lang.parser.GeneratedParserUtilBase;
import com.intellij.psi.tree.IElementType;
import dev.pell.intellij.psi.PellElementTypes;

/**
 * Custom parser utility for Grammar-Kit's external rules — handles the
 * pell-specific disambiguations the Python parser solves with explicit
 * lookahead:
 *
 *   - {@link #consumeIdent} consumes an IDENT whose text equals a given
 *     constant ("map", "state", "step", "merge", "finish") — needed
 *     because these are *contextual keywords* in the Python parser, not
 *     KW_* tokens.
 *
 *   - {@link #atStructLit} peeks past LBRACE to disambiguate
 *     `Name { field: expr }` (struct lit) from `Name { stmt; }` (block).
 *     Mirrors `_looks_like_struct_lit` in compiler/pell/parser.py.
 *
 *   - {@link #atTurbofish} distinguishes `expr::<T>(...)` (turbofish, in
 *     postfix position) from `expr::Path` (further qualification).
 *
 * PSI TRACK — owned by the PSI work stream. See intellij/PSI_TRACK.md.
 */
public class PellParserUtil extends GeneratedParserUtilBase {

    /**
     * If the current token is IDENT and its text matches {@code text},
     * consume it and return true. Otherwise leave the builder untouched.
     */
    public static boolean consumeIdent(PsiBuilder b, int level, String text) {
        if (b.getTokenType() == PellElementTypes.IDENT
                && text.equals(b.getTokenText())) {
            b.advanceLexer();
            return true;
        }
        return false;
    }

    /**
     * True iff the upcoming `Name { ... }` looks like a struct literal
     * (next-after-{ is RBRACE, or IDENT followed by COLON/COMMA/RBRACE)
     * rather than a `Name` reference followed by a block. Mirrors
     * `_looks_like_struct_lit` in parser.py.
     *
     * Used by `qualifiedExprOrStructLit` to decide whether to consume
     * the LBRACE as a struct-literal opener.
     */
    public static boolean atStructLit(PsiBuilder b, int level) {
        if (b.getTokenType() != PellElementTypes.LBRACE) return false;
        IElementType t1 = b.lookAhead(1);
        if (t1 == PellElementTypes.RBRACE) return true;        // Name {}
        if (t1 == PellElementTypes.IDENT) {
            IElementType t2 = b.lookAhead(2);
            return t2 == PellElementTypes.COLON
                || t2 == PellElementTypes.COMMA
                || t2 == PellElementTypes.RBRACE;              // Name { foo: ... }
        }
        return false;
    }

    /** True iff the next two tokens are COLONCOLON LT (turbofish). */
    public static boolean atTurbofish(PsiBuilder b, int level) {
        return b.getTokenType() == PellElementTypes.COLONCOLON
            && b.lookAhead(1) == PellElementTypes.LT;
    }

    /**
     * True iff next is COLONCOLON IDENT (qualified-name continuation).
     * Used to greedy-qualify `foo::bar::baz` in expression position
     * while stopping at `foo::<T>` (turbofish).
     */
    public static boolean atQualCont(PsiBuilder b, int level) {
        return b.getTokenType() == PellElementTypes.COLONCOLON
            && b.lookAhead(1) == PellElementTypes.IDENT;
    }
}
