#!/usr/bin/env bash
set -euo pipefail

# Safe self-host data backup and restore. Volume names and the immutable tar
# utility image are discovered from the running containers for backup and are
# never inferred from the Compose directory or project name.

POSTGRES_CONTAINER="silentsuite-postgres"
SERVER_CONTAINER="silentsuite-server"
POSTGRES_TARGET="/var/lib/postgresql/data"
SERVER_TARGET="/data"
UTILITY_IMAGE_RE='^ghcr\.io/silent-suite/silentsuite-server@sha256:[0-9a-f]{64}$'
METADATA_NAME="metadata"
CHECKSUMS_NAME="checksums"
DATABASE_DUMP_NAME="database.sql"
SERVER_ARCHIVE_NAME="server-data.tar.gz"

usage() {
  cat <<'EOF'
Usage:
  backup-restore.sh backup  --backup-dir DIRECTORY [--install-dir DIRECTORY]
  backup-restore.sh record  --metadata FILE [--install-dir DIRECTORY]
  backup-restore.sh restore --backup-dir DIRECTORY [--install-dir DIRECTORY]

backup  records the live named volumes, then writes database.sql,
        server-data.tar.gz, an optional .env.backup, and strict checksums.
record  records only the validated live volume identities. It is used by the
        upgrade helper before its durable cohort snapshot is mutated.
restore reads the recorded identities and restores without inspecting the
        current stack to choose replacement volume names.
EOF
}

COMMAND="${1:-}"
[ -n "$COMMAND" ] || { usage >&2; exit 2; }
shift

BACKUP_DIR=""
METADATA_PATH=""
INSTALL_DIR="$(pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --backup-dir)
      [ $# -ge 2 ] && [ -n "$2" ] || { echo "ERROR: --backup-dir requires a directory" >&2; exit 2; }
      BACKUP_DIR="$2"
      shift 2
      ;;
    --metadata)
      [ $# -ge 2 ] && [ -n "$2" ] || { echo "ERROR: --metadata requires a file" >&2; exit 2; }
      METADATA_PATH="$2"
      shift 2
      ;;
    --install-dir)
      [ $# -ge 2 ] && [ -n "$2" ] || { echo "ERROR: --install-dir requires a directory" >&2; exit 2; }
      INSTALL_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument '$1'" >&2
      exit 2
      ;;
  esac
done

case "$COMMAND" in
  backup|restore) [ -n "$BACKUP_DIR" ] || { echo "ERROR: --backup-dir is required" >&2; exit 2; } ;;
  record) [ -n "$METADATA_PATH" ] || { echo "ERROR: --metadata is required" >&2; exit 2; } ;;
  *) echo "ERROR: unknown command '$COMMAND'" >&2; usage >&2; exit 2 ;;
esac

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: Docker Compose is required" >&2
  exit 1
fi

valid_volume_name() {
  local name="$1"
  [ "${#name}" -ge 1 ] && [ "${#name}" -le 255 ] || return 1
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
}

custom_mount_guidance() {
  local label="$1" container="$2" target="$3" details="$4"
  echo "ERROR: $label container $container does not have exactly one named Docker volume at $target ($details)." >&2
  echo "       The operator is using a bind or other custom mount; no empty replacement volume will be created." >&2
  echo "       Use custom backup tooling for that mount and restore it manually before starting SilentSuite." >&2
  exit 1
}

custom_volume_guidance() {
  local label="$1" container="$2" target="$3"
  echo "ERROR: $label volume at $target has custom driver/options unsupported by the bundled backup/restore helper." >&2
  echo "       The helper only recreates ordinary named volumes using Docker's local driver with empty options." >&2
  echo "       Use custom backup/restore tooling for that volume and restore it manually before starting SilentSuite." >&2
  exit 1
}

inspect_volume_field() {
  local format="$1" name="$2" output last_byte
  local -a output_lines
  output="$(mktemp "${TMPDIR:-/tmp}/silentsuite-volume-inspect.XXXXXXXX")" || return 1
  chmod 600 "$output"
  if ! docker volume inspect --format "$format" "$name" >"$output" 2>/dev/null; then
    rm -f -- "$output"
    return 1
  fi
  last_byte="$(tail -c 1 "$output" | od -An -tx1 | tr -d ' \n')"
  mapfile -t output_lines < "$output"
  rm -f -- "$output"
  [ "$last_byte" = "0a" ] || return 1
  [ "${#output_lines[@]}" = "1" ] || return 1
  VOLUME_INSPECT_VALUE="${output_lines[0]}"
}

validate_volume_recreation_contract() {
  local label="$1" container="$2" target="$3" name="$4" driver options
  inspect_volume_field '{{.Driver}}' "$name" || custom_volume_guidance "$label" "$container" "$target"
  driver="$VOLUME_INSPECT_VALUE"
  inspect_volume_field '{{json .Options}}' "$name" || custom_volume_guidance "$label" "$container" "$target"
  options="$VOLUME_INSPECT_VALUE"
  [ "$driver" = "local" ] || custom_volume_guidance "$label" "$container" "$target"
  case "$options" in
    null|'{}') ;;
    *) custom_volume_guidance "$label" "$container" "$target" ;;
  esac
}

discover_volume() {
  local label="$1" container="$2" target="$3" running mounts count type name source
  running="$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null)" \
    || { echo "ERROR: could not inspect $container; start the stack before backup" >&2; exit 1; }
  [ "$running" = "true" ] || { echo "ERROR: $container is not running; backup requires running container mount metadata" >&2; exit 1; }
  mounts="$(docker inspect --format "{{range .Mounts}}{{if eq .Destination \"$target\"}}{{.Type}}|{{.Name}}|{{.Source}}{{\"\\n\"}}{{end}}{{end}}" "$container" 2>/dev/null)" \
    || { echo "ERROR: could not inspect mounts for $container" >&2; exit 1; }
  mapfile -t matches <<< "$mounts"
  count=0
  for mount in "${matches[@]}"; do
    [ -n "$mount" ] || continue
    count=$((count + 1))
    IFS='|' read -r type name source <<< "$mount"
  done
  [ "$count" = "1" ] || custom_mount_guidance "$label" "$container" "$target" "found $count matching mounts"
  [ "$type" = "volume" ] || custom_mount_guidance "$label" "$container" "$target" "type=${type:-<empty>}"
  valid_volume_name "$name" || custom_mount_guidance "$label" "$container" "$target" "volume name is empty or not a strict Docker name"
  validate_volume_recreation_contract "$label" "$container" "$target" "$name"
  printf '%s\n' "$name"
}

discover_utility_image() {
  local running image
  running="$(docker inspect --format '{{.State.Running}}' "$SERVER_CONTAINER" 2>/dev/null)" \
    || { echo "ERROR: could not inspect $SERVER_CONTAINER; start the stack before backup" >&2; exit 1; }
  [ "$running" = "true" ] || { echo "ERROR: $SERVER_CONTAINER is not running; backup requires running container image metadata" >&2; exit 1; }
  image="$(docker inspect --format '{{.Config.Image}}' "$SERVER_CONTAINER" 2>/dev/null)" \
    || { echo "ERROR: could not inspect the running server image" >&2; exit 1; }
  [[ "$image" =~ $UTILITY_IMAGE_RE ]] \
    || { echo "ERROR: running server image is not the required canonical immutable digest" >&2; exit 1; }
  printf '%s\n' "$image"
}

write_metadata() {
  local path="$1" postgres_volume="$2" server_volume="$3" utility_image="$4" temporary
  temporary="${path}.tmp.$$"
  umask 077
  printf 'schemaVersion=1\npostgresVolume=%s\npostgresVolumeRecreationContract=local-empty-options\nserverDataVolume=%s\nserverDataVolumeRecreationContract=local-empty-options\nutilityImage=%s\n' \
    "$postgres_volume" "$server_volume" "$utility_image" > "$temporary"
  chmod 600 "$temporary"
  mv -f -- "$temporary" "$path"
  chmod 600 "$path"
}

record_live_volumes() {
  local destination="$1" postgres_volume server_volume utility_image
  postgres_volume="$(discover_volume "PostgreSQL" "$POSTGRES_CONTAINER" "$POSTGRES_TARGET")"
  server_volume="$(discover_volume "server-data" "$SERVER_CONTAINER" "$SERVER_TARGET")"
  utility_image="$(discover_utility_image)"
  [ -e "$destination" ] && [ ! -L "$destination" ] && {
    echo "ERROR: refusing to overwrite existing metadata '$destination'" >&2
    exit 1
  }
  mkdir -p -- "$(dirname -- "$destination")"
  write_metadata "$destination" "$postgres_volume" "$server_volume" "$utility_image"
}

prepare_backup_dir() {
  [ ! -L "$BACKUP_DIR" ] || { echo "ERROR: backup directory is a symlink" >&2; exit 1; }
  mkdir -p -- "$BACKUP_DIR"
  [ -d "$BACKUP_DIR" ] || { echo "ERROR: backup path is not a directory" >&2; exit 1; }
  chmod 700 "$BACKUP_DIR"
  BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
  [ ! -e "$BACKUP_DIR/$METADATA_NAME" ] && [ ! -L "$BACKUP_DIR/$METADATA_NAME" ] \
    || { echo "ERROR: backup metadata already exists; choose a new backup directory" >&2; exit 1; }
  [ ! -e "$BACKUP_DIR/$CHECKSUMS_NAME" ] && [ ! -L "$BACKUP_DIR/$CHECKSUMS_NAME" ] \
    || { echo "ERROR: backup checksums already exist; choose a new backup directory" >&2; exit 1; }
  [ ! -e "$BACKUP_DIR/$DATABASE_DUMP_NAME" ] && [ ! -L "$BACKUP_DIR/$DATABASE_DUMP_NAME" ] \
    || { echo "ERROR: database backup already exists; choose a new backup directory" >&2; exit 1; }
  [ ! -e "$BACKUP_DIR/$SERVER_ARCHIVE_NAME" ] && [ ! -L "$BACKUP_DIR/$SERVER_ARCHIVE_NAME" ] \
    || { echo "ERROR: server-data backup already exists; choose a new backup directory" >&2; exit 1; }
  [ ! -e "$BACKUP_DIR/.env.backup" ] && [ ! -L "$BACKUP_DIR/.env.backup" ] \
    || { echo "ERROR: environment backup already exists; choose a new backup directory" >&2; exit 1; }
}

read_metadata() {
  local path="$1" line schema_entries=0 postgres_entries=0 postgres_contract_entries=0 server_entries=0 server_contract_entries=0 utility_entries=0
  [ -f "$path" ] && [ ! -L "$path" ] || { echo "ERROR: backup metadata is missing or not a regular file" >&2; exit 1; }
  [ "$(stat -c '%a' "$path" 2>/dev/null || stat -f '%Lp' "$path")" = "600" ] \
    || { echo "ERROR: backup metadata must have mode 600" >&2; exit 1; }
  POSTGRES_VOLUME=""
  POSTGRES_VOLUME_RECREATION_CONTRACT=""
  SERVER_VOLUME=""
  SERVER_VOLUME_RECREATION_CONTRACT=""
  UTILITY_IMAGE=""
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      schemaVersion=1) schema_entries=$((schema_entries + 1)) ;;
      postgresVolume=*) postgres_entries=$((postgres_entries + 1)); POSTGRES_VOLUME="${line#postgresVolume=}" ;;
      postgresVolumeRecreationContract=*) postgres_contract_entries=$((postgres_contract_entries + 1)); POSTGRES_VOLUME_RECREATION_CONTRACT="${line#postgresVolumeRecreationContract=}" ;;
      serverDataVolume=*) server_entries=$((server_entries + 1)); SERVER_VOLUME="${line#serverDataVolume=}" ;;
      serverDataVolumeRecreationContract=*) server_contract_entries=$((server_contract_entries + 1)); SERVER_VOLUME_RECREATION_CONTRACT="${line#serverDataVolumeRecreationContract=}" ;;
      utilityImage=*) utility_entries=$((utility_entries + 1)); UTILITY_IMAGE="${line#utilityImage=}" ;;
      '') ;;
      *) echo "ERROR: backup metadata contains an unknown field" >&2; exit 1 ;;
    esac
  done < "$path"
  [ "$schema_entries" = "1" ] || { echo "ERROR: backup metadata must contain exactly one schema version" >&2; exit 1; }
  [ "$postgres_entries" = "1" ] || { echo "ERROR: backup metadata must contain exactly one postgres volume identity" >&2; exit 1; }
  [ "$postgres_contract_entries" = "1" ] && [ "$POSTGRES_VOLUME_RECREATION_CONTRACT" = "local-empty-options" ] \
    || { echo "ERROR: backup metadata must prove the PostgreSQL volume uses the local driver with empty options" >&2; exit 1; }
  [ "$server_entries" = "1" ] || { echo "ERROR: backup metadata must contain exactly one server-data volume identity" >&2; exit 1; }
  [ "$server_contract_entries" = "1" ] && [ "$SERVER_VOLUME_RECREATION_CONTRACT" = "local-empty-options" ] \
    || { echo "ERROR: backup metadata must prove the server-data volume uses the local driver with empty options" >&2; exit 1; }
  [ "$utility_entries" = "1" ] || { echo "ERROR: backup metadata must contain exactly one utility image identity" >&2; exit 1; }
  valid_volume_name "$POSTGRES_VOLUME" || { echo "ERROR: recorded PostgreSQL volume name is invalid" >&2; exit 1; }
  valid_volume_name "$SERVER_VOLUME" || { echo "ERROR: recorded server-data volume name is invalid" >&2; exit 1; }
  [[ "$UTILITY_IMAGE" =~ $UTILITY_IMAGE_RE ]] || { echo "ERROR: recorded utility image is not the required canonical immutable digest" >&2; exit 1; }
}

write_checksum_manifest() {
  local path="$BACKUP_DIR/$CHECKSUMS_NAME" temporary name digest
  local -a artifacts=("$DATABASE_DUMP_NAME" "$SERVER_ARCHIVE_NAME")
  if [ -f "$BACKUP_DIR/.env.backup" ] && [ ! -L "$BACKUP_DIR/.env.backup" ]; then
    artifacts=(".env.backup" "${artifacts[@]}")
  fi
  temporary="${path}.tmp.$$"
  umask 077
  : > "$temporary"
  chmod 600 "$temporary"
  for name in "${artifacts[@]}"; do
    digest="$(sha256sum -- "$BACKUP_DIR/$name" | awk '{print $1}')"
    printf '%s  %s\n' "$digest" "$name" >> "$temporary"
  done
  mv -f -- "$temporary" "$path"
  chmod 600 "$path"
}

verify_backup_integrity() {
  local manifest="$BACKUP_DIR/$CHECKSUMS_NAME" line digest name actual last_byte index
  local -a lines expected_names
  [ -f "$manifest" ] && [ ! -L "$manifest" ] \
    || { echo "ERROR: backup checksums are missing or not a regular file" >&2; exit 1; }
  [ "$(stat -c '%a' "$manifest" 2>/dev/null || stat -f '%Lp' "$manifest")" = "600" ] \
    || { echo "ERROR: backup checksums must have mode 600" >&2; exit 1; }
  last_byte="$(tail -c 1 "$manifest" | od -An -tx1 | tr -d ' \n')"
  [ "$last_byte" = "0a" ] \
    || { echo "ERROR: backup checksums must end with exactly a newline" >&2; exit 1; }

  if [ -L "$BACKUP_DIR/.env.backup" ] || { [ -e "$BACKUP_DIR/.env.backup" ] && [ ! -f "$BACKUP_DIR/.env.backup" ]; }; then
    echo "ERROR: .env.backup is not a regular file" >&2
    exit 1
  fi
  for name in "$DATABASE_DUMP_NAME" "$SERVER_ARCHIVE_NAME"; do
    [ -f "$BACKUP_DIR/$name" ] && [ ! -L "$BACKUP_DIR/$name" ] \
      || { echo "ERROR: backup artifact '$name' is missing or not a regular file" >&2; exit 1; }
  done

  expected_names=("$DATABASE_DUMP_NAME" "$SERVER_ARCHIVE_NAME")
  [ -f "$BACKUP_DIR/.env.backup" ] && [ ! -L "$BACKUP_DIR/.env.backup" ] \
    && expected_names=(".env.backup" "${expected_names[@]}")
  mapfile -t lines < "$manifest"
  [ "${#lines[@]}" = "${#expected_names[@]}" ] \
    || { echo "ERROR: backup checksums must contain exactly one record per artifact" >&2; exit 1; }
  for index in "${!expected_names[@]}"; do
    line="${lines[$index]}"
    [ "${#line}" -ge 67 ] || { echo "ERROR: backup checksums contain a malformed record" >&2; exit 1; }
    digest="${line:0:64}"
    [ "${line:64:2}" = "  " ] && [[ "$digest" =~ ^[0-9a-f]{64}$ ]] \
      || { echo "ERROR: backup checksums contain a malformed record" >&2; exit 1; }
    name="${line:66}"
    [ "$name" = "${expected_names[$index]}" ] \
      || { echo "ERROR: backup checksums contain an unexpected or out-of-order artifact name" >&2; exit 1; }
    actual="$(sha256sum -- "$BACKUP_DIR/$name" | awk '{print $1}')"
    [ "$actual" = "$digest" ] \
      || { echo "ERROR: checksum mismatch for '$name'" >&2; exit 1; }
  done
}

validate_operator_override() {
  local path="$INSTALL_DIR/docker-compose.override.yml"
  if [ -L "$path" ]; then
    echo "ERROR: refusing to use symlinked Compose override '$path'" >&2
    exit 1
  fi
  if [ -e "$path" ] && [ ! -f "$path" ]; then
    echo "ERROR: refusing to use non-regular Compose override '$path'" >&2
    exit 1
  fi
}

run_compose() {
  (
    cd "$INSTALL_DIR"
    "${COMPOSE[@]}" "$@"
  )
}

backup() {
  local metadata postgres_volume server_volume utility_image
  # Discover both identities before creating backup artefacts or starting any
  # data operation, so a bind/empty/ambiguous mount fails closed.
  postgres_volume="$(discover_volume "PostgreSQL" "$POSTGRES_CONTAINER" "$POSTGRES_TARGET")"
  server_volume="$(discover_volume "server-data" "$SERVER_CONTAINER" "$SERVER_TARGET")"
  utility_image="$(discover_utility_image)"
  prepare_backup_dir
  metadata="$BACKUP_DIR/$METADATA_NAME"
  # Discovery and the mode-600 identity record happen before any backup or
  # shutdown mutation. This is the only source restore uses for volume names.
  write_metadata "$metadata" "$postgres_volume" "$server_volume" "$utility_image"
  read_metadata "$metadata"
  docker exec "$POSTGRES_CONTAINER" pg_dump -U silentsuite silentsuite > "$BACKUP_DIR/$DATABASE_DUMP_NAME"
  : > "$BACKUP_DIR/$SERVER_ARCHIVE_NAME"
  chmod 600 "$BACKUP_DIR/$SERVER_ARCHIVE_NAME"
  docker run --rm \
    --network none \
    --entrypoint tar \
    --mount "type=volume,source=$SERVER_VOLUME,target=/data,readonly" \
    "$UTILITY_IMAGE" czf - -C /data . > "$BACKUP_DIR/$SERVER_ARCHIVE_NAME"
  if [ -f "$INSTALL_DIR/.env" ] && [ ! -L "$INSTALL_DIR/.env" ]; then
    cp -p -- "$INSTALL_DIR/.env" "$BACKUP_DIR/.env.backup"
    chmod 600 "$BACKUP_DIR/.env.backup"
  fi
  chmod 600 "$BACKUP_DIR/$DATABASE_DUMP_NAME" "$BACKUP_DIR/$SERVER_ARCHIVE_NAME"
  write_checksum_manifest
  echo "Backup complete: $BACKUP_DIR"
}

restore() {
  local metadata override operator_override
  BACKUP_DIR="$(cd "$BACKUP_DIR" 2>/dev/null && pwd)" \
    || { echo "ERROR: backup directory does not exist" >&2; exit 1; }
  metadata="$BACKUP_DIR/$METADATA_NAME"
  read_metadata "$metadata"
  verify_backup_integrity
  validate_operator_override
  docker pull "$UTILITY_IMAGE" >/dev/null \
    || { echo "ERROR: recorded utility image is unavailable; restore will not modify the stack" >&2; exit 1; }

  override="$(mktemp "${TMPDIR:-/tmp}/silentsuite-restore.XXXXXXXX.yml")"
  chmod 600 "$override"
  RESTORE_OVERRIDE="$override"
  trap 'rm -f -- "$RESTORE_OVERRIDE"' EXIT
  printf 'volumes:\n  pgdata:\n    external: true\n    name: %s\n  server_data:\n    external: true\n    name: %s\n' "$POSTGRES_VOLUME" "$SERVER_VOLUME" > "$override"
  operator_override="$INSTALL_DIR/docker-compose.override.yml"
  RESTORE_COMPOSE_ARGS=(-f docker-compose.yml)
  if [ -f "$operator_override" ]; then
    RESTORE_COMPOSE_ARGS+=(-f docker-compose.override.yml)
  fi
  RESTORE_COMPOSE_ARGS+=(-f "$override")
  compose_restore() {
    (cd "$INSTALL_DIR"; "${COMPOSE[@]}" "${RESTORE_COMPOSE_ARGS[@]}" down)
  }
  compose_start_postgres() {
    (cd "$INSTALL_DIR"; "${COMPOSE[@]}" "${RESTORE_COMPOSE_ARGS[@]}" up -d postgres)
  }
  compose_start_all() {
    (cd "$INSTALL_DIR"; "${COMPOSE[@]}" "${RESTORE_COMPOSE_ARGS[@]}" up -d)
  }

  compose_restore
  for volume in "$POSTGRES_VOLUME" "$SERVER_VOLUME"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      docker volume rm -- "$volume"
    fi
    docker volume create -- "$volume" >/dev/null
  done
  docker run --rm \
    --network none \
    --entrypoint tar \
    --mount "type=volume,source=$SERVER_VOLUME,target=/data" \
    "$UTILITY_IMAGE" xzf - -C /data < "$BACKUP_DIR/$SERVER_ARCHIVE_NAME"
  compose_start_postgres
  for attempt in $(seq 1 30); do
    if [ "$(docker inspect --format '{{.State.Health.Status}}' "$POSTGRES_CONTAINER" 2>/dev/null || true)" = "healthy" ]; then break; fi
    [ "$attempt" = 30 ] && { echo "ERROR: PostgreSQL did not become healthy during restore" >&2; exit 1; }
    sleep 1
  done
  docker exec -i "$POSTGRES_CONTAINER" psql -U silentsuite silentsuite < "$BACKUP_DIR/$DATABASE_DUMP_NAME"
  compose_start_all
  echo "Restore complete using the recorded volume identities"
}

case "$COMMAND" in
  record)
    record_live_volumes "$METADATA_PATH"
    ;;
  backup) backup ;;
  restore) restore ;;
esac
