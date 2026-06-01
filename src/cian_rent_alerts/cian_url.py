from __future__ import annotations

import re
from urllib.parse import urlencode
from urllib.parse import parse_qs, unquote, urlsplit

from .cian_locations import CIAN_LOCATION_REGION_IDS
from .config import ConfigError


CITY_HOSTS = {
    "Москва": "www.cian.ru",
    "Санкт-Петербург": "spb.cian.ru",
    "Казань": "kazan.cian.ru",
    "Екатеринбург": "ekb.cian.ru",
    "Нижний Новгород": "nn.cian.ru",
    "Новосибирск": "novosibirsk.cian.ru",
    "Самара": "samara.cian.ru",
}

SORT_VALUES = {
    "default": None,
    "price_from_min_to_max": "price_object_order",
    "price_from_max_to_min": "total_price_desc",
    "creation_date_from_newer_to_older": "creation_date_desc",
    "creation_date_from_older_to_newer": "creation_date_asc",
}


def build_cian_search_url(
    *,
    city: str,
    region_id: str | None,
    rooms: tuple[str, ...],
    min_price: int | None,
    max_price: int | None,
    rent_type: str,
    sort_by: str,
    polygon: str | None = None,
) -> str:
    resolved_region_id = region_id or CIAN_LOCATION_REGION_IDS.get(city)
    if not resolved_region_id:
        raise ConfigError(
            f"Unknown CIAN city {city!r}. Set CIAN_REGION_ID explicitly or add the city mapping."
        )

    query: list[tuple[str, str | int]] = [
        ("engine_version", "2"),
        ("p", "1"),
        ("with_neighbors", "0"),
        ("region", resolved_region_id),
        ("deal_type", "rent"),
        ("offer_type", "flat"),
    ]

    if rent_type == "long":
        query.append(("type", "4"))
    elif rent_type == "short":
        query.append(("type", "2"))
    elif rent_type == "all":
        pass
    else:
        raise ConfigError("CIAN_RENT_TYPE must be 'long', 'short', or 'all'")

    for room in rooms:
        room = room.strip().lower()
        if not room or room == "all":
            continue
        if room == "studio":
            query.append(("room9", "1"))
            continue
        if not room.isdigit() or not 1 <= int(room) <= 5:
            raise ConfigError("CIAN_ROOMS must contain 1..5, studio, or all")
        query.append((f"room{room}", "1"))

    if min_price is not None:
        query.append(("minprice", min_price))
    if max_price is not None:
        query.append(("maxprice", max_price))

    if polygon:
        normalized_polygon = normalize_polygon(polygon)
        query.append(("in_polygon[0]", normalized_polygon))
        query.append(("polygon_name[0]", "Выделенная область"))

    sort_value = SORT_VALUES.get(sort_by)
    if sort_value is None and sort_by != "default":
        raise ConfigError(f"Unknown CIAN_SORT_BY value: {sort_by}")
    if sort_value is not None:
        query.append(("sort", sort_value))

    host = CITY_HOSTS.get(city, "www.cian.ru")
    return f"https://{host}/cat.php?{urlencode(query)}"


def extract_polygon(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ConfigError("Area value is empty")

    if stripped.startswith(("http://", "https://")):
        query = parse_qs(urlsplit(stripped).query)
        polygon_values = query.get("in_polygon[0]") or query.get("in_polygon%5B0%5D")
        if not polygon_values:
            raise ConfigError("CIAN URL does not contain a selected map area")
        return normalize_polygon(polygon_values[0])

    return normalize_polygon(stripped)


def normalize_polygon(value: str) -> str:
    decoded = unquote(value).strip()
    points = [point.strip() for point in decoded.split(",") if point.strip()]
    if len(points) < 3:
        raise ConfigError("Area must contain at least three map points")

    for point in points:
        if not re.fullmatch(r"-?\d+(?:\.\d+)?_-?\d+(?:\.\d+)?", point):
            raise ConfigError("Area must use CIAN polygon format like 49.1513318_55.778841,...")

    return ",".join(points)
