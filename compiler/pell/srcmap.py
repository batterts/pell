"""pell ↔ PL/SQL source-line maps for the debugger.

A debug build (`pell build --debug` / `pell deploy --debug`) makes the
emitter append a trailing `-- @pell:<line>` marker to every emitted
statement (plus each fn/method header). This module turns the emitted
SQL text into a line map per CREATE'd unit:

    pell line  ->  unit line   (breakpoint placement)
    unit line  ->  pell line   (stack-frame mapping)

"Unit line" is the line number Oracle reports — line 1 is the line
containing the unit keyword, because `CREATE OR REPLACE ` is stripped
but the rest of that first line is kept. JDWP locations and ALL_SOURCE
both use this numbering, so the map needs no database round-trip: the
emitted text is the deployed text.

JDWP exposes PL/SQL units as synthetic classes:

    $Oracle.PackageBody.SCHEMA.PKG    (where statements live)
    $Oracle.TypeBody.SCHEMA.TYP       (sealed-type method bodies)
    $Oracle.Block                     (anonymous blocks — the debug stub)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MARKER_RE = re.compile(r"--\s*@pell:(\d+)\b")
# `CREATE OR REPLACE [EDITIONABLE] PACKAGE BODY [schema.]name` etc.
UNIT_RE = re.compile(
    r"^\s*CREATE\s+OR\s+REPLACE\s+(?:EDITIONABLE\s+)?"
    r"(PACKAGE\s+BODY|TYPE\s+BODY|PACKAGE|TYPE|FUNCTION|PROCEDURE|TRIGGER)\s+"
    r'(?:("?[A-Za-z0-9_$#]+"?)\s*\.\s*)?("?[A-Za-z0-9_$#]+"?)',
    re.IGNORECASE,
)

# unit kind -> JDWP class-name segment
_JDWP_KIND = {
    "PACKAGE BODY": "PackageBody",
    "TYPE BODY": "TypeBody",
    "PACKAGE": "Package",
    "TYPE": "Type",
    "FUNCTION": "Function",
    "PROCEDURE": "Procedure",
    "TRIGGER": "Trigger",
}


@dataclass
class UnitMap:
    kind: str                 # "PACKAGE BODY", "TYPE BODY", ...
    schema: str | None        # None = connected schema
    name: str                 # unit name, as written (unquoted, upper)
    file_line: int            # 1-based line of the CREATE in the .sql file
    # marker entries, ascending by unit_line
    entries: list[dict] = field(default_factory=list)  # {pell_line, unit_line}

    def jdwp_class(self, default_schema: str = "") -> str:
        schema = (self.schema or default_schema).upper()
        return f"$Oracle.{_JDWP_KIND[self.kind]}.{schema}.{self.name.upper()}"

    def unit_line_for_pell(self, pell_line: int) -> int | None:
        """Best unit line for a breakpoint at `pell_line`: the first
        marker at or after that pell line (markers within a unit are
        not necessarily sorted by pell line — fns can be reordered —
        so prefer an exact hit, else the nearest following marker)."""
        exact = [e for e in self.entries if e["pell_line"] == pell_line]
        if exact:
            return exact[0]["unit_line"]
        after = [e for e in self.entries if e["pell_line"] > pell_line]
        if after:
            return min(after, key=lambda e: e["pell_line"])["unit_line"]
        return None

    def pell_line_for_unit(self, unit_line: int) -> int | None:
        """Pell line for a stack frame at `unit_line`: nearest marker
        at or above (statements span multiple emitted lines)."""
        at_or_above = [e for e in self.entries if e["unit_line"] <= unit_line]
        if not at_or_above:
            return None
        return max(at_or_above, key=lambda e: e["unit_line"])["pell_line"]

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "schema": self.schema,
            "name": self.name,
            "jdwp_class": self.jdwp_class(),
            # 1-based line of the CREATE in the emitted .sql FILE — lets
            # the IDE convert unit lines to file lines (unit line 1 IS
            # the CREATE line): file_line + unit_line - 1.
            "file_line": self.file_line,
            "entries": self.entries,
        }


def _clean_ident(s: str | None) -> str | None:
    if s is None:
        return None
    return s.strip('"').upper()


def compute_srcmap(sql_text: str) -> list[UnitMap]:
    """Scan emitted SQL for CREATE'd units and their @pell markers."""
    units: list[UnitMap] = []
    current: UnitMap | None = None
    for i, line in enumerate(sql_text.splitlines(), start=1):
        m = UNIT_RE.match(line)
        if m:
            kind = re.sub(r"\s+", " ", m.group(1).upper())
            current = UnitMap(
                kind=kind,
                schema=_clean_ident(m.group(2)),
                name=_clean_ident(m.group(3)) or "",
                file_line=i,
            )
            units.append(current)
            continue
        if current is None:
            continue
        mk = MARKER_RE.search(line)
        if mk:
            current.entries.append({
                "pell_line": int(mk.group(1)),
                # Oracle strips "CREATE OR REPLACE " but keeps the rest
                # of the first line, so unit line 1 IS the CREATE line.
                "unit_line": i - current.file_line + 1,
            })
    return units


def srcmap_json(sql_text: str, pell_file: str | None = None) -> dict:
    """The shape `pell srcmap` prints and the IDE consumes."""
    return {
        "pell_file": pell_file,
        "units": [u.to_json() for u in compute_srcmap(sql_text)],
    }
