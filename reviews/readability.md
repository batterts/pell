# Review: Skeptical end-user / readability

## Verdict

Cautiously yes — but only if §6 (errors) lands the way the doc claims and the
`finally` foot-gun gets a real lint, not just a doc note. The language is at
its best when it's helping me grep, narrow my catches, and stop writing
`DECLARE … BEGIN … EXCEPTION WHEN OTHERS THEN log; RAISE; END;` for the
ten-thousandth time. It's at its worst when it borrows Rust ergonomics that
sound right in a slide deck but multiply the surface area (`Option<T>` *and*
`T?`, four row terminators, `with (…)` after `sql!{…}`, pipelines that
materialize and silently dodge SQL). The doc is mostly honest already; my
edits push it further toward "we are choosing a tradeoff" and away from
"this is just better."

## Strong points

- §4.5.1 "no explicit cursors" is the single most valuable feature in the
  whole document. The lowering sketch is the kind of thing that earns trust.
- §6.5 separating `.first()` / `.one()` / `.expect()` by *intent* is the right
  insight. `NO_DATA_FOUND` really is the worst PL/SQL wart and this is a clean
  answer.
- §6.3 framing `finally` as "cleanup, not handling" is the right framing — it
  just needs guard-rails (see Concerns).
- §9 annotations as a closed, validated set is genuinely better than the PL/SQL
  grab-bag, especially the rule that `@deterministic` on a fn with observable
  side effects is a compile error. That's a real win.
- Source maps (§4.1, §8) are non-negotiable for a transpiled language and the
  doc treats them as such.
- Non-goals (§2) are crisp. "Not a SQL replacement" alone justifies half the
  scope.
- §11 is mostly tradeoffs with stated biases — exactly the right tone.

## Concerns

- **§4.5 `with (status = "ACTIVE", dept_id = my_dept)`** — pure ceremony.
  `:status` already resolves from lexical scope per the same paragraph, so
  `with` is just renaming locals into themselves. Edited out.
- **§6.3 `finally` foot-gun is buried.** The doc literally has the example
  `log::error("do_stuff failed"); // only logs on error? no — always logs.`
  That's the migration foot-gun in plain sight. Without a lint, every team
  porting `WHEN OTHERS THEN log; RAISE;` to `finally` will log "failed" on
  successful calls and not notice for months. Added a real lint proposal.
- **§6.5 four row terminators is one too many.** `.one_or_none()` is the odd
  one out — it's `.one()` minus the absence-is-error semantics, which is
  almost always exactly the semantics you wanted. Reordered the table to put
  `.one()` first as the boring default and added prose telling readers when
  *not* to reach for `.one_or_none()`. Would not be sorry to see it cut
  entirely in v1.
- **`T?` vs `Option<T>` duplication.** Doc had them as exact synonyms with no
  guidance. Picked `T?` as the surface form and made `Option<T>` an internal
  alias used by prelude signatures only. `pell fmt` rewrites the other way.
- **§6.5.1 "uncatchable invariant panics" is overstated.** The emitted
  `RAISE_APPLICATION_ERROR(-20001)` is a perfectly normal Oracle exception
  and any hand-written PL/SQL upstream can catch it with `WHEN OTHERS`. The
  doc's "uncatchable" claim is true *within `pell`* but not at the runtime
  boundary. Added an honesty caveat.
- **§3 table weakest rows.** Three I'd argue against:
  1. ~~Comments `--` vs `//`~~ — cut. Pure churn; doesn't earn a table row.
     Editor support handles both anyway.
  2. "Compiler hints" row oversold the symmetry (it makes annotations look 1:1
     with pragmas; §9.2 shows the real constraints). Rewrote to mention closed
     set + validated combinations.
  3. "Records" row implies `record User { … }` is a strict win over
     `TYPE … IS RECORD`, but doesn't mention the lowering cost (every record
     becomes a `%rowtype`-shaped PL/SQL record type and a marshaller).
     Didn't edit — keep an eye on this in M1.
- **§4.6 pipelines** are oversold. Without auto-fusion (punted to v2), a
  pipeline over a `sql!{}` source materializes the result set and runs
  `filter`/`map` in PL/SQL. That's a real perf trap for large queries.
  Added the caveat in-line; would not be sad to cut §4.6 entirely until v2.
- **§13 (prior art) and §14 (naming)** add no engineering value. §14 trimmed
  to one sentence; §13 left because it's at least defensible as "reading
  list."
- **`fn` returning `Unit` = procedure (§3, §4.1).** Distinguishing procedures
  from functions in PL/SQL has stack-trace implications (procedures don't
  appear in `DBMS_UTILITY.FORMAT_CALL_STACK` the same way). Worth checking
  whether the unification costs us debugging clarity. Not edited; flagged
  for Shaun.
- **`set<T> = map<T, unit>` sugar (§5.1).** Cute, but iteration order will
  be unpredictable in ways users won't expect from a "set." Not edited.

## A realistic 200-line module sketch

This is what convinced me the language is worth the trouble — and where I
found half the concerns above. Module is a billing-ish thing because that's
where I spend my outages.

```pell
module billing.charges;

import std::log;
import std::time::{now, today};
import billing::accounts;

// --- types -------------------------------------------------------------

record Charge {
  id:           number,
  account_id:   number,
  amount:       number(12, 2),
  currency:     text,           // ISO 4217
  status:       ChargeStatus,
  idem_key:     text,
  created_at:   timestamp,
  posted_at:    timestamp?,     // null until posted
  failure_code: text?,          // null on success
}

enum ChargeStatus { Pending, Posted, Failed, Refunded }

// --- errors ------------------------------------------------------------

error NotFound          { entity: text, id: number }
error InsufficientFunds { account_id: number, shortfall: number(12, 2) }
error AccountFrozen     { account_id: number, reason: text }
error DuplicateCharge   { idem_key: text }
error LedgerOutOfSync   { account_id: number, expected: number, found: number }

// --- read paths --------------------------------------------------------

pub fn find_charge(id: number) -> Result<Charge, NotFound> {
  return sql! {
    select id, account_id, amount, currency, status,
           idem_key, created_at, posted_at, failure_code
    from charges where id = :id
  }.one().map_err(|_| NotFound { entity: "charge", id });
}

pub fn list_pending_for(account_id: number) -> list<Charge> {
  return sql! {
    select id, account_id, amount, currency, status,
           idem_key, created_at, posted_at, failure_code
    from charges
    where account_id = :account_id and status = 'PENDING'
    order by created_at
  }.collect();
}

// --- write path --------------------------------------------------------

pub fn post_charge(
  account_id: number,
  amount:     number(12, 2),
  currency:   text,
  idem_key:   text,
) -> Result<Charge, InsufficientFunds | AccountFrozen | DuplicateCharge | LedgerOutOfSync> {
  let span = log::span("post_charge", account_id, amount, currency, idem_key);

  // Idempotency: same key on the same account is a no-op return, not an error.
  let existing = sql! {
    select id, account_id, amount, currency, status,
           idem_key, created_at, posted_at, failure_code
    from charges
    where account_id = :account_id and idem_key = :idem_key
  }.first();

  if let Some(c) = existing {
    return Ok(c);
  }

  let account = accounts::find_locked(account_id)?;   // RowLock; bubbles NotFound? no, accounts::find_locked panics on missing
  if account.frozen {
    return Err(AccountFrozen {
      account_id,
      reason: account.freeze_reason.expect("frozen account has a reason"),
    });
  }
  if account.balance < amount {
    return Err(InsufficientFunds {
      account_id,
      shortfall: amount - account.balance,
    });
  }

  let charge_id = sql! {
    insert into charges(account_id, amount, currency, status, idem_key, created_at)
    values (:account_id, :amount, :currency, 'PENDING', :idem_key, :now)
    returning id into :out
  }.returning::<number>()
   .map_err(oracle::DupValOnIndex, |_| DuplicateCharge { idem_key });

  // Ledger update — must reconcile.
  let new_balance = account.balance - amount;
  let updated = sql! {
    update accounts
    set balance = :new_balance, version = version + 1
    where id = :account_id and version = :account.version
  }.rowcount();

  if updated != 1 {
    return Err(LedgerOutOfSync {
      account_id,
      expected: account.version,
      found:    account.version,   // racy read; real code refetches
    });
  }

  sql! {
    update charges set status = 'POSTED', posted_at = :now where id = :charge_id
  };

  return find_charge(charge_id).expect("just-inserted charge must exist");

} finally {
  log::info("post_charge took {span.elapsed()}ms");
  //          ^^^ this is the foot-gun. If I had written
  //          log::error("post_charge failed") here by reflex from PL/SQL,
  //          it would log "failed" on every successful call. The lint
  //          proposed in §6.3 catches that.
}

// --- batch refund ------------------------------------------------------

@autonomous
pub fn refund_batch(ids: list<number>) -> Result<int, LedgerOutOfSync> {
  // Single round-trip; lists pass directly as nested tables.
  let n = sql! {
    update charges
    set status = 'REFUNDED'
    where id in (select column_value from table(:ids))
      and status = 'POSTED'
  }.rowcount();

  // Audit trail in the autonomous tx so it survives a rollback above us.
  sql! {
    insert into refund_audit(refunded_at, count)
    values (:now, :n)
  };
  commit;

  return Ok(n);
}

// --- tests -------------------------------------------------------------

@test
fn idempotency_returns_existing_charge() {
  // pure-pell test, no DB
  // …
}

@test(db)
fn double_post_with_same_idem_key_is_idempotent() {
  // requires Oracle 23 connection
  // …
}
```

**What the sketch surfaces:**

- The `with (…)` clause from §4.5 is not used anywhere — every call site
  already had the locals named correctly. Confirms it doesn't earn its keep.
- I reached for `.one()` (not `.first()`, not `.one_or_none()`) for the
  primary read. That confirms `.one()` should be the documented default.
- The `finally` block at the end of `post_charge` is exactly where a porting
  developer would write `log::error("post_charge failed")` by muscle memory.
  The lint proposed in §6.3 (added in this pass) catches that.
- `find_locked(account_id)?` propagates an error variant the caller didn't
  declare — would be a compile error and force me to add `NotFound` to the
  signature. That's correct, and the kind of pain that pays off.
- `.expect("just-inserted charge must exist")` on the post-insert refetch
  is the canonical "this shouldn't happen" usage. Reads well at 11pm.
- `.returning::<number>()` is *not* in the design doc. I had to make it up.
  Returning-into for DML is a real PL/SQL pattern and §4.5 doesn't address
  it. **Gap.**
- `.rowcount()` on a `sql!{}` UPDATE/DELETE is *not* in the design doc
  either. PL/SQL's `SQL%ROWCOUNT` is essential after every DML. **Gap.**
- `accounts::find_locked` (a `SELECT … FOR UPDATE`) has no surface in §4.5.
  Locking semantics for `sql!{}` are unaddressed. **Gap.**
- Optimistic concurrency (`version = :account.version`) works fine but the
  doc never models it; should `.rowcount()` checks against expected values
  get sugar? Probably not, but worth noting.
- I didn't reach for `Option<T>` once. `T?` was always clearer. Confirms the
  alias-demotion edit.

## Proposed edits (made in this worktree)

- **§1 (Goals)** — replaced `Option<T> (a.k.a. T?)` with `T?` to commit to one
  spelling in the headline. Added a one-line non-goal: brevity that hurts grep
  is a regression.
- **§3 (table)** — dropped the "Comments" row (pure churn, doesn't earn a
  row). Rewrote the "Compiler hints" row to mention the closed set and
  validated combinations instead of listing pragma names.
- **§4.5 (`sql!{}`)** — removed the `with (…)` clause from the running
  example. Rewrote the surrounding prose to say binds come from lexical scope,
  with a one-paragraph note explaining we considered `with (…)` and dropped it
  as ceremony.
- **§4.6 (pipelines)** — added an honest caveat that until v2 auto-fusion
  lands, pipelines over `sql!{}` materialize and run in PL/SQL, which is a
  perf trap.
- **§5 (type system)** — clarified that `T?` is the surface form and
  `Option<T>` is an internal prelude alias; `fmt` rewrites the latter.
- **§5.1.1 (`list<T>`)** — added a tradeoff note that `xs[i] -> T?`
  makes tight indexed loops more verbose, and steered readers toward
  `for x in xs` / `enumerate()` as the documented norm. Also fixed an
  inconsistency where the example used `Option<number>` instead of
  `number?` after the §5 edit.
- **§6.3 (finally)** — promoted the always-runs foot-gun from a buried
  doc note to a named warning section. Proposed a concrete compiler lint
  (heuristic regex on error-ish words in `finally` bodies that don't
  reference the caught error) with an `@allow(finally_error_log)` opt-out.
- **§6.5 (terminators)** — reordered the table to put `.one()` first as
  the boring default. Added a paragraph explaining when *not* to reach for
  `.one_or_none()`.
- **§6.5.1 (invariant panics)** — added an "Honesty caveat" paragraph
  acknowledging that the emitted `RAISE_APPLICATION_ERROR(-20001)` is
  catchable from hand-written PL/SQL upstream. The `pell` compiler can
  prevent `pell` code from catching it; it can't prevent legacy PL/SQL
  from doing so.
- **§14 (naming)** — collapsed the candidate table to one sentence.
- **End-of-doc fluff** — removed the "Argue with anything. The §3 table
  and §6 (errors) are the bits that will shape every subsequent
  decision." flourish.

## Open questions for Shaun

1. **`.returning::<T>()` and `.rowcount()` are missing from §4.5.** Every
   PL/SQL maintainer needs `RETURNING … INTO` for inserts and
   `SQL%ROWCOUNT` for DML. What's the surface? I deliberately didn't
   invent one; this is a real gap for M2.
2. **`SELECT … FOR UPDATE` / locking.** `sql!{}` says nothing about
   pessimistic locking. The 200-line sketch needed it for
   `accounts::find_locked`. Is locking implicit at the iterator boundary
   when the SQL contains `FOR UPDATE`, or is there a separate method?
3. **`.one_or_none()` — keep or cut?** I argued for demoting it; would
   you cut it from v1 entirely and let users compose `.one()` +
   `.map_err`?
4. **Stack traces for `fn returning Unit`.** PL/SQL distinguishes
   procedures from functions in `FORMAT_CALL_STACK`. Does unifying them
   in `pell` cost us debugging clarity, and if so, is it worth a flag?
5. **`@must_use` default for `Result<…>` returns.** §9.4 says it's
   default. Is this also true for `sql!{}` write statements (whose
   iterator-shape is `Result<Unit, oracle::*>`)? If yes, every UPDATE
   needs a `;` *and* a `?` or explicit discard — confirm.
6. **`set<T>` as `map<T, unit>` sugar.** Convenient, but iteration order
   in `set<text>` will be alphabetical (because of the assoc-array
   index), which is a surprise nobody asked for. Keep, or use a
   distinct backing type?
7. **`finally_body` lowering (§6.3) uses a nested procedure.** PL/SQL
   nested procedures forward-declare; will `pell` always emit `finally`
   nested procedures *before* the body in a way that doesn't break when
   the body references locals declared in the same block? Worth a
   second look in M3.
