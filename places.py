from dataclasses import dataclass, fields
from typing import Any, ClassVar, Iterable
import json
from pathlib import Path

from constants import NAME_TO_FIELD


@dataclass
class ShoppingMall:
    """Class for handling the shopping mall info"""
    place_id: str | None = None
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    opening_hours: dict[str, Any] | None = None
    secondary_opening_hours: list[dict[str, Any]] | None = None
    rating: float | None = None
    rating_count: int | None = None
    reviews: list[dict[str, Any]] | None = None
    website: str | None = None
    coordinates: dict[str, float] | None = None
    photos: list[dict[str, Any]] | None = None
    category: list[str] | None = None
    plus_code: str | None = None
    email: list[str] | None = None

    name_to_field: ClassVar[dict[str, str | tuple[str, str]]] = NAME_TO_FIELD

    @classmethod
    def request_fields(cls, *names: str) -> str:
        result: list[str] = ["places.id"]
        for name in names:
            api_field = cls.name_to_field.get(name)
            if api_field is None:
                continue
            if isinstance(api_field, str):
                result.append(api_field)
            else:
                result.extend(api_field)
        return ",".join(result)

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShoppingMall":
        """Construct from a plain dict (e.g. one loaded from our own JSON), ignoring unknown keys."""
        allowed = cls.field_names()
        return cls(**{k: v for k, v in data.items() if k in allowed})

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "ShoppingMall":
        display_name = data.get("displayName", {})
        plus_code = data.get("plusCode", {})
        return cls(
            place_id=data.get("id"),
            name=display_name.get("text"),
            address=data.get("formattedAddress"),
            phone=data.get("internationalPhoneNumber"),
            opening_hours=data.get("regularOpeningHours"),
            secondary_opening_hours=data.get("regularSecondaryOpeningHours"),
            rating=data.get("rating"),
            rating_count=data.get("userRatingCount"),
            reviews=data.get("reviews"),
            website=data.get("websiteUri"),
            coordinates=data.get("location"),
            photos=data.get("photos"),
            category=data.get("types"),
            plus_code=plus_code.get("globalCode"),
        )


class ShoppingMallList(list[ShoppingMall]):
    """A list of ShoppingMall objects"""
    def __init__(self, items: Iterable[Any] = ()) -> None:
        converted: list[ShoppingMall] = []
        for item in items:
            if isinstance(item, ShoppingMall):
                converted.append(item)
            elif isinstance(item, dict):
                converted.append(ShoppingMall.from_api_response(item)) # type: ignore
            else:
                raise TypeError(f"Expected ShoppingMall or dict, got {type(item).__name__}")
        super().__init__(converted)

    def dedupe(self) -> None:
        seen: set[str] = set()
        unique: list[ShoppingMall] = []
        for mall in self:
            if mall.place_id is None:
                unique.append(mall)
                continue
            if mall.place_id in seen:
                continue
            seen.add(mall.place_id)
            unique.append(mall)
        self[:] = unique

    def to_json_file(self, path: str | Path = "output.json") -> None:
        data = [mall.__dict__ for mall in self]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "ShoppingMallList":
        with open(path, encoding="utf-8") as f:
            data: Any = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON list at {path}, got {type(data).__name__}")
        malls: list[ShoppingMall] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise TypeError(f"Item {i} in {path} is {type(item).__name__}, expected dict")
            malls.append(ShoppingMall.from_dict(item))
        return cls(malls)


if __name__ == "__main__":
    import requests
    from api_key import GOOGLE_MAPS_API_KEY
    from constants import PLACES_URL

    requested_fields = ShoppingMall.request_fields(
        "address",
        "coordinates",
        "plus_code",
        "category")
    requested_fields1 = ShoppingMall.request_fields(
        "name",
        "address",
        "phone",
        "opening_hours",
        "rating",
        "reviews",
        "website",
        "coordinates",
        "photos",
        "category",
        "plus_code",
        "email")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": requested_fields1,
    }

    payload: dict[str, Any] = {
        "textQuery": "shopping malls in Mexico City",
        "languageCode": "en",
    }

    resp = requests.post(PLACES_URL, headers=headers, json=payload).json()
    malls = ShoppingMallList(resp.get("places", []))
    malls.to_json_file("malls.json")
    malls2 = ShoppingMallList.from_json_file("malls.json")

    for mall in malls2:
        print(mall)
