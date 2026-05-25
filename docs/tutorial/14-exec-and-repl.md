---
title: pell exec and pell repl
parent: Tutorial
nav_order: 14
---

`pell build` compiles a `.pell` file to PL/SQL that you then install
with SQL\*Plus or your tool of choice. For interactive use — try a
snippet, see the result, iterate — pell ships two driver-backed
subcommands: `pell exec` and `pell repl`.

## Setup

Install the optional driver dependencies:

```sh
pip install -e .[repl]      # installs oracledb + prompt_toolkit
```

Point at your Oracle instance via the `PELL_DB_URL` environment
variable:

```sh
export PELL_DB_URL="user/pass@host:1521/service"
```

Or pass `--connect user/pass@host:1521/service` on every command.

The driver uses python-oracledb in thin mode — no Oracle Instant
Client install required.

## `pell exec` — one-shot snippets

Compile a pell snippet into an anonymous PL/SQL block and run it:

```sh
$ pell exec -e 'dbms_output::put_line("hello, {sysdate()}");'
hello, 23-MAY-26
```

Or from a file:

```sh
$ pell exec snippet.pell
```

Or from stdin:

```sh
$ echo 'dbms_output::put_line("from stdin");' | pell exec
from stdin
```

The cell can contain function definitions, records, and top-level
statements:

```sh
$ pell exec -e '
fn fizzbuzz(n: number) -> text {
    if n % 15 == 0 { return "FizzBuzz"; }
    if n % 3 == 0  { return "Fizz"; }
    if n % 5 == 0  { return "Buzz"; }
    return "{n}";
}
let nums: list<number> = [1, 2, 3, 4, 5, 6, 9, 10, 15];
for n in nums { dbms_output::put_line(fizzbuzz(n)); }
'
1
2
Fizz
4
Buzz
Fizz
Fizz
Buzz
FizzBuzz
```

Useful flags:

| Flag             | Behavior                                                         |
|------------------|------------------------------------------------------------------|
| `--dry-run`      | Print the emitted PL/SQL; don't connect.                        |
| `--show-block`   | On runtime error, also dump the block to stderr for debugging.  |
| `--connect URL`  | Override `PELL_DB_URL`.                                         |
| `--target 19c`   | Compile for Oracle 19c.                                         |

### What it emits

A pell exec cell compiles to a self-contained DECLARE/BEGIN/END
block. Records become DECLARE-local TYPE decls; fns become nested
local subprograms; statements go into a synthetic `pell_anon_main`
procedure that BEGIN calls:

```sql
DECLARE
  TYPE t_pair IS RECORD (a NUMBER, b NUMBER);

  FUNCTION fizzbuzz(p_n IN NUMBER) RETURN VARCHAR2 IS
  BEGIN
    IF MOD(p_n, 15) = 0 THEN RETURN 'FizzBuzz'; END IF;
    ...
  END fizzbuzz;

  PROCEDURE pell_anon_main IS
    TYPE t_number_list IS TABLE OF NUMBER INDEX BY PLS_INTEGER;
    l_nums t_number_list;
  BEGIN
    l_nums(1) := 1; l_nums(2) := 2; ...
    FOR i_n IN l_nums.FIRST .. l_nums.LAST LOOP
      ...
    END LOOP;
  END pell_anon_main;
BEGIN
  pell_anon_main;
END;
/
```

Schema-level items (`type`, `sealed type`, `aggregate`, `@pipelined`
fns) need CREATE TYPE — they don't fit in an anonymous block.
`pell exec` rejects them with a clear error and points you at
`pell build` instead.

### CLOB-aware binding

The driver auto-promotes any Python `str` bind over 4000 bytes to
CLOB. So:

```python
from pell.driver import connect
conn = connect()
big = "X" * 8000           # 8KB string
conn.run_block("BEGIN dbms_output.put_line(length(:s)); END;", {"s": big})
# Output: 8000
```

VARCHAR2 to CLOB conversion is implicit and transparent — you don't
write LOB code.

## `pell repl` — notebook-style cells

For interactive exploration, `pell repl` opens a terminal notebook on
prompt_toolkit:

```sh
$ pell repl
pell repl — multi-line cell, blank line submits; \help for commands
[1]> fn double(n: number) -> number { return n * 2; }
    >
[2]> dbms_output::put_line("12 doubled = {double(12)}");
    >
12 doubled = 24
[3]>
```

How it works:

- Each cell is a pell snippet. Submit with a blank line.
- **Definitions** (fn, record, error, etc.) accumulate into a running
  session module.
- **Top-level statements** execute as the body of an anonymous block
  built from the full session — so cell 2 can call cell 1's `double`.
- Output (DBMS_OUTPUT) prints below the cell.

### Slash commands

| Command            | Behavior                                              |
|--------------------|-------------------------------------------------------|
| `\save <file>`     | Dump the running session as PL/SQL                    |
| `\load <file>`     | Append a `.pell` file's defs into the session         |
| `\sql <stmt>`      | Run one raw SQL statement; print rows or row count    |
| `\reset`           | Clear all accumulated defs                            |
| `\show`            | Print the anon block that would run on next cell      |
| `\connect <dsn>`   | Reconnect to a different database                     |
| `\help`            | List commands                                         |
| `\quit`            | Exit                                                  |

Example session:

```
[1]> record User { id: number, name: text }
    >
[2]> fn get_user(id: number) -> User {
    >    let u: User = sql! {
    >        select id, name from users where id = :id
    >    }.one();
    >    return u;
    > }
    >
[3]> let u: User = get_user(1);
    > dbms_output::put_line("user: {u.name}");
    >
user: Alice

[4]> \sql select count(*) from users
    >
count(*)
--------
12      
  (1 row)

[5]> \save explore.pell
  saved → explore.pell  (as PL/SQL; pell-source save TBD)
```

## When to use which

- `pell exec` for: scripted workflows, one-off pipelines, CI tasks,
  smoke-testing a new fn against the DB.
- `pell repl` for: exploration, learning, building up a slice of
  application logic interactively before committing it to a file.
- `pell build` for: production code that gets deployed.

## Where to go next

- [The compilation model](./15-compilation-model.md) — how anon
  blocks differ from full packages, and what each is good for.
- [Reference: runtime](../reference/runtime.md) — the `pell_runtime`
  package that errors lower into.