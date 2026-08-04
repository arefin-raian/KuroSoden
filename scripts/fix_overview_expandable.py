"""One-off migration: make the main-channel OverView blockquote expandable.

The main-channel post caption lives in three places, in precedence order:

  1. Mongo ``settings/runtime_overrides``  (panel edits — highest priority)
  2. ``config.yaml``  (main_channel.caption_template)
  3. the code default in ``nekofetch/core/config.py``

Changing the code default and config.yaml is not enough when a value is already
persisted in Mongo — ``SettingsService.apply_overrides`` re-applies the stored
override over config.yaml at startup, so the preview keeps showing the old plain
``<blockquote>``. This script rewrites the stored templates in BOTH the
``runtime_overrides`` and the informational ``runtime_defaults`` docs so the
OverView section becomes ``<blockquote expandable>``.

Idempotent: a template already expandable (or absent) is left untouched.

Run from WSL with the Windows venv:

  PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \
      scripts/fix_overview_expandable.py [--yes]
"""

from __future__ import annotations

import argparse
import asyncio

_OLD = "<blockquote><b>‣ OverView :</b>"
_NEW = "<blockquote expandable><b>‣ OverView :</b>"
_DOC_KEYS = ("runtime_overrides", "runtime_defaults")


def _patch(template: str) -> str | None:
    """Return the expandable template, or None when nothing needs changing."""
    if not isinstance(template, str) or _OLD not in template:
        return None
    return template.replace(_OLD, _NEW)


async def main(assume_yes: bool) -> None:
    from nekofetch.core.container import Container

    container = Container.create()
    await container.startup()
    try:
        settings = getattr(container, "collections", None)
        if settings is None or settings.settings is None:
            print("Mongo settings collection unavailable — nothing to migrate.")
            return

        changed = 0
        for key in _DOC_KEYS:
            doc = await settings.settings.find_one({"key": key})
            value = (doc or {}).get("value") or {}
            mc = value.get("main_channel")
            if not isinstance(mc, dict):
                print(f"  {key}: no main_channel block — skipped")
                continue
            patched = _patch(mc.get("caption_template", ""))
            if patched is None:
                print(f"  {key}: caption_template already expandable/absent — skipped")
                continue
            if not assume_yes:
                ans = input(f"Rewrite caption_template in '{key}'? Type 'yes': ")
                if ans.strip().lower() != "yes":
                    print(f"  {key}: aborted by operator")
                    continue
            mc["caption_template"] = patched
            await settings.settings.update_one(
                {"key": key}, {"$set": {"value": value}}, upsert=True
            )
            changed += 1
            print(f"  {key}: caption_template → expandable ✓")

        print(f"\nDone. {changed} document(s) updated.")
        if changed:
            print("Restart the bots (or /reload is not enough — this is config, "
                  "so restart) so apply_overrides re-reads the fixed template.")
        else:
            print("If the preview is still not expandable, config.yaml is now "
                  "authoritative — a restart applies it.")
    finally:
        shutdown = getattr(container, "shutdown", None)
        if shutdown is not None:
            await shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Make main-channel OverView expandable.")
    ap.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    args = ap.parse_args()
    asyncio.run(main(args.yes))
