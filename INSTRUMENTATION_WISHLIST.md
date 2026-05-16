# Instrumentation wishlist

Ideas for built-in observability features. Not a spec — a menu to draw from
when the rest of v0 is stable. Anchored to Oracle facilities that already
exist where possible; we shouldn't reinvent.

Five categories: **metrics, tracing, profiling, module/program/action,
cross-cutting**.

---

## 1. Metrics

**Oracle facility**: nothing built-in. The closest is `DBMS_APPLICATION_INFO.SET_SESSION_LONGOPS` for long-op progress, and rolling your own logging table. So this is a build-from-scratch tier.

| # | Wish | Notes |
|---|---|---|
| M1 | `metrics::counter("name").inc()` / `.inc_by(n)` | The primitive. Lowers to an `INSERT INTO pell_metrics_events` or a buffered batch. |
| M2 | `metrics::gauge("name").set(n)` | Last-write-wins point-in-time value. |
| M3 | `metrics::timer("op").observe(ms)` and `metrics::time("op") { ... }` block | Block form auto-measures wall time and emits on exit (success or error). |
| M4 | `metrics::histogram("latency", buckets: [1,5,10,50,100,500])` | Prometheus-style fixed-bucket histogram, since Oracle has no good HDR primitive. |
| M5 | Tag dimensions: `metrics::counter("orders").with(status: s, region: r).inc()` | Bounded cardinality enforced at compile time — declare the legal tag keys per metric so a typo or unbounded label can't blow up the cardinality. |
| M6 | Pluggable sink | Default is a `pell_metrics_events` side table. Allow user to point at a callback (`pell_runtime.emit_metric`) to integrate with their existing pipeline. |
| M7 | Sampling | `.inc_sampled(0.1)` — only record 10% of events. Compile-time random thinning so the cost of the unsampled 90% is one number-compare. |
| M8 | Self-metrics | pell-runtime emits its own metrics: error rates by variant, finally-block frequencies, FORALL batch sizes, etc. — opt-in via `[instrumentation] self_metrics = true`. |
| M9 | Out-of-band egress | Async flush from the metrics buffer to an HTTP collector via UTL_HTTP or a JDBC sidecar — for OTLP/StatsD without blocking the user's call. |

---

## 2. Tracing

**Oracle facility**: `DBMS_APPLICATION_INFO` for live module/action tags, `DBMS_MONITOR` for trace files, `DBMS_TRACE` for PL/SQL call flow, `DBMS_SESSION.SET_IDENTIFIER` for client identity. Distributed-trace standards (OpenTelemetry, W3C trace-context) have no native Oracle equivalent.

| # | Wish | Notes |
|---|---|---|
| T1 | Auto entry/exit tags on every `pub fn` | Tier-1 from earlier conversation. Sets `V$SESSION.MODULE` / `.ACTION` on entry, restores on exit. Default on, opt out with `@no_trace`. |
| T2 | Manual span block | `trace::span("checkout.compute_total") { ... }` — pushes a nested action; pops on exit. Lowers to nested `SET_ACTION` calls or to a custom trace-table write. |
| T3 | Span attributes | `trace::current().set_attr("user_id", id)` — attach arbitrary k/v to the active span. Stored in a trace-events table or attached as `CLIENT_INFO` for live visibility. |
| T4 | Distributed-trace context propagation | Read inbound `traceparent` / `tracestate` from a session var or a parameter; mint span IDs; propagate as outbound on external calls (UTL_HTTP). |
| T5 | Automatic error attribution | When an Err propagates through `?` or an unhandled exception leaves a span, tag the span with the error variant. Free observability for unhappy paths. |
| T6 | Sampling | Tail-based (mark the call interesting, decide at the end), or head-based (decide at entry). Tail-based is more useful but needs a buffer; head-based is cheaper. |
| T7 | CLIENT_IDENTIFIER from request context | `session::identify_as("user:42")` sets `V$SESSION.CLIENT_IDENTIFIER` so AWR aggregates per logical user, not per connection. |
| T8 | Correlation IDs | Auto-generated at the outermost pub-fn entry; threaded through all child calls in the same session; surfaces in metrics tags, log lines, span IDs. |
| T9 | "Long-op" awareness | For `forall`/`for` loops that run long, integrate with `DBMS_APPLICATION_INFO.SET_SESSION_LONGOPS` so they show up in `V$SESSION_LONGOPS` with a progress bar. |
| T10 | `pell trace` CLI | Read trace tables / files and produce: per-call timings, span-tree dumps, flamegraphs (via stackcollapse + flamegraph.pl shim). |

---

## 3. Profiling

**Oracle facility**: `DBMS_PROFILER` (line-level), `DBMS_HPROF` (call-tree), `DBMS_TRACE` (call flow), 10046 SQL trace, `DBMS_DEBUG` (interactive). All present in 19c and 23.

| # | Wish | Notes |
|---|---|---|
| P1 | `@profile(lines)` annotation | Lowers to a `DBMS_PROFILER.START_PROFILER` / `STOP_PROFILER` wrap. Gated by `pell_runtime.profiling_enabled()` so prod is free unless engaged. |
| P2 | `@profile(calls)` annotation (default for bare `@profile`) | Lowers to `DBMS_HPROF.START_PROFILING` / `STOP_PROFILING` plus an `ANALYZE` invocation that loads the trace into `PLSQL_HPROF_*`. Same gate. |
| P3 | `@trace_calls` annotation | Lowers to a `DBMS_TRACE.SET_PLSQL_TRACE` wrap — useful for "what executed in what order" diagnoses. |
| P4 | Session-level profile toggle | `EXEC pell_runtime.set_profiling(true);` once per debug session enables every gate at once. CI / smoke runs can leave it on. |
| P5 | Sampled profiling | "Profile 1% of calls" without wrapping every call — compile-time emits a randomized gate per fn entry. Useful for production-safe always-on profiling. |
| P6 | `pell profile` CLI subcommand | Reads `PLSQL_HPROF_FUNCTION_INFO` / `PLSQL_PROFILER_DATA` and dumps a readable summary keyed by pell module/fn, not the mangled package names. Uses the source map (§8). |
| P7 | Flamegraph output | `pell profile --flamegraph >out.svg` — stackcollapse from HPROF data → svg. |
| P8 | SQL-plan capture inside profiled regions | When `@profile(calls)` is engaged, also capture `V$SQL_PLAN` for every SQL statement the fn runs. Tie plan changes to perf regressions. |
| P9 | Memory profiling | Track PGA usage via `V$SESSTAT('session pga memory')` deltas around the fn — partial; PL/SQL doesn't give per-object allocation. |
| P10 | Differential profiling | `pell profile diff <run-a> <run-b>` — show which fns got faster/slower between two profile runs. Standard "is the new build slower?" loop. |

---

## 4. Module / Program / Action

**Oracle facility**: `DBMS_APPLICATION_INFO.SET_MODULE` (sets `V$SESSION.MODULE` + clears `.ACTION`), `SET_ACTION` (sets `.ACTION` keeping MODULE), `SET_CLIENT_INFO` (sets `.CLIENT_INFO`), `DBMS_SESSION.SET_IDENTIFIER` (sets `.CLIENT_IDENTIFIER`). `V$SESSION.PROGRAM` is set by the client at connect time and not directly mutable from PL/SQL post-connect.

| # | Wish | Notes |
|---|---|---|
| A1 | Auto MODULE/ACTION on every `pub fn` | (T1 — listed under tracing too because the line is blurry.) Default on; `@no_trace` opts out. |
| A2 | Three-level pell-side tag hierarchy | `program` (project, from `pell.toml`) → `module` (pell module name) → `action` (fn name). Default: MODULE = `<program>.<module>`, ACTION = `<fn>`. Configurable. |
| A3 | `@action(name)` override | When auto-derived names collide or aren't descriptive, override per fn. |
| A4 | Nested action push/pop | `module::push_action("validate")` inside a fn body; pops on scope exit. Surfaces in `V$SESSION.ACTION` as nested context. |
| A5 | `session::identify_as("user:42")` | Sets `CLIENT_IDENTIFIER`. Critical for multi-tenant apps so AWR / per-user dashboards group correctly. |
| A6 | `session::client_info("invoice:42")` | Sets `CLIENT_INFO` — looser tag for transient context. |
| A7 | Restore-on-exit guarantee | The wrapper reads the existing MODULE/ACTION at entry and restores it at exit (already sketched in earlier code). Means a pell call inside a pell call doesn't leak the inner module/action to the outer caller's V$SESSION view. |
| A8 | Heritability | Inner pell calls inherit the outer call's MODULE/ACTION unless they override — important so a private helper doesn't *replace* the public entry point's tags in V$SESSION. |
| A9 | Pell-aware `V$SESSION` lookup | A `pell session` CLI command that queries `V$SESSION` and renders module/action as pell-source paths (`hr.employees::promote`) instead of mangled package names. |
| A10 | "What's currently running" report | `pell session --aggregate` snapshots V$SESSION, groups by pell module/fn, and shows active session counts. Same data AWR has, but pell-aware and live. |

---

## 5. Cross-cutting

Concerns that touch all four of the above.

| # | Wish | Notes |
|---|---|---|
| X1 | Single `pell_runtime.instrumentation_level` knob | Off / minimal (module tags only) / standard (+ metrics + tracing) / full (+ profiling). Production default = minimal; dev default = standard. Per-fn `@instrument(level)` overrides. |
| X2 | Configuration surface | `pell.toml` `[instrumentation]` section: levels, sink URLs, sampling rates, opt-out lists. Per-environment via deploy profiles. |
| X3 | Source-map-aware reporting | Every CLI report (`pell profile`, `pell trace`, `pell session`) renders names as pell source paths, not PL/SQL mangled package names. Uses the source maps already planned in §8. |
| X4 | Free zero-overhead path | When `instrumentation_level = off`, the emitted code is exactly what we emit today. No wrappers, no dispatch, no `IF` checks. |
| X5 | Test affordances | `metrics::assert_counter("orders.placed").eq(3)` for use in `@test` blocks. Trace span assertions: "this fn was called exactly once during this test." |
| X6 | Egress idempotency | If the metrics sink fails, the user's call doesn't fail. Instrumentation never propagates errors to user code. |
| X7 | Compile-time cardinality / tag-key checking | Declare allowed tag keys per metric; the compiler rejects unknown ones. Stops the "metric explosion" outage pattern. |
| X8 | Documentation autogen | `pell doc` (post-M5) lists every metric/span/profile point a module emits, with descriptions. Operations dashboard knows what to plot. |
| X9 | Integration with the design's §6.6 error model | Errors raised via `pell_runtime` automatically annotate the active span and increment an error-counter keyed by the error variant. Free observability for the unhappy path. |
| X10 | Honest costs | Every primitive's overhead is documented in design.md and verified by a microbenchmark. Users need to be able to reason about cost. |

---

## What to build first, if/when this gets prioritized

In order of cost/benefit ratio:

1. **A1 / T1** — auto module/action tagging. ~50 lines of emitter work, every pell package becomes operationally visible.
2. **A5 / A6** — `session::identify_as`, `session::client_info`. Builds on A1 with minor surface additions.
3. **X1 / X4** — single knob with off-is-truly-zero. Required infra for everything else.
4. **P1 / P2 / P4** — `@profile(lines)`/`(calls)` with the session toggle. Real Oracle features, thin wraps.
5. **X3 / P6** — pell-aware profiling reports via the source map.
6. **T2 / T3** — manual spans + attributes once auto-tagging is solid.
7. **M1–M3** — metric primitives (counter/gauge/timer). The first piece that requires real new runtime infrastructure rather than thin wraps over Oracle.

Everything else (full distributed tracing, flamegraphs, differential profiling, fancy sinks) is the icing — defer until somebody asks for a specific one.
