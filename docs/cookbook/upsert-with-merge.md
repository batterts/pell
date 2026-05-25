---
title: Upsert with MERGE
parent: Cookbook
nav_order: 6
---

## Problem

You want to "insert this row if it doesn't exist, update if it does."
Doing it as a SELECT-then-INSERT-or-UPDATE has a race condition; you
need a single atomic operation.

## Solution

Use Oracle's `MERGE` statement inside a `sql!{}` block. MERGE is the
atomic upsert in PL/SQL.

```pell
module inventory;

pub fn upsert_sku(sku: text, quantity: number) -> number {
    let n: number = sql! {
        merge into inventory_skus i
        using (select :sku as sku, :quantity as quantity from dual) s
           on (i.sku = s.sku)
        when matched then
          update set quantity = s.quantity, updated_at = sysdate
        when not matched then
          insert (sku, quantity, created_at, updated_at)
          values (s.sku, s.quantity, sysdate, sysdate)
    }.rowcount();
    return n;
}
```

`.rowcount()` returns the number of rows touched (1 for either path).

## How it lowers

```sql
FUNCTION upsert_sku(p_sku IN VARCHAR2, p_quantity IN NUMBER) RETURN NUMBER IS
  l_n NUMBER;
BEGIN
  MERGE INTO inventory_skus i
  USING (SELECT p_sku AS sku, p_quantity AS quantity FROM dual) s
     ON (i.sku = s.sku)
  WHEN MATCHED THEN
    UPDATE SET quantity = s.quantity, updated_at = SYSDATE
  WHEN NOT MATCHED THEN
    INSERT (sku, quantity, created_at, updated_at)
    VALUES (s.sku, s.quantity, SYSDATE, SYSDATE);
  l_n := SQL%ROWCOUNT;
  RETURN l_n;
END upsert_sku;
```

## Why MERGE and not INSERT ... ON DUPLICATE

Oracle doesn't have `INSERT ... ON DUPLICATE KEY UPDATE` (that's
MySQL). MERGE is the equivalent and is generally more flexible —
you can branch on arbitrary join conditions, not just primary key.

## Bulk upsert

To upsert a list, combine MERGE with `forall`:

```pell
pub fn upsert_all(skus: list<text>, qtys: list<number>) {
    forall i in skus.indices() {
        sql! {
            merge into inventory_skus tgt
            using (select :skus(i) as sku, :qtys(i) as quantity from dual) src
               on (tgt.sku = src.sku)
            when matched then
              update set quantity = src.quantity, updated_at = sysdate
            when not matched then
              insert (sku, quantity, created_at, updated_at)
              values (src.sku, src.quantity, sysdate, sysdate)
        }
    }
}
```

For very large input lists (10K+), this is dramatically faster than
calling `upsert_sku` in a loop.

## Idempotency

MERGE is naturally idempotent — running the same input twice produces
the same final state (the second run becomes an UPDATE with the same
values, a no-op semantically). If your row has computed timestamps,
those will refresh, but row identity and data stay stable.

## See also

- [Bulk insert with FORALL](./bulk-insert-with-forall.md)
- [SQL blocks tutorial](../tutorial/04-sql-blocks.md)