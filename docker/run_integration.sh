#!/bin/sh
# One-shot integration run: boot Oracle, run the full pell test suite
# (emitter + live deploy + runtime execution) against it, tear down.
# Exit code mirrors pytest's.
#
#     ./docker/run_integration.sh
#
# First run downloads the Oracle Free image (~1.5 GB) and initializes
# a datafile — allow a few minutes. Subsequent runs reuse the image.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose -f $SCRIPT_DIR/docker-compose.yml"

cleanup() { $COMPOSE down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

$COMPOSE up --build --abort-on-container-exit --exit-code-from tests
