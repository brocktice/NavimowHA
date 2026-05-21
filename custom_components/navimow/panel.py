"""Home Assistant panel and HTTP API for Navimow Yard zone editing."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from homeassistant.components import frontend, panel_custom
from homeassistant.components.frontend import StaticPathConfig
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import ATTR_YARD_ZONE, ATTR_YARD_ZONES, CONF_ZONES, DOMAIN
from .coordinator import NavimowCoordinator
from .device_tracker import _extract_position
from .yard import find_zone, find_zones

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "navimow-yard"
STATIC_URL_PATH = "/navimow_yard_static"
API_URL = "/api/navimow_yard/zones"
WWW_DIR = Path(__file__).parent / "www"


async def async_setup_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the zone editor panel and API."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL_PATH, str(WWW_DIR), cache_headers=False)]
    )
    hass.http.register_view(NavimowYardZonesView(entry.entry_id))

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="navimow-yard-panel",
        sidebar_title="Navimow Yard",
        sidebar_icon="mdi:robot-mower",
        module_url=f"{STATIC_URL_PATH}/panel.js",
        embed_iframe=False,
        require_admin=True,
        config_panel_domain=DOMAIN,
        config={"url": f"{STATIC_URL_PATH}/zone_editor.html?ha=1"},
    )


async def async_unload_panel(hass: HomeAssistant) -> None:
    """Remove the zone editor panel."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)


class NavimowYardZonesView(HomeAssistantView):
    """Load and save Navimow Yard zone editor state."""

    url = API_URL
    name = "api:navimow_yard:zones"
    requires_auth = True

    def __init__(self, entry_id: str) -> None:
        """Initialize the view."""
        self._entry_id = entry_id

    async def get(self, request: web.Request) -> web.Response:
        """Return saved zones and current mower locations."""
        hass: HomeAssistant = request.app["hass"]
        entry = hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.json_message("Navimow Yard entry not found", status_code=404)

        zones = _parse_zones(entry.options.get(CONF_ZONES, "[]"))
        mowers: list[dict[str, Any]] = []
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        coordinators: dict[str, NavimowCoordinator] = data.get("coordinators", {})
        for coordinator in coordinators.values():
            state = coordinator.get_device_state()
            if state is None:
                continue
            latitude, longitude = _extract_position(state.position)
            if latitude is None or longitude is None:
                continue
            yard_zones = find_zones(latitude, longitude, zones)
            mowers.append(
                {
                    "id": coordinator.device.id,
                    "name": coordinator.device.name or coordinator.device.id,
                    "latitude": latitude,
                    "longitude": longitude,
                    ATTR_YARD_ZONE: find_zone(latitude, longitude, zones),
                    ATTR_YARD_ZONES: yard_zones,
                }
            )

        return self.json({"zones": zones, "mowers": mowers})

    async def post(self, request: web.Request) -> web.Response:
        """Save zones to the config entry options."""
        hass: HomeAssistant = request.app["hass"]
        entry = hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.json_message("Navimow Yard entry not found", status_code=404)

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return self.json_message("Invalid JSON", status_code=400)

        zones = payload.get("zones")
        if not isinstance(zones, list):
            return self.json_message("Expected zones list", status_code=400)

        clean_zones = _clean_zones(zones)
        options = dict(entry.options)
        options[CONF_ZONES] = json.dumps(clean_zones, indent=2)
        hass.config_entries.async_update_entry(entry, options=options)
        _LOGGER.info("Saved %s Navimow Yard zones", len(clean_zones))
        await coordinator_refresh(hass, entry)
        return self.json({"zones": clean_zones})


async def coordinator_refresh(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Refresh coordinator state after saving options."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for coordinator in data.get("coordinators", {}).values():
        await coordinator.async_request_refresh()


def _parse_zones(raw: str) -> list[dict[str, Any]]:
    try:
        zones = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(zones, list):
        return []
    return _clean_zones(zones)


def _clean_zones(zones: list[Any]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for zone in zones:
        if not isinstance(zone, dict) or not zone.get("name"):
            continue
        clean_zone: dict[str, Any] = {"name": str(zone["name"])}
        if _valid_center(zone):
            clean_zone["center"] = [
                float(zone["center"][0]),
                float(zone["center"][1]),
            ]
            clean_zone["radius_m"] = float(zone["radius_m"])
            clean.append(clean_zone)
            continue

        polygon = zone.get("polygon")
        if isinstance(polygon, list) and len(polygon) >= 3:
            points = []
            for point in polygon:
                if not isinstance(point, list) or len(point) != 2:
                    points = []
                    break
                points.append([float(point[0]), float(point[1])])
            if points:
                clean_zone["polygon"] = points
                clean.append(clean_zone)
    return clean


def _valid_center(zone: dict[str, Any]) -> bool:
    center = zone.get("center")
    return (
        isinstance(center, list)
        and len(center) == 2
        and isinstance(zone.get("radius_m"), int | float)
    )
