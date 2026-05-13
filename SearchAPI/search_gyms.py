import asyncio
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search import paginated_search
from places import ShoppingMallList, ShoppingMall
from constants import CDMX_COLONIAS, CDMX_COLONIAS_OPTIMIZED


QUERY_TEMPLATE = "gimnasios en {colonia}, CDMX"
OUTPUT_PATH = "gyms.json"

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
requested_fields = "places.id"

async def search_gyms(colonias: list[str] = CDMX_COLONIAS) -> list[ShoppingMallList]:
    async with aiohttp.ClientSession() as session:
        tasks = [paginated_search(session, QUERY_TEMPLATE.format(colonia=c), requested_fields=requested_fields) for c in colonias]
        return await asyncio.gather(*tasks)


if __name__ == "__main__":
    results = asyncio.run(search_gyms(CDMX_COLONIAS_OPTIMIZED))

    all_gyms = ShoppingMallList()
    for query_result in results:
        all_gyms.extend(query_result)

    print(f"Total: {len(all_gyms)}")
    all_gyms.dedupe()
    print(f"Unique: {len(all_gyms)}")
    all_gyms.to_json_file(OUTPUT_PATH)
