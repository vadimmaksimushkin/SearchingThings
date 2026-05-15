from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import Any
import sys
import logging

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)


MOCK_RESPONSE: list[dict[str, Any]] = [
    {
      "place_id": "ChIJd51Z9PUB0oURE7x89-mYKpw",
      "main_type": "gym",
      "name": "Fitspin Lomas",
      "address": "Volcán 150, Lomas - Virreyes, Lomas de Chapultepec, Miguel Hidalgo, 11000 Ciudad de México, CDMX, Mexico",
      "phone": None,
      "website": "https://www.fitspin.mx/",
      "rating": 4.6,
      "rating_count": 34,
      "latitude": 19.429278399999998,
      "longitude": -99.2076096,
      "plus_code": "76F2CQHR+PX",
      "category": ["fitness_center","gym","health","sports_activity_location","point_of_interest","establishment"],
      "emails": None,
    },
    {
      "place_id": "ChIJLSnWhmj_0YURXYYaZBTa4ZY",
      "main_type": "gym",
      "name": "Serena Studio del Valle",
      "address": "San Francisco 323, entre Luz Saviñon y Pedro Romero de Terreros, Col del Valle Nte, Benito Juárez, 03103 Ciudad de México, CDMX, Mexico",
      "phone": "+52 55 1225 3146",
      "website": "http://my.fitune.io/serena-studio/info",
      "rating": 5,
      "rating_count": 9,
      "latitude": 19.394672099999998,
      "longitude": -99.17053630000001,
      "plus_code": "76F29RVH+VQ",
      "category": ["gym","sports_school","sports_complex","sports_activity_location","health","point_of_interest","establishment"],
      "emails": None,
    },
    {
      "place_id": "ChIJMwharpP_0YURUFjUgab7I-s",
      "main_type": "gym",
      "name": "60 Mind Fitness",
      "address": "Casa del Obrero Mundial 410-Piso 6, Narvarte Poniente, Benito Juárez, 03000 Ciudad de México, CDMX, Mexico",
      "phone": "+52 56 2541 7143",
      "website": None,
      "rating": 4.9,
      "rating_count": 40,
      "latitude": 19.4019668,
      "longitude": -99.1559159,
      "plus_code": "76F2CR2V+QJ",
      "category": ["gym","sports_activity_location","health","point_of_interest","establishment"],
      "emails": None,
    },
    {
      "place_id": "ChIJ08jJnHr_0YURU9-Rg6F5qgg",
      "main_type": "gym",
      "name": "Casa Hera",
      "address": "Anaxágoras 915, Narvarte Poniente, Benito Juárez, 03100 Ciudad de México, CDMX, Mexico",
      "phone": None,
      "website": "https://pilateshera.com/",
      "rating": 4.1,
      "rating_count": 28,
      "latitude": 19.384880799999998,
      "longitude": -99.15865319999999,
      "plus_code": "76F29RMR+XG",
      "category": ["yoga_studio","fitness_center","gym","sports_complex","sports_school","sports_activity_location","health","point_of_interest","establishment"],
      "emails": "",
    },
  ]


@app.get("/")
async def read_root():
    return {"Hello": "World"}


@app.get("/items/{item_id}")
async def read_item(item_id: int, q: str | None = None) -> dict[str, Any]:
    return {"item_id": item_id, "q": q}

@app.get("/searchByLocation")
async def search_by_location(
  main_type: str,
  lat: float,
  lon: float,
  local_only: bool = True,
  max_results: int = 10) -> list[dict[str, Any]]:
  log.info(f"mainType={main_type}, lat={lat}, lon={lon}, localOnly={local_only}, maxResults={max_results}")
  return MOCK_RESPONSE