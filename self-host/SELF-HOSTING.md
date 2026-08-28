# SilentSuite Self-Hosting Guide

Run your own SilentSuite sync server. Your data stays on your hardware, fully end-to-end encrypted.

## How It Works

You run the SilentSuite sync server and a PostgreSQL database (2 containers). Your users connect via [app.silentsuite.io](https://app.silentsuite.io) or the SilentSuite mobile apps, pointing at your server URL in Advanced Settings.

You provide your own reverse proxy (Caddy, nginx, Traefik, Cloudflare Tunnel) to handle TLS and forward traffic to the SilentSuite server on port 3735.

```
    Your Server                         SilentSuite Apps
  ┌─────────────────┐
  │  Your Reverse   │◄──────────── app.silentsuite.io
  │  Proxy (HTTPS)  │              (or mobile apps)
  └────────┬────────┘              enter your server URL
           │                       in Advanced Settings
  ┌────────┴────────┐
  │   SilentSuite   │
  │     Server      │
  │      :3735      │
  └────────┬────────┘
           │
  ┌────────┴────────┐
  │  PostgreSQL 16  │
  │    (internal)   │
  └─────────────────┘
```

| Service | Image | Role |
|---------|-------|------|
| **SilentSuite Server** | `ghcr.io/silent-suite/silentsuite-server`, pinned to the immutable OCI index digest of the release you installed | Sync server (Etebase protocol). All data is E2E encrypted. |
| **PostgreSQL** | `postgres:16.9-alpine` | Database for encrypted sync data and user accounts. |

## Prerequisites

- A Linux server (Ubuntu 22.04+, Debian 12+, or similar)
- Docker Engine 24+ with Compose v2
- A reverse proxy for TLS termination
- A domain name (e.g., `sync.example.com`) with DNS pointing to your server
- `curl`, `tar`, and `sha256sum` (or `shasum`) for the installer's download verification

### Supported architectures

Server images are published for `linux/amd64` and `linux/arm64`. The installer
detects your architecture and refuses to continue on anything else rather than
installing an image that cannot run.

`linux/arm64` support is verified in the release pipeline on native ARM64
runners. Acceptance on specific ARM64 single-board hardware, such as a Raspberry
Pi, has not been completed yet — treat it as untested until that evidence is
published.

## How Releases Are Pinned

Every SilentSuite release publishes three self-host assets:

| Asset | Purpose |
|-------|---------|
| `silentsuite-self-host-<tag>.tar.gz` | the version-matched `docker-compose.yml`, helper scripts (including `backup-restore.sh`), and landing page; stage-only retains this archive for upgrade-time re-verification |
| `silentsuite-self-host-<tag>.tar.gz.sha256` | the bundle's checksum, as a single strict record |
| `server-image.json` | the immutable image identity: release tag, source commit, OCI index digest, per-architecture digests, supported platforms, and the expected image revision label |

`docker-compose.yml` contains no image digest. It reads
`SILENTSUITE_SERVER_IMAGE` from `.env`, which the installer writes as
`ghcr.io/silent-suite/silentsuite-server@sha256:<index digest>` only after every
check above has passed. A mutable `:version` tag is used to *find* a release,
never to decide what runs.

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/silent-suite/silentsuite/main/self-host/install.sh | bash
```

The installer will:
1. Check that Docker, Docker Compose, and the download-verification tools are installed
2. Refuse to continue if `silentsuite-server/` already contains an installation — re-running the installer is not the upgrade path
3. Detect your architecture and resolve the newest published release that ships verified self-host assets
4. Download the release bundle, its checksum, and `server-image.json` into a private temporary directory
5. Verify the checksum record's exact grammar and the bundle's exact bytes before anything is extracted
6. Verify the manifest: schema, release tag, source commit, canonical image repository, index and per-architecture digests, supported platforms, and expected image revision
7. Confirm with GitHub that the release tag really points at the commit the manifest names
8. Reject the archive unless every entry is a regular file or directory inside the bundle root and the file set is exactly the published inventory — nothing missing, nothing extra — then extract to a temporary staging directory
9. Confirm the manifest inside the bundle is byte-identical to the separately published one
10. Pull the image by digest and confirm the registry serves the promised digest, revision, and architecture
11. Create `silentsuite-server/`, install the verified files, ask for your domain, generate secure random passwords, and write `.env` including the verified image digest
12. Start the containers and wait for health checks to pass

If any check fails, the installer stops before creating or changing anything,
removes its temporary files, and leaves any existing installation untouched.

The first user to sign up in the SilentSuite app becomes the server admin.

Then set up your reverse proxy to forward HTTPS traffic to `localhost:3735`.

### Installing a specific version

To pin to a specific SilentSuite release rather than the latest:

```bash
# Curl-pipe style (env var):
curl -fsSL https://raw.githubusercontent.com/silent-suite/silentsuite/main/self-host/install.sh | SILENTSUITE_VERSION=v0.1.0-beta bash

# Locally cloned style (CLI flag):
bash install.sh --version v0.1.0-beta
```

The requested release must be published and must ship the self-host assets;
otherwise the installer stops. There is no branch fallback: a branch has no
verified server image, so it is not an installable source.

### Inspecting a release before installing it

```bash
bash install.sh --version v0.1.0-beta --stage-only ./silentsuite-release
```

This performs every download and verification step and writes the verified
bundle contents into `./silentsuite-release`, along with the original archive,
its strict checksum sidecar, and the published manifest. It does not create an
installation, pull an image, or start a container. The stage directory has a
closed top-level inventory: the archive, sidecar, manifest, and the archive's
verified managed files.

## Manual Setup

> `install.sh --version vX.Y.Z --stage-only ./staged` performs the release-tag,
> manifest, bundle, checksum, and archive checks, but not live registry image
> verification. It writes the verified files out without installing or starting
> anything.
> Prefer it unless you specifically want to do this by hand.

1. **Download and verify the release bundle** (replace `vX.Y.Z` with the release
   you want, from [the releases page](https://github.com/silent-suite/silentsuite/releases)):
   ```bash
   BASE=https://github.com/silent-suite/silentsuite/releases/download/vX.Y.Z
   curl -fLO "$BASE/silentsuite-self-host-vX.Y.Z.tar.gz"
   curl -fLO "$BASE/silentsuite-self-host-vX.Y.Z.tar.gz.sha256"
   curl -fLO "$BASE/server-image.json"

   # The sidecar must be exactly one record naming exactly this archive.
   cat silentsuite-self-host-vX.Y.Z.tar.gz.sha256
   sha256sum -c silentsuite-self-host-vX.Y.Z.tar.gz.sha256

   tar -tzf silentsuite-self-host-vX.Y.Z.tar.gz      # review before extracting
   tar -xzf silentsuite-self-host-vX.Y.Z.tar.gz
   ```

   Do not skip the checksum step: it is the only thing standing between you and
   an altered bundle.

2. **Bind the manifest to the bundle you verified.** The checksum covers the
   archive only, so the separately downloaded `server-image.json` proves nothing
   until you show it is the same file that is *inside* the verified archive:
   ```bash
   cmp server-image.json silentsuite-self-host-vX.Y.Z/server-image.json
   ```
   Any difference means the manifest is not the one this bundle was built with —
   stop there. From this point on, use the copy inside the extracted bundle.

3. **Install the verified files:**
   ```bash
   mkdir silentsuite-server && chmod 750 silentsuite-server
   cp -R silentsuite-self-host-vX.Y.Z/. silentsuite-server/
   cd silentsuite-server
   cp .env.example .env
   ```

4. **Pin the server image.** Read `indexDigest` out of the bundled
   `server-image.json` and put it in `.env`:
   ```bash
   SILENTSUITE_SERVER_IMAGE=ghcr.io/silent-suite/silentsuite-server@sha256:<indexDigest>
   ```
   Compose has no default for this value and will refuse to start without it.
   Then confirm the registry really serves that image, for your architecture,
   built from the commit the manifest claims:
   ```bash
   docker pull "$SILENTSUITE_SERVER_IMAGE"
   docker image inspect "$SILENTSUITE_SERVER_IMAGE" \
     --format '{{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
   ```
   The architecture must match your host and the revision must equal the
   manifest's `expectedRevision`. The installer additionally asks GitHub to
   confirm the release tag points at `sourceCommit`; by hand, check the tag on
   the releases page.

5. **Generate passwords:**
   ```bash
   openssl rand -base64 32 | tr -d '/+='   # use for DATABASE_PASSWORD
   openssl rand -base64 16 | tr -d '/+='   # use for SUPER_PASS
   ```

6. **Edit `.env`:**
   - `DATABASE_PASSWORD` -- the generated database password
   - `SUPER_PASS` -- the generated admin password
   - Save with `chmod 600 .env` so only the host operator can read it.

7. **Create `etebase-server.ini`** (server-side configuration; mounted into the container). Replace `YOUR_DATABASE_PASSWORD` with the value you set in `.env`, and `sync.example.com` with your domain:
   ```ini
   [global]
   secret_file = /data/secret.txt
   debug = false
   media_root = /data/media
   static_root = /data/static

   [allowed_hosts]
   allowed_host1 = sync.example.com
   allowed_host2 = localhost

   [database]
   engine = django.db.backends.postgresql
   name = silentsuite
   user = silentsuite
   password = YOUR_DATABASE_PASSWORD
   host = postgres
   port = 5432
   ```
   Save with `chmod 644` so the container's `etebase` user can read it via the bind mount. Keep the install directory itself at `0750`; `etebase-server.ini` contains the database password and should not live in a shared directory. Users outside the directory owner/group cannot traverse a `0750` directory, but members of that group can still read the file.

8. **Start the stack:**
   ```bash
   docker compose up -d
   ```

9. **Set up your reverse proxy** (see examples below).

## Reverse Proxy Examples

Docker publishes the SilentSuite server on host loopback at `127.0.0.1:3735` by default. Configure your reverse proxy to forward HTTPS traffic to it.

### Caddy (recommended -- automatic HTTPS)

```
sync.example.com {
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "strict-origin-when-cross-origin"
        Permissions-Policy "camera=(), microphone=(), geolocation=()"
    }
    reverse_proxy localhost:3735
}
```

### nginx

```nginx
server {
    listen 443 ssl;
    server_name sync.example.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

    location / {
        proxy_pass http://127.0.0.1:3735;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 50m;
    }
}
```

### Trusted Proxy Headers

The server only accepts `X-Forwarded-*` headers from `TRUSTED_PROXY_IPS`.
Keep the default `127.0.0.1` when Caddy/nginx/cloudflared connects through the
host loopback port. If a Docker-network proxy connects directly to the server
container, set `TRUSTED_PROXY_IPS` in `.env` to that proxy's exact container IP
before recreating the server. Multiple values are comma-separated, for example
`TRUSTED_PROXY_IPS=127.0.0.1,172.18.0.5`. Uvicorn matches exact IPs here; CIDR
ranges are not supported.

### Traefik (Docker labels)

```yaml
# Add these labels to the server service in docker-compose.yml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.silentsuite.rule=Host(`sync.example.com`)"
  - "traefik.http.routers.silentsuite.tls.certresolver=letsencrypt"
  - "traefik.http.services.silentsuite.loadbalancer.server.port=3735"
```

> If Traefik runs in Docker, replace the `ports:` mapping with `expose: ["3735"]` and ensure Traefik shares the `silentsuite` Docker network. Also set `TRUSTED_PROXY_IPS` in `.env` to the Traefik container's exact IP so only that proxy can set `X-Forwarded-*` headers.

### Cloudflare Tunnel

```bash
cloudflared tunnel --url http://localhost:3735
```

### Recommended Security Headers

Add these in your reverse proxy for defense in depth if your proxy example does
not already include them:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`

## Connecting Your Apps

Once your server is running and your reverse proxy is configured:

1. Open [app.silentsuite.io](https://app.silentsuite.io) in a browser for the first signup
2. On the signup page, expand **Advanced Settings**
3. Enter the one-time first-admin URL printed by the installer as the server URL:
   `https://sync.example.com/?bootstrap_token=YOUR_TOKEN`
4. Create your account and start syncing
5. **Run `./close-signups.sh`** from your install directory — see below.

## Closing Open Signups

The server ships with `ETEBASE_DISABLE_SIGNUP=false` so your first account can be created from the SilentSuite app. The installer also generates `ETEBASE_BOOTSTRAP_ADMIN_TOKEN` in `.env`; while the server has zero users, the first signup must include that token in the server URL query string:

```text
https://sync.example.com/?bootstrap_token=YOUR_TOKEN
```

This keeps a random visitor from racing you for the first account if they discover the server URL during setup. Once any user exists, the token is no longer required; you should still close signups immediately after creating your admin account. After that first browser signup, you can connect the mobile app with the normal server URL (`https://sync.example.com`).

If you configure self-hosting manually instead of using `install.sh`, generate and set a strong `ETEBASE_BOOTSTRAP_ADMIN_TOKEN` yourself before first boot. Leaving it empty preserves the old open-first-signup behavior for compatibility and does **not** protect the first account from a public first-signup race.

Because the bootstrap token is passed in the server URL, it may appear in local browser history, terminal scrollback, or reverse-proxy access logs. Complete the first signup promptly; after the first app account exists, the token is ignored. If you suspect it was exposed before signup, rotate `ETEBASE_BOOTSTRAP_ADMIN_TOKEN` in `.env`, clear any leaked URL from logs/history where practical, and recreate the server container before trying again.

Once your admin account is registered, close signups:

```bash
cd silentsuite-server
./close-signups.sh
```

The script flips `ETEBASE_DISABLE_SIGNUP=true` in `.env` and recreates the server container. New registrations are blocked at the API layer thereafter. To re-open (e.g. to add another user), edit `.env`, set `ETEBASE_DISABLE_SIGNUP=false`, and run `docker compose up -d --force-recreate server`.

## Restarting the version you have

`./update.sh` re-pulls the images already pinned in `.env` and recreates the
containers. It is useful after host-level changes. It does **not** move between
SilentSuite versions — the digest in `.env` is immutable by design.

```bash
./update.sh
```

## Upgrading to a new version

Re-running `install.sh` is **not** the upgrade path. It refuses to touch an
existing installation, because regenerating credentials and restarting a stack
without backing up your database is not a safe upgrade.

There is no unattended scheduler for this work. The checked helper below is
still deliberately operator-invoked, but it creates the durable cohort backup
and performs bounded failure recovery around the migration.

Until it ships, upgrade deliberately:

1. **Back up first** (see [Backup and Restore](#backup-and-restore)). The bundled
   helper records the actual live Docker volume identities before writing the
   dump or archive; copy `.env` somewhere safe as part of that backup.
2. **Get the new release's verified bundle** without installing it. Run this
   from the existing installation directory; the stage directory must be
   nonexistent or empty:
   ```bash
   cd silentsuite-server
   bash ./install.sh --version vX.Y.Z --stage-only "./silentsuite-vX.Y.Z"
   ```
   Stage-only verifies the release tag, manifest, checksum, and archive, but it
   does not verify the live registry image. `upgrade.sh` performs the live pull,
   platform, revision-label, and `RepoDigest` checks against the staged image
   identity before mutating the installation. It retains the checksummed archive
   beside its extracted files so the upgrade helper can repeat the archive checks
   after staging.
3. **Run the checked manual upgrade helper**:
   ```bash
   bash "./silentsuite-vX.Y.Z/upgrade.sh" \
     --staged "$PWD/silentsuite-vX.Y.Z" \
     --install-dir "$PWD"
   ```

   The helper derives the canonical `imageRepository@indexDigest` from the
   verified manifest, then re-verifies the staged archive's strict checksum, closed
   tar inventory, safe paths and file types, and embedded manifest. It compares
   every retained extracted managed file with that freshly verified archive and
   stops if staging changed any bytes. The archive is then the only source for
   release-managed files: `.env.example`, `SELF-HOSTING.md`, `backup-restore.sh`,
   `close-signups.sh`, `docker-compose.yml`, `install.sh`, `success.html`, `upgrade.sh`,
   `update.sh`, `verify.sh`, and `server-image.json`. The existing `.env`
   values other than the image entry, `etebase-server.ini`,
   `docker-compose.override.yml`, named volumes, and operator data are not
   copied or recreated. The installed manifest and checksum must match the
   verified release identity or the helper stops.

   Before replacing any managed file or `.env`, the helper creates a durable,
   collision-safe snapshot under `.silentsuite-upgrade-backups/`. The snapshot
   contains the previous `.env`, every existing managed file, the previous
   manifest, every existing regular release checksum sidecar, non-secret
   identity metadata naming the target, prior sidecars, and the validated live
   Docker volume names and the immutable backup utility image, plus an exact
   restore helper. Matching symlink or
   non-regular sidecars, like other managed destinations, are rejected before
   backup or mutation. A successful upgrade may retain both the prior and
   target sidecars. Do not delete this snapshot until the upgrade is known to
   be healthy and the normal data backup is safely retained.

   The upgrade snapshot is a release-cohort snapshot, not a database or
   server-data archive. Its metadata records the validated volume names and
   immutable utility image; cohort restore only restores release files and does
   not run the tar utility. A separately retained `backup-restore.sh` backup
   can be restored against the same identities.

   After admission and the snapshot, the helper replaces the new cohort, stops
   only the `server` service (PostgreSQL remains running), applies migrations
   with the admitted image, recreates only `server`, and runs `verify.sh`.
   It is a deliberate, operator-invoked upgrade; there is no unattended upgrade
   path.

If anything fails before migrations begin, the helper restores the previous
cohort automatically—including all backed-up prior sidecars—and leaves the
previously running server untouched (or restarts and verifies it if stopping had
already been requested). If the target sidecar was absent from the prior cohort,
rollback removes only that newly installed target sidecar. If migration
has begun, the helper restores the previous image and managed-file cohort where
safe and attempts to restart and verify it, but migrations remain forward-applied
and may require restoring the database backup. The helper reports whether that
restart was actually verified; it never claims a full rollback. If automatic
recovery cannot be confirmed, use the exact `restore-previous-cohort.sh` command
printed by the helper from the durable backup directory, then recreate `server`
and run `./verify.sh`. Restore the cohort and image together—do not perform a
digest-only rollback against a newer managed-file cohort.

If the forward migration requires data recovery, run the bundled data restore
against the backup directory whose mode-600 metadata was created before the
upgrade:

```bash
bash ./backup-restore.sh restore --backup-dir "$BACKUP_DIR" --install-dir "$PWD"
```

## Health Checks

```bash
./verify.sh
```

## Admin Panel

The advanced Django admin panel is disabled by default in self-host installs (`ETEBASE_DISABLE_DJANGO_ADMIN=true`) because normal operators do not need it exposed on the public sync domain. If you need it for recovery or advanced maintenance, set `ETEBASE_DISABLE_DJANGO_ADMIN=false` in `.env`, recreate the server container, and protect `/admin/` with your reverse proxy or Cloudflare Access before exposing it.

## Backup and Restore

### Backup

Run this from the installation directory. Use a new, private directory for
each backup:

```bash
BACKUP_DIR="$PWD/backups/$(date -u +%Y%m%dT%H%M%SZ)"
./backup-restore.sh backup --backup-dir "$BACKUP_DIR" --install-dir "$PWD"
```

The helper inspects the running `silentsuite-postgres` mount at
`/var/lib/postgresql/data` and the running `silentsuite-server` mount at `/data`.
Each must have exactly one `volume` mount with a nonempty strict Docker volume
name. The underlying volume object must also use Docker's ordinary `local`
driver with null or empty options, because restore recreates it with plain
`docker volume create`. The validated names and recreation contract are written
to mode-600 `metadata` before the dump or archive is started. A bind, other
mount type, missing mount, ambiguous match, custom driver, or local-driver
option fails with custom-backup guidance. The helper supports ordinary local
named volumes only; use operator-specific backup/restore tooling for custom
drivers/options or host binds. The database dump remains container-based; the
server-data archive uses the recorded named volume. Its tar utility is the
exact immutable digest reported by the running `silentsuite-server` container,
and networking is disabled for the utility container. The backup also includes
`.env.backup` when `.env` is a regular file and never prints its contents. After
all artifacts are written, it creates a mode-600 `checksums` manifest containing
one strict SHA-256 record per artifact in fixed sorted order.

### Restore

Restore from the same backup directory, from the installation directory:

```bash
./backup-restore.sh restore --backup-dir "$BACKUP_DIR" --install-dir "$PWD"
```

Before any Compose down or volume removal/creation, restore validates the
mode-600 metadata, the strict `checksums` manifest, and every artifact's
SHA-256 digest. Missing, extra, malformed, reordered, or mismatched records,
artifact symlinks, and nonregular artifacts fail closed. It then uses those
exact recorded names for Compose's external-volume mapping, volume
removal/creation, and server-data extraction. It pulls the recorded immutable
utility digest before Compose down and uses that image with networking disabled.
It does not inspect a changed stack to choose names, so a different Compose
project name cannot redirect the restore to a new empty volume. An existing regular
`docker-compose.override.yml` is included in every restore Compose command;
symlinked or nonregular overrides are rejected before down. Keep the stack
stopped until the command reports success; inspect the health checks afterward
with `./verify.sh`. The recorded local-empty-options proof is mandatory, so old
or tampered metadata without it is rejected before pull, down, removal, or
creation. Custom drivers/options and binds require operator-specific tooling.

If either installation uses a bind or other custom mount, this helper refuses
to proceed. Use custom tooling that snapshots that host path and restore it
manually before starting SilentSuite; never replace it with a newly created
named volume.

## Troubleshooting

### Containers won't start
```bash
docker compose logs server
docker compose logs postgres
```

### Server returns 400 Bad Request
Your domain is not in `etebase-server.ini`'s `[allowed_hosts]` section. Edit the file (under `[allowed_hosts]`, add `allowed_hostN = your.domain`) and recreate:
```bash
docker compose up -d --force-recreate server
```

### Database connection errors
- Verify PostgreSQL is healthy: `docker compose ps`
- Check that `DATABASE_PASSWORD` in `.env` matches the original value (changing it after first run requires a volume reset or manual password change in PostgreSQL)

### Server won't start: SILENTSUITE_SERVER_IMAGE is not set
Compose refuses to start without a verified image digest. Copy `indexDigest`
from `server-image.json` in your install directory into `.env` as
`SILENTSUITE_SERVER_IMAGE=ghcr.io/silent-suite/silentsuite-server@sha256:...`,
or re-install into a fresh directory from a published release.

### Reset everything
```bash
docker compose down -v   # WARNING: Deletes all data!
rm -rf silentsuite-server
curl -fsSL https://raw.githubusercontent.com/silent-suite/silentsuite/main/self-host/install.sh | bash
```
The installer will not overwrite an existing installation, so the old directory
must be removed first. Everything in it, including your credentials, is gone at
that point.

## Security Notes

- PostgreSQL is only accessible within the Docker network (not exposed to the host)
- Docker publishes the server port on host loopback only: `127.0.0.1:${SERVER_PORT:-3735}:3735`. Do not change this to `0.0.0.0` unless you put the server behind your own network firewall or proxy controls.
- All sync traffic is end-to-end encrypted. The server never sees your plaintext data.
- The server image is selected by an immutable digest, never by a mutable tag, so the bytes you verified at install time are the bytes that keep running.
- Built on the [Etebase protocol](https://docs.etebase.com), an open standard for E2E encrypted data sync.

## Full Documentation

For more details, see [docs.silentsuite.io/self-hosting](https://docs.silentsuite.io/self-hosting/).
