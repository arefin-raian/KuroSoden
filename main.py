"""Kuro Sōden (黒送伝) — The Dark Relay Pipeline entry point.

Boots the NekoFetch container (shared DB/cache/config), then starts all four
pipeline bots on a single event loop:

    Lelouch Vi Britannia  —  Request Bot   (request intake, dedup, admin assignment)
    Levi Ackerman         —  Downloader Bot (source selection, download, processing)
    Senku Ishigami        —  Distribution Bot (channel creation, content generation)
    Gojo Satoru           —  Publisher Bot  (main channel, index, recovery)

Kuro Sōden is a STANDALONE repository — NekoFetch's source is vendored under
kurosoden/nekofetch/ so no external imports are needed.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

# Standalone: the project has a FLAT layout — ``docs/``, ``bots/``,
# ``shared/``, ``nekofetch/``, ``tests/`` all live at the repo root. Python
# imports use the prefix ``kurosoden.<sub>`` (legacy of when this was a sub-folded
# repo called ``kurosoden/`` inside ``NekoFetch/``). The handoff below registers a
# synthetic ``kurosoden`` namespace whose subpackages map back to the real dirs
# via ``__path__`` shims — so ``from kurosoden.shared.X import Y`` resolves to
# ``./shared/X.py`` regardless of where the project is unpacked (parented
# locally, or at ``/app/`` on Render / Railway).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                  # /app/ — picks up top-level
                                                 # packages like ``nekofetch``
os.chdir(str(_HERE))

# ── ``kurosoden`` namespace alias ─────────────────────────────────────────────────
# Register ``kurosoden`` and its top-level subpackages as lightweight ``ModuleType``
# shims whose ``__path__`` points at the real directories. Once these entries
# are in ``sys.modules``, Python's normal importer resolves
# ``kurosoden.<sub>.<mod>`` by searching ``__path__`` exactly as it would for any
# regular package — no more fragile parent-directory sys.path manipulation.
#
# Caveat (theoretical, not active here): if any code ever does BOTH
# ``from shared.X import ...`` and ``from kurosoden.shared.X import ...``, Python
# will cache them as two distinct module objects. The kurosoden codebase uniformly
# uses the ``kurosoden.`` prefix, so this is inert. If that ever changes, switch to
# git-tracked symlinks or rename the project root to a ``kurosoden/`` sub-folder.
import types as _types
_kage = _types.ModuleType("kurosoden")
_kage.__path__ = [str(_HERE)]
sys.modules["kurosoden"] = _kage
for _sub in ("shared", "bots", "nekofetch", "tests"):
    if (_HERE / _sub / "__init__.py").is_file():
        _shim = _types.ModuleType(f"kurosoden.{_sub}")
        _shim.__path__ = [str(_HERE / _sub)]
        sys.modules[f"kurosoden.{_sub}"] = _shim
# ────────────────────────────────────────────────────────────────────────────


async def _run() -> None:
    from nekofetch.core.config import get_env, get_app_config
    from nekofetch.core.logging import configure_logging, get_logger
    from nekofetch.core.container import Container

    env = get_env()
    configure_logging(level=env.log_level, json=env.log_json)
    log = get_logger("kurosoden")

    # Upload/download-speed diagnostics: confirm the fast paths are actually
    # active. If fast_crypto is False, Pyrogram is doing pure-Python AES — the
    # single biggest upload bottleneck — so the tgcrypto extension isn't being
    # picked up and should be reinstalled.
    def _fast_crypto() -> bool:
        for mod in ("tgcrypto", "tgcrypto_pyrofork"):
            try:
                __import__(mod)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    _uvloop_active = "uvloop" in str(type(asyncio.get_event_loop_policy())).lower()
    log.info("kurosoden.speedups", fast_crypto=_fast_crypto(),
             uvloop=_uvloop_active,
             upload_concurrency=int(getattr(env, "upload_concurrency", 0) or 8))

    container = Container.create()
    await container.startup()

    # Register Kage's ORM models so ``Base.metadata.create_all()`` and
    # Alembic pick up ``admin_assignments`` + ``admin_availability``.
    import kurosoden.shared.models  # noqa: F401

    # Build stamp for restart verification.
    import subprocess as _sp

    def _build_id() -> str:
        try:
            out = _sp.run(
                ["git", "-C", str(_HERE), "log", "-1", "--format=%h %cd",
                 "--date=format:%Y-%m-%d %H:%M"],
                capture_output=True, text=True, timeout=5,
            )
            return out.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    from nekofetch import __version__ as _ver

    build = _build_id()
    log.info("kuro-soden.starting", version=_ver, build=build)
    print(f"\n  Kuro Sōden {_ver}  ·  build {build}  ·  4-bot pipeline\n", flush=True)

    # ── Pipeline manager ──────────────────────────────────────────────────────
    from kurosoden.shared.pipeline_manager import PipelineManager

    manager = PipelineManager(container)
    stop = asyncio.Event()

    def _signal_handler() -> None:
        log.info("kuro-soden.stopping")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:  # Windows
            pass

    editor_server = None
    editor_task = None
    try:
        await manager.start()
        # Mapping-editor web server (Telegram Mini App) — only when a public HTTPS
        # base URL is configured. Runs on the SAME event loop as the bots via a
        # uvicorn Server task, so it shares Redis/Postgres and stops with them.
        if getattr(container.env, "mapping_editor_base_url", ""):
            try:
                import uvicorn

                from nekofetch.web.app import build_app

                cfg = uvicorn.Config(
                    build_app(container), host="0.0.0.0",
                    port=int(container.env.mapping_editor_port),
                    log_level="warning", access_log=False,
                )
                editor_server = uvicorn.Server(cfg)
                editor_server.install_signal_handlers = lambda: None  # we own signals
                editor_task = asyncio.create_task(editor_server.serve())
                log.info("mapping_editor.serving",
                         port=int(container.env.mapping_editor_port),
                         base_url=container.env.mapping_editor_base_url)
            except Exception as exc:  # noqa: BLE001 — editor is optional; never block bots
                log.warning("mapping_editor.start_failed", error=str(exc))
        await stop.wait()
    finally:
        if editor_server is not None:
            editor_server.should_exit = True
            if editor_task is not None:
                try:
                    await asyncio.wait_for(editor_task, timeout=5)
                except Exception:  # noqa: BLE001 — best-effort shutdown
                    editor_task.cancel()
        await manager.stop()
        await container.shutdown()


def main() -> None:
    # uvloop is a drop-in asyncio replacement (libuv/Cython) that makes the loop
    # 2–4× faster — a direct win for Pyrogram's parallel upload/download chunking,
    # which is loop-bound. Best-effort: absent (e.g. on Windows) → stock asyncio.
    # MUST run before asyncio.run()/Client creation.
    try:
        import uvloop  # type: ignore

        uvloop.install()
    except Exception:  # noqa: BLE001 — no uvloop (Windows/unsupported) → stock loop
        pass
    asyncio.run(_run())


if __name__ == "__main__":
    main()
