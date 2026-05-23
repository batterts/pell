-- ============================================================================
-- pell_re — PCRE-ish regex engine for Oracle PL/SQL.
--
-- Why this exists: Oracle's regexp_* functions are POSIX-ish, with no
-- shorthand classes (\d \w \s), no named groups, and capture-group
-- semantics that get awkward fast. pell_re is a Thompson-NFA matcher
-- (linear time, no catastrophic backtracking) written in pure PL/SQL,
-- so it runs identically on 19c, 23ai, Autonomous, and RDS.
--
-- v0 supports: literal chars, . * + ? {n,m} {n} {n,} | (...) (?:...)
-- character classes [a-z] [^a-z] with shorthands \d \D \w \W \s \S,
-- anchors ^ $ \A \z \b \B. Captures present but extraction is in v1.
-- Excluded by design: backreferences, lookbehinds (would need a
-- backtracking matcher), Unicode property classes (\p{L}), possessive
-- quantifiers.
--
-- Bytecode layout in a RAW(32767):
--     [program]               instructions, address 0..N
--     [class_pool]             character class records, address N..end
--
-- Opcodes (1-byte tag, then operands; multi-byte fields are big-endian):
--     01  MATCH                                        size 1   accept
--     02  CHAR  cp:u32                                  size 5   match codepoint
--     03  ANY                                          size 1   . (no \n)
--     04  ANYD                                         size 1   . (dotall)
--     05  CLS   pool_offset:u16                        size 3   character class
--     06  JMP   addr:u16                               size 3   goto
--     07  SPLIT addr1:u16 addr2:u16                    size 5   NFA fork (prefer addr1)
--     08  SAVE  slot:u8                                size 2   capture position
--     09  BOL                                          size 1   ^
--     0A  EOL                                          size 1   $
--     0B  BOS                                          size 1   \A
--     0C  EOS                                          size 1   \z
--     0D  WB                                           size 1   \b
--     0E  NWB                                          size 1   \B
--
-- Class record (in pool):
--     negated:u8 | range_count:u16 | (start_cp:u32, end_cp:u32) * range_count
-- ============================================================================

CREATE OR REPLACE PACKAGE pell_re AS
  TYPE t_text_list    IS TABLE OF VARCHAR2(4000) INDEX BY PLS_INTEGER;
  TYPE t_int_list     IS TABLE OF PLS_INTEGER    INDEX BY PLS_INTEGER;

  -- Capture record — one per group, including group 0 (the whole match).
  -- start_pos / end_pos are 1-based codepoint positions; end_pos is
  -- exclusive (matches Python re's group span semantics). If a group
  -- never participated (e.g. inside an unmatched alternation branch),
  -- start_pos and end_pos are NULL and match_text is NULL.
  TYPE t_capture IS RECORD (
    start_pos  PLS_INTEGER,
    end_pos    PLS_INTEGER,
    match_text VARCHAR2(4000)
  );
  TYPE t_capture_list IS TABLE OF t_capture INDEX BY PLS_INTEGER;
  TYPE t_capture_map  IS TABLE OF t_capture INDEX BY VARCHAR2(64);

  -- Boolean test: does any substring of `p_s` match the full pattern?
  -- (Equivalent to PCRE without anchors — patterns matching anywhere.)
  FUNCTION matches(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN BOOLEAN;

  -- Return the substring of the first match starting at or after p_pos;
  -- NULL if no match.
  FUNCTION find(p_s   IN VARCHAR2,
                p_pat IN VARCHAR2,
                p_pos IN PLS_INTEGER DEFAULT 1) RETURN VARCHAR2;

  -- All non-overlapping matches, left to right.
  FUNCTION find_all(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN t_text_list;

  -- Return the [start, end] codepoint positions of the first match, as a
  -- 2-element t_int_list (positions are 1-based, end-exclusive).
  -- Returns an empty list if no match.
  FUNCTION find_span(p_s   IN VARCHAR2,
                     p_pat IN VARCHAR2,
                     p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_int_list;

  -- Return the capture spans for the first match. Index 0 is always the
  -- whole match; indices 1..N are the parenthesized groups in source
  -- order (capturing groups only — `(?:...)` is skipped). Returns an
  -- empty list when there is no match.
  FUNCTION capture(p_s   IN VARCHAR2,
                   p_pat IN VARCHAR2,
                   p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_capture_list;

  -- Return only the named-group captures of the first match, keyed by
  -- name. Names without a successful match are absent from the map.
  FUNCTION capture_by_name(p_s   IN VARCHAR2,
                           p_pat IN VARCHAR2,
                           p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_capture_map;

  -- Replace all non-overlapping matches with p_rep (literal — no $1
  -- backreferences in v0).
  FUNCTION replace_all(p_s   IN VARCHAR2,
                       p_pat IN VARCHAR2,
                       p_rep IN VARCHAR2) RETURN VARCHAR2;

  -- Split p_s at every match of p_pat. Empty separator matches and
  -- leading/trailing matches produce empty fields, matching PCRE's split
  -- semantics.
  FUNCTION split(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN t_text_list;

  -- Drop the per-session compiled-pattern cache. Useful for tests and
  -- after large pattern churn (the cache is unbounded).
  PROCEDURE clear_cache;
END pell_re;
/

CREATE OR REPLACE PACKAGE BODY pell_re AS

  -- -------------------------------------------------------------------------
  -- Opcodes
  -- -------------------------------------------------------------------------
  C_OP_MATCH CONSTANT PLS_INTEGER := 1;
  C_OP_CHAR  CONSTANT PLS_INTEGER := 2;
  C_OP_ANY   CONSTANT PLS_INTEGER := 3;
  C_OP_ANYD  CONSTANT PLS_INTEGER := 4;
  C_OP_CLS   CONSTANT PLS_INTEGER := 5;
  C_OP_JMP   CONSTANT PLS_INTEGER := 6;
  C_OP_SPLIT CONSTANT PLS_INTEGER := 7;
  C_OP_SAVE  CONSTANT PLS_INTEGER := 8;
  C_OP_BOL   CONSTANT PLS_INTEGER := 9;
  C_OP_EOL   CONSTANT PLS_INTEGER := 10;
  C_OP_BOS   CONSTANT PLS_INTEGER := 11;
  C_OP_EOS   CONSTANT PLS_INTEGER := 12;
  C_OP_WB    CONSTANT PLS_INTEGER := 13;
  C_OP_NWB   CONSTANT PLS_INTEGER := 14;

  C_MAX_PROG CONSTANT PLS_INTEGER := 32767;
  C_MAX_THREADS CONSTANT PLS_INTEGER := 4096;

  -- -------------------------------------------------------------------------
  -- Compiled pattern
  -- -------------------------------------------------------------------------
  -- name → group index (1-based: group 0 is the whole match, group 1
  -- is the first capturing group, etc.).
  TYPE t_name_to_idx IS TABLE OF PLS_INTEGER INDEX BY VARCHAR2(64);

  TYPE t_compiled IS RECORD (
    prog        RAW(32767),
    prog_size   PLS_INTEGER,
    pool_start  PLS_INTEGER,
    group_count PLS_INTEGER,
    group_names t_name_to_idx
  );

  -- A thread in Pike's NFA simulator carries its program counter plus
  -- a capture array. Capture array layout: slot 0 = group 0 start,
  -- slot 1 = group 0 end, slot 2 = group 1 start, etc.
  TYPE t_thread IS RECORD (
    pc   PLS_INTEGER,
    caps t_int_list
  );
  TYPE t_thread_list IS TABLE OF t_thread INDEX BY PLS_INTEGER;

  TYPE t_cache_map IS TABLE OF t_compiled INDEX BY VARCHAR2(64);
  g_cache t_cache_map;

  -- Builder state, scoped per-compile (held at package level so the
  -- parser's nested helpers can mutate it without passing it around).
  g_buf       RAW(32767);
  g_buf_size  PLS_INTEGER;
  g_pool_buf  RAW(32767);
  g_pool_size PLS_INTEGER;
  g_pat         VARCHAR2(4000);
  g_pos         PLS_INTEGER;
  g_groups      PLS_INTEGER;
  -- Names of capturing groups as seen, indexed by group number (1-based).
  g_group_names t_name_to_idx;

  -- -------------------------------------------------------------------------
  -- Codepoint helpers
  -- -------------------------------------------------------------------------

  -- Codepoint of a one-character pell string. BMP only in v0 — characters
  -- above U+FFFF return their UTF-16 surrogate-pair *first* value, which
  -- is good enough for v0 since the matcher uses the same encoding for
  -- both pattern and input.
  FUNCTION codepoint_of(p_ch IN VARCHAR2) RETURN PLS_INTEGER IS
    r RAW(4);
  BEGIN
    IF p_ch IS NULL THEN RETURN NULL; END IF;
    r := UTL_I18N.STRING_TO_RAW(p_ch, 'AL16UTF16');
    RETURN UTL_RAW.CAST_TO_BINARY_INTEGER(UTL_RAW.SUBSTR(r, 1, 2));
  END;

  -- Pack/unpack big-endian integers in/out of the bytecode RAW.
  FUNCTION u8(p IN PLS_INTEGER) RETURN RAW IS
  BEGIN
    RETURN UTL_RAW.CAST_FROM_BINARY_INTEGER(p, UTL_RAW.BIG_ENDIAN);
  END;

  FUNCTION pack_u8(p IN PLS_INTEGER) RETURN RAW IS
    r RAW(4);
  BEGIN
    r := UTL_RAW.CAST_FROM_BINARY_INTEGER(p, UTL_RAW.BIG_ENDIAN);
    -- CAST_FROM_BINARY_INTEGER produces 4 bytes; we want just the low byte.
    RETURN UTL_RAW.SUBSTR(r, 4, 1);
  END;

  FUNCTION pack_u16(p IN PLS_INTEGER) RETURN RAW IS
    r RAW(4);
  BEGIN
    r := UTL_RAW.CAST_FROM_BINARY_INTEGER(p, UTL_RAW.BIG_ENDIAN);
    RETURN UTL_RAW.SUBSTR(r, 3, 2);  -- low 16 bits
  END;

  FUNCTION pack_u32(p IN PLS_INTEGER) RETURN RAW IS
  BEGIN
    RETURN UTL_RAW.CAST_FROM_BINARY_INTEGER(p, UTL_RAW.BIG_ENDIAN);
  END;

  FUNCTION read_u8(p_prog IN RAW, p_pos IN PLS_INTEGER) RETURN PLS_INTEGER IS
  BEGIN
    RETURN UTL_RAW.CAST_TO_BINARY_INTEGER(UTL_RAW.SUBSTR(p_prog, p_pos, 1));
  END;

  FUNCTION read_u16(p_prog IN RAW, p_pos IN PLS_INTEGER) RETURN PLS_INTEGER IS
  BEGIN
    RETURN UTL_RAW.CAST_TO_BINARY_INTEGER(UTL_RAW.SUBSTR(p_prog, p_pos, 2));
  END;

  FUNCTION read_u32(p_prog IN RAW, p_pos IN PLS_INTEGER) RETURN PLS_INTEGER IS
  BEGIN
    RETURN UTL_RAW.CAST_TO_BINARY_INTEGER(UTL_RAW.SUBSTR(p_prog, p_pos, 4));
  END;

  -- -------------------------------------------------------------------------
  -- Parser — pattern string → "Fragment" of bytecode.
  --
  -- The parser walks the pattern using a single position cursor and emits
  -- bytecode directly into `g_buf`, tracking a small AST in flight only as
  -- needed for grouping. Quantifiers operate on the *previous atom*; the
  -- compiler remembers each atom's start address so it can wrap it in
  -- SPLIT/JMP scaffolding.
  -- -------------------------------------------------------------------------

  PROCEDURE emit(p_bytes IN RAW) IS
  BEGIN
    IF g_buf IS NULL OR g_buf_size = 0 THEN
      g_buf := p_bytes;
    ELSE
      g_buf := UTL_RAW.CONCAT(g_buf, p_bytes);
    END IF;
    g_buf_size := g_buf_size + UTL_RAW.LENGTH(p_bytes);
  END;

  -- Emit a u16 placeholder; return its byte offset so the caller can
  -- patch it later when the target address is known.
  FUNCTION emit_placeholder_u16 RETURN PLS_INTEGER IS
    pos PLS_INTEGER;
  BEGIN
    pos := g_buf_size + 1;
    emit(pack_u16(0));
    RETURN pos;
  END;

  -- Overwrite a previously-emitted u16 at offset `p_at` with `p_val`.
  PROCEDURE patch_u16(p_at IN PLS_INTEGER, p_val IN PLS_INTEGER) IS
    before RAW(32767);
    after  RAW(32767);
  BEGIN
    IF p_at > 1 THEN
      before := UTL_RAW.SUBSTR(g_buf, 1, p_at - 1);
    END IF;
    IF p_at + 2 <= g_buf_size THEN
      after := UTL_RAW.SUBSTR(g_buf, p_at + 2);
    END IF;
    IF before IS NULL THEN
      g_buf := UTL_RAW.CONCAT(pack_u16(p_val), after);
    ELSIF after IS NULL THEN
      g_buf := UTL_RAW.CONCAT(before, pack_u16(p_val));
    ELSE
      g_buf := UTL_RAW.CONCAT(UTL_RAW.CONCAT(before, pack_u16(p_val)), after);
    END IF;
  END;

  FUNCTION peek_ch RETURN VARCHAR2 IS
  BEGIN
    IF g_pos > LENGTHC(g_pat) THEN RETURN NULL; END IF;
    RETURN SUBSTRC(g_pat, g_pos, 1);
  END;

  PROCEDURE advance IS
  BEGIN
    g_pos := g_pos + 1;
  END;

  FUNCTION eof RETURN BOOLEAN IS
  BEGIN
    RETURN g_pos > LENGTHC(g_pat);
  END;

  PROCEDURE die(p_msg IN VARCHAR2) IS
  BEGIN
    raise_application_error(-20800,
      'pell_re: ' || p_msg || ' at position ' || g_pos || ' in ' || g_pat);
  END;

  -- -------------------------------------------------------------------------
  -- Character class compilation
  -- -------------------------------------------------------------------------

  -- Append a class record to the class pool, return its pool offset.
  FUNCTION emit_class(p_negated IN BOOLEAN,
                      p_ranges_lo IN t_int_list,
                      p_ranges_hi IN t_int_list) RETURN PLS_INTEGER IS
    offset PLS_INTEGER;
    n      PLS_INTEGER := p_ranges_lo.COUNT;
    chunk  RAW(32767);
  BEGIN
    offset := g_pool_size + 1;
    chunk := pack_u8(CASE WHEN p_negated THEN 1 ELSE 0 END);
    chunk := UTL_RAW.CONCAT(chunk, pack_u16(n));
    FOR i IN 1 .. n LOOP
      chunk := UTL_RAW.CONCAT(chunk,
                              UTL_RAW.CONCAT(pack_u32(p_ranges_lo(i)),
                                             pack_u32(p_ranges_hi(i))));
    END LOOP;
    IF g_pool_buf IS NULL OR g_pool_size = 0 THEN
      g_pool_buf := chunk;
    ELSE
      g_pool_buf := UTL_RAW.CONCAT(g_pool_buf, chunk);
    END IF;
    g_pool_size := g_pool_size + UTL_RAW.LENGTH(chunk);
    RETURN offset;
  END;

  -- Build the canonical class for one of \d \D \w \W \s \S.
  PROCEDURE shorthand_ranges(p_ch  IN VARCHAR2,
                             p_lo OUT t_int_list,
                             p_hi OUT t_int_list,
                             p_neg OUT BOOLEAN) IS
    lo t_int_list;
    hi t_int_list;
  BEGIN
    p_neg := FALSE;
    IF p_ch = 'd' THEN
      lo(1) := 48; hi(1) := 57;  -- 0-9
    ELSIF p_ch = 'D' THEN
      lo(1) := 48; hi(1) := 57;
      p_neg := TRUE;
    ELSIF p_ch = 'w' THEN
      lo(1) := 48; hi(1) := 57;
      lo(2) := 65; hi(2) := 90;
      lo(3) := 97; hi(3) := 122;
      lo(4) := 95; hi(4) := 95;  -- _
    ELSIF p_ch = 'W' THEN
      lo(1) := 48; hi(1) := 57;
      lo(2) := 65; hi(2) := 90;
      lo(3) := 97; hi(3) := 122;
      lo(4) := 95; hi(4) := 95;
      p_neg := TRUE;
    ELSIF p_ch = 's' THEN
      lo(1) := 9;  hi(1) := 13;   -- \t \n \v \f \r
      lo(2) := 32; hi(2) := 32;   -- space
    ELSIF p_ch = 'S' THEN
      lo(1) := 9;  hi(1) := 13;
      lo(2) := 32; hi(2) := 32;
      p_neg := TRUE;
    ELSE
      raise_application_error(-20800,
        'pell_re: unknown shorthand class \' || p_ch);
    END IF;
    p_lo := lo;
    p_hi := hi;
  END;

  -- Parse one escape sequence inside a character class. Returns the
  -- codepoint of the resulting char, or -1 if it was a shorthand class
  -- (in which case p_lo/p_hi/p_neg are populated and the caller appends
  -- them to its own range list).
  FUNCTION parse_escape_in_class(p_lo OUT t_int_list,
                                 p_hi OUT t_int_list,
                                 p_neg OUT BOOLEAN) RETURN PLS_INTEGER IS
    ch VARCHAR2(4);
  BEGIN
    IF eof THEN die('trailing backslash'); END IF;
    ch := peek_ch; advance;
    IF ch IN ('d','D','w','W','s','S') THEN
      shorthand_ranges(ch, p_lo, p_hi, p_neg);
      RETURN -1;
    END IF;
    p_neg := FALSE;
    -- Common escapes
    CASE ch
      WHEN 'n' THEN RETURN 10;
      WHEN 'r' THEN RETURN 13;
      WHEN 't' THEN RETURN 9;
      WHEN 'f' THEN RETURN 12;
      WHEN 'v' THEN RETURN 11;
      WHEN '0' THEN RETURN 0;
      ELSE RETURN codepoint_of(ch);  -- literal escaped char
    END CASE;
  END;

  -- Parse a `[...]` character class starting just *after* the opening `[`.
  -- Emits a CLS opcode referencing the pool entry.
  PROCEDURE compile_class IS
    negated   BOOLEAN := FALSE;
    lo        t_int_list;
    hi        t_int_list;
    n         PLS_INTEGER := 0;
    ch        VARCHAR2(4);
    cp        PLS_INTEGER;
    sublo     t_int_list;
    subhi     t_int_list;
    subneg    BOOLEAN;
    pool_off  PLS_INTEGER;
  BEGIN
    IF peek_ch = '^' THEN
      negated := TRUE;
      advance;
    END IF;
    LOOP
      IF eof THEN die('unterminated [...] class'); END IF;
      ch := peek_ch;
      IF ch = ']' AND n > 0 THEN
        advance;
        EXIT;
      END IF;
      advance;
      IF ch = '\' THEN
        cp := parse_escape_in_class(sublo, subhi, subneg);
        IF cp = -1 THEN
          -- Shorthand class — merge its ranges in
          IF subneg THEN
            -- We don't support nested negation in a class — die for now
            die('cannot use a negated shorthand inside a [...] class');
          END IF;
          FOR i IN 1 .. sublo.COUNT LOOP
            n := n + 1;
            lo(n) := sublo(i);
            hi(n) := subhi(i);
          END LOOP;
          CONTINUE;
        END IF;
      ELSE
        cp := codepoint_of(ch);
      END IF;
      -- Range `a-z`? Look for `-` followed by a non-`]` char.
      IF peek_ch = '-' AND g_pos + 1 <= LENGTHC(g_pat)
         AND SUBSTRC(g_pat, g_pos + 1, 1) != ']' THEN
        advance;  -- consume `-`
        DECLARE
          ch2 VARCHAR2(4) := peek_ch;
          cp2 PLS_INTEGER;
        BEGIN
          advance;
          IF ch2 = '\' THEN
            cp2 := parse_escape_in_class(sublo, subhi, subneg);
            IF cp2 = -1 THEN
              die('shorthand class as range endpoint not supported');
            END IF;
          ELSE
            cp2 := codepoint_of(ch2);
          END IF;
          IF cp2 < cp THEN die('inverted range in class'); END IF;
          n := n + 1;
          lo(n) := cp;
          hi(n) := cp2;
        END;
      ELSE
        n := n + 1;
        lo(n) := cp;
        hi(n) := cp;
      END IF;
    END LOOP;
    pool_off := emit_class(negated, lo, hi);
    emit(pack_u8(C_OP_CLS));
    emit(pack_u16(pool_off));
  END;

  -- -------------------------------------------------------------------------
  -- Atom / repetition / sequence / alternation
  --
  -- compile_atom returns the start address of the atom in g_buf so the
  -- quantifier wrapper can re-emit / reference it.
  -- -------------------------------------------------------------------------

  FUNCTION compile_alt RETURN PLS_INTEGER;  -- fwd decl

  FUNCTION compile_atom RETURN PLS_INTEGER IS
    start_addr PLS_INTEGER;
    ch         VARCHAR2(4);
    cp         PLS_INTEGER;
    sublo      t_int_list;
    subhi      t_int_list;
    subneg     BOOLEAN;
    pool_off   PLS_INTEGER;
    dummy      PLS_INTEGER;
  BEGIN
    start_addr := g_buf_size + 1;
    ch := peek_ch;
    IF ch = '(' THEN
      advance;
      -- `(?:...)` non-capturing — consume `?:` and recurse
      IF peek_ch = '?' AND g_pos + 1 <= LENGTHC(g_pat)
         AND SUBSTRC(g_pat, g_pos + 1, 1) = ':' THEN
        advance; advance;
      -- `(?<name>...)` named capture — extract name, capture
      ELSIF peek_ch = '?' AND g_pos + 1 <= LENGTHC(g_pat)
            AND SUBSTRC(g_pat, g_pos + 1, 1) = '<' THEN
        advance; advance;  -- consume `?<`
        DECLARE
          nm VARCHAR2(64) := '';
        BEGIN
          WHILE NOT eof AND peek_ch != '>' LOOP
            IF NOT (peek_ch BETWEEN 'a' AND 'z' OR peek_ch BETWEEN 'A' AND 'Z'
                    OR peek_ch BETWEEN '0' AND '9' OR peek_ch = '_') THEN
              die('invalid character in group name');
            END IF;
            nm := nm || peek_ch;
            advance;
          END LOOP;
          IF nm IS NULL OR LENGTHC(nm) = 0 THEN die('empty group name'); END IF;
          IF peek_ch != '>' THEN die('expected `>` after group name'); END IF;
          advance;
          g_groups := g_groups + 1;
          g_group_names(nm) := g_groups;
          emit(pack_u8(C_OP_SAVE));
          emit(pack_u8(g_groups * 2));   -- slot for group N start
        END;
      ELSE
        -- Plain `(...)` capturing group — emit SAVE start
        g_groups := g_groups + 1;
        emit(pack_u8(C_OP_SAVE));
        emit(pack_u8(g_groups * 2));     -- slot for group N start
      END IF;
      dummy := compile_alt;
      IF peek_ch != ')' THEN die('expected `)`'); END IF;
      advance;
      -- Emit SAVE end if the group was capturing. Simplest: track via
      -- presence of the SAVE start that we'd just emitted; but the
      -- non-capturing path also doesn't push a SAVE. We rely on the
      -- group_idx — if we incremented g_groups before recursing, this
      -- was a capture. Re-check by comparing g_groups *now* with what
      -- we'd have without — too fragile; simpler: peek at the byte at
      -- start_addr to see if it's a SAVE.
      IF read_u8(g_buf, start_addr) = C_OP_SAVE THEN
        emit(pack_u8(C_OP_SAVE));
        emit(pack_u8(read_u8(g_buf, start_addr + 1) + 1));
      END IF;
      RETURN start_addr;
    ELSIF ch = '[' THEN
      advance;
      compile_class;
      RETURN start_addr;
    ELSIF ch = '.' THEN
      advance;
      emit(pack_u8(C_OP_ANY));
      RETURN start_addr;
    ELSIF ch = '^' THEN
      advance;
      emit(pack_u8(C_OP_BOL));
      RETURN start_addr;
    ELSIF ch = '$' THEN
      advance;
      emit(pack_u8(C_OP_EOL));
      RETURN start_addr;
    ELSIF ch = '\' THEN
      advance;
      DECLARE
        ec VARCHAR2(4) := peek_ch;
      BEGIN
        IF ec IS NULL THEN die('trailing backslash'); END IF;
        advance;
        IF ec IN ('d','D','w','W','s','S') THEN
          shorthand_ranges(ec, sublo, subhi, subneg);
          pool_off := emit_class(subneg, sublo, subhi);
          emit(pack_u8(C_OP_CLS));
          emit(pack_u16(pool_off));
        ELSIF ec = 'A' THEN
          emit(pack_u8(C_OP_BOS));
        ELSIF ec = 'z' THEN
          emit(pack_u8(C_OP_EOS));
        ELSIF ec = 'b' THEN
          emit(pack_u8(C_OP_WB));
        ELSIF ec = 'B' THEN
          emit(pack_u8(C_OP_NWB));
        ELSIF ec = 'n' THEN
          emit(pack_u8(C_OP_CHAR));
          emit(pack_u32(10));
        ELSIF ec = 'r' THEN
          emit(pack_u8(C_OP_CHAR));
          emit(pack_u32(13));
        ELSIF ec = 't' THEN
          emit(pack_u8(C_OP_CHAR));
          emit(pack_u32(9));
        ELSE
          emit(pack_u8(C_OP_CHAR));
          emit(pack_u32(codepoint_of(ec)));
        END IF;
      END;
      RETURN start_addr;
    ELSE
      -- literal
      advance;
      emit(pack_u8(C_OP_CHAR));
      emit(pack_u32(codepoint_of(ch)));
      RETURN start_addr;
    END IF;
  END;

  -- Wrap the just-emitted atom with the quantifier scaffolding.
  PROCEDURE apply_quantifier(p_atom_start IN PLS_INTEGER) IS
    ch          VARCHAR2(4) := peek_ch;
    greedy      BOOLEAN     := TRUE;
    atom_size   PLS_INTEGER;
    atom_bytes  RAW(32767);
    split_addr  PLS_INTEGER;
    jmp_addr    PLS_INTEGER;
    s1_off      PLS_INTEGER;
    s2_off      PLS_INTEGER;
    j_off       PLS_INTEGER;
    min_n       PLS_INTEGER;
    max_n       PLS_INTEGER;
    has_max     BOOLEAN;
    digits      VARCHAR2(20);
  BEGIN
    IF ch NOT IN ('*', '+', '?', '{') THEN RETURN; END IF;

    -- Capture the atom's bytecode (we may need to replicate it for {n,m})
    atom_size  := g_buf_size - p_atom_start + 1;
    atom_bytes := UTL_RAW.SUBSTR(g_buf, p_atom_start, atom_size);

    -- {n,m} parse
    IF ch = '{' THEN
      advance;
      digits := '';
      WHILE NOT eof AND peek_ch BETWEEN '0' AND '9' LOOP
        digits := digits || peek_ch; advance;
      END LOOP;
      IF digits IS NULL THEN die('expected number after `{`'); END IF;
      min_n := TO_NUMBER(digits);
      max_n := min_n;
      has_max := TRUE;
      IF peek_ch = ',' THEN
        advance;
        digits := '';
        WHILE NOT eof AND peek_ch BETWEEN '0' AND '9' LOOP
          digits := digits || peek_ch; advance;
        END LOOP;
        IF digits IS NULL THEN
          has_max := FALSE;
        ELSE
          max_n := TO_NUMBER(digits);
        END IF;
      END IF;
      IF peek_ch != '}' THEN die('expected `}`'); END IF;
      advance;

      -- Strip the just-emitted atom (we'll re-emit it min_n times, then
      -- max_n - min_n optional times). UTL_RAW.SUBSTR errors on len=0,
      -- so handle the "atom is the first thing emitted" case explicitly.
      IF p_atom_start > 1 THEN
        g_buf := UTL_RAW.SUBSTR(g_buf, 1, p_atom_start - 1);
      ELSE
        g_buf := NULL;
      END IF;
      g_buf_size := p_atom_start - 1;

      FOR i IN 1 .. min_n LOOP
        emit(atom_bytes);
      END LOOP;
      IF has_max THEN
        FOR i IN min_n + 1 .. max_n LOOP
          -- Optional copy: SPLIT next, atom...
          emit(pack_u8(C_OP_SPLIT));
          s1_off := emit_placeholder_u16;
          s2_off := emit_placeholder_u16;
          patch_u16(s1_off, g_buf_size + 1);
          emit(atom_bytes);
          patch_u16(s2_off, g_buf_size + 1);
        END LOOP;
      ELSE
        -- Unbounded — emit a `*` loop after the min_n fixed copies.
        DECLARE
          loop_start PLS_INTEGER;
          s1_o       PLS_INTEGER;
          s2_o       PLS_INTEGER;
        BEGIN
          loop_start := g_buf_size + 1;
          emit(pack_u8(C_OP_SPLIT));
          s1_o := emit_placeholder_u16;
          s2_o := emit_placeholder_u16;
          patch_u16(s1_o, g_buf_size + 1);
          emit(atom_bytes);
          emit(pack_u8(C_OP_JMP));
          j_off := emit_placeholder_u16;
          patch_u16(j_off, loop_start);
          patch_u16(s2_o, g_buf_size + 1);
        END;
      END IF;
      RETURN;
    END IF;

    -- ? + *
    advance;
    -- Lazy variant? Currently treated as greedy (lazy is a v1 feature).
    IF peek_ch = '?' THEN
      greedy := FALSE;
      advance;
    END IF;

    IF ch = '?' THEN
      -- Insert SPLIT before the atom
      DECLARE
        rest RAW(32767) := UTL_RAW.SUBSTR(g_buf, p_atom_start, atom_size);
        prefix RAW(32767) :=
          CASE WHEN p_atom_start > 1
               THEN UTL_RAW.SUBSTR(g_buf, 1, p_atom_start - 1)
               ELSE NULL END;
      BEGIN
        g_buf := prefix;
        g_buf_size := p_atom_start - 1;
        IF g_buf IS NULL THEN g_buf := NULL; END IF;
        emit(pack_u8(C_OP_SPLIT));
        s1_off := emit_placeholder_u16;
        s2_off := emit_placeholder_u16;
        IF greedy THEN
          patch_u16(s1_off, g_buf_size + 1);
          emit(rest);
          patch_u16(s2_off, g_buf_size + 1);
        ELSE
          -- lazy ?: swap addr1/addr2 to prefer skip
          patch_u16(s2_off, g_buf_size + 1);
          emit(rest);
          patch_u16(s1_off, g_buf_size + 1);
        END IF;
      END;
    ELSIF ch = '*' THEN
      -- Loop: L0: SPLIT L1, L2 ; L1: atom ; JMP L0 ; L2: ...
      DECLARE
        prefix RAW(32767) :=
          CASE WHEN p_atom_start > 1
               THEN UTL_RAW.SUBSTR(g_buf, 1, p_atom_start - 1)
               ELSE NULL END;
      BEGIN
        g_buf := prefix;
        g_buf_size := p_atom_start - 1;
        IF g_buf IS NULL THEN g_buf := NULL; END IF;
        DECLARE
          l0 PLS_INTEGER := g_buf_size + 1;
        BEGIN
          emit(pack_u8(C_OP_SPLIT));
          s1_off := emit_placeholder_u16;
          s2_off := emit_placeholder_u16;
          IF greedy THEN
            patch_u16(s1_off, g_buf_size + 1);
            emit(atom_bytes);
            emit(pack_u8(C_OP_JMP));
            j_off := emit_placeholder_u16;
            patch_u16(j_off, l0);
            patch_u16(s2_off, g_buf_size + 1);
          ELSE
            patch_u16(s2_off, g_buf_size + 1);
            emit(atom_bytes);
            emit(pack_u8(C_OP_JMP));
            j_off := emit_placeholder_u16;
            patch_u16(j_off, l0);
            patch_u16(s1_off, g_buf_size + 1);
          END IF;
        END;
      END;
    ELSIF ch = '+' THEN
      -- Atom already emitted; then SPLIT back / fall-through
      DECLARE
        l1 PLS_INTEGER := p_atom_start;
      BEGIN
        emit(pack_u8(C_OP_SPLIT));
        s1_off := emit_placeholder_u16;
        s2_off := emit_placeholder_u16;
        IF greedy THEN
          patch_u16(s1_off, l1);
          patch_u16(s2_off, g_buf_size + 1);
        ELSE
          patch_u16(s2_off, l1);
          patch_u16(s1_off, g_buf_size + 1);
        END IF;
      END;
    END IF;
  END;

  -- Walk a bytecode chunk (compiled as if it lived at offset 1) and add
  -- `p_delta` to every embedded SPLIT and JMP target address, in place.
  -- Branches in compile_alt are each compiled into their own buffer and
  -- then assembled into the global program at varying offsets — without
  -- this shift, every internal SPLIT/JMP would point at the wrong place.
  PROCEDURE shift_addresses(p_bytes IN OUT RAW, p_delta IN PLS_INTEGER) IS
    i  PLS_INTEGER := 1;
    sz PLS_INTEGER;
    op PLS_INTEGER;
    a  PLS_INTEGER;
    b  PLS_INTEGER;
  BEGIN
    IF p_bytes IS NULL OR p_delta = 0 THEN RETURN; END IF;
    sz := UTL_RAW.LENGTH(p_bytes);
    WHILE i <= sz LOOP
      op := UTL_RAW.CAST_TO_BINARY_INTEGER(UTL_RAW.SUBSTR(p_bytes, i, 1));
      CASE op
        WHEN C_OP_MATCH THEN i := i + 1;
        WHEN C_OP_CHAR  THEN i := i + 5;
        WHEN C_OP_ANY   THEN i := i + 1;
        WHEN C_OP_ANYD  THEN i := i + 1;
        WHEN C_OP_CLS   THEN i := i + 3;
        WHEN C_OP_JMP   THEN
          a := UTL_RAW.CAST_TO_BINARY_INTEGER(UTL_RAW.SUBSTR(p_bytes, i + 1, 2));
          p_bytes := UTL_RAW.OVERLAY(pack_u16(a + p_delta), p_bytes, i + 1);
          i := i + 3;
        WHEN C_OP_SPLIT THEN
          a := UTL_RAW.CAST_TO_BINARY_INTEGER(UTL_RAW.SUBSTR(p_bytes, i + 1, 2));
          b := UTL_RAW.CAST_TO_BINARY_INTEGER(UTL_RAW.SUBSTR(p_bytes, i + 3, 2));
          p_bytes := UTL_RAW.OVERLAY(pack_u16(a + p_delta), p_bytes, i + 1);
          p_bytes := UTL_RAW.OVERLAY(pack_u16(b + p_delta), p_bytes, i + 3);
          i := i + 5;
        WHEN C_OP_SAVE THEN i := i + 2;
        WHEN C_OP_BOL  THEN i := i + 1;
        WHEN C_OP_EOL  THEN i := i + 1;
        WHEN C_OP_BOS  THEN i := i + 1;
        WHEN C_OP_EOS  THEN i := i + 1;
        WHEN C_OP_WB   THEN i := i + 1;
        WHEN C_OP_NWB  THEN i := i + 1;
        ELSE
          raise_application_error(-20800,
            'pell_re internal: unknown opcode ' || op || ' at offset ' || i
            || ' while shifting addresses');
      END CASE;
    END LOOP;
  END;

  -- Sequence: zero or more atoms+quantifiers, stopping at `)` or `|` or EOF.
  FUNCTION compile_seq RETURN PLS_INTEGER IS
    start_addr PLS_INTEGER := g_buf_size + 1;
    atom_start PLS_INTEGER;
  BEGIN
    WHILE NOT eof AND peek_ch NOT IN (')', '|') LOOP
      atom_start := compile_atom;
      apply_quantifier(atom_start);
    END LOOP;
    RETURN start_addr;
  END;

  -- Alternation: seq ( '|' seq )*
  FUNCTION compile_alt RETURN PLS_INTEGER IS
    start_addr   PLS_INTEGER := g_buf_size + 1;
    branches     t_int_list;  -- offsets of trailing JMPs to patch
    nbranches    PLS_INTEGER := 0;
    seq_start    PLS_INTEGER;
    split_addr   PLS_INTEGER;
    s1_off       PLS_INTEGER;
    s2_off       PLS_INTEGER;
    j_off        PLS_INTEGER;
    saved_buf    RAW(32767);
    saved_size   PLS_INTEGER;
    branch_buf   RAW(32767);
    branch_size  PLS_INTEGER;
    first_branch BOOLEAN := TRUE;
    -- The implementation strategy: parse all branches into temporary buffers,
    -- then assemble: SPLIT a, b ; <branch 1> ; JMP end ; <branch 2> ; ... ; end.
    -- For multi-way alternation, we chain SPLITs.
    branch_bytes t_text_list;  -- HEX of each branch's bytecode
    branch_addrs t_int_list;
  BEGIN
    -- Save current buf, parse first branch into a fresh sub-buffer
    saved_buf := g_buf;
    saved_size := g_buf_size;
    g_buf := NULL; g_buf_size := 0;
    seq_start := compile_seq;
    branch_size := g_buf_size;
    branch_bytes(1) := CASE WHEN g_buf IS NULL THEN '' ELSE RAWTOHEX(g_buf) END;
    nbranches := 1;

    WHILE peek_ch = '|' LOOP
      advance;
      g_buf := NULL; g_buf_size := 0;
      seq_start := compile_seq;
      nbranches := nbranches + 1;
      branch_bytes(nbranches) :=
        CASE WHEN g_buf IS NULL THEN '' ELSE RAWTOHEX(g_buf) END;
    END LOOP;

    -- Restore the parent buffer and emit the assembled alternation.
    g_buf := saved_buf; g_buf_size := saved_size;
    IF g_buf IS NULL THEN g_buf := NULL; END IF;

    IF nbranches = 1 THEN
      -- No `|` — just append the single branch's bytes, shifted to land
      -- at the current global position.
      IF branch_bytes(1) IS NOT NULL AND LENGTH(branch_bytes(1)) > 0 THEN
        DECLARE
          bin RAW(32767) := HEXTORAW(branch_bytes(1));
        BEGIN
          shift_addresses(bin, g_buf_size);  -- compiled at offset 1 → +g_buf_size
          emit(bin);
        END;
      END IF;
      RETURN start_addr;
    END IF;

    -- Chain of SPLITs: for n branches we emit n-1 SPLITs each preferring
    -- the current branch's start, falling through to the next SPLIT.
    DECLARE
      end_jumps t_int_list;
      nej       PLS_INTEGER := 0;
      branch_bin RAW(32767);
    BEGIN
      FOR i IN 1 .. nbranches LOOP
        IF i < nbranches THEN
          emit(pack_u8(C_OP_SPLIT));
          s1_off := emit_placeholder_u16;
          s2_off := emit_placeholder_u16;
          patch_u16(s1_off, g_buf_size + 1);  -- this branch
        END IF;
        IF LENGTH(branch_bytes(i)) > 0 THEN
          branch_bin := HEXTORAW(branch_bytes(i));
          shift_addresses(branch_bin, g_buf_size);  -- branch compiled at offset 1
          emit(branch_bin);
        END IF;
        IF i < nbranches THEN
          emit(pack_u8(C_OP_JMP));
          j_off := emit_placeholder_u16;
          nej := nej + 1;
          end_jumps(nej) := j_off;
          patch_u16(s2_off, g_buf_size + 1);
        END IF;
      END LOOP;
      -- Patch all trailing JMPs to the post-alternation position
      FOR i IN 1 .. nej LOOP
        patch_u16(end_jumps(i), g_buf_size + 1);
      END LOOP;
    END;
    RETURN start_addr;
  END;

  -- -------------------------------------------------------------------------
  -- Top-level compile
  -- -------------------------------------------------------------------------

  PROCEDURE compile_pattern(p_pat IN VARCHAR2, p_out OUT t_compiled) IS
    dummy PLS_INTEGER;
    empty_names t_name_to_idx;
  BEGIN
    g_buf := NULL; g_buf_size := 0;
    g_pool_buf := NULL; g_pool_size := 0;
    g_pat := p_pat;
    g_pos := 1;
    g_groups := 0;
    g_group_names := empty_names;

    -- Group 0 wraps the whole match: SAVE 0 at start, SAVE 1 before MATCH.
    emit(pack_u8(C_OP_SAVE));
    emit(pack_u8(0));

    dummy := compile_alt;
    IF NOT eof THEN die('unexpected `' || peek_ch || '`'); END IF;

    emit(pack_u8(C_OP_SAVE));
    emit(pack_u8(1));
    emit(pack_u8(C_OP_MATCH));

    p_out.prog_size  := g_buf_size;
    p_out.pool_start := g_buf_size + 1;
    -- Append the class pool to the program
    IF g_pool_size > 0 THEN
      IF g_buf IS NULL OR g_buf_size = 0 THEN
        g_buf := g_pool_buf;
      ELSE
        g_buf := UTL_RAW.CONCAT(g_buf, g_pool_buf);
      END IF;
    END IF;
    p_out.prog        := g_buf;
    p_out.group_count := g_groups;
    p_out.group_names := g_group_names;
  END;

  FUNCTION cache_key(p_pat IN VARCHAR2) RETURN VARCHAR2 IS
    h RAW(32);
  BEGIN
    SELECT STANDARD_HASH(p_pat, 'SHA256') INTO h FROM dual;
    RETURN RAWTOHEX(h);
  END;

  FUNCTION get_compiled(p_pat IN VARCHAR2) RETURN t_compiled IS
    k    VARCHAR2(64) := cache_key(p_pat);
    c    t_compiled;
  BEGIN
    IF g_cache.EXISTS(k) THEN
      RETURN g_cache(k);
    END IF;
    compile_pattern(p_pat, c);
    g_cache(k) := c;
    RETURN c;
  END;

  -- -------------------------------------------------------------------------
  -- Matcher — Pike's algorithm (Thompson NFA simulation)
  --
  -- We maintain two sets of "threads": `cur` (active at current input
  -- position) and `nxt` (active at next position). For each input char,
  -- we run every cur-thread, advance CHAR/CLS/ANY into nxt, and process
  -- epsilon transitions (SPLIT/JMP/SAVE/anchors) in place inside cur.
  --
  -- Duplicate PC suppression: two threads at the same PC are equivalent
  -- for v0 since we don't track capture differences. (When captures are
  -- added in v1 we'll keep the earlier one — leftmost wins.)
  -- -------------------------------------------------------------------------

  -- Test a character class at pool offset against a codepoint.
  FUNCTION class_matches(p_prog IN RAW,
                         p_offset IN PLS_INTEGER,
                         p_cp IN PLS_INTEGER) RETURN BOOLEAN IS
    neg    PLS_INTEGER;
    n      PLS_INTEGER;
    lo     PLS_INTEGER;
    hi     PLS_INTEGER;
    p      PLS_INTEGER := p_offset;
    match  BOOLEAN := FALSE;
  BEGIN
    neg := read_u8(p_prog, p); p := p + 1;
    n   := read_u16(p_prog, p); p := p + 2;
    FOR i IN 1 .. n LOOP
      lo := read_u32(p_prog, p); p := p + 4;
      hi := read_u32(p_prog, p); p := p + 4;
      IF p_cp BETWEEN lo AND hi THEN
        match := TRUE;
        EXIT;
      END IF;
    END LOOP;
    IF neg = 1 THEN
      RETURN NOT match;
    END IF;
    RETURN match;
  END;

  -- Run one match attempt starting at codepoint position p_start.
  -- Returns the codepoint position of where the match ends (exclusive),
  -- or NULL if no match starting here.
  --
  -- This is the inner kernel — called by `find` from each candidate
  -- start position; calling once from p_start=1 only would implement
  -- "matches at start" (^anchored) semantics.
  -- Run one match attempt starting at codepoint position p_start.
  -- Returns NULL end_pos if no match. On success, p_caps_out receives
  -- the capture array of the longest-match thread (greedy/leftmost-longest
  -- semantics): caps(0/1) is the whole match, (2/3) is group 1, etc.
  PROCEDURE run_at(p_s         IN  VARCHAR2,
                   p_slen      IN  PLS_INTEGER,
                   p_compiled  IN  t_compiled,
                   p_start     IN  PLS_INTEGER,
                   p_end_out   OUT PLS_INTEGER,
                   p_caps_out  OUT t_int_list) IS
    prog        RAW(32767)    := p_compiled.prog;
    psize       PLS_INTEGER   := p_compiled.prog_size;
    cur_threads t_thread_list;
    nxt_threads t_thread_list;
    seen        t_int_list;  -- pc → 1 if already added this step
    cur_n       PLS_INTEGER;
    nxt_n       PLS_INTEGER;
    pc          PLS_INTEGER;
    op          PLS_INTEGER;
    pos         PLS_INTEGER;
    cur_ch      VARCHAR2(4);
    cur_cp      PLS_INTEGER;
    best_end    PLS_INTEGER := NULL;
    best_caps   t_int_list;
    cls_off     PLS_INTEGER;
    -- The active "park list" — `add` pushes consumer-op threads here.
    -- It's an indirection so the same nested procedure can populate either
    -- cur_threads (during seeding) or nxt_threads (during step-into-next).
    target_is_nxt BOOLEAN;

    PROCEDURE park(p_pc IN PLS_INTEGER, p_caps IN t_int_list) IS
    BEGIN
      IF target_is_nxt THEN
        nxt_n := nxt_n + 1;
        nxt_threads(nxt_n).pc   := p_pc;
        nxt_threads(nxt_n).caps := p_caps;
      ELSE
        cur_n := cur_n + 1;
        cur_threads(cur_n).pc   := p_pc;
        cur_threads(cur_n).caps := p_caps;
      END IF;
    END;

    PROCEDURE add(p_pc IN PLS_INTEGER, p_at_pos IN PLS_INTEGER, p_caps IN t_int_list) IS
      pc_loc PLS_INTEGER := p_pc;
      op_loc PLS_INTEGER;
      aa     PLS_INTEGER;
      bb     PLS_INTEGER;
    BEGIN
      IF pc_loc < 1 OR pc_loc > psize THEN RETURN; END IF;
      IF seen.EXISTS(pc_loc) THEN RETURN; END IF;
      seen(pc_loc) := 1;
      op_loc := read_u8(prog, pc_loc);
      CASE op_loc
        WHEN C_OP_JMP THEN
          add(read_u16(prog, pc_loc + 1), p_at_pos, p_caps);
        WHEN C_OP_SPLIT THEN
          aa := read_u16(prog, pc_loc + 1);
          bb := read_u16(prog, pc_loc + 3);
          add(aa, p_at_pos, p_caps);
          add(bb, p_at_pos, p_caps);
        WHEN C_OP_SAVE THEN
          DECLARE
            slot PLS_INTEGER := read_u8(prog, pc_loc + 1);
            new_caps t_int_list := p_caps;
          BEGIN
            new_caps(slot) := p_at_pos;
            add(pc_loc + 2, p_at_pos, new_caps);
          END;
        WHEN C_OP_BOL THEN
          IF p_at_pos = 1 OR (p_at_pos > 1 AND SUBSTRC(p_s, p_at_pos - 1, 1) = CHR(10)) THEN
            add(pc_loc + 1, p_at_pos, p_caps);
          END IF;
        WHEN C_OP_EOL THEN
          IF p_at_pos > p_slen OR SUBSTRC(p_s, p_at_pos, 1) = CHR(10) THEN
            add(pc_loc + 1, p_at_pos, p_caps);
          END IF;
        WHEN C_OP_BOS THEN
          IF p_at_pos = 1 THEN add(pc_loc + 1, p_at_pos, p_caps); END IF;
        WHEN C_OP_EOS THEN
          IF p_at_pos > p_slen THEN add(pc_loc + 1, p_at_pos, p_caps); END IF;
        WHEN C_OP_WB THEN
          DECLARE
            left_w  BOOLEAN;
            right_w BOOLEAN;
            cc      VARCHAR2(4);
          BEGIN
            IF p_at_pos = 1 THEN
              left_w := FALSE;
            ELSE
              cc := SUBSTRC(p_s, p_at_pos - 1, 1);
              left_w :=
                (cc BETWEEN 'a' AND 'z') OR (cc BETWEEN 'A' AND 'Z') OR
                (cc BETWEEN '0' AND '9') OR cc = '_';
            END IF;
            IF p_at_pos > p_slen THEN
              right_w := FALSE;
            ELSE
              cc := SUBSTRC(p_s, p_at_pos, 1);
              right_w :=
                (cc BETWEEN 'a' AND 'z') OR (cc BETWEEN 'A' AND 'Z') OR
                (cc BETWEEN '0' AND '9') OR cc = '_';
            END IF;
            IF left_w != right_w THEN add(pc_loc + 1, p_at_pos, p_caps); END IF;
          END;
        WHEN C_OP_NWB THEN
          DECLARE
            left_w  BOOLEAN;
            right_w BOOLEAN;
            cc      VARCHAR2(4);
          BEGIN
            IF p_at_pos = 1 THEN
              left_w := FALSE;
            ELSE
              cc := SUBSTRC(p_s, p_at_pos - 1, 1);
              left_w :=
                (cc BETWEEN 'a' AND 'z') OR (cc BETWEEN 'A' AND 'Z') OR
                (cc BETWEEN '0' AND '9') OR cc = '_';
            END IF;
            IF p_at_pos > p_slen THEN
              right_w := FALSE;
            ELSE
              cc := SUBSTRC(p_s, p_at_pos, 1);
              right_w :=
                (cc BETWEEN 'a' AND 'z') OR (cc BETWEEN 'A' AND 'Z') OR
                (cc BETWEEN '0' AND '9') OR cc = '_';
            END IF;
            IF left_w = right_w THEN add(pc_loc + 1, p_at_pos, p_caps); END IF;
          END;
        ELSE
          -- CHAR / ANY / ANYD / CLS / MATCH — terminal for this step;
          -- park the thread with its current captures.
          park(pc_loc, p_caps);
      END CASE;
    END;

  BEGIN
    cur_n := 0;
    nxt_n := 0;
    seen.DELETE;
    target_is_nxt := FALSE;
    DECLARE empty_caps t_int_list; BEGIN
      add(1, p_start, empty_caps);
    END;

    pos := p_start;
    LOOP
      -- Check whether any active thread is on MATCH — if so we've found a
      -- candidate end position. The first such thread to arrive is the
      -- leftmost-greedy winner (other threads are discarded by seen-dedup).
      FOR i IN 1 .. cur_n LOOP
        IF read_u8(prog, cur_threads(i).pc) = C_OP_MATCH THEN
          IF best_end IS NULL OR pos > best_end THEN
            best_end  := pos;
            best_caps := cur_threads(i).caps;
          END IF;
        END IF;
      END LOOP;

      EXIT WHEN cur_n = 0;
      EXIT WHEN pos > p_slen;

      cur_ch := SUBSTRC(p_s, pos, 1);
      cur_cp := codepoint_of(cur_ch);

      nxt_n := 0;
      seen.DELETE;
      target_is_nxt := TRUE;

      FOR i IN 1 .. cur_n LOOP
        pc := cur_threads(i).pc;
        op := read_u8(prog, pc);
        CASE op
          WHEN C_OP_CHAR THEN
            IF read_u32(prog, pc + 1) = cur_cp THEN
              add(pc + 5, pos + 1, cur_threads(i).caps);
            END IF;
          WHEN C_OP_ANY THEN
            IF cur_ch != CHR(10) THEN
              add(pc + 1, pos + 1, cur_threads(i).caps);
            END IF;
          WHEN C_OP_ANYD THEN
            add(pc + 1, pos + 1, cur_threads(i).caps);
          WHEN C_OP_CLS THEN
            cls_off := read_u16(prog, pc + 1) + p_compiled.pool_start - 1;
            IF class_matches(prog, cls_off, cur_cp) THEN
              add(pc + 3, pos + 1, cur_threads(i).caps);
            END IF;
          WHEN C_OP_MATCH THEN
            NULL;  -- already recorded above
          ELSE
            NULL;
        END CASE;
      END LOOP;

      -- Swap cur <- nxt
      cur_threads := nxt_threads;
      cur_n       := nxt_n;
      target_is_nxt := FALSE;
      pos := pos + 1;
    END LOOP;

    -- Final pass — anchors like $ / \z may have just been satisfied.
    FOR i IN 1 .. cur_n LOOP
      IF read_u8(prog, cur_threads(i).pc) = C_OP_MATCH THEN
        IF best_end IS NULL OR pos > best_end THEN
          best_end  := pos;
          best_caps := cur_threads(i).caps;
        END IF;
      END IF;
    END LOOP;

    p_end_out  := best_end;
    p_caps_out := best_caps;
  END;

  -- Thin scalar-return wrapper for callers that don't need captures.
  FUNCTION run_at_end(p_s        IN VARCHAR2,
                      p_slen     IN PLS_INTEGER,
                      p_compiled IN t_compiled,
                      p_start    IN PLS_INTEGER) RETURN PLS_INTEGER IS
    e PLS_INTEGER;
    c t_int_list;
  BEGIN
    run_at(p_s, p_slen, p_compiled, p_start, e, c);
    RETURN e;
  END;

  -- -------------------------------------------------------------------------
  -- Public API
  -- -------------------------------------------------------------------------

  FUNCTION matches(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN BOOLEAN IS
    c     t_compiled := get_compiled(p_pat);
    slen  PLS_INTEGER := LENGTHC(p_s);
    e     PLS_INTEGER;
  BEGIN
    FOR sp IN 1 .. slen + 1 LOOP
      e := run_at_end(p_s, slen, c, sp);
      IF e IS NOT NULL THEN RETURN TRUE; END IF;
    END LOOP;
    RETURN FALSE;
  END;

  FUNCTION find(p_s   IN VARCHAR2,
                p_pat IN VARCHAR2,
                p_pos IN PLS_INTEGER DEFAULT 1) RETURN VARCHAR2 IS
    c     t_compiled := get_compiled(p_pat);
    slen  PLS_INTEGER := LENGTHC(p_s);
    e     PLS_INTEGER;
  BEGIN
    FOR sp IN p_pos .. slen + 1 LOOP
      e := run_at_end(p_s, slen, c, sp);
      IF e IS NOT NULL THEN
        RETURN SUBSTRC(p_s, sp, e - sp);
      END IF;
    END LOOP;
    RETURN NULL;
  END;

  FUNCTION find_span(p_s   IN VARCHAR2,
                     p_pat IN VARCHAR2,
                     p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_int_list IS
    c     t_compiled := get_compiled(p_pat);
    slen  PLS_INTEGER := LENGTHC(p_s);
    e     PLS_INTEGER;
    out   t_int_list;
  BEGIN
    FOR sp IN p_pos .. slen + 1 LOOP
      e := run_at_end(p_s, slen, c, sp);
      IF e IS NOT NULL THEN
        out(1) := sp;
        out(2) := e;
        RETURN out;
      END IF;
    END LOOP;
    RETURN out;  -- empty
  END;

  FUNCTION capture(p_s   IN VARCHAR2,
                   p_pat IN VARCHAR2,
                   p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_capture_list IS
    c       t_compiled := get_compiled(p_pat);
    slen    PLS_INTEGER := LENGTHC(p_s);
    e       PLS_INTEGER;
    caps    t_int_list;
    out     t_capture_list;
    cap     t_capture;
    s_slot  PLS_INTEGER;
    e_slot  PLS_INTEGER;
  BEGIN
    FOR sp IN p_pos .. slen + 1 LOOP
      run_at(p_s, slen, c, sp, e, caps);
      IF e IS NOT NULL THEN
        -- Group 0 is the whole match.
        FOR g IN 0 .. c.group_count LOOP
          s_slot := g * 2;
          e_slot := g * 2 + 1;
          IF caps.EXISTS(s_slot) AND caps.EXISTS(e_slot) THEN
            cap.start_pos  := caps(s_slot);
            cap.end_pos    := caps(e_slot);
            cap.match_text := SUBSTRC(p_s, cap.start_pos, cap.end_pos - cap.start_pos);
          ELSE
            cap.start_pos  := NULL;
            cap.end_pos    := NULL;
            cap.match_text := NULL;
          END IF;
          out(g) := cap;
        END LOOP;
        RETURN out;
      END IF;
    END LOOP;
    RETURN out;  -- empty
  END;

  FUNCTION capture_by_name(p_s   IN VARCHAR2,
                           p_pat IN VARCHAR2,
                           p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_capture_map IS
    c     t_compiled := get_compiled(p_pat);
    all_caps t_capture_list;
    out   t_capture_map;
    nm    VARCHAR2(64);
  BEGIN
    all_caps := capture(p_s, p_pat, p_pos);
    IF all_caps.COUNT = 0 THEN RETURN out; END IF;
    nm := c.group_names.FIRST;
    WHILE nm IS NOT NULL LOOP
      DECLARE
        idx PLS_INTEGER := c.group_names(nm);
      BEGIN
        IF all_caps.EXISTS(idx) AND all_caps(idx).start_pos IS NOT NULL THEN
          out(nm) := all_caps(idx);
        END IF;
      END;
      nm := c.group_names.NEXT(nm);
    END LOOP;
    RETURN out;
  END;

  FUNCTION find_all(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN t_text_list IS
    c     t_compiled := get_compiled(p_pat);
    slen  PLS_INTEGER := LENGTHC(p_s);
    e     PLS_INTEGER;
    pos   PLS_INTEGER := 1;
    out   t_text_list;
    n     PLS_INTEGER := 0;
  BEGIN
    WHILE pos <= slen + 1 LOOP
      e := run_at_end(p_s, slen, c, pos);
      IF e IS NULL THEN
        pos := pos + 1;
      ELSE
        n := n + 1;
        out(n) := SUBSTRC(p_s, pos, e - pos);
        -- prevent infinite loops on zero-width matches
        IF e = pos THEN
          pos := pos + 1;
        ELSE
          pos := e;
        END IF;
      END IF;
    END LOOP;
    RETURN out;
  END;

  FUNCTION replace_all(p_s   IN VARCHAR2,
                       p_pat IN VARCHAR2,
                       p_rep IN VARCHAR2) RETURN VARCHAR2 IS
    c     t_compiled := get_compiled(p_pat);
    slen  PLS_INTEGER := LENGTHC(p_s);
    e     PLS_INTEGER;
    pos   PLS_INTEGER := 1;
    out   VARCHAR2(32767) := '';
  BEGIN
    WHILE pos <= slen + 1 LOOP
      e := run_at_end(p_s, slen, c, pos);
      IF e IS NULL THEN
        IF pos <= slen THEN
          out := out || SUBSTRC(p_s, pos, 1);
        END IF;
        pos := pos + 1;
      ELSE
        out := out || p_rep;
        IF e = pos THEN
          -- zero-width: emit one char and skip past it to avoid infinite loop
          IF pos <= slen THEN
            out := out || SUBSTRC(p_s, pos, 1);
          END IF;
          pos := pos + 1;
        ELSE
          pos := e;
        END IF;
      END IF;
    END LOOP;
    RETURN out;
  END;

  FUNCTION split(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN t_text_list IS
    c     t_compiled := get_compiled(p_pat);
    slen  PLS_INTEGER := LENGTHC(p_s);
    e     PLS_INTEGER;
    pos   PLS_INTEGER := 1;
    seg_start PLS_INTEGER := 1;
    out   t_text_list;
    n     PLS_INTEGER := 0;
  BEGIN
    WHILE pos <= slen + 1 LOOP
      e := run_at_end(p_s, slen, c, pos);
      IF e IS NULL THEN
        pos := pos + 1;
      ELSIF e = pos THEN
        -- zero-width: include the next char in the current segment and skip
        pos := pos + 1;
      ELSE
        n := n + 1;
        out(n) := SUBSTRC(p_s, seg_start, pos - seg_start);
        seg_start := e;
        pos := e;
      END IF;
    END LOOP;
    n := n + 1;
    out(n) := SUBSTRC(p_s, seg_start, slen + 1 - seg_start);
    RETURN out;
  END;

  PROCEDURE clear_cache IS
  BEGIN
    g_cache.DELETE;
  END;

END pell_re;
/
