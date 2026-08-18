"""Prefetch the metadata datasets so the resilience chain is armed at boot.

The metadata chain is AniList → Kaggle → LeoRigasaki → Jikan → Kitsu. The two
dataset tiers lazy-download in the BACKGROUND on first use and MISS meanwhile, so
right after a fresh boot they aren't on disk yet and early lookups fall through to
Jikan/Kitsu until the download finishes. This warms them UP FRONT (foreground)
into the SAME cache the bots read (``<STORAGE_PATH>/cache``), so the chain is
fully armed the moment the bots start.

    python scripts/prefetch_datasets.py

Idempotent: a present, non-stale CSV is a no-op. Non-fatal + always exits 0 — a
dataset miss must never block the launcher; the running bot's background refresh
still keeps them current. The big Kaggle set (~257 MB zip → ~438 MB CSV) makes the
first run take a few minutes; later runs skip.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

# UTF-8 stdout so dataset paths / logs never crash a cp1252 Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# ── ``kurosoden`` namespace bootstrap (mirrors requeue_senku.py) ─────────────
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
os.chdir(str(_HERE))

_kage = types.ModuleType("kurosoden")
_kage.__path__ = [str(_HERE)]
sys.modules["kurosoden"] = _kage
for _sub in ("shared", "bots", "nekofetch", "tests"):
    if (_HERE / _sub / "__init__.py").is_file():
        _shim = types.ModuleType(f"kurosoden.{_sub}")
        _shim.__path__ = [str(_HERE / _sub)]
        sys.modules[f"kurosoden.{_sub}"] = _shim
# ──────────────────────────────────────────────────────────────────────────────


def _size_mb(path: Path) -> str:
    try:
        return f"{path.stat().st_size / (1024 * 1024):.0f} MB"
    except OSError:
        return "missing"


async def main() -> int:
    from nekofetch.core.config import get_env
    from nekofetch.sources.telegram.anime_dataset import AnimeDatasetClient
    from nekofetch.sources.telegram.kaggle_dataset import KaggleDatasetClient

    cache = get_env().storage_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    print(f"[prefetch] cache dir: {cache}")

    kaggle = KaggleDatasetClient()
    dataset = AnimeDatasetClient()
    kaggle.set_cache_dir(cache)
    dataset.set_cache_dir(cache)

    # Each dataset is independent + best-effort — one failing never blocks the
    # other or the launcher. The Kaggle set is large; warn so a slow first run
    # doesn't look hung.
    jobs = [
        ("Kaggle AniList (full, w/ relations — up to a few min on first run)",
         kaggle, kaggle._csv_path),
        ("LeoRigasaki AniList (seasonal)", dataset, dataset._csv_path),
    ]
    try:
        for label, client, csv_path in jobs:
            fresh = csv_path.exists()
            print(f"[prefetch] {label}: "
                  + ("already present, checking freshness…" if fresh
                     else "downloading…"))
            try:
                await client.prefetch()
            except Exception as exc:  # noqa: BLE001 — never block boot
                print(f"[prefetch]   SKIPPED ({type(exc).__name__}: {exc})")
                continue
            print(f"[prefetch]   {'ready' if csv_path.exists() else 'unavailable'} "
                  f"({_size_mb(csv_path)})")
    finally:
        for _label, client, _p in jobs:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
    # Exit non-zero when a dataset is still absent, so a MARKER-gated launcher
    # (run.bat) doesn't record "done" and retries next boot. run.sh treats any
    # non-zero as a non-fatal skip and proceeds regardless.
    missing = [label for label, _c, p in jobs if not p.exists()]
    if missing:
        print(f"[prefetch] still missing: {', '.join(missing)} — will retry next boot.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:  # noqa: BLE001 — a prefetch failure must not fail boot
        print(f"[prefetch] aborted ({type(exc).__name__}: {exc}); continuing.")
        raise SystemExit(0)
