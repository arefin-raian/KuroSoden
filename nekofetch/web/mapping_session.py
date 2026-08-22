"""Mapping-editor session store + the pure save-transform.

A "mapping session" is the working set the visual editor operates on — the same
data the ``3 S2`` text flow used, addressed by an opaque token so a Web App URL
can carry it. Lives in Redis (short TTL) keyed by token; the token also records
which confirm-gate to release on save (DDL ``ddlmap`` job or a torrent FSM code).

The transform (:func:`layout_to_overrides` + :func:`apply_layout`) converts the
editor's visual layout — included files with a season + drag position, plus an
excluded set — into the ``season_overrides`` / ``episode_overrides`` /
``junk_indices`` that :func:`build_torrent_mapping` already understands. It is a
pure function (no I/O) so it is fully unit-testable.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from nekofetch.core.logging import get_logger
from nekofetch.core.redis_safe import (
    safe_redis_delete, safe_redis_get, safe_redis_set,
)

log = get_logger(__name__)

_SESSION_TTL = 3600  # 1h — an editing session is short-lived
_K_SESSION = "nf:mapedit:{token}"


def _session_key(token: str) -> str:
    return _K_SESSION.format(token=token)


@dataclass
class MappingSession:
    """The editor's working set + how to commit it back.

    ``working_set`` is the DDL/torrent stash ({mapping, ordered_files, franchise,
    episode_titles, encode_heights, fallbacks_cfg}). ``release`` records the gate:
    ``{"kind": "ddlmap", "job_id": N}`` for the DDL worker gate, or
    ``{"kind": "torrent", "code": "REQ-…"}`` for the torrent FSM flow."""
    token: str
    working_set: dict
    release: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"token": self.token, "working_set": self.working_set,
                           "release": self.release})

    @classmethod
    def from_json(cls, raw: str) -> "MappingSession":
        d = json.loads(raw)
        return cls(token=d["token"], working_set=d.get("working_set") or {},
                   release=d.get("release") or {})


async def create_session(redis, working_set: dict, release: dict) -> str:
    """Mint a token for a working set + gate, store it, return the token."""
    token = secrets.token_urlsafe(24)
    sess = MappingSession(token=token, working_set=working_set, release=release)
    await safe_redis_set(redis, _session_key(token), sess.to_json(),
                         ex=_SESSION_TTL, label="mapedit.session.set")
    return token


async def load_session(redis, token: str) -> "MappingSession | None":
    raw = await safe_redis_get(redis, _session_key(token), label="mapedit.session.get")
    if not raw:
        return None
    try:
        return MappingSession.from_json(raw)
    except Exception as exc:  # noqa: BLE001 — corrupt/expired
        log.debug("mapedit.session.parse_failed", token=token[:6], error=str(exc))
        return None


async def save_session(redis, sess: MappingSession) -> None:
    await safe_redis_set(redis, _session_key(sess.token), sess.to_json(),
                         ex=_SESSION_TTL, label="mapedit.session.resave")


async def delete_session(redis, token: str) -> None:
    await safe_redis_delete(redis, _session_key(token), label="mapedit.session.del")


# ── The pure save-transform ────────────────────────────────────────────────────

def layout_to_overrides(
    layout: dict,
) -> tuple[dict[int, int], dict[int, int], dict[int, str], set[int]]:
    """Editor layout → ``(season_overrides, episode_overrides, kind_overrides, junk)``.

    ``layout`` = ``{"files": [{"index", "kind", "season", "position"}, …],
    "excluded": [int, …]}``. Each included file carries the SECTION the admin put
    it in: ``kind`` is ``"episode"`` (a season section) or ``"special"``/
    ``"movie"``/``"ova"`` (a non-season entry). For episode files, ``position`` is
    the 1-based drag order within the season → becomes the episode number (so drag
    order is authoritative), re-numbered 1..N per season so gaps/dupes never
    collide. Non-episode files carry only their forced ``kind``. Excluded files
    become junk. Files left in the "Unsure" bucket are simply absent from both
    lists → no override → they stay unmatched. ``kind`` defaults to ``"episode"``
    for backward-compat with the old season-only layout schema."""
    excluded = {int(i) for i in (layout.get("excluded") or [])}
    files = [f for f in (layout.get("files") or [])
             if int(f.get("index")) not in excluded]

    season_overrides: dict[int, int] = {}
    episode_overrides: dict[int, int] = {}
    kind_overrides: dict[int, str] = {}

    by_season: dict[int, list[dict]] = {}
    for f in files:
        idx = int(f["index"])
        kind = (f.get("kind") or "episode").lower()
        kind_overrides[idx] = kind
        if kind == "episode":
            by_season.setdefault(int(f.get("season", 1)), []).append(f)

    for season, group in by_season.items():
        group.sort(key=lambda f: (int(f.get("position", 0)), int(f.get("index", 0))))
        for ep, f in enumerate(group, start=1):
            idx = int(f["index"])
            season_overrides[idx] = season
            episode_overrides[idx] = ep
    return season_overrides, episode_overrides, kind_overrides, excluded


def apply_layout(working_set: dict, layout: dict) -> dict:
    """Rebuild the mapping from a working set + an editor layout; return its dict.

    Mirrors ``_rebuild_ddl_mapping`` / ``_rebuild_mapping_from_fsm`` (reconstruct
    the franchise from the stored mapping entries — no AniList re-walk) but drives
    it from the visual layout's overrides. Returns ``TorrentMapping.to_dict()``."""
    from nekofetch.services.franchise_flow import FranchiseMapping
    from nekofetch.services.torrent_mapping import TorrentMapping, build_torrent_mapping

    md = working_set.get("mapping")
    ordered = working_set.get("ordered_files")
    if not md or not ordered:
        raise ValueError("working set missing mapping/ordered_files")

    prev = TorrentMapping.from_dict(md)
    franchise = FranchiseMapping(
        anime_doc_id="", root_title="",
        entries=[e.franchise_entry for e in prev.entries],
    )
    # JSON stringifies int keys; coerce episode_titles back.
    raw_titles = working_set.get("episode_titles") or {}
    ep_titles: dict[int, list] = {}
    for k, v in raw_titles.items():
        try:
            ep_titles[int(k)] = v
        except (TypeError, ValueError):
            continue

    season_ov, episode_ov, kind_ov, junk = layout_to_overrides(layout)
    mapping = build_torrent_mapping(
        ordered, franchise,
        episode_titles=ep_titles or None,
        season_overrides=season_ov or None,
        episode_overrides=episode_ov or None,
        kind_overrides=kind_ov or None,
        junk_indices=junk or None,
    )
    return mapping.to_dict()


def build_editor_payload(sess: MappingSession) -> dict:
    """The JSON the editor page consumes.

    Shape:
      * ``sections`` — ONE per franchise entry (every season AND every special/
        movie/OVA), shown even when empty, each with a stable ``section_id``, a
        human ``label``, its target ``kind`` (``"episode"`` for seasons, else the
        entry kind) and ``season_number`` so the editor can POST the assignment.
      * ``files`` — every file with its current ``section_id`` (the entry it
        mapped to, or ``null`` when it's ambiguous/unmatched → the editor's
        "Unsure" bucket) and an ``ambiguous`` flag.

    Read-only shape derived from the working set; the editor renders it and POSTs
    back a :func:`layout_to_overrides`-shaped layout."""
    ws = sess.working_set
    ordered = ws.get("ordered_files") or []
    mapping = ws.get("mapping") or {}

    title_by_num: dict[int, str] = {}
    for _aid, eps in (ws.get("episode_titles") or {}).items():
        for e in eps or []:
            try:
                title_by_num.setdefault(int(e.get("number")), e.get("title") or "")
            except (TypeError, ValueError):
                continue

    # One section per franchise entry (seasons + specials/movies/OVAs), even empty.
    sections: list[dict] = []
    file_section: dict[int, str] = {}       # file_index → section_id it mapped to
    for i, e in enumerate(mapping.get("entries", [])):
        sid = f"e{i}"
        kind = e.get("kind")
        if kind == "season":
            sn = e.get("season_number") or 1
            part = e.get("season_part")
            label = f"Season {int(sn):02d}" + (f" · Part {part}" if part else "")
            target_kind, season_number = "episode", int(sn)
        else:
            label = (kind or "extra").title()
            if e.get("title"):
                label += f" · {str(e['title'])[:34]}"
            target_kind, season_number = (kind or "extra"), int(e.get("season_number") or 0)
        sections.append({
            "section_id": sid, "label": label, "kind": target_kind,
            "season_number": season_number, "season_part": e.get("season_part"),
            "expected": e.get("episodes"),
        })
        for f in e.get("files", []):
            file_section[int(f["file_index"])] = sid

    unmatched_idx = {int(f["file_index"]) for f in mapping.get("unmatched", [])}
    src_by_idx = {int(f.get("index")): f for f in ordered}

    files = []
    for f in ordered:
        idx = int(f.get("index"))
        num = f.get("episode")
        of = src_by_idx.get(idx, {})
        # "Unsure": unmatched, unnumbered, or numbered only by last-resort file
        # order (episode_source == "order") — surface these for the owner to
        # confirm/assign rather than silently trusting a guess.
        ambiguous = (idx in unmatched_idx or num is None
                     or of.get("episode_source") == "order")
        files.append({
            "index": idx,
            "name": f.get("name") or "",
            "kind": f.get("kind", "episode"),
            "episode": num,
            "ep_title": title_by_num.get(int(num)) if num else "",
            "resolutions": f.get("resolutions")
                or ([f["resolution"]] if f.get("resolution") else []),
            "section_id": None if ambiguous else file_section.get(idx),
            "ambiguous": ambiguous,
        })
    return {
        "title": ws.get("title") or (sess.release.get("code") or "Mapping"),
        "sections": sections,
        "files": files,
    }
