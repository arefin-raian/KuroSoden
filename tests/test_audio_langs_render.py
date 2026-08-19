"""Phase 3 coverage: render sites read REAL languages, fall back to the enum.

The whole point of the multi-audio patch: a genuine English/Japanese/Korean
release must render "Korean" (not the hardcoded Hindi), while already-published
content with no stored languages renders EXACTLY as before. These tests pin both
halves on each render surface:

  * entry_language_tag  (distribution entry card LANGUAGE line)
  * build_pack_caption  (storage-pack header caption, line 2)
  * caption_langs / compact_label (the shared code-formatters)

The main-channel summary, bot name, and thumbnail card all funnel through the
shared resolver (nekofetch.services.audio_langs), which is unit-tested in
test_audio_langs.py; here we prove the language-list SITES that had their own
hardcoded strings.
"""

from __future__ import annotations

from types import SimpleNamespace

from nekofetch.domain.enums import AudioType
from nekofetch.services.audio_langs import caption_langs, language_summary
from nekofetch.services.bot_content import BotContentService
from nekofetch.services.bot_naming import build_pack_caption


def _line2(caption: str) -> str:
    return caption.split("\n")[1]


# ── entry_language_tag: real langs vs enum fallback ───────────────────────────

def test_entry_multi_real_korean():
    """A real Eng/Jpn/Kor multi-audio entry reads Korean, not Hindi."""
    tag = BotContentService.entry_language_tag(
        {AudioType.MULTI}, langs={"english", "japanese", "korean"})
    assert tag == "Multi Audio [Eng, Jpn & Kor]"
    assert "Hin" not in tag


def test_entry_multi_no_langs_is_legacy():
    """No stored langs → the operator's canonical Eng/Jpn/Hin string (unchanged)."""
    assert BotContentService.entry_language_tag({AudioType.MULTI}) == (
        "Multi Audio [Eng, Jpn & Hin]"
    )


def test_entry_dual_real_korean_full_names():
    tag = BotContentService.entry_language_tag(
        {AudioType.DUAL_AUDIO}, langs={"english", "korean"})
    assert tag == "Dual Audio [English & Korean]"


def test_entry_sub_dub_real_langs():
    tag = BotContentService.entry_language_tag(
        {AudioType.SUBBED, AudioType.DUBBED}, langs={"english", "korean"})
    assert tag == "Sub & Dub [English & Korean]"


def test_entry_sub_only_ignores_langs():
    """Sub/Dub-only carry a fixed descriptor, not a language union — unchanged."""
    assert BotContentService.entry_language_tag(
        {AudioType.SUBBED}, langs={"korean"}) == "Sub [Japanese + ESubs]"


def test_entry_multi_wins_over_dual_still_holds_with_langs():
    """MULTI-before-DUAL ordering is preserved when real langs are supplied."""
    tag = BotContentService.entry_language_tag(
        {AudioType.MULTI, AudioType.DUAL_AUDIO},
        langs={"english", "japanese", "korean"})
    assert tag.startswith("Multi Audio")


# ── build_pack_caption: real langs vs enum fallback ───────────────────────────

def _cap(audio, audio_langs=None):
    return _line2(build_pack_caption(
        "Show", season=1, season_part=None, resolution="1080p",
        audio=audio, audio_langs=audio_langs,
    ))


def test_caption_multi_real_korean():
    line = _cap(AudioType.MULTI, ["en", "ja", "ko"])
    assert "ENG + JPN + KOR" in line
    assert "HIN" not in line


def test_caption_multi_fallback_hindi():
    assert "ENG + JPN + HIN" in _cap(AudioType.MULTI)


def test_caption_dual_real_korean():
    assert "ENG + KOR" in _cap(AudioType.DUAL_AUDIO, ["en", "ko"])


def test_caption_dual_fallback():
    assert "ENG + JPN" in _cap(AudioType.DUAL_AUDIO)


def test_caption_sub_ignores_langs():
    """SUB's fixed 'JPN + EngSubs' descriptor is not a language union."""
    line = _cap(AudioType.SUBBED, ["ko"])
    assert "JPN + EngSubs" in line
    assert "KOR" not in line


# ── caption_langs formatter ───────────────────────────────────────────────────

def test_caption_langs_uppercase_plus_joined():
    assert caption_langs(["en", "ja", "ko"]) == "ENG + JPN + KOR"
    assert caption_langs({"english"}) == "ENG"
    assert caption_langs([]) == ""


# ── main-channel summary through the resolver (packs with real langs) ─────────

def test_main_channel_summary_reads_real_langs():
    packs = [SimpleNamespace(audio=AudioType.MULTI, audio_langs=["en", "ja", "ko"])]
    assert language_summary(packs) == "English, Japanese & Korean"


def test_main_channel_summary_fallback_when_null():
    packs = [SimpleNamespace(audio=AudioType.MULTI, audio_langs=None)]
    assert language_summary(packs) == "English, Japanese & Hindi"


# ── _build_season_card end-to-end (real langs on the pack) ────────────────────

def test_season_card_renders_real_korean():
    from tests.test_bot_content_cards import _svc as make_svc, _Pack
    svc = make_svc()
    pack = _Pack()
    pack.audio = AudioType.MULTI
    pack.audio_langs = ["en", "ja", "ko"]
    caption, _ = svc._build_season_card({"title": "Show", "entry_episodes": 12,
                                         "duration_min": 24}, season=1, packs=[pack])
    assert "Kor" in caption
    assert "Hin" not in caption


def test_season_card_null_langs_is_legacy():
    from tests.test_bot_content_cards import _svc as make_svc, _Pack
    svc = make_svc()
    pack = _Pack()
    pack.audio = AudioType.MULTI  # no audio_langs attr → enum fallback
    caption, _ = svc._build_season_card({"title": "Show", "entry_episodes": 12,
                                         "duration_min": 24}, season=1, packs=[pack])
    assert "Multi Audio [Eng, Jpn & Hin]" in caption
