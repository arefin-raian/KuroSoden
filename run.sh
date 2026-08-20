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

# --- ensure a 7-Zip binary is available (DDL rar/7z extraction) -------------
# DDL archive links unpack .rar/.7z via the 7-Zip CLI (stdlib handles .zip with
# no external tooling). Best-effort, mirroring the Chromium block: skip silently
# when 7-Zip is already present, otherwise try the system package manager and
# fall back to a static `7zz` dropped into tools/ (no root needed). Non-fatal —
# the bots start regardless; only rar/7z DDL links need it.
if command -v 7z >/dev/null 2>&1 || command -v 7zz >/dev/null 2>&1 \
    || command -v 7za >/dev/null 2>&1 || [ -x "tools/7zz" ]; then
    echo "[Kuro Sōden] 7-Zip already available."
else
    echo "[Kuro Sōden] Installing 7-Zip (for DDL rar/7z extraction) ..."
    sevenzip_ok=""
    case "$(uname -s)" in
        Linux)
            if command -v apt-get >/dev/null 2>&1 && sudo apt-get install -y p7zip-full >/dev/null 2>&1; then
                sevenzip_ok=1
            elif command -v dnf >/dev/null 2>&1 && sudo dnf install -y p7zip >/dev/null 2>&1; then
                sevenzip_ok=1
            elif command -v pacman >/dev/null 2>&1 && sudo pacman -S --noconfirm p7zip >/dev/null 2>&1; then
                sevenzip_ok=1
            fi
            # Fallback: static 7zz into tools/ (no root). Only kept if extraction works.
            if [ -z "$sevenzip_ok" ] && command -v curl >/dev/null 2>&1; then
                mkdir -p tools
                if curl -fsSL "https://www.7-zip.org/a/7z2408-linux-x64.tar.xz" -o /tmp/kuro-7z.tar.xz \
                    && tar -xf /tmp/kuro-7z.tar.xz -C tools 7zz 2>/dev/null; then
                    chmod +x tools/7zz && sevenzip_ok=1
                fi
                rm -f /tmp/kuro-7z.tar.xz
            fi
            ;;
        Darwin)
            if command -v brew >/dev/null 2>&1 && brew install p7zip >/dev/null 2>&1; then
                sevenzip_ok=1
            fi
            ;;
    esac
    if [ -z "$sevenzip_ok" ]; then
        echo "[Kuro Sōden] NOTE: couldn't auto-install 7-Zip. DDL .rar/.7z links need it"
        echo "[Kuro Sōden]       (.zip works without it). Install manually, e.g.:"
        echo "[Kuro Sōden]           sudo apt install p7zip-full   # or: brew install p7zip"
    fi
fi

# --- sanity: secrets file -----------------------------------
if [ ! -f ".env" ]; then
    echo "[Kuro Sōden] WARNING: .env not found."
    echo "[Kuro Sōden] Copy .env.example to .env and fill in your tokens:"
    echo "[Kuro Sōden]     cp .env.example .env"
    echo ""
fi

# --- optional: self-hosted local Jikan (jikan-rest) ---------
# The public api.jikan.moe 502/504s per-resource under load. On a Docker-capable
# VPS, set JIKAN_SELF_HOST=1 in .env to auto-deploy a local jikan-rest stack and
# JIKAN_FALLBACK_URL=http://localhost:8080/v4 so the metadata client fails over
# to it. Best-effort + non-fatal (mirrors the Playwright/7-Zip blocks): a failure
# never blocks the bots — they run on the public API and use the local one once warm.
if [ -f ".env" ] && grep -qE '^[[:space:]]*JIKAN_SELF_HOST[[:space:]]*=[[:space:]]*1[[:space:]]*$' .env; then
    echo "[Kuro Sōden] JIKAN_SELF_HOST=1 — bringing up local jikan-rest (best-effort) ..."
    bash scripts/jikan_local.sh || echo "[Kuro Sōden] NOTE: local Jikan setup skipped; using public API."
fi

# --- prefetch metadata datasets (Kaggle + LeoRigasaki) ------
# The metadata chain (AniList → Kaggle → LeoRigasaki → Jikan → Kitsu) reads two
# on-disk CSV datasets that otherwise lazy-download in the BACKGROUND on first
# use (missing until ready). Warm them into <storage>/cache up front so the chain
# is armed the moment the bots start. Idempotent (skips when current) and
# non-fatal — the bots start regardless; the tiers just fill in once ready.
# The Kaggle set is ~257 MB, so the FIRST run can take a few minutes.
echo "[Kuro Sōden] Prefetching metadata datasets (first run may take a few minutes) ..."
"$VENV_PY" scripts/prefetch_datasets.py \
    || echo "[Kuro Sōden] NOTE: dataset prefetch skipped — the tiers self-fill at runtime."

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
