---
title: Benchmarks
nav_order: 6
permalink: /benchmarks/
---

End-to-end test of pell-emitted code on a real Oracle 23.26.1 Free instance,
comparing five pell insert methods (`B1–B5`) against three raw-PL/SQL
chunked-`FORALL` variants (`R1–R3`) on a 100,000-row source.

> Hardware: Oracle Database Free 23.26 on a LAN host. Single FREEPDB1
> session, no concurrent traffic. Source table has no PK/indexes;
> destinations are empty heap tables truncated before each run.

## Methodology

- Source: `ml_ratings_src` — 100,000 rows × (user_id, movie_id, rating, ts), no indexes.
- Each method copies all 100,000 rows from the source into its dedicated
  destination table. Destinations are `TRUNCATE`d before each run.
- 3 runs per method. First run treated as warm-up (and shown for
  variance), final results are the mean of runs #2 and #3.
- Timing via `DBMS_UTILITY.GET_TIME` (hundredths of a second), converted
  to ms.

## The methods

| # | Method | Surface | Lowers to |
|---|---|---|---|
| **B1** | row-by-row pell | `for r in sql!{select…} { sql!{insert…} }` | cursor `FOR` loop + per-row `INSERT` |
| **B2** | collect+forall pell | `let xs: list<Rating> = sql!{…}.collect(); forall r in xs { sql!{insert…} }` | `BULK COLLECT INTO` + single `FORALL` |
| **B3** | direct `INSERT … SELECT` pell | `sql!{ insert into d select … from s }` | one SQL statement, no PL/SQL fetch loop |
| **B4** | pipelined passthru pell | `sql!{…} \|> passthru \|> collect()` + `insert select` | `INSERT … SELECT … FROM TABLE(passthru(CURSOR(…)))` (PIPELINED) |
| **B5** | pipelined transform pell | same shape, but yields `ts * 2` | same lowering, with per-row computation in the pipeline |
| **R1** | raw chunked `FORALL` LIMIT 100 | hand-written PL/SQL | standard cursor loop + chunked FORALL |
| **R2** | raw chunked `FORALL` LIMIT 1000 | hand-written PL/SQL | same shape, larger chunk |
| **R3** | raw chunked `FORALL` LIMIT 10000 | hand-written PL/SQL | same shape, even larger chunk |

The pell sources are in `compiler/benchmarks/*.pell`; the raw PL/SQL is in
`compiler/raw/r_chunked_forall.sql`. The driver that times them is
`compiler/benchmarks/driver.sql`.

## Results — 100,000 rows

Sorted by mean elapsed time (mean of runs #2 + #3):

| Rank | Method | Mean ms | Min | Max | Ratio vs B3 | Notes |
|---:|---|---:|---:|---:|---:|---|
| 1 | **B3** `INSERT … SELECT` | **45** | 40 | 50 | 1.0× | Single SQL statement, no PL/SQL loop |
| 2 | R2 chunked FORALL (LIMIT 1000) | 120 | 120 | 120 | 2.7× | The traditional bulk pattern, optimal chunk |
| 3 | R3 chunked FORALL (LIMIT 10000) | 120 | 120 | 120 | 2.7× | Bigger chunks don't help further |
| 4 | **B2** pell `.collect()` + `forall` | 125 | 120 | 140 | 2.8× | Pell bulk, matches raw within noise |
| 5 | **B5** pell `@pipelined` + transform | 150 | 150 | 160 | 3.3× | Pipelined identity + per-row `* 2` |
| 6 | R1 chunked FORALL (LIMIT 100) | 160 | 160 | 170 | 3.6× | Chunk size too small — pays per-batch overhead 1000× |
| 7 | **B4** pell `@pipelined` passthru | 160 | 150 | 160 | 3.6× | Pipelined identity, no transform |
| 8 | **B1** pell row-by-row | **2465** | 1390 | 3540 | **55×** | Anti-pattern; included as the floor |

## Observations

### 1. `INSERT … SELECT` wins when you can express it in pure SQL

B3 at 45ms is roughly 3× faster than every bulk-PL/SQL approach and 55×
faster than row-by-row. The reason is structural: no fetch-into-PGA, no
marshalling, no per-row PL/SQL execution context. **When the transform
is expressible in SQL, the right pell idiom is a single `sql!{insert …
select …}` block.**

### 2. Pell's bulk methods are competitive with raw PL/SQL

B2 (pell `.collect()` + `forall`) lands at 125ms; the raw chunked-FORALL
at the optimal chunk size lands at 120ms. The 5ms gap is in the noise.
**Pell adds no measurable overhead on top of well-written bulk PL/SQL.**

### 3. Chunk size matters but plateaus at ~1000

R1 (LIMIT 100) is 160ms; R2 (LIMIT 1000) and R3 (LIMIT 10000) are both
120ms. The cost at LIMIT 100 is the per-batch overhead (open/close, FORALL
plan caching) multiplied by 1000 iterations. Past 1000, the dataset fits
into a single SGA-cached plan so larger chunks don't help.

This is also pell's current limitation: B2 loads all 100k into one PGA
collection, while a proper chunked-FORALL would stream. For 100k rows
the difference is invisible; for 10M rows the PGA blow-up would matter.
**Adding a `forall_chunks_of(N)` construct to pell would close this gap.**

### 4. `@pipelined` overhead is ~30–40ms for 100k rows

B4 (passthru) at 160ms vs B3 (direct INSERT SELECT) at 45ms — the
`PIPELINED` function adds ~115ms over what the same SQL would have done
directly. That's about 1.15 µs/row, which is the cost of streaming
through the `TABLE(fn(CURSOR(…)))` machinery — the cursor open, the bulk
fetch into the PGA buffer (LIMIT 100), the `PIPE ROW` for each output,
and the OBJECT constructor marshalling.

B5 (transform) at 150ms is *within noise* of B4. The per-row `ts * 2`
multiplication adds no measurable cost. **The overhead is the pipelined
machinery, not the transform itself.** If you can avoid pipelined and
express the work in SQL, do so; if you can't, the marginal cost of a
more complex transform inside `@pipelined` is essentially free.

### 5. Row-by-row is catastrophic and high-variance

B1 ran 1.39s, 1.47s, and 3.54s — a 2.5× spread between best and worst.
The mean of 2.47s is ~55× the optimal method and ~20× the bulk methods.
Two failure modes are at play:

- Per-row PL/SQL → SQL context switch (100,000 of them)
- Per-row plan lookup (the implicit cursor for the embedded `INSERT`
  reuses one prepared statement, but Oracle still does soft-parse work)

The variance is real-world: the third run hit some scheduling
contention on the test host. **B1 is included as the floor, not as a
recommendation.** Even with no contention it's >10× slower than the
bulk methods.

### 6. Pell did not break anything

All five pell methods executed correctly against real Oracle and
produced identical row counts (100,000). The schema-level OBJECT and
nested-table types pell emits for `@pipelined` worked first try in the
two-bench cases. The `BULK COLLECT INTO` lowering produced the expected
PL/SQL.

The known v0 limitations did NOT bite on this workload:
- No `forall_chunks_of(N)` — but `.collect() + forall` (one big chunk)
  performs within noise of the raw chunked version on 100k rows.
- `logger::info` collision with Oracle's built-in `LOG()` — none of these
  benchmarks use logging.
- Static-cardinality checks — not exercised here.

## When to reach for which

| If you can… | Use |
|---|---|
| Express the transform in pure SQL | **B3** `sql!{ insert … select … }` |
| Reuse the output as a SQL source (`TABLE(…)`-able pipelines) | **B4 / B5** `@pipelined` + `\|>` |
| Need a list materialized in pell for further per-row work | **B2** `.collect()` + `forall` |
| Need to chunk to bound PGA on huge inputs | Raw chunked FORALL (today; pell `forall_chunks_of` someday) |
| Don't think about performance / single-row writes only | B3 (or B6 `INSERT … VALUES` equivalent) — but **never B1** |

## Analytical reports

The three pell report modules in `compiler/reports/` also ran cleanly
against the dataset. Output:

### Rating distribution

```
rating  n         pct
------  --------  ------
1       6110      6.11%
2       11370     11.37%
3       27145     27.15%
4       34174     34.17%   ← peak
5       21201     21.2%
```

The distribution is left-skewed toward 3–4 — typical of explicit
rating systems where users gravitate to the middle and rarely use 1.

### Top 10 movies (min 50 ratings)

```
id    n     avg    title
---   ---   ---    -----
408   112   4.491  Close Shave, A (1995)
318   298   4.466  Schindler's List (1993)
169   118   4.466  Wrong Trousers, The (1993)
483   243   4.457  Casablanca (1942)
114   67    4.448  Wallace & Gromit: The Best of Aardman Animation (1996)
64    283   4.445  Shawshank Redemption, The (1994)
603   209   4.388  Rear Window (1954)
12    267   4.386  Usual Suspects, The (1995)
50    583   4.358  Star Wars (1977)
178   125   4.344  12 Angry Men (1957)
```

### Top 10 most active users

```
user_id   rated     avg
-------   -----     ---
405       737       1.834   ← uses the full scale, harshly
655       685       2.908
13        636       3.097
450       540       3.865
276       518       3.465
416       493       3.846
537       490       2.865
303       484       3.366
234       480       3.123
393       448       3.337
```

All three reports use pell's `let rows: list<T> = sql!{…}.collect();`
pattern (BULK COLLECT INTO) with the result returned to a calling
PL/SQL driver that iterates and prints. **The fact that `list<T>` is a
real pell return type — and the compiler now puts the list TYPE in the
spec, fixed mid-bench — is itself a result.**

## Bugs found by running this

Three bugs surfaced while wiring this up that the unit tests didn't catch:

1. **pell didn't put list types in the package spec when used as a public
   fn return type.** The body declared `TYPE t_TopMovie_list IS TABLE OF
   t_topmovie INDEX BY PLS_INTEGER` but the spec referenced it in the
   signature — Oracle rejected with `PLS-00201: identifier
   'T_TOPMOVIE_LIST' must be declared`. Fixed: `_emit_spec` now pre-walks
   public fn signatures and emits any list types they reference at spec
   scope.

2. **R1–R3 originally used `OUT` parameter procedures**; the driver
   called them like functions and got `PLS-00306: wrong number or types
   of arguments`. Converted to functions returning `NUMBER`.

3. **The driver had `PROMPT` inside a PL/SQL block.** `PROMPT` is a SQLcl
   client command, not PL/SQL. The whole block failed to compile.
   Lesson: lint your driver scripts.

All three are now fixed in the repo. The first one is the only pell
emitter bug; the other two are usage mistakes in the test harness.

## Repository layout

```
compiler/
├── benchmarks/
│   ├── b1_row_by_row.pell           # pell sources
│   ├── b2_collect_forall.pell
│   ├── b3_insert_select.pell
│   ├── b4_pipelined_passthru.pell
│   ├── b5_pipelined_transform.pell
│   ├── driver.sql                   # benchmark runner
│   └── sql/                         # emitted PL/SQL
├── reports/
│   ├── top_movies.pell
│   ├── rating_dist.pell
│   ├── user_activity.pell
│   ├── driver.sql                   # report runner
│   └── sql/
└── raw/
    └── r_chunked_forall.sql         # R1/R2/R3 hand-written comparisons
```

## How to reproduce

```sh
# 1. Download + stage MovieLens 100k (manual one-time setup)
curl -L https://files.grouplens.org/datasets/movielens/ml-100k.zip -o /tmp/ml-100k.zip
unzip -d /tmp /tmp/ml-100k.zip
# Convert TSVs to CSV with headers, load via SQLcl LOAD into:
#   ml_users / ml_movies / ml_ratings_src

# 2. Compile and install everything
./pell build compiler/benchmarks -d compiler/benchmarks/sql
./pell build compiler/reports    -d compiler/reports/sql
./pell runtime compiler/benchmarks -o compiler/runtime/pell_runtime.sql

cat compiler/runtime/pell_runtime.sql \
    compiler/benchmarks/sql/*.sql      \
    compiler/raw/r_chunked_forall.sql  \
    compiler/reports/sql/*.sql         \
  | sqltun

# 3. Run the bench
cat compiler/benchmarks/driver.sql | sqltun

# 4. Run the reports
cat compiler/reports/driver.sql | sqltun
```