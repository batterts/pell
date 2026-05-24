# 11. Regex with re:: and /pattern/

Oracle's `regexp_*` functions are POSIX-flavored: no `\d`, no `\w`, no
named groups, awkward capture handling. Pell ships `pell_re`, a pure
PL/SQL Thompson-NFA regex engine, and a `re::` namespace that wraps it.
This chapter covers the surface.

## Setup

Install the `pell_re` runtime package once per schema:

```sh
./pell runtime --re > pell_re.sql       # or just runtime/pell_re.sql in this repo
# Install on Oracle:
sqlplus user/pass @pell_re.sql
```

The engine has no external dependencies — pure PL/SQL with bytecode in
RAW(32767). Compiled patterns are cached per session.

## The `/pattern/` literal

Patterns are written with JavaScript-style delimiters:

```pell
let ok: bool = re::matches(s, /\d{3,5}/);
```

Inside `/.../`:
- `\d \w \s \b` etc. work as expected — backslashes preserved.
- `{n,m}` quantifiers work — braces preserved.
- `\/` is a literal `/`. `\\` is a literal `\`.
- Newlines are not allowed; regex literals are single-line.

### Disambiguation from division

Pell uses JavaScript's rule: after a value-producing token (IDENT,
NUMBER, STRING, `)`, `]`, `}`), `/` is division; everything else
means a regex starts here.

```pell
let count = 10 / 2;                    // division
let pat   = /\d+/;                      // regex (after `=`)
re::find(s, /\d+/);                     // regex (after `,`)
let half  = items.len() / 2;            // division (after `)`)
return n / 2;                           // division (`n` is a value)
return /\d+/;                           // regex (after `return`)
```

If you ever need a regex in a "division-ish" position, use a
backtick raw string: `re::find(s, \`\d+\`)`.

## The `re::` surface

```pell
re::matches(s, /pat/)              // bool
re::find(s, /pat/[, pos])          // text — first match or NULL
re::find_all(s, /pat/)             // list<text>
re::replace_all(s, /pat/, rep)     // text — literal replacement
re::split(s, /pat/)                // list<text>
re::capture::<Record>(s, /pat/)    // Record (named groups → fields)
```

Examples:

```pell
pub fn is_email(s: text) -> bool {
    return re::matches(s, /\w+@\w+\.\w+/);
}

pub fn first_number(s: text) -> text {
    return re::find(s, /\d+/);
}

pub fn all_numbers(s: text) -> list<text> {
    return re::find_all(s, /\d+/);
}

pub fn redact_digits(s: text) -> text {
    return re::replace_all(s, /\d/, "*");
}

pub fn words(s: text) -> list<text> {
    return re::split(s, /\s+/);
}
```

## Named captures into typed records — the headline feature

This is what `regexp_substr` can't do:

```pell
pub record Phone {
    area:   text,
    prefix: text,
    line:   text,
}

pub fn parse_phone(s: text) -> Phone {
    let p: Phone = re::capture::<Phone>(
        s,
        /(?<area>\d{3})-(?<prefix>\d{3})-(?<line>\d{4})/
    );
    return p;
}
```

```pell
let p: Phone = parse_phone("555-123-4567");
log::info("{p.area}-{p.prefix}-{p.line}");   // 555-123-4567
```

The lowering calls `pell_re.capture_by_name(s, pat)` once and assigns
each record field from the named-group result:

```sql
DECLARE
  l_pell_caps_0 pell_re.t_capture_map;
BEGIN
  l_pell_caps_0 := pell_re.capture_by_name(p_s, '(?<area>\d{3})-...');
  IF l_pell_caps_0.EXISTS('area') THEN
    l_p.area := l_pell_caps_0('area').match_text;
  END IF;
  IF l_pell_caps_0.EXISTS('prefix') THEN
    l_p.prefix := l_pell_caps_0('prefix').match_text;
  END IF;
  IF l_pell_caps_0.EXISTS('line') THEN
    l_p.line := l_pell_caps_0('line').match_text;
  END IF;
END;
```

Group names map to record fields by name. Groups without a matching
field are dropped; fields without a matching group stay NULL.

## Supported regex syntax

| Construct       | Description                                        |
|-----------------|----------------------------------------------------|
| `.`             | any char except `\n`                               |
| `*` `+` `?`     | greedy quantifiers                                 |
| `{n}` `{n,}` `{n,m}` | counted quantifiers                           |
| `|`             | alternation                                        |
| `(...)`         | capturing group                                    |
| `(?:...)`       | non-capturing group                                |
| `(?<name>...)`  | named capture                                      |
| `[abc]` `[^abc]`| character class, negated class                     |
| `[a-z]`         | range                                              |
| `\d \D \w \W \s \S` | shorthand classes (ASCII)                      |
| `^` `$`         | line start / end                                   |
| `\A` `\z`       | string start / end                                 |
| `\b` `\B`       | word boundary / non-boundary                       |
| `\n \r \t`      | escape sequences                                   |

### Not supported (v0)

- **Backreferences** `\1` — would need backtracking matcher.
- **Lookbehinds** `(?<=...)` `(?<!...)` — same reason.
- **Possessive quantifiers** `*+` `++` `?+`.
- **Unicode property classes** `\p{L}`.
- **`/pattern/flags`** — flags after the closing slash. Use inline
  modifiers in the pattern instead: `/(?i)foo/` (when the engine
  grows inline-modifier support; not yet).

## Cache + performance

Each call goes through a session-cached compile:

1. First call with a new pattern → parse → compile to bytecode →
   store in `g_cache` keyed by `STANDARD_HASH(pattern, 'SHA256')`.
2. Subsequent calls with the same pattern → cache hit, skip compile.

Typical pattern + short input: well under a millisecond. Pathological
patterns can't catastrophically backtrack — Pike's algorithm is
guaranteed linear in input length. For 1MB inputs and complex
patterns, expect ~10ms.

Call `pell_re.clear_cache` to flush the session cache. Useful when
churning through many one-off patterns (rare).

## Mixing with Oracle's regexp

You can still use Oracle's `REGEXP_*` functions inside `sql!{}` for
the cases where they're sufficient and a SQL-only path is desirable
(e.g., index-only operations):

```pell
let active: list<text> = sql! {
    select name from users where regexp_like(email, '\w+@\w+\.\w+')
}.collect();
```

`re::` and `regexp_*` don't share state, so you can mix freely.

## Where to go next

- [jq](./12-jq.md) — `jq!{}` macro that uses regex-like syntax for
  iterating JSON documents.
- [Reference: pell_re](../reference/pell-re.md) — full API of the
  runtime package, including bytecode-level details if you're curious.
