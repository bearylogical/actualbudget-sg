# Build and run the stack. Each step runs in order and stops at the first failure.
#
#   make           = make up
#   make up        check versions → build → start (waits for healthchecks) → load budget → health
#   make bridge    the same, for actual-bridge only (e.g. after upgrading Actual)
#   make check     compare the Actual server's version with the bridge's @actual-app/api
#   make health    show service health
#   make down      stop everything
#
# The bridge build installs the @actual-app/api that matches $ACTUAL_SERVER_URL/info
# (see actual-bridge/Dockerfile); `check` makes sure ACTUAL_SERVER_URL is in .env first.

.NOTPARALLEL:
.PHONY: up bridge check build build-bridge start start-bridge load health down

COMPOSE ?= docker compose
BACKEND ?= http://localhost:8000

up: check build start load health

bridge: check build-bridge start-bridge load health

check:
	@scripts/actual-version.sh

build:
	$(COMPOSE) build

build-bridge:
	$(COMPOSE) build actual-bridge

start:
	$(COMPOSE) up -d --wait

start-bridge:
	$(COMPOSE) up -d --wait actual-bridge

# A fresh bridge has no budget; any budget call makes the backend reload the saved one
load:
	@curl -fsS -m 90 -o /dev/null $(BACKEND)/actual/accounts \
		&& echo "Budget loaded" \
		|| echo "Budget not loaded — load it once in the web UI (http://localhost:3000)"

health:
	@curl -fsS -m 30 $(BACKEND)/health | python3 -c 'import json,sys; d=json.load(sys.stdin); \
		print("overall:", d["status"]); \
		[print(" ", k.ljust(14), v.get("status"), "", v.get("detail", "")) for k, v in d["components"].items()]'

down:
	$(COMPOSE) down
