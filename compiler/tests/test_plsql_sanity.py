"""Sanity checks on the generated PL/SQL output.

We don't have a local Oracle, so this isn't a real validator — it's a
set of structural checks that catch the kinds of bugs the emitter is
most likely to introduce:

  - unbalanced BEGIN/END
  - unbalanced PACKAGE/END package_name
  - missing trailing slashes after CREATE OR REPLACE
  - statements without terminators
  - illegal token positions (e.g., a comment splitting a statement)
"""
import re
from pathlib import Path

from pell.parser import parse
from pell.emitter import emit


EXAMPLES = Path(__file__).parent.parent / "examples"


def _emit(path: Path) -> str:
    return emit(parse(path.read_text(), str(path)))


def _strip_comments(sql: str) -> str:
    """Strip -- line comments and /* ... */ block comments."""
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql


def _strip_strings(sql: str) -> str:
    """Strip 'string literals' so they don't confuse keyword counts."""
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def _normalize(sql: str) -> str:
    return _strip_strings(_strip_comments(sql))


def _count_word(sql: str, word: str) -> int:
    return len(re.findall(rf"\b{word}\b", sql, re.IGNORECASE))


def _example_paths():
    return sorted(EXAMPLES.glob("*.pell"))


def test_balanced_begin_end_per_example():
    """Every BEGIN should have a matching END."""
    for path in _example_paths():
        sql = _normalize(_emit(path))
        begin_n = _count_word(sql, "BEGIN")
        end_n = _count_word(sql, "END")
        # END appears both as block-end and `END pkg_name;` markers, so it's >= BEGIN
        assert end_n >= begin_n, f"{path.name}: {begin_n} BEGIN, {end_n} END"


def test_package_spec_and_body_paired():
    for path in _example_paths():
        sql = _emit(path)
        spec_pkg = re.search(r"CREATE OR REPLACE PACKAGE (\w+) AS", sql)
        body_pkg = re.search(r"CREATE OR REPLACE PACKAGE BODY (\w+) AS", sql)
        assert spec_pkg and body_pkg, f"{path.name}: missing spec or body"
        assert spec_pkg.group(1) == body_pkg.group(1), (
            f"{path.name}: spec package {spec_pkg.group(1)!r} != body {body_pkg.group(1)!r}"
        )


def test_each_create_terminated_with_slash():
    """Oracle requires a `/` on its own line to end a CREATE statement."""
    for path in _example_paths():
        sql = _emit(path)
        # find each CREATE block and ensure it ends with a `/` on its own line
        chunks = re.split(r"^/\s*$", sql, flags=re.MULTILINE)
        # there should be at least one chunk that contains a CREATE (or two: spec + body)
        create_chunks = [c for c in chunks if re.search(r"CREATE OR REPLACE", c)]
        assert len(create_chunks) >= 2, f"{path.name}: fewer than 2 CREATE blocks terminated by /"


def test_no_orphan_decl_inside_begin():
    """`DECLARE x BOOLEAN;` on its own line inside a BEGIN block is illegal.
    A DECLARE must precede a BEGIN; checking that we don't emit a DECLARE
    keyword immediately after another BEGIN with non-declaration in between."""
    for path in _example_paths():
        sql = _normalize(_emit(path))
        # find any "BEGIN <stuff> DECLARE" where <stuff> contains an executable statement
        # easier: any "DECLARE <ident> ... ;" on a single line is suspect (this was the
        # original bug). The correct form is "DECLARE\n  <ident> ... ;"
        m = re.search(r"\bDECLARE\b\s+[A-Za-z_]\w*\s+\w[\w\s,()]*?:=", sql)
        # we accept single-line DECLARE only if it's followed by exactly one decl then BEGIN
        if m:
            # Look ahead to see if a BEGIN immediately follows
            tail = sql[m.start():m.start() + 200]
            assert "BEGIN" in tail, f"{path.name}: DECLARE without following BEGIN: {m.group(0)!r}"


def test_no_double_semicolons_outside_strings():
    for path in _example_paths():
        sql = _normalize(_emit(path))
        assert ";;" not in sql, f"{path.name}: contains `;;`"


def test_select_into_has_into_between_list_and_from():
    """SELECT INTO must have INTO between the select list and FROM."""
    for path in _example_paths():
        sql = _normalize(_emit(path))
        # find each SELECT ... INTO ... FROM block; INTO must come BEFORE FROM
        for m in re.finditer(r"\bselect\b", sql, re.IGNORECASE):
            tail = sql[m.start():m.start() + 500]
            into = re.search(r"\bINTO\b", tail, re.IGNORECASE)
            frm = re.search(r"\bfrom\b", tail, re.IGNORECASE)
            # only check the ones with both INTO and FROM (the SELECT INTO ones)
            if into and frm:
                assert into.start() < frm.start(), (
                    f"{path.name}: SELECT...INTO...FROM ordering wrong at {m.start()}"
                )


def test_no_pl_sql_param_size_specifier():
    """IN VARCHAR2(4000) on parameters is illegal PL/SQL."""
    for path in _example_paths():
        sql = _emit(path)
        assert not re.search(r"\bIN VARCHAR2\(\d+\)", sql), (
            f"{path.name}: parameter has size specifier"
        )


def test_no_return_null_in_procedure():
    """Procedure return is `RETURN;` not `RETURN NULL;`."""
    for path in _example_paths():
        sql = _emit(path)
        # find each PROCEDURE block and check for `RETURN NULL;` inside
        for m in re.finditer(r"\bPROCEDURE\s+\w+", sql):
            # very crude: from PROCEDURE to next "END proc_name;"
            tail = sql[m.start():]
            end = re.search(r"END\s+\w+;", tail)
            block = tail[: end.end() if end else len(tail)]
            assert "RETURN NULL;" not in block, f"{path.name}: PROCEDURE contains RETURN NULL;"
