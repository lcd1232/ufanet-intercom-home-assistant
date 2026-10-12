"""Async API client for Ufanet SmartHome and UCAMS.

The endpoints in this module were reconstructed from the Android application and
validated against the live service for authentication, SKUD discovery, door
opening, UCAMS authentication, HLS live video and snapshots.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, TypedDict
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import UCAMS_BASE_URL, UCAMS_TOKEN_TTL, UFANET_BASE_URL
from .hls import resolve_media_playlist_url

CAMERA_FIELDS = [
    "number",
    "token_l",
    "token_r",
    "is_llhls_enabled",
    "permission",
    "address",
    "title",
    "timezone",
    "is_fav",
    "is_public",
    "inactivity_period",
    "server",
    "analytics",
    "tariff",
    "is_sounding",
    "streams_count",
]

CALL_PREVIEW_MAX_BYTES = 32 * 1024 * 1024
KEY_INTERCOM_PAGE_SIZE = 10
KEY_PASSAGE_PAGE_SIZE = 25


class PhysicalKey(TypedDict):
    """Privacy-minimized physical key data used by the integration."""

    key_id: int
    devices: tuple[int, ...]


class KeyPassage(TypedDict):
    """Normalized physical-key passage returned by Ufanet."""

    key_id: int
    key_name: str
    timestamp: int


class KeyPassagePage(TypedDict):
    """Normalized page of physical-key passages."""

    count: int
    current_page: int
    page_count: int
    page_size: int
    results: list[KeyPassage]


class UfanetApiError(Exception):
    """Base exception for Ufanet API errors."""


class UfanetAuthError(UfanetApiError):
    """Authentication failed or credentials are no longer valid."""


class UfanetConnectionError(UfanetApiError):
    """The service could not be reached."""


class UfanetResponseError(UfanetApiError):
    """The service returned an unexpected response."""


class UfanetCallPreviewError(UfanetResponseError):
    """A call preview failed a privacy-safe validation or size check."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_call_preview_url(preview_url: str) -> tuple[str, bool]:
    """Return an HTTPS-only preview URL and whether HTTP was upgraded locally."""
    try:
        parsed = urlsplit(preview_url)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        _port = parsed.port  # Validate a present port.
    except (TypeError, ValueError) as err:
        raise UfanetCallPreviewError(
            "invalid_url",
            "Call preview URL is malformed",
        ) from err

    if username is not None or password is not None:
        raise UfanetCallPreviewError(
            "embedded_credentials",
            "Call preview URL must not contain embedded credentials",
        )
    if not hostname:
        raise UfanetCallPreviewError(
            "missing_host",
            "Call preview URL has no hostname",
        )

    if parsed.scheme == "https":
        return urlunsplit(parsed), False
    if parsed.scheme == "http":
        return urlunsplit(parsed._replace(scheme="https")), True
    raise UfanetCallPreviewError(
        "unsupported_scheme",
        "Call preview URL uses an unsupported scheme",
    )


def _jwt_exp(token: str | None) -> int | None:
    """Read exp from a JWT without validating its signature.

    This is used only for refresh scheduling. Token validity is always enforced
    by the remote server.
    """
    if not token:
        return None
    raw = token.replace("JWT ", "").replace("Bearer ", "")
    try:
        payload = raw.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        value = json.loads(decoded.decode("utf-8")).get("exp")
        return int(value) if value is not None else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _token_valid_for(token: str | None, seconds: int) -> bool:
    exp = _jwt_exp(token)
    return bool(token) and (exp is None or exp > int(time.time()) + seconds)


class UfanetApi:
    """Client for the Ufanet SmartHome and UCAMS cloud APIs."""

    def __init__(
        self,
        session: ClientSession,
        username: str,
        password: str,
        *,
        ufanet_base_url: str = UFANET_BASE_URL,
        ucams_base_url: str = UCAMS_BASE_URL,
    ) -> None:
        self._session = session
        self.username = username.upper()
        self._password = password
        self._ufanet_base_url = ufanet_base_url.rstrip("/")
        self._ucams_base_url = ucams_base_url.rstrip("/")

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._ucams_token: str | None = None
        self._camera_cache: dict[str, dict[str, Any]] = {}

        self._auth_lock = asyncio.Lock()
        self._ucams_lock = asyncio.Lock()
        self._camera_locks: dict[str, asyncio.Lock] = {}

    @property
    def access_token(self) -> str | None:
        """Return the current raw Ufanet access JWT."""
        return self._access_token

    def diagnostic_auth_state(self) -> dict[str, Any]:
        """Return token health metadata without exposing token values."""
        return {
            "ufanet_access_present": bool(self._access_token),
            "ufanet_access_expires_at": _jwt_exp(self._access_token),
            "ufanet_refresh_present": bool(self._refresh_token),
            "ufanet_refresh_expires_at": _jwt_exp(self._refresh_token),
            "ucams_access_present": bool(self._ucams_token),
            "ucams_access_expires_at": _jwt_exp(self._ucams_token),
            "cached_camera_count": len(self._camera_cache),
        }


    async def async_login(self) -> None:
        """Authenticate with contract/login and password."""
        async with self._auth_lock:
            await self._async_login_unlocked()

    async def _async_login_unlocked(self) -> None:
        payload = {"contract": self.username, "password": self._password}
        data = await self._request_json(
            "POST",
            f"{self._ufanet_base_url}/api/v1/auth/auth_by_contract/",
            json_body=payload,
            auth_kind=None,
            auth_failure_statuses={400, 401, 403},
        )
        try:
            token = data["token"]
            self._access_token = token["access"]
            self._refresh_token = token["refresh"]
        except (KeyError, TypeError) as err:
            raise UfanetResponseError("Login response does not contain access/refresh tokens") from err

        self._ucams_token = None
        self._camera_cache.clear()

    async def _async_refresh_ufanet(self) -> None:
        """Refresh Ufanet access/refresh tokens, falling back to login."""
        async with self._auth_lock:
            if _token_valid_for(self._access_token, 120):
                return

            if not self._refresh_token:
                await self._async_login_unlocked()
                return

            try:
                data = await self._request_json(
                    "POST",
                    f"{self._ufanet_base_url}/api/v1/auth/refresh/",
                    json_body={"token": self._refresh_token.replace("JWT ", "")},
                    auth_kind=None,
                )
                self._access_token = data["access"]
                self._refresh_token = data["refresh"]
                self._ucams_token = None
                self._camera_cache.clear()
            except (UfanetAuthError, UfanetResponseError, KeyError, TypeError):
                await self._async_login_unlocked()

    async def _ensure_ufanet_access(self) -> None:
        if _token_valid_for(self._access_token, 120):
            return
        await self._async_refresh_ufanet()

    async def _async_ufanet_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        retry_auth: bool = True,
    ) -> Any:
        await self._ensure_ufanet_access()
        try:
            return await self._request_json(
                method,
                f"{self._ufanet_base_url}{path}",
                params=params,
                json_body=json_body,
                auth_kind="ufanet",
            )
        except UfanetAuthError:
            if not retry_auth:
                raise
            # Force a refresh/login regardless of the decoded exp value.
            self._access_token = None
            await self._async_refresh_ufanet()
            return await self._async_ufanet_json(
                method,
                path,
                params=params,
                json_body=json_body,
                retry_auth=False,
            )

    async def async_get_skuds(self) -> list[dict[str, Any]]:
        """Return all directly assigned and shared SKUD/intercom objects."""
        shared, own = await asyncio.gather(
            self._async_ufanet_json("GET", "/api/v0/skud/shared/"),
            self._async_ufanet_json("GET", "/api/v0/skud/"),
        )

        by_id: dict[int, dict[str, Any]] = {}
        for is_shared, values in ((True, shared), (False, own)):
            if not isinstance(values, list):
                continue
            for raw in values:
                if not isinstance(raw, dict) or "id" not in raw:
                    continue
                item = dict(raw)
                item["_is_shared"] = is_shared
                by_id[int(item["id"])] = item
        return list(by_id.values())

    async def async_get_key_recording_intercom_ids(self) -> set[int]:
        """Return all intercom IDs that advertise physical-key recording."""
        page = 1
        visited_pages: set[int] = set()
        intercom_ids: set[int] = set()

        while page not in visited_pages:
            visited_pages.add(page)
            data = await self._async_ufanet_json(
                "POST",
                "/api/v0/intercoms/",
                json_body={
                    "page": page,
                    "page_size": KEY_INTERCOM_PAGE_SIZE,
                    "filters": {"has_key_recording_support": True},
                },
            )
            if not isinstance(data, dict) or not isinstance(
                result := data.get("result"), dict
            ):
                raise UfanetResponseError("Unexpected key-capable intercom response")
            intercoms = result.get("intercoms")
            if not isinstance(intercoms, list):
                raise UfanetResponseError(
                    "Key-capable intercom response has no intercom list"
                )

            for item in intercoms:
                if not isinstance(item, dict):
                    raise UfanetResponseError(
                        "Key-capable intercom response contains an invalid item"
                    )
                skud_id = item.get("id")
                supported = item.get("has_key_recording_support")
                if (
                    not isinstance(skud_id, int)
                    or isinstance(skud_id, bool)
                    or not isinstance(supported, bool)
                ):
                    raise UfanetResponseError(
                        "Key-capable intercom response contains invalid fields"
                    )
                if supported:
                    intercom_ids.add(skud_id)

            page_info = result.get("page_info")
            next_page = (
                page_info.get("next_page") if isinstance(page_info, dict) else None
            )
            if next_page is None:
                break
            if (
                not isinstance(next_page, int)
                or isinstance(next_page, bool)
                or next_page < 1
            ):
                raise UfanetResponseError(
                    "Key-capable intercom response has invalid pagination"
                )
            page = next_page

        return intercom_ids

    async def async_get_physical_keys(self) -> list[PhysicalKey]:
        """Return physical keys with only the fields needed for counting."""
        data = await self._async_ufanet_json("POST", "/api/v4/key/list/")
        payload = data.get("data") if isinstance(data, dict) else None
        raw_keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(raw_keys, list):
            raise UfanetResponseError("Physical-key response has no key list")

        keys: list[PhysicalKey] = []
        for item in raw_keys:
            if not isinstance(item, dict):
                raise UfanetResponseError("Physical-key response contains invalid item")
            key_id = item.get("id")
            raw_devices = item.get("devices")
            if (
                not isinstance(key_id, int)
                or isinstance(key_id, bool)
                or not isinstance(raw_devices, list)
            ):
                raise UfanetResponseError("Physical-key response contains invalid fields")

            devices: list[int] = []
            for raw_device in raw_devices:
                try:
                    device_id = int(raw_device)
                except (TypeError, ValueError) as err:
                    raise UfanetResponseError(
                        "Physical-key response contains invalid device reference"
                    ) from err
                devices.append(device_id)
            keys.append({"key_id": key_id, "devices": tuple(devices)})
        return keys

    async def async_get_key_passage_history(
        self,
        skud_id: int,
        *,
        page: int = 0,
        page_size: int = KEY_PASSAGE_PAGE_SIZE,
    ) -> KeyPassagePage:
        """Return one normalized page of physical-key passage history."""
        data = await self._async_ufanet_json(
            "POST",
            f"/api/v4/key/skud/{int(skud_id)}/key/pass_history/",
            json_body={"page": int(page), "page_size": int(page_size)},
        )
        if not isinstance(data, dict):
            raise UfanetResponseError("Unexpected key-passage response")

        pagination: dict[str, int] = {}
        for field in ("count", "current_page", "page_count", "page_size"):
            value = data.get(field)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise UfanetResponseError(
                    "Key-passage response contains invalid pagination"
                )
            pagination[field] = value

        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise UfanetResponseError("Key-passage response has no results list")

        passages: list[KeyPassage] = []
        for item in raw_results:
            if not isinstance(item, dict):
                raise UfanetResponseError("Key-passage response contains invalid item")
            key_id = item.get("key")
            key_name = item.get("key_name")
            timestamp = item.get("time_passage")
            if (
                not isinstance(key_id, int)
                or isinstance(key_id, bool)
                or not isinstance(key_name, str)
                or not isinstance(timestamp, int)
                or isinstance(timestamp, bool)
                or timestamp <= 0
                or timestamp > 253_402_300_799
            ):
                raise UfanetResponseError("Key-passage response contains invalid fields")
            passages.append(
                {
                    "key_id": key_id,
                    "key_name": key_name,
                    "timestamp": timestamp,
                }
            )

        return {
            "count": pagination["count"],
            "current_page": pagination["current_page"],
            "page_count": pagination["page_count"],
            "page_size": pagination["page_size"],
            "results": passages,
        }

    async def async_open_door(self, skud_id: int, door: int = 1) -> None:
        """Open a SKUD/intercom relay using the HTTP open method."""
        data = await self._async_ufanet_json(
            "GET",
            f"/api/v0/skud/shared/{int(skud_id)}/open/",
            params={"door": int(door)},
        )
        if not isinstance(data, dict) or data.get("result") is not True:
            raise UfanetResponseError(f"Door open was not confirmed by the server: {data!r}")

    async def async_get_call_history(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
    ) -> list[dict[str, Any]]:
        """Return intercom call history from the Ufanet API."""
        data = await self._async_ufanet_json(
            "GET",
            "/api/v1/skuds/call-history/",
            params={"page": int(page), "page_size": int(page_size)},
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and "results" in data:
            results = data["results"]
            if isinstance(results, list):
                return [item for item in results if isinstance(item, dict)]
        raise UfanetResponseError("Unexpected call-history response")

    async def async_get_call_media(self, uuid: str) -> dict[str, Any]:
        """Return preview/archive URLs for one call UUID."""
        data = await self._async_ufanet_json(
            "POST",
            "/api/v1/cctv/history/",
            json_body={"uuid": uuid},
        )
        if not isinstance(data, dict):
            raise UfanetResponseError("Unexpected CCTV history response")
        return data

    async def async_get_call_preview(self, preview_url: str) -> bytes:
        """Download one tokenized call-preview video without exposing its URL."""
        normalized_url, _upgraded = normalize_call_preview_url(preview_url)
        body = await self._request_bytes(
            normalized_url,
            allow_redirects=False,
        )
        if not body:
            raise UfanetCallPreviewError(
                "empty_preview",
                "Call preview is empty",
            )
        if len(body) > CALL_PREVIEW_MAX_BYTES:
            raise UfanetCallPreviewError(
                "size_limit",
                "Call preview exceeds the in-memory size limit",
            )
        return body

    async def async_register_fcm_device(
        self,
        *,
        token: str,
        device_id: str,
        title: str,
        application: str,
    ) -> None:
        """Associate one user-owned virtual FCM installation with Ufanet."""
        await self._async_ufanet_json(
            "POST",
            "/api/v0/fcm/",
            json_body={
                "token": token,
                "device_id": device_id,
                "title": title,
                "application": application,
                "os": 0,
                "token_type": 0,
            },
        )

    async def async_get_authorized_fcm_devices(self) -> list[dict[str, Any]]:
        """Return the validated minimal authorized-device inventory."""
        data = await self._async_ufanet_json(
            "POST",
            "/api/v4/fcm_device/authorized_devices/",
        )
        payload = data.get("data") if isinstance(data, dict) else None
        devices = payload.get("device_list") if isinstance(payload, dict) else None
        if not isinstance(devices, list):
            raise UfanetResponseError(
                "Authorized-device response has no device list"
            )

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in devices:
            if not isinstance(item, dict):
                raise UfanetResponseError(
                    "Authorized-device response contains an invalid item"
                )
            device_id = item.get("device_id")
            if not isinstance(device_id, str) or not device_id:
                raise UfanetResponseError(
                    "Authorized-device response contains an invalid device ID"
                )
            if device_id in seen:
                raise UfanetResponseError(
                    "Authorized-device response contains a duplicate device ID"
                )
            seen.add(device_id)

            title = item.get("title")
            last_update = item.get("last_update")
            call_access = item.get("is_call_access")
            if title is not None and not isinstance(title, str):
                raise UfanetResponseError(
                    "Authorized-device response contains an invalid title"
                )
            if (
                not isinstance(last_update, str)
                or not last_update.strip()
                or not isinstance(call_access, bool)
            ):
                raise UfanetResponseError(
                    "Authorized-device response contains invalid status fields"
                )

            result.append(
                {
                    "device_id": device_id,
                    "title": title,
                    "last_update": last_update,
                    "is_call_access": call_access,
                    "os": item.get("os"),
                    "os_display": item.get("os_display"),
                }
            )
        return result

    async def async_get_fcm_authorization_status(
        self,
        *,
        device_id: str,
    ) -> dict[str, Any] | None:
        """Return only the authorization fields for one exact FCM device ID."""
        devices = await self.async_get_authorized_fcm_devices()
        for item in devices:
            if item["device_id"] == device_id:
                return {
                    "call_access": item["is_call_access"],
                    "last_update": item["last_update"],
                }
        return None

    async def async_logout_fcm_device(self, *, device_id: str) -> None:
        """Terminate one authorized device/session using the live-confirmed API."""
        await self._async_ufanet_json(
            "POST",
            "/api/v4/fcm_device/logout_device/",
            json_body={"device_id": device_id},
        )

    async def async_unregister_fcm_device(self, *, device_id: str) -> None:
        """Remove one user-owned virtual FCM installation from Ufanet."""
        await self._async_ufanet_json(
            "DELETE",
            "/api/v0/fcm/",
            json_body={"device_id": device_id},
        )

    async def async_get_temporary_guest_links(self) -> list[dict[str, Any]]:
        """Return temporary guest links/keys."""
        data = await self._async_ufanet_json(
            "GET",
            "/api/v1/skuds/skud_share_open/",
        )
        if not isinstance(data, dict):
            raise UfanetResponseError("Unexpected temporary guest-link response")
        result = data.get("result", [])
        if not isinstance(result, list):
            raise UfanetResponseError("Temporary guest-link response has no result list")
        return [item for item in result if isinstance(item, dict)]

    async def async_create_temporary_guest_link(
        self,
        skud_id: int,
        duration_minutes: int,
    ) -> dict[str, Any]:
        """Create a temporary web key for one intercom.

        Live-confirmed request:
          POST /api/v1/skuds/skud_share_open/
          {"time": "<minutes>", "id": <skud_id>}
        """
        data = await self._async_ufanet_json(
            "POST",
            "/api/v1/skuds/skud_share_open/",
            json_body={
                "time": str(int(duration_minutes)),
                "id": int(skud_id),
            },
        )
        if not isinstance(data, dict):
            raise UfanetResponseError("Unexpected temporary guest-link create response")
        link = data.get("link")
        if not isinstance(link, str) or not link:
            raise UfanetResponseError(
                "Temporary guest-link create response has no link"
            )
        return data

    async def async_revoke_temporary_guest_link(
        self,
        skud_id: int,
        token: str,
    ) -> dict[str, Any]:
        """Revoke one temporary web key.

        Android request:
          DELETE /api/v1/skuds/skud_share_open/
          {"token": "<token>", "id": <skud_id>}
        """
        data = await self._async_ufanet_json(
            "DELETE",
            "/api/v1/skuds/skud_share_open/",
            json_body={
                "token": token,
                "id": int(skud_id),
            },
        )
        if not isinstance(data, dict):
            raise UfanetResponseError("Unexpected temporary guest-link revoke response")
        return data

    async def async_get_shared_access_users(
        self,
        skud_id: int,
    ) -> list[dict[str, Any]]:
        """Return users that have shared access to an intercom."""
        data = await self._async_ufanet_json(
            "GET",
            "/api/v4/token/shared/users/",
            params={"skud_id": int(skud_id)},
        )
        if not isinstance(data, dict):
            raise UfanetResponseError("Unexpected shared-access-users response")
        users = data.get("data", [])
        if not isinstance(users, list):
            raise UfanetResponseError("Shared-access-users response has no data list")
        return [item for item in users if isinstance(item, dict)]

    async def async_create_shared_guest_invite(
        self,
        skud_id: int,
    ) -> dict[str, Any]:
        """Create an invitation URL for shared intercom access."""
        data = await self._async_ufanet_json(
            "POST",
            "/api/v4/token/shared/create_token/",
            json_body={"skud_id": int(skud_id)},
        )
        if not isinstance(data, dict):
            raise UfanetResponseError("Unexpected guest-invite response")
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise UfanetResponseError("Guest-invite response has no data object")
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            raise UfanetResponseError("Guest-invite response has no data.url")
        return {
            "url": url,
            "access_id": payload.get("access_id"),
            "status": data.get("status"),
            "detail": data.get("detail"),
        }

    async def async_revoke_shared_access(
        self,
        contract_object_id: int,
    ) -> dict[str, Any]:
        """Revoke one accepted shared-access grant.

        Live-confirmed Android request:
          POST /api/v4/token/delete/
          {"contract_object_id": <access_id>}
        """
        data = await self._async_ufanet_json(
            "POST",
            "/api/v4/token/delete/",
            json_body={"contract_object_id": int(contract_object_id)},
        )
        if not isinstance(data, dict):
            raise UfanetResponseError("Unexpected shared-access revoke response")
        if data.get("status") not in (None, "ok"):
            raise UfanetResponseError(
                f"Shared-access revoke was not confirmed: {data!r}"
            )
        return data

    async def _ensure_ucams_auth(self) -> None:
        await self._ensure_ufanet_access()
        if _token_valid_for(self._ucams_token, 120):
            return

        async with self._ucams_lock:
            if _token_valid_for(self._ucams_token, 120):
                return
            if not self._access_token:
                raise UfanetAuthError("Ufanet access token is unavailable")

            data = await self._request_json(
                "POST",
                f"{self._ucams_base_url}/api/v0/auth/",
                params={"ttl": UCAMS_TOKEN_TTL},
                auth_kind="ufanet",
            )
            try:
                self._ucams_token = data["token"]
            except (KeyError, TypeError) as err:
                raise UfanetResponseError("UCAMS authentication response has no token") from err
            self._camera_cache.clear()

    async def _async_ucams_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        retry_auth: bool = True,
    ) -> Any:
        await self._ensure_ucams_auth()
        try:
            return await self._request_json(
                method,
                f"{self._ucams_base_url}{path}",
                params=params,
                json_body=json_body,
                auth_kind="ucams",
            )
        except UfanetAuthError:
            if not retry_auth:
                raise
            self._ucams_token = None
            self._camera_cache.clear()
            await self._ensure_ucams_auth()
            return await self._async_ucams_json(
                method,
                path,
                params=params,
                json_body=json_body,
                retry_auth=False,
            )

    async def async_get_camera(self, camera_number: str, *, force: bool = False) -> dict[str, Any]:
        """Return UCAMS camera details and fresh live/archive tokens."""
        cached = self._camera_cache.get(camera_number)
        if (
            not force
            and cached
            and _token_valid_for(cached.get("token_l"), 300)
            and _token_valid_for(cached.get("token_r"), 300)
        ):
            return cached

        lock = self._camera_locks.setdefault(camera_number, asyncio.Lock())
        async with lock:
            cached = self._camera_cache.get(camera_number)
            if (
                not force
                and cached
                and _token_valid_for(cached.get("token_l"), 300)
                and _token_valid_for(cached.get("token_r"), 300)
            ):
                return cached

            payload = {
                "fields": CAMERA_FIELDS,
                "token_l_ttl": UCAMS_TOKEN_TTL,
                "token_r_ttl": UCAMS_TOKEN_TTL,
                "numbers": [camera_number],
            }
            data = await self._async_ucams_json(
                "POST",
                "/api/v0/cameras/this/",
                json_body=payload,
            )
            try:
                results = data["results"]
                if not results:
                    raise UfanetResponseError(f"UCAMS camera {camera_number} was not returned")
                camera = dict(results[0])
                server = camera["server"]
                if not camera.get("token_l") or not server.get("domain") or not server.get("screenshot_domain"):
                    raise UfanetResponseError("UCAMS camera response misses streaming fields")
            except (KeyError, TypeError) as err:
                raise UfanetResponseError("Unexpected UCAMS camera response") from err

            self._camera_cache[camera_number] = camera
            return camera

    async def async_get_hls_url(self, camera_number: str, stream_number: int = 1) -> str:
        """Return a fresh HLS URL for a camera."""
        camera = await self.async_get_camera(camera_number)
        domain = camera["server"]["domain"]
        token = camera["token_l"]
        number = quote(camera_number, safe="")
        query = urlencode({"token": token, "tracks": f"v{int(stream_number)}a1"})
        return f"https://{domain}/{number}/index.m3u8?{query}"

    async def async_get_live_media_playlist(
        self,
        camera_number: str,
        stream_number: int = 1,
        *,
        force: bool = False,
    ) -> tuple[str, str]:
        """Return the live HLS media playlist text and its URL.

        Follows the master playlist to its first variant. ``force`` refreshes
        the cached camera token first.
        """
        if force:
            await self.async_get_camera(camera_number, force=True)
        url = await self.async_get_hls_url(camera_number, stream_number)
        playlist = (await self._request_bytes(url)).decode()
        if (media_url := resolve_media_playlist_url(playlist, url)) is not None:
            url = media_url
            playlist = (await self._request_bytes(url)).decode()
        return playlist, url

    async def async_get_snapshot(self, camera_number: str, *, small: bool = False) -> bytes:
        """Return the current JPEG snapshot for a camera."""
        camera = await self.async_get_camera(camera_number)
        domain = camera["server"]["screenshot_domain"]
        token = camera["token_l"]
        suffix = "~600" if small else ""
        number = quote(camera_number, safe="")
        url = f"https://{domain}/api/v0/screenshots/{number}{suffix}.jpg"

        try:
            return await self._request_bytes(url, params={"token": token})
        except UfanetAuthError:
            # Live token can be invalidated independently; force one refresh.
            camera = await self.async_get_camera(camera_number, force=True)
            domain = camera["server"]["screenshot_domain"]
            token = camera["token_l"]
            url = f"https://{domain}/api/v0/screenshots/{number}{suffix}.jpg"
            return await self._request_bytes(url, params={"token": token})

    async def async_get_archive_ranges(
        self,
        camera_number: str,
    ) -> list[dict[str, int]]:
        """Return recorded archive ranges for a camera.

        UCAMS/UMS exposes a recording_status.json endpoint on the camera media
        server. The Android application requests ``request=ranges`` with the
        archive token (token_r). Returned timestamps are Unix seconds.
        """
        for attempt in range(2):
            camera = await self.async_get_camera(camera_number, force=attempt > 0)
            token = camera.get("token_r")
            server = camera.get("server") or {}
            domain = server.get("domain")
            if not token or not domain:
                raise UfanetResponseError("UCAMS camera has no archive token/media domain")

            number = quote(camera_number, safe="")
            url = f"https://{domain}/{number}/recording_status.json"
            try:
                data = await self._request_json(
                    "GET",
                    url,
                    params={"from": 0, "request": "ranges", "token": token},
                    auth_kind=None,
                )
            except UfanetAuthError:
                if attempt == 0:
                    continue
                raise

            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                raise UfanetResponseError("Unexpected UCAMS archive ranges response")

            raw_ranges = data.get("ranges", [])
            if not isinstance(raw_ranges, list):
                raise UfanetResponseError("UCAMS archive response has no ranges list")

            ranges: list[dict[str, int]] = []
            for raw in raw_ranges:
                if not isinstance(raw, dict):
                    continue
                try:
                    start = int(raw["from"])
                    duration = int(raw["duration"])
                except (KeyError, TypeError, ValueError):
                    continue
                if duration <= 3:
                    continue
                ranges.append({"from": start, "duration": duration})
            return ranges

        raise UfanetAuthError("Unable to refresh UCAMS archive token")

    async def async_get_archive_url(
        self,
        camera_number: str,
        start: int,
        duration: int,
    ) -> dict[str, Any]:
        """Build a validated archive HLS master URL.

        The requested start must fall inside an actually recorded range. If the
        requested duration crosses a recording gap/end, it is clipped to the
        current contiguous range.
        """
        start = int(start)
        duration = int(duration)
        if duration <= 0:
            raise UfanetResponseError("Archive duration must be greater than zero")

        ranges = await self.async_get_archive_ranges(camera_number)
        selected: dict[str, int] | None = None
        for item in ranges:
            range_start = int(item["from"])
            range_end = range_start + int(item["duration"])
            if range_start <= start < range_end:
                selected = item
                break

        if selected is None:
            raise UfanetResponseError("Requested time is outside recorded archive ranges")

        range_end = int(selected["from"]) + int(selected["duration"])
        effective_duration = min(duration, range_end - start)
        if effective_duration <= 0:
            raise UfanetResponseError("No recorded video is available at requested time")

        camera = await self.async_get_camera(camera_number)
        token = camera.get("token_r")
        server = camera.get("server") or {}
        domain = server.get("domain")
        vendor = str(server.get("vendor_name") or "")
        if not token or not domain:
            raise UfanetResponseError("UCAMS camera has no archive token/media domain")

        number = quote(camera_number, safe="")
        if vendor.upper() == "UMS":
            path = f"/{number}/archive-{start}-{effective_duration}.m3u8"
        else:
            path = (
                f"/{number}/tracks-v1a1/"
                f"archive-{start}-{effective_duration}.m3u8"
            )
        url = f"https://{domain}{path}?{urlencode({'token': token})}"

        return {
            "url": url,
            "start": start,
            "duration": effective_duration,
            "requested_duration": duration,
            "range_from": int(selected["from"]),
            "range_duration": int(selected["duration"]),
            "vendor": vendor,
            "token_expires_at": _jwt_exp(token),
        }


    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        auth_kind: str | None,
        auth_failure_statuses: set[int] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if json_body is not None or method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers["Content-Type"] = "application/json"
        if auth_kind == "ufanet":
            if not self._access_token:
                raise UfanetAuthError("Ufanet access token is unavailable")
            headers["Authorization"] = f"JWT {self._access_token.replace('JWT ', '')}"
        elif auth_kind == "ucams":
            if not self._ucams_token:
                raise UfanetAuthError("UCAMS token is unavailable")
            headers["Authorization"] = f"Bearer {self._ucams_token.replace('Bearer ', '')}"

        response: ClientResponse | None = None
        try:
            async with asyncio.timeout(30):
                response = await self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
                body = await response.text()
        except (TimeoutError, ClientError) as err:
            raise UfanetConnectionError(str(err)) from err

        if response.status in (auth_failure_statuses or {401, 403}):
            raise UfanetAuthError(_response_message(response.status, body))
        if response.status >= 400:
            if response.status in (400, 404, 422):
                raise UfanetResponseError(_response_message(response.status, body))
            raise UfanetConnectionError(_response_message(response.status, body))

        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as err:
            raise UfanetResponseError(f"Invalid JSON response from {url}") from err

    async def _request_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        allow_redirects: bool = True,
    ) -> bytes:
        response: ClientResponse | None = None
        try:
            async with asyncio.timeout(30):
                response = await self._session.get(
                    url,
                    params=params,
                    allow_redirects=allow_redirects,
                )
                body = await response.read()
        except (TimeoutError, ClientError) as err:
            raise UfanetConnectionError(str(err)) from err

        if 300 <= response.status < 400:
            raise UfanetConnectionError("HTTP redirect blocked while fetching media")
        if response.status in (401, 403):
            raise UfanetAuthError(f"HTTP {response.status}")
        if response.status >= 400:
            raise UfanetConnectionError(f"HTTP {response.status} while fetching snapshot")
        return body


def _response_message(status: int, body: str) -> str:
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            detail = parsed.get("detail") or parsed.get("error") or parsed.get("message")
            if detail:
                return f"HTTP {status}: {detail}"
    except (json.JSONDecodeError, TypeError):
        pass
    compact = body.strip().replace("\n", " ")[:300]
    return f"HTTP {status}: {compact}" if compact else f"HTTP {status}"
