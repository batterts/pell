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

Run as a DBA (already in `compiler/scripts/setup_example_schemas.sql`
and the docker init):

```sql
GRANT DEBUG CONNECT SESSION TO pell_test;
GRANT DEBUG ANY PROCEDURE TO pell_test;   -- step into other schemas
BEGIN
  DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
    host => '*',
    ace  => xs$ace_type(privilege_list => xs$name_list('JDWP'),
                        principal_name => 'PELL_TEST',
                        principal_type => xs_acl.ptype_db));
END;
/
```

Without the ACE, `CONNECT_TCP` raises ORA-24247.

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

## CLI pieces (usable standalone)

```sh
pell deploy app/ --debug         # markers + PLSQL_DEBUG=TRUE units
pell srcmap app/billing.pell     # pell↔unit line map (JSON)
pell srcmap stub.pell --anon     # same for an exec script's block
pell debug-target stub.pell --jdwp 192.168.1.10:5005 [--wait-for-go]
pell debug-source HR.EMPLOYEES --type PACKAGE_BODY
```

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
