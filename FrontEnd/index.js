"use strict";

const API_PORT = 8000;
const API_BASE = `http://localhost:${API_PORT}`;
const MOCK_RESPONSE = [
  {
    place_id: "ChIJEe9KcCP_0YURyJvp7XuCwSo",
    main_type: "gym",
    name: "TRX Nápoles",
    address:
      "Parque Alfonso Esparza, C. Pensilvania, Nápoles, Benito Juárez, 03840 Ciudad de México, CDMX, Mexico         --Mo",
    phone: "+52 55 4572 3411",
    website: null,
    rating: 5,
    rating_count: 44,
    latitude: 19.3896473,
    longitude: -99.177818,
    plus_code: "76F29RQC+VV",
    category: [
      "gym",
      "sports_activity_location",
      "health",
      "point_of_interest",
      "establishment",
    ],
    emails: null,
  },
  {
    place_id: "ChIJCYm2BGkB0oUR_DPjq6sDRDY",
    main_type: "gym",
    name: "Reform Studio",
    address:
      "Monte Athos 149, Lomas - Virreyes, Lomas de Chapultepec, Miguel Hidalgo, 11000 Ciudad de México, CDMX, Mexico",
    phone: "+52 55 2507 8493",
    website:
      "https://www.instagram.com/reformstudio_pilates?igsh=amUyNmdjczNrOGE2",
    rating: 4.5,
    rating_count: 16,
    latitude: 19.420948199999998,
    longitude: -99.2102039,
    plus_code: "76F2CQCQ+9W",
    category: [
      "yoga_studio",
      "fitness_center",
      "gym",
      "sports_complex",
      "sports_school",
      "health",
      "sports_activity_location",
      "point_of_interest",
      "establishment",
    ],
    emails: null,
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
  const useMockInput = document.getElementById("use-mock");
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
    const main_type = typeInput.value.trim();
    const local_only = localOnlyInput.checked;
    const use_mock = useMockInput.checked;

    userMarker.position = {lat, lng: lon};

    for (const m of resultMarkers) {
        m.map = null;
        m.map = null;
    }
    resultMarkers = [];

    debug.textContent = `Mock results: ${use_mock}\n`
    debug.textContent += `Input mainType=${main_type}, lat=${lat}, lon=${lon}, localOnly=${local_only}\n`
    try {
      const results = await searchByLocation(main_type, lat, lon, local_only, -1234, use_mock);
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

async function searchByLocation(main_type, lat, lon, local_only, max_results, use_mock) {
  if (use_mock) {
      return mockSearch(lat, lon, main_type);
  }
  const params = new URLSearchParams({
    main_type,
    lat: String(lat),
    lon: String(lon),
    local_only: String(local_only),
    max_results: String(max_results),
  });
  const response = await fetch(`${API_BASE}/searchByLocation?${params}`);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

function mockSearch(lat, lon, mainType) {
    return MOCK_RESPONSE;
}

void init();
