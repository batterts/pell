# 10. JSON

Pell uses Oracle 23ai's native `JSON` datatype as a first-class
value. The surface covers construction, path access, record
round-tripping, and a fluent dot-chain builder for incremental
mutation.

## The `json` type

```pell
let j: json = json::object(name = "Alice", age = 30);
```

`json` is a primitive that lowers to `JSON` on 23ai and
`VARCHAR2(32767)` on 19c (with `--target 19c`). The surface stays the
same; the storage type changes.

## Construction

### `json::object(...)`

Build a JSON object from keyword arguments:

```pell
let j: json = json::object(
    action = "promote",
    who    = "alice",
    ts     = sysdate(),
);
```

lowers to:

```sql
JSON_OBJECT(
  'action' VALUE 'promote',
  'who'    VALUE 'alice',
  'ts'     VALUE SYSDATE
  RETURNING JSON
)
```

### `json::array(...)`

Build a JSON array from a list literal:

```pell
let j: json = json::array([1, 2, 3, 4, 5]);
let m: json = json::array(["alice", "bob", "carol"]);
```

lowers to:

```sql
JSON_ARRAY(1, 2, 3, 4, 5 RETURNING JSON)
JSON_ARRAY('alice', 'bob', 'carol' RETURNING JSON)
```

### `json::parse(text)`

Parse a JSON string:

```pell
let j: json = json::parse('{"name":"Alice","age":30}');
```

lowers to: `JSON('{"name":"Alice","age":30}')`.

### `json::stringify(json)`

Serialize a JSON value to text:

```pell
let s: text = json::stringify(j);
```

lowers to: `JSON_SERIALIZE(j)`.

## Path access

| Function                       | Lowers to                                                |
|--------------------------------|----------------------------------------------------------|
| `json::get_text(j, path)`      | `JSON_VALUE(j, path)`                                    |
| `json::get_number(j, path)`    | `JSON_VALUE(j, path RETURNING NUMBER)`                   |
| `json::get_json(j, path)`      | `JSON_QUERY(j, path)`                                    |
| `json::has(j, path)`           | `JSON_EXISTS(j, path)`                                   |

Example:

```pell
let name: text   = json::get_text(j, "$.name");
let age:  number = json::get_number(j, "$.age");
let kids: json   = json::get_json(j, "$.children");
let ok:   bool   = json::has(j, "$.user.email");
```

Missing paths return NULL per Oracle's JSON_VALUE default. Wrap with
`nvl(...)` if you want a default:

```pell
let label: text = nvl(json::get_text(j, "$.label"), "(unset)");
```

## Records ⇄ JSON

### `json::from(record_var)` — record → JSON

```pell
pub record User { name: text, age: number }

pub fn pack(u: User) -> json {
    return json::from(u);
}
```

lowers to:

```sql
JSON_OBJECT('name' VALUE p_u.name, 'age' VALUE p_u.age RETURNING JSON)
```

One JSON_OBJECT key per field. The key is the pell field name; the
value is the field's value, with type-appropriate coercion.

### `json::into::<Record>(j)` — JSON → record

```pell
pub fn unpack(j: json) -> User {
    let u: User = json::into::<User>(j);
    return u;
}
```

lowers to:

```sql
l_u.name := JSON_VALUE(p_j, '$.name');
l_u.age  := JSON_VALUE(p_j, '$.age' RETURNING NUMBER);
```

One assignment per record field. Fields with no matching JSON property
are left at NULL.

For richer typed JSON-to-record (named groups, etc.), see
[Regex](./11-regex.md) and [jq](./12-jq.md).

## Dot-chain construction

To build up a JSON value incrementally, use the `.set()` / `.remove()`
/ `.append()` method chain on a json-typed receiver:

```pell
pub fn build_user_profile() -> json {
    let j: json = json::object()
        .set("user.name", "Bob")
        .set("user.email", "bob@example.com")
        .set("user.address.city", "NYC")
        .set("user.address.zip", "10001")
        .set("meta.created", "today");
    return j;
}
```

The whole chain collapses to a single `JSON_TRANSFORM`:

```sql
SELECT JSON_TRANSFORM(
  JSON_OBJECT(RETURNING JSON),
    SET '$.user'         = JSON_OBJECT(RETURNING JSON) CREATE ON MISSING,
    SET '$.user.name'    = 'Bob',
    SET '$.user.email'   = 'bob@example.com',
    SET '$.user.address' = JSON_OBJECT(RETURNING JSON) CREATE ON MISSING,
    SET '$.user.address.city' = 'NYC',
    ...
) INTO l_j FROM dual;
```

Auto-vivify: nested paths (`user.address.city`) automatically create
their intermediate objects via `CREATE ON MISSING`. Shared prefixes
are deduplicated within the chain — you only get one `$.user`
creation even if you set `$.user.name`, `$.user.email`, and
`$.user.address.city`.

### Operations

```pell
let j: json = input
    .set("updated_at", "yes")    // SET path = value
    .remove("draft")             // REMOVE path
    .append("history", "edited"); // APPEND path = value (to array)
```

| Method               | JSON_TRANSFORM clause                |
|----------------------|---------------------------------------|
| `.set(path, value)`  | `SET '$.path' = value`                |
| `.remove(path)`      | `REMOVE '$.path'`                     |
| `.append(path, v)`   | `APPEND '$.path' = v`                 |

### Why SELECT INTO and not bare assignment?

Oracle's `JSON_TRANSFORM` with `SET` is a SQL-only construct — the
PL/SQL compiler doesn't accept it directly. Pell wraps the call in
`SELECT JSON_TRANSFORM(...) INTO target FROM dual` so the result
lands in the receiving local.

## Things NOT in the surface

- **Indexer sugar** `j["key"]` — pell has no indexer overloading;
  adding it just for json would be one-off and confusing against list
  indexing.
- **Direct dot notation** `j.name` — works in Oracle SQL contexts but
  uneven in PL/SQL across versions. Use `json::get_text(j, "$.name")`
  for portability.
- **JSON_MERGEPATCH** — `.merge(other)` isn't yet supported in
  dot-chains because it breaks the single-call collapse.
  Workaround: `json::object(a = j1, b = j2)` for now.
- **JSON Schema validation** — 23ai has `IS JSON VALIDATING`; pell
  doesn't yet expose it. Wrap in a CHECK constraint at DDL time.

## Where to go next

- [Regex](./11-regex.md) — pattern matching on string fields,
  including named-capture into typed records.
- [jq](./12-jq.md) — `jq!{}` macro for iterating JSON arrays with
  filters and projection.
