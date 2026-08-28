#!/usr/bin/env bash
set -euo pipefail

# SilentSuite Self-Hosted Manual Upgrade
# --------------------------------------
# Applies one already staged and verified release bundle. This is deliberately
# an operator-invoked procedure: it never discovers a release or performs an
# unattended upgrade.

STAGED_DIR=""
INSTALL_DIR=""

usage() {
  cat <<EOF
Usage: upgrade.sh --staged <directory> --install-dir <directory>

  --staged <directory>      Output from install.sh --stage-only.
  --install-dir <directory> Existing SilentSuite installation to upgrade.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --staged)
      [ $# -ge 2 ] && [ -n "$2" ] || { echo "ERROR: --staged requires a directory" >&2; exit 2; }
      STAGED_DIR="$2"
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
      echo "ERROR: unknown argument '$1'. Run with --help for usage." >&2
      exit 2
      ;;
  esac
done

[ -n "$STAGED_DIR" ] || { echo "ERROR: --staged is required" >&2; exit 2; }
[ -n "$INSTALL_DIR" ] || { echo "ERROR: --install-dir is required" >&2; exit 2; }
[ -d "$STAGED_DIR" ] || { echo "ERROR: staged directory '$STAGED_DIR' does not exist" >&2; exit 1; }
[ -d "$INSTALL_DIR" ] || { echo "ERROR: install directory '$INSTALL_DIR' does not exist" >&2; exit 1; }

STAGED_DIR="$(cd "$STAGED_DIR" && pwd)"
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd)"
[ "$STAGED_DIR" != "$INSTALL_DIR" ] || { echo "ERROR: staged and installed directories must differ" >&2; exit 1; }
[ -f "$INSTALL_DIR/.env" ] || { echo "ERROR: '$INSTALL_DIR/.env' is missing" >&2; exit 1; }

MANIFEST_NAME="server-image.json"
MANAGED_FILES=(
  .env.example
  SELF-HOSTING.md
  close-signups.sh
  docker-compose.yml
  install.sh
  success.html
  upgrade.sh
  update.sh
  verify.sh
)

fail() {
  echo "ERROR: $1" >&2
  exit 1
}

if command -v sha256sum >/dev/null 2>&1; then
  sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  fail "neither 'sha256sum' nor 'shasum' is available; cannot verify the staged archive"
fi

escape_ere() {
  printf '%s' "$1" | sed 's/[][^.*$\\/+?(){}|]/\\&/g'
}

for file in "${MANAGED_FILES[@]}" "$MANIFEST_NAME"; do
  [ -f "$STAGED_DIR/$file" ] || fail "staged release is missing '$file'"
done

MANIFEST="$STAGED_DIR/$MANIFEST_NAME"
manifest_value() {
  local key="$1"
  grep -E "^  \"$key\": \"" "$MANIFEST" | sed -E "s/^  \"$key\": \"(.*)\",?\$/\1/"
}
manifest_line_count() {
  local pattern="$1"
  grep -Ec "$pattern" "$MANIFEST" || true
}

[ "$(wc -l < "$MANIFEST" | tr -d ' ')" = "14" ] || fail "server-image.json has an unexpected length"
[ "$(tail -c 1 "$MANIFEST" | od -An -tu1 | tr -d ' \n')" = "10" ] || fail "server-image.json must end with a newline"
[ "$(manifest_line_count '^  "')" = "9" ] || fail "server-image.json has an unexpected field set"
[ "$(manifest_line_count '^  "schemaVersion": 1,$')" = "1" ] || fail "unsupported server-image.json schema version"

TAG="$(manifest_value tag)"
SOURCE_COMMIT="$(manifest_value sourceCommit)"
IMAGE_REPOSITORY="$(manifest_value imageRepository)"
INDEX_DIGEST="$(manifest_value indexDigest)"
AMD64_DIGEST="$(manifest_value amd64Digest)"
ARM64_DIGEST="$(manifest_value arm64Digest)"
EXPECTED_REVISION="$(manifest_value expectedRevision)"

printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$' || fail "staged manifest tag is not an immutable release tag"
printf '%s' "$SOURCE_COMMIT" | grep -Eq '^[0-9a-f]{40}$' || fail "staged manifest source commit is not immutable"
[ "$IMAGE_REPOSITORY" = "ghcr.io/silent-suite/silentsuite-server" ] || fail "staged manifest image repository is not canonical"
printf '%s' "$INDEX_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' || fail "staged manifest index digest is not immutable"
printf '%s' "$AMD64_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' || fail "staged manifest amd64 digest is not immutable"
printf '%s' "$ARM64_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' || fail "staged manifest arm64 digest is not immutable"
[ "$INDEX_DIGEST" != "$AMD64_DIGEST" ] || fail "staged manifest index and amd64 digests must differ"
[ "$INDEX_DIGEST" != "$ARM64_DIGEST" ] || fail "staged manifest index and arm64 digests must differ"
[ "$AMD64_DIGEST" != "$ARM64_DIGEST" ] || fail "staged manifest platform digests must differ"
printf '%s' "$EXPECTED_REVISION" | grep -Eq '^[0-9a-f]{40}$' || fail "staged manifest revision is not immutable"
[ "$EXPECTED_REVISION" = "$SOURCE_COMMIT" ] || fail "staged manifest revision does not match its source commit"
[ "$(manifest_line_count '^    "linux/amd64",$')" = "1" ] || fail "staged manifest platform list is invalid"
[ "$(manifest_line_count '^    "linux/arm64"$')" = "1" ] || fail "staged manifest platform list is invalid"

BUNDLE_NAME="silentsuite-self-host-${TAG}.tar.gz"
BUNDLE_PREFIX="silentsuite-self-host-${TAG}"
CHECKSUM_NAME="${BUNDLE_NAME}.sha256"
[ -f "$STAGED_DIR/$BUNDLE_NAME" ] || fail "staged release is missing '$BUNDLE_NAME'"
[ -f "$STAGED_DIR/$CHECKSUM_NAME" ] || fail "staged release is missing '$CHECKSUM_NAME'"
CHECKSUM_RECORD="$(cat "$STAGED_DIR/$CHECKSUM_NAME")"
[ "$(wc -l < "$STAGED_DIR/$CHECKSUM_NAME" | tr -d ' ')" = "1" ] || fail "staged checksum must contain exactly one record"
[ "$(tail -c 1 "$STAGED_DIR/$CHECKSUM_NAME" | od -An -tu1 | tr -d ' \n')" = "10" ] || fail "staged checksum must end with a newline"
printf '%s' "$CHECKSUM_RECORD" | grep -Eq "^[0-9a-fA-F]{64}  $(escape_ere "$BUNDLE_NAME")$" || fail "staged checksum names the wrong archive"

EXPECTED_DIGEST="$(cut -c1-64 < "$STAGED_DIR/$CHECKSUM_NAME" | tr 'A-F' 'a-f')"
ACTUAL_DIGEST="$(sha256_of "$STAGED_DIR/$BUNDLE_NAME" | tr 'A-F' 'a-f')"
[ "$EXPECTED_DIGEST" = "$ACTUAL_DIGEST" ] || fail "staged archive does not match its checksum sidecar"

STAGED_FILES="$(find "$STAGED_DIR" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' | LC_ALL=C sort)"
EXPECTED_FILES="$(printf '%s\n' "${MANAGED_FILES[@]}" "$MANIFEST_NAME" "$BUNDLE_NAME" "$CHECKSUM_NAME" | LC_ALL=C sort)"
[ "$STAGED_FILES" = "$EXPECTED_FILES" ] || fail "staged release contains an unexpected or missing file"
NON_FILES="$(find "$STAGED_DIR" -mindepth 1 -maxdepth 1 ! -type f -print -quit)"
[ -z "$NON_FILES" ] || fail "staged release contains a non-regular entry '$NON_FILES'"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/silentsuite-upgrade.XXXXXXXX")"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# Stage-only leaves both the archive and its extracted files in place for
# operator inspection. At upgrade time the archive is authoritative: verify it
# again and never install mutable staged extracted copies.
MEMBER_LIST="$WORKDIR/members.txt"
if ! tar -tzf "$STAGED_DIR/$BUNDLE_NAME" > "$MEMBER_LIST"; then
  fail "'$BUNDLE_NAME' is not a readable gzip archive"
fi
if ! tar -tvzf "$STAGED_DIR/$BUNDLE_NAME" > "$WORKDIR/members-verbose.txt"; then
  fail "'$BUNDLE_NAME' could not be listed"
fi
if grep -qE '^[^-d]' "$WORKDIR/members-verbose.txt" || grep -q ' -> ' "$WORKDIR/members-verbose.txt"; then
  fail "'$BUNDLE_NAME' contains links or special files"
fi

ENTRY_LIST="$WORKDIR/entries.txt"
: > "$ENTRY_LIST"
while IFS= read -r member; do
  [ -n "$member" ] || continue
  case "$member" in
    /*|"$BUNDLE_PREFIX"/../*|*/../*|*/..|../*|..)
      fail "'$BUNDLE_NAME' contains an unsafe path: $member"
      ;;
  esac
  case "$member" in
    "$BUNDLE_PREFIX"|"$BUNDLE_PREFIX"/) continue ;;
    "$BUNDLE_PREFIX"/*) printf '%s\n' "${member#"$BUNDLE_PREFIX"/}" >> "$ENTRY_LIST" ;;
    *) fail "'$BUNDLE_NAME' contains a member outside $BUNDLE_PREFIX/: $member" ;;
  esac
done < "$MEMBER_LIST"

EXPECTED_MEMBERS="$(printf '%s\n' \
  .env.example \
  SELF-HOSTING.md \
  close-signups.sh \
  docker-compose.yml \
  install.sh \
  server-image.json \
  success.html \
  upgrade.sh \
  update.sh \
  verify.sh | LC_ALL=C sort)"
ACTUAL_MEMBERS="$(sed 's#/$##' "$ENTRY_LIST" | LC_ALL=C sort)"
[ "$ACTUAL_MEMBERS" = "$EXPECTED_MEMBERS" ] || fail "'$BUNDLE_NAME' does not contain the expected set of files"

STAGING="$WORKDIR/staging"
mkdir -p "$STAGING"
tar -xzf "$STAGED_DIR/$BUNDLE_NAME" -C "$STAGING" --no-same-owner
BUNDLE_ROOT="$STAGING/$BUNDLE_PREFIX"
while IFS= read -r extracted; do
  [ -f "$BUNDLE_ROOT/$extracted" ] || fail "'$BUNDLE_NAME' did not extract $extracted as a regular file"
done <<MEMBERS
$EXPECTED_MEMBERS
MEMBERS

cmp -s "$BUNDLE_ROOT/$MANIFEST_NAME" "$MANIFEST" || fail "the manifest inside '$BUNDLE_NAME' differs from the staged published manifest"
for file in "${MANAGED_FILES[@]}" "$MANIFEST_NAME"; do
  cmp -s "$STAGED_DIR/$file" "$BUNDLE_ROOT/$file" || fail "staged '$file' differs from the verified archive"
done

MACHINE="$(uname -m)"
case "$MACHINE" in
  x86_64|amd64) HOST_PLATFORM="linux/amd64" ;;
  aarch64|arm64) HOST_PLATFORM="linux/arm64" ;;
  *) fail "unsupported host architecture '$MACHINE'" ;;
esac
case "$HOST_PLATFORM" in
  linux/amd64) grep -Fqx '    "linux/amd64",' "$MANIFEST" || fail "staged release does not publish an image for $HOST_PLATFORM" ;;
  linux/arm64) grep -Fqx '    "linux/arm64"' "$MANIFEST" || fail "staged release does not publish an image for $HOST_PLATFORM" ;;
esac

TARGET_IMAGE="${IMAGE_REPOSITORY}@${INDEX_DIGEST}"
ENV_MATCHES="$(grep -Ec '^SILENTSUITE_SERVER_IMAGE=' "$INSTALL_DIR/.env" || true)"
[ "$ENV_MATCHES" = "1" ] || fail "installed .env must contain exactly one SILENTSUITE_SERVER_IMAGE entry"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  fail "docker Compose is required"
fi

update_env() {
  local input="$1" output="$2" image="$3"
  awk -v image="$image" '
    /^SILENTSUITE_SERVER_IMAGE=/ {
      count++
      print "SILENTSUITE_SERVER_IMAGE=" image
      next
    }
    { print }
    END { if (count != 1) exit 1 }
  ' "$input" > "$output" || fail "could not update the installed image identity"
}

prepare_admission_dir() {
  cp "$BUNDLE_ROOT/docker-compose.yml" "$WORKDIR/docker-compose.yml"
  cp "$INSTALL_DIR/.env" "$WORKDIR/.env"
  for file in success.html etebase-server.ini docker-compose.override.yml; do
    if [ -e "$INSTALL_DIR/$file" ]; then
      [ -f "$INSTALL_DIR/$file" ] || fail "operator file '$file' is not a regular file"
      cp "$INSTALL_DIR/$file" "$WORKDIR/$file"
    fi
  done
  update_env "$WORKDIR/.env" "$WORKDIR/.env.next" "$TARGET_IMAGE"
  mv "$WORKDIR/.env.next" "$WORKDIR/.env"
}

compose_images() {
  local directory="$1"
  (
    cd "$directory"
    if [ -f docker-compose.override.yml ]; then
      "${COMPOSE[@]}" -f docker-compose.yml -f docker-compose.override.yml config --images
    else
      "${COMPOSE[@]}" -f docker-compose.yml config --images
    fi
  )
}

confirm_compose_image() {
  local directory="$1" images matches
  images="$(compose_images "$directory")"
  matches="$(printf '%s\n' "$images" | grep -Fxc "$TARGET_IMAGE" || true)"
  [ "$matches" = "1" ] || fail "Compose did not render exactly the staged image identity '$TARGET_IMAGE'"
}

inspect_image() {
  local revision platform repo_digests
  revision="$(docker image inspect "$TARGET_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
  platform="$(docker image inspect "$TARGET_IMAGE" --format '{{.Os}}/{{.Architecture}}')"
  repo_digests="$(docker image inspect "$TARGET_IMAGE" --format '{{json .RepoDigests}}')"
  [ "$revision" = "$EXPECTED_REVISION" ] || fail "pulled image revision '$revision' does not match $EXPECTED_REVISION"
  [ "$platform" = "$HOST_PLATFORM" ] || fail "pulled image platform '$platform' does not match $HOST_PLATFORM"
  case "$repo_digests" in
    *"$TARGET_IMAGE"*) ;;
    *) fail "pulled image does not carry the staged repository@index identity" ;;
  esac
}

# Admit the target in an isolated Compose directory before changing the
# installation. The staged manifest and checksum are the independent release
# identity; .env is only used to make Compose render that exact identity.
prepare_admission_dir
confirm_compose_image "$WORKDIR"
docker pull "$TARGET_IMAGE" >/dev/null
inspect_image

echo "Installing verified release files..."
for file in "${MANAGED_FILES[@]}" "$MANIFEST_NAME"; do
  cp "$BUNDLE_ROOT/$file" "$INSTALL_DIR/$file"
done
cp "$STAGED_DIR/$CHECKSUM_NAME" "$INSTALL_DIR/$CHECKSUM_NAME"
chmod +x "$INSTALL_DIR/install.sh" "$INSTALL_DIR/update.sh" "$INSTALL_DIR/upgrade.sh" "$INSTALL_DIR/verify.sh" "$INSTALL_DIR/close-signups.sh"
update_env "$INSTALL_DIR/.env" "$WORKDIR/.env.next" "$TARGET_IMAGE"
chmod 600 "$WORKDIR/.env.next"
mv "$WORKDIR/.env.next" "$INSTALL_DIR/.env"

cmp -s "$STAGED_DIR/$MANIFEST_NAME" "$INSTALL_DIR/$MANIFEST_NAME" || fail "installed server-image.json differs from the staged manifest"
cmp -s "$STAGED_DIR/$CHECKSUM_NAME" "$INSTALL_DIR/$CHECKSUM_NAME" || fail "installed checksum differs from the staged checksum"
confirm_compose_image "$INSTALL_DIR"

echo "Pulling the admitted image through Compose..."
(
  cd "$INSTALL_DIR"
  if [ -f docker-compose.override.yml ]; then
    "${COMPOSE[@]}" -f docker-compose.yml -f docker-compose.override.yml pull server
  else
    "${COMPOSE[@]}" -f docker-compose.yml pull server
  fi
)
inspect_image

echo "Applying database migrations..."
(
  cd "$INSTALL_DIR"
  if [ -f docker-compose.override.yml ]; then
    "${COMPOSE[@]}" -f docker-compose.yml -f docker-compose.override.yml run --rm --no-deps server python manage.py migrate --noinput
    "${COMPOSE[@]}" -f docker-compose.yml -f docker-compose.override.yml up -d
  else
    "${COMPOSE[@]}" -f docker-compose.yml run --rm --no-deps server python manage.py migrate --noinput
    "${COMPOSE[@]}" -f docker-compose.yml up -d
  fi
)
"$INSTALL_DIR/verify.sh"
echo "Manual upgrade complete: $TARGET_IMAGE"
