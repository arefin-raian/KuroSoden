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
) -> tuple[dict[int, int], dict[int, int], set[int]]:
    """Editor layout → ``(season_overrides, episode_overrides, junk_indices)``.

    ``layout`` = ``{"files": [{"index": int, "season": int, "position": int}, …],
    "excluded": [int, …]}``. ``position`` is the 1-based order the admin dragged
    the file into WITHIN its season; it becomes the episode number so drag order
    is authoritative. Excluded files become junk (dropped from the episode
    stream). Robust to missing/duplicate positions: within each season the
    included files are ordered by their given ``position`` (stable) and
    RE-NUMBERED 1..N so gaps/dupes never produce colliding episode numbers."""
    excluded = {int(i) for i in (layout.get("excluded") or [])}
    files = [f for f in (layout.get("files") or [])
             if int(f.get("index")) not in excluded]

    # Group included files by season, order by the admin's drag position.
    by_season: dict[int, list[dict]] = {}
    for f in files:
        by_season.setdefault(int(f.get("season", 1)), []).append(f)

    season_overrides: dict[int, int] = {}
    episode_overrides: dict[int, int] = {}
    for season, group in by_season.items():
        group.sort(key=lambda f: (int(f.get("position", 0)), int(f.get("index", 0))))
        for ep, f in enumerate(group, start=1):
            idx = int(f["index"])
            season_overrides[idx] = season
            episode_overrides[idx] = ep
    return season_overrides, episode_overrides, excluded


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

    season_ov, episode_ov, junk = layout_to_overrides(layout)
    mapping = build_torrent_mapping(
        ordered, franchise,
        episode_titles=ep_titles or None,
        season_overrides=season_ov or None,
        episode_overrides=episode_ov or None,
        junk_indices=junk or None,
    )
    return mapping.to_dict()


def build_editor_payload(sess: MappingSession) -> dict:
    """The JSON the editor page consumes: per-file rows (with episode titles +
    current season/order) grouped for display, plus the franchise's season list.

    Read-only shape derived from the working set; the editor renders it and POSTs
    back a :func:`layout_to_overrides`-shaped layout."""
    ws = sess.working_set
    ordered = ws.get("ordered_files") or []
    # Episode titles keyed by anilist_id → flatten to a per-(season,number) lookup
    # is overkill for the picker; the editor just needs the file's own name +
    # current season/episode. Titles are surfaced best-effort by episode number.
    title_by_num: dict[int, str] = {}
    for _aid, eps in (ws.get("episode_titles") or {}).items():
        for e in eps or []:
            try:
                title_by_num.setdefault(int(e.get("number")), e.get("title") or "")
            except (TypeError, ValueError):
                continue

    files = []
    for f in ordered:
        idx = int(f.get("index"))
        num = f.get("episode")
        files.append({
            "index": idx,
            "name": f.get("name") or "",
            "season": int(f.get("season", 1) or 1),
            "episode": num,
            "ep_title": title_by_num.get(int(num)) if num else "",
            "kind": f.get("kind", "episode"),
            "resolutions": f.get("resolutions") or [],
        })

    # Season list from the stored mapping (season entries + their expected counts).
    seasons: list[dict] = []
    for e in (ws.get("mapping") or {}).get("entries", []):
        if e.get("kind") == "season":
            seasons.append({
                "season_number": e.get("season_number"),
                "season_part": e.get("season_part"),
                "expected": e.get("episodes"),
                "title": e.get("title") or "",
            })
    return {
        "title": ws.get("title") or (sess.release.get("code") or "Mapping"),
        "files": files,
        "seasons": seasons,
    }
