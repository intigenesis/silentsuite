# Updating

How SilentSuite versions are selected on a self-hosted instance, and what is and is not supported today.

## How Versions Are Pinned

`docker-compose.yml` contains no image reference of its own. It requires `SILENTSUITE_SERVER_IMAGE` from `.env`, which the installer writes as the immutable OCI index digest of the release it verified:

```
SILENTSUITE_SERVER_IMAGE=ghcr.io/silent-suite/silentsuite-server@sha256:<index digest>
```

There is deliberately no default, so Compose refuses to start rather than run an unverified image. A mutable `:version` tag is only ever used to *find* a release; `latest` is never the authority for what runs, and moving a tag cannot change your running server.

## Restarting the Version You Have

```bash
./update.sh
```

This re-pulls the image already pinned in `.env`, recreates the containers, and waits for health checks to pass. It does **not** change SilentSuite versions — the digest in `.env` is immutable by design, so `docker compose pull` is a no-op across versions.

## Upgrading to a New Version

**There is no supported cross-version update procedure yet.** A version-aware updater that moves an existing installation from one release to the next is deliberately deferred to a later change.

Until it ships:

- **Re-running the installer is not the upgrade path.** It refuses to run against an existing target directory, because regenerating credentials and restarting a stack without migrating or backing up its data is not a safe upgrade.
- **Do not edit the digest in `.env` by hand.** A newer server image may expect a database schema your current volume does not have.
- **Do not switch the image to a mutable tag.** That gives up the verification the installer performed and can silently change what runs.

You can inspect a newer release without touching your installation. Staging only writes into a directory that does not exist yet, and never pulls an image or starts a container:

```bash
bash ./install.sh --version vX.Y.Z --stage-only ./silentsuite-vX.Y.Z
```

This verifies the release tag, the manifest, the checksum sidecar, and the archive contents.

## Verify

```bash
docker compose ps
./verify.sh
```

All services should report `healthy`.

## Data Safety

Restarting with `./update.sh` preserves your data: Docker named volumes (`pgdata`, `server_data`) are not affected by container recreation. It is still good practice to [back up](./backup-and-restore.md) before maintenance. Losing the server data volume means losing the server secret key, and encrypted data becomes unrecoverable without it.
