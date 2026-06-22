// JFlex spec for the pell language lexer. Phase 1 — full port of
// compiler/pell/lexer.py. Token shape exactly mirrors the Python lexer;
// the round-trip test (Phase final) catches any divergence.
//
// Architecture:
//   - YYINITIAL handles 95% of input: whitespace, comments, identifiers,
//     keywords, literals, punctuation.
//   - Two state-machine readers live in the %{...%} block:
//       readRegex()       — consume /.../ literal, set zzMarkedPos
//       readMacroBlock()  — consume sql!{…} or jq!{…}, brace-aware,
//                           string-aware, --comment-aware
//   - lastWasValue tracks "did the previous significant token produce a
//     value?" — mirrors _VALUE_TOKEN_KINDS in lexer.py. Used to
//     disambiguate `/` between division and the start of a regex literal.
//
// Generated to: src/main/gen/dev/pell/intellij/parser/_PellLexer.java
// Used by:      PellLexerAdapter (FlexAdapter wrapper, Phase 2).

package dev.pell.intellij.parser;

import com.intellij.lexer.FlexLexer;
import com.intellij.psi.tree.IElementType;
import static dev.pell.intellij.psi.PellElementTypes.*;
import static com.intellij.psi.TokenType.WHITE_SPACE;
import static com.intellij.psi.TokenType.BAD_CHARACTER;

%%

%class _PellLexer
%implements FlexLexer
%function advance
%type IElementType
%unicode
%public

%{
    // Did the *previous* significant (non-whitespace, non-comment) token
    // produce a value? Used to decide between `/` (division) and `/.../`
    // (regex literal). Mirrors compiler/pell/lexer.py _VALUE_TOKEN_KINDS.
    private boolean lastWasValue = false;

    /** Mark a value-producing token. */
    private IElementType value(IElementType t) {
        lastWasValue = true;
        return t;
    }

    /** Mark a non-value-producing token (operators, keywords, etc.). */
    private IElementType nonValue(IElementType t) {
        lastWasValue = false;
        return t;
    }

    /**
     * Consume a /.../ regex literal starting from the character just past
     * the opening `/`. Updates zzMarkedPos to point past the closing `/`.
     * Returns REGEX or BAD_CHARACTER for unterminated/multi-line patterns.
     *
     * Mirrors Lexer._read_regex in compiler/pell/lexer.py exactly:
     *   - `\\` consumes the next char (escape, including `\/`)
     *   - bare `/` ends the literal
     *   - newline → unterminated → BAD_CHARACTER (Python raises LexError)
     *   - trailing flag chars (i/m/s/x) → BAD_CHARACTER (not yet supported)
     */
    private IElementType readRegex() {
        // zzMarkedPos already points one past the opening `/` because that
        // single char was matched. Scan body.
        while (zzMarkedPos < zzEndRead) {
            char c = zzBuffer.charAt(zzMarkedPos);
            if (c == '\\') {
                zzMarkedPos++;
                if (zzMarkedPos < zzEndRead) zzMarkedPos++;
                continue;
            }
            if (c == '/') {
                zzMarkedPos++;  // consume closing /
                // No flags supported yet — bail on trailing i/m/s/x.
                if (zzMarkedPos < zzEndRead) {
                    char flag = zzBuffer.charAt(zzMarkedPos);
                    if (flag == 'i' || flag == 'm' || flag == 's' || flag == 'x') {
                        return BAD_CHARACTER;
                    }
                }
                return REGEX;
            }
            if (c == '\n' || c == '\r') {
                // Unterminated — pell regex literals are single-line.
                return BAD_CHARACTER;
            }
            zzMarkedPos++;
        }
        // Hit EOF without closing.
        return BAD_CHARACTER;
    }

    /**
     * Consume a sql!{…} or jq!{…} block starting from just past the
     * opening `{` (the prefix + optional whitespace + `{` was already
     * matched). Tracks brace depth so braces inside SQL string literals
     * don't terminate the block prematurely. Mirrors
     * Lexer._read_macro_block in compiler/pell/lexer.py.
     */
    private IElementType readMacroBlock(IElementType kind) {
        int depth = 1;
        while (zzMarkedPos < zzEndRead && depth > 0) {
            char c = zzBuffer.charAt(zzMarkedPos);
            if (c == '{') {
                depth++;
                zzMarkedPos++;
                continue;
            }
            if (c == '}') {
                depth--;
                if (depth == 0) {
                    zzMarkedPos++;
                    return kind;
                }
                zzMarkedPos++;
                continue;
            }
            if (c == '\'') {
                // SQL single-quoted string. Inside, '\' is escape; '' is also
                // escape for ' in SQL but lexer.py doesn't special-case it
                // (the '\\' escape covers both styles).
                zzMarkedPos++;
                while (zzMarkedPos < zzEndRead) {
                    char d = zzBuffer.charAt(zzMarkedPos);
                    if (d == '\\') {
                        zzMarkedPos++;
                        if (zzMarkedPos < zzEndRead) zzMarkedPos++;
                        continue;
                    }
                    if (d == '\'') {
                        zzMarkedPos++;
                        break;
                    }
                    zzMarkedPos++;
                }
                continue;
            }
            if (c == '"') {
                // SQL double-quoted (quoted identifier) — no escapes.
                zzMarkedPos++;
                while (zzMarkedPos < zzEndRead && zzBuffer.charAt(zzMarkedPos) != '"') {
                    zzMarkedPos++;
                }
                if (zzMarkedPos < zzEndRead) zzMarkedPos++;
                continue;
            }
            if (c == '-' && zzMarkedPos + 1 < zzEndRead && zzBuffer.charAt(zzMarkedPos + 1) == '-') {
                // SQL line comment.
                while (zzMarkedPos < zzEndRead && zzBuffer.charAt(zzMarkedPos) != '\n') {
                    zzMarkedPos++;
                }
                continue;
            }
            zzMarkedPos++;
        }
        // Unterminated.
        return BAD_CHARACTER;
    }

    /**
     * Consume a "..." string literal starting from just past the opening
     * `"`. JFlex match already includes the opening quote. We scan to the
     * closing quote, respecting backslash escapes (which can include `\"`
     * to embed a literal quote).
     *
     * For Phase 1 we emit a single STRING token whose text spans the full
     * literal including quotes. Phase 2's parser sees the raw token; the
     * lowering pass handles `\n` etc. The {name} interpolation syntax is
     * NOT special-cased here — it's part of the string content.
     */
    private IElementType readDoubleQuotedString() {
        while (zzMarkedPos < zzEndRead) {
            char c = zzBuffer.charAt(zzMarkedPos);
            if (c == '\\') {
                zzMarkedPos++;
                if (zzMarkedPos < zzEndRead) zzMarkedPos++;
                continue;
            }
            if (c == '"') {
                zzMarkedPos++;
                return STRING;
            }
            if (c == '\n') {
                // pell strings are single-line — match Python lexer error.
                return BAD_CHARACTER;
            }
            zzMarkedPos++;
        }
        return BAD_CHARACTER;
    }

    /** Backtick raw string — everything until next backtick. */
    private IElementType readRawString() {
        while (zzMarkedPos < zzEndRead) {
            char c = zzBuffer.charAt(zzMarkedPos);
            if (c == '`') {
                zzMarkedPos++;
                return RAWSTRING;
            }
            zzMarkedPos++;
        }
        return BAD_CHARACTER;
    }
%}

EOL         = \r|\n|\r\n
WHITESPACE  = ({EOL}|[ \t\f])+
LINE_BODY   = "//" [^\r\n]*
BLOCK_BODY  = "/*" ~"*/"

IDENT_START = [A-Za-z_]
IDENT_CONT  = [A-Za-z0-9_]
IDENT_BODY  = {IDENT_START}{IDENT_CONT}*

DIGIT       = [0-9]
NUMBER_BODY = {DIGIT}+ ("." {DIGIT}+)?

// Optional inter-token whitespace allowed between `sql!` and `{`.
INLINE_WS   = [ \t\r\n]*

%%

<YYINITIAL> {

  // ---- trivia (skipped by parser via parserDefinition.getCommentTokens) -
  {WHITESPACE}    { return WHITE_SPACE; }
  {LINE_BODY}     { return LINE_COMMENT; }
  {BLOCK_BODY}    { return BLOCK_COMMENT; }

  // ---- sql!{…} / jq!{…} — match prefix-then-brace, scan body in helper -
  "sql!" {INLINE_WS} "{"  { return value(readMacroBlock(SQL_BLOCK)); }
  "jq!"  {INLINE_WS} "{"  { return value(readMacroBlock(JQ_BLOCK));  }

  // ---- keywords (must precede IDENT_BODY rule; JFlex picks first on tie) -
  // Value-producing literal-keywords: true, false, null, self
  "true"        { return value(KW_TRUE); }
  "false"       { return value(KW_FALSE); }
  "null"        { return value(KW_NULL); }
  "self"        { return value(KW_SELF); }
  // Non-value keywords — operator/control/declarator-flavoured.
  "module"      { return nonValue(KW_MODULE); }
  "import"      { return nonValue(KW_IMPORT); }
  "pub"         { return nonValue(KW_PUB); }
  "fn"          { return nonValue(KW_FN); }
  "let"         { return nonValue(KW_LET); }
  "var"         { return nonValue(KW_VAR); }
  "return"      { return nonValue(KW_RETURN); }
  "yield"       { return nonValue(KW_YIELD); }
  "if"          { return nonValue(KW_IF); }
  "else"        { return nonValue(KW_ELSE); }
  "for"         { return nonValue(KW_FOR); }
  "forall"      { return nonValue(KW_FORALL); }
  "in"          { return nonValue(KW_IN); }
  "match"       { return nonValue(KW_MATCH); }
  "transaction" { return nonValue(KW_TRANSACTION); }
  "record"      { return nonValue(KW_RECORD); }
  "error"       { return nonValue(KW_ERROR); }
  "Some"        { return nonValue(KW_SOME); }
  "None"        { return nonValue(KW_NONE); }
  "Ok"          { return nonValue(KW_OK); }
  "Err"         { return nonValue(KW_ERR); }
  "unsafe"      { return nonValue(KW_UNSAFE); }
  "finally"     { return nonValue(KW_FINALLY); }
  "and"         { return nonValue(KW_AND); }
  "or"          { return nonValue(KW_OR); }
  "not"         { return nonValue(KW_NOT); }
  "type"        { return nonValue(KW_TYPE); }
  "sealed"      { return nonValue(KW_SEALED); }
  "aggregate"   { return nonValue(KW_AGGREGATE); }
  "case"        { return nonValue(KW_CASE); }
  "seq"         { return nonValue(KW_SEQ); }
  "out"         { return nonValue(KW_OUT); }
  "inout"       { return nonValue(KW_INOUT); }
  "enum"        { return nonValue(KW_ENUM); }

  // ---- identifiers + numeric literals + string literals -----------------
  {IDENT_BODY}    { return value(IDENT); }
  {NUMBER_BODY}   { return value(NUMBER); }
  \"              { return value(readDoubleQuotedString()); }
  `               { return value(readRawString()); }

  // ---- multi-char operators (must precede single-char) ------------------
  "..="         { return nonValue(DOTDOTEQ); }
  ".."          { return nonValue(DOTDOT); }
  "::"          { return nonValue(COLONCOLON); }
  "->"          { return nonValue(ARROW); }
  "=>"          { return nonValue(FATARROW); }
  "=="          { return nonValue(EQEQ); }
  "!="          { return nonValue(BANGEQ); }
  "<="          { return nonValue(LE); }
  ">="          { return nonValue(GE); }
  "&&"          { return nonValue(AMPAMP); }
  "||"          { return nonValue(PIPEPIPE); }
  "|>"          { return nonValue(PIPEGT); }

  // ---- `/` is special: division vs regex literal --------------------------
  // `/` not followed by `/` or `*` (those are comments, handled earlier).
  "/" {
      if (lastWasValue) {
          // Division operator.
          return nonValue(SLASH);
      }
      return value(readRegex());
  }

  // ---- single-char punctuation (value-producing brackets are separate) --
  // Value brackets (closing): emit value()-tagged so a following `/` is
  // treated as division. Mirrors _VALUE_TOKEN_KINDS in lexer.py.
  ")"           { return value(RPAREN); }
  "]"           { return value(RBRACKET); }
  "}"           { return value(RBRACE); }
  // Non-value openers + the rest.
  "("           { return nonValue(LPAREN); }
  "{"           { return nonValue(LBRACE); }
  "["           { return nonValue(LBRACKET); }
  ";"           { return nonValue(SEMI); }
  ","           { return nonValue(COMMA); }
  ":"           { return nonValue(COLON); }
  "."           { return nonValue(DOT); }
  "@"           { return nonValue(AT); }
  "?"           { return nonValue(QUESTION); }
  "|"           { return nonValue(PIPE); }
  "&"           { return nonValue(AMP); }
  "!"           { return nonValue(BANG); }
  "+"           { return nonValue(PLUS); }
  "-"           { return nonValue(MINUS); }
  "*"           { return nonValue(STAR); }
  "%"           { return nonValue(PERCENT); }
  "<"           { return nonValue(LT); }
  ">"           { return nonValue(GT); }
  "="           { return nonValue(EQ); }

  // ---- fallthrough: unexpected character --------------------------------
  .             { return BAD_CHARACTER; }
}
