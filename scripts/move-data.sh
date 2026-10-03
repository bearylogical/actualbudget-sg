#!/usr/bin/env sh
# Move the budget-app's own data between Docker hosts (Mac → server).
#
#   on the Mac:     docker compose stop scheduler && scripts/move-data.sh export
#   copy budget-app-data.tgz to the server (scp / rsync)
#   on the server:  scripts/move-data.sh import budget-app-data.tgz   (before `docker compose up`)
#
# What moves: the scheduler-data volume — review queue + what it learned, account map,
# category aliases, LLM cache, the saved Actual connection. Your budget itself lives on
# the Actual server and isn't touched; actual-data is only the bridge's cache and rebuilds.
set -eu
VOL=budget-app_scheduler-data
cmd=${1:-}
case "$cmd" in
  export)
    out=${2:-budget-app-data.tgz}
    docker volume inspect "$VOL" >/dev/null
    docker run --rm -v "$VOL":/v:ro -v "$(pwd)":/out alpine \
      tar czf "/out/$(basename "$out")" -C /v .
    echo "Wrote $out — contains your saved Actual password; copy it over a private link and delete it afterwards."
    ;;
  import)
    in=${2:?usage: move-data.sh import <file.tgz> [--force]}
    # labels = what Compose would set, so it adopts the volume without a warning
    docker volume inspect "$VOL" >/dev/null 2>&1 || docker volume create \
      --label com.docker.compose.project=budget-app \
      --label com.docker.compose.volume=scheduler-data "$VOL" >/dev/null
    if [ "${3:-}" != "--force" ] && [ -n "$(docker run --rm -v "$VOL":/v alpine ls -A /v)" ]; then
      echo "$VOL already has data. Re-run with --force to overwrite it." >&2; exit 1
    fi
    docker run --rm -v "$VOL":/v -v "$(cd "$(dirname "$in")" && pwd)":/in:ro alpine \
      sh -c "rm -rf /v/* /v/.[!.]* 2>/dev/null; tar xzf /in/$(basename "$in") -C /v"
    echo "Imported into $VOL. Now: docker compose up -d --build"
    ;;
  *) sed -n 2,12p "$0"; exit 2 ;;
esac
