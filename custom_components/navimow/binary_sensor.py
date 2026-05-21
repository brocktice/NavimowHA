"""Binary sensors for Navimow."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import ATTR_YARD_ZONE, ATTR_YARD_ZONES, CONF_ZONES, DEFAULT_ZONES, DOMAIN
from .coordinator import NavimowCoordinator, _is_problem_state
from .entity import NavimowEntity
from .yard import find_zone, find_zones, parse_zones


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navimow binary sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]
    added_zone_entities: set[tuple[str, str]] = set()
    entities: list[BinarySensorEntity] = []
    for coordinator in coordinators.values():
        entities.append(NavimowProblemBinarySensor(coordinator, entry))

    async_add_entities(entities)

    @callback
    def add_missing_zone_entities() -> None:
        zone_entities: list[BinarySensorEntity] = []
        zones = parse_zones(entry.options.get(CONF_ZONES, DEFAULT_ZONES))
        for coordinator in coordinators.values():
            for zone in zones:
                zone_name = zone.get("name")
                if not zone_name:
                    continue
                key = (coordinator.device.id, str(zone_name))
                if key in added_zone_entities:
                    continue
                added_zone_entities.add(key)
                zone_entities.append(
                    NavimowZoneBinarySensor(coordinator, entry, str(zone_name))
                )
        if zone_entities:
            async_add_entities(zone_entities)

    add_missing_zone_entities()
    for coordinator in coordinators.values():
        entry.async_on_unload(coordinator.async_add_listener(add_missing_zone_entities))


class NavimowProblemBinarySensor(NavimowEntity, BinarySensorEntity):
    """Mower stuck/problem sensor."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_name = "Stuck"

    def __init__(self, coordinator: NavimowCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"navimow_{self.mower_id}_stuck"

    @property
    def is_on(self) -> bool | None:
        """Return true if mower appears stuck or in error."""
        state = self.state_message
        if state is None:
            return None
        return _is_problem_state(state)

    @property
    def extra_state_attributes(self) -> dict:
        """Return diagnostic attributes."""
        state = self.state_message
        error = _error_code(state.error) if state else None
        mower_point = self.mower_position
        zones = parse_zones(self._entry.options.get(CONF_ZONES, DEFAULT_ZONES))
        yard_zones = (
            find_zones(mower_point[0], mower_point[1], zones) if mower_point else []
        )
        return {
            **super().extra_state_attributes,
            "state": state.state if state else None,
            "error_code": error,
            "battery": state.battery if state else None,
            ATTR_YARD_ZONE: find_zone(mower_point[0], mower_point[1], zones)
            if mower_point
            else None,
            ATTR_YARD_ZONES: yard_zones,
        }


class NavimowZoneBinarySensor(NavimowEntity, BinarySensorEntity):
    """Sensor indicating whether the mower is in a configured yard zone."""

    def __init__(
        self, coordinator: NavimowCoordinator, entry: ConfigEntry, zone_name: str
    ) -> None:
        """Initialize the zone sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self.zone_name = zone_name
        self._attr_name = f"In {zone_name}"
        self._attr_unique_id = (
            f"{DOMAIN}_{self.mower_id}_in_zone_{slugify(zone_name)}"
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if mower is in this zone."""
        mower_point = self.mower_position
        if mower_point is None:
            return None
        zones = parse_zones(self._entry.options.get(CONF_ZONES, DEFAULT_ZONES))
        return self.zone_name in find_zones(mower_point[0], mower_point[1], zones)

    @property
    def extra_state_attributes(self) -> dict:
        """Return zone attributes."""
        mower_point = self.mower_position
        zones = parse_zones(self._entry.options.get(CONF_ZONES, DEFAULT_ZONES))
        yard_zones = (
            find_zones(mower_point[0], mower_point[1], zones) if mower_point else []
        )
        return {
            **super().extra_state_attributes,
            "zone": self.zone_name,
            ATTR_YARD_ZONES: yard_zones,
        }


def _error_code(error) -> str | None:
    if isinstance(error, dict):
        code = error.get("code") or error.get("error_code")
        return str(code) if code else None
    if isinstance(error, str):
        return error
    return None
