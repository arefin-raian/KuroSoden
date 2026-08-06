# NekoFetch / KuroSoden — FINAL EXECUTION PLAN (v2, code-verified)

> **You are the executor.** This plan was written by an AI that read the codebase and verified every
> path/line/signature below. Follow it exactly. Where it says "already exists — just do X", do NOT
> rebuild from scratch. Two different AIs following this file must produce functionally identical
> results. All line numbers are from the state of the repo at handoff; re-`grep` to confirm before
> editing (they may shift by a few lines).

---

## 0. ENVIRONMENT (do not get this wrong)
- **WSL/Linux, Bash only. No PowerShell.** Working dir: `/mnt/c/Users/Admin/Documents/NekoFetch/KuroSoden`.
- Python: `./.venv/Scripts/python.exe` (Windows venv from WSL). Tests: `./.venv/Scripts/python.exe -m pytest <path> -q`. Compile: `./.venv/Scripts/python.exe -m py_compile <files>`.
- **Remote DBs (Render PG / Mongo / Redis) are UNREACHABLE here.** Verify ONLY with in-memory SQLite unit tests + py_compile. Never write a script that connects to prod.
- `kurosoden.*` is a synthetic namespace (conftest.py) mapping onto `shared/`, `bots/`, `nekofetch/`.

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

### EXECUTION DISCIPLINE (non-negotiable)
1. **Read the cited file+lines before editing.** Never invent a symbol; confirm it exists.
2. **Reuse existing code** the plan points to. No parallel re-implementations.
3. Every task: add/adjust a unit test, `py_compile` clean, `pytest -q` (new test + full suite) green.
4. **Run a code-review pass** (self-review the diff or `/code-review` skill) before "done"; fix findings.
5. Don't regress §2 DONE items. Items marked **[CONFIRM WITH OWNER]** must be confirmed first.

---

## 1. ARCHITECTURE MAP (verified)

**Four Pyrogram bots**, wired by `shared/pipeline_manager.py` (owns all 4 clients: `lelouch`, `levi`, `senku`, `gojo`; `container.admin_client` = admin bot or gojo fallback). Stage handoffs in `shared/handoff.py`:
- `handoff_download_to_distribution(container, code, title)` — Levi→Senku (`complete_task(code,"levi")` → `assign(code,"senku")`).
- `handoff_distribution_to_publish(container, code, title)` — Senku→Gojo. Called from `bots/senku/handlers/wizard.py:~870` after `SenkuPublisher.publish`.

**Pipeline:** Lelouch (request/batch) → Levi (download+encode) → storage packs → Senku (distribution channel + thumbnails) → Gojo (main-channel post + A-Z index).

### Data models — `nekofetch/infrastructure/database/postgres/models.py`
- `Request` (59) — `user_id` FKs `users.id` (NOT telegram_id; resolve via `UserRepository.get_or_create(tg).id`). Holds `franchise_data` JSON.
- `DownloadJob` (94, table `download_queue`) — `priority`, `status`, `current_episode`. **No `processing_order` column.**
- `MediaFile` (122, table `files`) — has `season`, **`season_part`**, `episode`, `resolution`, `audio`.
- `StoragePack` (215) — unique `(anime_doc_id, season, season_part, resolution, audio, entry_id)`; `header_message_id`, `start_message_id`, `end_message_id`, `file_message_ids` JSON, `episode_from/to`, `entry_id`, `enabled`. **NO `caption` column.**
- `ChannelPost` (262) — one per `anime_doc_id`; `main_message_id`, `index_letter`, `index_message_id`.
- `ChannelLayout` (358) — per distribution bot, ordered `seq`; `kind` ∈ {info_card, season_card, movie_card, watch_guide, divider, footer}, `anilist_id`, `tg_message_id`, `is_pinned`.
- `PublishedPostBackup` (277) / `ChannelContentBackup` (313) — recovery snapshots (caption HTML, mirrored image URLs, `button_data`, `cards` JSON). Editing a live post must also update its backup.
- `IndexSection` (438) — A-Z slots; `base_letter`, `label`, `message_id` (posted iff non-null), `repurposed`.

### Franchise mapping — `nekofetch/services/franchise_flow.py`
`FranchiseFlowService.build_mapping(franchise_data, anime_doc_id, franchise_entries=None) -> FranchiseMapping` (186). `MappingEntry` dataclass (74): `anilist_id, kind (ContentKind SEASON|MOVIE|SPECIAL), season_number, season_part, title, episodes, included, auto_detected_part`. `FranchiseMapping.included_entries` (96). **`dataclasses.asdict()` works** (no custom serializer). `dict_to_mapping(mapping_dict)` (505) already reads a dict of the same shape.

### UI conventions (MANDATORY — match exactly)
- `nekofetch/ui/screens.py::Screen(caption, image, keyboard)` + `send_screen(client, chat_id, screen, old_msg=...)`; `card(...)` builder.
- `nekofetch/ui/components.py::keyboard(*rows)`; rows are `[(label, callback)]`; `cb("<bot>","<action>",*args)`.
- `nekofetch/ui/artwork.py::pick_artwork("<bot>")`. Owner gate `shared/access_gate.py::is_owner(container,obj)`; staff `is_staff(obj)`.
- Voice in `shared/<bot>_voice.py` as `V`. Localized strings via `nekofetch/localization/messages.py::t(M.KEY, **kw)` reading `resources/language/en.json` (`{placeholder}` syntax, Telegram HTML incl. `<a href>`; unknown placeholders render literally — safe).
- **FSM text capture:** `nekofetch/bots/fsm.py::FSM(redis, bot=..., ttl=900)` with `.set/.get/.update/.clear`; register the text handler at a UNIQUE `group=` (e.g. Gojo schedule uses one, Levi naming uses group=13) so it doesn't fight other handlers. Model new caption-edit flows on `bots/levi/handlers/naming_confirm_handler.py`.

---

## 2. STATUS
### ✅ DONE & TESTED (do not redo; don't regress)
1. Gojo `/stats` — DB-driven `nekofetch/services/stats_service.py::compute()`. Tests `tests/test_stats_db_driven.py`.
2. Gojo Schedule button → real list view + Back (`bots/gojo/handlers/schedule.py`).
3. Gojo `/start` vs Settings menu drift → `bots/gojo/app.py::_home_rows`. Tests `tests/test_gojo_home_menu.py`.
4. Gojo Settings trimmed (`bots/gojo/handlers/__init__.py`) — removed thumbnail_channel + timezone.
5. **Lelouch `/redo` fixed.** Root cause: `bots/lelouch/handlers/requests.py:97` `LELOUCH_COMMANDS` (the free-text title handler's `~filters.command(...)` exclusion list) was missing `"redo"`. Because `register_requests` mounts that group-0 text handler BEFORE `register_redo`, typing `/redo` matched `_text`, was treated as an anime-title search, found nothing, and returned — so the real `/redo` handler in `redo.py` never fired and nothing was logged (`/settings` worked only because it WAS in the list). Fix: added `"redo"` to the list. Tests `tests/test_lelouch_redo_command.py` (2 pass) — one pins `redo`, one asserts EVERY registered Lelouch command is in the exclusion list so this can't regress.

### 🟡 PARTIAL — `_map_episode_to_part` exists (`nekofetch/services/download_service.py:1942`) and `_record_file` (1561) already calls it + sets `MediaFile.season_part`, BUT it reads `franchise_data["entries"]` which is never written. **Task A is the linchpin.**

### Key discoveries that SHRINK the work
- **Pack splitting per part is ALREADY implemented.** `publishing_service._upload_packs` groups by `(season, season_part, resolution, audio, entry_id)` (`publishing_service.py:560`) and orders by season→part→resolution (575). Once `season_part` is populated (Task A), Vanitas becomes 2 packs automatically. **Task B is mostly verification + Senku entry cards.**
- **The update/append flow ALREADY exists.** `publishing_service.py:373` `is_update_entry` branch → `SenkuPublisher.update_distribution_channel(...)`; append logic `_append_and_refooter` deletes guide/dividers/footer, appends new card, re-posts guide+footer, rewrites `ChannelLayout`. Test `tests/test_senku_channel_update.py`. **Task K reuses this**, adding: torrent partial-download + Gojo reply-to-main + thumbnail-as-step-1.
- **Per-entry thumbnail infra ALREADY exists.** `SenkuThumbnailAdapter.next_pending(code)` / `is_complete(code)`; selections are per `(code,index)` in `shared/distribution_cache.py`. The wizard just hardcodes `index=0` (`bots/senku/handlers/wizard.py:~700`). **Task D = loop + approve/reject buttons.**


---

## 3. TASKS

### TASK A — Persist franchise entries → finish S01P02E01 + episode renumbering (THE LINCHPIN; do first)
**Why:** Vanitas S1(12)+S1P2(12)=24 showed `S01E13`. `_map_episode_to_part` (`download_service.py:1942`) reads `franchise_data["entries"]`, which is never written, so it returns `None` and packs/naming/progress all lose the part. **Plus:** episodes must restart at 1 per part — S01P02 files are `E01–E12`, not `E13–E24`.

**A1. Persist the mapping at request-commit.**
- File `bots/lelouch/handlers/requests.py`, function `_finalize` (~423–484) builds `franchise_json` and calls `RequestService(container).submit(..., franchise_data=franchise_json)`. Batch equivalent: `bots/lelouch/handlers/batch.py` commit (`keep`/`franchise_data`).
- Before submit, build the mapping and attach entries. Use the SAME resolution path the pipeline uses:
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
  (`kind.value` is lowercase `"season"|"movie"|"special"` — matches what `_map_episode_to_part` checks: `e.get("kind")=="season"`.) Do this in BOTH the single-request `_finalize` and the batch commit so every Request carries `entries`.

**A2. Rewrite `_map_episode_to_part` to return `(part, renumbered_episode)`.** Currently it returns `part | None`. Change to return `tuple[int|None, int]` — part number AND the episode-within-part. For S01P02 ep 13 from the torrent → `(2, 1)`. Logic: find the part via cumulative boundaries (current logic), then `renumbered = episode_num - sum(eps for prior parts in same season)`. Single-part seasons still return `(None, episode_num)` — no renumber.

**A3. Thread the renumbered episode through `_record_file`.** Currently `MediaFile.episode = ep.number` (the torrent's raw number). Change to:
```python
part, renumbered = _map_episode_to_part(ep.number, ep.season, req.franchise_data or {})
session.add(MediaFile(..., season_part=part, episode=renumbered, ...))
```
So `MediaFile.episode` stores the **renumbered** value (1–12 for part 2), not the raw 13–24.

**A4. Filename + progress use renumbered episode.** `nekofetch/services/processing/stages.py:604–642` renders filename; it reads `MediaFile.episode`, which is now renumbered (A3) — **verify no second adjustment needed**. Progress card (`nekofetch/ui/progress.py:209`) currently shows `E{int(current_episode):02d}` — pass the renumbered value from the mapping. Template default still changes to `{title} S{season}{season_part}E{episode} ...` (add `{season_part}`).

**A5. Re-ask filename template when a part is present.** (unchanged from prior)

**A6. TEST** `tests/test_episode_part_mapping.py`: `franchise_data={"entries":[{"kind":"season","season_number":1,"season_part":1,"episodes":12},{"kind":"season","season_number":1,"season_part":2,"episodes":12}]}`. Assert `_map_episode_to_part(13,1,fd) == (2, 1)`, `(24,1,fd) == (2, 12)`, `(12,1,fd) == (1, 12)`, `(1,1,fd) == (1, 1)`; single-entry season → `(None, <unchanged>)`. Also assert a `MediaFile` created with raw ep=13 stores `episode=1, season_part=2`.

### TASK B — Two entry cards for a split season (packs already split)
**Already done:** pack grouping/order in `publishing_service._upload_packs` (560/575) yields one `StoragePack` per `(season, season_part, resolution, audio, entry_id)` once Task A populates `season_part`. **Verify** the end sticker is per pack (`storage_channel_service.py:231`, inside `upload_pack`) — it is.

**What to add:** ensure Senku posts **one `season_card` per `(season, season_part)`**, not per season. In `shared/senku_publisher.py` (`_build_posts` ~447, `_send_posts` ~856) and `nekofetch/services/bot_content.py` (`_build_franchise_watch_guide` ~960, season lines), entries are keyed by `anilist_id`. Vanitas S1 and S1P2 are **separate AniList entries** with distinct `anilist_id`, so they should already produce two cards **if** both appear in the franchise walk. **Confirm** by reading `_build_update_cards`/card build: does it iterate franchise entries (per `anilist_id`) or collapse by `season_number`? If it collapses by season_number, change it to iterate per entry (`anilist_id`, carrying `season_part`) so both parts get a card + a watch-guide line. Card `kind` stays `season_card`; label uses `season_part` (e.g. "Season 1 Part 2").

**TEST** `tests/test_two_part_cards.py`: with a franchise of two TV entries sharing season_number=1 (parts 1,2), assert the post/layout builder emits two `season_card` entries with distinct `anilist_id` and correct labels.

### TASK C — Edit pack captions from Levi (persist + live edit)
**Facts:** caption is generated on the fly by `nekofetch/services/bot_naming.py::build_pack_caption(...)` (427) via `storage_channel_service.header_text()` (80→110); **not stored.** `StoragePack.header_message_id` (models.py:236) is the message to edit; edit API `edit_message_caption` (used in `main_channel_service.py:456`, `index_channel_service.py:558`).

**Owner's mental model (two distinct things — don't conflate):**
- **Filename title** (used in `S01E01`-style filenames): short, filesystem-safe (e.g. "Vanitas no Carte"). When the owner changes the filename title on ONE entry during naming, it propagates the **title portion** to ALL entries of that show — but each entry keeps its own `S1`/`S1P2` label. See A7 below.
- **Pack caption** (the header text on the storage pack, can be longer): editable separately in Levi. The owner may set filename="Vanitas no Carte" but caption="The Case Study of Vanitas". THIS task is the caption editor.

**Do:**
1. **Add a nullable `caption` column** to `StoragePack` (models.py ~215–260): `caption: Mapped[str | None] = mapped_column(Text)`. Add an Alembic migration if the repo uses Alembic (grep `alembic/versions`); else it's created by `Base.metadata.create_all` on dev. Persist the generated caption in `storage_channel_service._persist` (~245–274) so existing packs backfill on next upload; when `caption` is set, `header_text()` should prefer the stored value.
2. **Levi menu:** add `("✏️ Edit Captions", cb("levi","captions"))` to BOTH `/start` (`bots/levi/app.py:~193`) and the `levi|home` callback (`~101`). **Refactor Levi's menu to a single `_home_rows`-style builder** (mirror the Gojo precedent) so the two never drift — this also satisfies the cross-bot menu-consistency rule.
3. **Flow:** `levi|captions` → list **one row per entry** = distinct `(anime_doc_id, season, season_part)` across `StoragePack` (dedupe resolutions/audio). Label via `build_pack_caption`'s entry label (e.g. "Vanitas — Season 1 Part 2"). Tap entry → `FSM(container.redis, bot="levi_caption").set(user_id,"edit_caption", anime_doc_id=..., season=..., season_part=...)`; prompt "send the new caption". Register a text handler at a UNIQUE group (e.g. `group=14`) modeled on `naming_confirm_handler.py`.
4. **Save:** update `StoragePack.caption` for all rows of that `(anime_doc_id, season, season_part)`; then `for pack: await levi.edit_message_caption(pack.channel_id, pack.header_message_id, new_caption)`. Filename NEVER changes. If a `ChannelContentBackup` card exists for this entry, update its stored caption too (so a restore uses the corrected text). Persist to whichever store holds it (Postgres primary; mirror to Redis/Mongo if the caption is cached there).
5. **Owner/staff gate** with `is_owner`/`is_staff` consistent with other Levi owner-only tools.
6. **TEST** `tests/test_levi_caption_edit.py`: seed 2 entries × 3 resolutions, run the save path with a stub client recording `edit_message_caption` calls; assert only the targeted entry's rows got the new `caption` and one edit per stored `header_message_id`.

**A7 — Filename-title propagation (belongs to the naming flow, Task A).** When the owner edits the filename title for one entry in the naming confirm, apply that title to ALL entries of the same `anime_doc_id` (the name portion only). Each entry's `S{season}{season_part}` stays as-is — S1 stays S1, S1P2 stays S1P2; only `{title}`/`{short_title}` changes across entries. Store the chosen title on the request (e.g. `franchise_data["_display_title"]`) so every entry's rename uses it. **TEST:** editing the title for the S1P2 entry updates the S1 entry's rendered filename title too, while S1 keeps `S01...` and S1P2 keeps `S01P02...`.


### TASK D — Per-entry thumbnail approve/reject in Senku
**Facts:** `bots/senku/handlers/wizard.py` thumbnail loop (~698–863) fetches `cache.get_entry(code, 0)` — hardcoded index 0. Per-entry selection storage already exists: `shared/distribution_cache.py::Selection` keyed `(code,index)` (`get_selection`/`set_selection` ~317–369, `all_done` ~386). Adapter `SenkuThumbnailAdapter.next_pending(code)`/`is_complete(code)` already exist. Render: `nekofetch/services/thumbnail_service.py::render_thumbnail(...)` (336) → WebP under `data/thumbnails/`.

**Do:**
1. In `_enter_thumbnails` (~698) replace `get_entry(code,0)` with `entry = await thumbs.next_pending(code)`; if `None`, go to watch-order/publish. Track the current index in FSM data so pick callbacks (`senku|wiz|pick|<code>|<index>|<asset>|<n>`, already carry `index`) target the right entry.
2. After `_thumb_generate` renders the preview, show **Approve / Redo** buttons (new callbacks `senku|wiz|thumbok|<code>|<index>` and `senku|wiz|thumbredo|<code>|<index>`):
   - **Approve** → `cache.set_selection(code, index, done=True)` then `_enter_thumbnails` again (advances to `next_pending`, or finishes).
   - **Redo** → clear THIS entry's selection only (`set_selection(code,index, asset="logo"/"poster"/"backdrop"=None, done=False)` or add a `clear_selection(code,index)` to distribution_cache) and re-enter the logo step **for this same index**. Do NOT touch other entries (they stay `done`).
3. Because approve/reject advance via `next_pending`, a reject on entry 2 restarts entry 2 only — entry 1 remains confirmed. This satisfies the exact requirement.
4. **TEST** `tests/test_thumb_per_entry_fsm.py`: a pure state-machine helper `advance(entries, index, action)` → `(next_index, substate)`; assert approve advances 0→1, reject at 1 stays at 1 with substate "select_logo", 0 stays done; `all_done` gates completion.

### TASK E — Thumbnail editing (Senku for distribution, Gojo for main) — [CONFIRM WITH OWNER: HTML host]
**Facts:** `render_thumbnail` writes the HTML to `data/thumbnails/assets_{title}/thumbnail.html` then the WebP (thumbnail_service.py ~441–459). Template `thumbnail/index.html` tokens: `{{TITLE}} {{NATIVE_TITLE}} {{ROMAJI_TITLE}} {{SYNOPSIS}} {{GENRE_PILLS}} {{STUDIO}} {{TMDB_RATING}} {{ANILIST_SCORE}} {{LOGO_IMAGE}} {{POSTER_IMAGE}} {{BG_IMAGE}} {{FLAG_IMAGE}}` (substitution ~419–439). Field gathering: `gather_thumbnail_fields(container,title,anime_doc_id)` (131). Main-post record: `MainChannelService` (`_record` 481, `publish` 423); reply/edit via `client.edit_message_media` / `edit_message_caption`.

**Storage decision — RECOMMENDATION (confirm before coding):** persist the thumbnail's **input fields** (the dict that produced it: logo_url, poster_url, bg_url, synopsis, rating, genres, studio, titles) in the DB, not just the WebP. Add a small table `thumbnail_source(anime_doc_id, anilist_id, fields JSONB, html TEXT NULL, updated_at)` (or a JSONB column on `StoragePack`/`ChannelPost`). This is durable + transactional + needs no external host. The owner floated Catbox/GitHub — do **NOT** hardcode an external host without sign-off. Fallback chain when editing: stored fields → re-gather via `gather_thumbnail_fields` → prompt user to regenerate.

**Do:**
1. Persist fields on every render (hook `render_thumbnail` callers in the wizard + publishing to write the `thumbnail_source` row).
2. **Senku `/edit_thumbnail`** (owner/staff): pick anime → pick entry → menu (Edit Logo / Poster / Backdrop / Synopsis / Rating / Genres). Asset edits reuse the wizard's existing gallery pickers; text edits use the FSM text-capture pattern. Re-render via `render_thumbnail`, update the `thumbnail_source` row, re-post/edit the distribution card (Senku owns that channel), and update the `ChannelContentBackup` card.
3. **Main-channel edits go through Gojo** (Senku has no main access). Route via `pipeline_manager`/`container.admin_client`: Senku signals Gojo (or the owner runs the same edit in Gojo). Gojo re-renders, `edit_message_media` on `ChannelPost.main_message_id`, and updates `PublishedPostBackup`. Provide the SAME edit UI in Gojo for main posts.
4. All edits update the DB (source row + relevant backup) so a later restore uses corrected values.
5. **TEST** `tests/test_thumbnail_edit.py`: seed a `thumbnail_source` row, apply a synopsis edit through the service function, assert the row's `fields["synopsis"]` changed and a re-render was requested (stub the renderer). Assert main-channel path updates `PublishedPostBackup`.

### TASK E2 — Main-channel thumbnail/caption metadata rules (mostly VERIFY, some fix)
The main-channel post's **generated thumbnail** differs from a distribution entry card. Confirm each rule holds; fix if not.
1. **Synopsis = TMDB franchise overview, NOT AniList per-entry.** ALREADY IMPLEMENTED — `main_channel_service.py:164–165, 250–277` ("TMDB's franchise-level synopsis is preferred for the main post per spec"); the entry-card thumbnail keeps AniList's per-season synopsis. Just verify the main-post thumbnail render is fed `facts.overview` (TMDB), not the AniList synopsis.
2. **AniList rating = average of ALL franchise entries.** ALREADY IMPLEMENTED — `main_channel_service.py::_avg_score_pct` (38–49), used at 352/380. Verify the thumbnail's `anilist_score` uses this franchise average on the main post.
3. **Language label rule (main-channel thumbnail AND main-channel caption if audio shown there).** The label is franchise-level: **if we provide English (dub/dual) for even ONE entry, the whole show reads "English & Japanese"** — a season that's sub-only, or extras that are sub-only, do NOT downgrade it. Find where the `languages` set feeding `language_label`/the thumbnail is built for the main post (grep `languages=` in `main_channel_service.py:391`, `gather_facts`, and `bot_content.py::_gather_metadata` ~509). Ensure it UNIONs languages across all included entries (so one English entry ⇒ English present) rather than reading a single entry. `language_label` (`bot_naming.py:143`) already renders "English & Japanese" from the set; the fix (if any) is upstream set-construction. Apply the same to the main-channel caption's audio/language line if it shows one.
4. **TEST** `tests/test_main_channel_meta.py`: build facts from entries where only ONE entry has English audio and the rest are sub-only → assert language label == "English & Japanese"; assert rating == average of all entry scores; assert synopsis source is the TMDB overview field.

### TASK E3 — Episode-count line format ("25 + 3 extras", "+ 2 movies")
The main-channel post (and/or info card) has an **episodes** section. Format it as:
- Total **main seasonal** episodes as the base number (e.g. `25`).
- If there are extras (OVA/ONA/Special), append ` + N extras` where N = total extra EPISODES across all extra entries (e.g. two OVAs of 2 eps + 1 ep = `+ 3 extras`). If exactly one extra episode → ` + 1 extra` (singular).
- If there are movies, append ` + N movie(s)` (e.g. `+ 1 movie`, `+ 2 movies`, `+ 3 movies`).
- Example composite: `25 + 3 extras + 2 movies`.
**Do:** find where the episode count is rendered (grep `episodes`, `{episodes}`, `ep count`, `franchise_episodes` in `main_channel_service.py` caption build + `bot_content.py` info card). Build the string from `franchise_data["entries"]`: sum `episodes` for `kind=="season"` entries → base; sum `episodes` for `kind=="special"` → extras; count `kind=="movie"` → movies. Handle singular/plural. **TEST** `tests/test_episode_count_label.py`: assert `25`, `25 + 1 extra`, `25 + 3 extras`, `25 + 2 movies`, `25 + 3 extras + 2 movies` for the matching entry sets.

### TASK F — Encode in RELEASE order AND episode order
**Requirement (clarified):** BOTH levels — entries in release order (S1 → S1P2 → S2 → S2P2 → …) **and** episodes ascending within each (E1 → E2 → E3, never E1 → E4).
**Facts:** queue claims **jobs** not episodes (`queue_repo.py::next_queued` (15) = `ORDER BY priority ASC, created_at ASC`). Within a job, the encode stage iterates `ctx.files` (`stages.py:~604`).
**Do:**
1. Sort the encode iteration list by the full key `(season, season_part or 0, episode)` ascending right before the encode/processing loop (`ctx.files` in `stages.py`). This handles episode-order within a request AND part-order within a season.
2. For cross-request order (S2 request vs S1 request), keep `priority, created_at` (S1 created first → release-ordered). Add `processing_order` only if reading proves neither sort suffices.
3. **TEST** `tests/test_encode_order.py`: feed files as `[S1E3, S1E1, S1P2E2, S1E2, S1P2E1]` → assert iteration yields `S1E1, S1E2, S1E3, S1P2E1, S1P2E2`.

### TASK G — DDL option missing in "Begin Now"
**Facts:** source selection is in the admin review flow mounted on Levi (`bots/levi/handlers/__init__.py:~40` mounts `nekofetch/bots/admin/handlers/review`); source callbacks look like `staff|rsource|<code>|<src>`. DDL client exists (`nekofetch/sources/ddl.py`).
**Do:** grep `Begin Now`, `rsource`, `Open Downloader`, `DDL`, `direct` in `bots/levi/handlers/tasks.py` + `nekofetch/bots/admin/handlers/review.py` + `nekofetch/ui/screens.py`. Find the source keyboard builder and the reason DDL is absent (missing row or a conditional). Add the DDL row matching the other options' style; confirm the `staff|rsource|<code>|ddl` branch has a handler (wire it to the existing DDL flow if missing). **TEST:** assert the source keyboard builder includes a DDL button under the same conditions as Torrent/Website.

### TASK H — Levi/Lelouch batch "board" button → "Open Tasks"
**Facts:** batch commit screen `bots/lelouch/handlers/batch.py:547–554` shows buttons `[[V.BTN_QUEUE→lelouch|queue|0, V.BTN_HOME→lelouch|home]]`. `lelouch|queue` shows request stats ("The Board"), NOT Levi tasks. **Lelouch and Levi are separate bot clients — a callback can't cross bots.** Precedent: `shared/access_gate.py::lelouch_link` (66) resolves a bot's `https://t.me/<username>` via `pipeline_manager`.
**Do:** add a `levi_link(container)` helper (mirror `lelouch_link`, using `mgr.levi`). On the batch-done screen add a **URL button** "📋 Open Tasks" → `levi_link(container)` (a deep link into Levi where the admin runs `/tasks`). Omit the button if the link can't resolve (never show a dead button). Keep Home. **TEST** `tests/test_batch_done_button.py`: with a stub pipeline_manager exposing a Levi username, assert the batch-done keyboard contains a URL button to `t.me/<levi>`.


### TASK J — Monthly ban recovery + full channel rebuild
**Facts (already substantial):** monthly sweep registered in `nekofetch/bots/manager.py:703` (`entity_full_check_days`, default 30) → `_monthly_full_check` (385) → `check_all_entities` (136) → `_probe_entity` → ban markers (`maintenance_service.py:34`). On ban: disable + `BotOrchestratorService.recreate_bot(anime_doc_id)` (`bot_orchestrator.py:77`) which pre-backs-up (`BackupService.record_distribution_channel`), deletes old, creates fresh, `_restore_channel` → `BackupService.restore_distribution_channel` (`backup_service.py:425`), regenerates content, refreshes index. Warm-up exists: `SenkuPublisher._warm_global_search` (~1487). Main/index manual recovery: `/recovermain`, `/recoverindex` (`nekofetch/bots/admin/handlers/commands.py:184/228`).

**What needs fixing/adding:**
1. **Admin assignment messages.** Gojo DMs a free admin via the existing channel-creation message flow (reuse the SAME recurring artwork, UI layout, character voice from the normal Senku channel-creation wizard — find those in `bots/senku/handlers/wizard.py::_ask_channel` and related). Light modifications for ban-recovery context: "Restoring a banned channel — create a replacement or use the userbot." Admin creates the channel, gives it a username, **adds the PFP (profile picture)**, removes the "changed photo" service messages, adds Senku + Gojo as admins. That's all the human does.
2. **Button-link bug.** `restore_distribution_channel` re-posts entry cards with verbatim `button_data` → Download buttons keep the OLD invite link. Fix: after restore, call `BotManagementService.bind_title` (or the `BotContentService.generate_posts` path) to rebuild buttons with the new `invite_link`. Invoke inside `recreate_bot` after `_restore_channel`.
3. **Main-channel Download button + reply.** After the restore, go to the main-channel post (`ChannelPost.main_message_id` for `anime_doc_id`), **edit the Download button's invite link** to the new channel's (real button, not text), **then reply** to that post with a block-quoted, bulleted, en.json-editable message announcing the restore — including a **"click here" hyperlink** to the new channel. Add `"ban_recovery_reply": "<blockquote><b>{title}</b>\n• The channel was banned and has been restored\n• <a href=\"{channel_link}\">Click here</a> to access the new channel</blockquote>"` to `resources/language/en.json` + constant `BAN_RECOVERY_REPLY` to `messages.py::M`. Send via `MainChannelService`: `await admin_client.send_message(main_channel_id, t(M.BAN_RECOVERY_REPLY, ...), reply_to_message_id=main_msg_id, parse_mode=ParseMode.HTML)` — **no `reply_markup`**.
4. Confirm warm-up runs before re-posting, and watch-guide deep links are remapped (the restore already remaps `{BOT_QUAL#...}` — verify).
5. **TEST** `tests/test_ban_recovery_full.py`: seed `ChannelContentBackup` with stale button links, run restore+rebind+main-edit+reply path with stub client, assert (a) re-posted cards have NEW invite link, (b) guide references new message IDs, (c) main-post Download button updated, (d) reply posted with text hyperlink + no buttons.

### TASK K — Update flow (new season/part appended) — reuses existing append
**Facts:** append already implemented — `publishing_service.py:373` `is_update_entry` → `SenkuPublisher.update_distribution_channel` → `_append_and_refooter` (deletes guide/dividers/footer, appends new `season_card`, re-posts guide+footer, rewrites `ChannelLayout`); test `tests/test_senku_channel_update.py`. Handoff/assignment via `shared/handoff.py` + `AdminAssignmentEngine.assign`.

**What's missing (add these):**
1. **Torrent partial download.** When admins supply a torrent that contains S1+S2 (or S1+S1P2) but the request is only the new entry, download ONLY the new entry's episode range. Find file selection in the download path (grep `torrent`, file-index selection, `select_files`, `qbittorrent` in `nekofetch/sources/`). Use the new entry's `(episodes, season_part)` from `franchise_data["entries"]` (Task A) to compute the wanted episode window (e.g. S1P2 = eps 13–24, S2 = its own 1–N) and select only those files. Handle: 13 separate torrents (one per ep), one combined torrent (subset), and a season-pack torrent.
2. **Thumbnail becomes step 1 on updates.** On the update branch, Senku must NOT create a channel; it starts at thumbnail generation for the new entry only. Gate the wizard: if `is_update_entry` (channel already exists for `anime_doc_id`), skip channel-create states and enter `_enter_thumbnails` for the new entry index. (Task D's per-entry loop makes this natural.)
3. **Gojo replies to the main post.** After the append completes, Gojo replies to `ChannelPost.main_message_id` (look it up by `anime_doc_id`) with an **en.json-driven, block-quoted, bulleted** message containing a **channel hyperlink** and **NO buttons**:
   - Add to `resources/language/en.json` a key e.g. `"season_update_reply": "<blockquote><b>{title} — {entry_label}</b>\n• {episodes} episodes • {quality}\n• <a href=\"{channel_link}\">click here</a> to watch</blockquote>"` (use the `•` bullet; owner may reword — that's the point of en.json).
   - Add `SEASON_UPDATE_REPLY = "season_update_reply"` to `nekofetch/localization/messages.py::M`.
   - Send: `await container.admin_client.send_message(main_channel_id, t(M.SEASON_UPDATE_REPLY, ...), reply_to_message_id=main_msg_id, parse_mode=ParseMode.HTML)` — **no `reply_markup`**. Put this in `MainChannelService` as a new method `reply_update(anime_doc_id, entry_label, episodes, quality, channel_link)` and call it from the update branch after Senku finishes.
4. **TEST** `tests/test_update_flow.py`: (a) file-selection picks only the new entry's episodes from a combined torrent listing; (b) update branch skips channel creation; (c) `reply_update` posts a reply to the recorded `main_message_id` using the en.json template with a hyperlink and no buttons (assert `reply_markup is None`).

**K5. Update-DURING-redo (owner now wants this — NOT deferred).** Scenario: owner redoes a series early in the month; a new season aired yesterday but the monthly check hasn't run. So the redo flow must OFFER the update inline and let the owner choose.
- In the `/redo` flow (`RedoService` / `bots/lelouch/handlers/redo.py`), after resolving the franchise, detect entries present in `franchise_data` but NOT yet in the channel (compare to existing `ChannelLayout`/`StoragePack` entries for `anime_doc_id`). If new entries exist, show a prompt: "Season N is available — include it in this redo? [Yes] [No]".
- **If Yes:** run the normal update flow (K1–K3) for the NEW entry: download its episodes, generate ONLY the new entry's thumbnail, append its `season_card` (remove guide+divider+footer → add new card → re-post updated guide+divider+footer), then Gojo replies to the main post (K3).
- **For the entry being redone (e.g. S1):** its thumbnail already exists — do NOT regenerate for the user; just replace its Download/quality buttons with the fresh packs' links (the redo-relink path). See Task O for metadata refresh that DOES force regeneration (rating/episode-count/language changes).
- **If No:** redo only the selected entry; skip the update.
- **TEST** extend `tests/test_update_flow.py`: redo with a detectable new season → prompt path invoked; Yes → append+reply for new entry only; No → no append.

### TASK L — Special/OVA handling + naming preview
**Facts:** classifier `nekofetch/services/processing/stages.py::classify_kind` (172) + `_content_type_label` (187); extras filtered in `download_service.py:213–229` (keeps `kind=="episode"` when an explicit list isn't given). MAL client with episode names EXISTS: `nekofetch/sources/telegram/myanimelist.py::MyAnimeListClient.episode_titles(mal_id, max_pages=5) -> [{number,title}]` (663).

**Do:**
1. **Skip unselected specials.** If `franchise_data["entries"]` has no SPECIAL/OVA entry, exclude extra files (13.5 etc.) — extend the existing filter (213) to also drop `kind in {"special","ova","ona","extra"}` unless a matching mapping entry is `included`.
2. **Single OVA → auto-map** to that entry.
3. **Multiple OVAs → name-match.** Fetch `episode_titles` from MAL (resolve MAL id from the mapping/AniList→MAL); fuzzy-match torrent filenames to episode titles (use `difflib.SequenceMatcher` ratio > 0.8; no new dep). Auto-map on confident match.
4. **Fallback → ask the user.** FSM flow listing unmatched OVA files vs OVA entries; user pairs them (buttons or "file → entry"). Persist the mapping so re-download reuses it.
5. **Naming/caption preview for OVAs/movies** (not just seasons): `NamingConfirm.confirm` must run for extra entries too, showing the proposed filename + caption preview. For seasons the template just swaps `{season}`; for OVAs/movies use `special_template`/`movie_template` (`config.py:258–265`).
6. **TEST** `tests/test_special_mapping.py`: (a) no OVA entry → 13.5 excluded; (b) one OVA → auto-mapped; (c) two OVAs with MAL titles matching filenames → auto-mapped; (d) no match → mapping prompt path invoked.

### TASK M — Movie >2000 MB: 2-pass compress to ≤2000 MB @1080p, else split
**Facts:** encoder `nekofetch/sources/_transcode.py::_encode` (445) builds the ffmpeg cmd (libx264, `-crf` from `_CRF={1080:21,...}` line 22, `-preset fast -tune animation`); oversize heuristic `is_oversized_1080` (348) exists but **there is NO 2000 MB cap and NO splitting anywhere.** Movies classified via `classify_kind` (KIND_MOVIE). Encode stage `stages.py::EncodeStage` (1350), upload in `publishing_service._upload_packs` (~654 `storage.upload_pack`).

**Do:**
1. Add `_encode_to_target_size(src, out, target_mb=1990, height=1080)` in `_transcode.py`: probe duration (there's already an ffprobe helper — grep `ffprobe`/`_duration`), compute video bitrate `= (target_mb*8*1024)/duration_s - audio_kbps`, run **2-pass** libx264 (`-pass 1`/`-pass 2 -b:v <calc>k`), keep `-vf scale=-2:1080`, audio AAC 128k. Verify output ≤ 2000 MB (hard: not one MB over — target 1990 for safety).
2. Hook it for MOVIE packs before upload in `_upload_packs` (~650): if `content_type==Movie` and file > 2000 MB → compress; re-check.
3. **If still >2000 MB → split.** Add `split_movie(src, target_mb=1990) -> [part1, part2, ...]` using ffmpeg segment by computed time (or `-fs`), name `"{title} Part 1"`, `"{title} Part 2"`; upload as separate files within the same pack (or sub-packs with `entry_id` suffix `_p1/_p2`) so the pack reads "Movie (2 parts)". Ensure `MediaFile`/pack metadata distinguishes the parts.
4. **TEST** `tests/test_movie_size.py` (mock ffmpeg/ffprobe + `os.stat`): (a) 1.8 GB → no action; (b) 2.5 GB compressible → single compressed file ≤2000 MB (assert bitrate math); (c) 4 GB not compressible → split into 2 parts. Assert exact 2000 MB boundary handling (2001 MB triggers, 2000 does not).

### TASK N — RESEARCH ONLY: userbot+bot-token to add buttons to a user's message [CONFIRM WITH OWNER]
Research whether a userbot (user session) can attach an inline keyboard to a message the USER posted (bots can't edit messages they didn't send). In Pyrogram/Telegram: a bot can only add `reply_markup` to its own messages; a user account cannot attach inline keyboards at all (only bots can). Investigate the real options: (a) the bot reposts the content with buttons and the user deletes theirs; (b) `via_bot`/inline-mode; (c) business-connection APIs. **Deliverable:** write findings to `docs/userbot_edit_research.md` — is it possible, what setup/permissions, recommended approach or the concrete alternative. **Do not implement** anything in production until the owner decides.

### TASK O — Redo metadata refresh (rating/episode-count/language/channel-title)
When `/redo` is run on a title, and metadata has changed since the original publish (a new season was added → episode count increased; dual-audio arrived → language changed; rating shifted), the main-channel thumbnail AND entry-card thumbnails must regenerate, captions update, and channel metadata (title, PFP, service messages) refresh.

**Facts:** `/redo` is `shared/redo_service.py` + `bots/lelouch/handlers/redo.py`. Main-post publish: `MainChannelService.publish`. Distribution: `SenkuPublisher`. Thumbnail render: `thumbnail_service.py::render_thumbnail` + stored source in `thumbnail_source` (Task E1). Channel title edit: Pyrogram `edit_chat_title`, followed by deleting the "channel name changed" service message.

**O1. Detect metadata changes.** After the redo downloads/encodes fresh files, compare the NEW `franchise_data["entries"]` to the stored `thumbnail_source` fields for each entry + the main post:
- **Episode count** — sum `episodes` for `kind=="season"` entries + extras/movies (Task E3 format). Compare to stored.
- **Language** — union languages across all entries; apply "one English entry ⇒ English & Japanese" rule (Task E2). Compare to stored main-post language.
- **Rating** — franchise-average AniList score (Task E2, already implemented). Compare to stored.
- If ANY differ, regenerate thumbnails for affected entries + main post.

**O2. Regenerate thumbnails ONLY for changed metadata.** For entries being redone that have NO metadata change (e.g. redo for quality upgrade only), do NOT regenerate the thumbnail — just replace Download/quality buttons (the redo-relink path). For entries with changed language/episode-count/rating, call `render_thumbnail` with updated fields, save to `thumbnail_source`, and `edit_message_media` on the entry card (distribution) or main post (Gojo). Update `ChannelContentBackup` + `PublishedPostBackup`.

**O3. Update captions.** Main-channel caption (`MainChannelService._caption`) and distribution entry-card captions use the episode-count line + language label. Re-render and `edit_message_caption` where needed. Also update the stored backups.

**O4. Channel title refresh.** If the franchise title changed in `franchise_data` (e.g. user switched from "Case Study of Vanitas" to "Vanitas no Carte" — Task A clarification), update the distribution channel's title via `edit_chat_title(new_title)`, then delete the "channel name changed to…" service message (grep service-message deletion in `senku_publisher.py::_warm_global_search` line ~1512 for the pattern).

**O5. TEST** `tests/test_redo_metadata_refresh.py`: seed old `thumbnail_source` with rating=75, language="Japanese"; redo with new entries giving rating=82, one English entry → assert thumbnails regenerated for main+affected entries, captions updated with new episode count + "English & Japanese", channel title edited + service message deleted. Assert NO regeneration when only quality changed (redo-relink only).

### TASK P — Lelouch admin panel: "Manage REQ/WRK" + Redo/Clear-DB row (owner-only)
**Facts:** admin panel keyboard is `bots/lelouch/screens.py::admin_panel` (49–66). Current rows include `[("🗂 Manage Requests", cb("mg","reqs",0))]` (62) and `[(V.BTN_CLEAR_DATABASE, cb(BOT,"dbclear"))]` (63). The manage plane is `bots/lelouch/handlers/management.py` (namespace `mg|…`); it currently lists/cancels/reassigns **requests** (REQ). Work items are WRK codes (`kurosoden.shared.work_service`). Redo command handler is `bots/lelouch/handlers/redo.py`; the redo entry callback is `redo|new` (redo.py:104).

**Do:**
1. **Rename + merge to "Manage REQ/WRK".** Change the button label to "🗂 Manage REQ/WRK". In `management.py`'s `mg|reqs` list, include BOTH REQ (Request rows) and WRK (WorkItem rows), each with the same actions the request list already offers (cancel, reassign). Read `management.py` to see how REQ rows are listed/actioned and extend the query + action handlers to cover WRK codes. Label each row with its code prefix so REQ vs WRK is visible.
2. **Redo + Clear-DB row (owner-only).** Replace the standalone Clear-DB row (63) with a single two-button row: `[("🔁 Redo", cb("redo","new")), (V.BTN_CLEAR_DATABASE, cb(BOT,"dbclear"))]`. The `redo|new` callback already exists (redo.py:104) and starts the redo flow. **Gate the whole row to owner only** — `admin_panel` is already admin-scoped, so wrap this row in `if is_owner(...)`. Since `admin_panel` is a pure screen builder, thread an `is_owner: bool` param into it (the caller in `app.py`/handlers has the update object) and only append the Redo/Clear row when true.
3. **TEST** `tests/test_lelouch_admin_panel.py`: `admin_panel(..., is_owner=True)` → keyboard contains a row with BOTH a `redo|new` button and a `dbclear` button, and a "Manage REQ/WRK" button; `is_owner=False` → no Redo/Clear-DB row. Plus a management test: the REQ/WRK list includes a seeded WorkItem with a cancel action.

---

## 4. ORDER & DEPENDENCIES
1. **A first** (episode renumber + persist entries — unblocks B, E2, E3, F, K, L, O).
2. **B, C** (packs/captions).
3. **E2, E3** (main-post metadata rules + episode-count format — mostly verify; feed O).
4. **D → E → E1** (per-entry thumbnails → edit → source storage).
5. **G, H** (small UI fixes, independent).
6. **J, N** (ban recovery; research — independent).
7. **K** (update flow, incl. K5 update-during-redo; needs A, D, E).
8. **L, M** (specials/movies; need A).
9. **O** (redo metadata refresh; needs A, E1, E2, E3).
10. **I = redo Sabikui Bisco, ORB, Vanitas LAST**, via `/redo` (`shared/redo_service.py`), verifying Vanitas → 2 packs, 2 cards, `S01P02E01`–`E12` (renumbered), release+episode-order encode, and any metadata refresh (O).

## 5. DEFINITION OF DONE (every task)
- Matches UI/voice/menu conventions; single-source-of-truth menus (no `/start` vs callback drift).
- en.json-driven copy where specified; DB writes update the matching backup snapshot.
- New unit test green **and** full suite green: `./.venv/Scripts/python.exe -m pytest -q`.
- `py_compile` clean; no remote-DB dependency for verification.
- Code-review pass done; findings fixed. §2 DONE items not regressed.

## 6. FILES CHANGED THIS SESSION (context; do not undo)
`nekofetch/services/stats_service.py`, `bots/gojo/app.py`, `bots/gojo/handlers/schedule.py`, `bots/gojo/handlers/__init__.py`, `nekofetch/services/download_service.py` (`_map_episode_to_part` + `_record_file` wiring, INCOMPLETE per Task A), tests `tests/test_gojo_home_menu.py`, `tests/test_stats_db_driven.py`.



