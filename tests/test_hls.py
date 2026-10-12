"""Tests for the live HLS playlist helpers."""

from __future__ import annotations

from custom_components.ufanet_intercom.hls import (
    parse_media_playlist,
    resolve_media_playlist_url,
    trim_to_live_edge,
)

MASTER_URL = "https://cdn.example/CAM/index.m3u8?token=t&tracks=v1a1"
MEDIA_URL = "https://cdn.example/CAM/0/index.m3u8?token=t"

MASTER = """#EXTM3U
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-STREAM-INF:BANDWIDTH=7076000,RESOLUTION=1920x1080,CODECS="avc1.420029,mp4a.40.2"
0/index.m3u8?token=t
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-ALLOW-CACHE:NO
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:100

#EXTINF:4.003,
CAM-1-100.ts?token=t
#EXTINF:4.003,
CAM-1-101.ts?token=t
#EXTINF:4.002,
CAM-1-102.ts?token=t
"""


def test_resolve_media_playlist_url_follows_first_variant() -> None:
    assert resolve_media_playlist_url(MASTER, MASTER_URL) == MEDIA_URL


def test_resolve_media_playlist_url_returns_none_for_media() -> None:
    assert resolve_media_playlist_url(MEDIA, MEDIA_URL) is None


def test_trim_keeps_newest_segment_and_advances_sequence() -> None:
    out = trim_to_live_edge(MEDIA, MEDIA_URL, 1).splitlines()

    assert out[:4] == [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-ALLOW-CACHE:NO",
        "#EXT-X-TARGETDURATION:4",
    ]
    assert "#EXT-X-MEDIA-SEQUENCE:102" in out
    assert out[-2:] == [
        "#EXTINF:4.002,",
        "https://cdn.example/CAM/0/CAM-1-102.ts?token=t",
    ]
    assert not any("CAM-1-100" in line or "CAM-1-101" in line for line in out)


def test_trim_none_keeps_all_segments_with_absolute_uris() -> None:
    out = trim_to_live_edge(MEDIA, MEDIA_URL, None).splitlines()

    assert "#EXT-X-MEDIA-SEQUENCE:100" in out
    assert [line for line in out if not line.startswith("#")] == [
        f"https://cdn.example/CAM/0/CAM-1-{n}.ts?token=t" for n in (100, 101, 102)
    ]


def test_trim_keeps_segment_tags_with_their_segment() -> None:
    media = MEDIA.replace(
        "#EXTINF:4.002,",
        "#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-10-12T10:00:00Z\n#EXTINF:4.002,",
    )

    out = trim_to_live_edge(media, MEDIA_URL, 1).splitlines()

    assert out[-4:-1] == [
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-PROGRAM-DATE-TIME:2026-10-12T10:00:00Z",
        "#EXTINF:4.002,",
    ]


def test_trim_keeps_trailing_endlist() -> None:
    out = trim_to_live_edge(MEDIA + "#EXT-X-ENDLIST\n", MEDIA_URL, 1).splitlines()

    assert out[-1] == "#EXT-X-ENDLIST"


def test_parse_numbers_segments_from_media_sequence() -> None:
    playlist = parse_media_playlist(MEDIA, MEDIA_URL)

    assert [s.sequence for s in playlist.segments] == [100, 101, 102]
    assert playlist.newest_sequence == 102
    assert playlist.segments[0].tags == ["#EXTINF:4.003,"]


def test_render_maps_segment_uris() -> None:
    playlist = parse_media_playlist(MEDIA, MEDIA_URL)

    out = playlist.render(uri=lambda s: f"{s.sequence}.ts").splitlines()

    assert "#EXT-X-MEDIA-SEQUENCE:100" in out
    assert [line for line in out if not line.startswith("#")] == [
        "100.ts",
        "101.ts",
        "102.ts",
    ]


def test_empty_playlist_has_no_newest_sequence() -> None:
    playlist = parse_media_playlist("#EXTM3U\n#EXT-X-TARGETDURATION:4\n", MEDIA_URL)

    assert playlist.newest_sequence is None
    assert "#EXT-X-MEDIA-SEQUENCE:0" in playlist.render(1)
