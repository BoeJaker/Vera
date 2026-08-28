#!/bin/sh
# Containerised production build for the VENDORED Godseye app (a separate
# upstream repo, never part of Vera's source tree — see godseye_core.py).
#
# Runs inside a throwaway node image with the vendor directory bind-mounted at
# /work, so the host needs no Node toolchain at all. Everything it writes lands
# in the bind mount: the build log (so the caller can read it while the
# container is still running) and the app's own dist/.
#
# Environment (all supplied by godseye.build):
#   GODSEYE_SRC            clone directory inside the container  (default /work/godseye)
#   GODSEYE_LOG            build log path inside the container   (default /work/.godseye/build.log)
#   GODSEYE_BASE           Vite public base path                 (default /godseye/app/)
#   GODSEYE_REFRESH        "1" to also refresh the runtime manifests (slow, network-heavy)
#   GODSEYE_CLEAN_INSTALL  "1" to force `npm ci` even when node_modules is current
set -u

SRC="${GODSEYE_SRC:-/work/godseye}"
LOG="${GODSEYE_LOG:-/work/.godseye/build.log}"
BASE="${GODSEYE_BASE:-/godseye/app/}"
REFRESH="${GODSEYE_REFRESH:-0}"
CLEAN="${GODSEYE_CLEAN_INSTALL:-0}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null

# Truncate, then append: the log is the ONLY progress channel the caller has
# (the container is detached), so every stage must flush into it as it happens.
: > "$LOG" || { echo "cannot write $LOG" >&2; exit 3; }
{
  echo "[godseye] build started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[godseye] src=$SRC base=$BASE refresh_manifests=$REFRESH clean_install=$CLEAN"
} >> "$LOG" 2>&1

cd "$SRC" 2>>"$LOG" || { echo "GODSEYE_EXIT=2 (no clone at $SRC)" >> "$LOG"; exit 2; }

echo "[godseye] node $(node --version) / npm $(npm --version)" >> "$LOG" 2>&1

# `npm ci` wipes and reinstalls node_modules from scratch every time (minutes,
# and Cesium alone is hundreds of MB). Only pay that when the lockfile has
# actually moved — or when the caller explicitly asks for a clean install.
if [ "$CLEAN" = "1" ] || [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then
  echo "[godseye] --- npm ci ---" >> "$LOG" 2>&1
  npm ci --no-audit --no-fund >> "$LOG" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "GODSEYE_EXIT=$rc (npm ci failed)" >> "$LOG"
    exit "$rc"
  fi
else
  echo "[godseye] --- npm ci skipped (node_modules is current) ---" >> "$LOG" 2>&1
fi

# The committed public/manifests/*.json are what the app ships with. Refreshing
# them re-verifies thousands of upstream CCTV/satellite endpoints over the
# network and takes a long time, so it is opt-in and NON-FATAL: a failed refresh
# must still leave the committed manifests in place and let the build proceed.
if [ "$REFRESH" = "1" ]; then
  echo "[godseye] --- manifests:refresh ---" >> "$LOG" 2>&1
  npm run manifests:refresh >> "$LOG" 2>&1 || \
    echo "[godseye] manifests:refresh failed; keeping the committed manifests" >> "$LOG" 2>&1
fi

echo "[godseye] --- vite build (base=$BASE) ---" >> "$LOG" 2>&1
npx vite build --base="$BASE" >> "$LOG" 2>&1
rc=$?

# vite-plugin-cesium bug: it emits Cesium's URL as `<base>/cesium/` but copies
# the runtime to `dist/<base>/cesium/` — the base gets applied twice, once in
# the URL and once in the output path, so every Cesium worker/asset 404s when
# base is anything but "/". Move the copy to where the emitted URLs point.
if [ "$rc" -eq 0 ] && [ "$BASE" != "/" ]; then
  NESTED="dist/${BASE#/}"
  NESTED="${NESTED%/}/cesium"
  if [ -d "$NESTED" ]; then
    echo "[godseye] --- un-nesting cesium runtime ($NESTED -> dist/cesium) ---" >> "$LOG" 2>&1
    rm -rf dist/cesium
    mv "$NESTED" dist/cesium >> "$LOG" 2>&1 || rc=$?
    # Drop the now-empty base-shaped directories the copy left behind
    # (dist/godseye/app/ for base=/godseye/app/), deepest first.
    REL="${BASE#/}"
    TOP="${REL%%/*}"
    case "$TOP" in
      ""|.|..|dist|assets) ;;
      *) find "dist/$TOP" -depth -type d -empty -delete 2>/dev/null ;;
    esac
  fi
fi

echo "GODSEYE_EXIT=$rc" >> "$LOG" 2>&1
exit "$rc"
