import asyncio
import sys
import aiohttp
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from places import ShoppingMallList, ShoppingMall
from credentials import GOOGLE_MAPS_API_KEY
from constants import CITIES, STATES, PLACES_URL

PAGE_SIZE = 20
MAX_PAGES = 3

requested_fields = "places.id" # cheap query
# requested_fields = ShoppingMall.request_fields(
#     "name",
#     "address",
#     "phone",
#     "opening_hours",
#     "rating",
#     "reviews",
#     "website",
#     "coordinates",
#     "photos",
#     "category",
#     "plus_code",
#     "email")


query_state = "shopping malls in {state}, Mexico"
query_city = "shopping malls in {city}, {state}"

async def paginated_search(session: aiohttp.ClientSession, text_query: str, depth: int = MAX_PAGES, requested_fields: str = requested_fields) -> ShoppingMallList:
    if depth > MAX_PAGES:
        depth = MAX_PAGES
    elif depth <= 0:
        depth = 1

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": requested_fields + ",nextPageToken",
    }

    payload: dict[str, Any] = {
        "textQuery": text_query,
        "languageCode": "en",
        "pageSize": PAGE_SIZE,
    }

    malls = ShoppingMallList()
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
        malls.extend(ShoppingMallList(data.get("places", [])))
        token = data.get("nextPageToken")
        if not token:
            break
        payload["pageToken"] = token
    return malls

async def search_cities_and_states(states: list[str], cities: list[tuple[str, str]]) -> list[ShoppingMallList]:
    async with aiohttp.ClientSession() as session:
        state_tasks = [paginated_search(session, query_state.format(state=state), MAX_PAGES) for state in states]
        city_tasks = [paginated_search(session, query_city.format(city=city, state=state), MAX_PAGES) for city, state in cities]
        return await asyncio.gather(*state_tasks, *city_tasks)


if __name__ == "__main__":
    results = asyncio.run(search_cities_and_states(STATES, CITIES))

    all_malls = ShoppingMallList()
    for query_result in results:
        all_malls.extend(query_result)

    print(f"Total malls: {len(all_malls)}")
    all_malls.dedupe()
    # all_malls.to_json_file("malls.json")
    print(f"Total unique malls: {len(all_malls)}")
