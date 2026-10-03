#!/usr/bin/env sh
# Compare your Actual server's version with the bridge's @actual-app/api.
#
#   scripts/actual-version.sh        show both; add ACTUAL_SERVER_URL to .env if missing
#   scripts/actual-version.sh --up   … then rebuild + restart actual-bridge
#
# The bridge build matches the server on its own (see actual-bridge/Dockerfile) as long
# as ACTUAL_SERVER_URL is in .env, so `docker compose up --build` is normally enough.
# This is for checking, and for filling in ACTUAL_SERVER_URL from the connection saved
# in the web UI. An ACTUAL_API_VERSION line in .env overrides the automatic match.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=.env

up=0
case "${1:-}" in
  --up) up=1 ;;
  -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--up]" >&2; exit 2 ;;
esac

env_get() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 | tr -d '"'"'" || true; }

url=$(env_get ACTUAL_SERVER_URL)
if [ -z "$url" ]; then
  url=$(docker exec budget-backend python -c \
    "import connection; print(connection.status()['server_url'] or '')" 2>/dev/null || true)
  if [ -z "$url" ]; then
    echo "No ACTUAL_SERVER_URL in $ENV_FILE and no saved connection — add it to $ENV_FILE." >&2
    exit 1
  fi
  url=${url%/}
  printf 'ACTUAL_SERVER_URL=%s\n' "$url" >> "$ENV_FILE"
  echo "Added ACTUAL_SERVER_URL=$url to $ENV_FILE"
fi
url=${url%/}

server=$(curl -fsS -m 10 "$url/info" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p') || true
bridge=$(docker exec budget-actual-bridge node -e \
  "console.log(require('/app/node_modules/@actual-app/api/package.json').version)" 2>/dev/null || true)
pin=$(env_get ACTUAL_API_VERSION)

echo "Actual server   $url: ${server:-unreachable}"
echo "Bridge api      ${bridge:-not running}"
[ -z "$pin" ] || echo "Override        ACTUAL_API_VERSION=$pin in $ENV_FILE (remove it to follow the server)"

if [ "$up" = 1 ]; then
  docker compose up -d --build actual-bridge
  echo "Bridge rebuilt. The backend reloads the saved budget on the next request."
elif [ -n "$server" ] && [ "$server" != "$bridge" ]; then
  echo "Mismatch — run: make bridge"
fi
