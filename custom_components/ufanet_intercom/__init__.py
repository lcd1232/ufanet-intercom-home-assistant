"""Ufanet Intercom integration."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .analytics import UfanetMotionAnalyticsCoordinator
from .api import UfanetAuthError, UfanetConnectionError, UfanetResponseError
from .archive import UfanetArchiveController
from .auto_export import UfanetCallAutoSaveManager
from .const import (
    CALL_UPDATE_MODE_FCM,
    CONF_ARCHIVE_DEFAULT_DURATION,
    CONF_ARCHIVE_DEFAULT_STEP,
    CONF_CALL_SCAN_INTERVAL,
    CONF_CALL_UPDATE_MODE,
    CONF_FCM_CONFIG_PATH,
    CONF_MEDIA_REFRESH_INTERVAL,
    CONF_PASSWORD,
    CONF_SKUD_SCAN_INTERVAL,
    CONF_USERNAME,
    DOMAIN,
    EVENT_INTERCOM_CALL,
    EVENT_KEY_PASSAGE,
    EVENT_MOTION_ANALYTICS,
    FCM_FALLBACK_SCAN_INTERVAL_SECONDS,
)
from .coordinator import UfanetCallCoordinator, UfanetCoordinator
from .entity import device_name
from .fcm import (
    async_remove_stored_fcm_registration,
    async_retry_pending_fcm_unregister,
)
from .fcm_key import UfanetFcmManager
from .firebase_config import UfanetFirebaseConfigError, async_load_firebase_config
from .guest_store import UfanetGuestInviteStore
from .image_status import UfanetLastCallImageStatusManager
from .key_coordinator import UfanetKeyPassageCoordinator
from .key_inventory import UfanetApi
from .key_management import async_setup_key_services
from .options import effective_options
from .private_services import async_setup_private_camera_services
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_ARCHIVE_CARD_PATH = _FRONTEND_DIR / "ufanet-archive-card.js"
_ARCHIVE_CARD_URL = "/ufanet_intercom/ufanet-archive-card.js"
_ARCHIVE_CARD_MODULE_URL = f"{_ARCHIVE_CARD_URL}?v=0.32.0"
_PHYSICAL_KEYS_CARD_PATH = _FRONTEND_DIR / "ufanet-physical-keys-card.js"
_PHYSICAL_KEYS_CARD_URL = "/ufanet_intercom/ufanet-physical-keys-card.js"
_PHYSICAL_KEYS_CARD_MODULE_URL = f"{_PHYSICAL_KEYS_CARD_URL}?v=0.32.0"
_KEY_HISTORY_CARD_PATH = _FRONTEND_DIR / "ufanet-key-history-card.js"
_KEY_HISTORY_CARD_URL = "/ufanet_intercom/ufanet-key-history-card.js"
_KEY_HISTORY_CARD_MODULE_URL = f"{_KEY_HISTORY_CARD_URL}?v=0.32.0"
_PRIVATE_CAMERA_CARD_PATH = _FRONTEND_DIR / "ufanet-private-camera-card.js"
_PRIVATE_CAMERA_CARD_URL = "/ufanet_intercom/ufanet-private-camera-card.js"
_PRIVATE_CAMERA_CARD_MODULE_URL = f"{_PRIVATE_CAMERA_CARD_URL}?v=0.32.0"
_LIVE_CAMERA_CARD_PATH = _FRONTEND_DIR / "ufanet-live-camera-card.js"
_LIVE_CAMERA_CARD_URL = "/ufanet_intercom/ufanet-live-camera-card.js"
_LIVE_CAMERA_CARD_MODULE_URL = f"{_LIVE_CAMERA_CARD_URL}?v=0.32.0-live1"
_HLS_JS_PATH = _FRONTEND_DIR / "hls.light.min.js"
_HLS_JS_URL = "/ufanet_intercom/hls.light.min.js"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.DATETIME,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SENSOR,
]


def _path_is_file(path: Path) -> bool:
    """Return whether a packaged path is a regular file."""
    return path.is_file()


async def _async_ensure_lovelace_module(hass: HomeAssistant, url: str) -> bool:
    """Register a card as a Lovelace module when storage resources are writable.

    ``frontend.add_extra_js_url`` is intentionally only a fallback here. Extra JS
    injection can race Lovelace card construction after a browser refresh, leaving
    a transient ``Custom element doesn't exist`` / configuration error. A normal
    Lovelace module resource participates in the dashboard resource-loading phase.
    """
    try:
        lovelace = hass.data.get("lovelace")
        if lovelace is None:
            return False

        resources = getattr(lovelace, "resources", None)
        if resources is None and isinstance(lovelace, dict):
            resources = lovelace.get("resources")
        if resources is None:
            return False

        async_get_info = getattr(resources, "async_get_info", None)
        if callable(async_get_info):
            await async_get_info()

        async_items = getattr(resources, "async_items", None)
        if not callable(async_items):
            return False

        base_url = str(url).partition("?")[0]
        existing = next(
            (
                item
                for item in async_items()
                if str(item.get("url") or "").partition("?")[0] == base_url
            ),
            None,
        )

        if existing is not None:
            if (
                str(existing.get("url") or "") == url
                and str(existing.get("res_type") or "module") == "module"
            ):
                return True

            async_update_item = getattr(resources, "async_update_item", None)
            resource_id = existing.get("id")
            if not callable(async_update_item) or resource_id is None:
                return False

            await async_update_item(
                resource_id,
                {"res_type": "module", "url": url},
            )
            return True

        async_create_item = getattr(resources, "async_create_item", None)
        if not callable(async_create_item):
            return False

        await async_create_item({"res_type": "module", "url": url})
        return True
    except Exception as err:  # noqa: BLE001 - frontend fallback must stay available
        _LOGGER.warning(
            "Could not register Lovelace module %s; using frontend fallback: %s",
            str(url).partition("?")[0],
            type(err).__name__,
        )
        return False


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-level services and dashboard frontend resources."""
    guest_invite_store = UfanetGuestInviteStore(hass)
    await guest_invite_store.async_load()

    # Keep hass.data[DOMAIN] reserved for config-entry runtime dictionaries.
    # Service handlers retain this Store instance through their closures.
    async_setup_services(hass, guest_invite_store)
    async_setup_private_camera_services(hass)
    async_setup_key_services(hass)

    archive_card_exists = await hass.async_add_executor_job(
        _path_is_file,
        _ARCHIVE_CARD_PATH,
    )
    physical_keys_card_exists = await hass.async_add_executor_job(
        _path_is_file,
        _PHYSICAL_KEYS_CARD_PATH,
    )
    key_history_card_exists = await hass.async_add_executor_job(
        _path_is_file,
        _KEY_HISTORY_CARD_PATH,
    )
    private_camera_card_exists = await hass.async_add_executor_job(
        _path_is_file,
        _PRIVATE_CAMERA_CARD_PATH,
    )

    if archive_card_exists:
        static_paths = [
            StaticPathConfig(
                _ARCHIVE_CARD_URL,
                str(_ARCHIVE_CARD_PATH),
                False,
            )
        ]
        if physical_keys_card_exists:
            static_paths.append(
                StaticPathConfig(
                    _PHYSICAL_KEYS_CARD_URL,
                    str(_PHYSICAL_KEYS_CARD_PATH),
                    False,
                )
            )
        if key_history_card_exists:
            static_paths.append(
                StaticPathConfig(
                    _KEY_HISTORY_CARD_URL,
                    str(_KEY_HISTORY_CARD_PATH),
                    False,
                )
            )
        if private_camera_card_exists:
            static_paths.append(
                StaticPathConfig(
                    _PRIVATE_CAMERA_CARD_URL,
                    str(_PRIVATE_CAMERA_CARD_PATH),
                    False,
                )
            )
        await hass.http.async_register_static_paths(static_paths)

        archive_registered = await _async_ensure_lovelace_module(
            hass,
            _ARCHIVE_CARD_MODULE_URL,
        )
        if not archive_registered:
            frontend.add_extra_js_url(hass, _ARCHIVE_CARD_MODULE_URL)

        if physical_keys_card_exists:
            keys_registered = await _async_ensure_lovelace_module(
                hass,
                _PHYSICAL_KEYS_CARD_MODULE_URL,
            )
            if not keys_registered:
                frontend.add_extra_js_url(hass, _PHYSICAL_KEYS_CARD_MODULE_URL)
        else:
            _LOGGER.warning(
                "Ufanet physical-key card extension was not found at %s",
                _PHYSICAL_KEYS_CARD_PATH,
            )

        if key_history_card_exists:
            history_registered = await _async_ensure_lovelace_module(
                hass,
                _KEY_HISTORY_CARD_MODULE_URL,
            )
            if not history_registered:
                frontend.add_extra_js_url(hass, _KEY_HISTORY_CARD_MODULE_URL)
        else:
            _LOGGER.warning(
                "Ufanet key-history card extension was not found at %s",
                _KEY_HISTORY_CARD_PATH,
            )

        if private_camera_card_exists:
            private_camera_registered = await _async_ensure_lovelace_module(
                hass,
                _PRIVATE_CAMERA_CARD_MODULE_URL,
            )
            if not private_camera_registered:
                frontend.add_extra_js_url(hass, _PRIVATE_CAMERA_CARD_MODULE_URL)
        else:
            _LOGGER.warning(
                "Ufanet standalone-camera card extension was not found at %s",
                _PRIVATE_CAMERA_CARD_PATH,
            )

        _LOGGER.info("Ufanet archive card resource URL: %s", _ARCHIVE_CARD_MODULE_URL)
        if physical_keys_card_exists:
            _LOGGER.info(
                "Ufanet physical-key card extension URL: %s",
                _PHYSICAL_KEYS_CARD_MODULE_URL,
            )
        if key_history_card_exists:
            _LOGGER.info(
                "Ufanet key-history card extension URL: %s",
                _KEY_HISTORY_CARD_MODULE_URL,
            )
        if private_camera_card_exists:
            _LOGGER.info(
                "Ufanet standalone-camera card extension URL: %s",
                _PRIVATE_CAMERA_CARD_MODULE_URL,
            )
    else:
        _LOGGER.warning(
            "Ufanet archive Lovelace card was not found at %s",
            _ARCHIVE_CARD_PATH,
        )

    # Low-latency live camera card (independent of the archive card).
    if await hass.async_add_executor_job(_path_is_file, _LIVE_CAMERA_CARD_PATH):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    _LIVE_CAMERA_CARD_URL, str(_LIVE_CAMERA_CARD_PATH), False
                ),
                StaticPathConfig(_HLS_JS_URL, str(_HLS_JS_PATH), True),
            ]
        )
        if not await _async_ensure_lovelace_module(
            hass, _LIVE_CAMERA_CARD_MODULE_URL
        ):
            frontend.add_extra_js_url(hass, _LIVE_CAMERA_CARD_MODULE_URL)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ufanet Intercom from a config entry."""
    api = UfanetApi(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    try:
        await api.async_login()
    except UfanetAuthError as err:
        raise ConfigEntryAuthFailed from err
    except (UfanetConnectionError, UfanetResponseError) as err:
        raise ConfigEntryNotReady(str(err)) from err

    options = effective_options(entry)
    call_update_mode = str(options[CONF_CALL_UPDATE_MODE])
    call_scan_interval = int(options[CONF_CALL_SCAN_INTERVAL])
    fcm_cleanup_pending = False
    if call_update_mode != CALL_UPDATE_MODE_FCM:
        fcm_cleanup_pending = await async_retry_pending_fcm_unregister(
            hass,
            entry,
            api,
        )
    firebase_config: dict[str, str] | None = None
    fcm_config_error_type: str | None = None
    if call_update_mode == CALL_UPDATE_MODE_FCM:
        try:
            firebase_config = await async_load_firebase_config(
                hass,
                str(options[CONF_FCM_CONFIG_PATH]),
            )
        except (FileNotFoundError, UfanetFirebaseConfigError) as err:
            fcm_config_error_type = type(err).__name__
            _LOGGER.warning(
                "FCM mode requested but local Firebase configuration is unavailable: %s",
                type(err).__name__,
            )

    coordinator = UfanetCoordinator(
        hass,
        api,
        scan_interval_seconds=int(options[CONF_SKUD_SCAN_INTERVAL]),
    )
    await coordinator.async_config_entry_first_refresh()

    call_coordinator = UfanetCallCoordinator(
        hass,
        api,
        # Keep normal polling until the FCM transport confirms its MCS login.
        scan_interval_seconds=call_scan_interval,
        media_refresh_seconds=int(options[CONF_MEDIA_REFRESH_INTERVAL]),
    )
    # Call history is an enhancement. A temporary failure of this endpoint must
    # not prevent door opening/live camera from loading.
    await call_coordinator.async_refresh()
    if call_coordinator.data is None:
        call_coordinator.data = {}

    key_passage_coordinator = UfanetKeyPassageCoordinator(
        hass,
        api,
        entry.entry_id,
        set(coordinator.data),
    )
    await key_passage_coordinator.async_initialize()
    # Physical-key history is optional. Empty/unsupported accounts and a
    # temporary endpoint failure must never block core intercom controls.
    await key_passage_coordinator.async_refresh()
    if key_passage_coordinator.data is None:
        key_passage_coordinator.data = {}

    camera_by_skud = {
        int(skud["id"]): str(skud["cctv_number"])
        for skud in coordinator.data.values()
        if skud.get("cctv_number")
    }
    analytics_coordinator = UfanetMotionAnalyticsCoordinator(
        hass,
        api,
        entry.entry_id,
        camera_by_skud,
    )
    await analytics_coordinator.async_initialize()
    # Motion analytics is read-only and optional. A temporary analytics outage
    # must never block door, camera, calls or archive setup.
    await analytics_coordinator.async_refresh()
    if analytics_coordinator.data is None:
        analytics_coordinator.data = {}

    archive_controllers = {
        int(skud["id"]): UfanetArchiveController(
            api,
            skud,
            default_duration=int(options[CONF_ARCHIVE_DEFAULT_DURATION]),
            default_step=int(options[CONF_ARCHIVE_DEFAULT_STEP]),
        )
        for skud in coordinator.data.values()
        if skud.get("cctv_number")
    }
    if archive_controllers:
        # Archive metadata is optional. Individual initialization failures are
        # handled inside each controller and must not block the intercom.
        await asyncio.gather(
            *(controller.async_initialize() for controller in archive_controllers.values())
        )

    auto_save_manager = UfanetCallAutoSaveManager(hass, options)
    image_status_manager = UfanetLastCallImageStatusManager(
        hass,
        entry,
        coordinator.data.values(),
    )
    await image_status_manager.async_initialize()
    fcm_manager: UfanetFcmManager | None = None

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "call_coordinator": call_coordinator,
        "key_passage_coordinator": key_passage_coordinator,
        "analytics_coordinator": analytics_coordinator,
        "archive_controllers": archive_controllers,
        "entry": entry,
        "options": options,
        "auto_save_manager": auto_save_manager,
        "image_status_manager": image_status_manager,
        "call_update_mode": call_update_mode,
        "fcm_manager": None,
        "fcm_config_error_type": fcm_config_error_type,
        "fcm_cleanup_pending": fcm_cleanup_pending,
    }

    device_registry = dr.async_get(hass)

    def _handle_call_update() -> None:
        """Publish newly detected Ufanet calls on the Home Assistant event bus."""
        for call in call_coordinator.new_calls:
            event_data = _call_event_data(call)

            camera_number = call.get("camera_number")
            skud = next(
                (
                    item
                    for item in coordinator.data.values()
                    if str(item.get("cctv_number") or "") == str(camera_number or "")
                ),
                None,
            )
            if skud is not None:
                skud_id = int(skud["id"])
                event_data["skud_id"] = skud_id
                event_data["device_name"] = device_name(skud)
                device = device_registry.async_get_device(
                    identifiers={(DOMAIN, str(skud_id))},
                )
                if device is not None:
                    event_data["device_id"] = device.id
                    auto_save_manager.schedule(call, device.id)

            hass.bus.async_fire(EVENT_INTERCOM_CALL, event_data)

    def _handle_key_passage_update() -> None:
        """Publish sanitized physical-key passages on the HA event bus."""
        for skud_id, passages in key_passage_coordinator.new_passages.items():
            skud = coordinator.data.get(skud_id)
            if skud is None:
                continue
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, str(skud_id))},
            )
            for passage in passages:
                event_data: dict[str, Any] = {
                    "type": "passage",
                    "skud_id": skud_id,
                    "device_name": device_name(skud),
                    "key_name": passage["key_name"],
                    "occurred_at": passage["occurred_at"],
                }
                if device is not None:
                    event_data["device_id"] = device.id
                hass.bus.async_fire(EVENT_KEY_PASSAGE, event_data)

    def _handle_motion_analytics_update() -> None:
        """Publish coarse motion events without camera or cursor identifiers."""
        for skud_id, events in analytics_coordinator.new_events.items():
            skud = coordinator.data.get(skud_id)
            if skud is None:
                continue
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, str(skud_id))},
            )
            for motion in events:
                event_data: dict[str, Any] = {
                    "type": "motion",
                    "skud_id": skud_id,
                    "device_name": device_name(skud),
                    "occurred_at": motion["occurred_at"],
                }
                if device is not None:
                    event_data["device_id"] = device.id
                hass.bus.async_fire(EVENT_MOTION_ANALYTICS, event_data)

    if firebase_config is not None:

        def _handle_fcm_health_change(healthy: bool) -> None:
            call_coordinator.async_set_scan_interval(
                max(call_scan_interval, FCM_FALLBACK_SCAN_INTERVAL_SECONDS)
                if healthy
                else call_scan_interval
            )

        fcm_manager = UfanetFcmManager(
            hass,
            entry,
            api,
            firebase_config,
            call_coordinator.async_request_refresh,
            on_health_change=_handle_fcm_health_change,
        )
        hass.data[DOMAIN][entry.entry_id]["fcm_manager"] = fcm_manager
        await fcm_manager.async_start()

    # These listeners keep optional coordinators polling even when their entity
    # is disabled. They are registered after runtime setup so only new batches
    # produced after the private baseline/cursor state can reach the event bus.
    entry.async_on_unload(call_coordinator.async_add_listener(_handle_call_update))
    entry.async_on_unload(
        key_passage_coordinator.async_add_listener(_handle_key_passage_update)
    )
    entry.async_on_unload(
        analytics_coordinator.async_add_listener(_handle_motion_analytics_update)
    )
    entry.async_on_unload(auto_save_manager.cancel_all)

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        if fcm_manager is not None:
            await fcm_manager.async_stop()
        image_status_manager.stop()
        raise

    # Recover a very recent call after a Home Assistant/integration restart.
    # Duplicate export is prevented by the hashed call marker in the filename.
    if auto_save_manager.enabled and isinstance(call_coordinator.data, dict):
        for recent_call in call_coordinator.data.values():
            camera_number = recent_call.get("camera_number")
            skud = next(
                (
                    item
                    for item in coordinator.data.values()
                    if str(item.get("cctv_number") or "")
                    == str(camera_number or "")
                ),
                None,
            )
            if skud is None:
                continue
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, str(int(skud["id"])))},
            )
            if device is not None:
                auto_save_manager.schedule(
                    recent_call,
                    device.id,
                    recovery=True,
                )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        fcm_manager = runtime.get("fcm_manager")
        if fcm_manager is not None:
            await fcm_manager.async_stop()
            new_call_update_mode = str(
                effective_options(entry)[CONF_CALL_UPDATE_MODE]
            )
            if (
                runtime.get("call_update_mode") == CALL_UPDATE_MODE_FCM
                and new_call_update_mode != CALL_UPDATE_MODE_FCM
            ):
                await fcm_manager.async_unregister()
        image_status_manager = runtime.get("image_status_manager")
        if image_status_manager is not None:
            image_status_manager.stop()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the owned FCM registration and all private local FCM state."""
    api = UfanetApi(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    await async_remove_stored_fcm_registration(hass, entry, api)


def _call_event_data(call: dict[str, Any]) -> dict[str, Any]:
    """Build event data without persistent tokenized media URLs."""
    data: dict[str, Any] = {
        "type": "call",
        "has_preview": bool(call.get("preview_url")),
        "has_archive": bool(call.get("archive_url")),
    }
    for source, target in (
        ("uuid", "uuid"),
        ("called_at", "called_at"),
        ("timezone", "timezone"),
        ("camera_number", "camera_number"),
        ("address", "address"),
        ("porch", "porch"),
        ("flat", "flat"),
    ):
        value = call.get(source)
        if value is not None:
            data[target] = value
    return data
