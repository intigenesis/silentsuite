#!/usr/bin/env bash
set -euo pipefail

# SilentSuite Self-Hosted Updater
# --------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if docker compose version &>/dev/null; then
  COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE="docker-compose"
else
  echo "ERROR: 'docker compose' is not available."
  exit 1
fi

# This script only restarts the stack against the image identities the release
# already fixed: SILENTSUITE_SERVER_IMAGE from .env, and the PostgreSQL index
# digest pinned in docker-compose.yml. Both are immutable OCI digests, so
# `docker compose pull` re-fetches exactly the same bytes and is a no-op across
# versions by design — including for the database, which is not selected by a
# mutable upstream tag.
#
# Re-running install.sh is NOT the upgrade path either — it refuses to touch an
# existing installation. A version-aware cross-version updater is not supplied
# yet; see SELF-HOSTING.md.
echo "Pulling pinned images..."
$COMPOSE pull

echo "Recreating containers..."
$COMPOSE up -d

echo ""
echo "Waiting for services to become healthy..."

MAX_WAIT=120
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
  HEALTHY=0

  for container in silentsuite-postgres silentsuite-server; do
    status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "unknown")
    if [ "$status" = "healthy" ]; then
      HEALTHY=$((HEALTHY + 1))
    fi
  done

  if [ "$HEALTHY" -ge 2 ]; then
    echo "All services healthy."
    break
  fi

  sleep 5
  ELAPSED=$((ELAPSED + 5))
  echo "  $HEALTHY/2 services healthy ($ELAPSED/${MAX_WAIT}s)..."
done

if [ "$HEALTHY" -lt 2 ]; then
  echo ""
  echo "WARNING: Not all services are healthy after ${MAX_WAIT}s."
  echo "Run '$COMPOSE logs' to troubleshoot."
  exit 1
fi

echo ""
echo "Restart complete — the stack is running the exact images this release pinned."
echo "This script does not change SilentSuite versions."
echo ""
echo "If your reverse proxy reaches the server over a Docker network,"
echo "make sure TRUSTED_PROXY_IPS in .env contains that proxy container's"
echo "exact IP, then run: $COMPOSE up -d --force-recreate server"
