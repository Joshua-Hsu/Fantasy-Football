#!/bin/bash
# SessionStart hook: prepare the Python environment so tests and the ingestion
# CLI work immediately in a Claude Code on the web session.
#
# Idempotent and non-interactive: reuses an existing .venv, and pip's editable
# install is a cheap no-op once dependencies are present. Runs synchronously so
# the session only starts once the environment is ready.
set -euo pipefail

# Only do this in the remote (web) environment; local dev manages its own venv.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# Diagnostic output goes to stderr so it doesn't pollute the hook's stdout.
log() { echo "[session-start] $*" >&2; }

# 1. Virtual environment (container state is cached, so this persists between
#    sessions and is skipped on warm starts).
if [ ! -d .venv ]; then
  log "creating virtualenv"
  python3 -m venv .venv
fi

# 2. Dependencies: project + dev (pytest) + ingest (pandas, pyarrow). Editable
#    install so the package and CLI are importable.
log "installing dependencies"
./.venv/bin/pip install --quiet --upgrade pip >&2
./.venv/bin/pip install --quiet -e ".[dev,ingest]" >&2

# 3. Create the database schema (create_all is a no-op if tables already exist).
log "initializing database schema"
./.venv/bin/python -m fantasy_football.cli init-db >&2

# 4. Activate the venv for every subsequent command in this session.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"${CLAUDE_PROJECT_DIR:-$PWD}/.venv\""
    echo "export PATH=\"${CLAUDE_PROJECT_DIR:-$PWD}/.venv/bin:\$PATH\""
  } >> "$CLAUDE_ENV_FILE"
fi

log "ready"
