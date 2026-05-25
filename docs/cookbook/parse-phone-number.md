---
title: Parse a phone number into a typed record
parent: Cookbook
nav_order: 2
---

## Problem

You have phone numbers as text like `"555-123-4567"` and want them
parsed into a typed record with `area`, `prefix`, and `line` fields.

## Solution

```pell
module phone_book;

pub record Phone {
    area:   text,
    prefix: text,
    line:   text,
}

pub fn parse(s: text) -> Phone {
    let p: Phone = re::capture::<Phone>(
        s,
        /(?<area>\d{3})-(?<prefix>\d{3})-(?<line>\d{4})/
    );
    return p;
}
```

Each `(?<name>...)` group in the pattern maps to the record field of
the same name. The `re::capture::<Record>` lowering populates the
record field-by-field from the named-group result.

## How it lowers

```sql
FUNCTION parse(p_s IN VARCHAR2) RETURN t_phone IS
  l_p t_phone;
  l_pell_caps_0 pell_re.t_capture_map;
BEGIN
  l_pell_caps_0 := pell_re.capture_by_name(p_s,
    '(?<area>\d{3})-(?<prefix>\d{3})-(?<line>\d{4})');
  IF l_pell_caps_0.EXISTS('area')   THEN l_p.area   := l_pell_caps_0('area').match_text;   END IF;
  IF l_pell_caps_0.EXISTS('prefix') THEN l_p.prefix := l_pell_caps_0('prefix').match_text; END IF;
  IF l_pell_caps_0.EXISTS('line')   THEN l_p.line   := l_pell_caps_0('line').match_text;   END IF;
  RETURN l_p;
END parse;
```

If the input doesn't match the pattern, all three fields stay NULL.

## Use it

```pell
let p: Phone = phone_book::parse("555-123-4567");
log::info("({p.area}) {p.prefix}-{p.line}");
// → "(555) 123-4567"
```

## Handling failure

When the input doesn't match, returning a record with NULL fields is
a quiet failure mode. To make failure explicit:

```pell
pub error InvalidPhone { input: text }

pub fn parse_or_err(s: text) -> Result<Phone, InvalidPhone> {
    let p: Phone = re::capture::<Phone>(
        s, /(?<area>\d{3})-(?<prefix>\d{3})-(?<line>\d{4})/
    );
    if p.area == "" {
        return Err(InvalidPhone { input: s });
    }
    return Ok(p);
}
```

(Remember: in Oracle, the empty string `""` is NULL, so `p.area ==
""` is actually testing for NULL. See [Records and types](../tutorial/03-records-types.md#what-about-null--nil).)

## More formats

International numbers — let any format that contains 10 digits be
treated as US:

```pell
pub fn parse_loose(s: text) -> Phone {
    let p: Phone = re::capture::<Phone>(
        s, /(?<area>\d{3})\D*(?<prefix>\d{3})\D*(?<line>\d{4})/
    );
    return p;
}
```

`\D` is "any non-digit." This handles `"555 123 4567"`, `"5551234567"`,
`"(555) 123-4567"`, `"555.123.4567"`.

## See also

- [Regex tutorial: named captures](../tutorial/11-regex.md#named-captures-into-typed-records-the-headline-feature)
- [pell_re reference](../reference/pell-re.md)