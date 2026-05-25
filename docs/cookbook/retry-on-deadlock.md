---
title: Retry on deadlock
parent: Cookbook
nav_order: 4
---

## Problem

A transactional operation occasionally fails with ORA-00060 (deadlock
detected) or ORA-08177 (serializable isolation conflict). The user
just needs to wait a moment and try again.

## Solution

Wrap the function in `@retry`. Pell's retry loop catches transient
errors, rolls back to a SAVEPOINT, and tries again with backoff.

```pell
module orders;

pub record Order { id: number, status: text }

@retry(n=5, backoff=100, jitter=50, exponential=true, cap=2000)
pub fn promote_to_paid(order_id: number) -> Order {
    let o: Order = sql! {
        select id, status from orders where id = :order_id for update
    }.one();

    if o.status != "pending" {
        return o;
    }

    sql! { update orders set status = 'paid' where id = :order_id };
    return Order { id: order_id, status: "paid" };
}
```

## How it lowers

```sql
FUNCTION promote_to_paid(p_order_id IN NUMBER) RETURN t_order IS
  l_o t_order;
  l_pell_attempt PLS_INTEGER := 0;
BEGIN
  LOOP
    SAVEPOINT pell_attempt;
    BEGIN
      SELECT id, status INTO l_o.id, l_o.status
        FROM orders WHERE id = p_order_id FOR UPDATE;
      IF l_o.status <> 'pending' THEN
        RETURN l_o;
      END IF;
      UPDATE orders SET status = 'paid' WHERE id = p_order_id;
      l_o.id := p_order_id;
      l_o.status := 'paid';
      RETURN l_o;
    EXCEPTION
      WHEN OTHERS THEN
        IF pell_is_panic(SQLCODE) THEN RAISE; END IF;
        l_pell_attempt := l_pell_attempt + 1;
        ROLLBACK TO pell_attempt;
        IF l_pell_attempt >= 5 THEN RAISE; END IF;
        DBMS_SESSION.SLEEP(
          LEAST(0.1 * POWER(2, l_pell_attempt - 1) + DBMS_RANDOM.VALUE(0, 0.05),
                2.0)
        );
    END;
  END LOOP;
END promote_to_paid;
```

Each attempt:

1. Sets a `SAVEPOINT` so partial work can be undone.
2. Runs the body.
3. On any non-panic exception, increments the attempt counter,
   rolls back to the SAVEPOINT (undoing the partial work), and
   sleeps with exponential backoff + jitter.
4. After `n` attempts, re-raises the last error.

`@panic` errors (in SQLCODE range -20300..-20399, plus ZERO_DIVIDE
and friends) bypass the retry — they're programmer errors, not
transient.

## Tuning the parameters

| Scenario              | Suggested config                                     |
|-----------------------|------------------------------------------------------|
| Local transient lock  | `n=3, backoff=50, jitter=20`                         |
| Network-flaky service | `n=5, backoff=200, jitter=100, exponential=true, cap=2000` |
| Database deadlock     | `n=5, backoff=100, jitter=50, exponential=true`     |
| Slow recoverable error| `n=3, backoff=1000, jitter=500`                     |

Without `jitter`, multiple processes retrying simultaneously can fall
into lockstep and keep colliding. A small random jitter spreads them
out.

`cap` is the maximum sleep duration (in ms) — useful with
`exponential=true` to prevent an unreasonable last-attempt wait.

## What @retry CAN'T do

- **Recover from logic bugs.** If your code raises `InvariantViolation`
  (`@panic`), it'll re-raise immediately. Don't use `@retry` to hide
  programming errors.
- **Coexist with `finally`.** Pell rejects the combination at compile
  time — the SAVEPOINT semantics get confusing. Restructure your
  cleanup into a helper fn.
- **Work with `@autonomous` or `@pipelined`.** Same reason —
  rejected at compile time with a clear error.

## See also

- [Errors and @retry](../tutorial/06-errors-and-retry.md) — full
  treatment of error categories and how `pell_is_panic` decides.
- [Reference: annotations](../reference/annotations.md) — every
  `@retry` parameter.