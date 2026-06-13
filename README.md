# pell — a modern language that compiles to Oracle PL/SQL 23

A statically-typed surface language with first-class tooling, designed to
fix the worst ergonomic pain points of PL/SQL while still emitting
PL/SQL you can deploy to a real Oracle 23 database.

## IntelliJ plugin

[![JetBrains Plugin Version](https://img.shields.io/jetbrains/plugin/v/31994?label=plugin&color=blue)](https://plugins.jetbrains.com/plugin/31994-pell)
[![JetBrains Plugin Downloads](https://img.shields.io/jetbrains/plugin/d/31994?color=blue)](https://plugins.jetbrains.com/plugin/31994-pell)
[![JetBrains Plugin Rating](https://img.shields.io/jetbrains/plugin/r/rating/31994?color=blue)](https://plugins.jetbrains.com/plugin/31994-pell)

Available on JetBrains Marketplace — split-editor with live PL/SQL
preview, gutter build/run buttons, REPL launcher, new-project wizard.

📦 **[plugins.jetbrains.com/plugin/31994-pell](https://plugins.jetbrains.com/plugin/31994-pell)**

> The rich Marketplace card with one-click install lives on the docs
> site instead — see https://batterts.github.io/pell — because GitHub
> sandboxes `<script>` and `<iframe>` tags for security.

## Status

- **[design.md](./design.md)** — the language spec (3000+ lines).
- **[GETTING_STARTED.md](./GETTING_STARTED.md)** — how to actually
  compile, deploy, test, and debug a `.pell` file today.
- **[compiler/](./compiler/)** — Python transpiler, ~14k LOC, 320+ tests
  (offline snapshots + live deploy/execution sweeps against a real
  Oracle). Implements a large, practical subset of the language.
- **[compiler/examples/](./compiler/examples/)** — 36 `.pell` programs
  and their emitted `.sql`, vetted end-to-end by a utPLSQL suite.
- **[docs/reviews/](./docs/reviews/)** — five-reviewer critique reports
  plus a SUMMARY of the design review.

Beyond the compiler, the toolchain now covers the full edit→ship loop:

- **`pell deploy`** — install to Oracle with honest cross-schema error
  reporting; **`pell test`** — run utPLSQL `@suite`/`@test` modules and
  emit doc/junit/sonar/coverage reports.
- **Step debugger** — real breakpoints and variable inspection over
  DBMS_DEBUG_JDWP (and an outbound-only DBMS_DEBUG transport for
  tunnels/NAT). See [docs/DEBUGGER.md](./docs/DEBUGGER.md).
- **REPL** — notebook-style cells with variable persistence.
- **[JetBrains plugin](https://plugins.jetbrains.com/plugin/31994-pell)**
  + an LSP server (diagnostics, hover, completion, go-to-def).

## Quick taste

```pell
module hr.employees;

pub record Employee { id: number, name: text, level: number }

pub error NotFound { id: number }
pub error PolicyViolation { reason: text }

pub fn promote(id: number) -> Result<Unit, NotFound | PolicyViolation> {
  let e = sql! {
    select id, name, level from employees where id = :id
  }.one()?;

  if e.level >= 9 {
    return Err(PolicyViolation { reason: "already at max level" });
  }

  transaction {
    sql! { update employees set level = level + 1 where id = :id };
  }
  return Ok(());
}
```

Lowers to a `CREATE OR REPLACE PACKAGE BODY hr_employees` with proper
`SAVEPOINT`/`COMMIT`/`ROLLBACK` semantics, `RAISE_APPLICATION_ERROR`-coded
typed exceptions via `pell_runtime`, and `SELECT INTO` with handler
mapping. See `compiler/expected/02_employees.sql` for the literal output.

## Try it

```sh
./pell build compiler/examples/02_employees.pell
```

For everything else — what's implemented, what isn't, how to add a feature,
how the runtime works — see [GETTING_STARTED.md](./GETTING_STARTED.md).

## License

pell is licensed under the **Apache License, Version 2.0** — see
[LICENSE](./LICENSE) for the full text.

The PL/SQL output the pell compiler produces is **not** a derivative work
of pell and is **not** subject to pell's license. You own your compiled
output and can license it however you choose.

That output is provided **as-is**. pell makes no warranty that the
generated PL/SQL is correct, complete, fit for any particular purpose,
free of defects, or compatible with any specific Oracle Database version,
schema, or workload. You are responsible for reviewing, testing, and
validating the emitted code before deploying it. See the "Compiled output
disclaimer" section at the bottom of [LICENSE](./LICENSE).

"Oracle" and "PL/SQL" are trademarks of Oracle Corporation. They are used
here descriptively to identify the compilation target; pell is not
affiliated with, endorsed by, or sponsored by Oracle.
