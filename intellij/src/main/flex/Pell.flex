// JFlex spec for the pell language lexer.
//
// PSI TRACK PHASE 0 STATUS: smoke implementation. This file defines
// only enough tokens to prove the Grammar-Kit + JFlex toolchain
// generates working code. Phase 1 replaces the production rules with
// the full port of compiler/pell/lexer.py.
//
// Source of truth: compiler/pell/lexer.py. Any divergence in token
// recognition between this file and the Python lexer is a bug and
// will be caught by intellij/src/test/.../parser/RoundTripTest.kt.
//
// Generated to: src/main/gen/dev/pell/intellij/parser/_PellLexer.java
// Wrap with com.intellij.lexer.FlexAdapter to use in PellParserDefinition.

package dev.pell.intellij.parser;

import com.intellij.lexer.FlexLexer;
import com.intellij.psi.tree.IElementType;
import static dev.pell.intellij.psi.PellTokenTypes.*;
import static com.intellij.psi.TokenType.WHITE_SPACE;
import static com.intellij.psi.TokenType.BAD_CHARACTER;

%%

%class _PellLexer
%implements FlexLexer
%function advance
%type IElementType
%unicode

EOL                = \r|\n|\r\n
WHITESPACE         = ({EOL}|[ \t\f])+
LINE_COMMENT_BODY  = "//" [^\r\n]*
BLOCK_COMMENT_BODY = "/*" ~"*/"
IDENT_BODY         = [a-zA-Z_][a-zA-Z0-9_]*

%%

<YYINITIAL> {
  {WHITESPACE}          { return WHITE_SPACE; }
  {LINE_COMMENT_BODY}   { return LINE_COMMENT; }
  {BLOCK_COMMENT_BODY}  { return BLOCK_COMMENT; }
  {IDENT_BODY}          { return IDENT; }
  .                     { return BAD_CHARACTER; }
}
