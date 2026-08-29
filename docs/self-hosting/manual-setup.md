# Manual Setup

There is no supported setup path that skips the release bundle.

`docker-compose.yml` deliberately has no server image of its own. It requires
`SILENTSUITE_SERVER_IMAGE`, and the only place that value comes from is
`server-image.json` inside a verified release bundle. Cloning the repository or
downloading files from `main` cannot supply it, so those instructions have been
removed rather than left as a path that stops at the first `docker compose up`.

## Use the installer

```bash
curl -fsSL https://raw.githubusercontent.com/silent-suite/silentsuite/main/self-host/install.sh | bash
```

The installer resolves a published release, verifies the bundle checksum, the
manifest, the archive contents, and the registry image identity, then writes the
verified digest into `.env`. See [Quick Start](./quick-start.md) for the full
walkthrough and the reverse-proxy step that follows it.

## Audit a release before installing it

If what you wanted from manual setup was to see exactly what will be installed,
stage it instead. This runs the release-metadata, tag-to-commit, checksum,
manifest and archive checks and writes the verified files out. It does not pull
an image or contact the registry — the live image-identity check happens only
during a real install:

```bash
bash install.sh --version vX.Y.Z --stage-only ./silentsuite-vX.Y.Z
```

The staged directory holds the archive, its checksum sidecar, the published
manifest, and the verified bundle contents. Read them, then run the installer
when you are satisfied.

## Configure it yourself afterwards

Every environment variable remains yours to set. Edit `.env` in the installed
directory and recreate the containers:

```bash
cd silentsuite-server
$EDITOR .env
docker compose up -d
```

See the [Configuration Reference](./configuration.md) for what each variable
does. Do not edit `SILENTSUITE_SERVER_IMAGE` by hand — it is the verified image
digest the installer wrote.

## A note on scope

This installs a fresh instance. There is no supported cross-version update
procedure yet; see [Updating](./updating.md) for what is and is not supported
today.

## Next Steps

- [Configuration](./configuration.md) -- full reference for all environment variables.
- [Admin Dashboard](./admin-dashboard.md) -- manage your instance via the Django admin panel.
