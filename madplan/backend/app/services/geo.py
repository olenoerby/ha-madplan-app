"""Address and store lookups from OpenStreetMap's Nominatim.

These send the household's address or position to a public service, so they only run when
someone has switched the option on and pressed the button. Coordinates can also just be typed
in, or distances entered by hand.

Nominatim's usage policy allows lookups directly triggered by a user, at no more than one
request a second, from an application that identifies itself and credits the data. It also asks
that the service can be switched without a software update, hence MADPLAN_NOMINATIM_URL. Overpass
would be the obvious tool for finding shops, but its public endpoint is for human-operated tools.
Data © OpenStreetMap contributors.
"""

from __future__ import annotations

import math
import time

import httpx

from app.core.config import settings
from app.models import Store
from app.services.stores import chain_slug, haversine_km

_SECONDS_BETWEEN_REQUESTS = 1.1


class GeoError(Exception):
    pass


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        base_url=settings.nominatim_url,
        timeout=20,
        headers={"User-Agent": settings.crawler_user_agent, "Accept-Language": "da"},
        transport=transport,
    )


def geocode(address: str, transport: httpx.BaseTransport | None = None) -> tuple[float, float] | None:
    try:
        with _client(transport) as client:
            response = client.get("/search", params={"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "dk"})
            response.raise_for_status()
            rows = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GeoError(str(exc)) from exc
    return (float(rows[0]["lat"]), float(rows[0]["lon"])) if rows else None


def nearest_branches(
    lat: float, lon: float, radius_km: float, stores: list[Store], transport: httpx.BaseTransport | None = None, sleep=time.sleep
) -> dict[str, dict]:
    """The closest branch of each chain within the radius, keyed by store slug. One request per chain."""
    lat_span = radius_km / 111
    lon_span = radius_km / (111 * max(math.cos(math.radians(lat)), 0.1))
    viewbox = f"{lon - lon_span},{lat + lat_span},{lon + lon_span},{lat - lat_span}"
    best: dict[str, dict] = {}
    failures = 0
    with _client(transport) as client:
        for index, store in enumerate(stores):
            if index:
                sleep(_SECONDS_BETWEEN_REQUESTS)
            try:
                response = client.get(
                    "/search",
                    params={"q": store.name, "format": "jsonv2", "limit": 10, "countrycodes": "dk", "viewbox": viewbox, "bounded": 1, "layer": "poi"},
                )
                response.raise_for_status()
                rows = response.json()
            except (httpx.HTTPError, ValueError):
                # One chain failing still leaves the others useful.
                failures += 1
                continue
            for row in rows:
                if row.get("category") != "shop" or chain_slug(row.get("name") or "") != store.slug:
                    continue
                point_lat, point_lon = float(row["lat"]), float(row["lon"])
                distance = haversine_km(lat, lon, point_lat, point_lon)
                if store.slug not in best or distance < best[store.slug]["straight_km"]:
                    best[store.slug] = {
                        "name": (row.get("display_name") or row["name"]).split(",")[0:2],
                        "lat": point_lat,
                        "lon": point_lon,
                        "straight_km": distance,
                    }
    if failures and failures == len(stores):
        raise GeoError("every lookup failed")
    for branch in best.values():
        branch["name"] = ", ".join(part.strip() for part in branch["name"])
    return best
