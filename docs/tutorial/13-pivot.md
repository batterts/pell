---
title: Pivot — typed and dynamic
parent: Tutorial
nav_order: 13
---

Oracle's PIVOT clause widens rows into columns. Pell wraps it in two
shapes: a `pivot::sum` builtin for the common case where the column
set is known at compile time (via an enum), and a `pivot::sum_dyn`
form for the case where the column set is discovered at runtime.

## Why a wrapper around PIVOT?

Oracle's PIVOT syntax is dense and easy to get wrong:

```sql
SELECT * FROM (
  SELECT department, status, salary FROM employees
)
PIVOT (
  SUM(salary) FOR status IN ('active' AS active, 'leave' AS leave, 'terminated' AS terminated)
);
```

The `FOR ... IN (...)` clause is the column list. With `pivot::sum`,
pell lets you reference a pell `enum` so the column list is checked at
compile time and renders automatically.

## Typed pivot — `pivot::sum`

Declare an enum:

```pell
pub enum Status {
    active     = "active",
    leave      = "leave",
    terminated = "terminated",
}
```

Then pivot:

```pell
pub record DeptPivot {
    department: text,
    active:     number,
    leave:      number,
    terminated: number,
}

pub fn pivot_by_dept() -> list<DeptPivot> {
    let xs: list<DeptPivot> = pivot::sum(
        source = sql! { select department, status, salary from employees },
        rows   = department,
        col    = status,
        over   = Status,
        value  = salary,
    ).collect();
    return xs;
}
```

lowers to:

```sql
FUNCTION pivot_by_dept RETURN t_DeptPivot_list IS
  l_xs t_DeptPivot_list;
BEGIN
  SELECT department, active, leave, terminated
    BULK COLLECT INTO l_xs
    FROM (
      SELECT department, status, salary FROM employees
    ) PIVOT (
      SUM(salary) FOR status IN (
        'active'     AS "active",
        'leave'      AS "leave",
        'terminated' AS "terminated"
      )
    );
  RETURN l_xs;
END pivot_by_dept;
```

The `over = Status` ties the column list to the enum's variants; if you
add a new status variant to the enum, the pivot grows automatically.
If you change a variant string, the pivot column header changes too.

## Dynamic pivot — `pivot::sum_dyn`

When the columns aren't known until runtime (e.g., the user picks
which statuses to pivot on), use `pivot::sum_dyn`:

```pell
unsafe fn pivot_for_statuses(statuses: list<text>) -> cursor<rowtype<employees>>
    @touches(employees)
{
    return pivot::sum_dyn(
        source   = "select department, status, salary from employees",
        rows     = "department",
        col      = "status",
        columns  = statuses,
        value    = "salary",
    );
}
```

`pivot::sum_dyn` builds the PIVOT SQL string at runtime, then opens a
SYS_REFCURSOR over it. The return type is a cursor because the column
list is dynamic — pell can't generate a typed record for it.

Caller pattern:

```pell
for row in sql! { select * from table(pivot_for_statuses(:statuses)) } { ... }
```

`pivot::sum_dyn` is only available inside `unsafe fn` (because the
source string is interpolated dynamically). Use `@touches` to declare
the underlying table for dependency tracking.

## When to use which

- **Typed pivot** when the column set is bounded and stable. Best
  ergonomics: result rows are typed records, no string-mangling.
- **Dynamic pivot** when the column set is user-driven or grows over
  time. Slight loss of type safety, but you get the flexibility.

## Beyond `pivot::sum`

Oracle's PIVOT supports any aggregate — `MIN`, `MAX`, `AVG`,
`COUNT`. v0 of pell only exposes `pivot::sum`; more aggregates
will land as `pivot::min`, `pivot::max`, etc. when needed.

For complex cases (multiple aggregate columns, multiple pivot
columns), drop back to raw `sql!{}` with the explicit PIVOT clause —
pell's wrapper covers the common 80%.

## Where to go next

- [pell exec and pell repl](./14-exec-and-repl.md) — try out pivots
  interactively against your data.
- [Reference: annotations](../reference/annotations.md) — the
  annotation surface (including `@touches`).