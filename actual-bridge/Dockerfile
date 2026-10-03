FROM node:22-alpine

RUN apk add --no-cache python3 make g++

WORKDIR /app
COPY package.json .
# @actual-app/api must match your Actual server's release, or loads fail with
# out-of-sync-migrations. So the build asks the server: ADD fetches its /info on
# every build and only invalidates the cache when the content (i.e. version) changes,
# so `docker compose up --build` reinstalls exactly when you've upgraded Actual.
#   ACTUAL_INFO_URL     <server>/info, set by docker-compose.yml from ACTUAL_SERVER_URL;
#                       unset → npm's dist-tags, i.e. "latest"
#   ACTUAL_API_VERSION  explicit override (e.g. a nightly server with no npm release)
# Needs Node >= 22 (api 26.x).
ARG ACTUAL_INFO_URL
ARG ACTUAL_API_VERSION
ADD ${ACTUAL_INFO_URL:-https://registry.npmjs.org/-/package/@actual-app/api/dist-tags} /tmp/actual-info.json
RUN set -e; \
    v="${ACTUAL_API_VERSION:-$(node -e 'const j=require("/tmp/actual-info.json"); \
        process.stdout.write(j.build?.version || j.latest || "")')}"; \
    [ -n "$v" ] || { echo "No version in ${ACTUAL_INFO_URL:-npm dist-tags}" >&2; exit 1; }; \
    echo "Installing @actual-app/api@$v"; \
    npm pkg set "dependencies.@actual-app/api=$v"; \
    npm install || { echo "@actual-app/api@$v failed to install. If your server runs a build \
that is not on npm, set ACTUAL_API_VERSION in .env." >&2; exit 1; }

COPY polyfill.js .
COPY index.js dedup.js ./
RUN mkdir -p /data/actual

EXPOSE 3001
CMD ["node", "--import", "./polyfill.js", "index.js"]
