#!/bin/sh
set -e

case "$1" in
  api)
    alembic upgrade head
    # The container is only reachable through the reverse proxy / tunnel on a private
    # Docker network, so trusting forwarded headers from any peer is fine here.
    exec uvicorn app.main:app \
      --host 0.0.0.0 --port 8000 \
      --proxy-headers --forwarded-allow-ips='*' \
      --no-server-header
    ;;
  worker)
    exec python -m app.worker
    ;;
  backup)
    exec python -m app.backup loop
    ;;
  *)
    exec "$@"
    ;;
esac
