from typing import Any
import requests
import json

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

QUERY = """
[out:json][timeout:60];
area["name"="Ciudad de México"]["admin_level"="4"]->.searchArea;
(
  node["shop"="mall"](area.searchArea);
  way["shop"="mall"](area.searchArea);
  relation["shop"="mall"](area.searchArea);
);
out center tags;
"""

QUERY_MX = """
[out:json][timeout:300];
area["ISO3166-1"="MX"]["admin_level"="2"]->.searchArea;
(
  node["shop"="mall"](area.searchArea);
  way["shop"="mall"](area.searchArea);
  relation["shop"="mall"](area.searchArea);
);
out center tags;
"""


def save_to_file(data: Any, file_name: str = "output.json") -> None:
    with open(file_name, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

USER_AGENT = "mall-finder/0.1"

HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

def fetch_malls(query: str = QUERY, timeout: int = 120) -> list[dict[str, Any]]:
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("elements", [])


def print_malls(malls: list[dict[str, Any]]) -> None:
    for m in malls:
        name = m.get("tags", {}).get("name", "(unnamed)")
        lat = m.get("lat") or m.get("center", {}).get("lat")
        lon = m.get("lon") or m.get("center", {}).get("lon")
        print(f"{name} -> {lat},{lon}")


if __name__ == "__main__":
    cdmx = fetch_malls(QUERY)
    save_to_file(cdmx, "osm_shopping_malls_mexico_city.json")
    print(f"Mexico City: {len(cdmx)} malls")

    mx = fetch_malls(QUERY_MX, timeout=360)
    save_to_file(mx, "osm_shopping_malls_mexico.json")
    print(f"Mexico (all): {len(mx)} malls")
