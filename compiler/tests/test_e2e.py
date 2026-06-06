"""End-to-end: compile each example file and check basic properties."""
import re
from pathlib import Path

from pell.parser import parse
from pell.emitter import emit, scan_project_records
from pell.repl import scan_project_signatures


EXAMPLES = Path(__file__).parent.parent / "examples"

# One scan for the whole module. Same shape `pell build` uses so
# cross-package examples resolve under the import-scoped rules.
_PROJECT_SIGS = scan_project_signatures()
_PROJECT_RECS = scan_project_records()


def _examples():
    return sorted(EXAMPLES.glob("*.pell"))


def _emit_for(path: Path):
    return emit(
        parse(path.read_text(), str(path)),
        source_path=str(path),
        project_signatures=_PROJECT_SIGS,
        project_records=_PROJECT_RECS,
    )


def test_all_examples_compile_without_exception():
    for path in _examples():
        sql = _emit_for(path)
        assert sql.strip(), f"empty SQL for {path}"
        assert "CREATE OR REPLACE PACKAGE" in sql, f"{path} missing package header"


def test_each_example_emits_matching_package_body():
    for path in _examples():
        src = path.read_text()
        module = parse(src, str(path))
        sql = _emit_for(path)
        qn = module.qualified_name             # schema-qualified or bare
        pkg = module.package_name              # bare package name (for END clause)
        assert f"CREATE OR REPLACE PACKAGE {qn} AS" in sql
        assert f"CREATE OR REPLACE PACKAGE BODY {qn} AS" in sql
        # END clause uses just the package name (Oracle accepts either)
        assert f"END {pkg};" in sql


def test_no_unresolved_todos_in_critical_paths():
    """Spot-check: examples should not leave TODO markers in *emitted* declarations
    (TODO in lower hints/notes is OK, but a TODO in a TYPE/DECL line means it's
    syntactically broken)."""
    for path in _examples():
        sql = _emit_for(path)
        # the only allowed TODO is in the "please annotate" inferrals; otherwise flag.
        forbidden = re.findall(r"/\* TODO:.*?\*/", sql)
        assert not forbidden, f"{path}: emitted /* TODO */ markers: {forbidden}"
