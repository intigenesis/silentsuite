# Backup & Restore

Protect your self-hosted SilentSuite data with regular backups.

## What to Back Up

| Item | Why |
|---|---|
| **PostgreSQL database** | All encrypted sync data and user accounts |
| **Server data volume** | Server secret key and media files. **If you lose the secret key, existing encrypted data becomes unrecoverable.** |
| **`.env` file** | Contains all passwords and configuration |

---

## Create a Backup

Run the bundled helper from the installation directory, using a new private
directory for each backup:

```bash
BACKUP_DIR="$PWD/backups/$(date -u +%Y%m%dT%H%M%SZ)"
./backup-restore.sh backup --backup-dir "$BACKUP_DIR" --install-dir "$PWD"
```

The helper derives the actual named volumes from the running containers: the
PostgreSQL mount at `/var/lib/postgresql/data` and the server mount at `/data`.
Each must have exactly one `volume` mount and a strict nonempty Docker volume
name. The underlying volume object must also use Docker's ordinary `local`
driver with null or empty options, because restore recreates it with plain
`docker volume create`. It records those names and this recreation contract in
mode-600 `metadata` before creating the container-based database dump or
server-data archive. Bind mounts, missing or ambiguous mounts, custom drivers,
and local-driver options fail with custom-backup guidance; use operator-specific
backup/restore tooling for those volumes. The helper only supports ordinary
local named volumes. `.env.backup` is included when `.env` is a regular file;
its contents are never printed. The archive utility is the exact immutable
digest recorded by the running `silentsuite-server` container, and its
networking is disabled. After all artifacts are written, the helper creates a
mode-600 `checksums` manifest with one SHA-256 record per artifact in fixed
sorted order.

## Automated Backups

Set up a daily cron job:

```bash
crontab -e
```

Add:

```
# SilentSuite daily backup at 2:00 AM; use a unique directory per run
0 2 * * * cd /path/to/silentsuite/self-host && d="$PWD/backups/$(date -u +\%Y\%m\%dT\%H\%M\%SZ)" && ./backup-restore.sh backup --backup-dir "$d" --install-dir "$PWD"
```

For off-site backups, use `rsync`, `rclone`, or your preferred tool to copy the backup directory to another server.

---

## Restore

From the installation directory, restore from the same backup directory:

```bash
./backup-restore.sh restore --backup-dir "$BACKUP_DIR" --install-dir "$PWD"
```

Before any Compose down or volume removal/creation, restore validates the
mode-600 metadata, the strict `checksums` manifest, and every artifact's
SHA-256 digest. Missing, extra, malformed, reordered, or mismatched records,
artifact symlinks, and nonregular artifacts fail closed. It then uses the exact
recorded names for external-volume mapping, removal/creation, and server-data
extraction. It pulls the recorded immutable archive utility digest before
Compose down and uses that image with networking disabled. It does not derive
names from the current Compose project, so a
changed project name cannot redirect the restore to an empty volume. An
existing regular `docker-compose.override.yml` is included in every restore
Compose command; symlinked or nonregular overrides are rejected before down.
The recorded local-empty-options proof is mandatory, so old or tampered
metadata without it is rejected before pull, down, removal, or creation. For
binds or other custom drivers/options, use operator-specific tooling to restore
the host paths; this helper refuses them.

## Test Your Backups

Backups you've never restored from are not backups. Periodically verify by restoring to a test instance.
