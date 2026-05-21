"""Base entities for Navimow."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from mower_sdk.models import DeviceStateMessage

from .const import ATTR_MODEL, ATTR_MOWER_ID, ATTR_SERIAL_NUMBER, DOMAIN
from .coordinator import NavimowCoordinator
from .device_tracker import _extract_position


class NavimowEntity(CoordinatorEntity[NavimowCoordinator]):
    """Base Navimow entity."""

    _attr_has_entity_name = True

    @property
    def mower_id(self) -> str:
        """Return mower ID."""
        return self.coordinator.device.id

    @property
    def mower_name(self) -> str:
        """Return user-friendly mower name."""
        return self.coordinator.device.name or f"Navimow {self.mower_id[:8]}"

    @property
    def state_message(self) -> DeviceStateMessage | None:
        """Return latest state."""
        return self.coordinator.get_device_state()

    @property
    def mower_position(self) -> tuple[float, float] | None:
        """Return latest mower coordinate."""
        state = self.state_message
        if not state:
            return None
        latitude, longitude = _extract_position(state.position)
        if latitude is None or longitude is None:
            return None
        return latitude, longitude

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry info."""
        device = self.coordinator.device
        return DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            manufacturer="Navimow",
            model=device.model or "Unknown",
            name=self.mower_name,
            serial_number=device.serial_number or device.id,
            sw_version=device.firmware_version or None,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return common attributes."""
        device = self.coordinator.device
        return {
            ATTR_MOWER_ID: device.id,
            ATTR_MODEL: device.model,
            ATTR_SERIAL_NUMBER: device.serial_number,
        }
