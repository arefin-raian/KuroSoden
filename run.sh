#!/usr/bin/env bash
# ============================================================
#  Kuro Sōden launcher (Linux / macOS)
#  Run:  bash run.sh
#  Creates the venv + installs deps on first run, then boots
#  all four pipeline bots (Lelouch, Levi, Senku, Gojo).
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

VENV_PY=".venv/bin/python3"

# --- ensure a virtual environment exists --------------------
if [ ! -f "$VENV_PY" ]; then
    echo "[Kuro Sōden] No virtual environment found. Creating .venv ..."
    python3.12 -m venv .venv 2>/dev/null || python3 -m venv .venv
    if [ ! -f "$VENV_PY" ]; then
        echo "[Kuro Sōden] ERROR: could not create a virtual environment."
        echo "[Kuro Sōden] Install Python 3.12+ from https://python.org and retry."
        exit 1
    fi
fi

# --- ensure deps are installed ------------------------------
echo "[Kuro Sōden] Syncing dependencies ..."
"$VENV_PY" -m pip install --upgrade pip -q
"$VENV_PY" -m pip install -e .
if [ $? -ne 0 ]; then
    echo "[Kuro Sōden] ERROR: dependency install failed. See the output above."
    exit 1
fi

# --- ensure the Playwright Chromium browser is installed ----
# Thumbnail rendering (Senku) needs a headless Chromium; the pip package alone
# doesn't ship the browser binary. `playwright install` is idempotent — a fast
# no-op once the browser exists, downloading only on first run. Non-fatal so the
# bots still start (thumbnails just won't render until it succeeds).
echo "[Kuro Sōden] Ensuring Playwright Chromium is installed ..."
if ! "$VENV_PY" -m playwright install chromium; then
    echo "[Kuro Sōden] WARNING: 'playwright install chromium' failed — thumbnail"
    echo "[Kuro Sōden]          rendering may not work until it succeeds."
fi
# System libraries Chromium needs (Linux only). Needs root, so best-effort:
# skip silently when it can't run (e.g. macOS, or no sudo).
if [ "$(uname -s)" = "Linux" ]; then
    "$VENV_PY" -m playwright install-deps chromium >/dev/null 2>&1 \
        || sudo "$VENV_PY" -m playwright install-deps chromium >/dev/null 2>&1 \
        || echo "[Kuro Sōden] NOTE: couldn't install Chromium system deps (need root). If thumbnails fail: sudo $VENV_PY -m playwright install-deps chromium"
fi

# --- sanity: secrets file -----------------------------------
if [ ! -f ".env" ]; then
    echo "[Kuro Sōden] WARNING: .env not found."
    echo "[Kuro Sōden] Copy .env.example to .env and fill in your tokens:"
    echo "[Kuro Sōden]     cp .env.example .env"
    echo ""
fi

# --- run ----------------------------------------------------
echo "[Kuro Sōden] Starting 4-bot pipeline..."
echo "[Kuro Sōden]   🎭 Lelouch    🡆 Request intake"
echo "[Kuro Sōden]   🪖 Levi       🡆 Download delegation"
echo "[Kuro Sōden]   🧪 Senku      🡆 Distribution"
echo "[Kuro Sōden]   🔮 Gojo       🡆 Publishing"
echo ""
echo "[Kuro Sōden] Press Ctrl+C to stop."
export PATH="$(pwd)/.venv/bin:$PATH"

exec "$VENV_PY" main.py
