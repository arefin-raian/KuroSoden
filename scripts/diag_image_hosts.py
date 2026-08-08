"""Diagnostic: upload a random image to every configured backup host, live.

Runs the REAL pipeline functions from ``kurosoden.shared.image_backup`` against
a freshly generated random image and verifies each returned URL by re-downloading
it. Use this from anywhere:

    ./.venv/Scripts/python.exe scripts/diag_image_hosts.py

Expected on a healthy box:

    imgbb     : skipped (no IMGBB_API_KEY)          ← needs the env key to run
    catbox    : https://files.catbox.moe/....       ← verified 200
    telegraph : https://kappa.lol/....               ← verified 200 (kappa.lol fills
                                                       the telegraph slot; telegra.ph
                                                       /upload is dead)

Any host that 404s on retrieval is reported as FAIL. Nothing is persisted — the
script only proves the chain works.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Allow running directly from a checkout (``python scripts/diag_image_hosts.py``).
# ``kurosoden`` is a SYNTHETIC namespace forged by main.py / conftest.py (the
# repo is flat-layout; imports just carry the prefix), so replicate that shim
# here exactly like conftest does before importing anything.
_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
if "kurosoden" not in sys.modules:
    import types as _types

    _kage = _types.ModuleType("kurosoden")
    _kage.__path__ = [str(_HERE)]
    _init = _HERE / "__init__.py"
    if _init.is_file():
        _kage.__file__ = str(_init)
        exec(compile(_init.read_text(encoding="utf-8"), str(_init), "exec"),
             _kage.__dict__)
    sys.modules["kurosoden"] = _kage
for _sub in ("shared", "bots", "nekofetch", "tests"):
    _name = f"kurosoden.{_sub}"
    if _name not in sys.modules and (_HERE / _sub / "__init__.py").is_file():
        _shim = _types.ModuleType(_name)
        _shim.__path__ = [str(_HERE / _sub)]
        sys.modules[_name] = _shim

import httpx

from kurosoden.shared.image_backup import (
    backup_bytes,
    _upload_catbox,
    _upload_imgbb,
    _upload_telegraph,
)

_GENERATED = None


def _random_image() -> bytes:
    """A deterministic-ish random 640x360 JPEG + webp so every host gets real bytes."""
    global _GENERATED
    if _GENERATED is not None:
        return _GENERATED
    from PIL import Image

    img = Image.new("RGB", (640, 360))
    px = img.load()
    for y in range(0, 360, 8):
        for x in range(0, 640, 8):
            px[x, y] = ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    _GENERATED = buf.getvalue()
    return _GENERATED


def _container() -> SimpleNamespace:
    class _Env:
        imgbb_api_key = os.environ.get("IMGBB_API_KEY", "")

    class _ThumbCfg:
        telegraph_access_token = os.environ.get("TELEGRAPH_ACCESS_TOKEN", "")

    class _BotCfg:
        image_host_order = None

    class _Cfg:
        bot = _BotCfg()
        thumbnail_channel = _ThumbCfg()

    return SimpleNamespace(env=_Env(), config=_Cfg())


async def _verify(url: str) -> bool:
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as cli:
            r = await cli.get(url)
            return r.status_code == 200 and len(r.content) > 0
    except Exception as exc:  # noqa: BLE001
        print(f"      retrieval error: {exc}")
        return False


async def main() -> int:
    blob = _random_image()
    print(f"random image: {len(blob)} bytes JPEG\n")
    c = _container()
    failed = 0

    async def _report(name: str, url: str | None):
        nonlocal failed
        if not url:
            print(f"  FAIL {name:11} : no URL returned")
            failed += 1
            return
        ok = await _verify(url)
        print(f"  {'OK  ' if ok else 'FAIL'} {name:11} : {url}  (retrieval {'200' if ok else 'FAIL'})")
        if not ok:
            failed += 1

    print("individual hosts (real pipeline functions):")
    await _report("imgbb", await _upload_imgbb(c, blob))
    await _report("catbox", await _upload_catbox(blob, "image/jpeg", ".jpg", ""))
    await _report("telegraph", await _upload_telegraph(c, blob, "image/jpeg", ""))

    print("\nfull chain (backup_bytes, honors bot.image_host_order):")
    result = await backup_bytes(c, blob, mime="image/jpeg", source_url="")
    await _report("primary", result.primary)

    print()
    if failed:
        print(f"RESULT: {failed} host(s) failed - see above. IMGBB needs IMGBB_API_KEY.")
        return 1
    print("RESULT: every host accepted the upload and serves it back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
