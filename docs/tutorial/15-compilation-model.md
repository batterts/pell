# 15. The compilation model

This chapter zooms out to show how the pieces fit together: from
`.pell` source through to the multi-file PL/SQL deployment, with notes
on packages, runtime, schema layout, and dependency tracking.

## What `pell build` produces

For a single module file:

```
hello.pell                          (your input)
  ↓ pell build
hello.sql                           (one CREATE PACKAGE + CREATE PACKAGE BODY)
```

For a directory of modules:

```
my_app/
├── employees.pell    →  employees.sql
├── billing.pell      →  billing.sql
└── reports.pell      →  reports.sql
```

Each module produces one PL/SQL file with its own package spec + body.
No bundling, no cross-file optimization — each module is compiled
independently.

Schema-level items (OBJECT TYPE for `pub type`, `pub sealed type`,
`pub aggregate`, and the OBJECT TABLE TYPES backing `@pipelined`) are
emitted **before** the package spec, as separate CREATE TYPE
statements. The output file orders them correctly:

```
CREATE OR REPLACE TYPE money AS OBJECT (...);
/
CREATE OR REPLACE TYPE money_list AS TABLE OF money;
/
CREATE OR REPLACE PACKAGE money_pkg AS ...; END money_pkg;
/
CREATE OR REPLACE PACKAGE BODY money_pkg AS ...; END money_pkg;
/
```

## The runtime package

If your module declares pell `error`s, those compile to PRAGMA
EXCEPTION_INIT entries in a shared `pell_runtime` package. Generate it
with:

```sh
pell runtime > pell_runtime.sql
```

The `runtime` subcommand walks your project directory and aggregates
every `error` declaration into a single `pell_runtime` package
containing all the EXCEPTIONS plus a `set_err` / `get_err` /
`clear_err` triple for stashing error payloads in SYS_CONTEXT:

```sql
CREATE OR REPLACE CONTEXT pell_err USING pell_runtime;

CREATE OR REPLACE PACKAGE pell_runtime AS
  PROCEDURE set_err(p_key IN VARCHAR2, p_payload IN VARCHAR2);
  PROCEDURE clear_err(p_key IN VARCHAR2);
  FUNCTION  get_err(p_key IN VARCHAR2) RETURN VARCHAR2;

  hr_employees_notfound EXCEPTION;
  PRAGMA EXCEPTION_INIT(hr_employees_notfound, -20100);
  ...
END pell_runtime;
/
```

Install order: `pell_runtime.sql` first, then individual module
files. Pell module files can `RAISE pell_runtime.<exception>` and that
EXCEPTION_INIT mapping makes SQLCODE = -20100 (etc.) trigger the
right WHEN.

You also need to install `pell_re.sql` (for any module using regex):

```sh
# in this repo
sqlplus user/pass @runtime/pell_re.sql
```

`pell_re` has no dependencies on `pell_runtime`; install order between
them doesn't matter.

## Deployment order

A clean install:

1. `pell_runtime.sql` — once per schema.
2. `pell_re.sql` — once per schema (if any module uses regex).
3. Each module's `.sql` file — order matters only if modules
   reference each other; pell doesn't infer the topology, you do.

Pell does not currently emit a `deploy.sql` master script that lists
everything in the right order. The convention is to maintain that
script by hand, or use your build system's dependency declarations.

## Anonymous-block mode (`pell exec` / `pell repl`)

When you run `pell exec`, the emit mode flips:

- No CREATE PACKAGE — everything goes into a single `DECLARE`/`BEGIN`/
  `END;` block.
- `record` decls become `TYPE ... IS RECORD` inside `DECLARE`.
- `fn` items become nested local subprograms.
- Top-level statements become the body of a synthetic
  `pell_anon_main` procedure that `BEGIN` calls.

The block executes once per `pell exec` invocation, leaving no trace
on the schema. This is great for quick experiments but can't host
schema-level objects (OBJECT TYPE, TABLE TYPE, aggregates) — those
need `CREATE TYPE` and `pell exec` errors out with a clear message
pointing you at `pell build`.

## Dependency tracking

Static SQL inside `sql!{}` blocks is fully visible to Oracle. After
installing your package, `ALL_DEPENDENCIES` will show every
table/view/sequence/package the module references:

```sql
SELECT name, referenced_name, referenced_type
  FROM all_dependencies
 WHERE owner = 'HR' AND name = 'EMPLOYEES_PKG';
```

This is what makes "drop a column → packages depending on it become
INVALID" work.

Dynamic SQL inside `exec_dyn(...)` is opaque to Oracle by default —
the SQL is a string. To make `ALL_DEPENDENCIES` see them, declare
`@touches(...)` on the unsafe fn:

```pell
unsafe fn count_for(tname: text) -> number
    @touches(employees, departments)
{
    ...
}
```

Pell emits "pinning cursors" — never-called CURSOR declarations with
`WHERE 1=0` — so Oracle's parser sees the table reference and
records the dependency. See [Dynamic SQL](./09-dynamic-sql.md).

## Compiler internals at a glance

```
.pell sources
  ↓ lexer (pell/lexer.py)
token stream
  ↓ parser (pell/parser.py)
AST (pell/ast.py)
  ↓ emitter (pell/emitter.py)
PL/SQL text (CREATE PACKAGE / BODY / TYPE / etc.)
```

There's no separate intermediate representation — the AST goes
straight to PL/SQL text via the emitter. The emitter is the largest
file (~4200 lines as of this writing) and most of the language's
behavior lives there.

The `pell_re` regex engine is a different animal: it's pure PL/SQL
runtime code (no Python), installed once and queried at execution
time. Its bytecode-compile-and-cache scheme is described in
[Reference: pell_re](../reference/pell-re.md).

## Reproducible builds

Add `--reproducible` to omit volatile metadata (build timestamp,
uncommitted-tree hash) from the file header. The output then depends
only on the source content + compiler git SHA. Useful for:

- CI byte-comparing emitted SQL against committed snapshots.
- Diffing PRs cleanly — no "build timestamp" noise.

The test suite uses this for the `expected/` snapshots.

## The shape of a real deployment

A medium-size pell project might lay out like:

```
my_app/
├── README.md
├── compile.sh
├── deploy.sh
├── pell/                       (the compiler — vendored or via PYTHONPATH)
├── src/
│   ├── auth.pell
│   ├── billing.pell
│   ├── reports.pell
│   └── inventory.pell
├── built/                      (gitignored — `pell build src -d built`)
│   ├── auth.sql
│   ├── billing.sql
│   ├── reports.sql
│   ├── inventory.sql
│   ├── pell_runtime.sql
│   └── pell_re.sql
└── tests/
    ├── test_auth.pell
    └── test_billing.pell
```

`compile.sh` runs `pell build` + `pell runtime`. `deploy.sh` connects
to Oracle and applies the files in order. The pell source is the
source of truth; the `.sql` is the deployable artifact (gitignored
or stored separately depending on your team's convention).

## You've finished the tutorial

If you've read every chapter, you now know:

- How to declare modules, functions, records, and types.
- How to talk to the database with `sql!{}` and consume results.
- How to handle errors with typed `Result<T, E>` and `@retry`.
- How to use the advanced features: aggregates, pipelined functions,
  dynamic SQL, JSON, regex, jq.
- How to iterate interactively with `pell exec` and `pell repl`.

Reach for the [Reference](../reference/) pages when you need exact
behavior on a specific feature, and the [Cookbook](../cookbook/) for
worked solutions to common problems.
