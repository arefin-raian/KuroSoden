#!/usr/bin/env bash
# ============================================================
#  Kuro Sōden — local Jikan (jikan-rest) deployer
#  Best-effort: brings up a self-hosted jikan-rest stack via
#  docker compose so the metadata chain can fail over to it
#  when the public api.jikan.moe 502/504s.
#
#  Invoked by run.sh only when JIKAN_SELF_HOST=1. Safe to run
#  standalone:  bash scripts/jikan_local.sh
#
#  NON-FATAL BY DESIGN: any failure (no Docker, clone error,
#  slow start) prints a NOTE and exits 0 — the bots still run
#  on the public API and pick up the local instance once warm.
# ============================================================
set -uo pipefail   # NOT -e: this script must never abort run.sh

cd "$(dirname "$0")/.."   # repo root

JIKAN_DIR="tools/jikan-rest"
JIKAN_REPO="https://github.com/jikan-me/jikan-rest"
HEALTH_URL="http://localhost:8080/v4/anime/1"

note() { echo "[Kuro Sōden][jikan] $*"; }

# --- 1. Require docker + a compose command ------------------
if ! command -v docker >/dev/null 2>&1; then
    note "Docker not found — skipping local Jikan. Install Docker to self-host,"
    note "or leave JIKAN_FALLBACK_URL empty to use the public API only."
    exit 0
fi
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    note "No 'docker compose' / 'docker-compose' — skipping local Jikan."
    exit 0
fi

# --- 2. Clone jikan-rest (their tested compose + config) ----
if [ ! -d "$JIKAN_DIR/.git" ]; then
    note "Cloning jikan-rest into $JIKAN_DIR ..."
    if ! git clone --depth 1 "$JIKAN_REPO" "$JIKAN_DIR" >/dev/null 2>&1; then
        note "Clone failed (network?) — skipping local Jikan for now."
        exit 0
    fi
fi

if [ ! -f "$JIKAN_DIR/docker-compose.yml" ]; then
    note "No docker-compose.yml in $JIKAN_DIR — upstream layout changed; skipping."
    exit 0
fi

# --- 3. Generate the 6 secret files the compose expects -----
# Paths are compose-file-relative (they live beside docker-compose.yml).
_rand() { openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
_secret() {
    # $1 = filename, $2 = value (only written if the file is missing)
    if [ ! -f "$JIKAN_DIR/$1" ]; then
        printf '%s' "$2" > "$JIKAN_DIR/$1"
        chmod 600 "$JIKAN_DIR/$1" 2>/dev/null || true
    fi
}
_secret db_username        "jikan"
_secret db_password        "$(_rand)"
_secret db_admin_username  "admin"
_secret db_admin_password  "$(_rand)"
_secret redis_password     "$(_rand)"
_secret typesense_api_key  "$(_rand)"

# --- 4. Bring the stack up (detached, idempotent) -----------
note "Starting jikan-rest stack (app + mongodb + redis + typesense) ..."
if ! ( cd "$JIKAN_DIR" && $COMPOSE up -d ) >/dev/null 2>&1; then
    note "docker compose up failed — check 'cd $JIKAN_DIR && $COMPOSE logs'."
    note "The bots will still run on the public API."
    exit 0
fi

# --- 5. Best-effort readiness poll (don't block the pipeline) -
# First run indexes Typesense and can take minutes; we only wait ~90s then
# return regardless — the local instance is a FALLBACK, not a hard dependency.
note "Waiting for local Jikan to answer at $HEALTH_URL (up to ~90s) ..."
ready=""
for _ in $(seq 1 30); do
    if command -v curl >/dev/null 2>&1; then
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "$HEALTH_URL" 2>/dev/null || echo 000)"
        [ "$code" = "200" ] && { ready=1; break; }
    else
        # No curl — assume the compose 'up' is enough; don't block.
        ready=1; break
    fi
    sleep 3
done
if [ -n "$ready" ]; then
    note "Local Jikan is up. Set JIKAN_FALLBACK_URL=http://localhost:8080/v4 in .env"
    note "(and optionally JIKAN_BASE_URL to prefer it) to route through it."
else
    note "Local Jikan not ready yet — it keeps warming in the background."
    note "First run indexes the search DB; re-check with: curl $HEALTH_URL"
fi
exit 0
