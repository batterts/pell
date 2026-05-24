# Extract typed fields from a JSON document

## Problem

You have a `json` value (from a column, an API response, or a literal)
and you want its fields extracted into a typed pell record.

## Solution

Declare a record with the field shape you want, then use
`json::into::<Record>(j)`:

```pell
module users;

pub record UserProfile {
    name:    text,
    email:   text,
    age:     number,
    is_admin: bool,
}

pub fn parse_profile(j: json) -> UserProfile {
    let u: UserProfile = json::into::<UserProfile>(j);
    return u;
}
```

Each field of the record is populated by a JSON_VALUE call against
the matching path in the JSON.

## How it lowers

```sql
FUNCTION parse_profile(p_j IN JSON) RETURN t_userprofile IS
  l_u t_userprofile;
BEGIN
  l_u.name     := JSON_VALUE(p_j, '$.name');
  l_u.email    := JSON_VALUE(p_j, '$.email');
  l_u.age      := JSON_VALUE(p_j, '$.age' RETURNING NUMBER);
  l_u.is_admin := CASE WHEN JSON_VALUE(p_j, '$.is_admin') = 'true' THEN 1 ELSE 0 END;
  RETURN l_u;
END parse_profile;
```

One assignment per field. Missing fields stay NULL. Number and bool
fields get type-appropriate coercion.

## Nested fields

For nested JSON, use the field-by-field accessors directly:

```pell
pub record Address {
    city:    text,
    zipcode: text,
}

pub record UserWithAddress {
    name:    text,
    address: text,   // raw JSON as a sub-record path
}

pub fn extract(j: json) -> UserWithAddress {
    let u: UserWithAddress = UserWithAddress {
        name:    json::get_text(j, "$.name"),
        address: json::get_text(j, "$.address.city"),
    };
    return u;
}
```

For full sub-record extraction:

```pell
pub fn extract_address(j: json) -> Address {
    let nested: json = json::get_json(j, "$.address");
    let a: Address = json::into::<Address>(nested);
    return a;
}
```

`json::get_json` returns the nested object as a `json` value; then
`json::into::<Address>` populates the record from that.

## Iterating an array of records

Use `jq!{...}` to iterate an array and populate records:

```pell
pub fn extract_users(j: json) -> list<UserProfile> {
    let users: list<UserProfile> = jq!{ j | .users[] }.collect();
    return users;
}
```

This lowers to a JSON_TABLE-backed SELECT that materializes the
records — one column per UserProfile field.

See [jq tutorial](../tutorial/12-jq.md) for the full surface.

## Handling missing fields

Missing fields produce NULL. If you want a default:

```pell
pub fn parse_with_defaults(j: json) -> UserProfile {
    let u: UserProfile = UserProfile {
        name:     nvl(json::get_text(j, "$.name"), "(anonymous)"),
        email:    nvl(json::get_text(j, "$.email"), ""),
        age:      nvl(json::get_number(j, "$.age"), 0),
        is_admin: false,    // explicit default
    };
    return u;
}
```

Or do the defaulting at the SQL level inside `nvl(...)`.

## Detecting absence (vs NULL)

In JSON, "field missing entirely" and "field present but null" are
different. `json::has(j, "$.field")` distinguishes them:

```pell
if !json::has(j, "$.email") {
    log::warn("no email field at all");
} else if json::get_text(j, "$.email") == "" {
    log::warn("email field present but empty/null");
}
```

## See also

- [JSON tutorial](../tutorial/10-json.md)
- [jq tutorial](../tutorial/12-jq.md)
- [Parse a phone number](./parse-phone-number.md) — record extraction
  with regex (the other side of the typed-extraction coin).
