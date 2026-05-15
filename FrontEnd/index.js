"use strict";

const API_PORT = 8000;
const API_BASE = `http://localhost:${API_PORT}`;
const USE_MOCK = true;
const MOCK_RESPONSE = [
    {
      place_id: "ChIJd51Z9PUB0oURE7x89-mYKpw",
      main_type: "gym",
      name: "Fitspin Lomas",
      address:
        "Volcán 150, Lomas - Virreyes, Lomas de Chapultepec, Miguel Hidalgo, 11000 Ciudad de México, CDMX, Mexico",
      phone: null,
      website: "https://www.fitspin.mx/",
      rating: 4.6,
      rating_count: 34,
      latitude: 19.429278399999998,
      longitude: -99.2076096,
      plus_code: "76F2CQHR+PX",
      category:
        ["fitness_center","gym","health","sports_activity_location","point_of_interest","establishment"],
      emails: null,
    },
    {
      place_id: "ChIJLSnWhmj_0YURXYYaZBTa4ZY",
      main_type: "gym",
      name: "Serena Studio del Valle",
      address: "San Francisco 323, entre Luz Saviñon y Pedro Romero de Terreros, Col del Valle Nte, Benito Juárez, 03103 Ciudad de México, CDMX, Mexico",
      phone: "+52 55 1225 3146",
      website: "http://my.fitune.io/serena-studio/info",
      rating: 5,
      rating_count: 9,
      latitude: 19.394672099999998,
      longitude: -99.17053630000001,
      plus_code: "76F29RVH+VQ",
      category: ["gym","sports_school","sports_complex","sports_activity_location","health","point_of_interest","establishment"],
      emails: null,
    },
    {
      place_id: "ChIJMwharpP_0YURUFjUgab7I-s",
      main_type: "gym",
      name: "60 Mind Fitness",
      address: "Casa del Obrero Mundial 410-Piso 6, Narvarte Poniente, Benito Juárez, 03000 Ciudad de México, CDMX, Mexico",
      phone: "+52 56 2541 7143",
      website: null,
      rating: 4.9,
      rating_count: 40,
      latitude: 19.4019668,
      longitude: -99.1559159,
      plus_code: "76F2CR2V+QJ",
      category: ["gym","sports_activity_location","health","point_of_interest","establishment"],
      emails: null,
    },
    {
      place_id: "ChIJ08jJnHr_0YURU9-Rg6F5qgg",
      main_type: "gym",
      name: "Casa Hera",
      address: "Anaxágoras 915, Narvarte Poniente, Benito Juárez, 03100 Ciudad de México, CDMX, Mexico",
      phone: null,
      website: "https://pilateshera.com/",
      rating: 4.1,
      rating_count: 28,
      latitude: 19.384880799999998,
      longitude: -99.15865319999999,
      plus_code: "76F29RMR+XG",
      category: ["yoga_studio","fitness_center","gym","sports_complex","sports_school","sports_activity_location","health","point_of_interest","establishment"],
      emails: "",
    },
  ];


async function init() {
  const [{ AdvancedMarkerElement, PinElement }] = await Promise.all([
    google.maps.importLibrary("marker"),
    google.maps.importLibrary("maps"),
  ]);

  const cdmx_center_lat = 19.416654;
  const cdmx_center_lon = -99.137536;
  const default_zoom = 12;
  const mapElement = document.querySelector("gmp-map");
  mapElement.setAttribute("center", `${cdmx_center_lat}, ${cdmx_center_lon}`);
  mapElement.setAttribute("zoom", default_zoom)
  const innerMap = mapElement.innerMap;
  innerMap.setOptions({ mapTypeControl: false });

  const latInput = document.getElementById("lat-input");
  const lonInput = document.getElementById("lon-input");
  const typeInput = document.getElementById("type-input");
  const localOnlyInput = document.getElementById("local-only");
  const searchBtn = document.getElementById("search-btn");
  const clearBtn = document.getElementById("clear-btn");
  const debug = document.getElementById("debug");

  const userPin = new PinElement({
    background: "#1a73e8",
    borderColor: "#0b47a1",
    glyphColor: "white",
  });
  const userMarker = new AdvancedMarkerElement({
    map: innerMap,
    position: { lat: Number(latInput.value), lng: Number(lonInput.value) },
    title: "Search location",
    content: userPin.element,
  });
  let resultMarkers = [];

  function setSearchLocation(lat, lng) {
    latInput.value = lat.toFixed(6);
    lonInput.value = lng.toFixed(6);
    userMarker.position = {lat, lng};
  }

  innerMap.addListener("click", (e) => {
    setSearchLocation(e.latLng.lat(), e.latLng.lng());
  });

  searchBtn.addEventListener("click", async () => {
    const lat = Number(latInput.value);
    const lon = Number(lonInput.value);
    const mainType = typeInput.value.trim();
    const localOnly = localOnlyInput.checked;

    userMarker.position = {lat, lng: lon};

    for (const m of resultMarkers) {
        m.map = null;
        m.map = null;
    }
    resultMarkers = [];

    debug.textContent = `Mock results: ${USE_MOCK}\n`
    debug.textContent += `Input mainType=${mainType}, lat=${lat}, lon=${lon}, localOnly=${localOnly}\n`
    try {
      const results = await searchByLocation(mainType, lat, lon, localOnly);
      debug.textContent += JSON.stringify(results, null, 2);

      for (const place of results) {
        const marker = new AdvancedMarkerElement({
          map: innerMap,
          position: {lat: place.latitude, lng: place.longitude},
          title: place.name,
        });
        resultMarkers.push(marker);
      }
    } catch (err) {
      debug.textContent += "Search failed: " + err.message;
    } finally {
      searchBtn.disabled = false;
    }
  });
  clearBtn.addEventListener("click", async () => {
    for (const m of resultMarkers) {
        m.map = null;
        m.map = null;
    }
    resultMarkers = [];
    userMarker.position = {lat: cdmx_center_lat, lng: cdmx_center_lon};
    debug.textContent = "";
  });
}

async function searchByLocation(mainType, lat, lon, localOnly, maxResults) {
  if (USE_MOCK) {
      return mockSearch(lat, lon, mainType);
  }
  // send request to http server, GET searh_by_location
}

function mockSearch(lat, lon, mainType) {
    return MOCK_RESPONSE;
}

void init();
