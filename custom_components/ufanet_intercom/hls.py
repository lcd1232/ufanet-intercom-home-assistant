"""Pure helpers for UCAMS live HLS playlists."""

from __future__ import annotations

from urllib.parse import urljoin

# Tags that belong to the following media segment rather than the header.
_SEGMENT_TAGS = (
    "#EXTINF",
    "#EXT-X-PROGRAM-DATE-TIME",
    "#EXT-X-DISCONTINUITY",
    "#EXT-X-BYTERANGE",
)


def trim_to_live_edge(playlist: str, base_url: str, keep: int | None) -> str:
    """Return a media playlist with absolute URIs, optionally trimmed.

    ``keep`` limits the playlist to its newest ``keep`` segments and advances
    ``#EXT-X-MEDIA-SEQUENCE`` accordingly. ``None`` keeps every segment.
    """
    header: list[str] = []
    segments: list[list[str]] = []
    pending: list[str] = []
    media_sequence = 0
    for raw in playlist.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])
            continue
        if (
            not segments
            and not pending
            and line.startswith("#")
            and not line.startswith(_SEGMENT_TAGS)
        ):
            header.append(line)
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        segments.append([*pending, urljoin(base_url, line)])
        pending = []

    dropped = 0
    if keep is not None and len(segments) > keep:
        dropped = len(segments) - keep
        segments = segments[dropped:]

    out = [*header, f"#EXT-X-MEDIA-SEQUENCE:{media_sequence + dropped}"]
    for segment in segments:
        out.extend(segment)
    out.extend(pending)  # e.g. a trailing #EXT-X-ENDLIST
    return "\n".join(out) + "\n"


def resolve_media_playlist_url(master: str, master_url: str) -> str | None:
    """Return the first variant URL of a master playlist, or None for media."""
    lines = [line.strip() for line in master.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
            return urljoin(master_url, lines[index + 1])
    return None
