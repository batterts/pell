# pell — a modern language that compiles to Oracle PL/SQL 23

A statically-typed surface language with first-class tooling, designed to
fix the worst ergonomic pain points of PL/SQL while still emitting
PL/SQL you can deploy to a real Oracle 23 database.

## Status

- **[design.md](./design.md)** — the language spec (1900+ lines).
  Status: draft 0.3.
- **[GETTING_STARTED.md](./GETTING_STARTED.md)** — how to actually
  compile a `.pell` file today.
- **[compiler/](./compiler/)** — Python v0 MVP transpiler. ~1700 LOC.
  75 tests passing. Implements a useful subset of the language.
- **[compiler/examples/](./compiler/examples/)** — `.pell` programs and
  their emitted `.sql`.
- **[reviews/](./reviews/)** — five-reviewer critique reports plus a
  SUMMARY of the design review.

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
