#!/usr/bin/env bash
# run_tests.sh — Run pytest with bytecode caching disabled.
#
# The Databricks /Workspace FUSE mount does not support __pycache__ creation.
# This wrapper sets PYTHONDONTWRITEBYTECODE=1 before invoking pytest.
#
# Usage:
#   ./run_tests.sh                          # run all tests
#   ./run_tests.sh tests/codebase/          # run codebase tests only
#   ./run_tests.sh -k "test_memory"         # filter by name
#   ./run_tests.sh -m fast                  # only fast-marked tests

set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=${PYTHON:-"$repo_root/.venv/bin/python"}
if [ ! -x "$python" ]; then
    python=python3
fi

exec "$python" "$repo_root/scripts/run_tests.py" "$@"
