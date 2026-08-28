#!/bin/sh
# Clone (or fast-forward) the VENDORED Godseye repo — inside a throwaway
# container, deliberately.
#
# Doing this on the host inherits the operator's ~/.gitconfig, and prod carries
#   url."git@github.com:".insteadOf = https://github.com/
# which silently rewrote the pinned HTTPS url to SSH and failed the clone of a
# PUBLIC repo with "Permission denied (publickey)". A container has no operator
# git config, no credential helper and no signing config, so that whole class of
# failure cannot occur here. It also means the host needs no git at all — the
# same reason the build runs in a container rather than needing Node.
#
# Environment (all supplied by godseye.repo.sync):
#   GODSEYE_SRC     clone directory inside the container  (default /work/godseye)
#   GODSEYE_URL     repository url (already pinned + validated by the caller)
#   GODSEYE_REF     branch/tag/sha to check out, or empty for the default branch
#   GODSEYE_DEPTH   shallow depth, 0 for a full clone       (default 1)
#   GODSEYE_META    where to write the commit metadata JSON
#   GODSEYE_UID/GID own the result as this host user so the build (which runs
#                   unprivileged) can write node_modules/dist into it
set -u

SRC="${GODSEYE_SRC:-/work/godseye}"
URL="${GODSEYE_URL:-}"
REF="${GODSEYE_REF:-}"
DEPTH="${GODSEYE_DEPTH:-1}"
META="${GODSEYE_META:-/work/.godseye/repo.json}"
UID_="${GODSEYE_UID:-0}"
GID_="${GODSEYE_GID:-0}"

[ -n "$URL" ] || { echo "GODSEYE_URL is required" >&2; exit 2; }

# The node image has no git. Installing it per run keeps this to ONE image for
# both sync and build, so there is no second thing to pull, pin or keep current.
if ! command -v git >/dev/null 2>&1; then
  apk add --no-cache git >/dev/null 2>&1 || {
    echo "could not install git into the build image" >&2; exit 3; }
fi

export GIT_TERMINAL_PROMPT=0     # fail instead of hanging on a credential prompt

# This container runs as root but the clone is owned by the HOST user (it is
# chowned below so the unprivileged build can write into it). On the second run
# git then refuses the repo with "detected dubious ownership" and the update
# path dies. Declare it safe — through the environment, so this throwaway
# container writes no git config anywhere.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="$SRC"

if [ -d "$SRC/.git" ]; then
  cd "$SRC" || exit 2
  # fetch + hard checkout rather than `git pull`: the clone is a managed
  # artifact, and a merge conflict here would wedge it with no way out.
  git fetch --depth "${DEPTH:-1}" origin "${REF:-HEAD}" || exit 4
  git checkout --force --detach FETCH_HEAD || exit 4
  ACTION=updated
else
  mkdir -p "$(dirname "$SRC")"
  set -- clone
  [ "${DEPTH:-1}" -gt 0 ] 2>/dev/null && set -- "$@" --depth "$DEPTH"
  [ -n "$REF" ] && set -- "$@" --branch "$REF"
  git "$@" -- "$URL" "$SRC" || exit 4
  cd "$SRC" || exit 2
  ACTION=cloned
fi

# Commit metadata is captured HERE, by the container that has git, so nothing
# downstream (godseye.status) needs git on the host to describe the clone.
mkdir -p "$(dirname "$META")"
SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
SHORT=$(git rev-parse --short HEAD 2>/dev/null || echo "")
WHEN=$(git log -1 --format=%cI 2>/dev/null || echo "")
SUBJECT=$(git log -1 --format=%s 2>/dev/null | tr -d '"\\' | cut -c1-160)
cat > "$META" <<JSON
{"sha":"$SHA","short":"$SHORT","committed_at":"$WHEN","subject":"$SUBJECT",
 "url":"$URL","ref":"$REF","action":"$ACTION","synced_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
JSON

# Hand the tree to the host user; the build runs unprivileged and must be able
# to write node_modules/ and dist/ into it.
chown -R "$UID_:$GID_" "$SRC" "$META" 2>/dev/null

echo "GODSEYE_SYNC=$ACTION $SHORT"
