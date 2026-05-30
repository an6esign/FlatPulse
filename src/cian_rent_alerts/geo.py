from __future__ import annotations

import math
from dataclasses import dataclass

import requests

from .config import ConfigError

EARTH_RADIUS_METERS = 6_371_000


@dataclass(frozen=True, slots=True)
class Coordinates:
    latitude: float
    longitude: float


def geocode_address(address: str, *, user_agent: str, timeout_seconds: int = 10) -> Coordinates:
    cleaned = address.strip()
    if not cleaned:
        raise ConfigError("Address is empty")

    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": cleaned, "format": "json", "limit": 1},
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            verify=requests.certs.where(),
        )
        response.raise_for_status()
    except requests.Timeout as exc:
        raise ConfigError("Geocoder timed out. Try again later.") from exc
    except requests.RequestException as exc:
        raise ConfigError("Geocoder service is unavailable. Try again later.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ConfigError("Geocoder returned an invalid response") from exc

    if not payload:
        raise ConfigError("Address was not found")

    location = payload[0]
    return Coordinates(latitude=float(location["lat"]), longitude=float(location["lon"]))


def build_radius_polygon(
    *,
    latitude: float,
    longitude: float,
    radius_meters: int,
    points: int = 24,
) -> str:
    if not 100 <= radius_meters <= 20_000:
        raise ConfigError("Radius must be between 100 and 20000 meters")
    if points < 8:
        raise ConfigError("Circle polygon must contain at least 8 points")

    lat_rad = math.radians(latitude)
    lon_rad = math.radians(longitude)
    angular_distance = radius_meters / EARTH_RADIUS_METERS

    polygon_points: list[str] = []
    for index in range(points):
        bearing = 2 * math.pi * index / points
        point_lat = math.asin(
            math.sin(lat_rad) * math.cos(angular_distance)
            + math.cos(lat_rad) * math.sin(angular_distance) * math.cos(bearing)
        )
        point_lon = lon_rad + math.atan2(
            math.sin(bearing) * math.sin(angular_distance) * math.cos(lat_rad),
            math.cos(angular_distance) - math.sin(lat_rad) * math.sin(point_lat),
        )
        polygon_points.append(f"{math.degrees(point_lon):.7f}_{math.degrees(point_lat):.7f}")

    return ",".join(polygon_points)
