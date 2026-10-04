#!/bin/sh
set -e
# First start: build the synthetic backend (idempotent when the DB already exists)
if [ ! -f /srv/data/.seeded ]; then
  python -m app.db.seed && touch /srv/data/.seeded
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${WEB_CONCURRENCY:-1}"
