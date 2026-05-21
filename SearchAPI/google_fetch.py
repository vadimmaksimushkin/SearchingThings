import aiohttp
import asyncio
import sys
import logging
import math
from dataclasses import dataclass
from typing import Any, ClassVar
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import GOOGLE_MAPS_API_KEY
from places import ShoppingMall
from SearchAPI.models import Photo, Place, Review
from SearchAPI.local_db_query import parse_published_at
from constants import PLACES_URL

log = logging.getLogger(__name__)

LIVE_QUERY_FIELDS = (
    "name", "address", "phone", "opening_hours", "rating",
    "reviews", "website", "coordinates", "photos", "category",
    "plus_code", "email",
)
LIVE_TEXT_SEARCH_MASK = ShoppingMall.request_fields(*LIVE_QUERY_FIELDS) + ",nextPageToken"
IDS_TEXT_SEARCH_MASK = "places.id,nextPageToken"
PAGE_SIZE = 20
MAX_PAGES = 3

@dataclass
class Location:
    M_PER_DEG_LAT: ClassVar[float] = 110_800.0
    south_west: tuple[float, float] | None = None
    north_east: tuple[float, float] | None = None
    center_point: tuple[float, float] | None = None
    radius: float | None = None  # m
    is_rectangle: bool = False

    @classmethod
    def from_center_point(cls, center_point: tuple[float, float], radius: float, is_rectangle: bool = False) -> "Location":
        lat, lon = center_point
        lat_offset = radius / cls.M_PER_DEG_LAT
        lon_offset = radius / (cls.M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6)) # prevent division by zero

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
        lat_m = (north_east[0] - south_west[0]) / 2 * cls.M_PER_DEG_LAT
        lon_m = (north_east[1] - south_west[1]) / 2 * cls.M_PER_DEG_LAT * math.cos(math.radians(center[0]))

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
    field_mask: str,
    depth: int = MAX_PAGES) -> list[dict[str, Any]]:

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": field_mask,
    }
    payload: dict[str, Any] = {
        "textQuery": text_query,
        "languageCode": "en",
        "pageSize": PAGE_SIZE,
        location_field: location_payload,
    }

    results: list[dict[str, Any]] = []
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
        results.extend(data.get("places", []))
        token = data.get("nextPageToken")
        if not token:
            break
        payload["pageToken"] = token
    return results

async def google_text_search(
    location: Location, is_rectangle: bool, text_query: str, live: bool = False,
) -> list[dict[str, Any]]:
    """Run a Google Places textSearch bounded by location, return
    up to 60 (3 pages by 20 results) raw place dicts

    live=False: textSearch IDs only
    live=True: full LIVE_QUERY_FIELDS in X-Goog-FieldMask

    is_rectangle=True: locationRestriction by rectangle
    is_rectangle=False: locationBias by circle
    """
    if is_rectangle:
        location_payload: dict[str, Any] = location.to_rectangle()
        location_restriction_type: str = "locationRestriction"
    else:
        location_payload: dict[str, Any] = location.to_circle()
        location_restriction_type: str = "locationBias"

    log.error(f"google_text_search.is_rectangle={is_rectangle}")
    field_mask = LIVE_TEXT_SEARCH_MASK if live else IDS_TEXT_SEARCH_MASK
    async with aiohttp.ClientSession() as session:
        return await paginated_search(
            session, text_query, location_restriction_type, location_payload, field_mask,
        )


def place_from_google(raw: dict[str, Any], main_type: str) -> Place:
    """Return Place from raw json from google"""
    location: dict[str, float] = raw.get("location", {})
    plus_code: dict[str, str] = raw.get("plusCode", {})
    display_name = raw.get("displayName", {})
    return Place(
        place_id=raw["id"],
        main_type=main_type,
        name=display_name.get("text"),
        address=raw.get("formattedAddress"),
        phone=raw.get("internationalPhoneNumber"),
        website=raw.get("websiteUri"),
        rating=raw.get("rating"),
        rating_count=raw.get("userRatingCount"),
        latitude=location["latitude"],
        longitude=location["longitude"],
        plus_code=plus_code.get("globalCode"),
        category=raw.get("types"),
        emails=None,
        preview_photo=None,
    )


def review_from_google(raw: dict[str, Any]) -> Review:
    """Return Review from raw json from google"""
    original: dict[str, str] = raw.get("originalText", {})
    author: dict[str, str] = raw.get("authorAttribution", {})
    return Review(
        name=raw["name"],
        rating=raw.get("rating"),
        text=original.get("text"),
        language_code=original.get("languageCode"),
        author_name=author.get("displayName"),
        author_uri=author.get("uri"),
        author_photo=author.get("photoUri"),
        published_at=parse_published_at(raw.get("publishTime")),
        flag_content_uri=raw.get("flagContentUri"),
        google_maps_uri=raw.get("googleMapsUri"),
    )


def photo_from_google(photo: dict[str, Any]) -> Photo:
    """Return Photo from raw json from google"""
    return Photo(
        name=photo["name"],
        width_px=photo.get("widthPx"),
        height_px=photo.get("heightPx"),
        google_maps_uri=photo.get("googleMapsUri"),
        flag_content_uri=photo.get("flagContentUri"),
    )
