# pell_re — regex engine

`pell_re` is a pure-PL/SQL regex engine. It's a Thompson-NFA matcher
(linear-time, no catastrophic backtracking) with bytecode held in
`RAW(32767)` via `UTL_RAW`. No Java, no extproc — runs identically
on 19c, 23ai, Autonomous Database, and AWS RDS for Oracle.

## Installation

```sh
# from the repo root
sqlplus user/pass @runtime/pell_re.sql
```

The package is self-contained; install once per schema.

## Public types

### `t_text_list`

```sql
TYPE t_text_list IS TABLE OF VARCHAR2(4000) INDEX BY PLS_INTEGER;
```

1-based list of strings. Returned by `find_all`, `split`.

### `t_int_list`

```sql
TYPE t_int_list IS TABLE OF PLS_INTEGER INDEX BY PLS_INTEGER;
```

1-based list of integers. Returned by `find_span` (2 entries: start
and end position).

### `t_capture`

```sql
TYPE t_capture IS RECORD (
  start_pos  PLS_INTEGER,
  end_pos    PLS_INTEGER,
  match_text VARCHAR2(4000)
);
```

A single capture: span (1-based positions, end-exclusive) and the
matched substring. Both `start_pos` and `match_text` are NULL if the
group didn't participate.

### `t_capture_list`

```sql
TYPE t_capture_list IS TABLE OF t_capture INDEX BY PLS_INTEGER;
```

Index 0 is the whole match; indices 1..N are the parenthesized groups
in source order.

### `t_capture_map`

```sql
TYPE t_capture_map IS TABLE OF t_capture INDEX BY VARCHAR2(64);
```

Captures keyed by group name (only `(?<name>...)` groups appear here).

## Public functions

### `matches(s, pat)`

```sql
FUNCTION matches(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN BOOLEAN;
```

True if any substring of `s` matches `pat` (unanchored).

### `find(s, pat[, pos])`

```sql
FUNCTION find(p_s IN VARCHAR2, p_pat IN VARCHAR2,
              p_pos IN PLS_INTEGER DEFAULT 1) RETURN VARCHAR2;
```

Returns the substring of the first match starting at or after `pos`.
NULL if no match.

### `find_all(s, pat)`

```sql
FUNCTION find_all(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN t_text_list;
```

All non-overlapping matches, left-to-right.

### `find_span(s, pat[, pos])`

```sql
FUNCTION find_span(p_s IN VARCHAR2, p_pat IN VARCHAR2,
                   p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_int_list;
```

A 2-element list with `[start_pos, end_pos]` (1-based, end-exclusive)
of the first match. Empty list if no match.

### `capture(s, pat[, pos])`

```sql
FUNCTION capture(p_s IN VARCHAR2, p_pat IN VARCHAR2,
                 p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_capture_list;
```

Returns the capture spans for the first match. Index 0 is the whole
match; indices 1..N are the parenthesized groups (capturing only —
`(?:...)` is skipped). Empty list if no match.

### `capture_by_name(s, pat[, pos])`

```sql
FUNCTION capture_by_name(p_s IN VARCHAR2, p_pat IN VARCHAR2,
                         p_pos IN PLS_INTEGER DEFAULT 1) RETURN t_capture_map;
```

Named-group captures only, keyed by name.

### `replace_all(s, pat, rep)`

```sql
FUNCTION replace_all(p_s IN VARCHAR2, p_pat IN VARCHAR2,
                     p_rep IN VARCHAR2) RETURN VARCHAR2;
```

Replace every non-overlapping match with `rep` (literal, no
backreferences in v0).

### `split(s, pat)`

```sql
FUNCTION split(p_s IN VARCHAR2, p_pat IN VARCHAR2) RETURN t_text_list;
```

Split on every match of `pat`. Leading/trailing matches produce
empty fields (matching PCRE-style `split` semantics).

### `clear_cache()`

```sql
PROCEDURE clear_cache;
```

Drop the per-session compiled-pattern cache. Useful for tests; rarely
needed otherwise.

## Supported regex syntax

| Construct           | Notes                                                  |
|---------------------|---------------------------------------------------------|
| `.`                 | any char except `\n`                                    |
| `*` `+` `?`         | greedy quantifiers                                      |
| `{n}` `{n,}` `{n,m}`| counted quantifiers                                     |
| `|`                 | alternation                                             |
| `(...)`             | capturing group                                         |
| `(?:...)`           | non-capturing group                                     |
| `(?<name>...)`      | named capture                                           |
| `[...]` `[^...]`    | char class / negated                                    |
| `[a-z]`             | range                                                   |
| `\d \D \w \W \s \S` | shorthand classes (ASCII)                               |
| `^` `$`             | start / end of line                                     |
| `\A` `\z`           | start / end of string                                   |
| `\b` `\B`           | word boundary / non-boundary                            |
| `\n \r \t \f \v \0` | char escapes                                            |
| `\.` `\(` `\)` `\\` | escape literals                                         |

Not supported in v0: backreferences `\1`, lookbehinds `(?<=...)`,
possessive quantifiers `*+`, Unicode property classes `\p{L}`, inline
flags `(?i)` (planned), `/pattern/flags` suffixes (rejected at lex
time with a helpful error).

## Bytecode layout

For the curious: the regex compiler produces variable-length opcodes
in `RAW(32767)`:

| Op    | Hex | Operands             | Size | Semantics                         |
|-------|-----|----------------------|------|------------------------------------|
| MATCH | 01  | -                    | 1    | accept                            |
| CHAR  | 02  | cp:u32               | 5    | match codepoint                   |
| ANY   | 03  | -                    | 1    | `.` (no `\n`)                     |
| ANYD  | 04  | -                    | 1    | `.` in dotall mode                |
| CLS   | 05  | pool_offset:u16      | 3    | character class                   |
| JMP   | 06  | addr:u16             | 3    | goto                              |
| SPLIT | 07  | addr1:u16, addr2:u16 | 5    | NFA fork (prefer addr1)           |
| SAVE  | 08  | slot:u8              | 2    | record current pos                |
| BOL   | 09  | -                    | 1    | `^`                               |
| EOL   | 0A  | -                    | 1    | `$`                               |
| BOS   | 0B  | -                    | 1    | `\A`                              |
| EOS   | 0C  | -                    | 1    | `\z`                              |
| WB    | 0D  | -                    | 1    | `\b`                              |
| NWB   | 0E  | -                    | 1    | `\B`                              |

Character classes live in a pool after the program. Each class:
1 byte negated flag + 2 bytes range count + N×8 bytes of
`(start_cp:u32, end_cp:u32)` ranges.

The matcher uses Pike's algorithm with per-thread capture arrays.
SAVE opcodes copy and update the capture array; seen-by-PC dedup
gives leftmost-greedy semantics.

## Cache

Each call goes through `get_compiled(pat)`, which:
1. Hashes the pattern: `RAWTOHEX(STANDARD_HASH(pat, 'SHA256'))`.
2. Checks `g_cache(hash)` — return if hit.
3. Otherwise compiles to bytecode and stores in `g_cache`.

The cache is package state; it lives for the duration of the session.
Call `clear_cache` to drop it manually.

## Performance characteristics

- **Compilation**: ~5-20µs for typical patterns. Cache wipes this out
  for repeat calls.
- **Matching**: linear in input length. ~50-200ns per input character
  for typical patterns. Worst case is the same as best case — no
  catastrophic backtracking.
- **Memory**: bounded by `O(N × P)` where N is input length and P is
  the number of NFA states (usually small).

For a 1KB input and a typical pattern, expect <1ms total. For 1MB
inputs, expect ~10-50ms. Both are well within what's tolerable for
OLTP workloads.

## Why pure PL/SQL?

Pell was designed to run on managed Oracle instances (Autonomous
Database, RDS for Oracle, etc.) where Java SP and extproc are either
disabled or impractical. Pure PL/SQL is portable, comes with no
"works on prem only" footnotes, and runs identically on every
Oracle version pell targets.

Cost: ~10-100× slower than C-implemented PCRE. But for the patterns
and inputs typical of OLTP workloads (validation, extraction, log
parsing), the absolute timings stay in the sub-millisecond range,
where the overhead is invisible to users.
