import asyncio
import math
import sys
from dataclasses import dataclass
from typing import Any

import aiohttp

from ShoppingMall import ShoppingMallList
from api_key import GOOGLE_MAPS_API_KEY
from constants import PLACES_URL

PAGE_SIZE = 20
MAX_PAGES = 3
M_PER_DEG_LAT = 110_800.0


@dataclass
class Location:
    south_west: tuple[float, float] | None = None
    north_east: tuple[float, float] | None = None
    center_point: tuple[float, float] | None = None
    radius: float | None = None  # m
    is_rectangle: bool = False

    @classmethod
    def from_center_point(cls, center_point: tuple[float, float], radius: float, is_rectangle: bool = False) -> "Location":
        lat, lon = center_point
        lat_offset = radius / M_PER_DEG_LAT
        lon_offset = radius / (M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6)) # prevent division by zero

        return cls(
            south_west=(lat - lat_offset, lon - lon_offset),
            north_east=(lat + lat_offset, lon + lon_offset),
            center_point=center_point,
            radius=radius,
            is_rectangle=is_rectangle,
        )

    @classmethod
    def from_corners(cls, south_west: tuple[float, float], north_east: tuple[float, float]) -> "Location":
        center = ((south_west[0] + north_east[0]) / 2,
                  (south_west[1] + north_east[1]) / 2)
        lat_m = (north_east[0] - south_west[0]) / 2 * M_PER_DEG_LAT
        lon_m = (north_east[1] - south_west[1]) / 2 * M_PER_DEG_LAT * math.cos(math.radians(center[0]))

        return cls(
            south_west=south_west,
            north_east=north_east,
            center_point=center,
            radius=math.hypot(lat_m, lon_m),
            is_rectangle=True,
        )

    def to_rectangle(self) -> dict[str, Any]:
        if self.south_west is None or self.north_east is None:
            raise ValueError("Location has no rectangle corners")
        return {
            "rectangle": {
                "low": {"latitude": self.south_west[0], "longitude": self.south_west[1]},
                "high": {"latitude": self.north_east[0], "longitude": self.north_east[1]},
            }
        }

    def to_circle(self) -> dict[str, Any]:
        if self.center_point is None or self.radius is None:
            raise ValueError("Location has no center/radius")
        return {
            "circle": {
                "center": {"latitude": self.center_point[0], "longitude": self.center_point[1]},
                "radius": self.radius,
            }
        }


async def paginated_search(
    session: aiohttp.ClientSession,
    text_query: str,
    location_field: str,
    location_payload: dict[str, Any],
    depth: int = MAX_PAGES) -> ShoppingMallList:

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "places.id,nextPageToken",
    }
    payload: dict[str, Any] = {
        "textQuery": text_query,
        "languageCode": "en",
        "pageSize": PAGE_SIZE,
        location_field: location_payload,
    }

    results = ShoppingMallList()
    for page in range(depth):
        try:
            async with session.post(PLACES_URL, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    print(f"[HTTP {resp.status}] page={page + 1} {text_query!r}: {body[:500]}", file=sys.stderr)
                    break
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"[{type(e).__name__}] {text_query!r}: {e}", file=sys.stderr)
            break
        results.extend(ShoppingMallList(data.get("places", [])))
        token = data.get("nextPageToken")
        if not token:
            break
        payload["pageToken"] = token
    return results


async def search_gyms_by_location(location: Location, is_bias: bool, text_query: str = "gimnasios") -> ShoppingMallList:
    async with aiohttp.ClientSession() as session:
        if is_bias:
            payload = location.to_rectangle() if location.is_rectangle else location.to_circle()
            return await paginated_search(session, text_query, "locationBias", payload)
        return await paginated_search(session, text_query, "locationRestriction", location.to_rectangle())


if __name__ == "__main__":
    RADIUS = 15_500  # m
    # CDMX_TEST = (19.4326, -99.1332)  # Zócalo
    CDMX_TEST = (19.412429, -99.1664120) # Near Condesa / Roma Norte / Roma Sur
    QUERY = "gimnasios"
    total_bias = ShoppingMallList()
    total_all = ShoppingMallList()

    rect_loc = Location.from_center_point(CDMX_TEST, RADIUS, is_rectangle=True)
    circle_loc = Location.from_center_point(CDMX_TEST, RADIUS, is_rectangle=False)

    restriction_rect = asyncio.run(search_gyms_by_location(rect_loc, is_bias=False, text_query=QUERY))
    print(f"locationRestriction + rectangle: {len(restriction_rect)}")

    bias_rect = asyncio.run(search_gyms_by_location(rect_loc, is_bias=True, text_query=QUERY))
    print(f"locationBias        + rectangle: {len(bias_rect)}")

    bias_circle = asyncio.run(search_gyms_by_location(circle_loc, is_bias=True, text_query=QUERY))
    print(f"locationBias        + circle:    {len(bias_circle)}")

    total_bias.extend(bias_rect + bias_circle)
    print(f"Total bias               + overall:   {len(total_bias)}")
    total_bias.dedupe()
    print(f"Total bias               + unique:    {len(total_bias)}")

    total_all.extend(restriction_rect + bias_rect + bias_circle)

    print(f"Total all               + overall:   {len(total_all)}")
    total_all.dedupe()
    print(f"Total all               + unique:    {len(total_all)}")
