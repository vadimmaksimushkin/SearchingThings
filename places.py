from api_key import GOOGLE_MAPS_API_KEY
from typing import Any
import requests
import json
from pathlib import Path
from typing import Any, Iterable


class ShoppingMall:
    name_to_field: dict[str, str] = {
        "name": "places.displayName",
        "address": "places.formattedAddress",
        "phone": "places.internationalPhoneNumber",
        "opening_hours": "places.regularOpeningHours", # FIXME: add places.regularSecondaryOpeningHours to requesting fields as well
        "rating": "places.rating", #FIXME: add places.userRatingCount to requesting fields as well
        "reviews": "places.reviews",
        "website": "places.websiteUri",
        "coordinates": "places.location",
        "photos": "places.photos",
        "category": "places.types",
        "plus_code": "places.plusCode",
        }

    def __init__(
        self,
        name: str | None = None,
        address: str | None = None,
        phone: str | None = None,
        opening_hours: str | None = None, #FIXME: add secondary hours
        rating: str | float | None = None, # FIXME: change to float # FIXME: add ratingCount
        reviews: str | list[Any] | None = None, #FIXME: change to list
        website: str | None = None,
        coordinates: str | None = None,
        photos: str | list[Any] | None = None, #FIXME: change to list
        category: str | list[Any] | None = None, #FIXME: change to list
        plus_code: str | None = None,
        email: str | None = None,
        ) -> None:
        """
        Class for handling the shopping mall info
        """
        self.name = name
        self.address = address
        self.phone = phone
        self.opening_hours = opening_hours
        self.rating = rating
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
        result = ["places.id"]
        for field in fields:
            api_field = cls.name_to_field.get(field)
            if api_field:
                result.append(api_field)
        return ",".join(result)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "ShoppingMall":
        display_name = data.get("displayName") or {}
        plus_code = data.get("plusCode") or {}

        return cls(
            name=display_name.get("text"),
            address=data.get("formattedAddress"),
            phone=data.get("internationalPhoneNumber"),
            opening_hours=data.get("regularOpeningHours"),
            rating=data.get("rating"),
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
                converted.append(ShoppingMall.from_api_response(item))
            else:
                raise TypeError(
                    f"Expected ShoppingMall or dict, got {type(item).__name__}"
                )
        super().__init__(converted)

    def to_json_file(self, path: str | Path = "output.json") -> None:
        data = [mall.__dict__ for mall in self]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "ShoppingMallList":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        instance = cls.__new__(cls)
        list.__init__(instance, [ShoppingMall(**item) for item in data])
        return instance



if __name__ == "__main__":
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

    url = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": requested_fields1,
    }

    payload: dict[str, Any] = {
        "textQuery": "shopping malls in Mexico City",
        "languageCode": "en",
        # Optional: bias to Mexico City
        "locationBias": {
            "circle": {
                "center": {"latitude": 19.4326, "longitude": -99.1332},
                "radius": 50000.0,
            }
        },
    }

    resp = requests.post(url, headers=headers, json=payload).json()
    malls = ShoppingMallList(resp.get("places", []))     # from raw API
    malls.to_json_file("malls.json")                     # save
    # malls = ShoppingMallList.from_json_file("malls.json") # load

    for mall in malls:
        print(mall)
