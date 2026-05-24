# pell_runtime — errors and context

`pell_runtime` is a package that pell generates from the union of
every `error` declaration across your project. It contains:

- One `EXCEPTION` + `PRAGMA EXCEPTION_INIT` per pell error type.
- A `set_err / get_err / clear_err` triple for stashing error payloads
  in SYS_CONTEXT.

You install it once per schema. Module code that raises pell errors
references `pell_runtime.<exception_name>` and the EXCEPTION_INIT
mapping ties it to a specific SQLCODE.

## Generating the package

```sh
./pell runtime > pell_runtime.sql
# Then install:
sqlplus user/pass @pell_runtime.sql
```

The `runtime` subcommand walks the directory (or specific file) and
aggregates every `error` it finds. Re-run it whenever you add a new
error type and reinstall.

You can target a single source path:

```sh
./pell runtime src/ -o build/pell_runtime.sql
```

## Generated structure

```sql
-- Context for stashing error payloads, scoped to pell_runtime
CREATE OR REPLACE CONTEXT pell_err USING pell_runtime;

CREATE OR REPLACE PACKAGE pell_runtime AS
  PROCEDURE set_err(p_key IN VARCHAR2, p_payload IN VARCHAR2);
  PROCEDURE clear_err(p_key IN VARCHAR2);
  FUNCTION  get_err(p_key IN VARCHAR2) RETURN VARCHAR2;

  -- One per pell error declaration
  hr_employees_notfound EXCEPTION;
  PRAGMA EXCEPTION_INIT(hr_employees_notfound, -20100);

  billing_charges_overdraft EXCEPTION;
  PRAGMA EXCEPTION_INIT(billing_charges_overdraft, -20102);
  ...
END pell_runtime;
/

CREATE OR REPLACE PACKAGE BODY pell_runtime AS
  PROCEDURE set_err(p_key IN VARCHAR2, p_payload IN VARCHAR2) IS
  BEGIN DBMS_SESSION.SET_CONTEXT('pell_err', p_key, p_payload); END;

  PROCEDURE clear_err(p_key IN VARCHAR2) IS
  BEGIN DBMS_SESSION.CLEAR_CONTEXT('pell_err', p_key); END;

  FUNCTION get_err(p_key IN VARCHAR2) RETURN VARCHAR2 IS
  BEGIN RETURN SYS_CONTEXT('pell_err', p_key); END;
END pell_runtime;
/
```

## Exception naming

Pell-generated exception names follow the pattern:

```
<schema>_<package>_<errortype>
```

Lowercased, joined by underscores. For an error `NotFound` declared
in module `hr.employees`, the exception becomes
`hr_employees_notfound`.

## SQLCODE ranges

| Category    | SQLCODE range     | Behavior with @retry |
|-------------|-------------------|----------------------|
| `@skip`     | -20100 to -20199  | exit loop normally   |
| (propagate) | -20200 to -20299  | re-raise after budget|
| `@panic`    | -20300 to -20399  | re-raise immediately |

Pell assigns SQLCODES sequentially within each range as it
encounters errors. The assignments are stable: re-running
`pell runtime` on the same input produces the same numbers.

## Raising errors from pell

You don't write `RAISE` directly. Use `Err(...)` in pell:

```pell
pub fn lookup(id: number) -> Result<text, NotFound> {
    if some_check_fails {
        return Err(NotFound { id: id });
    }
    return Ok(...);
}
```

The lowering uses `pell_runtime.set_err` to stash the JSON-serialized
payload, then `RAISE pell_runtime.<exception>`:

```sql
pell_runtime.set_err('hr_employees_notfound', '{"id":' || p_id || '}');
RAISE pell_runtime.hr_employees_notfound;
```

A caller's `WHEN OTHERS` (inside `@retry` or `finally`) sees the
exception by name; `pell_runtime.get_err(SQLCODE-keyed-thing)` can
recover the payload if needed.

## Catching errors elsewhere

In SQL\*Plus or other client code that calls pell-emitted packages,
the error surfaces as a normal ORA-20100 (or whatever code), and the
payload is in SYS_CONTEXT('pell_err', '<exception_name>'):

```sql
BEGIN
  hr.employees.lookup(:p_id);
EXCEPTION
  WHEN OTHERS THEN
    DBMS_OUTPUT.PUT_LINE('caught: ' || SQLERRM);
    DBMS_OUTPUT.PUT_LINE('payload: ' ||
      SYS_CONTEXT('pell_err', 'hr_employees_notfound'));
END;
```

For library code (other pell modules), you don't write `WHEN OTHERS`
yourself — let the error propagate up, and let `@retry` or `finally`
in a higher-level fn handle it. See [Errors and @retry](../tutorial/06-errors-and-retry.md).

## When to regenerate

Anytime you:

- Add a new `error` declaration to a module.
- Remove an error (regenerate so SYS_CONTEXT keys don't go stale).
- Change an error's category (`@skip` / `@panic` / unmarked) — the
  SQLCODE moves between ranges.

You can build `pell_runtime` regeneration into your CI / deploy
script:

```sh
pell runtime src/ -o build/pell_runtime.sql
diff -q build/pell_runtime.sql current_pell_runtime.sql && echo "no change" || apply_to_db
```

## A note on global state

`pell_runtime` is a single package shared across the schema. That
means every pell module's errors go in the same namespace. The
`<schema>_<package>_` prefix prevents name collisions, but be aware
that:

- All modules in a schema share the SQLCODE space.
- Reinstalling `pell_runtime` invalidates dependent packages until
  they recompile. This is normal Oracle behavior; usually a no-op
  if the spec didn't change.

If you want strict isolation, use separate schemas per module group
and install one `pell_runtime` per schema.
