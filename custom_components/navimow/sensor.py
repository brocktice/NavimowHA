"""Sensor platform for Navimow integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_YARD_ZONE,
    ATTR_YARD_ZONES,
    CONF_ZONES,
    DEFAULT_ZONES,
    DOMAIN,
)
from .coordinator import NavimowCoordinator
from .entity import NavimowEntity
from .yard import find_zone, find_zones, parse_zones


@dataclass(frozen=True, kw_only=True)
class NavimowSensorEntityDescription(SensorEntityDescription):
    """Describes Navimow sensor entity."""

    value_fn: Callable[[NavimowCoordinator], Any]


SENSOR_DESCRIPTIONS: tuple[NavimowSensorEntityDescription, ...] = (
    NavimowSensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: (
            state.battery if (state := coordinator.get_device_state()) else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navimow sensors from a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    devices = data["devices"]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]

    entities: list[SensorEntity] = []
    for device in devices:
        coordinator = coordinators[device.id]
        for description in SENSOR_DESCRIPTIONS:
            entities.append(
                NavimowSensor(
                    coordinator=coordinator,
                    entity_description=description,
                )
            )
        entities.append(NavimowStatusSensor(coordinator))
        entities.append(NavimowYardZoneSensor(coordinator, config_entry))
    async_add_entities(entities)


class NavimowSensor(CoordinatorEntity[NavimowCoordinator], SensorEntity):
    """Representation of a Navimow sensor."""

    entity_description: NavimowSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        entity_description: NavimowSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description

        device = coordinator.device
        self._attr_unique_id = f"{DOMAIN}_{device.id}_{entity_description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
        )

    @property
    def available(self) -> bool:
        if self.coordinator.get_device_state() is not None:
            return True
        return super().available

    @property
    def native_value(self) -> Any:
        """Return sensor value from coordinator."""
        return self.entity_description.value_fn(self.coordinator)


class NavimowStatusSensor(NavimowEntity, SensorEntity):
    """Human-readable mower status sensor."""

    _attr_name = "Status"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{self.mower_id}_status"

    @property
    def native_value(self) -> str | None:
        """Return mower status."""
        state = self.state_message
        return state.state if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return status attributes useful in automations."""
        state = self.state_message
        meta = self.coordinator.data.get("meta") or {}
        return {
            **super().extra_state_attributes,
            "battery": state.battery if state else None,
            "signal_strength": state.signal_strength if state else None,
            "error_code": _error_code(state.error) if state else None,
            "data_source": meta.get("last_data_source"),
        }


class NavimowYardZoneSensor(NavimowEntity, SensorEntity):
    """Sensor describing which configured yard zone contains the mower."""

    _attr_name = "Yard Zone"

    def __init__(self, coordinator: NavimowCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{self.mower_id}_yard_zone"

    @property
    def native_value(self) -> str | None:
        """Return the smallest matching yard zone."""
        mower_point = self.mower_position
        if mower_point is None:
            return None
        zones = parse_zones(self._entry.options.get(CONF_ZONES, DEFAULT_ZONES))
        return find_zone(mower_point[0], mower_point[1], zones) or "Unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return yard-zone attributes."""
        mower_point = self.mower_position
        zones = parse_zones(self._entry.options.get(CONF_ZONES, DEFAULT_ZONES))
        yard_zones = (
            find_zones(mower_point[0], mower_point[1], zones) if mower_point else []
        )
        return {
            **super().extra_state_attributes,
            ATTR_LATITUDE: mower_point[0] if mower_point else None,
            ATTR_LONGITUDE: mower_point[1] if mower_point else None,
            ATTR_YARD_ZONE: self.native_value,
            ATTR_YARD_ZONES: yard_zones,
        }


def _error_code(error: Any) -> str | None:
    if isinstance(error, dict):
        code = error.get("code") or error.get("error_code")
        return str(code) if code else None
    if isinstance(error, str):
        return error
    return None
