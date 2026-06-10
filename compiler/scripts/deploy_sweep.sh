#!/bin/sh
# ===========================================================================
# deploy_sweep.sh — deploy every example against a live Oracle and report.
#
# The anti-rot check: before `pell deploy` verified USER_ERRORS, examples
# could silently fail to compile for months. This sweep deploys each one
# and classifies the result:
#
#   ok        — built + installed + compiled clean
#   COMPILE   — Oracle rejected the package body (a real pell/example bug)
#   priv      — ORA-01031 / ORA-04050: schema-qualified module and the
#               connected user lacks cross-schema CREATE rights. Run
#               scripts/setup_example_schemas.sql as a DBA to clear these.
#   OTHER     — anything else (connectivity, etc.)
#
# Usage:
#   PELL_DB_URL=user/pass@host:port/service ./scripts/deploy_sweep.sh
#   ./scripts/deploy_sweep.sh /path/to/examples     # alternate dir
#
# Exit code: number of COMPILE/OTHER failures (0 = healthy).
# ===========================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPILER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
EXAMPLES_DIR="${1:-$COMPILER_DIR/examples}"
PYTHON="$COMPILER_DIR/.venv/bin/python"

if [ -z "${PELL_DB_URL:-}" ]; then
  echo "deploy_sweep: PELL_DB_URL not set — cannot deploy." >&2
  exit 2
fi

fails=0
for f in "$EXAMPLES_DIR"/*.pell; do
  name=$(basename "$f")
  out=$(PYTHONPATH="$COMPILER_DIR" "$PYTHON" -m pell deploy "$f" \
        --out-dir "$(mktemp -d)" 2>&1)
  if echo "$out" | grep -qE "compiled with [0-9]+ error|PLS-[0-9]"; then
    echo "COMPILE  $name"
    echo "$out" | grep -E "PLS-|compiled with" | head -2 | sed 's/^/         /'
    fails=$((fails + 1))
  elif echo "$out" | grep -qE "ORA-01031|ORA-04050"; then
    echo "priv     $name (run setup_example_schemas.sql as DBA)"
  elif echo "$out" | grep -q "✗"; then
    echo "OTHER    $name"
    echo "$out" | grep "✗" | head -1 | sed 's/^/         /'
    fails=$((fails + 1))
  else
    echo "ok       $name"
  fi
done

echo
if [ "$fails" -eq 0 ]; then
  echo "deploy_sweep: all examples healthy."
else
  echo "deploy_sweep: $fails failure(s)." >&2
fi
exit "$fails"
