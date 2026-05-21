"""Yard zone helpers for Navimow."""

from __future__ import annotations

import json
import math
from typing import Any

EARTH_RADIUS_M = 6371000


def parse_zones(raw: str | None) -> list[dict[str, Any]]:
    """Parse zone JSON from options."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [zone for zone in value if isinstance(zone, dict) and zone.get("name")]


def find_zones(
    latitude: float | None, longitude: float | None, zones: list[dict[str, Any]]
) -> list[str]:
    """Return configured zones containing a coordinate, smallest to largest."""
    if latitude is None or longitude is None:
        return []
    matches: list[tuple[float, str]] = []
    for zone in zones:
        if _in_circle(latitude, longitude, zone):
            matches.append((_circle_area(zone), str(zone["name"])))
        elif _in_polygon(latitude, longitude, zone):
            matches.append((_polygon_area_m2(zone), str(zone["name"])))
    return [name for _, name in sorted(matches, key=lambda match: match[0])]


def find_zone(
    latitude: float | None, longitude: float | None, zones: list[dict[str, Any]]
) -> str | None:
    """Return the smallest configured zone containing a coordinate."""
    matches = find_zones(latitude, longitude, zones)
    return matches[0] if matches else None


def _in_circle(latitude: float, longitude: float, zone: dict[str, Any]) -> bool:
    center = zone.get("center")
    radius_m = zone.get("radius_m")
    if (
        not isinstance(center, list)
        or len(center) != 2
        or not isinstance(radius_m, int | float)
    ):
        return False
    return _distance_m(latitude, longitude, float(center[0]), float(center[1])) <= radius_m


def _in_polygon(latitude: float, longitude: float, zone: dict[str, Any]) -> bool:
    polygon = zone.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 3:
        return False

    inside = False
    j = len(polygon) - 1
    for i, point in enumerate(polygon):
        previous = polygon[j]
        if not _valid_point(point) or not _valid_point(previous):
            return False

        yi, xi = float(point[0]), float(point[1])
        yj, xj = float(previous[0]), float(previous[1])
        intersects = (xi > longitude) != (xj > longitude) and latitude < (
            (yj - yi) * (longitude - xi) / (xj - xi) + yi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _valid_point(point: Any) -> bool:
    return isinstance(point, list) and len(point) == 2


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _circle_area(zone: dict[str, Any]) -> float:
    radius_m = zone.get("radius_m")
    if not isinstance(radius_m, int | float):
        return math.inf
    return math.pi * float(radius_m) ** 2


def _polygon_area_m2(zone: dict[str, Any]) -> float:
    polygon = zone.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 3:
        return math.inf

    points = []
    reference_lat = sum(float(point[0]) for point in polygon) / len(polygon)
    for point in polygon:
        if not _valid_point(point):
            return math.inf
        lat = float(point[0])
        lon = float(point[1])
        x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(reference_lat))
        y = math.radians(lat) * EARTH_RADIUS_M
        points.append((x, y))

    area = 0.0
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        area += point[0] * next_point[1] - next_point[0] * point[1]
    return abs(area) / 2
