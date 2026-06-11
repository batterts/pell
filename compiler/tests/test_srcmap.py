"""Debug line markers + srcmap — the debugger's pell↔PL/SQL line map."""
from pell.parser import parse
from pell.emitter import emit
from pell.srcmap import compute_srcmap

SRC = """\
module hello;
import std::logger;

pub fn greet(name: text) -> text {
    logger::info("greeting {name}");
    return "hello, " + name;
}
"""


def test_no_markers_by_default():
    sql = emit(parse(SRC))
    assert "@pell:" not in sql


def test_debug_markers_on_statements_and_fn_header():
    sql = emit(parse(SRC), debug=True)
    # fn header marks the `pub fn` line; each stmt marks its own line.
    assert "-- @pell:4" in sql   # pub fn greet
    assert "-- @pell:5" in sql   # logger::info
    assert "-- @pell:6" in sql   # return


def test_srcmap_roundtrip():
    sql = emit(parse(SRC), debug=True)
    units = compute_srcmap(sql)
    body = next(u for u in units if u.kind == "PACKAGE BODY")
    assert body.name == "HELLO"

    # Breakpoint on the `return` (pell line 6) lands on a real unit line,
    # and that unit line maps back to pell line 6.
    unit_line = body.unit_line_for_pell(6)
    assert unit_line is not None
    assert body.pell_line_for_unit(unit_line) == 6

    # A frame BETWEEN two markers maps to the nearest marker above.
    assert body.pell_line_for_unit(unit_line + 0) == 6

    # Unit line 1 is the CREATE line: Oracle strips "CREATE OR REPLACE "
    # but keeps the rest of the line, so file_line offsets must agree.
    lines = sql.splitlines()
    assert "PACKAGE BODY" in lines[body.file_line - 1].upper()
    # The marker for pell line 5 points at the emitted logger call.
    ul5 = body.unit_line_for_pell(5)
    assert "logger.info" in lines[body.file_line - 1 + ul5 - 1]


def test_srcmap_jdwp_class_names():
    sql = emit(parse("module hr::employees;\npub fn ping() {\n    p(1);\n}\n"),
               debug=True)
    units = compute_srcmap(sql)
    body = next(u for u in units if u.kind == "PACKAGE BODY")
    assert body.jdwp_class() == "$Oracle.PackageBody.HR.EMPLOYEES"
    # Unqualified modules defer to the connected schema at debug time.
    sql2 = emit(parse(SRC), debug=True)
    body2 = next(u for u in compute_srcmap(sql2) if u.kind == "PACKAGE BODY")
    assert body2.jdwp_class("PELL_TEST") == "$Oracle.PackageBody.PELL_TEST.HELLO"


def test_breakpoint_on_blank_line_snaps_forward():
    src = """\
module a;

pub fn f(n: number) -> number {
    let x = n + 1;

    let y = x * 2;
    return y;
}
"""
    sql = emit(parse(src), debug=True)
    body = next(u for u in compute_srcmap(sql) if u.kind == "PACKAGE BODY")
    # Line 5 is blank — breakpoint snaps to the next stmt (line 6).
    assert body.unit_line_for_pell(5) == body.unit_line_for_pell(6)
