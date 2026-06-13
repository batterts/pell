# pell

A statically-typed language that compiles to **Oracle PL/SQL** 19c / 23ai.
Modern syntax, real type checking, ergonomic SQL — and the emitted PL/SQL
is a deployable, human-readable, SHA-anchored artifact you commit, audit,
and `git blame` like any other code.

## Install

```sh
# Core compiler (zero deps)
pip install pell

# + Oracle driver + REPL (for `pell exec`, `pell repl`, `pell sql`, `pell deploy`)
pip install "pell[repl]"
```

After install, `pell` is on your PATH:

```sh
pell --help
pell build src/hello.pell
pell repl
```

## One look at the lowering

```pell
// src/hr/employees.pell
module hr.employees;

pub record Employee { id: number, name: text, level: number }
pub error NotFound { id: number }

pub fn promote(id: number) -> Result<Employee, NotFound> {
    let row: Employee = sql! {
        select id, name, level from employees where id = :id
    }.one()?;
    return Ok(Employee {
        id:    row.id,
        name:  row.name,
        level: row.level + 1,
    });
}
```

…lowers to PL/SQL you'd write by hand:

```sql
CREATE OR REPLACE PACKAGE hr.employees AS
  TYPE t_employee IS RECORD (
    id    NUMBER,
    name  VARCHAR2(4000),
    level NUMBER
  );
  FUNCTION promote(p_id IN NUMBER) RETURN t_employee;
END employees;
/

CREATE OR REPLACE PACKAGE BODY hr.employees AS
  FUNCTION promote(p_id IN NUMBER) RETURN t_employee IS
    l_row t_employee;
    l_ret t_employee;
  BEGIN
    SELECT id, name, level
      INTO l_row
      FROM employees
     WHERE id = p_id;
    l_ret.id    := l_row.id;
    l_ret.name  := l_row.name;
    l_ret.level := (l_row.level + 1);
    RETURN l_ret;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      RAISE pell_runtime.hr_employees_notfound;
  END promote;
END employees;
/
```

## Features

- **Generics & sum types** — `Result<T, E>`, `Option<T>`, `list<T>`
- **Real string interpolation** — `"got {count} tables"` with auto-stringify
  for any type
- **Embedded SQL** — `sql! { ... }` with `.one()` / `.collect()` / `.first()`
  terminators driving the right `SELECT INTO` shape
- **JSON / regex / jq-style queries** — first-class, using Oracle 23ai
  `JSON_OBJECT_T` natively
- **Notebook-style REPL** — variable persistence across cells,
  `\load` files, `catalog::` data dictionary helpers
- **Deploy & test** — `pell deploy` installs to Oracle with honest
  cross-schema error reporting; `pell test` runs utPLSQL `@suite` /
  `@test` modules and emits doc / junit / sonar / coverage reports
- **Step debugger** — breakpoints and variable inspection over
  `DBMS_DEBUG_JDWP` (plus an outbound-only `DBMS_DEBUG` transport for
  tunnels / NAT)
- **Deployment artifact** — `.sql` output is SHA-anchored, byte-stable
  with `--reproducible`, meant to be committed

## Commands

```sh
pell build <file|dir>     # compile to PL/SQL (-d <dir> for a tree, --target 19c)
pell deploy <file|dir>    # build + install on PELL_DB_URL (--debug, --keep-going)
pell test <suite>         # run a utPLSQL suite (--reporter, --ut-schema)
pell exec <script>        # run an anonymous-block script (--dry-run to just print)
pell repl                 # notebook-style REPL
pell sql <file>           # run a .sql file, statement by statement
pell srcmap <file>        # pell-line ↔ PL/SQL-line map (debugger support)
pell parse|tokens <file>  # AST / token stream (debugging the compiler)
```

## Learn more

- **Docs & tutorial:** https://batterts.github.io/pell
- **JetBrains IDE plugin:** https://plugins.jetbrains.com/plugin/31994-pell
- **Examples** (36 `.pell` files + their compiled output):
  https://github.com/batterts/pell/tree/main/compiler/examples
- **Source:** https://github.com/batterts/pell
- **Issues:** https://github.com/batterts/pell/issues

## License

Apache License 2.0 — see [LICENSE](https://github.com/batterts/pell/blob/main/LICENSE).
The PL/SQL the compiler emits is **not** a derivative work of pell and is
yours to license as you choose.
