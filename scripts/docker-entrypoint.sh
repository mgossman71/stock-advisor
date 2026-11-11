#!/usr/bin/env bash
# Container entrypoint: apply migrations, then exec whatever CMD was given.
#
# MIGRATE_ON_START=0 skips the migration (useful when a one-shot job should not
# race the long-lived app container for the same SQLite file).
set -euo pipefail

if [[ "${MIGRATE_ON_START:-1}" == "1" ]]; then
    echo "[entrypoint] alembic upgrade head"
    alembic upgrade head
fi

exec "$@"
