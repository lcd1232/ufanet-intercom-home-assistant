"""Local live-edge HLS playlist proxy for the UCAMS live camera.

UCAMS serves live HLS with ~4 s segments. FFmpeg (used by the Home Assistant
``stream`` component) starts a live HLS playlist three segments from the end
(``live_start_index=-3``) and HA offers no way to change that, so the picture
in HA lags the Ufanet app by roughly 8-12 s.

This view sits between HA and UCAMS: the first playlist request of a playback
session gets only the newest segment, so FFmpeg starts at the live edge. Later
reloads get the full upstream window (FFmpeg keeps its own sequence position,
so older entries are ignored) which keeps playback resilient. Media segments
are still fetched directly from UCAMS through absolute URLs.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .api import UfanetApiError, UfanetAuthError
from .const import DOMAIN
from .hls import trim_to_live_edge

_LOGGER = logging.getLogger(__name__)

DATA_LIVE_PLAYLISTS = f"{DOMAIN}_live_playlists"
LIVE_PLAYLIST_URL = "/api/ufanet_intercom/live/{token}.m3u8"
# A request after this much silence starts a new playback session.
SESSION_IDLE_SECONDS = 15.0
LIVE_EDGE_SEGMENTS = 1

PlaylistFetcher = Callable[[bool], Awaitable[tuple[str, str]]]


@dataclass
class _LiveSource:
    """Playlist fetcher plus per-session bookkeeping for one camera."""

    fetch: PlaylistFetcher
    last_request: float = field(default=0.0)


def async_register_live_source(
    hass: HomeAssistant, fetch: PlaylistFetcher
) -> tuple[str, Callable[[], None]]:
    """Register a live source; return its local URL path and an unregister."""
    sources: dict[str, _LiveSource] | None = hass.data.get(DATA_LIVE_PLAYLISTS)
    if sources is None:
        sources = hass.data[DATA_LIVE_PLAYLISTS] = {}
        hass.http.register_view(UfanetLivePlaylistView(sources))
    token = secrets.token_urlsafe(32)
    sources[token] = _LiveSource(fetch)

    def _unregister() -> None:
        sources.pop(token, None)

    return LIVE_PLAYLIST_URL.format(token=token), _unregister


def local_base_url(hass: HomeAssistant) -> str:
    """Return the loopback base URL FFmpeg inside HA can reach."""
    api = hass.config.api
    scheme = "https" if api is not None and api.use_ssl else "http"
    port = api.port if api is not None else 8123
    return f"{scheme}://127.0.0.1:{port}"


class UfanetLivePlaylistView(HomeAssistantView):
    """Serve live-edge playlists to the local FFmpeg stream worker."""

    url = "/api/ufanet_intercom/live/{token}.m3u8"
    name = "api:ufanet_intercom:live"
    # FFmpeg cannot authenticate; the 256-bit path token is the credential.
    requires_auth = False

    def __init__(self, sources: dict[str, _LiveSource]) -> None:
        self._sources = sources

    async def get(self, request: web.Request, token: str) -> web.Response:
        source = self._sources.get(token)
        if source is None:
            return web.Response(status=404)

        now = time.monotonic()
        new_session = now - source.last_request > SESSION_IDLE_SECONDS
        source.last_request = now

        try:
            try:
                playlist, playlist_url = await source.fetch(False)
            except UfanetAuthError:
                playlist, playlist_url = await source.fetch(True)
        except UfanetApiError as err:
            _LOGGER.warning("Unable to fetch Ufanet live playlist: %s", err)
            return web.Response(status=502)

        body = trim_to_live_edge(
            playlist,
            playlist_url,
            LIVE_EDGE_SEGMENTS if new_session else None,
        )
        return web.Response(
            body=body,
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )
