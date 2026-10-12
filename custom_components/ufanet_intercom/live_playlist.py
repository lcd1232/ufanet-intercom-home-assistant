"""Low-latency live HLS for the UCAMS live camera.

UCAMS serves live HLS with ~4 s segments that appear in whole-segment bursts.
Two consumers are served from here:

* The Home Assistant ``stream`` worker (FFmpeg) gets a local playlist URL.
  The first request of a session returns only the newest segment so FFmpeg
  starts at the live edge, and later reloads wait until UCAMS publishes the
  next segment instead of polling on FFmpeg's own ~4.5 s cadence.

* The ``ufanet-live-camera-card`` plays UCAMS HLS directly with hls.js and a
  small buffer, like the Ufanet app. The HA camera pipeline keeps a buffer of
  three of its own segments (~6 s) in the stock frontend player and disables
  LL-HLS for HLS inputs, so it cannot match the app. The card asks for a
  short-lived session over the websocket API and then loads the playlist and
  segments from this module, which proxies them from UCAMS: the browser never
  sees the UCAMS token and CORS does not apply.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientError, ClientTimeout, web
import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import UfanetApiError, UfanetAuthError
from .const import DOMAIN
from .hls import MediaPlaylist, parse_media_playlist

_LOGGER = logging.getLogger(__name__)

DATA_LIVE = f"{DOMAIN}_live"
LIVE_PLAYLIST_URL = "/api/ufanet_intercom/live/{token}.m3u8"
PLAY_PLAYLIST_URL = "/api/ufanet_intercom/play/{token}/index.m3u8"

# An FFmpeg request after this much silence starts a new playback session.
SESSION_IDLE_SECONDS = 15.0
# Card sessions are dropped after this much inactivity.
PLAY_SESSION_IDLE_SECONDS = 60.0
LIVE_EDGE_SEGMENTS = 1
# Long-poll: how often UCAMS is re-checked and for how long a reload may wait.
POLL_INTERVAL_SECONDS = 0.3
MAX_WAIT_SECONDS = 6.0
SEGMENT_TIMEOUT = ClientTimeout(total=30)

PlaylistFetcher = Callable[[bool], Awaitable[tuple[str, str]]]
UrlFetcher = Callable[[str], Awaitable[str]]


@dataclass
class _LiveSource:
    """Upstream access for one camera entity."""

    resolve: PlaylistFetcher
    fetch_url: UrlFetcher
    media_url: str | None = None

    async def async_fetch(self) -> MediaPlaylist:
        """Fetch the current media playlist, refreshing the token on 401/403."""
        if self.media_url is not None:
            try:
                text = await self.fetch_url(self.media_url)
                return parse_media_playlist(text, self.media_url)
            except UfanetAuthError:
                self.media_url = None
        try:
            text, url = await self.resolve(False)
        except UfanetAuthError:
            text, url = await self.resolve(True)
        self.media_url = url
        return parse_media_playlist(text, url)

    async def async_fetch_newer(self, after: int | None) -> MediaPlaylist:
        """Return the playlist once it has a segment newer than ``after``."""
        playlist = await self.async_fetch()
        if after is None:
            return playlist
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        while (
            playlist.newest_sequence is not None
            and playlist.newest_sequence <= after
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            playlist = await self.async_fetch()
        return playlist


@dataclass
class _FfmpegSession:
    """State of the HA stream worker's view of one source."""

    source: _LiveSource
    last_request: float = 0.0
    last_sequence: int | None = None


@dataclass
class _PlaySession:
    """A browser (card) playback session."""

    source: _LiveSource
    last_access: float = field(default_factory=time.monotonic)
    last_sequence: int | None = None
    segments: dict[int, str] = field(default_factory=dict)


@dataclass
class _LiveData:
    entity_sources: dict[str, _LiveSource] = field(default_factory=dict)
    ffmpeg: dict[str, _FfmpegSession] = field(default_factory=dict)
    play: dict[str, _PlaySession] = field(default_factory=dict)

    def expire_play_sessions(self) -> None:
        cutoff = time.monotonic() - PLAY_SESSION_IDLE_SECONDS
        for token in [t for t, s in self.play.items() if s.last_access < cutoff]:
            del self.play[token]


def _live_data(hass: HomeAssistant) -> _LiveData:
    if (data := hass.data.get(DATA_LIVE)) is None:
        data = hass.data[DATA_LIVE] = _LiveData()
        hass.http.register_view(UfanetLivePlaylistView(data))
        hass.http.register_view(UfanetPlayPlaylistView(data))
        hass.http.register_view(UfanetPlaySegmentView(hass, data))
        websocket_api.async_register_command(hass, websocket_live_stream)
    return data


@callback
def async_register_live_source(
    hass: HomeAssistant,
    entity_id: str,
    resolve: PlaylistFetcher,
    fetch_url: UrlFetcher,
) -> tuple[str, Callable[[], None]]:
    """Register a camera's live source.

    Returns the local playlist path for the HA stream worker and an
    unregister callback.
    """
    data = _live_data(hass)
    source = _LiveSource(resolve, fetch_url)
    data.entity_sources[entity_id] = source
    token = secrets.token_urlsafe(32)
    data.ffmpeg[token] = _FfmpegSession(source)

    @callback
    def _unregister() -> None:
        data.ffmpeg.pop(token, None)
        if data.entity_sources.get(entity_id) is source:
            del data.entity_sources[entity_id]
        for play_token in [t for t, s in data.play.items() if s.source is source]:
            del data.play[play_token]

    return LIVE_PLAYLIST_URL.format(token=token), _unregister


def local_base_url(hass: HomeAssistant) -> str:
    """Return the loopback base URL FFmpeg inside HA can reach."""
    api = hass.config.api
    scheme = "https" if api is not None and api.use_ssl else "http"
    port = api.port if api is not None else 8123
    return f"{scheme}://127.0.0.1:{port}"


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ufanet_intercom/live_stream",
        vol.Required("entity_id"): cv.entity_id,
    }
)
@callback
def websocket_live_stream(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Start a low-latency playback session for the live card."""
    entity_id = msg["entity_id"]
    if not connection.user.permissions.check_entity(entity_id, POLICY_READ):
        raise Unauthorized(entity_id=entity_id)
    data = _live_data(hass)
    if (source := data.entity_sources.get(entity_id)) is None:
        connection.send_error(
            msg["id"], "not_found", f"{entity_id} is not a Ufanet live camera"
        )
        return
    data.expire_play_sessions()
    token = secrets.token_urlsafe(32)
    data.play[token] = _PlaySession(source)
    connection.send_result(msg["id"], {"url": PLAY_PLAYLIST_URL.format(token=token)})


def _playlist_response(body: str) -> web.Response:
    return web.Response(
        body=body,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


def _log_live_edge(kind: str, playlist: MediaPlaylist, new_session: bool) -> None:
    """Debug-log how far behind capture the newest segment is."""
    if not _LOGGER.isEnabledFor(logging.DEBUG) or not playlist.segments:
        return
    newest = playlist.segments[-1]
    # UCAMS segment names embed the capture start in epoch ms: NAME-<ms>-<seq>.ts
    try:
        start_ms = int(newest.url.split("?", 1)[0].rsplit("-", 2)[-2])
        duration = float(newest.tags[-1].split(":", 1)[1].rstrip(","))
    except (IndexError, ValueError):
        return
    _LOGGER.debug(
        "%s playlist: new_session=%s newest_seq=%s newest_end_lag=%.2fs",
        kind,
        new_session,
        newest.sequence,
        time.time() - start_ms / 1000 - duration,
    )


class UfanetLivePlaylistView(HomeAssistantView):
    """Serve live-edge playlists to the local FFmpeg stream worker."""

    url = "/api/ufanet_intercom/live/{token}.m3u8"
    name = "api:ufanet_intercom:live"
    # FFmpeg cannot authenticate; the 256-bit path token is the credential.
    requires_auth = False

    def __init__(self, data: _LiveData) -> None:
        self._data = data

    async def get(self, request: web.Request, token: str) -> web.Response:
        if (session := self._data.ffmpeg.get(token)) is None:
            return web.Response(status=404)

        now = time.monotonic()
        new_session = now - session.last_request > SESSION_IDLE_SECONDS
        session.last_request = now
        try:
            playlist = await session.source.async_fetch_newer(
                None if new_session else session.last_sequence
            )
        except UfanetApiError as err:
            _LOGGER.warning("Unable to fetch Ufanet live playlist: %s", err)
            return web.Response(status=502)
        session.last_request = time.monotonic()
        session.last_sequence = playlist.newest_sequence
        _log_live_edge("FFmpeg", playlist, new_session)
        return _playlist_response(
            playlist.render(LIVE_EDGE_SEGMENTS if new_session else None)
        )


class UfanetPlayPlaylistView(HomeAssistantView):
    """Serve the card's playlist with segment URIs pointing back at HA."""

    url = "/api/ufanet_intercom/play/{token}/index.m3u8"
    name = "api:ufanet_intercom:play:playlist"
    # hls.js cannot add HA auth headers; the short-lived token is issued over
    # the authenticated websocket API and is the credential.
    requires_auth = False

    def __init__(self, data: _LiveData) -> None:
        self._data = data

    async def get(self, request: web.Request, token: str) -> web.Response:
        if (session := self._data.play.get(token)) is None:
            return web.Response(status=404)
        session.last_access = time.monotonic()
        new_session = session.last_sequence is None
        try:
            playlist = await session.source.async_fetch_newer(session.last_sequence)
        except UfanetApiError as err:
            _LOGGER.warning("Unable to fetch Ufanet live playlist: %s", err)
            return web.Response(status=502)
        session.last_access = time.monotonic()
        session.last_sequence = playlist.newest_sequence
        session.segments = {s.sequence: s.url for s in playlist.segments}
        _log_live_edge("Card", playlist, new_session)
        return _playlist_response(playlist.render(uri=lambda s: f"{s.sequence}.ts"))


class UfanetPlaySegmentView(HomeAssistantView):
    """Proxy one media segment of a card session from UCAMS."""

    url = "/api/ufanet_intercom/play/{token}/{sequence:\\d+}.ts"
    name = "api:ufanet_intercom:play:segment"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, data: _LiveData) -> None:
        self._hass = hass
        self._data = data

    async def get(
        self, request: web.Request, token: str, sequence: str
    ) -> web.StreamResponse:
        if (session := self._data.play.get(token)) is None:
            return web.Response(status=404)
        session.last_access = time.monotonic()
        if (url := session.segments.get(int(sequence))) is None:
            return web.Response(status=404)

        response: web.StreamResponse | None = None
        try:
            async with async_get_clientsession(self._hass).get(
                url, timeout=SEGMENT_TIMEOUT
            ) as upstream:
                if upstream.status != 200:
                    return web.Response(status=502)
                response = web.StreamResponse(
                    headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"}
                )
                if upstream.content_length is not None:
                    response.content_length = upstream.content_length
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                await response.write_eof()
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("Ufanet segment proxy failed: %s", err)
            if response is None:
                return web.Response(status=502)
        return response
