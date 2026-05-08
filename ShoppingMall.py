from typing import Any, Iterable
import json
from pathlib import Path

from constants import NAME_TO_FIELD

class ShoppingMall:
    """Class for handling the shopping mall info"""
    name_to_field: dict[str, str | tuple[str, str]] = NAME_TO_FIELD

    def __init__(
        self,
        place_id: str | None = None,
        name: str | None = None,
        address: str | None = None,
        phone: str | None = None,
        opening_hours: dict[str, Any] | None = None,
        secondary_opening_hours: list[dict[str, Any]] | None = None,
        rating: float | None = None,
        rating_count: int | None = None,
        reviews: list[dict[str, Any]] | None = None,
        website: str | None = None,
        coordinates: dict[str, float] | None = None,
        photos: list[dict[str, Any]] | None = None,
        category: list[str] | None = None,
        plus_code: str | None = None,
        email: str | None = None,
        ) -> None:
        self.place_id = place_id
        self.name = name
        self.address = address
        self.phone = phone
        self.opening_hours = opening_hours
        self.secondary_opening_hours = secondary_opening_hours
        self.rating = rating
        self.rating_count = rating_count
        self.reviews = reviews
        self.website = website
        self.coordinates = coordinates
        self.photos = photos
        self.category = category
        self.plus_code = plus_code
        self.email = email

    def __str__(self) -> str:
        return '\n'+str(self.__dict__)

    @classmethod
    def request_fields(cls, *fields: str) -> str:
        result: list[str] = ["places.id"]
        for field in fields:
            api_field = cls.name_to_field.get(field)
            if api_field is None:
                continue
            if isinstance(api_field, str):
                result.append(api_field)
            else:
                result.extend(api_field)
        return ",".join(result)

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
            data = json.load(f)
        return cls(ShoppingMall(**item) for item in data)


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
