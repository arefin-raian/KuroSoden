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
    if re.search(_B + r"(ncop|nced|opening|ending|preview|menu|pv)" + _E, low):
        kind = "extra"
    elif re.search(_B + r"ova" + _E, low):
        kind = "ova"
    elif re.search(_B + r"(special|specials|sp\d+|extra|oad)" + _E, low):
        kind = "special"
    elif re.search(_B + r"movie" + _E, low):
        kind = "movie"

    # season — try the most explicit forms first; default 1 if only episodes given
    season = 1
    ms = (re.search(r"\bs(\d{1,2})\s*e\s*\d", low)        # S1E1 / S01 E01 / S1 E 1
          or re.search(r"\bseason\s*(\d{1,2})\b", low)     # Season 1
          or re.search(r"\bs(\d{1,2})\b", low))            # S2
    if ms:
        season = int(ms.group(1))

    # episode number — ordered most-specific → least, stop at first hit.
    # Anchored on the STABLE 'E<num>' / 'episode <num>' / separator-number forms
    # rather than any audio/quality keyword (Dual/Sub/Multi vary, these don't).
    episode = None
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

    return {"kind": kind, "season": season, "episode": episode,
            "resolution": res, "base": base}


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


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
                 "number": number, "kind": e["kind"], "files": []}
            groups[key] = g
        g["files"].append({
            "index": e.get("index"),
            "path": e.get("path"),
            "name": e["name"],
            "length": e.get("length"),
            "resolution": e.get("resolution"),
        })
    for g in groups.values():
        g["files"].sort(
            key=lambda f: _resolution_rank(f.get("resolution")), reverse=True
        )
    return list(groups.values())
