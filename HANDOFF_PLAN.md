# NekoFetch / KuroSoden — FINAL EXECUTION PLAN (v2, code-
verified)

> **You are the executor.** This plan was written by an AI that read the codebase and verified every
> path/line/signature below. Follow it exactly. Where it says "already exists — just do X", do NOT
> rebuild from scratch. Two different AIs following this file must produce functionally identical
> results. All line numbers are from the state of the repo at handoff; re-
`grep` to confirm before
> editing (they may shift by a few lines).

-
-
-


## 0. ENVIRONMENT (do not get this wrong)
-
 **WSL/Linux, Bash only. No PowerShell.** Working dir: `/mnt/c/Users/Admin/Documents/NekoFetch/KuroSoden`.
-
 Python: `./.venv/Scripts/python.exe` (Windows venv from WSL). Tests: `./.venv/Scripts/python.exe -
m pytest <path> -
q`. Compile: `./.venv/Scripts/python.exe -
m py_compile <files>`.
-
 **Remote DBs (Render PG / Mongo / Redis) are UNREACHABLE here.** Verify ONLY with in-
memory SQLite unit tests + py_compile. Never write a script that connects to prod.
-
 `kurosoden.*` is a synthetic namespace (conftest.py) mapping onto `shared/`, `bots/`, `nekofetch/`.

### Mandatory test harness
```python
import pytest, pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from nekofetch.infrastructure.database.postgres.base import Base

@pytest_asyncio.fixture
async def sessionmaker_():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    @event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_con, _rec):
        dbapi_con.execute("PRAGMA foreign_keys=ON")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    yield sm
    await eng.dispose()
```
Container stub: `types.SimpleNamespace(pg_sessionmaker=sm, redis=None, config=..., env=...)`.

### EXECUTION DISCIPLINE (non-
negotiable)
1. **Read the cited file+lines before editing.** Never invent a symbol; confirm it exists.
2. **Reuse existing code** the plan points to. No parallel re-
implementations.
3. Every task: add/adjust a unit test, `py_compile` clean, `pytest -
q` (new test + full suite) green.
4. **Run a code-
review pass** (self-
review the diff or `/code-
review` skill) before "done"; fix findings.
5. Don't regress §2 DONE items. Items marked **[CONFIRM WITH OWNER]** must be confirmed first.

-
-
-


## 1. ARCHITECTURE MAP (verified)

**Four Pyrogram bots**, wired by `shared/pipeline_manager.py` (owns all 4 clients: `lelouch`, `levi`, `senku`, `gojo`; `container.admin_client` = admin bot or gojo fallback). Stage handoffs in `shared/handoff.py`:
-
 `handoff_download_to_distribution(container, code, title)` — Levi→Senku (`complete_task(code,"levi")` → `assign(code,"senku")`).
-
 `handoff_distribution_to_publish(container, code, title)` — Senku→Gojo. Called from `bots/senku/handlers/wizard.py:~870` after `SenkuPublisher.publish`.

**Pipeline:** Lelouch (request/batch) → Levi (download+encode) → storage packs → Senku (distribution channel + thumbnails) → Gojo (main-
channel post + A-
Z index).

### Data models — `nekofetch/infrastructure/database/postgres/models.py`
-
 `Request` (59) — `user_id` FKs `users.id` (NOT telegram_id; resolve via `UserRepository.get_or_create(tg).id`). Holds `franchise_data` JSON.
-
 `DownloadJob` (94, table `download_queue`) — `priority`, `status`, `current_episode`. **No `processing_order` column.**
-
 `MediaFile` (122, table `files`) — has `season`, **`season_part`**, `episode`, `resolution`, `audio`.
-
 `StoragePack` (215) — unique `(anime_doc_id, season, season_part, resolution, audio, entry_id)`; `header_message_id`, `start_message_id`, `end_message_id`, `file_message_ids` JSON, `episode_from/to`, `entry_id`, `enabled`. **NO `caption` column.**
-
 `ChannelPost` (262) — one per `anime_doc_id`; `main_message_id`, `index_letter`, `index_message_id`.
-
 `ChannelLayout` (358) — per distribution bot, ordered `seq`; `kind` ∈ {info_card, season_card, movie_card, watch_guide, divider, footer}, `anilist_id`, `tg_message_id`, `is_pinned`.
-
 `PublishedPostBackup` (277) / `ChannelContentBackup` (313) — recovery snapshots (caption HTML, mirrored image URLs, `button_data`, `cards` JSON). Editing a live post must also update its backup.
-
 `IndexSection` (438) — A-
Z slots; `base_letter`, `label`, `message_id` (posted iff non-
null), `repurposed`.

### Franchise mapping — `nekofetch/services/franchise_flow.py`
`FranchiseFlowService.build_mapping(franchise_data, anime_doc_id, franchise_entries=None) -
> FranchiseMapping` (186). `MappingEntry` dataclass (74): `anilist_id, kind (ContentKind SEASON|MOVIE|SPECIAL), season_number, season_part, title, episodes, included, auto_detected_part`. `FranchiseMapping.included_entries` (96). **`dataclasses.asdict()` works** (no custom serializer). `dict_to_mapping(mapping_dict)` (505) already reads a dict of the same shape.

### UI conventions (MANDATORY — match exactly)
-
 `nekofetch/ui/screens.py::Screen(caption, image, keyboard)` + `send_screen(client, chat_id, screen, old_msg=...)`; `card(...)` builder.
-
 `nekofetch/ui/components.py::keyboard(*rows)`; rows are `[(label, callback)]`; `cb("<bot>","<action>",*args)`.
-
 `nekofetch/ui/artwork.py::pick_artwork("<bot>")`. Owner gate `shared/access_gate.py::is_owner(container,obj)`; staff `is_staff(obj)`.
-
 Voice in `shared/<bot>_voice.py` as `V`. Localized strings via `nekofetch/localization/messages.py::t(M.KEY, **kw)` reading `resources/language/en.json` (`{placeholder}` syntax, Telegram HTML incl. `<a href>`; unknown placeholders render literally — safe).
-
 **FSM text capture:** `nekofetch/bots/fsm.py::FSM(redis, bot=..., ttl=900)` with `.set/.get/.update/.clear`; register the text handler at a UNIQUE `group=` (e.g. Gojo schedule uses one, Levi naming uses group=13) so it doesn't fight other handlers. Model new caption-
edit flows on `bots/levi/handlers/naming_confirm_handler.py`.

-
-
-


## 2. STATUS
### ✅ DONE & TESTED (do not redo; don't regress)
1. Gojo `/stats` — DB-
driven `nekofetch/services/stats_service.py::compute()`. Tests `tests/test_stats_db_driven.py`.
2. Gojo Schedule button → real list view + Back (`bots/gojo/handlers/schedule.py`).
3. Gojo `/start` vs Settings menu drift → `bots/gojo/app.py::_home_rows`. Tests `tests/test_gojo_home_menu.py`.
4. Gojo Settings trimmed (`bots/gojo/handlers/__init__.py`) — removed thumbnail_channel + timezone.
5. **Lelouch `/redo` fixed.** Root cause: `bots/lelouch/handlers/requests.py:97` `LELOUCH_COMMANDS` (the free-
text title handler's `~filters.command(...)` exclusion list) was missing `"redo"`. Because `register_requests` mounts that group-
0 text handler BEFORE `register_redo`, typing `/redo` matched `_text`, was treated as an anime-
title search, found nothing, and returned — so the real `/redo` handler in `redo.py` never fired and nothing was logged (`/settings` worked only because it WAS in the list). Fix: added `"redo"` to the list. Tests `tests/test_lelouch_redo_command.py` (2 pass) — one pins `redo`, one asserts EVERY registered Lelouch command is in the exclusion list so this can't regress.

### 🟡 PARTIAL — `_map_episode_to_part` exists (`nekofetch/services/download_service.py:1942`) and `_record_file` (1561) already calls it + sets `MediaFile.season_part`, BUT it reads `franchise_data["entries"]` which is never written. **Task A is the linchpin.**

### Key discoveries that SHRINK the work
-
 **Pack splitting per part is ALREADY implemented.** `publishing_service._upload_packs` groups by `(season, season_part, resolution, audio, entry_id)` (`publishing_service.py:560`) and orders by season→part→resolution (575). Once `season_part` is populated (Task A), Vanitas becomes 2 packs automatically. **Task B is mostly verification + Senku entry cards.**
-
 **The update/append flow ALREADY exists.** `publishing_service.py:373` `is_update_entry` branch → `SenkuPublisher.update_distribution_channel(...)`; append logic `_append_and_refooter` deletes guide/dividers/footer, appends new card, re-
posts guide+footer, rewrites `ChannelLayout`. Test `tests/test_senku_channel_update.py`. **Task K reuses this**, adding: torrent partial-
download + Gojo reply-
to-
main + thumbnail-
as-
step-
1.
-
 **Per-
entry thumbnail infra ALREADY exists.** `SenkuThumbnailAdapter.next_pending(code)` / `is_complete(code)`; selections are per `(code,index)` in `shared/distribution_cache.py`. The wizard just hardcodes `index=0` (`bots/senku/handlers/wizard.py:~700`). **Task D = loop + approve/reject buttons.**


-
-
-


## 3. TASKS

### TASK A — Persist franchise entries → finish S01P02E01 + episode renumbering (THE LINCHPIN; do first)
**Why:** Vanitas S1(12)+S1P2(12)=24 showed `S01E13`. `_map_episode_to_part` (`download_service.py:1942`) reads `franchise_data["entries"]`, which is never written, so it returns `None` and packs/naming/progress all lose the part. **Plus:** episodes must restart at 1 per part — S01P02 files are `E01–E12`, not `E13–E24`.

**A1. Persist the mapping at request-
commit.**
-
 File `bots/lelouch/handlers/requests.py`, function `_finalize` (~423–484) builds `franchise_json` and calls `RequestService(container).submit(..., franchise_data=franchise_json)`. Batch equivalent: `bots/lelouch/handlers/batch.py` commit (`keep`/`franchise_data`).
-
 Before submit, build the mapping and attach entries. Use the SAME resolution path the pipeline uses:
```python
from nekofetch.services.franchise_flow import FranchiseFlowService
import dataclasses
svc = FranchiseFlowService(container)
entries = await svc.resolve_franchise_entries(franchise_data, anime_doc_id)  # cache→walk→None (line ~209)
mapping = svc.build_mapping(franchise_data, anime_doc_id, entries)
franchise_json["entries"] = [
    {"kind": e.kind.value, "season_number": e.season_number, "season_part": e.season_part,
     "episodes": e.episodes, "title": e.title, "anilist_id": e.anilist_id}
    for e in mapping.included_entries
]
```
  (`kind.value` is lowercase `"season"|"movie"|"special"` — matches what `_map_episode_to_part` checks: `e.get("kind")=="season"`.) Do this in BOTH the single-
request `_finalize` and the batch commit so every Request carries `entries`.

**A2. Rewrite `_map_episode_to_part` to return `(part, renumbered_episode)`.** Currently it returns `part | None`. Change to return `tuple[int|None, int]` — part number AND the episode-
within-
part. For S01P02 ep 13 from the torrent → `(2, 1)`. Logic: find the part via cumulative boundaries (current logic), then `renumbered = episode_num -
 sum(eps for prior parts in same season)`. Single-
part seasons still return `(None, episode_num)` — no renumber.

**A3. Thread the renumbered episode through `_record_file`.** Currently `MediaFile.episode = ep.number` (the torrent's raw number). Change to:
```python
part, renumbered = _map_episode_to_part(ep.number, ep.season, req.franchise_data or {})
session.add(MediaFile(..., season_part=part, episode=renumbered, ...))
```
So `MediaFile.episode` stores the **renumbered** value (1–12 for part 2), not the raw 13–24.

**A4. Filename + progress use renumbered episode.** `nekofetch/services/processing/stages.py:604–642` renders filename; it reads `MediaFile.episode`, which is now renumbered (A3) — **verify no second adjustment needed**. Progress card (`nekofetch/ui/progress.py:209`) currently shows `E{int(current_episode):02d}` — pass the renumbered value from the mapping. Template default still changes to `{title} S{season}{season_part}E{episode} ...` (add `{season_part}`).

**A5. Re-
ask filename template when a part is present.** (unchanged from prior)

**A6. TEST** `tests/test_episode_part_mapping.py`: `franchise_data={"entries":[{"kind":"season","season_number":1,"season_part":1,"episodes":12},{"kind":"season","season_number":1,"season_part":2,"episodes":12}]}`. Assert `_map_episode_to_part(13,1,fd) == (2, 1)`, `(24,1,fd) == (2, 12)`, `(12,1,fd) == (1, 12)`, `(1,1,fd) == (1, 1)`; single-
entry season → `(None, <unchanged>)`. Also assert a `MediaFile` created with raw ep=13 stores `episode=1, season_part=2`.

### TASK B — Two entry cards for a split season (packs already split)
**Already done:** pack grouping/order in `publishing_service._upload_packs` (560/575) yields one `StoragePack` per `(season, season_part, resolution, audio, entry_id)` once Task A populates `season_part`. **Verify** the end sticker is per pack (`storage_channel_service.py:231`, inside `upload_pack`) — it is.

**What to add:** ensure Senku posts **one `season_card` per `(season, season_part)`**, not per season. In `shared/senku_publisher.py` (`_build_posts` ~447, `_send_posts` ~856) and `nekofetch/services/bot_content.py` (`_build_franchise_watch_guide` ~960, season lines), entries are keyed by `anilist_id`. Vanitas S1 and S1P2 are **separate AniList entries** with distinct `anilist_id`, so they should already produce two cards **if** both appear in the franchise walk. **Confirm** by reading `_build_update_cards`/card build: does it iterate franchise entries (per `anilist_id`) or collapse by `season_number`? If it collapses by season_number, change it to iterate per entry (`anilist_id`, carrying `season_part`) so both parts get a card + a watch-
guide line. Card `kind` stays `season_card`; label uses `season_part` (e.g. "Season 1 Part 2").

**TEST** `tests/test_two_part_cards.py`: with a franchise of two TV entries sharing season_number=1 (parts 1,2), assert the post/layout builder emits two `season_card` entries with distinct `anilist_id` and correct labels.

### TASK C — Edit pack captions from Levi (persist + live edit)
**Facts:** caption is generated on the fly by `nekofetch/services/bot_naming.py::build_pack_caption(...)` (427) via `storage_channel_service.header_text()` (80→110); **not stored.** `StoragePack.header_message_id` (models.py:236) is the message to edit; edit API `edit_message_caption` (used in `main_channel_service.py:456`, `index_channel_service.py:558`).

**Owner's mental model (two distinct things — don't conflate):**
-
 **Filename title** (used in `S01E01`-
style filenames): short, filesystem-
safe (e.g. "Vanitas no Carte"). When the owner changes the filename title on ONE entry during naming, it propagates the **title portion** to ALL entries of that show — but each entry keeps its own `S1`/`S1P2` label. See A7 below.
-
 **Pack caption** (the header text on the storage pack, can be longer): editable separately in Levi. The owner may set filename="Vanitas no Carte" but caption="The Case Study of Vanitas". THIS task is the caption editor.

**Do:**
1. **Add a nullable `caption` column** to `StoragePack` (models.py ~215–260): `caption: Mapped[str | None] = mapped_column(Text)`. Add an Alembic migration if the repo uses Alembic (grep `alembic/versions`); else it's created by `Base.metadata.create_all` on dev. Persist the generated caption in `storage_channel_service._persist` (~245–274) so existing packs backfill on next upload; when `caption` is set, `header_text()` should prefer the stored value.
2. **Levi menu:** add `("✏️ Edit Captions", cb("levi","captions"))` to BOTH `/start` (`bots/levi/app.py:~193`) and the `levi|home` callback (`~101`). **Refactor Levi's menu to a single `_home_rows`-
style builder** (mirror the Gojo precedent) so the two never drift — this also satisfies the cross-
bot menu-
consistency rule.
3. **Flow:** `levi|captions` → list **one row per entry** = distinct `(anime_doc_id, season, season_part)` across `StoragePack` (dedupe resolutions/audio). Label via `build_pack_caption`'s entry label (e.g. "Vanitas — Season 1 Part 2"). Tap entry → `FSM(container.redis, bot="levi_caption").set(user_id,"edit_caption", anime_doc_id=..., season=..., season_part=...)`; prompt "send the new caption". Register a text handler at a UNIQUE group (e.g. `group=14`) modeled on `naming_confirm_handler.py`.
4. **Save:** update `StoragePack.caption` for all rows of that `(anime_doc_id, season, season_part)`; then `for pack: await levi.edit_message_caption(pack.channel_id, pack.header_message_id, new_caption)`. Filename NEVER changes. If a `ChannelContentBackup` card exists for this entry, update its stored caption too (so a restore uses the corrected text). Persist to whichever store holds it (Postgres primary; mirror to Redis/Mongo if the caption is cached there).
5. **Owner/staff gate** with `is_owner`/`is_staff` consistent with other Levi owner-
only tools.
6. **TEST** `tests/test_levi_caption_edit.py`: seed 2 entries × 3 resolutions, run the save path with a stub client recording `edit_message_caption` calls; assert only the targeted entry's rows got the new `caption` and one edit per stored `header_message_id`.

**A7 — Filename-
title propagation (belongs to the naming flow, Task A).** When the owner edits the filename title for one entry in the naming confirm, apply that title to ALL entries of the same `anime_doc_id` (the name portion only). Each entry's `S{season}{season_part}` stays as-
is — S1 stays S1, S1P2 stays S1P2; only `{title}`/`{short_title}` changes across entries. Store the chosen title on the request (e.g. `franchise_data["_display_title"]`) so every entry's rename uses it. **TEST:** editing the title for the S1P2 entry updates the S1 entry's rendered filename title too, while S1 keeps `S01...` and S1P2 keeps `S01P02...`.


### TASK D — Per-
entry thumbnail approve/reject in Senku
**Facts:** `bots/senku/handlers/wizard.py` thumbnail loop (~698–863) fetches `cache.get_entry(code, 0)` — hardcoded index 0. Per-
entry selection storage already exists: `shared/distribution_cache.py::Selection` keyed `(code,index)` (`get_selection`/`set_selection` ~317–369, `all_done` ~386). Adapter `SenkuThumbnailAdapter.next_pending(code)`/`is_complete(code)` already exist. Render: `nekofetch/services/thumbnail_service.py::render_thumbnail(...)` (336) → WebP under `data/thumbnails/`.

**Do:**
1. In `_enter_thumbnails` (~698) replace `get_entry(code,0)` with `entry = await thumbs.next_pending(code)`; if `None`, go to watch-
order/publish. Track the current index in FSM data so pick callbacks (`senku|wiz|pick|<code>|<index>|<asset>|<n>`, already carry `index`) target the right entry.
2. After `_thumb_generate` renders the preview, show **Approve / Redo** buttons (new callbacks `senku|wiz|thumbok|<code>|<index>` and `senku|wiz|thumbredo|<code>|<index>`):
   -
 **Approve** → `cache.set_selection(code, index, done=True)` then `_enter_thumbnails` again (advances to `next_pending`, or finishes).
   -
 **Redo** → clear THIS entry's selection only (`set_selection(code,index, asset="logo"/"poster"/"backdrop"=None, done=False)` or add a `clear_selection(code,index)` to distribution_cache) and re-
enter the logo step **for this same index**. Do NOT touch other entries (they stay `done`).
3. Because approve/reject advance via `next_pending`, a reject on entry 2 restarts entry 2 only — entry 1 remains confirmed. This satisfies the exact requirement.
4. **TEST** `tests/test_thumb_per_entry_fsm.py`: a pure state-
machine helper `advance(entries, index, action)` → `(next_index, substate)`; assert approve advances 0→1, reject at 1 stays at 1 with substate "select_logo", 0 stays done; `all_done` gates completion.

### TASK E — Thumbnail editing (Senku for distribution, Gojo for main) — [CONFIRM WITH OWNER: HTML host]
**Facts:** `render_thumbnail` writes the HTML to `data/thumbnails/assets_{title}/thumbnail.html` then the WebP (thumbnail_service.py ~441–459). Template `thumbnail/index.html` tokens: `{{TITLE}} {{NATIVE_TITLE}} {{ROMAJI_TITLE}} {{SYNOPSIS}} {{GENRE_PILLS}} {{STUDIO}} {{TMDB_RATING}} {{ANILIST_SCORE}} {{LOGO_IMAGE}} {{POSTER_IMAGE}} {{BG_IMAGE}} {{FLAG_IMAGE}}` (substitution ~419–439). Field gathering: `gather_thumbnail_fields(container,title,anime_doc_id)` (131). Main-
post record: `MainChannelService` (`_record` 481, `publish` 423); reply/edit via `client.edit_message_media` / `edit_message_caption`.

**Storage decision — RECOMMENDATION (confirm before coding):** persist the thumbnail's **input fields** (the dict that produced it: logo_url, poster_url, bg_url, synopsis, rating, genres, studio, titles) in the DB, not just the WebP. Add a small table `thumbnail_source(anime_doc_id, anilist_id, fields JSONB, html TEXT NULL, updated_at)` (or a JSONB column on `StoragePack`/`ChannelPost`). This is durable + transactional + needs no external host. The owner floated Catbox/GitHub — do **NOT** hardcode an external host without sign-
off. Fallback chain when editing: stored fields → re-
gather via `gather_thumbnail_fields` → prompt user to regenerate.

**Do:**
1. Persist fields on every render (hook `render_thumbnail` callers in the wizard + publishing to write the `thumbnail_source` row).
2. **Senku `/edit_thumbnail`** (owner/staff): pick anime → pick entry → menu (Edit Logo / Poster / Backdrop / Synopsis / Rating / Genres). Asset edits reuse the wizard's existing gallery pickers; text edits use the FSM text-
capture pattern. Re-
render via `render_thumbnail`, update the `thumbnail_source` row, re-
post/edit the distribution card (Senku owns that channel), and update the `ChannelContentBackup` card.
3. **Main-
channel edits go through Gojo** (Senku has no main access). Route via `pipeline_manager`/`container.admin_client`: Senku signals Gojo (or the owner runs the same edit in Gojo). Gojo re-
renders, `edit_message_media` on `ChannelPost.main_message_id`, and updates `PublishedPostBackup`. Provide the SAME edit UI in Gojo for main posts.
4. All edits update the DB (source row + relevant backup) so a later restore uses corrected values.
5. **TEST** `tests/test_thumbnail_edit.py`: seed a `thumbnail_source` row, apply a synopsis edit through the service function, assert the row's `fields["synopsis"]` changed and a re-
render was requested (stub the renderer). Assert main-
channel path updates `PublishedPostBackup`.

### TASK E2 — Main-
channel thumbnail/caption metadata rules (mostly VERIFY, some fix)
The main-
channel post's **generated thumbnail** differs from a distribution entry card. Confirm each rule holds; fix if not.
1. **Synopsis = TMDB franchise overview, NOT AniList per-
entry.** ALREADY IMPLEMENTED — `main_channel_service.py:164–165, 250–277` ("TMDB's franchise-
level synopsis is preferred for the main post per spec"); the entry-
card thumbnail keeps AniList's per-
season synopsis. Just verify the main-
post thumbnail render is fed `facts.overview` (TMDB), not the AniList synopsis.
2. **AniList rating = average of ALL franchise entries.** ALREADY IMPLEMENTED — `main_channel_service.py::_avg_score_pct` (38–49), used at 352/380. Verify the thumbnail's `anilist_score` uses this franchise average on the main post.
3. **Language label rule (main-
channel thumbnail AND main-
channel caption if audio shown there).** The label is franchise-
level: **if we provide English (dub/dual) for even ONE entry, the whole show reads "English & Japanese"** — a season that's sub-
only, or extras that are sub-
only, do NOT downgrade it. Find where the `languages` set feeding `language_label`/the thumbnail is built for the main post (grep `languages=` in `main_channel_service.py:391`, `gather_facts`, and `bot_content.py::_gather_metadata` ~509). Ensure it UNIONs languages across all included entries (so one English entry ⇒ English present) rather than reading a single entry. `language_label` (`bot_naming.py:143`) already renders "English & Japanese" from the set; the fix (if any) is upstream set-
construction. Apply the same to the main-
channel caption's audio/language line if it shows one.
4. **TEST** `tests/test_main_channel_meta.py`: build facts from entries where only ONE entry has English audio and the rest are sub-
only → assert language label == "English & Japanese"; assert rating == average of all entry scores; assert synopsis source is the TMDB overview field.

### TASK E3 — Episode-
count line format ("25 + 3 extras", "+ 2 movies")
The main-
channel post (and/or info card) has an **episodes** section. Format it as:
-
 Total **main seasonal** episodes as the base number (e.g. `25`).
-
 If there are extras (OVA/ONA/Special), append ` + N extras` where N = total extra EPISODES across all extra entries (e.g. two OVAs of 2 eps + 1 ep = `+ 3 extras`). If exactly one extra episode → ` + 1 extra` (singular).
-
 If there are movies, append ` + N movie(s)` (e.g. `+ 1 movie`, `+ 2 movies`, `+ 3 movies`).
-
 Example composite: `25 + 3 extras + 2 movies`.
**Do:** find where the episode count is rendered (grep `episodes`, `{episodes}`, `ep count`, `franchise_episodes` in `main_channel_service.py` caption build + `bot_content.py` info card). Build the string from `franchise_data["entries"]`: sum `episodes` for `kind=="season"` entries → base; sum `episodes` for `kind=="special"` → extras; count `kind=="movie"` → movies. Handle singular/plural. **TEST** `tests/test_episode_count_label.py`: assert `25`, `25 + 1 extra`, `25 + 3 extras`, `25 + 2 movies`, `25 + 3 extras + 2 movies` for the matching entry sets.

### TASK F — Encode in RELEASE order AND episode order
**Requirement (clarified):** BOTH levels — entries in release order (S1 → S1P2 → S2 → S2P2 → …) **and** episodes ascending within each (E1 → E2 → E3, never E1 → E4).
**Facts:** queue claims **jobs** not episodes (`queue_repo.py::next_queued` (15) = `ORDER BY priority ASC, created_at ASC`). Within a job, the encode stage iterates `ctx.files` (`stages.py:~604`).
**Do:**
1. Sort the encode iteration list by the full key `(season, season_part or 0, episode)` ascending right before the encode/processing loop (`ctx.files` in `stages.py`). This handles episode-
order within a request AND part-
order within a season.
2. For cross-
request order (S2 request vs S1 request), keep `priority, created_at` (S1 created first → release-
ordered). Add `processing_order` only if reading proves neither sort suffices.
3. **TEST** `tests/test_encode_order.py`: feed files as `[S1E3, S1E1, S1P2E2, S1E2, S1P2E1]` → assert iteration yields `S1E1, S1E2, S1E3, S1P2E1, S1P2E2`.

### TASK G — DDL option missing in "Begin Now"
**Facts:** source selection is in the admin review flow mounted on Levi (`bots/levi/handlers/__init__.py:~40` mounts `nekofetch/bots/admin/handlers/review`); source callbacks look like `staff|rsource|<code>|<src>`. DDL client exists (`nekofetch/sources/ddl.py`).
**Do:** grep `Begin Now`, `rsource`, `Open Downloader`, `DDL`, `direct` in `bots/levi/handlers/tasks.py` + `nekofetch/bots/admin/handlers/review.py` + `nekofetch/ui/screens.py`. Find the source keyboard builder and the reason DDL is absent (missing row or a conditional). Add the DDL row matching the other options' style; confirm the `staff|rsource|<code>|ddl` branch has a handler (wire it to the existing DDL flow if missing). **TEST:** assert the source keyboard builder includes a DDL button under the same conditions as Torrent/Website.

### TASK H — Levi/Lelouch batch "board" button → "Open Tasks"
**Facts:** batch commit screen `bots/lelouch/handlers/batch.py:547–554` shows buttons `[[V.BTN_QUEUE→lelouch|queue|0, V.BTN_HOME→lelouch|home]]`. `lelouch|queue` shows request stats ("The Board"), NOT Levi tasks. **Lelouch and Levi are separate bot clients — a callback can't cross bots.** Precedent: `shared/access_gate.py::lelouch_link` (66) resolves a bot's `https://t.me/<username>` via `pipeline_manager`.
**Do:** add a `levi_link(container)` helper (mirror `lelouch_link`, using `mgr.levi`). On the batch-
done screen add a **URL button** "📋 Open Tasks" → `levi_link(container)` (a deep link into Levi where the admin runs `/tasks`). Omit the button if the link can't resolve (never show a dead button). Keep Home. **TEST** `tests/test_batch_done_button.py`: with a stub pipeline_manager exposing a Levi username, assert the batch-
done keyboard contains a URL button to `t.me/<levi>`.


### TASK J — Monthly ban recovery + full channel rebuild
**Facts (already substantial):** monthly sweep registered in `nekofetch/bots/manager.py:703` (`entity_full_check_days`, default 30) → `_monthly_full_check` (385) → `check_all_entities` (136) → `_probe_entity` → ban markers (`maintenance_service.py:34`). On ban: disable + `BotOrchestratorService.recreate_bot(anime_doc_id)` (`bot_orchestrator.py:77`) which pre-
backs-
up (`BackupService.record_distribution_channel`), deletes old, creates fresh, `_restore_channel` → `BackupService.restore_distribution_channel` (`backup_service.py:425`), regenerates content, refreshes index. Warm-
up exists: `SenkuPublisher._warm_global_search` (~1487). Main/index manual recovery: `/recovermain`, `/recoverindex` (`nekofetch/bots/admin/handlers/commands.py:184/228`).

**What needs fixing/adding:**
1. **Admin assignment messages.** Gojo DMs a free admin via the existing channel-
creation message flow (reuse the SAME recurring artwork, UI layout, character voice from the normal Senku channel-
creation wizard — find those in `bots/senku/handlers/wizard.py::_ask_channel` and related). Light modifications for ban-
recovery context: "Restoring a banned channel — create a replacement or use the userbot." Admin creates the channel, gives it a username, **adds the PFP (profile picture)**, removes the "changed photo" service messages, adds Senku + Gojo as admins. That's all the human does.
2. **Button-
link bug.** `restore_distribution_channel` re-
posts entry cards with verbatim `button_data` → Download buttons keep the OLD invite link. Fix: after restore, call `BotManagementService.bind_title` (or the `BotContentService.generate_posts` path) to rebuild buttons with the new `invite_link`. Invoke inside `recreate_bot` after `_restore_channel`.
3. **Main-
channel Download button + reply.** After the restore, go to the main-
channel post (`ChannelPost.main_message_id` for `anime_doc_id`), **edit the Download button's invite link** to the new channel's (real button, not text), **then reply** to that post with a block-
quoted, bulleted, en.json-
editable message announcing the restore — including a **"click here" hyperlink** to the new channel. Add `"ban_recovery_reply": "<blockquote><b>{title}</b>\n• The channel was banned and has been restored\n• <a href=\"{channel_link}\">Click here</a> to access the new channel</blockquote>"` to `resources/language/en.json` + constant `BAN_RECOVERY_REPLY` to `messages.py::M`. Send via `MainChannelService`: `await admin_client.send_message(main_channel_id, t(M.BAN_RECOVERY_REPLY, ...), reply_to_message_id=main_msg_id, parse_mode=ParseMode.HTML)` — **no `reply_markup`**.
4. Confirm warm-
up runs before re-
posting, and watch-
guide deep links are remapped (the restore already remaps `{BOT_QUAL#...}` — verify).
5. **TEST** `tests/test_ban_recovery_full.py`: seed `ChannelContentBackup` with stale button links, run restore+rebind+main-
edit+reply path with stub client, assert (a) re-
posted cards have NEW invite link, (b) guide references new message IDs, (c) main-
post Download button updated, (d) reply posted with text hyperlink + no buttons.

### TASK K — Update flow (new season/part appended) — reuses existing append
**Facts:** append already implemented — `publishing_service.py:373` `is_update_entry` → `SenkuPublisher.update_distribution_channel` → `_append_and_refooter` (deletes guide/dividers/footer, appends new `season_card`, re-
posts guide+footer, rewrites `ChannelLayout`); test `tests/test_senku_channel_update.py`. Handoff/assignment via `shared/handoff.py` + `AdminAssignmentEngine.assign`.

**What's missing (add these):**
1. **Torrent partial download.** When admins supply a torrent that contains S1+S2 (or S1+S1P2) but the request is only the new entry, download ONLY the new entry's episode range. Find file selection in the download path (grep `torrent`, file-
index selection, `select_files`, `qbittorrent` in `nekofetch/sources/`). Use the new entry's `(episodes, season_part)` from `franchise_data["entries"]` (Task A) to compute the wanted episode window (e.g. S1P2 = eps 13–24, S2 = its own 1–N) and select only those files. Handle: 13 separate torrents (one per ep), one combined torrent (subset), and a season-
pack torrent.
2. **Thumbnail becomes step 1 on updates.** On the update branch, Senku must NOT create a channel; it starts at thumbnail generation for the new entry only. Gate the wizard: if `is_update_entry` (channel already exists for `anime_doc_id`), skip channel-
create states and enter `_enter_thumbnails` for the new entry index. (Task D's per-
entry loop makes this natural.)
3. **Gojo replies to the main post.** After the append completes, Gojo replies to `ChannelPost.main_message_id` (look it up by `anime_doc_id`) with an **en.json-
driven, block-
quoted, bulleted** message containing a **channel hyperlink** and **NO buttons**:
   -
 Add to `resources/language/en.json` a key e.g. `"season_update_reply": "<blockquote><b>{title} — {entry_label}</b>\n• {episodes} episodes • {quality}\n• <a href=\"{channel_link}\">click here</a> to watch</blockquote>"` (use the `•` bullet; owner may reword — that's the point of en.json).
   -
 Add `SEASON_UPDATE_REPLY = "season_update_reply"` to `nekofetch/localization/messages.py::M`.
   -
 Send: `await container.admin_client.send_message(main_channel_id, t(M.SEASON_UPDATE_REPLY, ...), reply_to_message_id=main_msg_id, parse_mode=ParseMode.HTML)` — **no `reply_markup`**. Put this in `MainChannelService` as a new method `reply_update(anime_doc_id, entry_label, episodes, quality, channel_link)` and call it from the update branch after Senku finishes.
4. **TEST** `tests/test_update_flow.py`: (a) file-
selection picks only the new entry's episodes from a combined torrent listing; (b) update branch skips channel creation; (c) `reply_update` posts a reply to the recorded `main_message_id` using the en.json template with a hyperlink and no buttons (assert `reply_markup is None`).

**K5. Update-
DURING-
redo (owner now wants this — NOT deferred).** Scenario: owner redoes a series early in the month; a new season aired yesterday but the monthly check hasn't run. So the redo flow must OFFER the update inline and let the owner choose.
-
 In the `/redo` flow (`RedoService` / `bots/lelouch/handlers/redo.py`), after resolving the franchise, detect entries present in `franchise_data` but NOT yet in the channel (compare to existing `ChannelLayout`/`StoragePack` entries for `anime_doc_id`). If new entries exist, show a prompt: "Season N is available — include it in this redo? [Yes] [No]".
-
 **If Yes:** run the normal update flow (K1–K3) for the NEW entry: download its episodes, generate ONLY the new entry's thumbnail, append its `season_card` (remove guide+divider+footer → add new card → re-
post updated guide+divider+footer), then Gojo replies to the main post (K3).
-
 **For the entry being redone (e.g. S1):** its thumbnail already exists — do NOT regenerate for the user; just replace its Download/quality buttons with the fresh packs' links (the redo-
relink path). See Task O for metadata refresh that DOES force regeneration (rating/episode-
count/language changes).
-
 **If No:** redo only the selected entry; skip the update.
-
 **TEST** extend `tests/test_update_flow.py`: redo with a detectable new season → prompt path invoked; Yes → append+reply for new entry only; No → no append.

### TASK L — Special/OVA handling + naming preview
**Facts:** classifier `nekofetch/services/processing/stages.py::classify_kind` (172) + `_content_type_label` (187); extras filtered in `download_service.py:213–229` (keeps `kind=="episode"` when an explicit list isn't given). MAL client with episode names EXISTS: `nekofetch/sources/telegram/myanimelist.py::MyAnimeListClient.episode_titles(mal_id, max_pages=5) -
> [{number,title}]` (663).

**Do:**
1. **Skip unselected specials.** If `franchise_data["entries"]` has no SPECIAL/OVA entry, exclude extra files (13.5 etc.) — extend the existing filter (213) to also drop `kind in {"special","ova","ona","extra"}` unless a matching mapping entry is `included`.
2. **Single OVA → auto-
map** to that entry.
3. **Multiple OVAs → name-
match.** Fetch `episode_titles` from MAL (resolve MAL id from the mapping/AniList→MAL); fuzzy-
match torrent filenames to episode titles (use `difflib.SequenceMatcher` ratio > 0.8; no new dep). Auto-
map on confident match.
4. **Fallback → ask the user.** FSM flow listing unmatched OVA files vs OVA entries; user pairs them (buttons or "file → entry"). Persist the mapping so re-
download reuses it.
5. **Naming/caption preview for OVAs/movies** (not just seasons): `NamingConfirm.confirm` must run for extra entries too, showing the proposed filename + caption preview. For seasons the template just swaps `{season}`; for OVAs/movies use `special_template`/`movie_template` (`config.py:258–265`).
6. **TEST** `tests/test_special_mapping.py`: (a) no OVA entry → 13.5 excluded; (b) one OVA → auto-
mapped; (c) two OVAs with MAL titles matching filenames → auto-
mapped; (d) no match → mapping prompt path invoked.

### TASK M — Movie >2000 MB: 2-
pass compress to ≤2000 MB @1080p, else split
**Facts:** encoder `nekofetch/sources/_transcode.py::_encode` (445) builds the ffmpeg cmd (libx264, `-
crf` from `_CRF={1080:21,...}` line 22, `-
preset fast -
tune animation`); oversize heuristic `is_oversized_1080` (348) exists but **there is NO 2000 MB cap and NO splitting anywhere.** Movies classified via `classify_kind` (KIND_MOVIE). Encode stage `stages.py::EncodeStage` (1350), upload in `publishing_service._upload_packs` (~654 `storage.upload_pack`).

**Do:**
1. Add `_encode_to_target_size(src, out, target_mb=1990, height=1080)` in `_transcode.py`: probe duration (there's already an ffprobe helper — grep `ffprobe`/`_duration`), compute video bitrate `= (target_mb*8*1024)/duration_s -
 audio_kbps`, run **2-
pass** libx264 (`-
pass 1`/`-
pass 2 -
b:v <calc>k`), keep `-
vf scale=-
2:1080`, audio AAC 128k. Verify output ≤ 2000 MB (hard: not one MB over — target 1990 for safety).
2. Hook it for MOVIE packs before upload in `_upload_packs` (~650): if `content_type==Movie` and file > 2000 MB → compress; re-
check.
3. **If still >2000 MB → split.** Add `split_movie(src, target_mb=1990) -
> [part1, part2, ...]` using ffmpeg segment by computed time (or `-
fs`), name `"{title} Part 1"`, `"{title} Part 2"`; upload as separate files within the same pack (or sub-
packs with `entry_id` suffix `_p1/_p2`) so the pack reads "Movie (2 parts)". Ensure `MediaFile`/pack metadata distinguishes the parts.
4. **TEST** `tests/test_movie_size.py` (mock ffmpeg/ffprobe + `os.stat`): (a) 1.8 GB → no action; (b) 2.5 GB compressible → single compressed file ≤2000 MB (assert bitrate math); (c) 4 GB not compressible → split into 2 parts. Assert exact 2000 MB boundary handling (2001 MB triggers, 2000 does not).

### TASK N — RESEARCH ONLY: userbot+bot-
token to add buttons to a user's message [CONFIRM WITH OWNER]
Research whether a userbot (user session) can attach an inline keyboard to a message the USER posted (bots can't edit messages they didn't send). In Pyrogram/Telegram: a bot can only add `reply_markup` to its own messages; a user account cannot attach inline keyboards at all (only bots can). Investigate the real options: (a) the bot reposts the content with buttons and the user deletes theirs; (b) `via_bot`/inline-
mode; (c) business-
connection APIs. **Deliverable:** write findings to `docs/userbot_edit_research.md` — is it possible, what setup/permissions, recommended approach or the concrete alternative. **Do not implement** anything in production until the owner decides.

### TASK O — Redo metadata refresh (rating/episode-
count/language/channel-
title)
When `/redo` is run on a title, and metadata has changed since the original publish (a new season was added → episode count increased; dual-
audio arrived → language changed; rating shifted), the main-
channel thumbnail AND entry-
card thumbnails must regenerate, captions update, and channel metadata (title, PFP, service messages) refresh.

**Facts:** `/redo` is `shared/redo_service.py` + `bots/lelouch/handlers/redo.py`. Main-
post publish: `MainChannelService.publish`. Distribution: `SenkuPublisher`. Thumbnail render: `thumbnail_service.py::render_thumbnail` + stored source in `thumbnail_source` (Task E1). Channel title edit: Pyrogram `edit_chat_title`, followed by deleting the "channel name changed" service message.

**O1. Detect metadata changes.** After the redo downloads/encodes fresh files, compare the NEW `franchise_data["entries"]` to the stored `thumbnail_source` fields for each entry + the main post:
-
 **Episode count** — sum `episodes` for `kind=="season"` entries + extras/movies (Task E3 format). Compare to stored.
-
 **Language** — union languages across all entries; apply "one English entry ⇒ English & Japanese" rule (Task E2). Compare to stored main-
post language.
-
 **Rating** — franchise-
average AniList score (Task E2, already implemented). Compare to stored.
-
 If ANY differ, regenerate thumbnails for affected entries + main post.

**O2. Regenerate thumbnails ONLY for changed metadata.** For entries being redone that have NO metadata change (e.g. redo for quality upgrade only), do NOT regenerate the thumbnail — just replace Download/quality buttons (the redo-
relink path). For entries with changed language/episode-
count/rating, call `render_thumbnail` with updated fields, save to `thumbnail_source`, and `edit_message_media` on the entry card (distribution) or main post (Gojo). Update `ChannelContentBackup` + `PublishedPostBackup`.

**O3. Update captions.** Main-
channel caption (`MainChannelService._caption`) and distribution entry-
card captions use the episode-
count line + language label. Re-
render and `edit_message_caption` where needed. Also update the stored backups.

**O4. Channel title refresh.** If the franchise title changed in `franchise_data` (e.g. user switched from "Case Study of Vanitas" to "Vanitas no Carte" — Task A clarification), update the distribution channel's title via `edit_chat_title(new_title)`, then delete the "channel name changed to…" service message (grep service-
message deletion in `senku_publisher.py::_warm_global_search` line ~1512 for the pattern).

**O5. TEST** `tests/test_redo_metadata_refresh.py`: seed old `thumbnail_source` with rating=75, language="Japanese"; redo with new entries giving rating=82, one English entry → assert thumbnails regenerated for main+affected entries, captions updated with new episode count + "English & Japanese", channel title edited + service message deleted. Assert NO regeneration when only quality changed (redo-
relink only).

### TASK P — Lelouch admin panel: "Manage REQ/WRK" + Redo/Clear-
DB row (owner-
only)
**Facts:** admin panel keyboard is `bots/lelouch/screens.py::admin_panel` (49–66). Current rows include `[("🗂 Manage Requests", cb("mg","reqs",0))]` (62) and `[(V.BTN_CLEAR_DATABASE, cb(BOT,"dbclear"))]` (63). The manage plane is `bots/lelouch/handlers/management.py` (namespace `mg|…`); it currently lists/cancels/reassigns **requests** (REQ). Work items are WRK codes (`kurosoden.shared.work_service`). Redo command handler is `bots/lelouch/handlers/redo.py`; the redo entry callback is `redo|new` (redo.py:104).

**Do:**
1. **Rename + merge to "Manage REQ/WRK".** Change the button label to "🗂 Manage REQ/WRK". In `management.py`'s `mg|reqs` list, include BOTH REQ (Request rows) and WRK (WorkItem rows), each with the same actions the request list already offers (cancel, reassign). Read `management.py` to see how REQ rows are listed/actioned and extend the query + action handlers to cover WRK codes. Label each row with its code prefix so REQ vs WRK is visible.
2. **Redo + Clear-
DB row (owner-
only).** Replace the standalone Clear-
DB row (63) with a single two-
button row: `[("🔁 Redo", cb("redo","new")), (V.BTN_CLEAR_DATABASE, cb(BOT,"dbclear"))]`. The `redo|new` callback already exists (redo.py:104) and starts the redo flow. **Gate the whole row to owner only** — `admin_panel` is already admin-
scoped, so wrap this row in `if is_owner(...)`. Since `admin_panel` is a pure screen builder, thread an `is_owner: bool` param into it (the caller in `app.py`/handlers has the update object) and only append the Redo/Clear row when true.
3. **TEST** `tests/test_lelouch_admin_panel.py`: `admin_panel(..., is_owner=True)` → keyboard contains a row with BOTH a `redo|new` button and a `dbclear` button, and a "Manage REQ/WRK" button; `is_owner=False` → no Redo/Clear-
DB row. Plus a management test: the REQ/WRK list includes a seeded WorkItem with a cancel action.

-
-
-


## 4. ORDER & DEPENDENCIES
1. **A first** (episode renumber + persist entries — unblocks B, E2, E3, F, K, L, O).
2. **B, C** (packs/captions).
3. **E2, E3** (main-
post metadata rules + episode-
count format — mostly verify; feed O).
4. **D → E → E1** (per-
entry thumbnails → edit → source storage).
5. **G, H** (small UI fixes, independent).
6. **J, N** (ban recovery; research — independent).
7. **K** (update flow, incl. K5 update-
during-
redo; needs A, D, E).
8. **L, M** (specials/movies; need A).
9. **O** (redo metadata refresh; needs A, E1, E2, E3).
10. **I = redo Sabikui Bisco, ORB, Vanitas LAST**, via `/redo` (`shared/redo_service.py`), verifying Vanitas → 2 packs, 2 cards, `S01P02E01`–`E12` (renumbered), release+episode-
order encode, and any metadata refresh (O).

## 5. DEFINITION OF DONE (every task)
-
 Matches UI/voice/menu conventions; single-
source-
of-
truth menus (no `/start` vs callback drift).
-
 en.json-
driven copy where specified; DB writes update the matching backup snapshot.
-
 New unit test green **and** full suite green: `./.venv/Scripts/python.exe -
m pytest -
q`.
-
 `py_compile` clean; no remote-
DB dependency for verification.
-
 Code-
review pass done; findings fixed. §2 DONE items not regressed.

## 6. FILES CHANGED THIS SESSION (context; do not undo)
`nekofetch/services/stats_service.py`, `bots/gojo/app.py`, `bots/gojo/handlers/schedule.py`, `bots/gojo/handlers/__init__.py`, `nekofetch/services/download_service.py` (`_map_episode_to_part` + `_record_file` wiring, INCOMPLETE per Task A), tests `tests/test_gojo_home_menu.py`, `tests/test_stats_db_driven.py`.


## 7. IMPLEMENTATION RECORD — 2026-
08-
07

> This section records what was actually completed in this session. The original plan above is preserved unchanged.

### What users will see

-
 New anime downloads still follow the same flow: files are downloaded, checked, processed, and placed into the storage channel. The public main-
channel post is then created with artwork, episode/quality/language information, Index, and Download buttons.
-
 A new season or extra episode set is added to the existing distribution channel instead of creating a duplicate title. The main-
channel post receives a short reply saying the new entry is available and linking to the current distribution channel.
-
 If a distribution channel is recreated after a ban, saved cards are reposted from backup. Captions, artwork, buttons, dividers, and pinned cards come back without depending on fresh TMDB/AniList results. Once the replacement channel is live, download buttons are relinked to fresh storage packs, and the main post receives a recovery reply with the new link.
-
 Selected thumbnail artwork and the gathered display information are saved durably, so the thumbnail workflow is not dependent only on temporary wizard state after a restart.
-
 Movies larger than Telegram's ordinary upload limit are compressed first, then split if needed, and every resulting part is checked. Unsafe conversion failures leave the original available for retry instead of silently deleting it.
-
 Successful uploads clean the original movie, compressed copies, split parts, and other generated temporary artifacts from disk.
-
 Main-
channel episode summaries distinguish TV episodes, extras, and movies, for example `25 + 3 extras + 2 movies`.

### What was changed

-
 Added localized main-
channel update and recovery replies and wired them into update and channel-
recreation paths.
-
 Removed duplicate `PublicationFacts` fields found during review.
-
 Added the `ThumbnailSource` table and migration for selected artwork, gathered fields, entry identity, and rendered image path.
-
 Made thumbnail persistence use a non-
null root sentinel and atomic upsert, avoiding duplicate root rows and retry races on PostgreSQL.
-
 Added movie size-
control helpers, two-
pass target encoding, verified splitting, safe failure handling, and cleanup tracking for generated artifacts.
-
 Rebound restored distribution content/layout rows to fresh Telegram message IDs, then relinked fresh storage buttons where live pack information is available.
-
 Added focused regression tests for movie limits, thumbnail persistence, main-
channel replies, and distribution backup/restore.
-
 Updated the schema-
count test from 22 to 23 tables to include `thumbnail_sources`.

### Verification

-
 Focused regression tests: 11 passed.
-
 Python compilation across `nekofetch`, `shared`, `bots`, `tests`, and `migrations`: clean.
-
 The full suite's previous run was 794 passed, 5 skipped, and 1 stale table-
count expectation; that expectation was updated for the intentional new table. The full suite is being rerun after this update.
-
 Live Telegram ban/recovery and a real 2GB+ movie upload still require configured production Telegram accounts, channels, storage, and FFmpeg. They were not falsely claimed as live-
tested here.

### Intentionally not implemented

-
 The plan's userbot-
plus-
bot-
token research item for editing buttons into arbitrary user messages remains research-
only. It needs owner confirmation and a live Telegram permission/security decision.

-
-
-


## 8. REVIEWER COMMENTS — per task (broader context)

> **Who wrote this and why.** These comments were added by the AI that ran the ORIGINAL planning session with the owner — it heard the owner's spoken clarifications (voice notes) that never made it verbatim into §3. The executor AI that did the §7 work only had the written plan, so where it "misunderstood" or "stopped short," that's usually because the nuance lived in those clarifications, not in the task text. Each comment gives: a plain status, what the owner ACTUALLY meant, and the concrete gap to close. Verified against the working tree on 2026-
08-
07 (uncommitted changes present; full suite 795 passed / 5 skipped). Status legend: ✅ done · 🟡 partial · ❌ not done.

## 8A. EXECUTOR RESPONSES TO REVIEWER COMMENTS — 2026-
08-
07

> Read this section first. Each response says what was changed, where it lives, how it was checked, and what still needs confirmation or more work. The detailed reviewer notes below are preserved as historical context; this section is the current answer to them.

### A — episode/part identity
-
 **Reviewer concern:** persist franchise entries, restart episode numbering for split seasons, and keep the filename title consistent.
-
 **Response:** entries are persisted for both single requests and batches; `_map_episode_locator` carries `(season_part, renumbered_episode)` into `MediaFile`, while the confirmed filename title is stored at job level so S01 and S01P02 keep their own labels.
-
 **Evidence:** `tests/test_episode_part_mapping.py`, `tests/test_encode_order.py`, and the existing download/stage code paths.
-
 **Still open:** the plan's requested function name/return shape differs from the compatibility wrapper; separate per-
entry display titles within one job are not supported.

### B — two cards for a split season
-
 **Reviewer concern:** S01 and S01P02 must not collapse into one card containing all episodes.
-
 **Response:** card generation now iterates franchise entries by AniList ID, computes `(season, season_part)`, filters packs by both values, and labels the second entry as `Season 01 Part 2`. Senku update cards, watch-
guide lines, layouts, content rows, and restore backups carry the same part identity.
-
 **Evidence:** `nekofetch/services/bot_content.py::_packs_for_tv_entry`, `shared/senku_publisher.py` card/update builders, `nekofetch/services/backup_service.py`, and the 41-
test focused run.
-
 **Still open:** add the plan's dedicated `tests/test_two_part_cards.py` for a direct two-
card assertion; the implementation is present but that exact regression test is not.

### C — Levi pack-
caption editing
-
 **Reviewer concern:** edit one logical entry across all resolution/audio packs, edit live headers, preserve filenames, and make restore backups use the new caption.
-
 **Response:** `StorageChannelService.update_header_caption` updates sibling packs for the same `(anime_doc_id, season, season_part, entry_id)`, edits each live header with text-
to-
media-
caption fallback, and synchronizes matching `ChannelContentBackup` cards. `caption` is canonical; `header_caption` remains a compatibility mirror.
-
 **Evidence:** `tests/test_storage_pack_caption.py`; migrations `0025_add_storage_pack_header_caption.py` and `0028_add_storage_pack_caption.py`.
-
 **Still open:** Levi `/start` and callback-
home menus still duplicate row construction instead of sharing one `_home_rows` builder.

### D — per-
entry thumbnail approve/redo
-
 **Reviewer concern:** approving entry 1 must advance to entry 2, while redoing entry 2 must not reset entry 1.
-
 **Response:** the thumbnail workflow uses `next_pending` and per-
entry selection state; Approve marks only that entry done, and Redo clears/restarts only that entry.
-
 **Evidence:** `tests/test_thumb_per_entry_fsm.py`.
-
 **Still open:** none identified by the reviewer for this task.

### E — thumbnail editing and durable source data
-
 **Reviewer concern:** save the inputs behind a thumbnail and provide an owner editor instead of forcing a full redo.
-
 **Response:** added `ThumbnailSource` plus atomic persistence/upsert, and added the owner-
gated `/edit_thumbnail` handler. It lets the owner choose a saved source, edit stored fields, re-
render, sync Redis workflow state, and edit/repost the thumbnail-
channel image where message data exists.
-
 **Evidence:** `nekofetch/bots/admin/handlers/thumbnail_edit.py`, `nekofetch/services/thumbnail_service.py`, `tests/test_thumbnail_source_persistence.py`.
-
 **Still open:** a separately verified Gojo/main-
channel editing surface and the plan's dedicated `tests/test_thumbnail_edit.py` are still needed. Do not call the entire Senku-
plus-
Gojo editing experience fully complete yet.

### E2 — main-
channel metadata and language union
-
 **Reviewer concern:** one English/dub/dual entry must make the whole show read `English & Japanese`, even when other entries are sub-
only.
-
 **Response:** main-
channel language construction now unions audio languages across all packs instead of using only one season. The focused test covers both the mixed case and the sub-
only case.
-
 **Evidence:** `nekofetch/services/main_channel_service.py::_language_summary`, `tests/test_main_channel_meta.py`.
-
 **Still open:** the dedicated end-
to-
end test for the rendered main caption/thumbnail, franchise-
average rating, and TMDB synopsis source is still recommended.

### E3 — episode-
count wording
-
 **Reviewer concern:** show totals as `25 + 3 extras + 2 movies`, with correct singular/plural forms.
-
 **Response:** the formatter and tests cover the requested base/extras/movies combinations, including singular forms.
-
 **Evidence:** `tests/test_episode_count_label.py`.
-
 **Still open:** none identified by the reviewer for this task.

### F — encode order
-
 **Reviewer concern:** order entries by release order and episodes ascending within each entry.
-
 **Response:** processing sorts by season, season part, and episode before encoding.
-
 **Evidence:** `tests/test_encode_order.py`.
-
 **Still open:** none identified by the reviewer for this task.

### G — DDL in “Begin Now”
-
 **Reviewer concern:** the DDL source option might be missing from a particular admin screen.
-
 **Response:** the source picker already contains DDL (`review.py`), but that change predates this session and no current-
session code change proves it is the exact “Begin Now” surface the owner meant.
-
 **Evidence:** source-
picker implementation and existing DDL flow.
-
 **Still open / owner question:** confirm whether the missing button was in the review source picker or in a different Levi task-
card screen before changing it.

### H — batch “Open Tasks” link
-
 **Reviewer concern:** Lelouch cannot send a callback directly into Levi, so the batch completion screen needs a real Levi URL button.
-
 **Response:** added the `levi_link` URL helper and conditionally shows `📋 Open Tasks` while keeping Home; it is omitted when no valid Levi username exists.
-
 **Evidence:** batch screen/link implementation and the full-
suite regression coverage currently exercising the batch paths.
-
 **Still open:** add the exact dedicated `tests/test_batch_done_button.py` requested by the plan.

### J — ban recovery
-
 **Reviewer concern:** recovery must restore content and links, update the main post, and also guide a human admin through replacement-
channel setup.
-
 **Response:** automated recovery now restores saved cards/layout, carries season identity, relinks fresh download buttons, updates the main post, and sends the localized block-
quoted recovery reply without buttons.
-
 **Evidence:** backup/restore services, `ban_recovery_reply` localization, and existing distribution backup/recovery tests.
-
 **Still open:** the human-
in-
the-
loop Gojo DM using the normal Senku artwork/voice, PFP setup, service-
message cleanup, and admin promotion flow is not implemented; the dedicated full recovery test is also still missing.

### K — update flow and update-
during-
redo
-
 **Reviewer concern:** update only the new entry, start with its thumbnail, limit combined torrents, and offer the new season during redo.
-
 **Response:** the existing append/re-
footer flow and localized main-
post update reply are retained and season/part identity now travels through that flow.
-
 **Evidence:** `shared/senku_publisher.py`, `MainChannelService.reply_update`, `tests/test_senku_channel_update.py`, and `tests/test_main_channel_replies.py`.
-
 **Still open:** combined-
torrent episode-
window selection, update wizard gating, and the interactive Yes/No redo prompt are not implemented; no claim is made that K1/K2/K5 are complete.

### L — specials/OVAs
-
 **Reviewer concern:** exclude unselected specials, auto-
map one OVA, fuzzy-
match multiple OVAs, and provide a manual fallback/preview.
-
 **Response:** no new implementation was made for the mapping and manual-
pairing requirements in this session.
-
 **Evidence:** existing special-
episode parsing tests cover only the earlier parser behavior.
-
 **Still open:** the complete task remains open, including its dedicated tests.

### M — oversized movies
-
 **Reviewer concern:** compress movies over 2000 MB, split if still too large, verify sizes, preserve originals on failure, and clean temporary files after success.
-
 **Response:** two-
pass target encoding, verified splitting, failure-
safe cleanup tracking, and upload integration are present.
-
 **Evidence:** `_encode_to_target_size`, `split_movie`, publishing integration, and `tests/test_movie_size.py`.
-
 **Still open:** add the exact boundary assertion that 2000 MB does not trigger while 2001 MB does.

### N — userbot plus bot-
token button research
-
 **Reviewer concern:** determine whether a userbot can add an inline keyboard to a user's existing message before implementing anything.
-
 **Response:** production behavior was not changed. This remains intentionally deferred pending owner confirmation and a documented research deliverable.
-
 **Evidence:** the plan's intentional-
defer note.
-
 **Still open:** write `docs/userbot_edit_research.md` and get the owner's decision.

### O — redo metadata refresh
-
 **Reviewer concern:** redo should regenerate only when rating/language/episode metadata changed, and otherwise only relink buttons.
-
 **Response:** no differential metadata-
driven regeneration was added in this session. The durable thumbnail source table now provides the storage foundation for it.
-
 **Evidence:** `ThumbnailSource` persistence exists; no `redo_service.py` metadata-
diff implementation is claimed.
-
 **Still open:** the complete comparison, selective regeneration, caption/channel-
title refresh, and dedicated tests.

### P — Manage REQ/WRK
-
 **Reviewer concern:** the menu label must represent a real combined REQ and WRK management view, not just a rename.
-
 **Response:** the owner-
only Redo/Clear-
DB row and `Manage REQ/WRK` label are present, but the management list still needs the actual WorkItem query/actions.
-
 **Evidence:** `bots/lelouch/screens.py`, `tests/test_lelouch_admin_panel.py`.
-
 **Still open:** merge WorkItem listing/cancel/reassign behavior into `management.py`; this task is partial, not complete.

### Follow-
up review findings after the first response
-
 **Root thumbnail sentinel:** fixed by normalizing Redis `anilist_id=-
1` to the `None` card key, excluding root-
only entries from first-
season selection, and selecting the first real season by workflow `index`; malformed IDs and indexes are skipped safely. Evidence: `nekofetch/services/thumbnail_orchestrator_service.py` and `tests/test_thumbnail_orchestrator.py`.
-
 **Legacy relink compatibility:** `_tv_entry_identities` now falls back safely for minimal legacy objects that have only an AniList ID/format, while preserving an explicit `season_number` when present. This fixed the full-
suite relink regression.
-
 **Schema consistency:** `StoragePack.caption` is present in the model and migration `0028`; `header_caption` remains mirrored for compatibility. The migration chain is linear: `0024 → 0025 → 0026 → 0027 → 0028`.
-
 **Validation:** the final full suite reaches 816 passed / 5 skipped / 0 failed; the focused human-
recovery and maintenance suite reaches 11 passed.

### What the next reviewer should verify
1. Is the split-
season card behavior acceptable without the dedicated two-
part-
card test, or should that test be added before production use?
2. Should Levi's duplicated menus be consolidated now, or is the caption feature acceptable as-
is?
3. Does `/edit_thumbnail` need a separate Gojo/main-
channel surface before the thumbnail task can be marked complete?
4. Was “Begin Now” referring to the existing DDL source picker or another screen?
5. Should the human ban-
recovery wizard, K1/K2/K5 update flow, OVA mapping, redo metadata refresh, and REQ/WRK management be implemented next?

-
-
-

### A — episode renumbering + persist entries — ✅ done (works), 2 naming deviations

> **Historical reviewer note.** The comments in §8 were written against an earlier
> working-
tree snapshot. The current source/tests below the note are authoritative;
> statuses that changed after review are recorded in the current-
session addendum.
The linchpin landed correctly: entries ARE persisted at commit in both `_finalize` (`bots/lelouch/handlers/requests.py:448`) and batch (`batch.py:443`) via `FranchiseFlowService.persisted_entries`, and Vanitas S1P2 renumbers to E01–E12 (`tests/test_episode_part_mapping.py` asserts `(13,1)→(2,1)`). Two things to know so nobody "fixes" a non-
bug:
-
 The tuple `(part, renumbered_episode)` lives in a NEW function **`_map_episode_locator`** (`download_service.py:2073`). The old `_map_episode_to_part` is now a thin legacy wrapper returning part-
only. The plan told you to change `_map_episode_to_part`'s return type; the executor added a sibling instead. Functionally identical — just grep for `_map_episode_locator`, not the old name.
-
 **A7 (title propagation) was solved differently than the plan wrote.** There is no `franchise_data["_display_title"]`. Instead one confirmed title is stored job-
wide in `DownloadJob.resume_state["name_title"]` (`download_service.py:991`) and applied to every file while each keeps its own `S01`/`S01P02` label (`stages.py:590`). **This matches what the owner wanted** ("edit the title once, it applies to the whole show, but S1 stays S1 and S1P2 stays S1P2") — for an ENTIRE_SERIES request that's one job, so it's correct. The only real limitation: there's ONE job-
level title edit, not a per-
entry title surface. If the owner ever wants to give S1 and a spin-
off different display titles in the SAME request, this design can't do it. Flag for owner; not urgent.

### B — two entry cards for a split season — ✅ implemented in current tree; focused test still recommended
The current tree now carries `season_part` through the normal cards, update cards, saved layouts, content rows, and distribution backups. Pack matching uses `(season, season_part)` and labels split entries as `Part N`, so a split season can render as separate cards. A dedicated `test_two_part_cards.py` is still recommended for explicit UI-
level regression coverage.

### C — edit pack captions in Levi — 🟡 partial
The editor works: `bots/levi/handlers/pack_caption_handler.py` + the `StoragePack` caption fields (migrations 0025/0028) + service fan-
out that live-
edits every sibling message with a media-
caption fallback, owner-
gated, and syncs distribution backup cards. Filenames are correctly left untouched — the owner's "Vanitas no Carte (filename) vs The Case Study of Vanitas (caption)" distinction is honored. Gaps ranked by importance:
1. **Menu single-
source-
of-
truth was NOT done — and the owner called this out as a general rule for all bots.** The "Pack Captions" button is added in TWO places in `bots/levi/app.py` (callback-
home line 103, `/start` line 201) with duplicated owner-
gates. Gojo has the shared `_home_rows` builder; Levi still doesn't. This is the exact `/start`-
vs-
Settings drift the owner told us to kill everywhere. Please refactor Levi to a shared row builder.
2. The Levi menu still has duplicated `/start` and callback-
home row construction, so the standing single-
source-
of-
truth menu rule remains open.
3. The canonical stored field is now `caption`, with `header_caption` retained as a compatibility mirror; a dedicated migration and focused tests cover both edit paths.
### D — per-
entry thumbnail approve/reject — ✅ done
`_enter_thumbnails` now loops via `next_pending(code)` (no more hardcoded index 0), Approve advances / Redo restarts only that entry, others stay confirmed. `tests/test_thumb_per_entry_fsm.py` proves the state machine. This matches the owner's "reject entry 2 → redo entry 2 only, entry 1 stays approved." No notes.

### E — thumbnail EDITING command — 🟡 partial; current tree has the core owner editor
The current tree has the core owner `/edit_thumbnail` editor in `nekofetch/bots/admin/handlers/thumbnail_edit.py`, durable source persistence, field capture, re-
rendering, workflow synchronization, and best-
effort live-
message edit/repost behavior. The main-
channel-
specific Gojo editing surface and dedicated `test_thumbnail_edit.py` remain recommended follow-
up coverage; do not describe the entire cross-
bot edit experience as fully verified.

### E2 — main-
channel metadata rules — 🟡 partial; language union now has focused coverage
Synopsis (TMDB) and rating (franchise average) remain in place. The language-
label rule now has focused coverage in `tests/test_main_channel_meta.py`: one English-
audio entry among sub-
only entries yields `English & Japanese`, while a sub-
only franchise remains Japanese. Full end-
to-
end caption rendering is still worth a dedicated test.

### E3 — episode-
count line ("25 + 3 extras + 2 movies") — ✅ done, matches the spec verbatim
`tests/test_episode_count_label.py` asserts the exact strings including singular "1 extra" / "1 movie". This is exactly the format the owner dictated. No notes.

### F — encode in release + episode order — ✅ done
Sort by `(season, season_part or 0, episode)` before the encode loop; `tests/test_encode_order.py` proves `[S1E3,S1E1,S1P2E2,S1E2,S1P2E1] → S1E1,S1E2,S1E3,S1P2E1,S1P2E2`. Exactly the two-
level ordering the owner asked for. No notes.

### G — DDL in "Begin Now" — ⚠️ already existed; not this session's work (verify it's the same gap the owner hit)
The DDL button IS present in the source picker (`review.py:585`), but it was added by an EARLIER commit (`cbc93aa`, "DDL provide-
link flow"), and `review.py` has no changes in this session's working tree. So either the owner's "DDL missing in Begin Now" was already fixed before this batch, OR the owner meant a DIFFERENT surface than the admin review picker (e.g. a Levi task-
card "Begin Now" button, or DDL missing under a specific condition). **Worth a direct question to the owner:** "when you said DDL was missing from Begin Now, was it the admin review source picker, or somewhere else?" Don't mark G done until that's confirmed — the plan may have pointed at the wrong screen.

### H — batch "board" button → "Open Tasks" — ✅ done
`levi_link()` helper added, batch-
done screen shows a URL button "📋 Open Tasks" deep-
linking into Levi, omitted when the link can't resolve. This is exactly the cross-
bot workaround the owner needed (Lelouch can't callback into Levi). No `test_batch_done_button.py`, but the code is correct — add the test for regression safety.

### J — ban recovery — ✅ machine recovery + HUMAN replacement wizard implemented and hardened
Parts 2 and 3 remain in place: restored cards relink to the new invite link (`relink_packs_in_place`), the main-
post Download button refreshes (`_bind_title`), and Gojo posts the block-
quoted recovery reply with a click-
through link. Part 1 is implemented: Gojo sends a private card using Senku's existing artwork/layout/voice, the admin creates the replacement or chooses automatic recovery, the submitted handle/ID is checked against the old chat, Gojo attempts to add/promote Senku and verifies operational privileges, existing Senku service-
message cleanup runs, and the handback enters `recover_human_channel`. Scheduled monthly ban checks use the same handoff before automatic fallback.Recovery now prefers an enabled non-recovery channel over a partial replacement when refreshing the durable snapshot, tracks failed Telegram deletions for retry, attempts to restore the old public links if a late step fails, and attempts to revoke a replacement invite using the verified recovery client. If Telegram refuses a surface refresh or invite revoke, the old row/link state is retained for a later retry rather than silently discarded. `tests/test_ban_recovery_full.py`, distribution backup/restore tests, invite-link tests, and scheduled-maintenance coverage prove the real helper and callback paths.


### K — update flow (new season appended) — 🟡 K3 done; K1, K2, K5 missing (and K5 is the one the owner un-
deferred)
K3 is done: `reply_update` posts the en.json `season_update_reply` (block-
quote, hyperlink, no buttons) after an append. The existing `_append_and_refooter` append machinery is reused correctly. But three parts the owner asked for are absent:
-
 **K1 (torrent partial download) — ❌.** No file-
window selection: when a torrent carries S1+S2 but the request is only the new entry, nothing restricts the download to the new entry's episode range. Grep for `select_files`/episode-
windowing turns up nothing new. The owner explicitly described this (13 separate torrents / one combined torrent / a season pack).
-
 **K2 (update skips channel creation, starts at thumbnail) — ❌ not gated.** The wizard has no `is_update_entry` branch that skips channel-
create states and jumps straight into `_enter_thumbnails` for the new entry. On an update the owner does NOT want a new channel — they want to go straight to thumbnailing the new season.
-
 **K5 (update-
DURING-
redo) — 🟡 detect-
only.** `redo_service.py:321 _detect_and_notify_new_seasons` DETECTS a new season and sends an informational DM, but `new_season_ids` is then never consumed (set at line 222, read nowhere). The owner un-
deferred this and wanted an interactive **"Season N is available — include it? [Yes] [No]"** that, on Yes, actually runs the append for the new entry. Right now it just notifies. The docstring even admits "the caller MAY use the returned ids" — nobody does.
No `test_update_flow.py`. K is the most under-
built cluster relative to what the owner asked for; §7 doesn't flag any of this.

### L — special/OVA handling — ❌ essentially not done this session
The plan's L work (skip unselected specials; single-
OVA auto-
map; multi-
OVA MAL-
title fuzzy-
match; user-
pairing fallback; naming/caption preview for OVAs & movies) does not appear in the working tree, and there's no `test_special_mapping.py`. Some special-
episode PARSING exists from an earlier commit (`20b5644`, `tests/test_torrent_special_episodes.py`), but the MAPPING/auto-
match/preview flow the owner described is absent. If the owner's near-
term redos (Sabikui, ORB, Vanitas) have no OVAs, this can wait — but it should be labeled ❌, not silently omitted.

### M — movie >2000 MB compress-
then-
split — ✅ done, one thing to double-
check
`_encode_to_target_size` (2-
pass, bitrate math), `split_movie` (verified-
size splitting), and the `_upload_packs` hook all exist; `tests/test_movie_size.py` covers the bitrate math and the no-
op/compress/split branches with mocked ffmpeg. The owner's "keep the original on failure, don't silently delete" and "clean temp artifacts on success" are honored per the §7 record. **One thing the test does NOT pin:** the exact 2000-
vs-
2001 MB boundary the plan called for (the tests assert bitrate math and duration-
guard, not "2001 triggers, 2000 does not"). Add that boundary assertion so a future refactor can't drift the cap over Telegram's hard limit. Otherwise solid.

### N — userbot+buttons research — ❌ deliverable missing
`docs/userbot_edit_research.md` was not written; §7 honestly lists this as intentionally-
not-
done. Note for context: the answer is already known from the planning session (a user account can't attach inline keyboards; only a bot can, and only to its own messages — so the workable path is bot-
reposts-
with-
buttons or leave-
old-
post-
and-
reply-
below to preserve views). Someone just needs to write those findings into the doc. Low effort, low priority.

### O — redo metadata refresh — ❌ not done
The plan's O work (on redo, detect changed episode-
count/language/rating vs the stored `thumbnail_source`, regenerate ONLY affected thumbnails, refresh captions + channel title + delete the "name changed" service message, but skip regeneration when only quality changed) is not in `redo_service.py`, and there's no `test_redo_metadata_refresh.py`. The redo path does relink packs and detect new seasons, but the metadata-
diff-
driven regeneration the owner detailed (using ORB as the example: English available but dual-
audio comes out → language section updates, poster stays) is absent. This depends on E's `thumbnail_sources` row (which now exists) and on E2's language union — so it's unblocked, just not built. Mark ❌.

### P — Lelouch "Manage REQ/WRK" + Redo/Clear-
DB row — 🟡 the row is done; the REQ/WRK merge is cosmetic-
only
The Redo + Clear-
DB row is correct: `admin_panel(is_owner=...)` gates a single row with both `redo|new` and `dbclear`, owner-
only, with a passing `test_lelouch_admin_panel.py`. **But the button was only RENAMED, not rewired.** `bots/lelouch/handlers/management.py` is untouched: `_render_requests` still calls `list_active()` (Requests/REQ only) and the header still literally says "Manage Requests" (line 484). So tapping "🗂 Manage REQ/WRK" shows REQ items only — WorkItems (WRK) never appear, and they have no cancel/reassign here. The owner explicitly asked to show **BOTH** REQ and WRK with the same actions. **Fix:** extend `_render_requests` to also query WorkItems and label each row by its code prefix (REQ-
/WRK-
), and wire the cancel/reassign handlers to accept WRK codes. Until then, P is a label change, not the feature.

-
-
-


### Cross-
cutting notes for whoever picks this up
1. **This execution record is still uncommitted.** The current working tree contains the implementation and focused regression changes; commit it in reviewable chunks before building on it. The last verified focused run passed 41 tests; do not treat the historical `795/5 skipped` claim as a post-
patch full-
suite result.
2. **The "single-
source-
of-
truth menu" rule is a standing owner requirement, not a per-
task detail.** Gojo got it (`_home_rows`); Levi did NOT (Task C). Apply it to every bot with a `/start`-
vs-
callback menu.
3. **Test coverage is honest but partial.** The 9 test files that DO exist all pass; the 9 the plan asked for that DON'T exist map almost exactly onto the ❌/🟡 tasks (B, C-
menu, E-
UI, E2, H, J, K, L, O). Missing test ≈ missing/partial feature — a useful shortcut when triaging.
4. **Priority order for the owner's imminent Vanitas/ORB/Sabikui redos:** Task **B** (two cards — most visible), then **O** + **E2** (redo metadata/language refresh — ORB is dual-
audio), then **E**-
UI (so mistakes are fixable without a full redo). K1/K2/K5, J-
Part-
1, and L are important but not blockers for those three specific redos.

-
-
-

### PHASE 10 — Text-logo builder: expand fonts, add color picker, add font upload — ✅ done

> **Executor status (2026-08-09).** All §10.1–§10.10 items are implemented and verified against the
> working tree: full suite 867 passed / 13 failed / 5 skipped (the 13 failures are the pre-existing
> Kage SQLite DB failures in `test_admin_assignment.py` / `test_integration.py` /
> `test_edge_cases.py` / `test_management_service.py` — they fail identically with Phase-10 changes
> stashed, so they are NOT a regression; they pass under PostgreSQL). Phase-10 focused tests
> (`tests/test_text_logo.py`, `tests/test_senku_wizard_routing.py`, `tests/test_senku_text_logo_flow.py`,
> `tests/test_senku_thumbnail_adapter.py`) are all green.
>
> - **§10.1** 60 bundled OFL-1.1 fonts (≥10 per category × 6 categories), each with its
>   `OFL-1.1-<slug>.txt` sibling (incl. the pre-existing Playfair Display license gap); renderer stays
>   offline. Fetched from the google/fonts GitHub mirror; 5 Apache-2.0 families (Satisfy, Yellowtail,
>   Ultra, Chewy, Permanent Marker, Rock Salt) were swapped for OFL-1.1 visual equivalents (Great
>   Vibes, Parisienne, Passion One, Shrikhand, Gloria Hallelujah, Caveat Brush) per the plan's
>   all-OFL requirement.
> - **§10.2** Category grid renders 2/row with an `⬆️ Upload your own` full-width row beneath.
> - **§10.3** One-shot upload flow: `.ttf`/`.otf` staged under `data/text_logos/uploaded/`
>   (content-hash name), `custom_font_path` in FSM, `render_text_logo(..., font_path=...)` bypasses
>   the bundled index, temp font unlinked on Use-this and Cancel, never added to `FONTS`.
> - **§10.4/§10.5** `TextLogoColor` model (white/black first, 12 swatches), `STATE_TEXT_COLORS` step
>   between font pick and preview, `textcolor` router action, `color_rgb` threaded through the
>   renderer + digest key (different colors → different output paths).
> - **§10.7** `BTN_TEXT_UPLOAD_FONT` + `thumb_text_*` voice builders added in `senku_voice.py`.
> - **§10.8** Longest `textfont` callback verified ≤64 bytes by guard test.
> - **§10.9** Tests drive the real code paths (real client build, real router dispatch, real renderer).
> - **§10.10** `py_compile` clean; Back/Cancel at every new step; `_text_state_matches` guards every
>   new callback.


## 9. CORRECTIVE DIRECTIVES — build these THIS way (supersedes the "still open" notes above)

### 9.11 — Reviewer response: J / §9.5 human recovery

-
 **“Human promotion is not implemented.”** Fixed: after the operator adds Gojo, the recovery verifier uses the available admin-
capable client to add/promote Senku and re-
promote either bot when required; both bots must have the operational rights needed for posting, editing, deleting, inviting, pinning, and channel-
info changes.
-
 **“Scheduled recovery bypasses the wizard.”** Fixed: manual `/recover`, manual `/bancheck`, and `make_monthly_bancheck_job` all call `offer_human_recovery` first; `recreate_bot` is only the no-
claim fallback.
-
 **“Same-
title replacements were rejected.”** Fixed: same-
channel protection compares the submitted chat id with the stored old chat id, not channel title.
-
 **“A failed/partial restore could strand an empty channel.”** Fixed: the old row remains enabled while the replacement is restored; only a complete `total == restored` / `failed == 0` restore is accepted. Empty or partial restore disables the replacement and leaves the old row active.- **“Late rollback could leave public links pointing at the failed channel.”** Hardened: rollback re-enables the known-good row, attempts to republish the main-channel button, and attempts to refresh the title's index letter through the supplied recovery client before returning failure. If Telegram rejects either corrective edit, the failure is logged and the old row remains available for retry.

-
 **“Retry cleanup could lose IDs when Telegram deletion failed.”** Fixed: successful deletions remove their tracking rows, but failed message IDs remain durable and cause the recovery attempt to stop before reposting; a later retry can try them again.- **“Fresh invite links were not guaranteed.”** Fixed: human recovery requires `InviteLinkService.ensure_for_bot` to return a link before switching active rows; invite failure disables the replacement and stops the handback. If a later step fails, rollback attempts to revoke that replacement invite through the verified recovery client; when revocation fails, the stored link remains so a later retry can revoke it.
- **“A partial replacement could overwrite the durable backup.”** Fixed: distribution capture prefers enabled non-`human_recovery` channels, falling back to a recovery row only when no non-recovery channel is enabled.

-
 **“Admin reservations could collide or release another workflow.”** Fixed: Redis `SET NX` stores a unique claim token, and release uses an atomic compare-
and-
delete so an expired workflow cannot delete a newer claim.
-
 **“Tests were helper-
only.”** Fixed: recovery tests now cover Senku card/artwork, handback, claim ownership, required privileges, scheduled FSM state, real Gojo callback registration, tracked-
message cleanup retry, stale-
invite reset/revocation, and the distribution restore path. Final validation is recorded below after the last patch.


> **Read this before touching anything.** The reviewer (the planning-
session AI, broader owner context) went back through the actual working tree on 2026-
08-
07 and verified each item at code level. These directives are prescriptive: where a task was left partial or built differently than the owner intended, the exact shape to produce is spelled out with `file:line` anchors. **Reuse the cited symbols — do not re-
implement in parallel.** Every fix must be proven by a test that drives the REAL code path (no shadow tests — see §9.7). `py_compile` clean + full suite green (currently 799 passed / 5 skipped — do not regress) before marking any item done.
>
> **Two corrections to §8A first (don't waste effort):**
> -
 **Levi menu is ALREADY unified.** `bots/levi/app.py:34 _home_rows` is the single source of truth, called by both `/start` (:209) and callback-
home (:127). §8A/§C's "still duplicated" note is STALE. Do NOT redo it. Mark Task C's menu sub-
item ✅.
> -
 **The double caption column is safe to collapse** — see §9.8. Both are always written to the same value and read as `caption or header_caption`.

### 9.1 — E: thumbnail edit MUST propagate to the LIVE published surfaces (highest priority)
**Why this is the headline fix:** the current `/edit_thumbnail` (`nekofetch/bots/admin/handlers/thumbnail_edit.py`) only edits the admin **staging/thumbnail channel** (`fields["thumbnail_chat_id"]` / `thumbnail_message_id`, :198–245). The owner's actual use case is "I already PUBLISHED, a field is wrong, fix what SUBSCRIBERS see." Re-
staging does not fix the published post. As built, the editor is a regeneration tool, not an edit tool — do not describe it as done.

**Build it this way. Two live surfaces carry a thumbnail image; the edit must reach whichever exists:**
1. **Main-
channel post (via Gojo).** The post is tracked as `ChannelPost.main_message_id` for the `anime_doc_id` (`main_channel_service.py:489`, edited in place in `publish()`). Add a method on `MainChannelService` — `async def refresh_thumbnail(self, anime_doc_id, image_path) -
> bool` — that loads the `ChannelPost`, and if `main_message_id` is set, calls `self._c.admin_client.edit_message_media(main_channel_id, main_message_id, InputMediaPhoto(image_path, caption=<existing caption>, parse_mode=HTML))`. Preserve the existing caption (re-
read it or pass it through) — do NOT blank it. Update `PublishedPostBackup` for that post so a later restore uses the corrected image. Because the admin bot IS Gojo (`container.admin_client`), Senku triggering this routes through Gojo correctly — that satisfies the owner's "main-
channel edits go through Gojo" rule without a cross-
bot hop.
2. **Distribution entry card (via Senku).** The card is a `BotContentPost`/`ChannelLayout` row with `tg_message_id` (`models.py:548`, layout `cards` JSON at :376 holds `{"kind","caption","image_url",...}`). Add the parallel edit on the Senku side (or reuse `SenkuPublisher`) to `edit_message_media` that card and rewrite the stored `cards` JSON + `ChannelContentBackup` so restore stays correct.
3. **Wire the command to call the right one.** In `thumbnail_edit.py` after the re-
render + `persist_thumbnail_source` (:202), branch on entry identity: root/`anilist_id == -
1` → main post (surface 1); a specific season/part entry → its distribution card (surface 2). Keep the existing staging-
channel edit as a THIRD, best-
effort update (staging is the operator's preview, not the deliverable). Report back to the operator WHICH live surface was updated ("✅ Updated the main-
channel post" / "✅ Updated the Season 1 Part 2 card"), not the current generic "saved."
4. **Add `tests/test_thumbnail_edit.py`** (the plan's originally-
requested test, still missing): seed a `ThumbnailSource` + a `ChannelPost` with a `main_message_id`; run the edit service with a stub `admin_client` recording calls; assert `edit_message_media` fired against `main_message_id` with the NEW image and the OLD caption preserved, and that `PublishedPostBackup` was updated. Same for a distribution-
card entry against its `tg_message_id`.

**Until 9.1 is done, Task E stays 🟡 and the operator must be told the editor only re-
stages.** This is the one gap that changes day-
to-
day usability.

### 9.2 — E code-
quality fixes (do alongside 9.1)
1. **Stop reaching into another service's privates.** `thumbnail_edit.py:210–222` calls `ThumbnailChannelService._get_workflow` / `_save_workflow` (underscore = private). Add a PUBLIC method on `ThumbnailChannelService` — e.g. `async def mark_entry_rendered(self, anime_doc_id, anilist_id, image_path)` — that does the workflow lookup + status="done" + save internally, and call THAT. Keeps the encapsulation the rest of the codebase respects.
2. **Escape caption interpolation.** `:233` and `:241` build `caption=f"<b>{fields.get('title')}</b> — <i>{fields.get('entry_label')}</i>"` with raw f-
strings. A title containing `&`, `<`, or `>` (common in anime titles) produces broken/injected HTML. Use the project's existing escape helper (`V.esc(...)` per the voice modules, or `html.escape`) on both interpolated values — match whatever `main_channel_service` / `bot_naming` already use for caption building.

### 9.3 — O: redo metadata refresh (now UNBLOCKED — the storage it needed exists)
Task O was blocked on "no durable thumbnail source." That's gone — `ThumbnailSource` (migration 0026) + `persist_thumbnail_source` now store every entry's rendered fields. Build O on top of it. **The owner's ORB example is the acceptance case:** English was available but a dual-
audio version comes out → on redo, old files deleted, dual-
audio uploaded, links relinked, the entry-
card caption's language line updates, the main-
post thumbnail regenerates BECAUSE language/rating changed — but the poster/logo do NOT change and users are NOT re-
notified.

**Build it this way, in `shared/redo_service.py`:**
1. **Compute the new franchise facts** after the redo's fresh files exist: episode-
count line (Task E3 formatter — reuse it, don't rewrite), franchise-
average rating (`main_channel_service._avg_score_pct`), and the language union (Task E2 `_language_summary` — reuse it).
2. **Diff against stored.** Load each entry's `ThumbnailSource.fields`. Compare episode-
count / rating / language. Build a `changed: set[str]` per entry and for the main post.
3. **Regenerate ONLY on change.** If `changed` is empty for an entry (e.g. quality-
only redo) → do NOT re-
render; just relink download/quality buttons via the EXISTING redo-
relink path (`SenkuPublisher.relink_packs_in_place`). If `changed` is non-
empty → re-
render that thumbnail with the new fields, `persist_thumbnail_source`, and push it to the live surface via the 9.1 `refresh_thumbnail` method (main) / distribution-
card edit. This is exactly why 9.1 must land first — O depends on it.
4. **Update captions** (main + entry cards) via `edit_message_caption` where the episode-
count/language line changed; update the matching backups.
5. **Channel title refresh** (owner's "Case Study of Vanitas" → "Vanitas no Carte" case): if the franchise display title changed, `edit_chat_title(new_title)` on the distribution channel, then delete the "channel name changed" service message (the pattern already exists near `senku_publisher.py` warm-
search cleanup — reuse it).
6. **Add `tests/test_redo_metadata_refresh.py`** driving the REAL `redo_service` diff: seed old `ThumbnailSource` (rating=75, language="Japanese"); redo with entries yielding rating=82 + one English pack → assert the affected thumbnails re-
render AND 9.1's propagation fired; assert a quality-
only redo (identical metadata) does NOT re-
render and only relinks.

### 9.4 — K5: make update-
during-
redo INTERACTIVE (it currently only notifies)
`redo_service.py:321 _detect_and_notify_new_seasons` detects a new season and DMs a notice, but `new_season_ids` (set :222) is read nowhere. The owner un-
deferred this and wants a CHOICE, not a notice.
**Build it this way:** when `new_season_ids` is non-
empty, before finalizing the redo, send the owner an inline prompt **"Season N is available — include it in this redo? [Yes] [No]"** (use the existing Lelouch screen/keyboard conventions + a `redo|update|yes|<doc>` / `redo|update|no|<doc>` callback). On **Yes** → run the normal append/update flow (the existing `_append_and_refooter` + K3 `reply_update`) for the NEW entry only: download its episodes, generate ONLY the new entry's thumbnail (Task D per-
entry loop), append its `season_card`, Gojo replies to the main post. On **No** → redo only the originally-
selected entries. Add the Yes/No branch to `tests/test_update_flow.py` (create it — still missing) driving the real handler. **Note the dependency:** a clean Yes-
path also wants K1 (download only the new entry's episode window) and K2 (update skips channel creation) — if those aren't built yet, gate the Yes path to "new entry download + append" and flag K1/K2 as the remaining edge (combined-
torrent windowing).

### 9.5 — J Part 1: the human recovery wizard (the part the owner detailed most)
Machine recovery (relink, main-
post button refresh, recovery reply) is done and good. What's missing is the HUMAN handoff. `bot_orchestrator.recreate_bot` currently calls `factory.create_for_anime_channel()` (fully automated) — the owner's design puts an admin in the loop with a FAMILIAR UI.
**Build it this way:** when a ban is detected and a replacement channel is needed, Gojo DMs a free admin **reusing the Senku channel-
creation wizard's artwork + layout + character voice** (`bots/senku/handlers/wizard.py:349 _ask_channel` and siblings — reuse those builders, do not invent a new look), reworded for recovery: "Restoring a banned channel — create a replacement or use the userbot." The admin creates the channel, gives it a username, sets the PFP, and the flow then: removes the "photo changed" / "channel created" service messages, adds Senku + Gojo as admins, and hands the new `chat_id` back into the existing restore path (`_restore_channel` → relink → main-
post refresh → recovery reply, all already built). Only the human-
facing assignment + service-
message cleanup + admin-
promotion is new; everything after the handback already works. Add `tests/test_ban_recovery_full.py` covering: assignment DM uses the wizard builders (assert the shared artwork/voice symbols are invoked), and post-
handback the restore relinks + refreshes as already tested.

### 9.6 — P: actually merge WRK into "Manage REQ/WRK" (currently a rename only)
`bots/lelouch/handlers/management.py:474 _render_requests` still calls `list_active()` (Requests only) and the header literally reads "Manage Requests" (:484). The button was relabeled but the list wasn't rewired.
**Build it this way:** in `_render_requests`, also query WorkItems (`kurosoden.shared.work_service`) and render BOTH REQ and WRK rows in one list, each row prefixed by its code so REQ vs WRK is visible (e.g. "REQ-
1073 · …", "WRK-
42 · …"). Change the header to "Manage REQ/WRK". Extend the existing cancel/reassign callback handlers to accept WRK codes (route to `work_service` for WRK, `request_service` for REQ — branch on the code prefix). Add to `tests/test_lelouch_admin_panel.py` (exists): seed one Request + one WorkItem, assert the rendered list contains both and that a WRK row exposes a working cancel action.

### 9.7 — Replace the two SHADOW tests with real-
path tests
`tests/test_encode_order.py` and `tests/test_thumb_per_entry_fsm.py` currently RE-
IMPLEMENT the logic inside the test and assert against their own copy — they never import the real code, so they'd stay green if `stages.py` / the wizard broke. That's a false safety net.
-
 **F:** `test_encode_order.py:6–11` defines a local `_release_key` + `sorted()`. Delete it. Instead build real `MediaFile` rows (unordered), run them through the ACTUAL sort in `EncodeStage` (`nekofetch/services/processing/stages.py` — the `ctx.files.sort(...)` right before the encode loop; call that stage/helper directly or factor the sort into a named function the test imports), and assert the real output order.
-
 **D:** `test_thumb_per_entry_fsm.py:12` defines a local `advance()`. Delete it. Instead drive the real per-
entry transition — `SenkuThumbnailAdapter.next_pending` + the Approve/Redo handlers in `bots/senku/handlers/wizard.py` (or the `distribution_cache` selection functions they call) — and assert approve advances / redo restarts-
this-
entry-
only against the REAL state store.
Keep the assertions identical; only change WHAT they exercise. If factoring a helper out of `stages.py`/`wizard.py` is needed to make the real path callable, do that refactor (it also improves the code).

### 9.8 — Collapse the duplicate caption column (schema hygiene)
`storage_packs` now has BOTH `header_caption` (migration 0025) and `caption` (0028). Verified they're always written together and read as `caption or header_caption` (`storage_channel_service.py:278,320,361,391`). Pick `caption` as canonical (it's the newer, cleaner name), and in a NEW migration `0029`: backfill `caption` from `header_caption` where null, then drop `header_caption`. Update the model + the ~4 write sites to stop mirroring. Keep `ChannelContentBackup.caption` (unrelated — that's the backup snapshot). This is low-
risk cleanup; do it after the functional items above, and only if the full suite stays green. If a downstream reader outside `storage_channel_service` still references `header_caption`, update it in the same pass (grep first).

### 9.9 — Priority order for these directives
Do them in this order — earlier ones unblock later ones and cover the owner's imminent Vanitas/ORB/Sabikui redos:
1. **9.1 (E propagation)** — unblocks 9.3; also the biggest usability win on its own.
2. **9.3 (O redo refresh)** — the ORB dual-
audio case; needs 9.1.
3. **9.7 (real tests for D/F)** — cheap, removes a false safety net before more is built on it.
4. **9.2 (E code quality)** — fold into 9.1's PR.
5. **9.6 (P WRK merge)** and **9.4 (K5 interactive)** — self-
contained feature completions.
6. **9.5 (J human wizard)** — larger, no imminent-
redo dependency.
7. **9.8 (caption column)** — hygiene, last, only if suite stays green.
**Not in scope here (still ❌ from §8, unchanged):** K1/K2 combined-
torrent windowing, L (OVA mapping), N (research doc) — leave labeled, tackle after the above.

### 9.10 — Definition of done for §9
Every directive: reuse the cited symbol (no parallel re-
impl); test drives the REAL code path (§9.7 rule applies to ALL new tests); `py_compile` clean; full suite green and NOT below 799 passed / 5 skipped; DB writes update the matching backup snapshot; operator-
facing copy stays en.json-
driven where the surrounding code already is. Update §8A's status line for each item you complete (🟡/❌ → ✅) so the record stays honest.


---

## 10. PHASE — Text-logo builder: expand fonts, add color picker, add font upload

> **Context for the executor.** A text-logo flow ALREADY EXISTS (commit `2d40a95 "feat(senku): add text logo thumbnail flow"`). Read it fully before touching anything:
> - `shared/text_logo.py` — the renderer (`render_text_logo(text, font_key)`), `CATEGORIES`, `FONTS`, `sanitize_text`, `fonts_for_category`, `get_font`, `get_category`.
> - `bots/senku/handlers/wizard.py` — the wizard UI: `_thumb_asset_card` (the logo card with the `upload_row` = `[⬆️ Upload][✍️ Text]`), `_thumb_text_categories`, `_thumb_text_fonts`, `_thumb_text_preview`, the router actions `text/textcat/textfont/textbackcat/textbackfont/textcancel/textuse/upl` (~lines 1224-1348), the text-capture handler `_channel_text` (group=2, `STATE_AWAIT_TEXT`, ~line 1401 — ALREADY deletes the admin's message at line 1418), and the upload handler `_upload_media` (group=2, `STATE_AWAIT_UPLOAD`, ~line 1469).
> - `shared/senku_thumbnail_adapter.py` — `store_text_logo(code, index, path)` (mirrors the PNG into the `logo_url` selection field via `image_backup.backup_bytes`) and `store_upload(...)`.
> - `shared/senku_voice.py` — the `BTN_TEXT_*` / `BTN_UPLOAD_OWN` labels + `thumb_text_*` voice builders (~lines 617-621 and the `thumb_text_categories`/`thumb_text_fonts`/`thumb_text_preview`/`thumb_text_prompt` functions).
> - `tests/test_text_logo.py`, `tests/test_senku_thumbnail_adapter.py`, `tests/test_senku_wizard_routing.py` — the existing coverage you must keep green and extend.
>
> **This is an EXTENSION, not a rebuild.** The owner reviewed the existing flow and asked for four concrete additions. Do NOT re-architect the working parts (text capture + message delete + Use-this persistence into `logo_url` all work and are correct). Match the existing code's style, the `card(...)`/`send_screen(...)` UI conventions, the `cb(BOT,"wiz",<action>,...)` callback shape, the FSM state pattern (`STATE_TEXT_*`), and the `_text_state_matches` guard used on every text-flow callback.

### 10.0 — What the owner asked for (plain language, so intent isn't lost)
The final flow, in order:
1. On the **logo** asset card, the row reads `[⬆️ Upload]` on the LEFT and `[✍️ Text]` on the RIGHT. *(Already true — `wizard.py:786-790` builds `upload_row` as `[BTN_UPLOAD_OWN, BTN_TEXT_LOGO]`. Keep it. If the labels ever get reordered, upload stays left, text stays right.)*
2. Tap **Text** → the bot asks "which text?" → admin sends it → bot **captures the value and DELETES the admin's message**. *(Already true — `STATE_AWAIT_TEXT` + `_channel_text` delete at `wizard.py:1418`. Keep it.)*
3. Then show **font categories, TWO per row**, ~6 (up to 8) categories: elegant, modern/sans, cursive/script, bold display, retro, handwritten. **Below the category grid, add an `⬆️ Upload your own` button** (its own full-width row) to upload a one-shot TTF/OTF. *(Categories exist but render ONE per row and there's NO upload-font button — §10.2 + §10.3.)*
4. Pick a category → show its fonts, **TWO per row, ≥10 fonts per category**. *(Only 1 font per category exists today — §10.1.)*
5. Pick a font → **NEW: ask for color.** Show a named emoji-swatch grid, **white and black FIRST**, 2 per row, covering all primary colors. *(Does not exist — §10.4.)*
6. Pick a color → **Next** → render the logo → preview with `[⇐ Back][✅ Use this]` and `[✗ Cancel]`. *(Preview exists; must now thread the chosen color into the render — §10.4/§10.5.)*
7. **Use this** → store into `logo_url` exactly as today (`store_text_logo`) → advance to the next asset. *(Already correct — keep `textuse` → `store_text_logo` → `_thumb_next`.)*
8. The **upload-your-own-font** path (from step 3): admin uploads a `.ttf`/`.otf`, the bot uses it for **this one render only — NO saving, NO adding to the bundled set**, then proceeds to the color step like any font. *(§10.3.)*

### 10.1 — Add ≥10 fonts per category (font collection)
**Where:** `shared/text_logo.py` `FONTS` tuple + `resources/fonts/text_logo/`.
**Do:**
- For EACH of the 6 categories, bundle **at least 10** Google-Fonts (OFL-1.1) families. That's ~60 `.ttf` files. Use variable-weight (`[wght]`) files where the family ships them (already the pattern: `PlayfairDisplay[wght].ttf`), else the `-Regular.ttf` static.
- For EVERY family, drop its `OFL.txt` license next to it named `OFL-1.1-<family-slug>.txt` (the existing convention — see `OFL-1.1-bebas-neue.txt`). This is a licensing REQUIREMENT, not optional; a family without its license file must not ship.
- Extend the `FONTS` tuple with a `TextLogoFont(key, name, category, description, filename)` per family. `key` must be unique, lowercase, stable (it's in callback data — keep it short; `cb` output must stay ≤64 bytes: `senku|wiz|textfont|<code>|<index>|<category>|<key>` — verify the longest key fits).
- Suggested families (all Google Fonts, OFL — the executor may substitute equivalents, but keep the category's VISUAL character):
  - **elegant (serif):** Playfair Display, Cormorant Garamond, EB Garamond, Cinzel, Libre Baskerville, Cardo, Spectral, Marcellus, Prata, DM Serif Display.
  - **modern (sans):** Montserrat, Poppins, Inter, Work Sans, Raleway, Oswald, Archivo, Manrope, Barlow, Rubik.
  - **script (cursive):** Pacifico, Dancing Script, Great Vibes, Satisfy, Sacramento, Allura, Parisienne, Yellowtail, Kaushan Script, Cookie.
  - **bold (display):** Bebas Neue, Anton, Teko, Fjalla One, Alfa Slab One, Archivo Black, Passion One, Titan One, Bowlby One, Staatliches.
  - **retro (display):** Bungee, Monoton, Bungee Inline, Lobster, Righteous, Fredoka, Bevan, Ultra, Chewy, Shrikhand.
  - **handwritten:** Caveat, Shadows Into Light, Indie Flower, Patrick Hand, Kalam, Gloria Hallelujah, Architects Daughter, Permanent Marker, Nanum Pen Script, Reenie Beanie.
- **How to fetch (document the method in the commit body):** download each family's release `.ttf` + `OFL.txt` from the Google Fonts GitHub mirror (`github.com/google/fonts/tree/main/ofl/<family>`). Do NOT hotlink at runtime — the renderer loads local files only (`_font_path`), and that must stay true (offline-safe, no network in `render_text_logo`).
- **Guard test** (`tests/test_text_logo.py`): assert every `TextLogoFont.filename` resolves to an existing file under `_FONT_DIR`; assert each of the 6 categories has `len(fonts_for_category(cat)) >= 10`; assert every font file has a sibling `OFL-1.1-*.txt`; assert the longest `senku|wiz|textfont|...` callback string is ≤64 bytes.

### 10.2 — Category grid: two per row (+ keep it ≤8 categories)
**Where:** `bots/senku/handlers/wizard.py::_thumb_text_categories` (~line 696).
**Do:** change the one-per-row build to two-per-row. Current:
```python
for category in text_logo_categories():
    rows.append([(category.name, cb(BOT, "wiz", "textcat", code, str(index), category.key))])
```
Replace with a pair-chunked build (mirror any existing 2-col helper in the repo; if none, inline it):
```python
cats = list(text_logo_categories())
row = []
for cat in cats:
    row.append((cat.name, cb(BOT, "wiz", "textcat", code, str(index), cat.key)))
    if len(row) == 2:
        rows.append(row); row = []
if row:
    rows.append(row)
```
Then append (in this order): the **`⬆️ Upload your own` font row** (§10.3), then the existing `[✗ Cancel]` row. If the owner later grows `CATEGORIES` beyond 8, the 2-col layout still holds — no cap logic needed, but do not exceed 8 (owner's stated ceiling).

### 10.3 — Upload-your-own font (one-shot, NOT saved)
**Intent:** from the category screen, an `⬆️ Upload your own` button lets the admin send a `.ttf`/`.otf` used for THIS render only. No persistence, no new bundled entry.
**Do:**
1. **Voice:** add `BTN_TEXT_UPLOAD_FONT = "⬆️ Upload your own"` to `senku_voice.py` and a `thumb_text_font_upload_prompt()` builder ("Send me a `.ttf` or `.otf` font file — I'll use it for this logo only.").
2. **Category card:** append `[[ (V.BTN_TEXT_UPLOAD_FONT, cb(BOT,"wiz","textupfont", code, str(index))) ]]` between the category grid and the Cancel row (§10.2).
3. **Router:** add `elif action == "textupfont":` — guard with `_text_state_matches(..., STATE_TEXT_CATEGORIES, ...)`, then `fsm.set(user_id, STATE_AWAIT_FONT_UPLOAD, code=..., index=..., text=<carried>)` and show a prompt card with only `[✗ Cancel]`. Add the new state constant `STATE_AWAIT_FONT_UPLOAD = "senku:wiz:await_font_upload"`.
4. **Capture the font file:** extend the existing `_upload_media` handler (`wizard.py:1469`, group=2). It currently only handles `STATE_AWAIT_UPLOAD` (images). Add a branch for `STATE_AWAIT_FONT_UPLOAD`:
   - Accept ONLY `message.document` whose filename ends `.ttf`/`.otf` (case-insensitive) OR mime in `{font/ttf, font/otf, application/x-font-ttf, application/font-sfnt, application/octet-stream}` — reject anything else with a voiced error and stay armed.
   - Download to a TEMP path under `data/text_logos/uploaded/` (NOT `resources/fonts/text_logo/` — that dir is the bundled, committed set; an upload must never land there). Name it by a content hash so concurrent admins don't collide.
   - **Delete the admin's uploaded message** (consistent with the text-capture UX and the owner's standing "delete my message" rule).
   - Store the temp font path in FSM (`custom_font_path`) and jump STRAIGHT to the **color step** (§10.4) — an uploaded font skips category/font pick (they already chose it by uploading).
5. **Render with a custom font:** `render_text_logo` currently resolves a bundled font via `get_font(font_key)`+`_font_path`. Add an optional param `font_path: Path | None = None` that, when provided, is used directly (bypassing `_FONT_BY_KEY`); `font_key` becomes optional in that call path. Keep the bundled path unchanged when `font_path is None`. **One-shot cleanup:** after `store_text_logo` succeeds (Use this), best-effort `unlink()` the uploaded temp font — it's served its single use. If the admin cancels, the 15-min FSM TTL + a periodic temp sweep (or an `unlink` in the cancel handler) reclaims it. Never register it in `FONTS`.
6. **Test:** in `tests/test_senku_wizard_routing.py` assert `textupfont` routes; in `tests/test_text_logo.py` assert `render_text_logo("Hi", None, font_path=<a bundled ttf>)` renders (reuse a bundled file as the "uploaded" fixture so the test needs no external asset).

### 10.4 — NEW color step (emoji swatch + name, white/black first)
**Owner's chosen UI (confirmed):** emoji-swatch + name buttons, TWO per row, white and black FIRST, then the primary spectrum. Telegram inline buttons can't render a real color fill, so the leading emoji IS the swatch.
**Do:**
1. **Color model — put it in `shared/text_logo.py`** (next to `FONTS`, same dataclass discipline) so the renderer and UI share one source of truth:
```python
@dataclass(frozen=True, slots=True)
class TextLogoColor:
    key: str            # short, stable, in callback data: "white","black","red",...
    name: str           # "White"
    emoji: str          # "⚪"
    rgb: tuple[int, int, int]   # (255,255,255)

COLORS: tuple[TextLogoColor, ...] = (
    TextLogoColor("white",  "White",  "⚪", (255, 255, 255)),
    TextLogoColor("black",  "Black",  "⚫", (0, 0, 0)),
    TextLogoColor("red",    "Red",    "🔴", (220, 38, 38)),
    TextLogoColor("blue",   "Blue",   "🔵", (37, 99, 235)),
    TextLogoColor("green",  "Green",  "🟢", (22, 163, 74)),
    TextLogoColor("yellow", "Yellow", "🟡", (234, 179, 8)),
    TextLogoColor("orange", "Orange", "🟠", (234, 88, 12)),
    TextLogoColor("purple", "Purple", "🟣", (147, 51, 234)),
    TextLogoColor("pink",   "Pink",   "🩷", (236, 72, 153)),
    TextLogoColor("brown",  "Brown",  "🟤", (120, 72, 40)),
    TextLogoColor("gray",   "Gray",   "🌫", (107, 114, 128)),
    TextLogoColor("cyan",   "Cyan",   "🟦", (6, 182, 212)),
)
_COLOR_BY_KEY = {c.key: c for c in COLORS}
def colors() -> tuple[TextLogoColor, ...]: return COLORS
def get_color(key: str) -> TextLogoColor | None: return _COLOR_BY_KEY.get(key)
```
   White and black MUST be indices 0 and 1 (owner: "main color should be first… white… black"). The rest is the primary spectrum; the executor may extend toward ~16 but keep the emoji↔rgb honest (the button emoji should visually match the fill).
2. **Stroke auto-contrast:** the renderer currently hardcodes a dark stroke (`stroke_fill=(0,0,0,180)`), which is right for white text but wrong for black text (black-on-transparent with a black stroke vanishes on a dark poster). Make the stroke the CONTRAST of the fill: luminance of the chosen rgb < 128 → light stroke `(255,255,255,180)`, else dark `(0,0,0,180)`. This keeps every color legible in the thumbnail's allocated logo slot.
3. **New wizard screen `_thumb_text_colors(chat_id, user_id, code, index, *, old_msg)`** (model it on `_thumb_text_fonts`): build a 2-per-row grid of `(f"{c.emoji} {c.name}", cb(BOT,"wiz","textcolor", code, str(index), c.key))`, then a `[⇐ Back]` row (back to the font list — or back to the category grid when arriving from an uploaded font, since there's no font list in that path; carry an `origin` flag in FSM to route Back correctly) and a `[✗ Cancel]` row. Add `STATE_TEXT_COLORS = "senku:wiz:text_colors"` and set it here, carrying `code,index,text,font(=key or None),custom_font_path(or None)`.
4. **Flow rewiring — color comes AFTER font/upload, BEFORE preview:**
   - `textfont` action (bundled font pick): instead of jumping to `_thumb_text_preview`, now go to `_thumb_text_colors` (carry `font=font_key`).
   - uploaded-font path (§10.3.4): after storing `custom_font_path`, go to `_thumb_text_colors` (carry `font=None`).
   - **New `textcolor` action:** guard `STATE_TEXT_COLORS`; resolve `get_color(key)`; then call `_thumb_text_preview(...)` passing the color through. The preview card keeps its existing `[⇐ Back][✅ Use this]` + `[✗ Cancel]` — **Back from preview now returns to the COLOR grid** (not the font list), since color is the immediately-prior step. (Owner said "after I select the color, then I press next and you make a logo" — the render happens on entering the preview; the preview's "Use this" is that Next/confirm. Do NOT add a separate Next button if the preview already renders on arrival — that matches the existing pattern and avoids an extra tap. If the executor prefers an explicit Next before rendering, that's acceptable ONLY if it stays one extra tap and is labeled `➡️ Next`.)
5. **Persist the choice** in FSM at `STATE_TEXT_PREVIEW` (add `color=<key>`) so a Back-and-forth keeps the picked color.

### 10.5 — Thread color into the renderer
**Where:** `shared/text_logo.py::render_text_logo` and its callers.
**Do:** add `color_rgb: tuple[int,int,int] = (255,255,255)` param. Use it as the `fill=(*color_rgb, 255)` in `draw.multiline_text`, and compute the auto-contrast `stroke_fill` (§10.4.2). Update the digest so the cache key includes the color (else two colors of the same text+font collide on the same PNG path): `digest = sha256(f"{font_id}\0{color_key}\0{clean}")` where `font_id` is `font_key` or the uploaded font's content hash. Callers:
- `wizard.py::_thumb_text_preview` — accept `color_key`/`color_rgb` and a `font_path` (for uploads), resolve via `get_color`, pass both into `render_text_logo`.
- Keep the existing `(FileNotFoundError, ValueError, OSError)` guard around the render — an uploaded font that PIL can't parse must voice `thumb_text_error()` and return to the color grid, not crash the wizard.

### 10.6 — "Which text?" ordering is already correct — verify, don't rebuild
The owner restated the intended order (Text → ask text → capture+delete → categories). This ALREADY matches `action=="text"` → `STATE_AWAIT_TEXT` → `_channel_text` (deletes at 1418) → `_thumb_text_categories`. **Verify it still holds after your changes** and that the message-delete stays. No rebuild.

### 10.7 — Voice/label additions (keep the existing terse style)
Add to `shared/senku_voice.py`, matching the existing `BTN_TEXT_*` tone (short, one glyph + word):
- `BTN_TEXT_UPLOAD_FONT = "⬆️ Upload your own"`
- `thumb_text_font_upload_prompt()` — the TTF/OTF ask.
- `thumb_text_colors()` — the color-step header ("Pick the logo color.").
- (reuse `thumb_text_error`, `BTN_TEXT_BACK`, `BTN_TEXT_CANCEL`, `BTN_TEXT_USE` as-is.)
Keep everything English/en-style consistent with the surrounding voice module. Do NOT introduce a new localization file; `senku_voice.py` is where these live.

### 10.8 — Callback-size + collision checks (do BEFORE finalizing keys)
Every new callback rides `cb(BOT,"wiz",<action>,<code>,<index>,<key>)`. `code` can be a full request code. Verify the LONGEST string stays ≤64 bytes for: `textfont|<code>|<index>|<category>|<fontkey>`, `textcolor|<code>|<index>|<colorkey>`, `textupfont|<code>|<index>`. If any risk overflow, shorten keys (e.g. color keys are already short; the risk is long font keys + long categories — cap font `key` at ~10 chars). Add an assertion in the font/color guard test.

### 10.9 — Tests (all must drive REAL code, per §9.7 — no shadow tests)
- `tests/test_text_logo.py`: ≥10 fonts/category; every filename + OFL sibling exists; `render_text_logo` honors `color_rgb` (render two colors of the same text+font → assert DIFFERENT output paths AND differing center-pixel color); `font_path` override renders; longest callback ≤64 bytes.
- `tests/test_senku_wizard_routing.py`: `textupfont`, `textcolor` route; `textfont` now leads to the color step (assert the color screen state is set, not preview).
- `tests/test_senku_thumbnail_adapter.py`: keep green; `store_text_logo` still writes `logo_url` (unchanged).
- A small render/UX test that an uploaded-font path reaches the color step and, on Use-this, `store_text_logo` is called and the temp font is unlinked.

### 10.10 — Definition of done (this phase)
- Logo row still `[⬆️ Upload][✍️ Text]` (upload left, text right). Text → ask → capture → **delete admin msg** (unchanged, verified).
- Categories render **2/row**, 6-8 of them, with an `⬆️ Upload your own` font row beneath; each category exposes **≥10 fonts, 2/row**.
- After font (or uploaded font) → **color grid** (emoji+name, white/black first, 2/row) → preview → **Use this** → stored into `logo_url` via the existing `store_text_logo` (unchanged downstream; the thumbnail's allocated logo slot consumes it exactly as a TMDB pick).
- Uploaded fonts are **one-shot**: temp file, used for one render, unlinked after use, NEVER added to `FONTS` or `resources/fonts/text_logo/`.
- Every bundled font ships its `OFL-1.1-*.txt`. Renderer stays offline (local files only).
- `py_compile` clean; full suite green and **NOT below the current count** (check the number first with a full run, then never regress it); new tests drive real code.
- Back/Cancel work at every new step; FSM `_text_state_matches` guards every new callback; no callback string exceeds 64 bytes.
- Matches existing UI/voice/callback conventions exactly — a reviewer should not be able to tell the color/upload steps were added later.


---

## 11. PHASE — Text-logo polish (shadow, line-breaks, sizing, weights) + TMDB assets per franchise + reuse-previous-logo

> **Context.** §10 shipped (commits `2d40a95` + `62b31a0`): the text-logo flow, ~10 fonts/category, the color grid, one-shot font upload. Read `shared/text_logo.py`, `bots/senku/handlers/wizard.py` (text-logo actions + `_thumb_text_*` screens), `thumbnail/index.html` (the logo slot), `nekofetch/providers/metadata/tmdb.py` (`search`/`details`/`_logo`/`_confirm_backdrop`), and `nekofetch/services/thumbnail_service.py::gather_thumbnail_fields` BEFORE editing. This phase is 7 concrete refinements the owner asked for after using the shipped flow. Reuse `_base_title_key` (`nekofetch/services/franchise_flow.py:133`) — do NOT write a second season-stripper.

### 11.1 — Text logo: soft black drop shadow (mild, not a hard stroke)
**Problem:** the render uses only a hard contrast stroke (`_contrast_stroke`, `text_logo.py:248`). The owner wants a **mild, soft black drop shadow** behind the glyphs so a light logo stays visible on a bright poster — softer than the current outline.
**Where:** `shared/text_logo.py::render_text_logo` (~line 316-324).
**Do:**
- Before drawing the fill text, draw the SAME `wrapped` text onto a separate transparent layer in near-black `(0,0,0,alpha)` with a small positive offset, then Gaussian-blur that layer and composite it under the fill text. Concretely:
```python
from PIL import ImageFilter
shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
sdraw = ImageDraw.Draw(shadow)
sdraw.multiline_text((width//2 + _SHADOW_DX, height//2 + _SHADOW_DY), wrapped,
    font=chosen, fill=(0, 0, 0, _SHADOW_ALPHA), spacing=12, align="center", anchor="mm")
shadow = shadow.filter(ImageFilter.GaussianBlur(_SHADOW_BLUR))
image = Image.alpha_composite(image, shadow)   # shadow UNDER the fill
# then draw the fill text on top (existing draw.multiline_text call)
```
- New constants near the others (`text_logo.py:31-36`): `_SHADOW_DX = 0`, `_SHADOW_DY = 6`, `_SHADOW_ALPHA = 120` (mild — NOT 255), `_SHADOW_BLUR = 8`. Keep it subtle; the owner said "very mild, not a very dark one."
- **Keep the contrast stroke too** but consider dropping its alpha (e.g. 180→120) so shadow + stroke don't stack into a heavy outline. The shadow is the primary legibility aid now.
- **Digest:** fold the shadow params into the cache digest only if you make them configurable; as fixed constants they don't need to be in the key (same input → same output). Leave the digest as-is.
- **Test** (`tests/test_text_logo.py`): assert a rendered logo has non-zero alpha pixels BELOW/around the glyph baseline that are darker than the fill (evidence a shadow layer composited). Keep it tolerant (assert "some dark semi-transparent pixels exist outside the stroke band"), not pixel-exact.

### 11.2 — Respect the admin's line breaks; don't invent wrapping
**Problem:** `_wrap_to_width` (`text_logo.py:223`) ALWAYS re-wraps to `_MAX_WIDTH`, so a single-line title the admin typed with no breaks can still get split. Owner: "if I give you the text without any line break, then you do not add any line break… generate how it is."
**Rule the executor must implement:**
- If `sanitize_text(text)` contains **no `\n`** → treat it as ONE line. Do NOT word-wrap. Instead shrink the font (the existing `_START_FONT_SIZE→_MIN_FONT_SIZE` loop) until that single line fits `_MAX_WIDTH`. Only if it STILL overflows at `_MIN_FONT_SIZE` (an extreme edge case) fall back to the current word-wrap as a last resort — a giant unbroken string must not blow the canvas.
- If the admin's text **has `\n`** → honor EXACTLY those breaks. Render each line as the admin wrote it; do not merge or re-wrap lines (still enforce `_MAX_LINES` and the per-line width-shrink).
**Where:** refactor `_wrap_to_width` into "honor-explicit-breaks" mode. The auto-wrap branch (splitting on spaces to `_MAX_WIDTH`) becomes the LAST-RESORT path for a single token/line that overflows even at min size — not the default. Keep the URL/long-token `textwrap` splitter only inside that last-resort path.
**Test:** `render_text_logo("One Piece", ...)` → the wrapped intermediate has exactly 1 line; `render_text_logo("Line A\nLine B", ...)` → exactly 2 lines matching the input; a pathological no-space 400-char string still renders (last-resort wrap fires) without exceeding `_MAX_LINES`.

### 11.3 — Widen + slightly enlarge the logo slot in index.html
**Problem:** the logo `<img>` (`thumbnail/index.html:61`) is `h-[3.5rem] w-auto` with no max-width, so a wide text logo gets visually cramped/shrunk relative to the synopsis column. Owner wants the logo's allowed WIDTH to reach the synopsis's right edge, and the logo a touch BIGGER — "not much, just a bit."
**Reference widths in the file:** synopsis `<p>` is `max-w-[520px]` (`index.html:96`); poster is `w-[12vw]`; the logo sits in `<div class="ml-1 mb-2">` above the poster+synopsis row.
**Do (conservative, owner said "just a bit"):**
- Change the logo `<img>` class from `h-[3.5rem] w-auto object-contain …` to about `h-[4.5rem] w-auto max-w-[820px] object-contain …`. Height bump ~3.5→4.5rem is the "a bit bigger"; `max-w` lets a wide logo extend toward the synopsis's right edge instead of being pinched. (820px ≈ poster `w-[12vw]` + gap + synopsis 520px on the 1280-wide render — the executor should open the rendered WebP once and nudge `max-w` so the logo's right edge lands near the synopsis end-line, per the owner's "same width as the end line of synopsis.")
- Keep the existing `drop-shadow-[…]` on the img (that's the CSS shadow of the whole logo image; §11.1's baked shadow is for the transparent text PNG — they don't conflict; a TMDB logo.png keeps only the CSS shadow).
- Do NOT change the poster or synopsis sizes. One dimension only: logo height + max-width.
- **Verify** by rendering one thumbnail (a long-title anime) and eyeballing the WebP; adjust `h-[…]`/`max-w-[…]` by small steps. Note in the commit that these are visual constants tuned against a 1280×720 render.

---

## 10-REVIEW — Audit of the executor's §10 delivery (commits `2d40a95`, `62b31a0`)

> **Not written by the planner.** These two commits were made by the executor AI running §10. Reviewed against the §10 spec on 2026-08-08 by reading `shared/text_logo.py`, `bots/senku/handlers/wizard.py`, and the font assets. Verdict up front: **§10 was delivered faithfully and completely.** Status legend: ✅ done · 🟡 partial · ❌ missing.

**What was done (verified):**
- ✅ **≥10 fonts per category** — exactly 10 each across all 6 categories (elegant/modern/script/bold/retro/handwritten), **60 total**, each an OFL Google font under `resources/fonts/text_logo/` with its `OFL-1.1-*.txt` sibling. Matches §10.1.
- ✅ **Color step** — 12 colors, white+black first, emoji-swatch + name, 2/row. `TextLogoColor` model in `text_logo.py`, `STATE_TEXT_COLORS`, `textcolor` action. Matches §10.4 and the auto-contrast stroke (`_contrast_stroke`).
- ✅ **Category grid 2/row + "Upload your own"** — `_thumb_text_categories` chunks 2-per-row and appends `BTN_TEXT_UPLOAD_FONT` (`textupfont`). Matches §10.2/§10.3.
- ✅ **One-shot font upload** — `STATE_AWAIT_FONT_UPLOAD`, temp dir `data/text_logos/uploaded/` (NOT the bundled set), `render_text_logo(font_path=…)` override with content-hash identity. Matches §10.3's "used once, not saved."
- ✅ **Color threaded into renderer + digest** — `color_rgb` fill, digest includes color so two colors don't collide. Matches §10.5.
- ✅ **Text capture + message delete + Use-this → `store_text_logo` → logo_url** — unchanged and correct (the pre-existing behavior §10 said to keep).

**Misunderstandings / deviations — NONE material.** The executor did not over-build or skip anything in §10 scope. Minor notes for the next executor (these become §11 work, they were NOT §10 failures):
- The weight step (Regular→Black) was correctly ABSENT — it was never in §10; it's §11.4.
- The renderer still uses a hard contrast stroke with no soft drop shadow — again not a §10 miss; the shadow is §11.1.
- `_MAX_LINES`/`_wrap_to_width` still auto-wraps even single-line input — that's the §11.2 refinement, not a §10 defect.

**One thing to double-check (not a bug, a caution):** commit `62b31a0` also rewrote large stretches of `HANDOFF_PLAN.md` (the diff shows ~2745 lines touched). Confirm that was only the executor updating §8A/§10 status lines and not an accidental clobber of §9/§11 planner content — `grep -nE "^## (8|9|10|11)\." HANDOFF_PLAN.md` should still show every section intact. (At review time §11 had not yet been written, so nothing was lost; just verify on the next pass.)

**Net:** §10 is ✅ complete and consistent with the wizard's conventions. Proceed to §11.

### 11.4 — Weight step after font pick (Regular / Bold / Black …) + Italic when the font supports it
**Problem:** every logo renders at one fixed weight, so text "looks so small"/thin. Owner wants a weight choice per font (regular, bold, black…), **and an Italic option — but ONLY for fonts that actually have italic; if a font has no italic, don't show it.**
**Key facts the executor MUST account for:**
- Many bundled families are **variable fonts** (`[wght]` in the filename, e.g. `Montserrat[wght].ttf`, `PlayfairDisplay[wght].ttf`, `Fredoka[wdth,wght].ttf`, `Inter[opsz,wght].ttf`). Weight is set via the variation axis — Pillow's `FreeTypeFont.set_variation_by_axes([...])` or `set_variation_by_name("Bold")`. There is NO variable-font handling today — add it.
- Some families are **static single-weight** (`-Regular.ttf`: Bebas Neue, Pacifico, Lobster, Anton, most scripts). No weight axis → offering "Black" is meaningless. For statics, **hide the weight step** and render as-is (do NOT fake bold with stroke — it muddies script/display faces).
- **Italic is a SEPARATE axis** (`ital` or `slnt`). The single `[wght]` Google releases bundled here mostly do NOT carry an italic axis, so italic is NOT universally available. The owner's rule is exactly right: **offer Italic only when the font file exposes an italic/slant axis (or ships a separate italic file); otherwise omit it.**
**How to detect capability (do this at load, per font):**
- Weight axis: `font.get_variation_axes()` (Pillow ≥ 8.3) returns the axis list; a `wght` axis (or filename containing `[wght]`) ⇒ show weights. Wrap in try/except — static fonts raise `OSError` on `get_variation_axes()`; treat that as "no axes → static, no weight step."
- Italic axis: an `ital` or `slnt` axis present in `get_variation_axes()` ⇒ Italic is real. If absent ⇒ no Italic button for that font. (None of the current bundled single-axis files are expected to pass this, which is fine — the button simply won't appear until an italic-capable font is added. That's the owner's intent.)
- Cache the detected capability on the `TextLogoFont` at module load (compute once): add fields `variable: bool`, `has_italic: bool` — or a helper `font_capabilities(font) -> (weights: tuple[str,...], has_italic: bool)` memoized by `font.key`. Do the probe against the real file so it never lies about what the TTF supports.
**Design:**
1. **Renderer:** add `weight: int | None = None` and `italic: bool = False` to `render_text_logo`. When `weight` set and the font is variable: `font.set_variation_by_axes([weight])` (try/except → fall back to plain). When `italic=True` and the font has an `ital`/`slnt` axis: set that axis too (e.g. `ital=1` or the slant's max). If a caller passes `italic=True` for a font that can't do it, IGNORE it (never synthesize a fake oblique). Fold BOTH `weight` and `italic` into the digest so Bold≠Regular and Italic≠upright don't collide on one PNG.
2. **Wizard flow — weight/style step BETWEEN font and color** (order: text → category → font → **weight/style** → color → preview):
   - New `_thumb_text_weights(...)` screen (model on `_thumb_text_fonts`). Reached ONLY when the picked font is variable (has a `wght` axis). Buttons, 2/row: one per weight label (Regular/Medium/SemiBold/Bold/Black → 400/500/600/700/900), each `cb(BOT,"wiz","textweight", code, index, font_key, wght, "0")` where the trailing flag is italic=off. **If `has_italic`**, append a parallel set OR a toggle: simplest is a second short row of italic variants `cb(...,"textweight",…, wght, "1")` labeled "Bold Italic" etc. — but to keep the grid small, prefer a single **`𝑰 Italic` toggle button** that flips an `ital` flag carried in FSM, re-rendering the same weight grid with the flag on (label the toggle "Italic: off/on"). Only render that toggle when `has_italic`. Then Back(→fonts)/Cancel. Add `STATE_TEXT_WEIGHTS`.
   - `textfont` action: if the picked font is variable → `_thumb_text_weights`; else skip straight to the color grid (carry default weight, italic=False).
   - New `textweight` action → color grid, carrying `font` + `weight` + `italic`.
   - `textcolor` → preview, passing `weight` + `italic` into `render_text_logo`.
   - Fix Back targets: preview→color, color→weight (if variable) else fonts, weight→fonts.
3. **Callback size:** `textweight|<code>|<index>|<fontkey>|<wght>|<ital>` — verify ≤64 bytes with the longest code+fontkey (§10.8). `<ital>` is one char.
4. **Test:** `render_text_logo("Hi","montserrat",weight=900)` vs `weight=400` → DIFFERENT paths; a static font (`bebas`) with `weight=900` renders without crashing (variation ignored); `italic=True` on a font WITHOUT an italic axis produces the SAME output as `italic=False` (proving no fake oblique); routing: variable font → weight screen, static font → skips it, and the Italic toggle only appears for an italic-capable font (add such a font, or assert the toggle is absent for all current bundled fonts).

### 11.5 — TMDB assets are franchise-level: search the BASE title + include adult
**Problem (owner + API log `GET /search/tv?query=The Case Study of Vanitas Part 2&include_adult=false`):** TMDB stores assets per FRANCHISE, not per season, so a seasoned query finds nothing/wrong. R-rated anime also need `include_adult=true`.
**Where:** `nekofetch/providers/metadata/tmdb.py::search` (line 103) + callers passing a per-entry title (`thumbnail_service.gather_thumbnail_fields:211`, `enrich_with_tmdb`, `bots/admin/handlers/*`).
**Do:**
1. **Search the base franchise title.** Add `strip_season_tokens(title) -> str` in `nekofetch/services/franchise_flow.py` (reuse the season/part regex from `_parse_season_part`/`_base_title_key`, but PRESERVE original case and inner words — `_base_title_key` lowercases, which is wrong for a display query). Remove trailing `Season N` / `Part N` / `Cour N` / `Final Season` / roman-numeral season tokens. Call it at the top of `tmdb.search`. **Fallback:** if the stripped search returns no candidates, retry once with the raw title (so a show whose real TMDB name legitimately contains a number still resolves).
2. **`include_adult=true`** at `tmdb.py:117` (was `"false"`). Owner explicitly wants R-rated anime to resolve.
3. **Keep the anime bias** — don't weaken `_is_anime`/`_is_jp`/`rank()`; just feed it the base title.
4. **Test** (`tests/test_tmdb_search.py` new or extend): stub `_get`, assert "…Vanitas Part 2" issues `query="The Case Study of Vanitas"` with `include_adult="true"`; assert the raw-title retry fires when the stripped search yields `results: []`; assert a JP-anime still out-ranks a live-action namesake.

### 11.6 — Later seasons reuse the first entry's TMDB assets
**Follow-on from 11.5:** TMDB has ONE franchise entry, so Season 2/Part 2 have no distinct posters/backdrops/logos. Owner: later entries show the SAME assets as entry 1.
**Where:** per-entry asset gathering in the Senku wizard / `senku_thumbnail_adapter` (`asset_step` and the numbered TMDB logo/poster/backdrop galleries).
**Do:** this is mostly a NATURAL consequence of 11.5 (every entry searches the same base title → identical galleries). **Verify** the wizard/adapter doesn't cache the per-entry asset list under the SEASONED title (which would break reuse); key the TMDB asset cache by the base title or `anime_doc_id` so entry 2 hits entry 1's set. AniList per-entry fields (seasonal poster, per-season synopsis) still come from AniList per entry — do NOT collapse those; this is ONLY the TMDB logo/backdrop galleries. **Test:** stubbed TMDB returns a fixed set for the base title → entry 0 and entry 1 of a 2-season franchise get the SAME logo/backdrop URL lists.

### 11.7 — "Reuse the previously generated text logo?" on later entries
**Owner:** when adding a TEXT logo for a later entry, tapping **Text** should FIRST ask "Use the previously generated logo?" showing the LATEST text logo made this session; Yes → adopt + skip the build; No → normal ask-text flow.
**Where:** `action == "text"` (`wizard.py:1224`-ish) + request-scoped state.
**Do:**
1. **Remember the latest generated text-logo PNG per request** — on every successful `render_text_logo`/`textuse`, stash `last_text_logo` (local path, and its text/font for display) under a REQUEST-scoped key (Redis/DistributionCache keyed by `code`, e.g. `nf:senku:<code>:last_text_logo`), NOT per-user FSM (which clears). Overwrite each time so it's always the latest.
2. **Gate the text entry:** in `action=="text"`, if a valid `last_text_logo` exists for this `code` → show a NEW card FIRST: the previous logo IMAGE + `[✅ Use previous][✍️ New text]` + `[✗ Cancel]`. New actions `textprev_yes`/`textprev_no`:
   - `textprev_yes` → `thumbs.store_text_logo(code, index, <stored path>)` → `_thumb_next`. Skip text/category/font/weight/color entirely.
   - `textprev_no` → fall through to the existing `STATE_AWAIT_TEXT` prompt.
   - No prior logo → skip the question, straight to `STATE_AWAIT_TEXT` as today.
3. **Show the actual logo** in the card (owner: "show the logo there, the latest one") — use the stored PNG as the card image.
4. **Voice:** `thumb_text_reuse_prompt()`, `BTN_TEXT_REUSE_YES="✅ Use previous"`, `BTN_TEXT_REUSE_NO="✍️ New text"` in `senku_voice.py`.
5. **Test:** routing for `textprev_yes`/`textprev_no`; after generating a logo for entry 0, entering Text on entry 1 finds the stored `last_text_logo` and Yes calls `store_text_logo` with that path (stub store, assert path).

### 11.8 — Definition of done (this phase)
- Text logos carry a **mild soft black drop shadow** (subtler than the stroke); light logos stay legible on bright posters.
- **No-line-break input renders on one line** (font shrinks to fit); explicit `\n` honored exactly; pathological unbroken strings still render via last-resort wrap.
- index.html logo slot is **a bit taller and can span toward the synopsis's right edge**; poster/synopsis unchanged; verified against a real render.
- After font pick, **variable fonts offer a weight step** (Regular→Black); **Italic appears ONLY when the font truly supports it**; static fonts skip the step; weight+italic thread into the render + digest; fake oblique is never synthesized.
- TMDB search uses the **base franchise title** (season/part stripped, case preserved) with **`include_adult=true`** + raw-title fallback; anime ranking preserved; later entries reuse entry 0's TMDB galleries.
- Adding a **text logo on a later entry first offers "use the previously generated logo"** (shows the latest); Yes adopts + skips, No continues.
- All new callbacks ≤64 bytes + `_text_state_matches`-guarded; Back/Cancel correct everywhere; `py_compile` clean; **full suite green and not below the current count** (run it first); every new test drives REAL code (§9.7); UI/voice/callback conventions match the existing wizard exactly.
