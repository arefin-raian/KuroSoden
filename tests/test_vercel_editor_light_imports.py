"""Guard: the mapping-editor request path must stay import-light.

The Vercel deployment installs only fastapi + redis + structlog (see
requirements-vercel.txt) and imports the real editor code from ``nekofetch``. If
someone adds a top-level import of the heavy bot graph (``nekofetch.core.container``
pulls the source registry / DB / Pyrogram) to any module on the editor's request
path, the Vercel bundle silently balloons or fails to install. This test locks
the boundary by importing that whole path in a FRESH interpreter and asserting
the heavy modules never load.

Run in a subprocess because the main test session imports Pyrogram/container via
other tests, which would pollute ``sys.modules``.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = r"""
import sys
# The exact modules the Vercel entrypoint + editor request path pull in:
import nekofetch.web.app                     # build_app + auth
import nekofetch.web.mapping_session         # session load/save + apply_layout
import nekofetch.services.torrent_mapping    # what apply_layout imports to rebuild
import nekofetch.services.franchise_flow     # dataclasses used by the mapping
import nekofetch.bots.fsm                    # torrent commit writeback

heavy = [m for m in ("nekofetch.core.container", "pyrogram", "sqlalchemy",
                     "asyncpg", "motor", "PIL") if m in sys.modules]
assert not heavy, "editor path pulled heavy modules: " + repr(heavy)
print("OK")
"""


def test_editor_request_path_does_not_import_heavy_stack():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"import-isolation probe failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    )
    assert "OK" in proc.stdout
