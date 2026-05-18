# pell pass-through coverage — Oracle 23ai Free

Certifies the Oracle SQL functions that pell's pass-through mechanism
can hand off to PL/SQL natively. Re-run with:

    .venv/bin/python compiler/scripts/audit_functions.py

The script generates one anonymous PL/SQL block per function, executes
each via `EXECUTE IMMEDIATE` inside a `WHEN OTHERS` handler (so a
compile error in one test doesn't poison the others), and reports per
test.

## Summary (last run: 2026-05-18, Oracle 23.26.1.0.0)

**106 of 112 tests pass.** The 6 failures are all known SQL-only
functions documented in design.md §5.4.

| Category | Passing | Total |
|---|---|---|
| String | 33 | 33 |
| Numeric | 26 | 26 |
| Date / Timestamp | 16 | 16 |
| Conversion | 15 | 15 |
| Null / conditional | 8 | 9 |
| Environment / hash | 4 | 8 |
| Packaged (DBMS_*, UTL_*) | 5 | 5 |

## Pass-through verified

These calls work today as bare-identifier pell calls. Pell's
`<name>(args)` lowers verbatim; receiver-style `recv.<name>(args)`
also works because the emitter falls through to free-function dispatch
when the receiver isn't an OBJECT type.

### String (33/33)
ascii · asciistr · chr · concat · initcap · instr · instrb · instrc ·
length · lengthb · lengthc · lower · lpad · ltrim · nls_initcap ·
nls_lower · nls_upper · nlssort · regexp_count · regexp_instr ·
regexp_replace · regexp_substr · replace · rpad · rtrim · soundex ·
substr · substrb · substrc · translate · trim · unistr · upper

### Numeric (26/26)
abs · acos · asin · atan · atan2 · bitand · ceil · cos · cosh · exp ·
floor · ln · log(base, n) · mod · power · remainder · round (1-arg and
2-arg) · sign · sin · sinh · sqrt · tan · tanh · trunc (1-arg and 2-arg)

### Date / Timestamp (16/16)
sysdate · systimestamp · current_date · current_timestamp ·
localtimestamp · add_months · last_day · months_between · next_day ·
numtodsinterval · numtoyminterval · sys_extract_utc · dbtimezone ·
sessiontimezone · from_tz · tz_offset

### Conversion (15/15)
hextoraw · rawtohex · to_char (num and date forms) · to_number ·
to_date · to_timestamp · to_dsinterval · to_yminterval ·
to_binary_double · to_binary_float · to_clob · to_blob · compose ·
decompose

### Null / conditional (8/9)
nvl · coalesce · nullif (eq and neq cases) · greatest · least · nvl2 ✱

✱ `nvl2` is documented as SQL-only in 19c, but the audit shows it
*works* in PL/SQL on 23ai. Don't rely on it for 19c targets.

### Environment (4/8)
user · uid · sys_guid · sys_context

### Packaged (5/5)
`dbms_utility::get_hash_value` · `dbms_random::value` ·
`dbms_random::string` · `utl_raw::bit_xor` · `utl_raw::cast_to_raw`

## Confirmed SQL-only — wrap in `sql!{ SELECT … FROM dual }`

All return `PLS-00204` or `PLS-00201` when called bare from PL/SQL:

| Function | Why | Pell workaround |
|---|---|---|
| `decode` | SQL-only | `match` expression, or `sql!{}` |
| `lnnvl` | SQL-only | `case`/`if`, or `sql!{}` |
| `vsize` | SQL-only | `sql!{ select vsize(:x) from dual }` |
| `standard_hash` | SQL-only in PL/SQL context | `sql!{}` or `dbms_crypto.hash(...)` |
| `dump` | SQL-only | `sql!{}` |
| `ora_hash` | SQL-only | Use `dbms_utility::get_hash_value` |

## Out of scope for this audit

These exist but don't fit the "bare-identifier function call" pattern
that pell's pass-through covers:

- `CAST(expr AS type)` — a SQL operator with its own syntactic form, not
  a function call. Pell doesn't currently surface CAST; use the
  equivalent `to_<type>` conversion function instead.
- `EXTRACT(field FROM expr)` — a SQL operator. Pell exposes the common
  cases via `.year()`, `.month()`, `.day()`, etc. method aliases.
- Aggregate (`COUNT`, `SUM`, …) and analytic (`LAG`, `ROW_NUMBER`, …)
  functions — SQL-only by definition; usable only inside `sql!{}`.
- Functions taking complex types (BFILE ops, XML functions, JSON_TABLE,
  spatial) — not covered; almost all are SQL-only anyway.
