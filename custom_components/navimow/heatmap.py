"""Signed evidence heatmap rendering helpers."""

from __future__ import annotations

import math
from typing import Iterable

from PIL import Image

EvidencePoint = tuple[int, int, float, float]

CONFIDENCE_WEIGHT = 5.0
MIN_WEIGHT = 0.05
MIN_ALPHA = 45
MAX_ALPHA = 185


def apply_signed_evidence_heatmap(
    image: Image.Image,
    layer_size: tuple[int, int],
    evidence_points: Iterable[EvidencePoint],
    sample_radius: int,
) -> None:
    """Composite signed weighted evidence over an image.

    Evidence points are ``(x, y, value, weight)`` tuples where value ranges from
    -1.0 for problem samples to +1.0 for normal samples.
    """
    layer_width, layer_height = layer_size
    score_sums = [0.0] * (layer_width * layer_height)
    weight_sums = [0.0] * (layer_width * layer_height)

    for layer_x, layer_y, sample_value, weight in evidence_points:
        for offset_y in range(-sample_radius, sample_radius + 1):
            point_y = layer_y + offset_y
            if point_y < 0 or point_y >= layer_height:
                continue
            for offset_x in range(-sample_radius, sample_radius + 1):
                distance_squared = offset_x * offset_x + offset_y * offset_y
                if distance_squared > sample_radius * sample_radius:
                    continue
                point_x = layer_x + offset_x
                if point_x < 0 or point_x >= layer_width:
                    continue
                index = point_y * layer_width + point_x
                distance = math.sqrt(distance_squared) / sample_radius
                spatial_weight = max(0.0, 1.0 - distance * distance)
                evidence_weight = weight * spatial_weight
                score_sums[index] += sample_value * evidence_weight
                weight_sums[index] += evidence_weight

    overlay = Image.new("RGBA", layer_size, (0, 0, 0, 0))
    overlay_pixels = overlay.load()
    for y in range(layer_height):
        for x in range(layer_width):
            index = y * layer_width + x
            weight_sum = weight_sums[index]
            if weight_sum < MIN_WEIGHT:
                continue
            score = max(-1.0, min(1.0, score_sums[index] / weight_sum))
            confidence = min(1.0, weight_sum / CONFIDENCE_WEIGHT)
            alpha = round(MIN_ALPHA + confidence * (MAX_ALPHA - MIN_ALPHA))
            overlay_pixels[x, y] = (*_heatmap_color(score), alpha)

    overlay = overlay.resize(image.size, Image.Resampling.BICUBIC)
    image.paste(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"))


def _heatmap_color(score: float) -> tuple[int, int, int]:
    """Return a red-yellow-green color for a signed heatmap score."""
    red = (235, 38, 38)
    yellow = (245, 191, 66)
    green = (30, 180, 92)
    if score < 0:
        return _interpolate_color(red, yellow, score + 1.0)
    return _interpolate_color(yellow, green, score)


def _interpolate_color(
    start: tuple[int, int, int], end: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    """Linearly interpolate between two RGB colors."""
    ratio = max(0.0, min(1.0, ratio))
    return tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))
