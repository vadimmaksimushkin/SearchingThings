from api_key import GOOGLE_MAPS_API_KEY
from typing import Any
import requests

url = "https://places.googleapis.com/v1/places:searchText"

headers = {
    "Content-Type": "application/json",
    "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
    "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
}

payload: dict[str, Any] = {
    "textQuery": "shopping malls in Mexico City",
    "languageCode": "en",
    # Optional: bias to Mexico City
    "locationBias": {
        "circle": {
            "center": {"latitude": 19.4326, "longitude": -99.1332},
            "radius": 20000.0,
        }
    },
}

resp = requests.post(url, headers=headers, json=payload).json()

for place in resp.get("places", []):
    print(place["displayName"]["text"], "->", place["id"])