"""Guard: the Vercel entrypoint must import using ONLY the packages declared in
requirements-vercel.txt (plus their real dependency closure + the stdlib).

That set is exactly what Vercel installs. If the editor's import path grows a new
third-party need that isn't declared there, the deployed function crashes at
runtime with FUNCTION_INVOCATION_FAILED — invisible to the normal test suite,
which runs in the full venv where everything is installed. This test reproduces
Vercel's environment locally (block every import outside the declared closure,
raising ModuleNotFoundError exactly as a real absence would) so that failure
surfaces here instead of on deploy.

Regression it locks in: ``from fastapi import Request`` pulls
``starlette.formparsers`` → an UNGUARDED ``import python_multipart``, which pip
does not auto-install (it's a fastapi extra). It crashed every request until
``python-multipart`` was added to requirements-vercel.txt.

Runs in a subprocess because the main session imports the heavy stack via other
tests, which would pollute sys.modules.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

_REQ = pathlib.Path(__file__).resolve().parent.parent / "requirements-vercel.txt"

# The probe: build the allowed import-name set = closure of the declared dists
# (via installed metadata) mapped to their top-level import names, then block
# everything else with ModuleNotFoundError and import the whole editor path.
_PROBE = r"""
import re, sys, pathlib, importlib.abc
import importlib.metadata as md

roots = []
for line in pathlib.Path(REQ).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
        roots.append(re.split(r"[<>=!~;\[ ]", line)[0].strip())

def dist_closure(roots):
    seen, stack = set(), [r.lower().replace("_", "-") for r in roots]
    while stack:
        d = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        try:
            reqs = md.requires(d) or []
        except md.PackageNotFoundError:
            continue
        for r in reqs:
            if ";" in r and "extra" in r.split(";", 1)[1]:
                continue  # optional extras aren't installed by a bare `pip install`
            base = re.split(r"[<>=!~;\[ (]", r.strip())[0].strip().lower().replace("_", "-")
            if base:
                stack.append(base)
    return seen

dists = dist_closure(roots)
allow = {"sniffio"}  # anyio's runtime dep; harmless to always allow
for imp, ds in md.packages_distributions().items():
    if any(d.lower().replace("_", "-") in dists for d in ds):
        allow.add(imp)

std = set(getattr(sys, "stdlib_module_names", set()))

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        top = name.split(".")[0]
        if (top in allow or top in std or name.startswith("nekofetch")
                or top in sys.builtin_module_names):
            return None
        raise ModuleNotFoundError(
            f"No module named {top!r} (not in requirements-vercel.txt closure)", name=top)

sys.meta_path.insert(0, Blocker())

import nekofetch.web.vercel_entry                         # GET + auth load path
from nekofetch.web.mapping_session import apply_layout    # noqa: F401  POST rebuild
import nekofetch.services.torrent_mapping                 # noqa: F401
import nekofetch.bots.fsm                                 # noqa: F401  torrent commit
print("OK")
"""


def test_vercel_entrypoint_imports_with_declared_deps_only():
    code = _PROBE.replace("REQ", repr(str(_REQ)))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0 and "OK" in proc.stdout, (
        "The Vercel entrypoint needs a package that is NOT declared in "
        "requirements-vercel.txt (it would crash the deployed function). Add it "
        f"there.\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
