"""Pydantic models for the SearchAPI.

Two layers:
  - Domain models (Place, Review, Photo, PlaceDetail) describe entities.
  - Stream event models (PlacePreviewEvent, ReviewsEvent, ...) wrap entities
    with a `type` discriminator for the NDJSON streaming endpoint.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------- Domain models ----------

class Place(BaseModel):
    place_id: str
    main_type: str
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    rating_count: int | None = None
    latitude: float
    longitude: float
    plus_code: str | None = None
    category: list[str] | None = None
    emails: list[str] | None = None
    preview_photo: str | None = None


class Review(BaseModel):
    name: str
    rating: int | None = None
    text: str | None = None
    language_code: str | None = None
    author_name: str | None = None
    author_uri: str | None = None
    author_photo: str | None = None
    published_at: datetime | None = None
    flag_content_uri: str | None = None
    google_maps_uri: str | None = None


class Photo(BaseModel):
    name: str
    width_px: int | None = None
    height_px: int | None = None
    google_maps_uri: str | None = None
    flag_content_uri: str | None = None


class PlaceDetail(Place):
    """Used for the non-streaming /place/{id} endpoint."""
    reviews: list[Review] = Field(default_factory=list) # pyright: ignore[reportUnknownVariableType]
    photos: list[Photo] = Field(default_factory=list) # pyright: ignore[reportUnknownVariableType]


# ---------- Stream events (NDJSON wire format) ----------

class PlacePreviewEvent(BaseModel):
    type: Literal["place_preview"] = "place_preview"
    place: Place


class PlaceUpdateEvent(BaseModel):
    type: Literal["place_update"] = "place_update"
    place: Place


class ReviewsEvent(BaseModel):
    type: Literal["reviews"] = "reviews"
    place_id: str
    items: list[Review]


class PhotosEvent(BaseModel):
    type: Literal["photos"] = "photos"
    place_id: str
    items: list[Photo]


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    message: str


StreamEvent = (
    PlacePreviewEvent
    | PlaceUpdateEvent
    | ReviewsEvent
    | PhotosEvent
    | DoneEvent
    | ErrorEvent
)