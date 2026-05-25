---
title: Validate an email address
parent: Cookbook
nav_order: 1
---

## Problem

You have a `text` input that should look like an email address. You
want a `bool` answer: is it valid?

## Solution

```pell
module validation;

pub fn is_email(s: text) -> bool {
    return re::matches(s, /^[\w.+-]+@[\w-]+\.[\w.-]+$/);
}
```

`re::matches` returns `bool`. The pattern anchors with `^...$` so the
entire input has to match — partial matches don't count.

## How it lowers

```sql
CREATE OR REPLACE PACKAGE validation AS
  FUNCTION is_email(p_s IN VARCHAR2) RETURN BOOLEAN;
END validation;
/

CREATE OR REPLACE PACKAGE BODY validation AS
  FUNCTION is_email(p_s IN VARCHAR2) RETURN BOOLEAN IS
  BEGIN
    RETURN pell_re.matches(p_s, '^[\w.+-]+@[\w-]+\.[\w.-]+$');
  END is_email;
END validation;
/
```

## Use it

```pell
if is_email("alice@example.com") {
    log::info("looks good");
}

if !is_email("not-an-email") {
    log::info("nope");
}
```

## A more honest email regex

RFC 5322 is a tar pit. For most applications, the pattern above is
sufficient. If you want to match the spec more closely (at the cost
of complexity), use a richer pattern:

```pell
pub fn is_email_strict(s: text) -> bool {
    return re::matches(s, /^[\w!#$%&'*+\/=?^`{|}~.-]+@[\w-]+(\.[\w-]+)+$/);
}
```

For production, prefer **structural validation** (presence of `@`,
exactly one of them, domain has a `.`) over regex perfection — and
do the *real* validation by sending a confirmation email.

## Returning Option<text> instead

If you want to keep the validated value:

```pell
pub fn parse_email(s: text) -> Option<text> {
    if re::matches(s, /^[\w.+-]+@[\w-]+\.[\w.-]+$/) {
        return Some(lower(s));      // normalize to lowercase
    }
    return None;
}
```

## See also

- [Regex tutorial](../tutorial/11-regex.md) for the full `re::` surface.
- [Parse a phone number](./parse-phone-number.md) for the named-capture
  pattern.