# 7. Types, sealed types, and aggregates

pell has a small object-oriented surface for cases where records
aren't expressive enough: user-defined `type`s with methods, sealed
type hierarchies for sum-of-records, and user-defined `aggregate`s
that participate in SQL `GROUP BY`. All three lower to Oracle OBJECT
TYPE, which means they're schema-level — installed once, used across
packages.

## When to use which

| Surface              | What it gives you                                          |
|----------------------|------------------------------------------------------------|
| `record`             | A package-body-private RECORD with named fields. PL/SQL only. |
| `pub record`         | A package-spec RECORD. Still PL/SQL only.                  |
| `type`               | Schema-level OBJECT TYPE with methods. Usable in SQL.      |
| `sealed type`        | An OBJECT hierarchy with a closed set of subtypes.         |
| `aggregate`          | A user-defined GROUP BY aggregator.                        |

Records are by far the most common; reach for `type` only when you
need methods, polymorphism, or SQL-level visibility.

## `type` — typed objects with methods

```pell
pub type Money {
    cents: number,

    fn dollars() -> number {
        return self.cents / 100;
    }

    fn plus(other: Money) -> Money {
        return Money { cents: self.cents + other.cents };
    }
}
```

lowers to a schema-level OBJECT TYPE with member methods:

```sql
CREATE OR REPLACE TYPE money AS OBJECT (
  cents NUMBER,
  MEMBER FUNCTION dollars RETURN NUMBER,
  MEMBER FUNCTION plus(other money) RETURN money
);
/

CREATE OR REPLACE TYPE BODY money AS
  MEMBER FUNCTION dollars RETURN NUMBER IS
  BEGIN
    RETURN (SELF.cents / 100);
  END dollars;

  MEMBER FUNCTION plus(other money) RETURN money IS
  BEGIN
    RETURN money(SELF.cents + other.cents);
  END plus;
END;
/
```

Methods use `self.<field>` to access the receiver. The compiler turns
`self` into PL/SQL's `SELF`.

### Caveats

- **Don't name methods after reserved Oracle words**: `add`, `set`,
  `merge`, `update`, `delete`, etc. cause PLS-00330. Rename to
  `plus`, `assign`, etc.
- **Sealed-only inheritance**. There's no `extends` syntax outside the
  sealed-type case below. If you want polymorphism, use `sealed type`.

## `sealed type` — closed-set hierarchies

A sealed type is like a Rust enum-with-payloads — a parent type with
a known set of subtypes (cases). All cases must be declared in the
same module:

```pell
pub sealed type Shape {
    case Circle { radius: number }
    case Rectangle { width: number, height: number }
    case Triangle { base: number, height: number }

    fn area() -> number {
        match self {
            Circle { radius } => return 3.14159 * radius * radius;
            Rectangle { width, height } => return width * height;
            Triangle { base, height } => return 0.5 * base * height;
        }
    }
}
```

lowers to:

```sql
-- Parent
CREATE OR REPLACE TYPE shape AS OBJECT (
  sys_tag_ NUMBER,
  MEMBER FUNCTION area RETURN NUMBER
) NOT FINAL NOT INSTANTIABLE;
/

-- Each case as a subtype
CREATE OR REPLACE TYPE circle UNDER shape (
  radius NUMBER,
  OVERRIDING MEMBER FUNCTION area RETURN NUMBER
);
/
...
```

The `sys_tag_` field exists because Oracle requires OBJECT types to
have at least one attribute; pell synthesizes one when the parent
would otherwise be empty.

### Pattern matching

`match` on a sealed type uses the case names as patterns:

```pell
let s: Shape = Circle { radius: 5 };
let a: number = s.area();   // 78.5395

match s {
    Circle { radius } => log::info("circle r={radius}"),
    Rectangle { width, height } => log::info("rect {width}x{height}"),
    Triangle { .. } => log::info("triangle (we don't care about size)"),
}
```

The `..` pattern matches and discards remaining fields.

## `aggregate` — user-defined GROUP BY aggregators

Aggregates are functions that fold a column down to a scalar value,
usable in SQL `SELECT` lists with `GROUP BY`:

```pell
pub aggregate sum_squares(n: number) -> number {
    state { acc: number = 0; }
    step(v: number) { self.acc = self.acc + v * v; }
    finish() -> number { return self.acc; }
}
```

After installing, you can use it in SQL:

```sql
SELECT department_id, sum_squares(salary) FROM employees GROUP BY department_id;
```

The lowering uses Oracle's `ODCIAggregate` interface — an OBJECT TYPE
plus a `FUNCTION ... USING <type>` declaration:

```sql
CREATE OR REPLACE TYPE sum_squares_t AS OBJECT (
  acc NUMBER,
  STATIC FUNCTION ODCIAggregateInitialize(actx IN OUT sum_squares_t) RETURN NUMBER,
  MEMBER FUNCTION ODCIAggregateIterate(self IN OUT sum_squares_t, v NUMBER) RETURN NUMBER,
  MEMBER FUNCTION ODCIAggregateMerge(self IN OUT sum_squares_t, other IN sum_squares_t) RETURN NUMBER,
  MEMBER FUNCTION ODCIAggregateTerminate(self IN sum_squares_t, returnValue OUT NUMBER, flags IN NUMBER) RETURN NUMBER
);
/

CREATE OR REPLACE FUNCTION sum_squares(input NUMBER) RETURN NUMBER
  PARALLEL_ENABLE AGGREGATE USING sum_squares_t;
/
```

The pell surface hides the ceremony; you just write the
`state` / `step` / `finish` triple.

### `@parallel` — opt-in parallel aggregation

If your aggregate is associative and commutative, opt into parallel
execution:

```pell
@parallel
pub aggregate sum_squares(n: number) -> number {
    state { acc: number = 0; }
    step(v: number) { self.acc = self.acc + v * v; }
    merge(other: t) { self.acc = self.acc + other.acc; }
    finish() -> number { return self.acc; }
}
```

`@parallel` requires a `merge(other: t)` implementation — Oracle uses
it to combine sub-aggregates from parallel slaves.

### Multi-arg aggregates

Oracle's ODCI interface is single-argument only. To take multiple
arguments, pell auto-generates a tuple-wrapper:

```pell
pub aggregate weighted_avg(value: number, weight: number) -> number {
    state {
        total_weighted: number = 0,
        total_weight:   number = 0,
    }
    step(v: number, w: number) {
        self.total_weighted = self.total_weighted + v * w;
        self.total_weight   = self.total_weight + w;
    }
    finish() -> number {
        if self.total_weight = 0 {
            return 0;
        }
        return self.total_weighted / self.total_weight;
    }
}
```

The lowering wraps `(value, weight)` into a `weighted_avg_args_t`
OBJECT and feeds that as the single ODCI argument. Use it from SQL
exactly as if it took two:

```sql
SELECT department_id, weighted_avg(salary, fte_fraction)
  FROM employees GROUP BY department_id;
```

## Where to go next

- [Pipelined and parallel functions](./08-pipelined-and-parallel.md) —
  user-defined table functions for SELECT FROM TABLE(...) patterns.
- [Dynamic SQL](./09-dynamic-sql.md) — `unsafe fn` and the dynamic
  bind surface.
