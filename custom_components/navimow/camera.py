"""Read-only satellite yard map cameras for Navimow."""

from __future__ import annotations

import asyncio
import math
from io import BytesIO
from typing import Any

from aiohttp import ClientError
from PIL import Image, ImageDraw, ImageFont

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_HEATMAP_MAX_AGE_DAYS,
    ATTR_HEATMAP_SAMPLE_COUNT,
    ATTR_YARD_ZONES,
    CONF_ZONES,
    DEFAULT_ZONES,
)
from .coordinator import HEATMAP_MAX_AGE, NavimowCoordinator
from .entity import NavimowEntity
from .yard import find_zone, find_zones, parse_zones

WIDTH = 1600
HEIGHT = 900
DETAIL_WIDTH = 800
DETAIL_HEIGHT = 800
PADDING = 40
TEXT_SCALE = WIDTH / 900
DETAIL_TEXT_SCALE = DETAIL_WIDTH / 900
EARTH_RADIUS_M = 6371000
MAX_TILE_COUNT = 16
MAX_TILE_ZOOM = 19
MIN_TILE_ZOOM = 16
OVERVIEW_MIN_SPAN_M = 120
DETAIL_MIN_SPAN_M = 45
FONT_PATHS = (
    "/usr/local/lib/python3.14/site-packages/aioslimproto/font/DejaVu-Sans.ttf",
    "/usr/local/lib/python3.13/site-packages/aioslimproto/font/DejaVu-Sans.ttf",
    "/usr/local/lib/python3.12/site-packages/aioslimproto/font/DejaVu-Sans.ttf",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up yard map cameras."""
    data = hass.data["navimow"][entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]
    entities = []
    for coordinator in coordinators.values():
        entities.extend(
            [
                NavimowYardMapCamera(coordinator, entry),
                NavimowYardDetailMapCamera(coordinator, entry),
                NavimowYardHeatmapCamera(coordinator, entry),
            ]
        )
    async_add_entities(entities)


class NavimowYardMapCamera(NavimowEntity, Camera):
    """Camera entity that renders a static satellite yard map."""

    _attr_name = "Yard Map"

    def __init__(self, coordinator: NavimowCoordinator, entry: ConfigEntry) -> None:
        """Initialize the camera."""
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "image/png"
        self._attr_unique_id = f"navimow_{self.mower_id}_yard_map"
        self._entry = entry

    @property
    def zones(self) -> list[dict[str, Any]]:
        """Return configured yard zones."""
        return parse_zones(self._entry.options.get(CONF_ZONES, DEFAULT_ZONES))

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return PNG image bytes."""
        mower_point = self.mower_position
        zones = self.zones
        points = _collect_points(zones, mower_point)
        if not points:
            return _empty_png("No mower position or yard zones available")

        bounds = _bounds(points, WIDTH, HEIGHT, OVERVIEW_MIN_SPAN_M)
        tile_data = await _satellite_tiles(self.hass, bounds)
        yard_zone = _yard_zone(mower_point, zones)
        return await self.hass.async_add_executor_job(
            _render_png,
            bounds,
            zones,
            mower_point,
            self.mower_name,
            yard_zone or "Unknown",
            tile_data,
            WIDTH,
            HEIGHT,
            70,
            112,
            TEXT_SCALE,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return map attributes."""
        mower_point = self.mower_position
        zones = self.zones
        return {
            **super().extra_state_attributes,
            ATTR_YARD_ZONES: find_zones(
                mower_point[0] if mower_point else None,
                mower_point[1] if mower_point else None,
                zones,
            ),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Force the frontend to fetch a fresh still image after state updates."""
        self.async_update_token()
        super()._handle_coordinator_update()


class NavimowYardDetailMapCamera(NavimowYardMapCamera):
    """Camera entity that renders a square mower-centered map."""

    _attr_name = "Yard Map Detail"

    def __init__(self, coordinator: NavimowCoordinator, entry: ConfigEntry) -> None:
        """Initialize the camera."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"navimow_{self.mower_id}_yard_map_detail"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return square PNG image bytes centered on the mower."""
        mower_point = self.mower_position
        zones = self.zones
        points = _collect_points(zones, mower_point)
        if not points:
            return _empty_png("No mower position or yard zones available", DETAIL_WIDTH, DETAIL_HEIGHT)

        full_bounds = _bounds(points, WIDTH, HEIGHT, OVERVIEW_MIN_SPAN_M)
        bounds = _detail_bounds(full_bounds, mower_point)
        tile_data = await _satellite_tiles(self.hass, bounds)
        yard_zone = _yard_zone(mower_point, zones)
        return await self.hass.async_add_executor_job(
            _render_png,
            bounds,
            zones,
            mower_point,
            self.mower_name,
            yard_zone or "Unknown",
            tile_data,
            DETAIL_WIDTH,
            DETAIL_HEIGHT,
            0,
            0,
            DETAIL_TEXT_SCALE,
        )


class NavimowYardHeatmapCamera(NavimowYardMapCamera):
    """Camera entity that renders an aging problem/ok heatmap."""

    _attr_name = "Yard Heatmap"

    def __init__(self, coordinator: NavimowCoordinator, entry: ConfigEntry) -> None:
        """Initialize the camera."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"navimow_{self.mower_id}_yard_heatmap"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return heatmap attributes."""
        return {
            **super().extra_state_attributes,
            ATTR_HEATMAP_SAMPLE_COUNT: len(self.coordinator.heatmap_samples()),
            ATTR_HEATMAP_MAX_AGE_DAYS: HEATMAP_MAX_AGE.days,
        }

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return heatmap PNG image bytes."""
        mower_point = self.mower_position
        samples = self.coordinator.heatmap_samples()
        sample_points = [
            (float(sample["latitude"]), float(sample["longitude"]))
            for sample in samples
            if _coerce_float(sample.get("latitude")) is not None
            and _coerce_float(sample.get("longitude")) is not None
        ]
        zones = self.zones
        base_points = _collect_points(zones, mower_point)
        points = base_points or sample_points
        if not points:
            return _empty_png("No mower position, yard zones, or heatmap samples available")

        bounds = _bounds(points, WIDTH, HEIGHT, OVERVIEW_MIN_SPAN_M)
        tile_data = await _satellite_tiles(self.hass, bounds)
        yard_zone = _yard_zone(mower_point, zones)
        return await self.hass.async_add_executor_job(
            _render_png,
            bounds,
            zones,
            mower_point,
            self.mower_name,
            yard_zone or "Unknown",
            tile_data,
            WIDTH,
            HEIGHT,
            70,
            112,
            TEXT_SCALE,
            samples,
        )


def _yard_zone(
    mower_point: tuple[float, float] | None, zones: list[dict[str, Any]]
) -> str | None:
    if mower_point is None:
        return None
    return find_zone(mower_point[0], mower_point[1], zones)


def _collect_points(
    zones: list[dict[str, Any]], mower_point: tuple[float, float] | None
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if mower_point:
        points.append(mower_point)
    for zone in zones:
        if _is_circle(zone):
            lat = float(zone["center"][0])
            lon = float(zone["center"][1])
            radius = float(zone["radius_m"])
            delta_lat = math.degrees(radius / EARTH_RADIUS_M)
            delta_lon = math.degrees(radius / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
            points.extend(
                [
                    (lat - delta_lat, lon - delta_lon),
                    (lat + delta_lat, lon + delta_lon),
                ]
            )
        elif isinstance(zone.get("polygon"), list):
            for point in zone["polygon"]:
                if isinstance(point, list) and len(point) == 2:
                    points.append((float(point[0]), float(point[1])))
    return points


def _bounds(
    points: list[tuple[float, float]],
    image_width: int,
    image_height: int,
    min_span_m: float,
) -> dict[str, float]:
    reference_lat = sum(lat for lat, _ in points) / len(points)
    xy_points = [_to_xy(lat, lon, {"reference_lat": reference_lat}) for lat, lon in points]
    min_x = min(x for x, _ in xy_points)
    max_x = max(x for x, _ in xy_points)
    min_y = min(y for _, y in xy_points)
    max_y = max(y for _, y in xy_points)
    if max_x == min_x:
        max_x += 1
        min_x -= 1
    if max_y == min_y:
        max_y += 1
        min_y -= 1
    min_x, max_x = _expand_to_min_span(min_x, max_x, min_span_m)
    min_y, max_y = _expand_to_min_span(min_y, max_y, min_span_m)
    margin_x = max((max_x - min_x) * 0.28, 8)
    margin_y = max((max_y - min_y) * 0.28, 8)
    min_x -= margin_x
    max_x += margin_x
    min_y -= margin_y
    max_y += margin_y

    target_aspect = (image_width - PADDING * 2) / (image_height - PADDING * 2 - 70)
    current_aspect = (max_x - min_x) / (max_y - min_y)
    if current_aspect < target_aspect:
        needed_width = (max_y - min_y) * target_aspect
        extra = (needed_width - (max_x - min_x)) / 2
        min_x -= extra
        max_x += extra
    else:
        needed_height = (max_x - min_x) / target_aspect
        extra = (needed_height - (max_y - min_y)) / 2
        min_y -= extra
        max_y += extra

    return {
        "reference_lat": reference_lat,
        "min_x": min_x,
        "max_x": max_x,
        "min_y": min_y,
        "max_y": max_y,
    }


def _detail_bounds(
    full_bounds: dict[str, float], mower_point: tuple[float, float] | None
) -> dict[str, float]:
    """Return a square crop centered on the mower, roughly half the full map width."""
    if mower_point is None:
        return full_bounds
    mower_x, mower_y = _to_xy(mower_point[0], mower_point[1], full_bounds)
    full_width = full_bounds["max_x"] - full_bounds["min_x"]
    full_height = full_bounds["max_y"] - full_bounds["min_y"]
    side = max(full_width * 0.5, full_height * 0.5, DETAIL_MIN_SPAN_M)
    half = side / 2
    return {
        "reference_lat": full_bounds["reference_lat"],
        "min_x": mower_x - half,
        "max_x": mower_x + half,
        "min_y": mower_y - half,
        "max_y": mower_y + half,
    }


def _expand_to_min_span(min_value: float, max_value: float, min_span_m: float) -> tuple[float, float]:
    span = max_value - min_value
    if span >= min_span_m:
        return min_value, max_value
    center = (min_value + max_value) / 2
    half = min_span_m / 2
    return center - half, center + half


def _to_xy(lat: float, lon: float, bounds: dict[str, float]) -> tuple[float, float]:
    reference_lat = bounds["reference_lat"]
    x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(reference_lat))
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def _projector(
    bounds: dict[str, float],
    image_width: int = WIDTH,
    image_height: int = HEIGHT,
    top_offset: int = 70,
    y_base: int = 112,
):
    drawable_width = image_width - PADDING * 2
    drawable_height = image_height - PADDING * 2 - top_offset
    scale = max(
        drawable_width / (bounds["max_x"] - bounds["min_x"]),
        drawable_height / (bounds["max_y"] - bounds["min_y"]),
    )
    offset_x = (image_width - (bounds["max_x"] - bounds["min_x"]) * scale) / 2
    offset_y = y_base + (drawable_height - (bounds["max_y"] - bounds["min_y"]) * scale) / 2

    def project(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        return (
            offset_x + (x - bounds["min_x"]) * scale,
            offset_y + (bounds["max_y"] - y) * scale,
        )

    return project


def _radius_px(
    radius_m: float,
    bounds: dict[str, float],
    image_width: int = WIDTH,
    image_height: int = HEIGHT,
    top_offset: int = 70,
) -> float:
    scale = max(
        (image_width - PADDING * 2) / (bounds["max_x"] - bounds["min_x"]),
        (image_height - PADDING * 2 - top_offset)
        / (bounds["max_y"] - bounds["min_y"]),
    )
    return radius_m * scale


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_circle(zone: dict[str, Any]) -> bool:
    center = zone.get("center")
    return (
        isinstance(center, list)
        and len(center) == 2
        and isinstance(zone.get("radius_m"), int | float)
    )


async def _satellite_tiles(
    hass: HomeAssistant, bounds: dict[str, float]
) -> list[tuple[int, int, int, bytes]]:
    lat_lon_bounds = _lat_lon_bounds(bounds)
    zoom, tiles = _select_tiles(lat_lon_bounds)
    session = async_get_clientsession(hass)
    tile_results = await asyncio.gather(
        *[_fetch_tile_bytes(session, zoom, x, y) for x, y in tiles],
        return_exceptions=True,
    )
    return [
        (zoom, x, y, result)
        for (x, y), result in zip(tiles, tile_results, strict=True)
        if isinstance(result, bytes)
    ]


async def _fetch_tile_bytes(session, zoom: int, x: int, y: int) -> bytes | None:
    url = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        f"World_Imagery/MapServer/tile/{zoom}/{y}/{x}"
    )
    try:
        async with asyncio.timeout(8):
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                return await response.read()
    except (TimeoutError, ClientError):
        return None


def _render_png(
    bounds: dict[str, float],
    zones: list[dict[str, Any]],
    mower_point: tuple[float, float] | None,
    mower_name: str,
    yard_zone: str,
    tile_data: list[tuple[int, int, int, bytes]],
    image_width: int = WIDTH,
    image_height: int = HEIGHT,
    top_offset: int = 70,
    y_base: int = 112,
    text_scale: float = TEXT_SCALE,
    heatmap_samples: list[dict[str, Any]] | None = None,
) -> bytes:
    image = Image.new("RGB", (image_width, image_height), "#f4f6f5")
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, 0, image_width, image_height), fill="#dfe7e1")

    project = _projector(bounds, image_width, image_height, top_offset, y_base)
    for zoom, tile_x, tile_y, data in tile_data:
        try:
            tile = Image.open(BytesIO(data)).convert("RGB")
        except OSError:
            continue
        north, west, south, east = _tile_bounds(zoom, tile_x, tile_y)
        x1, y1 = project(_to_xy(north, west, bounds))
        x2, y2 = project(_to_xy(south, east, bounds))
        dst_left = min(x1, x2)
        dst_top = min(y1, y2)
        dst_width = abs(x2 - x1)
        dst_height = abs(y2 - y1)
        if dst_width <= 0 or dst_height <= 0:
            continue
        intersection = _intersect_rect(
            (
                math.floor(dst_left),
                math.floor(dst_top),
                math.ceil(dst_left + dst_width),
                math.ceil(dst_top + dst_height),
            ),
            (0, 0, image_width, image_height),
        )
        if intersection is None:
            continue
        crop = _source_tile_crop(
            intersection, dst_left, dst_top, dst_width, dst_height, tile.size
        )
        tile = tile.crop(crop).resize(
            (
                max(1, intersection[2] - intersection[0]),
                max(1, intersection[3] - intersection[1]),
            )
        )
        image.paste(tile, (intersection[0], intersection[1]))

    if heatmap_samples:
        _draw_heatmap(draw, bounds, heatmap_samples, image_width, image_height, top_offset, y_base)

    label_font = _font(8, text_scale)
    labels: list[tuple[str, float, float]] = []
    for index, zone in enumerate(zones):
        color = _hex_to_rgba(_zone_color(index), 72)
        outline = _hex_to_rgba(_zone_color(index), 255)
        if _is_circle(zone):
            center = _to_xy(float(zone["center"][0]), float(zone["center"][1]), bounds)
            x, y = project(center)
            radius = max(
                4,
                _radius_px(
                    float(zone["radius_m"]), bounds, image_width, image_height, top_offset
                ),
            )
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline=outline, width=5)
            labels.append((str(zone["name"]), x, y))
        elif isinstance(zone.get("polygon"), list):
            points = [
                project(_to_xy(float(point[0]), float(point[1]), bounds))
                for point in zone["polygon"]
                if isinstance(point, list) and len(point) == 2
            ]
            if len(points) < 3:
                continue
            draw.polygon(points, fill=color)
            draw.line([*points, points[0]], fill=outline, width=5)
            cx = sum(x for x, _ in points) / len(points)
            cy = sum(y for _, y in points) / len(points)
            labels.append((str(zone["name"]), cx, cy))

    for text, x, y in labels:
        _draw_label(draw, text, x, y, label_font, text_scale)

    if mower_point:
        x, y = project(_to_xy(mower_point[0], mower_point[1], bounds))
        marker_radius = 34
        draw.ellipse(
            (x - marker_radius, y - marker_radius, x + marker_radius, y + marker_radius),
            fill="#256d4d",
            outline="#ffffff",
            width=7,
        )
        glyph = [(x - 22, y - 9), (x + 4, y - 9), (x + 22, y + 9), (x + 22, y + 18), (x - 22, y + 18)]
        draw.polygon(glyph, fill="#ffffff")
        draw.ellipse((x - 22, y + 5, x - 8, y + 19), fill="#256d4d")
        draw.ellipse((x + 9, y + 8, x + 22, y + 21), fill="#256d4d")

    if top_offset:
        title_font = _font(18, text_scale)
        subtitle_font = _font(12, text_scale)
        draw.rounded_rectangle((24, 24, image_width - 24, 98), radius=10, fill=(255, 255, 255, 218))
        draw.text((42, 42), f"{mower_name} Yard Map", fill="#17201b", font=title_font)
        draw.text((42, 72), f"Current zone: {yard_zone}", fill="#52635a", font=subtitle_font)

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _draw_heatmap(
    draw: ImageDraw.ImageDraw,
    bounds: dict[str, float],
    samples: list[dict[str, Any]],
    image_width: int,
    image_height: int,
    top_offset: int,
    y_base: int,
) -> None:
    """Draw age-decayed problem/ok samples over the satellite map."""
    now = dt_util.utcnow()
    max_age_seconds = HEATMAP_MAX_AGE.total_seconds()
    project = _projector(bounds, image_width, image_height, top_offset, y_base)
    radius = max(
        18,
        min(70, _radius_px(8, bounds, image_width, image_height, top_offset)),
    )

    for sample in samples:
        latitude = _coerce_float(sample.get("latitude"))
        longitude = _coerce_float(sample.get("longitude"))
        sample_time = _sample_datetime(sample)
        if latitude is None or longitude is None or sample_time is None:
            continue
        age_seconds = max(0.0, (now - sample_time).total_seconds())
        weight = max(0.0, 1.0 - (age_seconds / max_age_seconds))
        if weight <= 0:
            continue
        x, y = project(_to_xy(latitude, longitude, bounds))
        if sample.get("stuck"):
            fill = (220, 32, 32, round(38 + 120 * weight))
            outline = (138, 18, 18, round(45 + 90 * weight))
            sample_radius = radius * (1.1 + 0.6 * weight)
        else:
            fill = (34, 160, 82, round(14 + 45 * weight))
            outline = (20, 100, 52, round(10 + 35 * weight))
            sample_radius = radius * (0.75 + 0.35 * weight)
        draw.ellipse(
            (
                x - sample_radius,
                y - sample_radius,
                x + sample_radius,
                y + sample_radius,
            ),
            fill=fill,
            outline=outline,
            width=max(1, round(2 * weight)),
        )


def _sample_datetime(sample: dict[str, Any]):
    value = sample.get("ts")
    if not isinstance(value, str):
        return None
    return dt_util.parse_datetime(value)


def _empty_png(message: str, image_width: int = WIDTH, image_height: int = HEIGHT) -> bytes:
    image = Image.new("RGB", (image_width, image_height), "#f4f6f5")
    draw = ImageDraw.Draw(image)
    font = _font(16)
    bbox = draw.textbbox((0, 0), message, font=font)
    draw.text(
        ((image_width - (bbox[2] - bbox[0])) / 2, (image_height - (bbox[3] - bbox[1])) / 2),
        message,
        fill="#52635a",
        font=font,
    )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _font(size: int, text_scale: float = TEXT_SCALE):
    scaled_size = round(size * text_scale)
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, scaled_size)
        except OSError:
            continue
    return ImageFont.load_default(scaled_size)


def _draw_label(
    draw: ImageDraw.ImageDraw, text: str, x: float, y: float, font, text_scale: float
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    pos = (x - width / 2, y - height / 2)
    pad_x = round(4 * text_scale)
    pad_y = round(2 * text_scale)
    box = (
        pos[0] - pad_x,
        pos[1] - pad_y,
        pos[0] + width + pad_x,
        pos[1] + height + pad_y,
    )
    draw.rounded_rectangle(box, radius=round(5 * text_scale), fill=(255, 255, 255, 218))
    draw.rounded_rectangle(box, radius=round(5 * text_scale), outline=(23, 32, 27, 80), width=1)
    draw.text(pos, text, fill="#17201b", font=font)


def _hex_to_rgba(color: str, alpha: int) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha


def _zone_color(index: int) -> str:
    colors = ["#256d4d", "#2f6da3", "#9a5b21", "#7b4fa3", "#a33f4b", "#60752f"]
    return colors[index % len(colors)]


def _intersect_rect(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _source_tile_crop(
    intersection: tuple[int, int, int, int],
    dst_left: float,
    dst_top: float,
    dst_width: float,
    dst_height: float,
    tile_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Return the source tile crop for the visible destination rectangle."""
    tile_width, tile_height = tile_size
    left = round(((intersection[0] - dst_left) / dst_width) * tile_width)
    top = round(((intersection[1] - dst_top) / dst_height) * tile_height)
    right = round(((intersection[2] - dst_left) / dst_width) * tile_width)
    bottom = round(((intersection[3] - dst_top) / dst_height) * tile_height)
    left = max(0, min(tile_width - 1, left))
    top = max(0, min(tile_height - 1, top))
    right = max(left + 1, min(tile_width, right))
    bottom = max(top + 1, min(tile_height, bottom))
    return (
        left,
        top,
        right,
        bottom,
    )


def _lat_lon_bounds(bounds: dict[str, float]) -> dict[str, float]:
    reference_lat = bounds["reference_lat"]

    def lon_from_x(x: float) -> float:
        return math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(reference_lat))))

    def lat_from_y(y: float) -> float:
        return math.degrees(y / EARTH_RADIUS_M)

    return {
        "north": lat_from_y(bounds["max_y"]),
        "south": lat_from_y(bounds["min_y"]),
        "west": lon_from_x(bounds["min_x"]),
        "east": lon_from_x(bounds["max_x"]),
    }


def _select_tiles(lat_lon_bounds: dict[str, float]) -> tuple[int, list[tuple[int, int]]]:
    for zoom in range(MAX_TILE_ZOOM, MIN_TILE_ZOOM - 1, -1):
        tile_range = _tile_range_for_bounds(lat_lon_bounds, zoom)
        if _tile_range_count(tile_range) <= MAX_TILE_COUNT:
            return zoom, _tiles_from_range(tile_range)
    return MIN_TILE_ZOOM, _tiles_from_range(
        _tile_range_for_bounds(lat_lon_bounds, MIN_TILE_ZOOM), MAX_TILE_COUNT
    )


def _tiles_for_bounds(
    lat_lon_bounds: dict[str, float], zoom: int
) -> list[tuple[int, int]]:
    return _tiles_from_range(_tile_range_for_bounds(lat_lon_bounds, zoom))


def _tile_range_for_bounds(
    lat_lon_bounds: dict[str, float], zoom: int
) -> tuple[int, int, int, int]:
    west = lat_lon_bounds["west"]
    east = lat_lon_bounds["east"]
    north = lat_lon_bounds["north"]
    south = lat_lon_bounds["south"]
    min_x, min_y = _lat_lon_to_tile(north, west, zoom)
    max_x, max_y = _lat_lon_to_tile(south, east, zoom)
    return min(min_x, max_x), max(min_x, max_x), min(min_y, max_y), max(min_y, max_y)


def _tile_range_count(tile_range: tuple[int, int, int, int]) -> int:
    min_x, max_x, min_y, max_y = tile_range
    return (max_x - min_x + 1) * (max_y - min_y + 1)


def _tiles_from_range(
    tile_range: tuple[int, int, int, int], limit: int | None = None
) -> list[tuple[int, int]]:
    min_x, max_x, min_y, max_y = tile_range
    tiles: list[tuple[int, int]] = []
    for x in range(min_x, max_x + 1):
        for y in range(min_y, max_y + 1):
            tiles.append((x, y))
            if limit is not None and len(tiles) >= limit:
                return tiles
    return tiles


def _lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int(
        (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
    )
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _tile_bounds(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    north, west = _tile_to_lat_lon(x, y, zoom)
    south, east = _tile_to_lat_lon(x + 1, y + 1, zoom)
    return north, west, south, east


def _tile_to_lat_lon(x: int, y: int, zoom: int) -> tuple[float, float]:
    n = 2**zoom
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    lat = math.degrees(lat_rad)
    return lat, lon
