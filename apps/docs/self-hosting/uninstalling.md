# Uninstalling

How to remove SilentSuite from your server.

## Stop Without Deleting Data

If you only want to stop the services but keep your data for later:

```bash
docker compose down
```

The data volumes will persist. Run `docker compose up -d` to start again.

## Complete Removal

To completely remove SilentSuite and all its data:

```bash
# Stop and remove all containers and volumes
docker compose down -v

# Remove the Docker images. Both are pinned by digest, and `latest` is never
# published for the server image, so remove them by the references you actually
# pulled — list them with `docker image ls`.
docker image rm postgres@sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7
docker image ls --filter 'reference=ghcr.io/silent-suite/silentsuite-server' --format '{{.ID}}' | xargs -r docker image rm

# Remove the cloned repository
cd ..
rm -rf silentsuite
```

> **Warning:** `docker compose down -v` deletes all data volumes. This is irreversible. [Back up](./backup-and-restore.md) first if you want to keep your data.
