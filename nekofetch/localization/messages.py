"""Centralized message catalog — the single access point for every visible string.

ALL user-facing text (bot messages, prompts, status/progress, errors, success,
admin output, log-channel cards, notifications, human-readable debug) lives in
``resources/language/<code>.json`` and is reached through this module. Business
logic references a message **key** (a constant on :class:`M`) — never a raw
string — so editing a word, emoji, or bit of HTML in the JSON propagates
everywhere with no code change.

HTML is the default parse mode; every string in the catalog is authored as HTML.

Usage::

    from nekofetch.localization.messages import t, M
    caption = t(M.WELCOME_TITLE, name="Raiyan")
"""

from __future__ import annotations

from pathlib import Path

from pyrogram.enums import ParseMode

from nekofetch.localization.i18n import Localizer

# Default parse mode for everything rendered from the catalog.
PARSE_MODE = ParseMode.HTML

# Absolute path to the catalog, computed from this file's location so it resolves
# correctly no matter what working directory the bot is launched from.
# kurosoden flattens the src/ layer — parents[2] reaches the project root
LANG_DIR = Path(__file__).resolve().parents[2] / "resources" / "language"
_localizer = Localizer(LANG_DIR, default="en")

# The single shared Localizer instance. Handlers and the container MUST use this
# (via ``container.localizer``) rather than constructing their own, so there is
# exactly one catalog in memory and an en.json edit propagates everywhere on the
# next ``reload()`` / restart.
localizer = _localizer


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Resolve a catalog ``key`` to its HTML string, formatting ``{placeholders}``.

    Unknown keys fall back to the default language then to the key itself, so a
    gap is visible rather than crashing.
    """
    return _localizer.get(key, lang=lang, **kwargs)


def reload() -> None:
    """Re-read the JSON catalogs (e.g. after an admin edits a message)."""
    _localizer.reload()


def languages() -> list[str]:
    return _localizer.languages


class M:
    """Message keys. The string value IS the JSON key — reference these, never
    raw text. Grouped by surface for navigation."""

    # ── meta / common ──
    SEP_DOT = "sep_dot"

    # ── welcome ──
    WELCOME_TITLE = "welcome_title"
    WELCOME_BODY = "welcome_body"
    WELCOME_LIBRARY = "welcome_library"

    # ── buttons ──
    BTN_REQUEST_ANIME = "btn_request_anime"
    BTN_MY_REQUESTS = "btn_my_requests"
    BTN_BACK = "btn_back"
    BTN_CANCEL = "btn_cancel"
    BTN_NEXT = "btn_next"
    BTN_PREV = "btn_prev"
    BTN_SERIES_YES = "btn_series_yes"
    BTN_SERIES_NO = "btn_series_no"
    BTN_VERSION_NEITHER = "btn_version_neither"
    BTN_VERSION_BOTH = "btn_version_both"
    BTN_RETRY = "btn_retry"
    BTN_REASSIGN = "btn_reassign"
    BTN_DISMISS = "btn_dismiss"

    # ── my requests ──
    MYREQ_TITLE = "myreq_title"
    MYREQ_EMPTY = "myreq_empty"
    MYREQ_ROW = "myreq_row"
    MYREQ_SUMMARY = "myreq_summary"

    # ── search / confirm ──
    ASK_TITLE = "ask_title"
    SEARCHING = "searching"
    CONFIRM_HEADER = "confirm_header"
    CONFIRM_QUESTION = "confirm_question"
    VERSION_HEADER = "version_header"
    RETRY_TITLE = "retry_title"

    # ── fields (label : value) ──
    F_TYPE = "field_type"
    F_CONTENT = "field_content"
    F_GENRES = "field_genres"
    F_RATING = "field_rating"
    F_ANIME = "field_anime"
    F_STATUS = "field_status"
    F_QUEUE = "field_queue"
    F_REQUEST = "field_request"
    F_BY = "field_by"
    F_SOURCE = "field_source"
    F_NOW = "field_now"
    F_STUCK_AT = "field_stuck_at"
    F_REASON = "field_reason"
    F_SEASONS = "field_seasons"
    F_QUALITIES = "field_qualities"
    F_EPISODES = "field_episodes"
    F_TOOK = "field_took"

    VALUE_TV = "value_tv_series"
    VALUE_MOVIE = "value_movie"
    VALUE_QUEUED = "value_queued"

    # ── request received ──
    REQ_RECEIVED = "req_received"
    REQ_RECEIVED_BODY = "req_received_body"
    F_REQUESTED_BY = "field_requested_by"
    F_REQUESTED_AT = "field_requested_at"
    F_BREAKDOWN = "field_breakdown"

    # ── lifecycle steps (log card) ──
    LC_REQUESTED = "lc_requested"
    LC_PENDING = "lc_pending"
    LC_SOURCE_ASSIGNED = "lc_source_assigned"
    LC_DOWNLOADING = "lc_downloading"
    LC_PROCESSING_META = "lc_processing_metadata"
    LC_EXTRACTING_SUBS = "lc_extracting_subtitles"
    LC_WATERMARK = "lc_applying_watermark"
    LC_UPLOADING = "lc_uploading"
    LC_PUBLISHED = "lc_published"
    LC_COMPLETED = "lc_completed"

    # ── log card headers ──
    LOG_PROGRESS_TITLE = "log_progress_title"
    LOG_COMPLETED_TITLE = "log_completed_title"
    LOG_BLOCKED_TITLE = "log_blocked_title"

    # ── admin ──
    ADMIN_NEW_REQUEST = "admin_new_request"
    ADMIN_ASSIGN_SOURCE = "admin_assign_source"
    ADMIN_BTN_TELEGRAM = "admin_btn_telegram"
    ADMIN_BTN_WEBSITE = "admin_btn_website"
    ADMIN_BTN_TORRENT = "admin_btn_torrent"
    ADMIN_BTN_DDL = "admin_btn_ddl"
    ADMIN_BTN_REJECT = "admin_btn_reject"
    ADMIN_BTN_AUTOMATIC = "admin_btn_automatic"
    ADMIN_BTN_MANUAL = "admin_btn_manual"
    ADMIN_TG_CHOOSE = "admin_tg_choose"
    ADMIN_TG_MANUAL_PROMPT = "admin_tg_manual_prompt"

    # ── Phase 1: search / confirm ──
    CONFIRM_ADAPTATION_CHOOSE = "confirm_adaptation_choose"
    BTN_CHOOSE = "btn_choose"
    BTN_READ_MORE = "btn_read_more"
    FIELD_TITLE_ROMAJI = "field_title_romaji"
    FRANCHISE_CONTENT = "franchise_content"
    RELATION_GRAPH = "relation_graph"
    UNIT_SEASONS = "unit_seasons"
    UNIT_MOVIES = "unit_movies"
    UNIT_OVAS = "unit_ovas"
    UNIT_ONAS = "unit_onas"
    UNIT_SPECIALS = "unit_specials"
    UNIT_SPINOFFS = "unit_spinoffs"
    UNIT_EPS = "unit_eps"
    FIELD_STUDIO = "field_studio"
    FIELD_SCORE = "field_score"
    FIELD_FORMAT = "field_format"
    FIELD_SYNOPSIS = "field_synopsis"
    SEARCH_NOT_FOUND = "search_not_found"

    # ── website source admin ──
    SITE_PREFERENCE_TITLE = "site_preference_title"
    SITE_PREFERENCE_PROMPT = "site_preference_prompt"
    SITE_PROVIDER_ANIKOTO = "site_provider_anikoto"
    SITE_PROVIDER_KICKASS = "site_provider_kickass"
    BTN_CONFIRM_PRIORITY = "btn_confirm_priority"

    # ── log channel events ──
    LOG_REQUEST_SUBMITTED = "log_request_submitted"
    LOG_REQUEST_APPROVED = "log_request_approved"
    LOG_REQUEST_REJECTED = "log_request_rejected"
    LOG_SOURCE_ASSIGNED = "log_source_assigned"
    LOG_JOB_QUEUED = "log_job_queued"
    LOG_JOB_COMPLETED = "log_job_completed"
    LOG_JOB_FAILED = "log_job_failed"
    LOG_STAGE_COMPLETE = "log_stage_complete"
    LOG_PUBLISHED = "log_published"

    # ── log channel: control-center sections ──
    CC_DASHBOARD_TITLE = "cc_dashboard_title"
    CC_PENDING_TITLE = "cc_pending_title"
    CC_ACTIVE_TITLE = "cc_active_title"
    CC_COMPLETED_TITLE = "cc_completed_title"
    CC_NOTICES_TITLE = "cc_notices_title"
    CC_CATALOG_TITLE = "cc_catalog_title"
    CC_INTRO = "cc_intro"
    CC_INITIALIZING = "cc_initializing"
    CC_UPDATED = "cc_updated"
    CC_EMPTY_PENDING = "cc_empty_pending"
    CC_EMPTY_ACTIVE = "cc_empty_active"
    CC_EMPTY_COMPLETED = "cc_empty_completed"
    CC_EMPTY_NOTICES = "cc_empty_notices"
    CC_EMPTY_CATALOG = "cc_empty_catalog"
    CC_STAT_USERS = "cc_stat_users"
    CC_STAT_DOWNLOADS = "cc_stat_downloads"
    CC_STAT_QUEUE = "cc_stat_queue"
    CC_STAT_FAILED = "cc_stat_failed"
    CC_STAT_PUBLISHED = "cc_stat_published"
    CC_STAT_ROW = "cc_stat_row"
    CC_MOST_REQUESTED = "cc_most_requested"
    CC_MOST_REQUESTED_ROW = "cc_most_requested_row"
    CC_PENDING_ROW = "cc_pending_row"
    CC_ACTIVE_ROW = "cc_active_row"
    CC_ACTIVE_EP = "cc_active_ep"
    CC_ACTIVE_EP_OF = "cc_active_ep_of"
    CC_ACTIVE_VER = "cc_active_ver"
    CC_ACTIVE_STAT_SPEED = "cc_active_stat_speed"
    CC_ACTIVE_STAT_SIZE = "cc_active_stat_size"
    CC_ACTIVE_STAT_SIZE_NOTOTAL = "cc_active_stat_size_nototal"
    CC_ACTIVE_STAT_ETA = "cc_active_stat_eta"
    CC_ACTIVE_STAT_SEP = "cc_active_stat_sep"
    CC_COMPLETED_ROW = "cc_completed_row"
    CC_NOTICE_ROW = "cc_notice_row"
    CC_RESERVED_1 = "cc_reserved_1"
    CC_RESERVED_2 = "cc_reserved_2"
    CC_RESERVED_3 = "cc_reserved_3"
    CC_REQUEST_CARD_TITLE = "cc_request_card_title"
    CC_REQUEST_CARD_BODY = "cc_request_card_body"
    CC_REQUEST_CHOOSE_SOURCE = "cc_request_choose_source"
    CC_AMBIGUITY_TITLE = "cc_ambiguity_title"
    CC_AMBIGUITY_BODY = "cc_ambiguity_body"
    CC_CONVO_LINE = "cc_convo_line"
    CC_FAILURE_TITLE = "cc_failure_title"
    CC_FAILURE_BODY = "cc_failure_body"
    CC_BTN_STOP_EP = "cc_btn_stop_ep"
    CC_BTN_CANCEL_JOB = "cc_btn_cancel_job"
    TOAST_STOPPING = "toast_stopping"
    TOAST_CANCELLING = "toast_cancelling"
    DOWNLOADS_CLEARED = "downloads_cleared"
    CC_ATTENTION_TITLE = "cc_attention_title"
    CC_ATTENTION_BODY = "cc_attention_body"
    CC_BTN_RETRY_EPS = "cc_btn_retry_eps"
    CC_BTN_SWITCH_SRC = "cc_btn_switch_src"
    CC_BTN_PROVIDE = "cc_btn_provide"
    CC_BTN_SWITCH_CONFIRM = "cc_btn_switch_confirm"
    TOAST_RETRY_QUEUED = "toast_retry_queued"
    ATTN_RETRYING = "attn_retrying"
    ATTN_CHECKING_ALT = "attn_checking_alt"
    ATTN_SWITCH_HEADER = "attn_switch_header"
    ATTN_SWITCH_HAS = "attn_switch_has"
    ATTN_SWITCH_LACKS = "attn_switch_lacks"
    ATTN_SWITCH_UNAVAILABLE = "attn_switch_unavailable"
    ATTN_PROVIDE_PROMPT = "attn_provide_prompt"
    ATTN_PROVIDE_DONE = "attn_provide_done"
    ATTN_PROVIDE_FAILED = "attn_provide_failed"
    ATTN_ABANDON_CONFIRM = "attn_abandon_confirm"
    ATTN_ABANDONED = "attn_abandoned"
    CC_BTN_ABANDON = "cc_btn_abandon"
    CC_BTN_ABANDON_CONFIRM = "cc_btn_abandon_confirm"

    # ── Levi live download card (Kuro Sōden — no log channel) ──
    DL_CARD_TITLE = "dl_card_title"
    DL_CARD_EP = "dl_card_ep"
    DL_CARD_VER = "dl_card_ver"
    DL_CARD_STAGE_DOWNLOADING = "dl_card_stage_downloading"
    DL_CARD_STAGE_RETRYING = "dl_card_stage_retrying"
    DL_CARD_STAGE_GENERIC = "dl_card_stage_generic"
    DL_CARD_STAT_SPEED = "dl_card_stat_speed"
    DL_CARD_STAT_SIZE = "dl_card_stat_size"
    DL_CARD_STAT_ETA = "dl_card_stat_eta"
    DL_CARD_STAT_ELAPSED = "dl_card_stat_elapsed"
    DL_CARD_LOW_DISK = "dl_card_low_disk"
    DL_CARD_DONE = "dl_card_done"
    DL_CARD_FAILED = "dl_card_failed"
    DL_BTN_RETRY = "dl_btn_retry"
    DL_BTN_SKIP = "dl_btn_skip"
    DL_TOAST_RETRYING = "dl_toast_retrying"
    DL_TOAST_SKIPPED = "dl_toast_skipped"

    # ── log channel: notice emoji + human labels (category → key) ──
    LOG_EMOJI = {
        "request": "log_emoji_request", "queue": "log_emoji_queue",
        "download": "log_emoji_download", "processing": "log_emoji_processing",
        "publish": "log_emoji_publish", "delivery": "log_emoji_delivery",
        "admin": "log_emoji_admin", "bot": "log_emoji_bot",
        "error": "log_emoji_error", "system": "log_emoji_system",
    }

    # ── commands / help ──
    CMD_START = "cmd_start"
    CMD_HELP = "cmd_help"
    CMD_CANCEL = "cmd_cancel"
    CMD_RELOAD = "cmd_reload"
    CMD_RESETOVERRIDES = "cmd_resetoverrides"
    CMD_CLEARDOWNLOADS = "cmd_cleardownloads"
    BOT_DESC_PLACEHOLDER = "bot_desc_placeholder"
    RELOAD_DONE = "reload_done"
    OVERRIDES_CLEARED = "overrides_cleared"
    HELP_TITLE = "help_title"
    HELP_INTRO = "help_intro"
    HELP_H_COMMANDS = "help_h_commands"
    HELP_H_EVERYONE = "help_h_everyone"
    HELP_H_STAFF = "help_h_staff"
    HELP_H_ADMIN = "help_h_admin"
    HELP_CMD_START = "help_cmd_start"
    HELP_CMD_HELP = "help_cmd_help"
    HELP_CMD_CANCEL = "help_cmd_cancel"
    HELP_CAP_REQUEST = "help_cap_request"
    HELP_CAP_MYREQ = "help_cap_myreq"
    HELP_CAP_REVIEW = "help_cap_review"
    HELP_CAP_QUEUE = "help_cap_queue"
    HELP_CAP_APPROVALS = "help_cap_approvals"
    HELP_CAP_ADMIN = "help_cap_admin"
    CANCELLED = "cancelled"

    # ── staff review / source assignment ──
    REVIEW_TITLE = "review_title"
    REVIEW_EMPTY = "review_empty"
    REVIEW_COUNT = "review_count"
    REVIEW_DETAIL_TITLE = "review_detail_title"
    REVIEW_DETAIL_BODY = "review_detail_body"
    REVIEW_ROW = "review_row"
    SITE_BTN_ANIKOTO_PRIMARY = "site_btn_anikoto_primary"
    SITE_BTN_KICKASS_PRIMARY = "site_btn_kickass_primary"
    SITE_BTN_MIRURO_PRIMARY = "site_btn_miruro_primary"
    WEB_REPORT_LOADING = "web_report_loading"
    WEB_REPORT_FAILED = "web_report_failed"
    WEB_REPORT_TITLE = "web_report_title"
    WEB_REPORT_EXPECTS = "web_report_expects"
    WEB_REPORT_EXPECTS_EPS = "web_report_expects_eps"
    WEB_REPORT_TREE_TITLE = "web_report_tree_title"
    WEB_REPORT_TREE_ROW = "web_report_tree_row"
    WEB_REPORT_COVERAGE_TITLE = "web_report_coverage_title"
    WEB_REPORT_COV_ROW = "web_report_cov_row"
    WEB_REPORT_COV_UNAVAILABLE = "web_report_cov_unavailable"
    WEB_REPORT_ANALYSIS_TITLE = "web_report_analysis_title"
    WEB_REPORT_PICK = "web_report_pick"
    WEB_REPORT_APPROX = "web_report_approx"
    TORRENT_LOADING = "torrent_loading"
    TORRENT_TITLE = "torrent_title"
    TORRENT_INTRO = "torrent_intro"
    TORRENT_EMPTY = "torrent_empty"
    TORRENT_BTN_AUTO = "torrent_btn_auto"
    TORRENT_ROW = "torrent_row"
    TORRENT_QUEUED = "torrent_queued"
    TORRENT_MODE_PROMPT = "torrent_mode_prompt"
    TORRENT_BTN_SEARCH = "torrent_btn_search"
    TORRENT_BTN_PROVIDE = "torrent_btn_provide"
    TORRENT_PROVIDE_PROMPT = "torrent_provide_prompt"
    TORRENT_MAGNET_ACK = "torrent_magnet_ack"
    DDL_PROVIDE_PROMPT = "ddl_provide_prompt"
    DDL_LINK_ACK = "ddl_link_ack"
    TORRENT_FILE_ACK = "torrent_file_ack"
    TORRENT_BTN_CONFIRM = "torrent_btn_confirm"
    TORRENT_MAP_TITLE = "torrent_map_title"
    TORRENT_MAP_CONFIRM = "torrent_map_confirm"
    TORRENT_MAP_BTN_CONFIRM = "torrent_map_btn_confirm"
    TORRENT_MAP_BTN_DETAIL = "torrent_map_btn_detail"
    TORRENT_MAP_BTN_EDIT = "torrent_map_btn_edit"
    TORRENT_MAP_BTN_TOGGLE = "torrent_map_btn_toggle"
    TORRENT_MAP_NO_FRANCHISE = "torrent_map_no_franchise"
    SCOPE_SEASON = "scope_season"
    SCOPE_SEASON_EPS = "scope_season_eps"
    TOAST_QUEUED = "toast_queued"
    TOAST_REJECTED = "toast_rejected"
    STATUS_UPDATING = "status_updating"
    STATUS_QUEUING = "status_queuing"
    MANUAL_QUEUED = "manual_queued"
    MANUAL_QUEUE_FAILED = "manual_queue_failed"

    # ── manual upload wizard ──
    MANUAL_WIZ_COMP_TITLE = "manual_wiz_comp_title"
    MANUAL_WIZ_COMP_PROMPT = "manual_wiz_comp_prompt"
    MANUAL_WIZ_COMP_SEASON = "manual_wiz_comp_season"
    MANUAL_WIZ_COMP_OVA = "manual_wiz_comp_ova"
    MANUAL_WIZ_COMP_MOVIE = "manual_wiz_comp_movie"
    MANUAL_WIZ_COMP_ONA = "manual_wiz_comp_ona"
    MANUAL_WIZ_COMP_SPECIAL = "manual_wiz_comp_special"
    MANUAL_WIZ_COMP_ENTIRE = "manual_wiz_comp_entire"
    MANUAL_WIZ_COMP_DONE = "manual_wiz_comp_done"
    MANUAL_WIZ_AUDIO_TITLE = "manual_wiz_audio_title"
    MANUAL_WIZ_AUDIO_SUBBED = "manual_wiz_audio_subbed"
    MANUAL_WIZ_AUDIO_DUBBED = "manual_wiz_audio_dubbed"
    MANUAL_WIZ_AUDIO_DUAL = "manual_wiz_audio_dual"
    MANUAL_WIZ_AUDIO_MULTI = "manual_wiz_audio_multi"
    MANUAL_WIZ_RES_TITLE = "manual_wiz_res_title"
    MANUAL_WIZ_RES_360P = "manual_wiz_res_360p"
    MANUAL_WIZ_RES_480P = "manual_wiz_res_480p"
    MANUAL_WIZ_RES_540P = "manual_wiz_res_540p"
    MANUAL_WIZ_RES_720P = "manual_wiz_res_720p"
    MANUAL_WIZ_RES_1080P = "manual_wiz_res_1080p"
    MANUAL_WIZ_RES_CUSTOM = "manual_wiz_res_custom"
    MANUAL_WIZ_RES_DONE = "manual_wiz_res_done"
    MANUAL_WIZ_CONFIRM_TITLE = "manual_wiz_confirm_title"
    MANUAL_WIZ_CONFIRM_LINE = "manual_wiz_confirm_line"
    MANUAL_WIZ_CONFIRM_EMPTY = "manual_wiz_confirm_empty"
    MANUAL_WIZ_CONFIRM_BTN = "manual_wiz_confirm_btn"
    MANUAL_WIZ_CHANGE_BTN = "manual_wiz_change_btn"
    MANUAL_INTAKE_PROMPT = "manual_intake_prompt"
    MANUAL_INTAKE_INSTRUCTIONS = "manual_intake_instructions"
    MANUAL_INTAKE_RECEIVED = "manual_intake_received"
    MANUAL_INTAKE_BATCH_DONE = "manual_intake_batch_done"
    MANUAL_INTAKE_NEXT = "manual_intake_next"
    MANUAL_INTAKE_ALL_DONE = "manual_intake_all_done"
    MANUAL_HANDOFF_CHANNEL = "manual_handoff_channel"   # shown in channel: continue in DM
    MANUAL_HANDOFF_BTN = "manual_handoff_btn"           # "Open bot" deep-link button
    MANUAL_HANDOFF_DM_INTRO = "manual_handoff_dm_intro" # first line once resumed in DM
    MANUAL_INTAKE_DONE_DM = "manual_intake_done_dm"     # DM: all files in, cleaned up
    MANUAL_BACK_TO_CHANNEL_BTN = "manual_back_to_channel_btn"
    MANUAL_PROCESSING_TITLE = "manual_processing_title"
    MANUAL_PROCESSING_DONE = "manual_processing_done"
    MANUAL_CANCELLED = "manual_cancelled"
    MANUAL_NO_FILES = "manual_no_files"
    MANUAL_INVALID_FILE = "manual_invalid_file"
    MANUAL_SORT_ERROR = "manual_sort_error"
    MANUAL_WIZ_RES_CUSTOM_PROMPT = "manual_wiz_res_custom_prompt"

    # ── admin home / settings ──
    BTN_REVIEW_REQUESTS = "btn_review_requests"
    ADMIN_BTN_PANEL = "admin_btn_panel"
    ADMIN_HOME_TITLE = "admin_home_title"
    ADMIN_HOME_INTRO = "admin_home_intro"
    ADMIN_BTN_QUEUE = "admin_btn_queue"
    ADMIN_BTN_ANALYTICS = "admin_btn_analytics"
    ADMIN_BTN_STAFF = "admin_btn_staff"
    ADMIN_BTN_BOTS = "admin_btn_bots"
    ADMIN_BTN_SETTINGS = "admin_btn_settings"
    ADMIN_BTN_STORAGE = "admin_btn_storage"
    ADMIN_BTN_APPROVALS = "admin_btn_approvals"
    ADMIN_BTN_BROADCAST = "admin_btn_broadcast"
    SETTINGS_HOME_TITLE = "settings_home_title"
    SETTINGS_HOME_INTRO = "settings_home_intro"
    SETTINGS_SECTION_INTRO = "settings_section_intro"
    SETTINGS_FIELD_ROW = "settings_field_row"
    SETTINGS_VALUE_ROW = "settings_value_row"
    SETTINGS_ON = "settings_on"
    SETTINGS_OFF = "settings_off"
    SETTINGS_EDIT_TITLE = "settings_edit_title"
    SETTINGS_LBL_ABOUT = "settings_lbl_about"
    SETTINGS_LBL_OPTIONS = "settings_lbl_options"
    SETTINGS_LBL_OPTIONS_TITLE = "settings_lbl_options_title"
    SETTINGS_LBL_OPTION_ROW = "settings_lbl_option_row"
    SETTINGS_LBL_PLACEHOLDERS = "settings_lbl_placeholders"
    SETTINGS_LBL_PLACEHOLDER_ROW = "settings_lbl_placeholder_row"
    SETTINGS_LBL_HTML = "settings_lbl_html"
    SETTINGS_LBL_EXAMPLE = "settings_lbl_example"
    SETTINGS_LBL_CURRENT = "settings_lbl_current"
    SETTINGS_EDIT_HINT = "settings_edit_hint"
    SETTINGS_LIST_HINT = "settings_list_hint"
    SETTINGS_EDIT_DONE = "settings_edit_done"
    SETTINGS_EDIT_BAD = "settings_edit_bad"
    SETTINGS_TOAST_TOGGLED = "settings_toast_toggled"
    SETTINGS_STATE_ON = "settings_state_on"
    SETTINGS_STATE_OFF = "settings_state_off"
    QUEUE_TITLE = "queue_title"
    QUEUE_EMPTY = "queue_empty"

    # ── approvals ──
    APPROVALS_TITLE = "approvals_title"
    APPROVALS_EMPTY = "approvals_empty"
    APPROVALS_DETAIL_TITLE = "approvals_detail_title"
    APPROVALS_DETAIL_BODY = "approvals_detail_body"
    APPROVALS_VALUE_YES = "approvals_value_yes"
    APPROVALS_VALUE_NO = "approvals_value_no"
    APPROVALS_PUBLISHED = "approvals_published"
    APPROVALS_CANCELLED = "approvals_cancelled"
    APPROVALS_TOAST_PUBLISHED = "approvals_toast_published"
    APPROVALS_TOAST_REPROCESSED = "approvals_toast_reprocessed"
    APPROVALS_TOAST_CANCELLED = "approvals_toast_cancelled"
    BTN_PUBLISH = "btn_publish"
    BTN_REPROCESS = "btn_reprocess"

    # ── staff & users ──
    STAFF_TITLE = "staff_title"
    STAFF_EMPTY = "staff_empty"
    STAFF_MEMBER_ROW = "staff_member_row"
    STAFF_MEMBER_DOT_ACTIVE = "staff_member_dot_active"
    STAFF_MEMBER_DOT_BANNED = "staff_member_dot_banned"
    STAFF_FLAG_BANNED = "staff_flag_banned"
    STAFF_BTN_REMOVE = "staff_btn_remove"
    STAFF_BTN_BAN = "staff_btn_ban"
    STAFF_BTN_UNBAN = "staff_btn_unban"
    STAFF_BTN_ADD = "staff_btn_add"
    STAFF_ADD_PROMPT = "staff_add_prompt"
    STAFF_ADD_BAD_ID = "staff_add_bad_id"
    STAFF_ADD_DONE = "staff_add_done"
    STAFF_TOAST_DEMOTED = "staff_toast_demoted"
    STAFF_TOAST_BANNED = "staff_toast_banned"
    STAFF_TOAST_UNBANNED = "staff_toast_unbanned"

    # ── distribution bots ──
    BOTS_TITLE = "bots_title"
    BOTS_EMPTY = "bots_empty"
    BOTS_ROW = "bots_row"
    BOTS_DOT_ACTIVE = "bots_dot_active"
    BOTS_DOT_DISABLED = "bots_dot_disabled"
    BOTS_PENDING_HEADER = "bots_pending_header"
    BOTS_PENDING_ROW = "bots_pending_row"
    BOTS_BTN_BIND = "bots_btn_bind"
    BOTS_BTN_ADD = "bots_btn_add"
    BOTS_BTN_CREATE = "bots_btn_create"
    BOTS_BTN_RECREATE = "bots_btn_recreate"
    BOTS_BIND_PROMPT = "bots_bind_prompt"
    BOTS_ADD_PROMPT = "bots_add_prompt"
    BOTS_VALIDATING = "bots_validating"
    BOTS_REGISTER_FAILED = "bots_register_failed"
    BOTS_REGISTERED = "bots_registered"
    BOTS_DETAIL_NAMED = "bots_detail_named"
    BOTS_DETAIL_NAME = "bots_detail_name"
    BOTS_BOUND = "bots_bound"
    BOTS_UNBOUND = "bots_unbound"

    # ── storage channel ──
    STORAGE_TITLE = "storage_title"
    STORAGE_STATUS = "storage_status"
    STORAGE_STATUS_ENABLED = "storage_status_enabled"
    STORAGE_STATUS_DISABLED = "storage_status_disabled"
    STORAGE_CHANNEL_UNSET = "storage_channel_unset"
    STORAGE_BTN_INDEX = "storage_btn_index"
    STORAGE_BTN_LIST = "storage_btn_list"
    STORAGE_PACKS_TITLE = "storage_packs_title"
    STORAGE_PACKS_EMPTY = "storage_packs_empty"
    STORAGE_PACK_ROW = "storage_pack_row"
    STORAGE_INDEX_PROMPT = "storage_index_prompt"
    STORAGE_INDEX_BAD_COUNT = "storage_index_bad_count"
    STORAGE_INDEX_BAD_FIELDS = "storage_index_bad_fields"
    STORAGE_INDEXING = "storage_indexing"
    STORAGE_INDEX_FAILED = "storage_index_failed"
    STORAGE_INDEX_FAILED_DEFAULT = "storage_index_failed_default"
    STORAGE_INDEXED = "storage_indexed"

    # ── broadcast ──
    BROADCAST_PROMPT = "broadcast_prompt"
    BROADCAST_SENDING = "broadcast_sending"
    BROADCAST_DONE = "broadcast_done"

    # ── channel broadcast (to every distribution channel) ──
    ADMIN_BTN_CH_BROADCAST = "admin_btn_ch_broadcast"
    CH_BROADCAST_PROMPT = "ch_broadcast_prompt"
    CH_BROADCAST_DURATION = "ch_broadcast_duration"
    CH_BROADCAST_SENDING = "ch_broadcast_sending"
    CH_BROADCAST_DONE = "ch_broadcast_done"
    CH_BROADCAST_NO_CHANNELS = "ch_broadcast_no_channels"
    BTN_CH_BC_PERMANENT = "btn_ch_bc_permanent"
    BTN_CH_BC_1H = "btn_ch_bc_1h"
    BTN_CH_BC_6H = "btn_ch_bc_6h"
    BTN_CH_BC_24H = "btn_ch_bc_24h"
    BTN_CH_BC_7D = "btn_ch_bc_7d"

    # ── misc / distribution ──
    BTN_REFRESH = "btn_refresh"
    DIST_NOT_SUBSCRIBED = "dist_not_subscribed"
    DIST_SUBSCRIBED_THANKS = "dist_subscribed_thanks"
    DIST_UNAVAILABLE = "dist_unavailable"

    # ── startup ──
    CONNECTING = "connecting"
    LOADING_STAGE_CONNECTING = "loading_stage_connecting"
    LOADING_STAGE_LOADING = "loading_stage_loading"
    LOADING_STAGE_VERIFYING = "loading_stage_verifying"
    # section labels keyed by config attribute
    SETTINGS_SECTIONS = {
        "features": "settings_sec_features",
        "downloads": "settings_sec_downloads",
        "processing": "settings_sec_processing",
        "rename": "settings_sec_rename",
        "metadata": "settings_sec_metadata",
        "thumbnail": "settings_sec_thumbnail",
        "watermark": "settings_sec_watermark",
        "branding": "settings_sec_branding",
        "distribution": "settings_sec_distribution",
        "queue": "settings_sec_queue",
        "security": "settings_sec_security",
        "storage_channel": "settings_sec_storage_channel",
        "log_channel": "settings_sec_log_channel",
        "main_channel": "settings_sec_main_channel",
        "index_channel": "settings_sec_index_channel",
        "acquisition": "settings_sec_acquisition",
        "access": "settings_sec_access",
        "shortlink": "settings_sec_shortlink",
        "filestore": "settings_sec_filestore",
        "sources": "settings_sec_sources",
        "ui": "settings_sec_ui",
    }

    # ── database statistics ──
    STATS_TITLE = "stats_title"
    STATS_OVERVIEW = "stats_overview"
    STATS_TOTAL = "stats_total"
    STATS_PUBLISHED = "stats_published"
    STATS_NOT_INDEXED = "stats_not_indexed"
    STATS_PENDING_TITLE = "stats_pending_title"
    STATS_NONE_PENDING = "stats_none_pending"
    STATS_UPDATED = "stats_updated"
    STATS_ROW = "stats_row"
    STATS_ENTRY = "stats_entry"

    # ── bot creation / distribution ──
    BOT_WATCH_GUIDE = "bot_watch_guide"
    BOT_WATCH_GUIDE_SEASON = "bot_watch_guide_season"
    BOT_WATCH_GUIDE_EXTRA = "bot_watch_guide_extra"
    BOT_INFO_CARD = "bot_info_card"
    BOT_SEASON_CARD = "bot_season_card"
    BOT_MOVIE_CARD = "bot_movie_card"
    BOT_FOOTER = "bot_footer"
    BOT_QUALITY_BUTTONS = "bot_quality_buttons"
    BOT_DOWNLOAD_NOW_BTN = "bot_download_now_btn"
    BOT_LANG_ENGLISH = "bot_lang_english"
    BOT_LANG_JAPANESE = "bot_lang_japanese"
    BOT_CHOOSE_QUALITY = "bot_choose_quality"
    BOT_QUALITY_480P = "bot_quality_480p"
    BOT_QUALITY_720P = "bot_quality_720p"
    BOT_QUALITY_1080P = "bot_quality_1080p"
    BOT_LOADING_PLACEHOLDER = "bot_loading_placeholder"
    BOT_COMING_SOON = "bot_coming_soon"
    ADMIN_BTN_CREATE_BOT = "admin_btn_create_bot"
    ADMIN_BTN_RECREATE_BOT = "admin_btn_recreate_bot"
    BOT_CREATING = "bot_creating"
    BOT_CREATED = "bot_created"
    BOT_CREATE_FAILED = "bot_create_failed"
    BOT_RECREATED = "bot_recreated"
    BOT_BANNED_STATUS = "bot_banned_status"
    BOT_AUTO_CREATED = "bot_auto_created"
    BOT_HEALTH_FAILED = "bot_health_failed"
    BOT_HEALTH_RECREATED = "bot_health_recreated"

    # ── thumbnail channel ──
    THUMB_QUEUE_TITLE = "thumb_queue_title"
    THUMB_QUEUE_EMPTY = "thumb_queue_empty"
    THUMB_QUEUE_ROW = "thumb_queue_row"
    THUMB_UPDATED = "thumb_updated"
    THUMB_FRANCHISE_TITLE = "thumb_franchise_title"
    THUMB_FRANCHISE_INTRO = "thumb_franchise_intro"
    THUMB_ENTRY_ROW = "thumb_entry_row"
    THUMB_REFRESH_BTN = "thumb_refresh_btn"
    THUMB_PICK_LOGO = "thumb_pick_logo"
    THUMB_PICK_POSTER = "thumb_pick_poster"
    THUMB_PICK_BG = "thumb_pick_bg"
    THUMB_GALLERY_TEASER = "thumb_gallery_teaser"

    # ── thumbnail generation ──
    THUMB_QUEUED_FOR_ASSETS = "thumb_queued_for_assets"
    THUMB_ASSETS_READY = "thumb_assets_ready"
    THUMB_NO_TMDB = "thumb_no_tmdb"
    THUMB_SELECT_NUMBER = "thumb_select_number"
    THUMB_LOGO_SELECTED = "thumb_logo_selected"
    THUMB_POSTER_SELECTED = "thumb_poster_selected"
    THUMB_BG_SELECTED = "thumb_bg_selected"
    THUMB_GENERATING = "thumb_generating"
    THUMB_GENERATED = "thumb_generated"
    THUMB_FAILED = "thumb_failed"
    THUMB_ENTRY_DONE = "thumb_entry_done"

    # ── settings section labels (keyed by config attribute) ──
    SETTINGS_SECTIONS = {
        **SETTINGS_SECTIONS,
        "thumbnail_channel": "settings_sec_thumbnail_channel",
        "bot": "settings_sec_bot",
        "post_format": "settings_sec_post_format",
        "localization": "settings_sec_localization",
    }

    # ── errors / notices ──
    ERR_GENERIC = "error_generic"
    ERR_NOT_FOUND = "error_not_found"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"
    OWNER_ONLY = "owner_only"

    # ── notifications ──
    NOTIF_READY_TITLE = "notif_ready_title"
    NOTIF_READY_BODY = "notif_ready_body"

    # ── batch request ──
    BATCH_TITLE = "batch_title"
    BATCH_INTRO = "batch_intro"
    BATCH_PROMPT = "batch_prompt"
    BATCH_PROCESSING = "batch_processing"
    BATCH_RESOLVED = "batch_resolved"
    BATCH_AMBIGUOUS = "batch_ambiguous"
    BATCH_RESOLVING = "batch_resolving"
    BATCH_CLARIFY_HEADER = "batch_clarify_header"
    BATCH_CONFIRM_TITLE = "batch_confirm_title"
    BATCH_CONFIRM_INTRO = "batch_confirm_intro"
    BATCH_CONFIRM_ROW = "batch_confirm_row"
    BATCH_CONFIRM_SUMMARY = "batch_confirm_summary"
    BATCH_CONFIRM_BTN = "batch_confirm_btn"
    BATCH_CANCEL_BTN = "batch_cancel_btn"
    BATCH_BACK_BTN = "batch_back_btn"
    BATCH_SUBMITTED = "batch_submitted"
    BATCH_SUBMIT_ROW = "batch_submit_row"
    BATCH_SUBMIT_FAILED_ROW = "batch_submit_failed_row"
    BATCH_NOT_FOUND = "batch_not_found"
    BATCH_EMPTY = "batch_empty"
    BATCH_SKIPPED = "batch_skipped"
    BATCH_PRIORITY_OWNER = "batch_priority_owner"
    BATCH_PRIORITY_ADMIN = "batch_priority_admin"
    BATCH_VERSION_PICK = "batch_version_pick"
    BATCH_VERSION_FORMAT = "batch_version_format"
    BATCH_VERSION_CONTINUE = "batch_version_continue"
    BATCH_VERSION_SKIP = "batch_version_skip"
    BTN_BATCH = "btn_batch"
    CMD_CHECKBANS = "cmd_checkbans"
    CMD_CHECKUPDATES = "cmd_checkupdates"
    CMD_RECOVERMAIN = "cmd_recovermain"
    CMD_RECOVERINDEX = "cmd_recoverindex"
    CMD_BATCH = "cmd_batch"
    HELP_CMD_BATCH = "help_cmd_batch"
    HELP_CAP_BATCH = "help_cap_batch"
