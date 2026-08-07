"""Main-channel metadata uses franchise-wide facts, not one season."""

from __future__ import annotations

from types import SimpleNamespace

from nekofetch.domain.enums import AudioType
from nekofetch.services.main_channel_service import _language_summary


def _pack(audio: AudioType):
    return SimpleNamespace(audio=audio)


def test_language_summary_unions_audio_across_all_entries():
    """One English-audio entry must not be downgraded by sub-only entries."""
    packs = [
        _pack(AudioType.SUBBED),
        _pack(AudioType.DUAL_AUDIO),
        _pack(AudioType.SUBBED),
    ]

    assert _language_summary(packs) == "English & Japanese"


def test_language_summary_keeps_sub_only_franchise_japanese():
    assert _language_summary([_pack(AudioType.SUBBED), _pack(AudioType.SUBBED)]) == "Japanese"
