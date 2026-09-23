#!/usr/bin/env bash
# Viam module entrypoint. Bootstraps a venv on first run, then execs the
# module server.
#
# The install guard checks for the imported package, not just the .venv
# directory, so a partial install (network flake, missing wheel) is
# retried on the next boot instead of skipped forever.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

if ! ./.venv/bin/python -c "import inventory_module" 2>/dev/null; then
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install .
fi

exec ./.venv/bin/python -m inventory_module.main "$@"
