"""Torrent metadata: a minimal bencode decoder + episode ordering.

No third-party torrent library is required to read a ``.torrent``'s file list —
bencode is trivial. We use the file list to map a release's videos to an ordered
EP1..EPN sequence while preserving the original filenames, and to classify
movies / specials / OVAs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nekofetch.core.logging import get_logger

log = get_logger(__name__)

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov")


# --------------------------------------------------------------------------- #
# bencode
# --------------------------------------------------------------------------- #

def bdecode(data: bytes):
    """Decode a bencoded byte string into Python objects."""
    def parse(i: int):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c.isdigit():
            colon = data.index(b":", i)
            n = int(data[i:colon])
            start = colon + 1
            return data[start:start + n], start + n
        if c == b"l":
            i += 1
            out = []
            while data[i:i + 1] != b"e":
                v, i = parse(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            i += 1
            out = {}
            while data[i:i + 1] != b"e":
                k, i = parse(i)
                v, i = parse(i)
                out[k] = v
            return out, i + 1
        raise ValueError(f"invalid bencode at byte {i}")

    value, _ = parse(0)
    return value


def torrent_files(data: bytes) -> tuple[str, list[dict]]:
    """Return (torrent_name, files) from raw .torrent bytes.

    Each file: ``{"path": rel/path, "name": basename, "length": bytes, "index": i}``
    (index is the bencode file order — needed for aria2c ``--select-file``).
    """
    meta = bdecode(data)
    info = meta[b"info"]
    name = info[b"name"].decode("utf-8", "replace")
    files: list[dict] = []
    if b"files" in info:
        for idx, f in enumerate(info[b"files"], start=1):
            parts = [p.decode("utf-8", "replace") for p in f[b"path"]]
            rel = "/".join(parts)
            files.append({"path": f"{name}/{rel}", "name": parts[-1],
                          "length": f[b"length"], "index": idx})
    else:
        files.append({"path": name, "name": name, "length": info[b"length"], "index": 1})
    return name, files


# --------------------------------------------------------------------------- #
# episode ordering
# --------------------------------------------------------------------------- #

_EXT_RE = re.compile(r"\.(mkv|mp4|avi|ts|m4v|mov)$", re.IGNORECASE)
_RES_RE = re.compile(r"(\d{3,4})p", re.IGNORECASE)


def parse_release_meta(name: str) -> dict:
    """Classify one filename: kind, season, episode number, resolution."""
    base = _EXT_RE.sub("", name)
    low = base.lower()

    kind = "episode"
    _B = r"(?:^|[\s_\-\.\[\(])"
    _E = r"(?:[\s_\-\.\]\)]|$)"
    # Non-episode junk: opening/ending creditless, previews, menus, and — the
    # owner's "Season 1 Trailer 1 matched as S01E01" bug — trailers/teasers/promos
    # and bare "NC" (no-credit) markers. Classify these as EXTRA so their trailing
    # numbers ("Trailer 1") are never captured as an episode number.
    if re.search(_B + r"(ncop|nced|nc|opening|ending|preview|menu|pv|trailer|teaser|promo|creditless|clean)" + _E, low):
        kind = "extra"
    elif re.search(_B + r"ova" + _E, low):
        kind = "ova"
    elif re.search(_B + r"(special|specials|sp\d+|extra|oad)" + _E, low):
        kind = "special"
    elif re.search(_B + r"movie" + _E, low):
        kind = "movie"

    # season — try the most explicit forms first; default 1 if only episodes given.
    # ``season_explicit`` records whether the NAME actually stated a season (vs. the
    # season-1 default) so the season-mapping cascade knows when to trust it verbatim
    # instead of falling through to title-match / absolute-numbering resolution.
    season = 1
    ms = (re.search(r"\bs(\d{1,2})\s*e\s*\d", low)        # S1E1 / S01 E01 / S1 E 1
          or re.search(r"\bseason\s*(\d{1,2})\b", low)     # Season 1
          or re.search(r"\bs(\d{1,2})\b", low))            # S2
    season_explicit = ms is not None
    if ms:
        season = int(ms.group(1))

    # episode number — ordered most-specific → least, stop at first hit.
    # Anchored on the STABLE 'E<num>' / 'episode <num>' / separator-number forms
    # rather than any audio/quality keyword (Dual/Sub/Multi vary, these don't).
    #
    # NEW: fractional episodes (13.5, 17.5, S01E13.5) are specials/OVAs that air
    # between main episodes. Detect them BEFORE the integer patterns so ".5" is
    # captured as a distinct marker, not discarded. Episode 00 (when present) is
    # either episode 1 (if the pack runs 00-12) or a special; we detect it here
    # and let the caller decide (torrent_mapping or coverage gate).
    episode = None
    fractional = False  # .5 marker — caller classifies as special/OVA

    # Fractional episodes: 13.5, S01E13.5, - 17.5, etc.
    frac_m = re.search(r"(?:e\s*|episode\s*|[\s\-_])(\d{1,3})\.5\b", low)
    if frac_m:
        episode = int(frac_m.group(1))
        fractional = True
    else:
        # Integer episodes — same patterns as before
        for pat in (
            r"\bs\d{1,2}\s*e\s*(\d{1,3})(?:v\d+)?\b",            # S1E12v3 / S01E01 / S1 E 12
            r"\bseason\s*\d{1,2}\s*episode\s*(\d{1,3})\b",    # Season 1 Episode 1
            r"\bepisode\s*(\d{1,3})\b",                       # Episode 12
            r"\bep\s*[._-]?\s*(\d{1,3})\b",                   # EP01 / Ep.1
            r"(?:^|[\s\-_])e(\d{1,3})\b",                     # - E17 / E17 / _E001
            r"[\s_]-[\s_]*(\d{1,3})(?:v\d+)?[\s_]*[\(\[]",    # - 24 [Dual] / _-_01_(
            r"[\s_]-[\s_]*(\d{1,3})(?:v\d+)?(?=[\s_]|$)",     # - 24 / _-_01_
            r"[\s_](\d{1,3})[\s_]*[\(\[]",                    #  24 (1080p) / _01_(
            r"#(\d{1,3})\b",                                  # #01
        ):
            m = re.search(pat, low)
            if m:
                episode = int(m.group(1))
                break

    res = None
    mr = _RES_RE.search(low)
    if mr:
        res = f"{mr.group(1)}p"

    # A fractional episode (13.5) is a between-cours special/OVA — route it out of
    # the main-episode stream so it lands in a special slot and is only downloaded
    # when the franchise map actually has one. Keep the integer part (13) so the
    # caller can slot it as "after episode 13".
    if fractional and kind == "episode":
        kind = "special"

    return {"kind": kind, "season": season, "episode": episode,
            "season_explicit": season_explicit, "fractional": fractional,
            "resolution": res, "base": base}


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _apply_positional_episodes(main: list[dict]) -> None:
    """Assign episode numbers to the main-episode files, IN PLACE, preferring the
    positional "incrementing column" over the per-file regex.

    The owner's rule: in a real pack the filename format is constant and only the
    episode number climbs +1,+1,+1 — the constant prefix (``Attack on Titan S01``)
    and any code/title on the right do NOT increment. ``analyze_pack`` finds that
    column even when a per-file regex would grab a wrong number or miss an unusual
    style (``01. Title``, ``S01 01``, ``S01 EP01``, ``- 01 [Dual]``). Precedence:

      1. ``analyze_pack`` aligned-column numbers, when that detection is confident
         (a ``template`` was built and the result is non-ambiguous) — authoritative.
      2. otherwise the per-file ``parse_release_meta`` number already on each dict.
      3. last resort — if NOTHING got a number either way, number the files 1..N by
         natural filename order (the owner's "worst case, use the file order").

    Never invents a number for a genuinely unnumbered lone file; only fills when
    the whole main set is otherwise numberless.
    """
    if not main:
        return
    pa = analyze_pack([e["name"] for e in main])
    # (1) Confident aligned column → its per-position numbers are the episodes.
    if pa.template and not pa.ambiguous and pa.episode_numbers:
        for e, num in zip(main, pa.episode_numbers, strict=False):
            if num is not None:
                e["episode"] = num
                e["episode_source"] = "column"
    # (3) Total miss (regex + column both empty) → fall back to file order.
    if all(e.get("episode") is None for e in main):
        for pos, e in enumerate(
            sorted(main, key=lambda x: _natural_key(x["name"])), start=1
        ):
            e["episode"] = pos
            e["episode_source"] = "order"


# --------------------------------------------------------------------------- #
# pack pattern analysis (secondary validation of episode order)
# --------------------------------------------------------------------------- #

@dataclass
class PackAnalysis:
    """Result of differencing a pack's filenames to find the episode segment."""
    episode_numbers: list[int | None]   # per input file, in the given order
    confidence: float                   # 0..1
    ambiguous: bool                     # True → ask admin to confirm
    template: str                       # constant filename template with {EP}
    detail: str


def analyze_pack(names: list[str]) -> PackAnalysis:
    """Find the single varying segment across a pack's filenames = episode number.

    Within one release group the name format is stable; only the episode number
    (and maybe a title) changes. We tokenize each name into alternating
    text/number chunks, align them, and pick the numeric column that varies as a
    near-contiguous increasing sequence. Falls back to per-file parsing when the
    structure isn't uniform. Confidence is low (→ ``ambiguous``) when several
    numeric columns vary or the detected numbers don't form a clean run.
    """
    if not names:
        return PackAnalysis([], 0.0, True, "", "empty pack")

    bases = [_EXT_RE.sub("", n) for n in names]
    toks = [re.findall(r"\d+|\D+", b) for b in bases]   # alternating chunks

    # Aligned analysis only when every name has the same chunk layout.
    if len(names) >= 2 and len({len(t) for t in toks}) == 1:
        width = len(toks[0])
        candidates: list[tuple[int, list[int]]] = []
        for j in range(width):
            col = [t[j] for t in toks]
            if all(c.isdigit() for c in col) and len({*col}) > 1:
                candidates.append((j, [int(c) for c in col]))
        scored = []
        for j, vals in candidates:
            uniq = len(set(vals)) == len(vals)
            srt = sorted(vals)
            contiguous = srt == list(range(srt[0], srt[0] + len(srt)))
            scored.append(((uniq, contiguous, len(set(vals))), j, vals))
        if scored:
            scored.sort(reverse=True)
            (uniq, contiguous, _n), j, vals = scored[0]
            multi = len(candidates) > 1
            if uniq and contiguous:
                conf = 0.95 if not multi else 0.8
            elif uniq:
                conf = 0.6
            else:
                conf = 0.35
            template = "".join("{EP}" if k == j else toks[0][k] for k in range(width))
            return PackAnalysis(
                episode_numbers=vals, confidence=conf,
                ambiguous=conf < 0.75,
                template=template,
                detail=f"aligned column {j}; {len(candidates)} varying numeric column(s)",
            )

    # Fallback: parse each filename independently.
    eps = [parse_release_meta(n)["episode"] for n in names]
    known = [e for e in eps if e is not None]
    uniq = len(set(known)) == len(known)
    conf = 0.6 if (len(known) == len(names) and uniq) else 0.3
    return PackAnalysis(eps, conf, conf < 0.75, "", "per-file parse fallback")


def validate_order(names: list[str]) -> dict:
    """Check whether the given file order matches the detected episode numbers.

    ``names`` are assumed to already be in intended episode order (index 0 = first
    episode). Returns the analysis plus whether the detected numbers increase in
    step with that order (``order_consistent``) and whether to confirm with admin.
    """
    pa = analyze_pack(names)
    nums = pa.episode_numbers
    have = [n for n in nums if n is not None]
    # consistent if the detected episode numbers strictly increase with position
    order_consistent = len(have) >= 2 and all(
        a < b for a, b in zip(have, have[1:], strict=False)
    )
    confirm = pa.ambiguous or not order_consistent or len(have) < len(names)
    return {
        "episode_numbers": nums,
        "confidence": round(pa.confidence, 2),
        "order_consistent": order_consistent,
        "needs_admin_confirmation": confirm,
        "template": pa.template,
        "detail": pa.detail,
    }


def _resolution_rank(res: str | None) -> int:
    """Numeric height for a resolution token ("1080p" → 1080). 0 when unknown."""
    if not res:
        return 0
    digits = "".join(c for c in str(res) if c.isdigit())
    return int(digits) if digits else 0


def _collapse_resolution(main: list[dict], prefer: int) -> tuple[list[dict], list[dict]]:
    """Collapse multi-resolution duplicates of the same (season, episode).

    A multi-quality torrent ships e.g. both a 1080p and a 720p file for episode 1.
    We download ONE quality per episode and derive the rest by encoding, so keep
    a single file per (season, episode): the ``prefer`` height when present, else
    the highest available. Returns ``(kept, low_only)`` where ``low_only`` lists
    the kept files for episodes that had NO ``prefer``-height option (so the
    caller can flag the "only 720p — download anyway?" case). Only numbered
    episodes are collapsed; callers must pass main episodes only.
    """
    from collections import OrderedDict

    groups: "OrderedDict[tuple, list[dict]]" = OrderedDict()
    for e in main:
        groups.setdefault((e["season"], e["episode"]), []).append(e)

    kept: list[dict] = []
    low_only: list[dict] = []
    for _key, files in groups.items():
        if len(files) == 1:
            kept.append(files[0])
            continue
        exact = [f for f in files if _resolution_rank(f.get("resolution")) == prefer]
        if exact:
            chosen = exact[0]
        else:
            chosen = max(files, key=lambda f: _resolution_rank(f.get("resolution")))
            low_only.append(chosen)
        kept.append(chosen)
    return kept, low_only


def _resolve_zero_based(main: list[dict], extras: list[dict]) -> list[dict]:
    """Resolve the episode-00 ambiguity for a main-episode list.

    Two cases when a file numbered ``00`` is present:

    * **Zero-based release** — the numbers form a contiguous run ``0..N`` with no
      gaps (e.g. 00,01,…,12). Here ``00`` IS the first episode, so every episode
      is shifted ``+1`` to land on the franchise's 1-based slots (00→ep1, 12→ep13).
    * **Prologue special** — a ``00`` sits alongside an already-complete ``1..N``
      run. Then ``00`` is episode 0 / a prologue special; it is moved OUT of the
      main stream into ``extras`` (mutated in place) so it only downloads when the
      map has a special slot, and the real episodes keep their numbers.

    Returns the (possibly renumbered / filtered) main list. Files with no detected
    number are left untouched. ``extras`` is appended to in the prologue case.
    """
    numbered = [e for e in main if e.get("episode") is not None]
    if not numbered or all(e["episode"] != 0 for e in numbered):
        return main

    nums = sorted({e["episode"] for e in numbered})
    contiguous_from_zero = nums == list(range(0, len(nums)))

    if contiguous_from_zero:
        # Zero-based: shift every numbered episode +1 (00 → ep1).
        for e in numbered:
            e["episode"] += 1
        return main

    # Otherwise 00 is a prologue special — reclassify and move it to extras.
    remaining: list[dict] = []
    for e in main:
        if e.get("episode") == 0:
            e["kind"] = "special"
            extras.append(e)
        else:
            remaining.append(e)
    return remaining


def order_episodes(files: list[dict], *, prefer_resolution: int | None = None) -> list[dict]:
    """Order a release's video files into an EP1..EPN sequence.

    Returns each kept file augmented with ``seq`` (1-based), ``season``,
    ``episode``, ``kind``, ``resolution`` and the original ``name``/``path``.
    Movies/specials/OVAs/extras are ordered after the main episodes. Original
    filenames are preserved; only the sequence index is synthesised.

    ``prefer_resolution`` (e.g. ``1080``), when set, collapses multi-resolution
    duplicates of the same numbered episode down to a single file — the preferred
    height when present, else the highest available — so a multi-quality torrent
    downloads ONE file per episode (the lower tiers are derived by encoding).
    Movies/extras (no episode number) are never collapsed.
    """
    vids = [f for f in files if f["name"].lower().endswith(VIDEO_EXT)]
    if not vids:
        return []
    enriched = []
    for f in vids:
        m = parse_release_meta(f["name"])
        enriched.append({**f, **m})

    # A lone video file with no detectable episode number is almost always a
    # movie (e.g. "A Silent Voice"), even without the word "movie" in the name.
    if len(enriched) == 1 and enriched[0]["kind"] == "episode" \
            and enriched[0]["episode"] is None:
        enriched[0]["kind"] = "movie"

    main = [e for e in enriched if e["kind"] == "episode"]
    movies = [e for e in enriched if e["kind"] == "movie"]
    extras = [e for e in enriched if e["kind"] in ("special", "ova", "extra")]

    # Episode numbers via the positional incrementing-column detector (falls back
    # to the per-file regex, then file order). Done BEFORE zero-based resolution
    # and the (season, episode) sort so both operate on the reliable numbers.
    _apply_positional_episodes(main)

    # Resolve episode-00 numbering: a release that runs 00..N is zero-based (00 is
    # episode 1) and must be shifted +1 so it maps onto the franchise's 1..N slots.
    # A stray 00 alongside an already-complete 1..N run is a prologue special.
    main = _resolve_zero_based(main, extras)

    # Collapse multi-resolution duplicates for numbered episodes only. Episodes
    # with no detected number (episode is None) are left alone — they'd all share
    # the same (season, None) key and must not be merged into one.
    if prefer_resolution is not None and main:
        numbered = [e for e in main if e["episode"] is not None]
        unnumbered = [e for e in main if e["episode"] is None]
        if numbered:
            kept, low_only = _collapse_resolution(numbered, prefer_resolution)
            if low_only:
                log.warning(
                    "torrent.resolution.no_preferred",
                    prefer=prefer_resolution,
                    episodes=[e.get("episode") for e in low_only],
                    got=[e.get("resolution") for e in low_only],
                    hint="episodes lacking the preferred height — admin confirm "
                         "before downloading a lower tier",
                )
            main = kept + unnumbered

    # If episode numbers were detected for the main set, sort by (season, ep);
    # otherwise fall back to a natural filename sort (stable per release).
    if main and all(e["episode"] is not None for e in main):
        main.sort(key=lambda e: (e["season"], e["episode"]))
    else:
        main.sort(key=lambda e: _natural_key(e["name"]))
    movies.sort(key=lambda e: _natural_key(e["name"]))
    extras.sort(key=lambda e: _natural_key(e["name"]))

    ordered = main + movies + extras
    for seq, e in enumerate(ordered, start=1):
        e["seq"] = seq
    return ordered


def group_variants(ordered: list[dict]) -> list[dict]:
    """Collapse an :func:`order_episodes` list into one entry per logical episode,
    keeping EVERY resolution as a sibling ``file`` (the download-all-qualities
    policy — we download each tier and encode only what's genuinely missing).

    Numbered episodes group by ``(season, episode)`` so a multi-quality release
    (e.g. ep1 in both 1080p and 720p) becomes a SINGLE episode with two files —
    NOT two episodes with a corrupted ``seq``-based number. Movies / specials /
    OVAs / extras (no episode number) stay individual, keyed by their unique
    ``seq`` so distinct extras never merge.

    Each returned dict carries ``season``, ``episode`` (real number or ``None``),
    ``number`` (the episode number, or ``seq`` for unnumbered), ``kind`` and
    ``files`` = ``[{index, path, name, length, resolution}, …]`` ordered
    highest-resolution first (so ``files[0]`` is the natural "primary"/encode
    source and the best single-file representative for callers that want one).
    """
    from collections import OrderedDict

    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for e in ordered:
        if e.get("episode") is not None:
            key = ("ep", e["season"], e["episode"])
            number = e["episode"]
        else:
            key = ("x", e["seq"])  # movies/extras never merge
            number = e["seq"]
        g = groups.get(key)
        if g is None:
            g = {"season": e["season"], "episode": e.get("episode"),
                 "number": number, "kind": e["kind"],
                 # Preserve whether the NAME actually stated a season, so a later
                 # franchise-mapping pass (DDL builds its mapping post-extract from
                 # these groups) can trust an explicit S02 instead of re-deriving
                 # and collapsing it onto S01.
                 "season_explicit": bool(e.get("season_explicit")),
                 "files": []}
            groups[key] = g
        g["files"].append({
            "index": e.get("index"),
            "path": e.get("path"),
            "name": e["name"],
            "length": e.get("length"),
            "resolution": e.get("resolution"),
            # Per-file source attribution for multi-torrent releases (each magnet
            # is a distinct torrent, so file_index/path are only meaningful WITH
            # the magnet they came from). Absent for single-torrent releases.
            **({"magnet": e["magnet"]} if e.get("magnet") else {}),
            **({"info_hash": e["info_hash"]} if e.get("info_hash") else {}),
        })
    for g in groups.values():
        g["files"].sort(
            key=lambda f: _resolution_rank(f.get("resolution")), reverse=True
        )
    return list(groups.values())
