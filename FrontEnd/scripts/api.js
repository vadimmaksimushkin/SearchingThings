'use strict';

import {API_BASE} from './config.js';

export async function searchByLocation(
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

export async function getPlaceDetail(place_id) {
  const response = await fetch(`${API_BASE}/place/${encodeURIComponent(place_id)}`);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

export async function searchByLocationStream(opts) {
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
