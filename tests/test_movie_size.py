from __future__ import annotations

import pytest

from nekofetch.sources._transcode import (
    MOVIE_MAX_BYTES,
    movie_needs_size_control,
    target_video_bitrate_kbps,
)


def test_movie_size_boundary_is_strict():
    assert movie_needs_size_control(MOVIE_MAX_BYTES) is False
    assert movie_needs_size_control(MOVIE_MAX_BYTES + 1) is True


def test_target_bitrate_leaves_audio_budget():
    bitrate = target_video_bitrate_kbps(1990 * 1024 * 1024, 3600, 128)
    assert bitrate == int((1990 * 1024 * 1024 * 8) / 3600 / 1000 - 128)


def test_target_bitrate_rejects_unknown_duration():
    with pytest.raises(ValueError):
        target_video_bitrate_kbps(1990 * 1024 * 1024, 0)
