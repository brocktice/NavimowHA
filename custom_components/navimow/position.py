"""Position helpers for Navimow payloads."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import math
from typing import Any

LATITUDE_KEYS = (
    "lat",
    "latitude",
    "latGcj02",
    "latWgs84",
    "wgs84Lat",
    "gcj02Lat",
    "latitudeValue",
)
LONGITUDE_KEYS = (
    "lng",
    "lon",
    "longitude",
    "lngGcj02",
    "lngWgs84",
    "wgs84Lng",
    "gcj02Lng",
    "longitudeValue",
)
RELATIVE_X_KEYS = (
    "x",
    "thetaX",
    "theta_x",
    "relativeX",
    "relative_x",
    "postureX",
    "posture_x",
)
RELATIVE_Y_KEYS = (
    "y",
    "thetaY",
    "theta_y",
    "relativeY",
    "relative_y",
    "postureY",
    "posture_y",
)
METERS_PER_DEGREE_LATITUDE = 111111.0
POSITION_KEYS = (
    "position",
    "location",
    "gps",
    "coordinate",
    "coordinates",
    "point",
    "pos",
)
TIMESTAMP_KEYS = (
    "time",
    "timestamp",
    "ts",
)


def extract_position(position: Any) -> tuple[float | None, float | None]:
    """Extract latitude/longitude from known Navimow payload shapes."""
    payload = _to_plain(position)
    latitude, longitude = _extract_from_payload(payload, depth=0)
    if latitude is not None and longitude is not None and _looks_like_coordinate(latitude, longitude):
        return latitude, longitude
    return None, None


def position_dict(position: Any) -> dict[str, float] | None:
    """Return a normalized position dict when coordinates are available."""
    latitude, longitude = extract_position(position)
    if latitude is None or longitude is None:
        return None
    return {"lat": latitude, "lng": longitude}


def position_dict_with_origin(
    position: Any, origin_latitude: float | None, origin_longitude: float | None
) -> dict[str, float] | None:
    """Return absolute GPS from payload, using base-station origin for relative x/y."""
    absolute = position_dict(position)
    if absolute is not None:
        return absolute
    if origin_latitude is None or origin_longitude is None:
        return None
    relative = extract_relative_xy(position)
    if relative is None:
        return None
    x_meters, y_meters = relative
    longitude_scale = METERS_PER_DEGREE_LATITUDE * math.cos(math.radians(origin_latitude))
    if abs(longitude_scale) < 0.000001:
        return None
    return {
        "lat": origin_latitude + (y_meters / METERS_PER_DEGREE_LATITUDE),
        "lng": origin_longitude + (x_meters / longitude_scale),
    }


def extract_relative_xy(position: Any) -> tuple[float, float] | None:
    """Extract mower-relative x/y meters from known location payload shapes."""
    payload = _to_plain(position)
    x_value, y_value = _extract_relative_from_payload(payload, depth=0)
    if x_value is None or y_value is None:
        return None
    return x_value, y_value


def extract_timestamp(position: Any) -> int | None:
    """Extract a millisecond/second timestamp from known location payload shapes."""
    payload = _to_plain(position)
    value = _extract_timestamp_from_payload(payload, depth=0)
    if value is None:
        return None
    return int(value)


def _extract_from_payload(
    payload: Any, depth: int
) -> tuple[float | None, float | None]:
    if depth > 6:
        return None, None

    if isinstance(payload, dict):
        latitude = _first_float(payload, LATITUDE_KEYS)
        longitude = _first_float(payload, LONGITUDE_KEYS)
        if latitude is not None and longitude is not None:
            return latitude, longitude

        for key in POSITION_KEYS:
            if key in payload:
                latitude, longitude = _extract_from_payload(payload[key], depth + 1)
                if latitude is not None and longitude is not None:
                    return latitude, longitude

        for value in payload.values():
            if isinstance(value, dict | list | tuple):
                latitude, longitude = _extract_from_payload(value, depth + 1)
                if latitude is not None and longitude is not None:
                    return latitude, longitude
        return None, None

    if isinstance(payload, list | tuple):
        if len(payload) >= 2:
            first = _as_float(payload[0])
            second = _as_float(payload[1])
            if first is not None and second is not None:
                return first, second
        for value in payload:
            if isinstance(value, dict | list | tuple):
                latitude, longitude = _extract_from_payload(value, depth + 1)
                if latitude is not None and longitude is not None:
                    return latitude, longitude

    return None, None


def _first_float(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in payload:
            value = _as_float(payload[key])
            if value is not None:
                return value
    return None


def _extract_relative_from_payload(
    payload: Any, depth: int
) -> tuple[float | None, float | None]:
    if depth > 6:
        return None, None

    if isinstance(payload, dict):
        x_value = _first_float(payload, RELATIVE_X_KEYS)
        y_value = _first_float(payload, RELATIVE_Y_KEYS)
        if x_value is not None and y_value is not None:
            return x_value, y_value

        for key in POSITION_KEYS:
            if key in payload:
                x_value, y_value = _extract_relative_from_payload(payload[key], depth + 1)
                if x_value is not None and y_value is not None:
                    return x_value, y_value

        for value in payload.values():
            if isinstance(value, dict | list | tuple):
                x_value, y_value = _extract_relative_from_payload(value, depth + 1)
                if x_value is not None and y_value is not None:
                    return x_value, y_value
        return None, None

    if isinstance(payload, list | tuple):
        for value in payload:
            if isinstance(value, dict | list | tuple):
                x_value, y_value = _extract_relative_from_payload(value, depth + 1)
                if x_value is not None and y_value is not None:
                    return x_value, y_value

    return None, None


def _extract_timestamp_from_payload(payload: Any, depth: int) -> float | None:
    if depth > 6:
        return None

    if isinstance(payload, dict):
        value = _first_float(payload, TIMESTAMP_KEYS)
        if value is not None:
            return value

        for key in POSITION_KEYS:
            if key in payload:
                value = _extract_timestamp_from_payload(payload[key], depth + 1)
                if value is not None:
                    return value

        for value in payload.values():
            if isinstance(value, dict | list | tuple):
                timestamp = _extract_timestamp_from_payload(value, depth + 1)
                if timestamp is not None:
                    return timestamp
        return None

    if isinstance(payload, list | tuple):
        for value in payload:
            if isinstance(value, dict | list | tuple):
                timestamp = _extract_timestamp_from_payload(value, depth + 1)
                if timestamp is not None:
                    return timestamp

    return None


def _to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict | list | tuple):
        return value
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except TypeError:
            pass
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


def _looks_like_coordinate(latitude: float, longitude: float) -> bool:
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
