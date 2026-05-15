"""End-to-end: compile each example file and check basic properties."""
import re
from pathlib import Path

from pell.parser import parse
from pell.emitter import emit


EXAMPLES = Path(__file__).parent.parent / "examples"


def _examples():
    return sorted(EXAMPLES.glob("*.pell"))


def test_all_examples_compile_without_exception():
    for path in _examples():
        src = path.read_text()
        module = parse(src, str(path))
        sql = emit(module)
        assert sql.strip(), f"empty SQL for {path}"
        assert "CREATE OR REPLACE PACKAGE" in sql, f"{path} missing package header"


def test_each_example_emits_matching_package_body():
    for path in _examples():
        src = path.read_text()
        module = parse(src, str(path))
        sql = emit(module)
        pkg = module.package_name
        # spec
        assert f"CREATE OR REPLACE PACKAGE {pkg} AS" in sql
        # body
        assert f"CREATE OR REPLACE PACKAGE BODY {pkg} AS" in sql
        # closing
        assert f"END {pkg};" in sql


def test_no_unresolved_todos_in_critical_paths():
    """Spot-check: examples should not leave TODO markers in *emitted* declarations
    (TODO in lower hints/notes is OK, but a TODO in a TYPE/DECL line means it's
    syntactically broken)."""
    for path in _examples():
        src = path.read_text()
        sql = emit(parse(src, str(path)))
        # the only allowed TODO is in the "please annotate" inferrals; otherwise flag.
        forbidden = re.findall(r"/\* TODO:.*?\*/", sql)
        assert not forbidden, f"{path}: emitted /* TODO */ markers: {forbidden}"
