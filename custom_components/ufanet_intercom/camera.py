"""Camera platform for Ufanet Intercom."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import UfanetApi, UfanetApiError
from .archive import UfanetArchiveController
from .const import DOMAIN
from .coordinator import UfanetCoordinator
from .entity import device_info
from .live_playlist import async_register_live_source, local_base_url
from .private_entities import async_setup_private_camera_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ufanet/UCAMS cameras."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator: UfanetCoordinator = runtime["coordinator"]
    api: UfanetApi = runtime["api"]

    controllers: dict[int, UfanetArchiveController] = runtime["archive_controllers"]

    entities: list[Camera] = []
    for skud in coordinator.data.values():
        if not skud.get("cctv_number"):
            continue
        entities.append(UfanetIntercomCamera(coordinator, api, skud))
        controller = controllers.get(int(skud["id"]))
        if controller is not None:
            entities.append(UfanetArchiveCamera(coordinator, controller, skud))

    async_add_entities(entities)
    await async_setup_private_camera_entities(
        hass,
        entry,
        async_add_entities,
        runtime,
    )


class UfanetIntercomCamera(Camera):
    """UCAMS-backed live camera associated with a Ufanet intercom."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: UfanetCoordinator,
        api: UfanetApi,
        skud: dict[str, Any],
    ) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.api = api
        self.skud_id = int(skud["id"])
        self.camera_number = str(skud["cctv_number"])
        self._attr_unique_id = f"{self.skud_id}_camera_{self.camera_number}"
        self._attr_device_info = device_info(skud)
        self._live_path: str | None = None

    @property
    def available(self) -> bool:
        """Return whether the parent intercom is currently available to the account."""
        if not self.coordinator.last_update_success:
            return False
        skud = self.coordinator.data.get(self.skud_id)
        return bool(
            skud
            and skud.get("cctv_number") == self.camera_number
            and not skud.get("is_blocked")
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates and expose the live-edge playlist."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
        self._live_path, unregister = async_register_live_source(
            self.hass, self._async_fetch_live_playlist
        )
        self.async_on_remove(unregister)

    async def _async_fetch_live_playlist(self, force: bool) -> tuple[str, str]:
        return await self.api.async_get_live_media_playlist(
            self.camera_number,
            stream_number=1,
            force=force,
        )

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a current JPEG snapshot."""
        try:
            # UCAMS supports a ~600 variant; use it only for small requests.
            small = bool(width is not None and width <= 600)
            return await self.api.async_get_snapshot(self.camera_number, small=small)
        except UfanetApiError as err:
            _LOGGER.warning(
                "Unable to fetch Ufanet snapshot for %s: %s",
                self.camera_number,
                err,
            )
            return None

    async def stream_source(self) -> str | None:
        """Return a live HLS URL usable by Home Assistant stream/ffmpeg.

        Prefer the local live-edge playlist so playback starts at the newest
        segment instead of FFmpeg's default three segments back.
        """
        if self._live_path is not None:
            return f"{local_base_url(self.hass)}{self._live_path}"
        try:
            return await self.api.async_get_hls_url(
                self.camera_number,
                stream_number=1,
            )
        except UfanetApiError as err:
            _LOGGER.warning(
                "Unable to obtain Ufanet HLS stream for %s: %s",
                self.camera_number,
                err,
            )
            return None


class UfanetArchiveCamera(Camera):
    """Virtual camera that plays the selected UCAMS archive interval."""

    _attr_has_entity_name = True
    _attr_translation_key = "archive_camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: UfanetCoordinator,
        controller: UfanetArchiveController,
        skud: dict[str, Any],
    ) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.controller = controller
        self.skud_id = int(skud["id"])
        self.camera_number = str(skud["cctv_number"])
        self._attr_unique_id = f"{self.skud_id}_archive_camera_{self.camera_number}"
        self._attr_device_info = device_info(skud)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        skud = self.coordinator.data.get(self.skud_id)
        return bool(
            skud
            and skud.get("cctv_number") == self.camera_number
            and not skud.get("is_blocked")
        )

    @property
    def use_stream_for_stills(self) -> bool:
        """Let HA extract the archive keyframe for the entity picture."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            self.controller.async_add_listener(self._handle_archive_change)
        )

    def _handle_archive_change(self) -> None:
        """Refresh state and, if open, restart the existing HA stream source."""
        self.async_write_ha_state()
        if self.stream is not None:
            self.hass.async_create_task(self._async_update_stream_source())

    async def _async_update_stream_source(self) -> None:
        try:
            new_source = await self.controller.async_get_stream_url()
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Unable to switch Ufanet archive stream for %s: %s",
                self.camera_number,
                err,
            )
            return
        if self.stream is not None:
            self.stream.update_source(new_source)
        self.async_update_token()
        self.async_write_ha_state()

    async def stream_source(self) -> str | None:
        try:
            return await self.controller.async_get_stream_url()
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Unable to obtain Ufanet archive stream for %s: %s",
                self.camera_number,
                err,
            )
            return None
