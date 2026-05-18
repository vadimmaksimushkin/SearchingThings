"use strict";

const API_PORT = 8000;
const API_BASE = `http://localhost:${API_PORT}`;


async function init() {
  const [{ AdvancedMarkerElement, PinElement }, { InfoWindow }] = await Promise.all([
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
  const infoWindow = new InfoWindow();

  const latInput = document.getElementById("lat-input");
  const lonInput = document.getElementById("lon-input");
  const radiusInput = document.getElementById("radius-input");
  const isRectangle = document.getElementById("location-is-rectangle-input");
  const maxResultsInput = document.getElementById("max-results-input");
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
    const radius = Number(radiusInput.value);
    const is_rectangle = isRectangle.checked;
    const max_results = Number(maxResultsInput.value);
    const main_type = typeInput.value.trim();
    const local_only = localOnlyInput.checked;

    userMarker.position = {lat, lng: lon};

    for (const m of resultMarkers) {
        m.map = null;
        m.map = null;
    }
    resultMarkers = [];

    debug.textContent +=
        `Input mainType=${main_type}, lat=${lat}, lon=${lon}, radius=${
            radius}, is_rectangle=${is_rectangle}, max-restuls=${
            max_results}, localOnly=${local_only}\n`
    try {
      const results = await searchByLocation(main_type, lat, lon, radius, is_rectangle, local_only, max_results);
      debug.textContent += `results count: ${results?.length}\n`;
      debug.textContent += JSON.stringify(results, null, 2);

      for (const place of results) {
        const marker = new AdvancedMarkerElement({
          map: innerMap,
          position: {lat: place.latitude, lng: place.longitude},
          title: place.name,
          gmpClickable: true,
        });
        marker.addListener("gmp-click", () => {
          const pre = document.createElement("pre");
          pre.textContent = JSON.stringify(place, null, 2);
          infoWindow.setContent(pre);
          infoWindow.open({ map: innerMap, anchor: marker });
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
    infoWindow.close();
    userMarker.position = {lat: cdmx_center_lat, lng: cdmx_center_lon};
    debug.textContent = "";
  });
}

async function searchByLocation(main_type, lat, lon, radius, is_rectangle, local_only, max_results) {
  const params = new URLSearchParams({
    main_type,
    lat: String(lat),
    lon: String(lon),
    radius: String(radius),
    is_rectangle: String(is_rectangle),
    local_only: String(local_only),
    max_results: String(max_results),
  });
  const response = await fetch(`${API_BASE}/searchByLocation?${params}`);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

await init();
