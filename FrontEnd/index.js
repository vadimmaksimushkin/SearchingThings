'use strict';

const API_PORT = 8000;
const API_BASE = `http://localhost:${API_PORT}`;


async function init() {
  const [{AdvancedMarkerElement, PinElement}, {InfoWindow}] =
      await Promise.all([
        google.maps.importLibrary('marker'),
        google.maps.importLibrary('maps'),
      ]);

  const cdmx_center_lat = 19.416654;
  const cdmx_center_lon = -99.137536;
  const default_zoom = 12;
  const mapElement = document.querySelector('gmp-map');
  mapElement.setAttribute('center', `${cdmx_center_lat}, ${cdmx_center_lon}`);
  mapElement.setAttribute('zoom', default_zoom)
  const innerMap = mapElement.innerMap;
  innerMap.setOptions({mapTypeControl: false});
  const infoWindow = new InfoWindow();

  const latInput = document.getElementById('lat-input');
  const lonInput = document.getElementById('lon-input');
  const radiusInput = document.getElementById('radius-input');
  const isRectangle = document.getElementById('location-is-rectangle-input');
  const maxResultsInput = document.getElementById('max-results-input');
  const typeInput = document.getElementById('type-input');
  const localOnlyInput = document.getElementById('local-only');
  const includeReviewsInput = document.getElementById('include-reviews-input');
  const includePhotosInput = document.getElementById('include-photos-input');
  const searchBtn = document.getElementById('search-btn');
  const clearBtn = document.getElementById('clear-btn');
  const debug = document.getElementById('debug');
  const debug2 = document.getElementById('debug2');

  const userPin = new PinElement({
    background: '#1a73e8',
    borderColor: '#0b47a1',
    glyphColor: 'white',
  });
  const userMarker = new AdvancedMarkerElement({
    map: innerMap,
    position: {lat: Number(latInput.value), lng: Number(lonInput.value)},
    title: 'Search location',
    content: userPin.element,
  });
  let resultMarkers = [];
  const placeStore = new Map();

  async function clearMarkers(markers) {
    const YIELD_EVERY = 30;
    let i = 0;
    for (const m of markers) {
      m.map = null;
      i += 1;
      if (i % YIELD_EVERY === 0) {
        await new Promise((r) => setTimeout(r, 0));
      }
    }
  }

  async function onMarkerClick(place_id) {
    const entry = placeStore.get(place_id);
    // if (entry?.enriched) {
    //   // Stream provided reviews/photos (or place has none of those); trust the store.
    //   const detail = {...entry.place, reviews: entry.reviews, photos: entry.photos};
    //   infoWindow.setContent(buildPlaceCard(detail));
    //   infoWindow.open({map: innerMap, anchor: entry.marker});
    //   return;
    // }
    const detail = {...entry?.place, reviews: entry?.reviews, photos: entry?.photos};
    infoWindow.setContent(buildPlaceCard(detail));
    infoWindow.open({map: innerMap, anchor: entry.marker});
  // // Fall back to /place/{id} fetch.
  //   const anchor = entry?.marker;
  //   infoWindow.setContent('Loading…');
  //   infoWindow.open({map: innerMap, anchor});
  //   try {
  //     const detail = await getPlaceDetail(place_id);
  //     infoWindow.setContent(buildPlaceCard(detail));
  //   } catch (err) {
  //     infoWindow.setContent(`Error loading place: ${err.message}`);
  //   }
  }

  async function upsertMarker(place, enriched, counter, yieldEvery) {
    const existing = placeStore.get(place.place_id);
    if (existing) {
      existing.place = place;
      existing.marker.title = place.name ?? '';
      return false;
    }
    const marker = new AdvancedMarkerElement({
      map: innerMap,
      position: {lat: place.latitude, lng: place.longitude},
      title: place.name,
      gmpClickable: true,
    });
    placeStore.set(place.place_id, {place, marker, reviews: [], photos: [], enriched});
    marker.addListener('gmp-click', () => onMarkerClick(place.place_id));
    resultMarkers.push(marker);
    counter.n += 1;
    if (counter.n % yieldEvery === 0) {
      debug.textContent = `received: ${counter.n}`;
      await new Promise((r) => setTimeout(r, 0));
    }
    return true;
  }

  function setSearchLocation(lat, lng) {
    latInput.value = lat.toFixed(6);
    lonInput.value = lng.toFixed(6);
    userMarker.position = {lat, lng};
  }

  innerMap.addListener('click', (e) => {
    setSearchLocation(e.latLng.lat(), e.latLng.lng());
  });

  searchBtn.addEventListener('click', async () => {
    const lat = Number(latInput.value);
    const lon = Number(lonInput.value);
    const radius = Number(radiusInput.value);
    const is_rectangle = isRectangle.checked;
    const max_results = Number(maxResultsInput.value);
    const main_type = typeInput.value.trim();
    const local_only = localOnlyInput.checked;
    const include_reviews = includeReviewsInput.checked;
    const include_photos = includePhotosInput.checked;

    userMarker.position = {lat, lng: lon};

    await clearMarkers(resultMarkers);
    resultMarkers = [];
    placeStore.clear();

    debug.textContent = '';
    debug2.textContent = '';
    debug.textContent += `Input mainType=${main_type}, lat=${lat}, lon=${
        lon}, radius=${radius}, is_rectangle=${is_rectangle}, max-restuls=${
        max_results}, localOnly=${local_only}, includeReviews=${
        include_reviews}, includePhotos=${include_photos}\n`
    try {
      // const results = await searchByLocation( main_type, lat,
      // lon, radius, is_rectangle, local_only, max_results); debug.textContent +=
      // `results count: ${results?.length}\n`; debug.textContent +=
      // JSON.stringify(results, null, 2);

      // for (const place of results) {
      //   const marker = new AdvancedMarkerElement({
      //     map: innerMap,
      //     position: {lat: place.latitude, lng: place.longitude},
      //     title: place.name,
      //     gmpClickable: true,
      //   });
      //   marker.addListener('gmp-click', () => {
      //     infoWindow.setContent(buildPlaceCard(place));
      //     infoWindow.open({map: innerMap, anchor: marker});
      //   });
      //   resultMarkers.push(marker);
      // }

      const rawLines = await searchByLocationStream({
        main_type, lat, lon, radius, is_rectangle, local_only,
        include_reviews, include_photos, max_results,
      });
      const YIELD_EVERY = 30;
      const enriched = include_reviews || include_photos;
      const counter = {n: 0};
      for await (const rawLine of rawLines) {
        const event = JSON.parse(rawLine);
        debug2.textContent += JSON.stringify(event, null, 2) + '\n';
        if (event.type === 'place_preview' || event.type === 'place_update') {
          await upsertMarker(event.place, enriched, counter, YIELD_EVERY);
        } else if (event.type === 'reviews') {
          const entry = placeStore.get(event.place_id);
          if (entry) entry.reviews.push(...event.items);
        } else if (event.type === 'photos') {
          const entry = placeStore.get(event.place_id);
          if (entry) entry.photos.push(...event.items);
        } else if (event.type === 'done') {
          debug.textContent = `done (total: ${counter.n})`;
        } else if (event.type === 'error') {
          debug.textContent += `\nstream error: ${event.message}`;
        } else {
          console.warn('unknown stream event type:', event);
        }
      }
    } catch (err) {
      debug.textContent += 'Search failed: ' + err.message;
    } finally {
      searchBtn.disabled = false;
    }
  });
  clearBtn.addEventListener('click', async () => {
    await clearMarkers(resultMarkers);
    resultMarkers = [];
    placeStore.clear();
    infoWindow.close();
    userMarker.position = {lat: cdmx_center_lat, lng: cdmx_center_lon};
    debug.textContent = '';
    debug2.textContent = '';
  });
}

async function searchByLocation(
    main_type, lat, lon, radius, is_rectangle, local_only, max_results) {
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

async function getPlaceDetail(place_id) {
  const response = await fetch(`${API_BASE}/place/${encodeURIComponent(place_id)}`);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

async function searchByLocationStream(opts) {
  const params = new URLSearchParams({
    main_type: opts.main_type,
    lat: String(opts.lat),
    lon: String(opts.lon),
    radius: String(opts.radius),
    is_rectangle: String(opts.is_rectangle),
    local_only: String(opts.local_only),
    include_reviews: String(opts.include_reviews),
    include_photos: String(opts.include_photos),
    max_results: String(opts.max_results),
  });
  const response = await fetch(`${API_BASE}/searchStream?${params}`);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  return readNdjson(response);
}

async function* readNdjson(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const {value, done} = await reader.read();
    if (done) {
      const tail = buf.trim();
      if (tail) yield tail;
      return;
    }
    buf += decoder.decode(value, {stream: true});
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line) yield line;
    }
  }
}

function buildPlaceCard(place) {
  const card = document.createElement('div');

  appendBoldRow(card, place.name);
  appendTextRow(card, place.place_id);
  appendTextRow(card, place.main_type);
  if (place.rating != null) {
    appendTextRow(
        card,
        `Rating ★ ${place.rating.toFixed(1)} (${place.rating_count} reviews)`);
  }
  appendTextRow(card, place.address);
  if (place.phone) {
    appendLinkRow(card, `tel:${place.phone}`, place.phone);
  }
  if (place.website) {
    appendLinkRow(card, place.website, place.website, '_blank');
  }
  if (place.emails && place.emails.length > 0) {
    const row = document.createElement('div');
    place.emails.forEach((email, i) => {
      if (i > 0) row.appendChild(document.createTextNode(', '));
      const a = document.createElement('a');
      a.href = `mailto:${email}`;
      a.textContent = email;
      row.appendChild(a);
    });
    card.appendChild(row);
  }
  if (place.latitude != null && place.longitude != null) {
    appendTextRow(card, `${place.latitude}, ${place.longitude}`);
  }
  appendTextRow(card, place.plus_code);
  if (place.category && place.category.length > 0) {
    appendTextRow(card, place.category.join(', '));
  }
  if (place.reviews && place.reviews.length > 0) {
    appendBoldRow(card, `Reviews (${place.reviews.length})`);
    for (const r of place.reviews) {
      const date = r.published_at ? r.published_at.slice(0, 10) : '';
      const star = r.rating != null ? `★ ${r.rating}` : '★ -';
      const author = r.author_name ?? 'anon';
      appendTextRow(card, `${star} — ${author} (${date})`);
      appendTextRow(card, JSON.stringify(r, null, 2));
    }
  }
  if (place.photos && place.photos.length > 0) {
    appendBoldRow(card, `Photos (${place.photos.length})`);
    for (const p of place.photos) {
      appendTextRow(card, JSON.stringify(p, null, 2));
    }
  }
  return card;
}

function appendTextRow(parent, text) {
  if (text == null) return;
  const row = document.createElement('div');
  row.textContent = text;
  parent.appendChild(row);
}

function appendBoldRow(parent, text) {
  if (text == null) return;
  const row = document.createElement('div');
  const strong = document.createElement('strong');
  strong.textContent = text;
  row.appendChild(strong);
  parent.appendChild(row);
}

function appendLinkRow(parent, href, text, target) {
  const row = document.createElement('div');
  const a = document.createElement('a');
  a.href = href;
  a.textContent = text;
  if (target) {
    a.target = target;
    a.rel = 'noopener noreferrer';
  }
  row.appendChild(a);
  parent.appendChild(row);
}

await init();
