---
title: "REPL deep dive"
parent: Tutorial
nav_order: 16
---

# REPL deep dive

The pell REPL is a notebook-style terminal for exploring Oracle
interactively. Each cell is a pell snippet — definitions accumulate,
variables persist, output prints inline. This tutorial walks through
everything the REPL can do, from first launch to advanced patterns.

If you just want the quick-start, see
[pell exec and pell repl](./14-exec-and-repl.md). This page is the
comprehensive reference.

---

## Launching the REPL

### From the terminal

```sh
# Requires the [repl] extra:
#   cd compiler && pip install -e .[repl]

# Connect via environment variable (recommended):
export PELL_DB_URL='user/pass@host:1521/service'
./pell repl

# Or pass inline:
./pell repl -c 'user/pass@host:1521/service'

# Start without a connection (for offline exploration):
./pell repl
# → "(no connection — use \connect user/pass@host:port/service to attach)"
```

### From IntelliJ (with the pell plugin)

Two ways:

1. **Pell tool window** → **Open pell REPL** toolbar button — opens a
   terminal tab with `./pell repl` running.
2. **Gutter icon on any `.pell` file** → **Open pell REPL (load this
   file)** — opens the REPL *and* auto-runs `\load <file>` so the
   file's definitions are immediately available.

Both use IntelliJ's built-in terminal (real TTY), so prompt_toolkit's
multi-line editing works correctly.

---

## Cell basics

The REPL shows a numbered prompt. Type pell code, then press Enter on
a blank line (or Ctrl-D) to submit:

```
[1]> let x: number = 42;
    >
```

The blank line after `let x: number = 42;` submits the cell. Output
(if any) prints below, then the next prompt appears:

```
[2]> dbms_output::put_line("x = {x}");
    >
x = 42
[3]>
```

### Multi-line cells

Just keep typing. The continuation prompt `    >` shows you're still
in the same cell:

```
[3]> fn fizzbuzz(n: number) -> text {
    >     if n % 15 == 0 { return "FizzBuzz"; }
    >     if n % 3 == 0  { return "Fizz"; }
    >     if n % 5 == 0  { return "Buzz"; }
    >     return "{n}";
    > }
    >
```

Submit with a blank line. The function is now in the session — use it
in subsequent cells.

---

## What persists across cells

### Definitions (always)

Functions, records, errors, enums, and sequences declared in any cell
stay in the running session module. Later cells can reference them:

```
[1]> record Employee { id: number, name: text, level: number }
    >
[2]> fn promote(e: Employee) -> Employee {
    >     return Employee { id: e.id, name: e.name, level: e.level + 1 };
    > }
    >
[3]> let alice: Employee = Employee { id: 1, name: "Alice", level: 5 };
    > let promoted: Employee = promote(alice);
    > dbms_output::put_line("{promoted.name} → level {promoted.level}");
    >
Alice → level 6
```

Re-declaring a definition with the same name replaces it — iterate
on a function without restarting:

```
[4]> fn promote(e: Employee) -> Employee {
    >     return Employee { id: e.id, name: e.name, level: e.level + 2 };
    > }
    >
```

Now `promote` adds 2 instead of 1.

### Variables (via offline snapshots)

`let` variables persist across cells. After each cell executes
successfully, the REPL captures every variable's value via
`DBMS_OUTPUT` and stores it on the Python side. The next cell gets
the actual computed value replayed as a literal — no re-execution of
the original expression.

```
[1]> let x: number = 42;
    >
[2]> let y: number = x * 2;
    >
[3]> dbms_output::put_line("x={x}, y={y}");
    >
x=42, y=84
```

Cell 3's anonymous block starts with `l_x := 42; l_y := 84;`
(literal replays from the snapshot) before executing the
`dbms_output` call.

### What types survive

| Type        | Capture method                              | Replay method          |
|-------------|---------------------------------------------|------------------------|
| `number`    | `TO_CHAR(x)`                                | `42` literal           |
| `text`      | `TO_CHAR(x)`                                | `'value'` literal      |
| `bigtext`   | `TO_CHAR(x)`                                | `'value'` literal      |
| `bool`      | `TO_CHAR` → `TRUE`/`FALSE`                  | `TRUE`/`FALSE`         |
| `json`      | `JSON_SERIALIZE(x)`                         | `json::parse('...')`   |
| `date`      | `TO_CHAR(x, 'YYYY-MM-DD HH24:MI:SS')`      | `TO_DATE('...')`       |
| `timestamp` | `TO_CHAR(x, 'YYYY-MM-DD HH24:MI:SS.FF9')`  | `TO_TIMESTAMP('...')`  |
| `bytes`     | `RAWTOHEX(x)`                               | `HEXTORAW('...')`      |

**Not yet supported**: records (need per-field capture), lists (need
iteration), cursors (live server state — fundamentally can't serialize).
For those, re-declare in each cell that needs them.

### Overwriting a variable

Re-declaring a variable replaces its snapshot:

```
[4]> let x: number = 100;
    >
[5]> dbms_output::put_line("x is now {x}");
    >
x is now 100
```

---

## Auto-stringify

You don't have to manually convert types to text for logging or
interpolation. The compiler wraps automatically:

```
[1]> let n: number = 42;
    > let d: date = sysdate();
    > let j: json = json::object(name = "Alice", age = 30);
    > let flag: bool = true;
    >
[2]> dbms_output::put_line(n);
42

[3]> dbms_output::put_line(j);
{"name":"Alice","age":30}

[4]> dbms_output::put_line(d);
2026-05-26 14:30:00

[5]> dbms_output::put_line(flag);
true

[6]> dbms_output::put_line("n={n}, d={d}, j={j}, flag={flag}");
n=42, d=2026-05-26 14:30:00, j={"name":"Alice","age":30}, flag=true
```

Works for `log::info` / `log::warn` / `log::error` too — same
auto-wrap. For the explicit version, use `.to_text()`:

```
[7]> let s: text = j.to_text();
    > dbms_output::put_line(s);
    >
{"name":"Alice","age":30}
```

---

## Slash commands

Type a backslash command at the start of a cell (single-line,
submitted immediately):

| Command               | What it does                                                        |
|-----------------------|---------------------------------------------------------------------|
| `\help`               | Print the command list                                              |
| `\quit` / `\q`       | Exit the REPL                                                       |
| `\reset`              | Clear all accumulated definitions + variable snapshots              |
| `\show`               | Print the anonymous PL/SQL block that would run on the next cell    |
| `\save <file>`        | Dump the running session as PL/SQL                                  |
| `\load <file>`        | Append a `.pell` file's definitions into the session                |
| `\sql <statement>`    | Run one raw SQL statement and print results                         |
| `\connect <dsn>`      | Reconnect to a different database                                   |

### `\load` — seeding from a file

```
[1]> \load compiler/examples/28_repl_demo.pell
  loaded 4 defs from compiler/examples/28_repl_demo.pell
[2]> dbms_output::put_line(greet("shaun"));
hello, shaun!
```

The file's `fn`s and `record`s merge into the session; you can use
them immediately. Top-level statements in the file are ignored (only
definitions are absorbed).

### `\sql` — raw SQL passthrough

```
[3]> \sql select count(*) from user_objects
count(*)
--------
47
  (1 row)

[4]> \sql select object_name, object_type from user_objects where rownum <= 5
object_name                    | object_type
-------------------------------+-----------
HELLO                          | PACKAGE
HELLO                          | PACKAGE BODY
PELL_RE                        | PACKAGE
PELL_RE                        | PACKAGE BODY
RE_DEMO                        | PACKAGE
  (5 rows)
```

SELECT results print as a tabular layout. DML prints the affected
row count:

```
[5]> \sql insert into demo_log(event) values ('repl test')
  ok (1 row)
```

### `\show` — inspect the anonymous block

```
[6]> \show
DECLARE
  TYPE t_employee IS RECORD (
    id NUMBER,
    name VARCHAR2(4000),
    level NUMBER
  );

  FUNCTION promote(p_e IN t_employee) RETURN t_employee IS
    ...
  END promote;

  PROCEDURE pell_anon_main IS
    l_x NUMBER;
  BEGIN
    l_x := 42;
  END pell_anon_main;
BEGIN
  pell_anon_main;
END;
/
```

This shows what the REPL would emit for the NEXT cell, including all
accumulated definitions and snapshotted variables. Useful for
debugging "why isn't my variable in scope?" or "what type did pell
infer?"

### `\reset` — start fresh

```
[7]> \reset
  (session cleared — defs + variables)
[8]>
```

Clears everything: function definitions, record types, error types,
and variable snapshots. The Oracle connection stays open.

---

## Working with JSON

The REPL is a great place to explore JSON construction and querying:

```
[1]> let j: json = json::object(
    >     first_name = "shaun",
    >     last_name  = "batterton",
    >     role       = "architect",
    > );
    >
[2]> dbms_output::put_line(j);
{"first_name":"shaun","last_name":"batterton","role":"architect"}

[3]> dbms_output::put_line(json::get_text(j, "$.role"));
architect

[4]> let pretty: text = sql! {
    >     select json_serialize(:j returning varchar2 pretty) from dual
    > }.one();
    > dbms_output::put_line(pretty);
    >
{
  "first_name" : "shaun",
  "last_name" : "batterton",
  "role" : "architect"
}
```

### Dot-chain builder

```
[5]> let profile: json = json::object()
    >     .set("user.name", "shaun")
    >     .set("user.email", "shaun@example.com")
    >     .set("meta.created", "2026-05-26");
    > dbms_output::put_line(profile);
    >
{"user":{"name":"shaun","email":"shaun@example.com"},"meta":{"created":"2026-05-26"}}
```

The JSON variable persists — use it in subsequent cells:

```
[6]> let name: text = json::get_text(profile, "$.user.name");
    > dbms_output::put_line("Hello, {name}!");
    >
Hello, shaun!
```

---

## Working with regex

```
[1]> let email: text = "alice@example.com";
    >
[2]> dbms_output::put_line(re::matches(email, /\w+@\w+\.\w+/));
true

[3]> let nums: list<text> = re::find_all("order #42, item #7, qty #100", /\d+/);
    > for n in nums { dbms_output::put_line("found: {n}"); }
    >
found: 42
found: 7
found: 100
```

### Named captures

```
[4]> record Phone { area: text, prefix: text, line: text }
    >
[5]> let p: Phone = re::capture::<Phone>(
    >     "555-123-4567",
    >     /(?<area>\d{3})-(?<prefix>\d{3})-(?<line>\d{4})/
    > );
    > dbms_output::put_line("({p.area}) {p.prefix}-{p.line}");
    >
(555) 123-4567
```

---

## Querying the database

Any `sql!{}` block works in the REPL just as it does in compiled code:

```
[1]> for row in sql! {
    >     select object_name, object_type
    >       from user_objects
    >      where object_type = 'PACKAGE'
    >      order by object_name
    >      fetch first 5 rows only
    > } {
    >     dbms_output::put_line("{row.object_name} ({row.object_type})");
    > }
    >
HELLO (PACKAGE)
PELL_RE (PACKAGE)
PELL_RUNTIME (PACKAGE)
RE_DEMO (PACKAGE)
RE_CAPTURE_DEMO (PACKAGE)
```

### DML

```
[2]> sql! { insert into demo_log(event, ts) values ('repl-test', sysdate) };
    >

[3]> let count: number = sql! {
    >     select count(*) from demo_log where event = 'repl-test'
    > }.one();
    > dbms_output::put_line("rows: {count}");
    >
rows: 1
```

Note: DML in the REPL is auto-committed at the end of each `pell
deploy` / `pell sql` run. In the REPL itself, each cell's anonymous
block runs in the connection's current transaction — call `\sql
COMMIT` or `\sql ROLLBACK` to control it explicitly.

---

## Calling deployed packages

Anything you've installed via `pell build` + `pell sql` (or `pell
deploy`) is callable by its package name:

```
[1]> dbms_output::put_line(hello::greet("world"));
    >
hello, world from mypell
```

Cross-module calls use `::` which lowers to Oracle's `.`:

```
[2]> dbms_output::put_line(re_demo::first_number("abc 42 def"));
    >
42
```

---

## Workflow patterns

### Prototype → deploy

1. Open the REPL.
2. Sketch a function in a cell. Test it.
3. Iterate: re-define, re-test.
4. When happy, `\save sketch.pell` to dump the session.
5. Edit `sketch.pell` into a proper module file (add `module
   my_module;` header, clean up).
6. `pell deploy` to install.

### Explore a schema

```
[1]> \sql select table_name from user_tables order by 1
[2]> \sql select column_name, data_type from user_tab_columns where table_name = 'EMPLOYEES'
[3]> for row in sql! { select id, name from employees fetch first 3 rows only } {
    >     dbms_output::put_line("{row.id}: {row.name}");
    > }
```

### Debug a deployed package

```
[1]> \load src/billing.pell
  loaded 12 defs from src/billing.pell
[2]> // Now call any fn from the module as a local:
    > let result: number = calculate_total(order_id = 42);
    > dbms_output::put_line("total = {result}");
```

### JSON pipeline

```
[1]> let raw: text = sql! {
    >     select payload from api_responses where id = 1
    > }.one();
    >
[2]> let j: json = json::parse(raw);
    > dbms_output::put_line(json::get_text(j, "$.status"));
    >
ok

[3]> let items: list<text> = jq!{ j | .items[] | .name }.collect();
    > for item in items { dbms_output::put_line("  - {item}"); }
    >
  - widget-a
  - widget-b
  - widget-c
```

---

## Tips and gotchas

### `sql!{...}.one()` can't be inlined as an argument

This fails:

```
dbms_output::put_line(sql!{ select count(*) from users }.one());
```

The `.one()` lowering needs a target variable. Split into two
statements:

```
let n: number = sql!{ select count(*) from users }.one();
dbms_output::put_line(n);
```

### `log::info` needs a `log` package

`log::*` isn't a pell builtin — it's a convention. If you see
`PLS-00225: subprogram or cursor 'LOG' reference is out of scope`,
install a tiny `log` package:

```sql
CREATE OR REPLACE PACKAGE log AS
  PROCEDURE info(msg IN VARCHAR2);
  PROCEDURE warn(msg IN VARCHAR2);
  PROCEDURE error(msg IN VARCHAR2);
END log;
/
CREATE OR REPLACE PACKAGE BODY log AS
  PROCEDURE info(msg IN VARCHAR2) IS
  BEGIN dbms_output.put_line('[INFO] ' || msg); END;
  PROCEDURE warn(msg IN VARCHAR2) IS
  BEGIN dbms_output.put_line('[WARN] ' || msg); END;
  PROCEDURE error(msg IN VARCHAR2) IS
  BEGIN dbms_output.put_line('[ERROR] ' || msg); END;
END log;
/
```

Save that to `log.sql` and install with `pell sql log.sql` (or
`\sql @log.sql` from the REPL — but multi-statement `@` isn't
supported; use the CLI).

### Empty string is NULL in Oracle

```
let s: text = "";
if s == "" {
    // This NEVER executes — both sides are NULL, and NULL = NULL is unknown
}
```

Use an explicit `seen` flag or test with `s.is_empty()`.

### Schema-qualified modules need DBA privileges

If your `.pell` file says `module hr.employees;`, the deploy tries to
create in schema `hr`. Your REPL user needs `CREATE ANY PROCEDURE` or
access to that schema. For sandbox use, prefer single-segment module
names (`module employees;`) so everything installs to the current
user's schema.

---

## Slash-command quick reference

```
\help                     — this list
\quit / \q / \exit        — exit
\reset                    — clear defs + variables
\show                     — print the anon block for the next cell
\save <file>              — dump session as PL/SQL
\load <file>              — load .pell defs into session
\sql <statement>          — run raw SQL
\connect <dsn>            — reconnect to a different database
```

---

## What the REPL compiles to

Every cell, including all accumulated definitions and snapshotted
variables, compiles to a single `DECLARE ... BEGIN ... END;`
anonymous PL/SQL block. Use `\show` to inspect it at any time.

The block structure:

```sql
DECLARE
  -- Record types from accumulated defs
  TYPE t_employee IS RECORD (...);

  -- Functions from accumulated defs
  FUNCTION promote(p_e IN t_employee) RETURN t_employee IS
    ...
  END promote;

  -- The cell's body (including snapshotted variable replays)
  PROCEDURE pell_anon_main IS
    l_x NUMBER;          -- from snapshot: x = 42
    l_name VARCHAR2(4000); -- from snapshot: name = 'Alice'
  BEGIN
    l_x := 42;           -- replayed literal (offline value)
    l_name := 'Alice';   -- replayed literal
    -- ... this cell's statements ...
  END pell_anon_main;
BEGIN
  pell_anon_main;
END;
/
```

Each cell recompiles the entire session. For a large session (many
defs + many variables), this can take a few hundred milliseconds.
If it gets sluggish, `\reset` and `\load` just the defs you need.
