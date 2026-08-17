"""Shared quality-tier gap-fill logic.

ONE source of truth for "which lower tiers must we encode?", used by BOTH:

* :class:`nekofetch.services.processing.stages.EncodeStage` — the actual encoder
  that derives missing renditions, and
* the DDL/torrent franchise-mapping card — which must PREVIEW the same decision
  ("Movie — 1080p present · encode 720p, 480p") so the card and the encoder can
  never drift.

The rule (the owner's "reversal" contract): a source ships some set of
resolutions; we derive only the ``encode_heights`` tiers that are strictly BELOW
the source height (never up-encode) and not already satisfied — where an
acceptable substitute (``resolution_fallbacks``: a 540p/360p file fills the 480p
slot) counts as satisfied so we don't duplicate the SD tier.

All functions are pure (config values passed in, not read) so they unit-test
without a container and can't drift from a live config object.
"""

from __future__ import annotations


def res_height(resolution: str | None) -> int:
    """Height in px from a resolution token (``"1080p"`` → 1080), 0 if unknown."""
    r = (resolution or "").rstrip("p").strip()
    return int(r) if r.isdigit() else 0


def parse_fallbacks(fallbacks_cfg: dict | None) -> dict[int, set[int]]:
    """Normalise a ``resolution_fallbacks`` config dict to ``{height: {alt…}}``.

    ``{"480p": ["540p", "360p"]}`` → ``{480: {540, 360}}``. Tolerant of missing
    or malformed entries (they're skipped), so an older config still works.
    """
    subs: dict[int, set[int]] = {}
    for tgt, alts in (fallbacks_cfg or {}).items():
        th = str(tgt).rstrip("p")
        if not th.isdigit():
            continue
        subs[int(th)] = {
            int(str(a).rstrip("p")) for a in (alts or [])
            if str(a).rstrip("p").isdigit()
        }
    return subs


def tier_satisfied(present: set[int], target_h: int,
                   subs: dict[int, set[int]] | None = None) -> bool:
    """True when ``target_h`` is already covered by the ``present`` heights.

    Covered = the exact height is present OR an acceptable substitute for that
    slot shipped instead (``subs`` from :func:`parse_fallbacks`). Mirrors
    ``EncodeStage``'s ``_tier_satisfied`` exactly.
    """
    if target_h in present:
        return True
    return any(s in present for s in (subs or {}).get(target_h, set()))


def tiers_to_encode(present: set[int], encode_heights, fallbacks_cfg: dict | None,
                    ) -> list[int]:
    """The lower tiers that WOULD be derived for a unit with ``present`` heights.

    Returns the subset of ``encode_heights`` that is strictly below the highest
    present height (no up-encode) and not already satisfied (exact or substitute).
    Empty when the unit already ships every requested tier — the "encode nothing"
    case. The result is the exact set ``EncodeStage`` would produce for the same
    inputs, so a mapping card built from it matches what actually happens.
    """
    present = {h for h in present if h > 0}
    if not present:
        return []
    max_h = max(present)
    subs = parse_fallbacks(fallbacks_cfg)
    out: list[int] = []
    for h in encode_heights or []:
        if h <= 0 or h >= max_h:
            continue  # never up-encode; skip the source tier itself
        if tier_satisfied(present, h, subs):
            continue
        out.append(h)
    return out
