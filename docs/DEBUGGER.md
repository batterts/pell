# The pell debugger

Step through pell source running inside Oracle — breakpoints in `.pell`
files, a generated stub per `pub fn`, and fall-through to deployed
PL/SQL source when pell source runs out.

## How it works

Oracle is the runtime, so the debugger drives the database's own
debugging engine: `DBMS_DEBUG_JDWP` (the SQL Developer mechanism). The
**database session connects out** to the IDE over the JDWP protocol;
the IDE controls it with breakpoints/steps like any JVM target.

```
IntelliJ (JDI listener) ◄──JDWP── Oracle session
        │                              ▲
        └── pell debug-target ─────────┘  (runs the stub block)
```

Line mapping is carried in the deployed source itself: debug builds
append `-- @pell:<line>` markers to every emitted statement
(`pell deploy --debug`), and `pell srcmap` reproduces the
pell ↔ PL/SQL line map from an in-memory rebuild — no sidecar files.

JDWP exposes PL/SQL units as synthetic classes
(`$Oracle.PackageBody.SCHEMA.NAME`, `$Oracle.Block.SCHEMA.<hash>` for
the stub's anonymous block). Frames in pell-compiled units map back to
`.pell` lines; frames in foreign PL/SQL (the logger runtime, hand-
written packages) fetch `ALL_SOURCE` via `pell debug-source` and open
read-only — stepping just keeps going.

## One-time database setup

Ask a DBA to run, once per developer:

```sh
sqlplus system/<pwd>@host:1521/<pdb> @compiler/scripts/grant_debug.sql YOUR_USER
```

That grants `DEBUG CONNECT SESSION` (required), `DEBUG ANY PROCEDURE`
(optional — stepping into other schemas), and the JDWP network ACE
(only needed for the default jdwp transport). This is exactly the
privilege bar SQL Developer's debugger has — Oracle treats session
debugging as a security boundary, and **there is no grant-free path**:
the SQL transport below checks the *same* `DEBUG CONNECT SESSION`
privilege.

## Two transports

| | jdwp (default) | sql (`PELL_DEBUG_TRANSPORT=sql`) |
|---|---|---|
| mechanism | DBMS_DEBUG_JDWP — DB connects back to the IDE | DBMS_DEBUG (Probe) — two outbound SQL sessions |
| network | needs an inbound path + JDWP ACE | **outbound only** — tunnels/NAT/no-ACL just work |
| grants | DEBUG CONNECT SESSION (+ ACE) | DEBUG CONNECT SESSION only |
| script (anon block) breakpoints | yes | no — step from the top instead |
| module breakpoints, stepping, locals, stack | yes | yes |

The sql transport is a full duplex JSON-lines protocol over the
`pell debug-serve` process's stdio (websocket-style, minus the
socket): the IDE writes commands, events stream back. The target
session suspends at startup waiting for the debugger — a built-in
rendezvous, so breakpoints can't be outrun.

```sh
# everything outbound — through the same tunnel as your SQL connection:
export PELL_DEBUG_TRANSPORT=sql
```

The engine is also usable standalone:

```sh
pell debug-serve script.pell     # JSON commands on stdin, events on stdout
```

The docker harness and `setup_example_schemas.sql` already include
these grants for `pell_test`.

## No DBA? Use trace mode

`--trace` is the zero-privilege fallback — line-by-line execution
tracing over DBMS_OUTPUT, needing nothing beyond what `pell exec`
already uses:

```sh
pell exec myscript.pell --trace        # trace the script
pell deploy app/ --trace               # instrument deployed packages too
```

Every statement prints `[pell-trace] file.pell:line` before it runs:

```
[pell-trace] trace_test.pell:2
INFO:  greeting world
[pell-trace] trace_test.pell:3
hello, world
```

It composes with `--debug` (markers and trace lines don't collide),
and traced packages stay traced for every caller until redeployed
without the flag.

## Using it in IntelliJ

1. Click the gutter icon on any `pub fn` → **Debug <fn>**. The plugin
   generates `.pell-debug/debug_<module>_<fn>.pell` — a small exec
   script importing the module and calling the fn with placeholder
   args — and opens it. Edit the arguments (the file is yours; it's
   regenerated only if deleted).
2. Set breakpoints in the stub and/or the module's `.pell` source.
3. The debug session: deploys the module with `--debug`, starts the
   JDWP listener, runs `pell debug-target` (which holds the block
   until breakpoints are armed), and stops at your lines. Variables,
   stack, step over/into/out all work; `Run` instead of `Debug` just
   executes the stub.

Connection comes from `PELL_DB_URL` in the IDE's environment.

### Connect-back host

The database must be able to reach the IDE:

| database location           | callback address used               |
|-----------------------------|-------------------------------------|
| docker on this machine      | `host.docker.internal` (automatic)  |
| remote host                 | the local interface's IP (automatic)|
| anything unusual            | set `PELL_DEBUG_CALLBACK_HOST`      |

### Through an SSH tunnel

If you reach the database through `ssh -L 1521:localhost:1521 dbhost`,
the auto-detection sees `localhost` and guesses wrong — and the
database can't open a connection straight back to your machine anyway.
Give the JDWP connection the same treatment as the SQL connection: a
reverse tunnel.

```sh
# one ssh session carries both directions:
ssh -L 1521:localhost:1521 -R 5005:localhost:5005 user@dbhost
```

Then tell pell both halves — the IDE must listen on a FIXED port (the
reverse tunnel needs one), and the database should connect to its own
loopback, where sshd is listening:

```sh
export PELL_DEBUG_JDWP_PORT=5005          # pin the IDE's listener port
export PELL_DEBUG_CALLBACK_HOST=127.0.0.1 # "connect to yourself" — sshd forwards it
```

Flow: Oracle session → `127.0.0.1:5005` on the DB host → sshd → your
machine's `5005` → the IDE's JDI listener. Works through bastions too,
as long as the `-R` listener lands on a host the database server can
reach (tunnel to the DB host itself and loopback always works; on a
separate bastion, Oracle must be able to reach `bastion:5005` and
you'd set `PELL_DEBUG_CALLBACK_HOST=<bastion>`).

The same two variables work for the standalone CLI:
`pell debug-target script.pell --jdwp 127.0.0.1:5005`.

## CLI pieces (usable standalone)

```sh
pell deploy app/ --debug         # markers + PLSQL_DEBUG=TRUE units
pell srcmap app/billing.pell     # pell↔unit line map (JSON)
pell srcmap stub.pell --anon     # same for an exec script's block
pell debug-target stub.pell --jdwp 192.168.1.10:5005 [--wait-for-go]
pell debug-source HR.EMPLOYEES --type PACKAGE_BODY
```

## Constructs Oracle's debugger can't step through

A few otherwise-valid constructs throw an Oracle **internal** error
while the JDWP debug VM is attached — even though the package compiles
and runs fine without `--debug`. The signature:

```
ORA-00600: internal error code, arguments: [15419], [severe error
           during PL/SQL execution], [2649], ...
ORA-06544: PL/SQL: internal error, arguments: [2649], ...
ORA-06553: PLS-707: unsupported construct or internal error [2649]
DPY-4011: the database or network closed the connection
```

The usual culprit is a **`FORALL` over the fields of a record
collection** — what `forall x in <list<Record>> { ... :x.field ... }`
lowers to (`FORALL i ... VALUES (rows(i).a, rows(i).b, ...)`). Oracle's
debug VM can't execute the per-field collection bind while attached.

This is an Oracle limitation, not a pell codegen fault — the code is
valid and runs correctly outside the debugger. `pell debug-target`
detects this error class and prints an explanation pointing here.

**Workarounds**

- Debug a *caller* and step **over** the bulk operation, or put the
  breakpoint *after* it — only executing the construct under the
  attached VM trips the error.
- Run the unit without `--debug` (it's valid; only live stepping fails).
- If you must step the body, temporarily rewrite the `forall` as a
  plain `for` loop with a per-row `sql!{ insert ... }` (row-by-row is
  debuggable — that's the B1 vs B2 benchmark difference).

## Protocol notes (pinned by OracleJdwpProtocolTest)

Run the protocol test against any Oracle to re-verify the contract:

```sh
PELL_DEBUG_TEST_DB=pell_test/pell_test@localhost:11521/FREEPDB1 \
  ./gradlew test --tests '*OracleJdwpProtocolTest*'
```

* Oracle does **not** suspend after `CONNECT_TCP` — `--wait-for-go`
  exists so short blocks can't finish before breakpoints arm.
* The target session needs `PLSQL_DEBUG=TRUE` for the anonymous block
  itself or stub frames report line `-1` (`debug-target` sets it).
* Strata is `[Java]`; JDI line numbers equal `ALL_SOURCE` lines.
* At a breakpoint the callee frame can already be on top; trust
  `thread.frames()`, not the event location.
* `STEP_OVER` stops at the next line at same-or-shallower frame
  *depth* — in a loop the landing line can be numerically smaller.
* Locals arrive as `$Oracle.Builtin.*` object mirrors; values render
  via `toString` invocation with a type-name fallback.
