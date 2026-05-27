'use strict';

import {SHOW_DEBUG, canonicalType, displayType} from './config.js';
import {searchByLocationStream} from './api.js';
import {buildPlaceRow, buildPlaceCard} from './render.js';
import {setupExport} from './export.js';

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
  const debug =
      SHOW_DEBUG ? document.getElementById('debug') : {textContent: ''};
  const debug2 =
      SHOW_DEBUG ? document.getElementById('debug2') : {textContent: ''};
  const placesList = document.getElementById('places-list');
  if (!SHOW_DEBUG) {
    document.getElementById('debug-section').hidden = true;
  }

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
  const placeStore = new Map();
  const clusterer = new markerClusterer.MarkerClusterer({map: innerMap});
  let lastSearch = null;

  async function onMarkerClick(place_id) {
    const entry = placeStore.get(place_id);
    const detail = {
      ...entry?.place,
      reviews: entry?.reviews,
      photos: entry?.photos,
      displayLabel: entry?.displayLabel
    };
    const card = buildPlaceCard(detail);
    Object.assign(card.style, {
      width: 'max(200px, 30vw)',
      fontSize: 'clamp(12px, 1.1vw, 20px)',
      lineHeight: '1.4',
    });
    const photo = card.querySelector('.preview-photo');
    if (photo)
      Object.assign(
          photo.style, {width: '100%', maxWidth: '100%', height: 'auto'});
    const name = card.querySelector('strong');
    const headerDiv = name?.parentElement;
    if (headerDiv) {
      headerDiv.remove();
      Object.assign(
          headerDiv.style, {whiteSpace: 'normal', overflowWrap: 'anywhere'});
      name.style.fontSize = 'clamp(13px, 1.3vw, 17px)';
      infoWindow.setHeaderContent(headerDiv);
    } else {
      infoWindow.setHeaderContent(null);
    }
    infoWindow.setContent(card);
    infoWindow.open({map: innerMap, anchor: entry.marker});
  }

  async function upsertMarker(place, enriched, counter, typeLabel, yieldEvery) {
    const existing = placeStore.get(place.place_id);
    if (existing) {
      existing.place = place;
      existing.marker.title = place.name ?? '';
      return false;
    }
    const marker = new AdvancedMarkerElement({
      position: {lat: place.latitude, lng: place.longitude},
      title: place.name,
      gmpClickable: true,
    });
    counter.n += 1;
    const displayLabel = `${typeLabel} ${counter.n}`;
    placeStore.set(place.place_id, {
      place,
      marker,
      reviews: [],
      photos: [],
      enriched,
      displayLabel,
      resultNumber: counter.n
    });
    marker.addListener('gmp-click', () => onMarkerClick(place.place_id));
    clusterer.addMarker(marker, true);
    if (counter.n % yieldEvery === 0) {
      debug.textContent = `received: ${counter.n}`;
      clusterer.render();
      await new Promise((r) => setTimeout(r, 0));
    }
    return true;
  }

  function clearPlacesList() {
    for (const row of placesList.querySelectorAll('.place-card')) {
      row.remove();
    }
  }

  const sortState = {column: null, direction: null};
  const filterState = {
    hideBlankWebsites: false,
    hideBlankEmails: false,
    hideBlankPhones: false
  };

  const headerRating = document.getElementById('header-rating');
  const headerRatingCount = document.getElementById('header-rating-count');
  const headerWebsite = document.getElementById('header-website');
  const headerEmails = document.getElementById('header-emails');
  const headerPhone = document.getElementById('header-phone');
  const resetViewBtn = document.getElementById('reset-view-btn');

  function applyFilterToEntry(entry) {
    if (!entry.listEntry) return;
    const hasWebsite = !!entry.place.website;
    const hasEmails = !!(entry.place.emails?.length);
    const hasPhone = !!entry.place.phone;
    const hidden = (filterState.hideBlankWebsites && !hasWebsite) ||
        (filterState.hideBlankEmails && !hasEmails) ||
        (filterState.hideBlankPhones && !hasPhone);
    entry.listEntry.style.display = hidden ? 'none' : '';
  }

  function sortEntries(entries) {
    const key = sortState.column;
    if (!key) {
      entries.sort((a, b) => a.resultNumber - b.resultNumber);
      return;
    }
    const dir = sortState.direction === 'asc' ? 1 : -1;
    entries.sort((a, b) => {
      const va = a.place[key];
      const vb = b.place[key];
      const aNull = va == null;
      const bNull = vb == null;
      if (aNull && bNull) return a.resultNumber - b.resultNumber;
      if (aNull) return 1;
      if (bNull) return -1;
      return (va - vb) * dir;
    });
  }

  function applySortAndFilter() {
    const entries = Array.from(placeStore.values()).filter((e) => e.listEntry);
    for (const entry of entries) applyFilterToEntry(entry);
    sortEntries(entries);
    const frag = document.createDocumentFragment();
    for (const entry of entries) frag.appendChild(entry.listEntry);
    placesList.appendChild(frag);
    updateHeaderUI();
  }

  function cycleSortState(column) {
    if (sortState.column !== column) {
      sortState.column = column;
      sortState.direction = 'asc';
    } else if (sortState.direction === 'asc') {
      sortState.direction = 'desc';
    } else {
      sortState.column = null;
      sortState.direction = null;
    }
  }

  function sortArrow(column) {
    if (sortState.column !== column) return '';
    return sortState.direction === 'asc' ? ' ↑' : ' ↓';
  }

  function updateHeaderUI() {
    headerRating.textContent = `Rating${sortArrow('rating')}`;
    headerRatingCount.textContent = `Rating count${sortArrow('rating_count')}`;
    headerWebsite.textContent =
        filterState.hideBlankWebsites ? 'Website (hiding blanks)' : 'Website';
    headerEmails.textContent =
        filterState.hideBlankEmails ? 'Emails (hiding blanks)' : 'Emails';
    headerPhone.textContent =
        filterState.hideBlankPhones ? 'Phone (hiding blanks)' : 'Phone';
  }

  headerRating.addEventListener('click', () => {
    cycleSortState('rating');
    applySortAndFilter();
  });
  headerRatingCount.addEventListener('click', () => {
    cycleSortState('rating_count');
    applySortAndFilter();
  });
  headerWebsite.addEventListener('click', () => {
    filterState.hideBlankWebsites = !filterState.hideBlankWebsites;
    applySortAndFilter();
  });
  headerEmails.addEventListener('click', () => {
    filterState.hideBlankEmails = !filterState.hideBlankEmails;
    applySortAndFilter();
  });
  headerPhone.addEventListener('click', () => {
    filterState.hideBlankPhones = !filterState.hideBlankPhones;
    applySortAndFilter();
  });
  resetViewBtn.addEventListener('click', () => {
    sortState.column = null;
    sortState.direction = null;
    filterState.hideBlankWebsites = false;
    filterState.hideBlankEmails = false;
    filterState.hideBlankPhones = false;
    applySortAndFilter();
  });
  placesList.addEventListener('click', (e) => {
    const nameCell = e.target.closest('.name-cell');
    if (!nameCell) return;
    const row = nameCell.closest('.place-card');
    const placeId = row?.dataset.placeId;
    if (!placeId) return;
    onMarkerClick(placeId);
    mapElement.scrollIntoView({behavior: 'smooth', block: 'start'});
  });
  updateHeaderUI();

  function renderListEntry(place_id) {
    const entry = placeStore.get(place_id);
    if (!entry) return;
    const detail = {
      ...entry.place,
      reviews: entry.reviews,
      photos: entry.photos,
      displayLabel: entry.displayLabel
    };
    const row = buildPlaceRow(detail);
    if (entry.listEntry) {
      entry.listEntry.replaceChildren(...row.children);
    } else {
      placesList.appendChild(row);
      entry.listEntry = row;
    }
    applyFilterToEntry(entry);
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
    const typedType = typeInput.value.trim();
    const canonical = canonicalType(typedType);
    const main_type = canonical ?? typedType;
    const typeLabel = canonical ? displayType(canonical) : typedType;
    const local_only = localOnlyInput.checked;
    const include_reviews = includeReviewsInput.checked;
    const include_photos = includePhotosInput.checked;
    lastSearch =
        {main_type, typeLabel, canonical, include_reviews, include_photos};

    userMarker.position = {lat, lng: lon};

    clusterer.clearMarkers();
    placeStore.clear();

    debug.textContent = '';
    debug2.textContent = '';
    clearPlacesList();
    debug.textContent += `Input mainType=${main_type}, lat=${lat}, lon=${
        lon}, radius=${radius}, is_rectangle=${is_rectangle}, max-restuls=${
        max_results}, localOnly=${local_only}, includeReviews=${
        include_reviews}, includePhotos=${include_photos}\n`
    try {
      const YIELD_EVERY = 30;
      const enriched = include_reviews || include_photos;
      const counter = {n: 0};
      const t0 = performance.now();
      const rawLines = await searchByLocationStream({
        main_type,
        lat,
        lon,
        radius,
        is_rectangle,
        local_only,
        include_reviews,
        include_photos,
        max_results,
      });
      for await (const rawLine of rawLines) {
        const dt = performance.now() - t0;
        const event = JSON.parse(rawLine);
        debug2.textContent += JSON.stringify(event, null, 2) + '\n';
        if (event.type === 'place_preview' || event.type === 'place_update') {
          event.place.query_time = dt;
          await upsertMarker(
              event.place, enriched, counter, typeLabel, YIELD_EVERY);
          renderListEntry(event.place.place_id);
        } else if (event.type === 'reviews') {
          const entry = placeStore.get(event.place_id);
          if (entry) {
            entry.place.query_time = dt;
            entry.reviews.push(...event.items);
            renderListEntry(event.place_id);
          }
        } else if (event.type === 'photos') {
          const entry = placeStore.get(event.place_id);
          if (entry) {
            entry.place.query_time = dt;
            entry.photos.push(...event.items);
            renderListEntry(event.place_id);
          }
        } else if (event.type === 'done') {
          clusterer.render();
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
  function getVisibleEntries() {
    const out = [];
    for (const row of placesList.querySelectorAll('.place-card')) {
      if (row.style.display === 'none') {
        continue
      };
      const entry = placeStore.get(row.dataset.placeId);
      if (entry) {
        out.push(entry)
      };
    }
    return out;
  }

  setupExport({
    getVisibleEntries,
    getLastSearch: () => lastSearch,
  });

  clearBtn.addEventListener('click', async () => {
    clusterer.clearMarkers();
    placeStore.clear();
    infoWindow.close();
    userMarker.position = {lat: cdmx_center_lat, lng: cdmx_center_lon};
    lonInput.value = cdmx_center_lon;
    latInput.value = cdmx_center_lat;
    debug.textContent = '';
    debug2.textContent = '';
    clearPlacesList();
  });
}

await init();
