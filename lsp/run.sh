#!/bin/sh
# Launch the pell LSP server over stdio. Editors / IDEs invoke this.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Put both the compiler package and the lsp package on PYTHONPATH.
export PYTHONPATH="$REPO_ROOT/compiler:$SCRIPT_DIR:${PYTHONPATH:-}"
exec "$REPO_ROOT/compiler/.venv/bin/python" -m pell_lsp.server "$@"
