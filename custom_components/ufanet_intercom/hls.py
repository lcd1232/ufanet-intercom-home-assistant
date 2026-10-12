"""Pure helpers for UCAMS live HLS playlists."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

# Tags that belong to the following media segment rather than the header.
_SEGMENT_TAGS = (
    "#EXTINF",
    "#EXT-X-PROGRAM-DATE-TIME",
    "#EXT-X-DISCONTINUITY",
    "#EXT-X-BYTERANGE",
)


@dataclass
class MediaSegment:
    """One media segment: its sequence number, tags and absolute URL."""

    sequence: int
    tags: list[str]
    url: str


@dataclass
class MediaPlaylist:
    """A parsed HLS media playlist."""

    header: list[str] = field(default_factory=list)
    media_sequence: int = 0
    segments: list[MediaSegment] = field(default_factory=list)
    trailer: list[str] = field(default_factory=list)

    @property
    def newest_sequence(self) -> int | None:
        return self.segments[-1].sequence if self.segments else None

    def render(
        self,
        keep: int | None = None,
        uri: Callable[[MediaSegment], str] | None = None,
    ) -> str:
        """Render the playlist, keeping the newest ``keep`` segments.

        ``uri`` maps a segment to the URI written out (default: its URL).
        """
        segments = self.segments
        if keep is not None and len(segments) > keep:
            segments = segments[-keep:]
        first = segments[0].sequence if segments else self.media_sequence
        out = [*self.header, f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for segment in segments:
            out.extend(segment.tags)
            out.append(uri(segment) if uri else segment.url)
        out.extend(self.trailer)  # e.g. a trailing #EXT-X-ENDLIST
        return "\n".join(out) + "\n"


def parse_media_playlist(playlist: str, base_url: str) -> MediaPlaylist:
    """Parse a media playlist, resolving segment URIs against ``base_url``."""
    parsed = MediaPlaylist()
    pending: list[str] = []
    for raw in playlist.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            parsed.media_sequence = int(line.split(":", 1)[1])
            continue
        if (
            not parsed.segments
            and not pending
            and line.startswith("#")
            and not line.startswith(_SEGMENT_TAGS)
        ):
            parsed.header.append(line)
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        parsed.segments.append(
            MediaSegment(
                parsed.media_sequence + len(parsed.segments),
                pending,
                urljoin(base_url, line),
            )
        )
        pending = []
    parsed.trailer = pending
    return parsed


def trim_to_live_edge(playlist: str, base_url: str, keep: int | None) -> str:
    """Return a media playlist with absolute URIs, optionally trimmed.

    ``keep`` limits the playlist to its newest ``keep`` segments and advances
    ``#EXT-X-MEDIA-SEQUENCE`` accordingly. ``None`` keeps every segment.
    """
    return parse_media_playlist(playlist, base_url).render(keep)


def resolve_media_playlist_url(master: str, master_url: str) -> str | None:
    """Return the first variant URL of a master playlist, or None for media."""
    lines = [line.strip() for line in master.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
            return urljoin(master_url, lines[index + 1])
    return None
