#!/bin/sh
# Clone / update OUR FORK of Godseye — inside a throwaway container, deliberately.
#
# Doing this on the host inherits the operator's ~/.gitconfig, and prod carries
#   url."git@github.com:".insteadOf = https://github.com/
# which silently rewrote the pinned HTTPS url to SSH and failed the clone of a
# PUBLIC repo with "Permission denied (publickey)". A container has no operator
# git config, no credential helper and no signing config, so that whole class of
# failure cannot occur here. It also means the host needs no git at all — the
# same reason the build runs in a container rather than needing Node.
#
# FORK MODEL
# ──────────
# The working copy is a real fork, not a mirror:
#   remote `upstream`  the original project, only ever fetched
#   remote `fork`      our durable home (a bare repo outside the disposable
#                      vendor dir); holds branch $WORKBRANCH
#   branch $WORKBRANCH our version — where local commits live
#
# Updating from upstream is a MERGE into our branch, never a hard reset: the
# whole point of a fork is that our commits survive the update. A conflicting
# merge stops and reports rather than discarding anything, and the previous tip
# is always recoverable from the fork remote.
#
# Environment (all supplied by godseye.repo.sync):
#   GODSEYE_SRC     clone directory inside the container  (default /work/godseye)
#   GODSEYE_URL     upstream url (pinned + validated by the caller)
#   GODSEYE_FORK    our fork remote (path or url); "" disables the fork remote
#   GODSEYE_BRANCH  our working branch                    (default vera)
#   GODSEYE_REF     upstream ref to track                 (default its HEAD)
#   GODSEYE_DEPTH   shallow depth, 0 for full             (default 0 — a fork
#                   needs real history to merge and push)
#   GODSEYE_META    where to write the commit metadata JSON
#   GODSEYE_UID/GID own the result as this host user so the build (which runs
#                   unprivileged) can write node_modules/dist into it
set -u

SRC="${GODSEYE_SRC:-/work/godseye}"
URL="${GODSEYE_URL:-}"
FORK="${GODSEYE_FORK:-}"
WORKBRANCH="${GODSEYE_BRANCH:-vera}"
REF="${GODSEYE_REF:-}"
DEPTH="${GODSEYE_DEPTH:-0}"
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
# chowned below so the unprivileged build can write into it). Git would refuse
# the repo with "detected dubious ownership"; declare it safe through the
# environment so this throwaway container writes no git config anywhere.
# Written to a real GLOBAL config, not passed as GIT_CONFIG_* / -c. Git
# deliberately ignores `safe.directory` from command-line-style config, and
# `git push` to a local path spawns receive-pack as a separate process that
# re-reads config anyway — so the env form silently failed to take with
# "detected dubious ownership in repository at '/fork'". HOME is /tmp inside
# this throwaway container, so this writes no state the host will ever see.
export HOME="${HOME:-/tmp}"
git config --global --add safe.directory "$SRC"
[ -n "$FORK" ] && git config --global --add safe.directory "$FORK"
# Merges need an identity. The fork is a machine-maintained integration branch;
# attribute it to Vera rather than to whoever happens to be running the host.
git config --global user.name "Vera"
git config --global user.email "vera@localhost"

# A local fork path is bootstrapped on first use. Doing it here rather than on
# the host keeps the "no git on the host" property intact.
case "$FORK" in
  ""|*://*) ;;                                  # remote url — nothing to create
  *)
    if [ ! -e "$FORK/HEAD" ]; then
      mkdir -p "$FORK"
      git init --bare --initial-branch="$WORKBRANCH" "$FORK" >/dev/null 2>&1 \
        || git init --bare "$FORK" >/dev/null 2>&1 \
        || { echo "could not create the fork repo at $FORK" >&2; FORK=""; }
    fi
    ;;
esac

depth_args() {
  # A fork must be able to merge and push, and neither works reliably from a
  # shallow history, so depth is opt-in rather than the default.
  [ "${DEPTH:-0}" -gt 0 ] 2>/dev/null && printf -- '--depth %s' "$DEPTH"
}

ACTION=""
if [ -d "$SRC/.git" ]; then
  cd "$SRC" || exit 2
else
  mkdir -p "$(dirname "$SRC")"
  # Prefer OUR fork as the clone source: it already carries our branch. Fall
  # back to upstream the first time, before the fork has anything in it.
  CLONED=""
  if [ -n "$FORK" ] && git ls-remote "$FORK" >/dev/null 2>&1; then
    if git clone $(depth_args) --origin fork --branch "$WORKBRANCH" \
         -- "$FORK" "$SRC" >/dev/null 2>&1; then
      CLONED=fork
    fi
  fi
  if [ -z "$CLONED" ]; then
    git clone $(depth_args) --origin upstream -- "$URL" "$SRC" || exit 4
    CLONED=upstream
  fi
  cd "$SRC" || exit 2
  ACTION="cloned-from-$CLONED"
fi

# Both remotes always present and correct, however we got here.
git remote get-url upstream >/dev/null 2>&1 \
  && git remote set-url upstream "$URL" \
  || git remote add upstream "$URL"
if [ -n "$FORK" ]; then
  git remote get-url fork >/dev/null 2>&1 \
    && git remote set-url fork "$FORK" \
    || git remote add fork "$FORK"
fi
# `origin` is ambiguous once there are two real remotes; drop it so nothing
# silently pushes our fork's commits at the upstream project.
git remote get-url origin >/dev/null 2>&1 && git remote remove origin

git fetch $(depth_args) upstream || exit 4
UPSTREAM_REF="upstream/${REF:-HEAD}"
git rev-parse --verify "$UPSTREAM_REF" >/dev/null 2>&1 || UPSTREAM_REF="upstream/HEAD"
git rev-parse --verify "$UPSTREAM_REF" >/dev/null 2>&1 || UPSTREAM_REF=FETCH_HEAD

# Make sure our working branch exists and is checked out.
if git rev-parse --verify "refs/heads/$WORKBRANCH" >/dev/null 2>&1; then
  git checkout "$WORKBRANCH" || exit 4
else
  git checkout -b "$WORKBRANCH" "$UPSTREAM_REF" || exit 4
  [ -n "$ACTION" ] || ACTION="branched"
fi

# Bring upstream in. MERGE, so our commits survive; a conflict stops here with
# the tree left exactly as it was for a human to resolve.
MERGE_STATUS=up-to-date
if [ "$(git rev-parse HEAD)" != "$(git rev-parse "$UPSTREAM_REF")" ]; then
  if git merge --no-edit "$UPSTREAM_REF" >/dev/null 2>&1; then
    MERGE_STATUS=merged
  else
    git merge --abort 2>/dev/null
    MERGE_STATUS=conflict
  fi
fi
[ -n "$ACTION" ] || ACTION="$MERGE_STATUS"

# Publish our branch so the fork survives this directory being deleted.
PUSHED=no
PUSH_ERR=""
if [ -n "$FORK" ]; then
  # Capture the reason. A silent "push=failed" is useless: the fork is the only
  # thing standing between our commits and a `rm -rf vendor/`, so when it does
  # not publish, the caller has to be told WHY.
  if PUSH_OUT=$(git push fork "$WORKBRANCH" 2>&1); then
    PUSHED=yes
  else
    PUSHED=failed
    PUSH_ERR=$(printf '%s' "$PUSH_OUT" | tr -d '"\\' | tr '\n' ' ' | cut -c1-300)
  fi
fi

mkdir -p "$(dirname "$META")"
SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
SHORT=$(git rev-parse --short HEAD 2>/dev/null || echo "")
WHEN=$(git log -1 --format=%cI 2>/dev/null || echo "")
SUBJECT=$(git log -1 --format=%s 2>/dev/null | tr -d '"\\' | cut -c1-160)
AHEAD=$(git rev-list --count "$UPSTREAM_REF..HEAD" 2>/dev/null || echo 0)
BEHIND=$(git rev-list --count "HEAD..$UPSTREAM_REF" 2>/dev/null || echo 0)
UP_SHORT=$(git rev-parse --short "$UPSTREAM_REF" 2>/dev/null || echo "")
cat > "$META" <<JSON
{"sha":"$SHA","short":"$SHORT","committed_at":"$WHEN","subject":"$SUBJECT",
 "url":"$URL","fork":"$FORK","branch":"$WORKBRANCH","ref":"$REF",
 "upstream_short":"$UP_SHORT","ahead":$AHEAD,"behind":$BEHIND,
 "merge":"$MERGE_STATUS","pushed":"$PUSHED","push_error":"$PUSH_ERR","action":"$ACTION",
 "synced_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
JSON

# Hand the tree to the host user; the build runs unprivileged and must be able
# to write node_modules/ and dist/ into it.
chown -R "$UID_:$GID_" "$SRC" "$META" 2>/dev/null
# Same for the fork: this container created it as root, but it has to stay
# usable by the host user long after the container is gone.
case "$FORK" in
  ""|*://*) ;;
  *) chown -R "$UID_:$GID_" "$FORK" 2>/dev/null ;;
esac

echo "GODSEYE_SYNC=$ACTION $SHORT ahead=$AHEAD behind=$BEHIND merge=$MERGE_STATUS push=$PUSHED"
[ -n "$PUSH_ERR" ] && echo "GODSEYE_PUSH_ERROR=$PUSH_ERR"
[ "$MERGE_STATUS" = "conflict" ] && exit 5
exit 0
