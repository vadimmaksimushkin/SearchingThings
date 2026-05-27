'use strict';

const XLSX_CDN_URL =
    'https://cdn.sheetjs.com/xlsx-latest/package/dist/xlsx.full.min.js';
const JSZIP_CDN_URL = 'https://unpkg.com/jszip@3.10.1/dist/jszip.min.js';


function field(id, label, get, defaultChecked) {
  return {id, label, defaultChecked, columns: [{header: label, get}]};
}


function formatHours(h) {
  if (h == null) return '';
  if (typeof h === 'string') return h;
  return JSON.stringify(h);
}

const COLUMNS = [
  field('place_id', 'place_id', (e) => e.place.place_id, false),
  field('query_time', 'Query time (ms)', (e) => e.place.query_time, true),
  field('type', 'Type', (e) => e.displayLabel, true),
  field('name', 'Name', (e) => e.place.name, true),
  field('rating', 'Rating', (e) => e.place.rating, true),
  field('rating_count', 'Rating count', (e) => e.place.rating_count, true),
  field('website', 'Website', (e) => e.place.website, true),
  field('emails', 'Emails', (e) => e.place.emails?.join(', '), true),
  field('phone', 'Phone', (e) => e.place.phone, true),
  field(
      'preview_photo', 'Preview photo URL', (e) => e.place.preview_photo, true),
  field('address', 'Address', (e) => e.place.address, false),
  {
    id: 'latlon',
    label: 'Latitude/Longitude',
    defaultChecked: false,
    columns: [
      {header: 'latitude', get: (e) => e.place.latitude},
      {header: 'longitude', get: (e) => e.place.longitude},
    ]
  },
  field('plus_code', 'Plus code', (e) => e.place.plus_code, false),
  field(
      'opening_hours', 'Opening hours',
      (e) => formatHours(e.place.opening_hours), false),
  field(
      'secondary_opening_hours', 'Secondary hours',
      (e) => formatHours(e.place.secondary_opening_hours), false),
];
const TABLE_VIEW_IDS = new Set([
  'query_time',
  'type',
  'name',
  'rating',
  'rating_count',
  'website',
  'emails',
  'phone',
  'preview_photo',
]);
// const REVIEW_FIELDS = [
//   {header: 'place_id', get: (e, r) => e.place.place_id},
//   {header: 'name', get: (e, r) => r.name},
//   {header: 'rating', get: (e, r) => r.rating},
//   {header: 'text', get: (e, r) => r.text},
//   {header: 'language_code', get: (e, r) => r.language_code},
//   {header: 'author_name', get: (e, r) => r.author_name},
//   {header: 'author_uri', get: (e, r) => r.author_uri},
//   {header: 'author_photo', get: (e, r) => r.author_photo},
//   {header: 'published_at', get: (e, r) => r.published_at},
//   {header: 'flag_content_uri', get: (e, r) => r.flag_content_uri},
//   {header: 'google_maps_uri', get: (e, r) => r.google_maps_uri},
// ];
// const PHOTO_FIELDS = [
//   {header: 'place_id', get: (e, p) => e.place.place_id},
//   {header: 'name', get: (e, p) => p.name},
//   {header: 'width_px', get: (e, p) => p.width_px},
//   {header: 'height_px', get: (e, p) => p.height_px},
//   {header: 'google_maps_uri', get: (e, p) => p.google_maps_uri},
//   {header: 'flag_content_uri', get: (e, p) => p.flag_content_uri},
//   {header: 'bucket_key', get: (e, p) => p.bucket_key},
//   {header: 'is_preview', get: (e, p) => p.is_preview},
// ];
const REVIEW_FIELDS = [
  'name',
  'rating',
  'text',
  'language_code',
  'author_name',
  'author_uri',
  'author_photo',
  'published_at',
  'flag_content_uri',
  'google_maps_uri',
];
const PHOTO_FIELDS = [
  'name',
  'width_px',
  'height_px',
  'google_maps_uri',
  'flag_content_uri',
  'bucket_key',
  'is_preview',
];


export function setupExport({getVisibleEntries, getLastSearch}) {
  const btn = document.getElementById('export-btn');
  const status = document.getElementById('export-status');
  const dialog = buildDialog();
  document.body.appendChild(dialog);

  const checkboxes = new Map();
  for (const col of COLUMNS) {
    checkboxes.set(
        col.id, dialog.querySelector(`input[data-col-id="${col.id}"]`));
  }

  const formatRadios =
      Array.from(dialog.querySelectorAll('input[name="export-format"]'));
  const downloadBtn = dialog.querySelector('[data-action="download"]');
  const cancelBtn = dialog.querySelector('[data-action="cancel"]');
  const countLabel = dialog.querySelector('.export-count');

  const relatedRefs = new Map();
  for (const wrap of dialog.querySelectorAll('.related-option')) {
    const kind = wrap.dataset.relatedKind;
    relatedRefs.set(kind, {
      wrap,
      input: wrap.querySelector('input'),
      text: wrap.childNodes[wrap.childNodes.length - 1],
    });
  }

  function selectedFormat() {
    return dialog.querySelector('input[name="export-format"]:checked').value;
  }

  let dialogContext = null;

  function refreshRelatedLabels() {
    const ext = selectedFormat();
    for (const [kind, ref] of relatedRefs) {
      ref.text.nodeValue = ` Include ${kind}.${ext}`;
    }
  }

  function refreshRelatedEnabled() {
    if (!dialogContext) return;
    const {lastSearch, reviewsCount, photosCount} = dialogContext;
    const reviewsAllowed = !!lastSearch?.include_reviews;
    const photosAllowed = !!lastSearch?.include_photos;
    setRelatedState('reviews', reviewsAllowed, reviewsCount);
    setRelatedState('photos', photosAllowed, photosCount);
  }

  function setRelatedState(kind, allowed, count) {
    const ref = relatedRefs.get(kind);
    const disabled = !allowed || count === 0;
    ref.input.disabled = disabled;
    if (disabled) ref.input.checked = false;
    if (!allowed) {
      ref.wrap.title = `Re-run search with "Include ${kind}" checked to enable`;
    } else if (count === 0) {
      ref.wrap.title = `No ${kind} loaded for visible places`;
    } else {
      ref.wrap.title = '';
    }
  }

  function updateDownloadEnabled() {
    if (!dialogContext) return;
    const {visible, reviewsCount, photosCount} = dialogContext;
    const anyColumn = Array.from(checkboxes.values()).some((cb) => cb.checked);
    const placesFile = anyColumn && visible > 0;
    const reviewsFile =
        relatedRefs.get('reviews').input.checked && reviewsCount > 0;
    const photosFile =
        relatedRefs.get('photos').input.checked && photosCount > 0;
    downloadBtn.disabled = !(placesFile || reviewsFile || photosFile);
    if (downloadBtn.disabled) {
      downloadBtn.title = visible === 0 ?
          'Nothing to export' :
          'Pick at least one column or related file';
    } else {
      downloadBtn.title = '';
    }
  }

  for (const cb of checkboxes.values()) {
    cb.addEventListener('change', updateDownloadEnabled);
  }
  for (const ref of relatedRefs.values()) {
    ref.input.addEventListener('change', updateDownloadEnabled);
  }
  for (const r of formatRadios) {
    r.addEventListener('change', refreshRelatedLabels);
  }

  dialog.querySelector('[data-action="select-all"]')
      .addEventListener('click', () => {
        for (const cb of checkboxes.values()) cb.checked = true;
        updateDownloadEnabled();
      });
  dialog.querySelector('[data-action="select-none"]')
      .addEventListener('click', () => {
        for (const cb of checkboxes.values()) cb.checked = false;
        updateDownloadEnabled();
      });
  dialog.querySelector('[data-action="match-table"]')
      .addEventListener('click', () => {
        for (const [id, cb] of checkboxes) cb.checked = TABLE_VIEW_IDS.has(id);
        updateDownloadEnabled();
      });

  cancelBtn.addEventListener('click', () => dialog.close());

  downloadBtn.addEventListener('click', async () => {
    if (!dialogContext) return;
    const {entries} = dialogContext;
    const selectedCols = COLUMNS.filter((c) => checkboxes.get(c.id).checked);
    const includeReviews = relatedRefs.get('reviews').input.checked &&
        !relatedRefs.get('reviews').input.disabled;
    const includePhotos = relatedRefs.get('photos').input.checked &&
        !relatedRefs.get('photos').input.disabled;
    const wantPlaces = selectedCols.length > 0 && entries.length > 0;
    if (!wantPlaces && !includeReviews && !includePhotos) return;
    const format = selectedFormat();
    dialog.close();
    await runExport({
      entries,
      selectedCols,
      format,
      includeReviews,
      includePhotos,
      getLastSearch,
      btn,
      status,
    });
  });

  btn.addEventListener('click', () => {
    const entries = getVisibleEntries();
    const lastSearch = getLastSearch();
    const reviewsCount = countNested(entries, 'reviews');
    const photosCount = countNested(entries, 'photos');
    dialogContext = {
      entries,
      lastSearch,
      visible: entries.length,
      reviewsCount,
      photosCount,
    };
    countLabel.textContent =
        `Exporting ${entries.length} place${entries.length === 1 ? '' : 's'}, ${
            reviewsCount} review${reviewsCount === 1 ? '' : 's'}, ${
            photosCount} photo${photosCount === 1 ? '' : 's'}`;
    refreshRelatedLabels();
    refreshRelatedEnabled();
    updateDownloadEnabled();
    dialog.showModal();
  });
}


async function runExport({
  entries,
  selectedCols,
  format,
  includeReviews,
  includePhotos,
  getLastSearch,
  btn,
  status
}) {
  btn.disabled = true;
  const lastSearch = getLastSearch();
  const ts = formatTimestamp(new Date());
  const typePart = buildTypePart(lastSearch);
  const ext = format === 'xlsx' ? 'xlsx' : 'csv';

  try {
    const datasets = buildDatasets({
      entries,
      selectedCols,
      includeReviews,
      includePhotos,
      typePart,
      ts,
      ext,
    });
    if (datasets.length === 0) return;

    const needsXLSX = format === 'xlsx';
    const needsZip = datasets.length > 1;
    if (needsXLSX || needsZip) status.textContent = 'Loading libraries…';
    const [XLSX, JSZip] = await Promise.all([
      needsXLSX ? loadScript(XLSX_CDN_URL, 'XLSX') : Promise.resolve(null),
      needsZip ? loadScript(JSZIP_CDN_URL, 'JSZip') : Promise.resolve(null),
    ]);

    status.textContent = needsZip ? 'Building archive…' : 'Building file…';
    await yieldToUI();

    const files = datasets.map(
        (ds) => ({
          filename: ds.filename,
          blob: format === 'csv' ?
              buildCSVBlob(ds.headers, ds.rows) :
              buildXLSXBlob(XLSX, ds.sheetName, ds.headers, ds.rows),
        }));

    let finalText;
    if (needsZip) {
      const zip = new JSZip();
      for (const f of files) zip.file(f.filename, f.blob);
      const zipBlob = await zip.generateAsync({type: 'blob'});
      const zipName = `places-${typePart}-${ts}.zip`;
      triggerDownload(zipBlob, zipName);
      finalText = `Saved ${zipName} (${files.length} files)`;
    } else {
      triggerDownload(files[0].blob, files[0].filename);
      finalText = `Saved ${files[0].filename}`;
    }
    status.textContent = finalText;
    setTimeout(() => {
      if (status.textContent === finalText) status.textContent = '';
    }, 4000);
  } catch (err) {
    status.textContent = `Export failed: ${err.message}`;
    console.error(err);
  } finally {
    btn.disabled = false;
  }
}


function buildDatasets(
    {entries, selectedCols, includeReviews, includePhotos, typePart, ts, ext}) {
  const datasets = [];
  if (selectedCols.length > 0 && entries.length > 0) {
    const headers = [];
    const getters = [];
    for (const col of selectedCols) {
      for (const c of col.columns) {
        headers.push(c.header);
        getters.push(c.get);
      }
    }
    datasets.push({
      sheetName: 'Places',
      filename: `places-${typePart}-${ts}.${ext}`,
      headers,
      rows: entries.map((e) => getters.map((g) => normalizeCell(g(e)))),
    });
  }
  if (includeReviews) {
    datasets.push({
      sheetName: 'Reviews',
      filename: `reviews-${typePart}-${ts}.${ext}`,
      // headers: REVIEW_FIELDS.map((f) => f.header),
      headers: ['place_id', ...REVIEW_FIELDS],
      rows: gatherNestedRows(entries, REVIEW_FIELDS, 'reviews'),
    });
  }
  if (includePhotos) {
    datasets.push({
      sheetName: 'Photos',
      filename: `photos-${typePart}-${ts}.${ext}`,
      // headers: PHOTO_FIELDS.map((f) => f.header),
      headers: ['place_id', ...PHOTO_FIELDS],
      rows: gatherNestedRows(entries, PHOTO_FIELDS, 'photos'),
    });
  }
  return datasets;
}


function gatherNestedRows(entries, fields, arrayKey) {
  const rows = [];
  for (const e of entries) {
    const items = e[arrayKey];
    if (!items?.length) continue;
    const placeId = normalizeCell(e.place.place_id);
    for (const item of items) {
      const row = [placeId];
      for (const f of fields) row.push(normalizeCell(item[f]));
      rows.push(row);
    }
    // for (const item of items) {
    //   rows.push(fields.map((f) => normalizeCell(f.get(e, item))));
    // }
  }
  return rows;
}


function buildXLSXBlob(XLSX, sheetName, headers, rows) {
  const ws = XLSX.utils.aoa_to_sheet([headers, ...rows]);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, sheetName);
  const buf = XLSX.write(wb, {bookType: 'xlsx', type: 'array'});
  return new Blob([buf], {type: 'application/octet-stream'});
}


function buildTypePart(lastSearch) {
  const raw = lastSearch?.typeLabel || lastSearch?.main_type || 'unknown';
  return sanitizeForFilename(raw) || 'unknown';
}


function countNested(entries, arrayKey) {
  let n = 0;
  for (const e of entries) n += e[arrayKey]?.length ?? 0;
  return n;
}


function normalizeCell(v) {  // NOSONAR
  if (v == null) return '';
  if (typeof v === 'number' || typeof v === 'string') return v;
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  return String(v);
}


function yieldToUI() {
  return new Promise((r) => setTimeout(r, 0));
}


function buildCSVBlob(headers, rows) {
  const sep = ';';
  const lines = [`sep=${sep}`];
  lines.push(rowToCSV(headers, sep));
  for (const row of rows) lines.push(rowToCSV(row, sep));
  // BOM so Excel auto-detects UTF-8.
  const text = '﻿' + lines.join('\r\n') + '\r\n';
  return new Blob([text], {type: 'text/csv;charset=utf-8;'});
}


function rowToCSV(cells, sep) {
  return cells.map((c) => escapeCSVCell(c, sep)).join(sep);
}


function escapeCSVCell(v, sep) {
  const s = typeof v === 'string' ? v : String(v);
  if (s === '') return '';
  if (s.includes(sep) || s.includes('"') || s.includes('\r') ||
      s.includes('\n')) {
    return '"' + s.replaceAll('"', '""') + '"';
  }
  return s;
}


function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}


const scriptLoadPromises = new Map();
function loadScript(url, globalName) {
  if (globalThis[globalName]) {
    return Promise.resolve(globalThis[globalName]);
  }
  const cached = scriptLoadPromises.get(globalName);
  if (cached) {
    return cached;
  }
  const promise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = url;
    s.onload = () => {
      if (globalThis[globalName]) {
        resolve(globalThis[globalName]);
      } else {
        reject(new Error(`${globalName} library loaded but global ${
            globalName} is missing`));
      }
    };
    s.onerror = () => {
      scriptLoadPromises.delete(globalName);
      reject(new Error(`Failed to load ${globalName} library from CDN`));
    };
    document.head.appendChild(s);
  });
  scriptLoadPromises.set(globalName, promise);
  return promise;
}


function sanitizeForFilename(s) {
  return s.toLowerCase()
      .replace(/[\s_]+/g, '-')
      .replace(/[^a-z0-9-]/g, '')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '');
}


function formatTimestamp(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
      `-${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
}


function buildDialog() {
  const dialog = document.createElement('dialog');
  dialog.id = 'export-dialog';
  dialog.className = 'export-dialog';

  const h = document.createElement('h3');
  h.textContent = 'Download search results';
  dialog.appendChild(h);

  dialog.appendChild(buildFormatFieldset());
  dialog.appendChild(buildColumnsFieldset());
  dialog.appendChild(buildRelatedFieldset());

  const count = document.createElement('div');
  count.className = 'export-count';
  dialog.appendChild(count);

  dialog.appendChild(buildDialogButtons());
  return dialog;
}


function buildFormatFieldset() {
  const fs = document.createElement('fieldset');
  const legend = document.createElement('legend');
  legend.textContent = 'Format';
  fs.appendChild(legend);
  for (const [value, label, checked] of [
           ['xlsx', 'XLSX', true], ['csv', 'CSV', false]]) {
    const wrap = document.createElement('label');
    wrap.className = 'format-option';
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'export-format';
    input.value = value;
    if (checked) input.checked = true;
    wrap.appendChild(input);
    wrap.appendChild(document.createTextNode(' ' + label));
    fs.appendChild(wrap);
  }
  return fs;
}


function buildColumnsFieldset() {
  const fs = document.createElement('fieldset');
  const legend = document.createElement('legend');
  legend.textContent = 'Columns';
  fs.appendChild(legend);

  const grid = document.createElement('div');
  grid.className = 'columns-grid';
  for (const col of COLUMNS) {
    const wrap = document.createElement('label');
    wrap.className = 'column-option';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.dataset.colId = col.id;
    if (col.defaultChecked) input.checked = true;
    wrap.appendChild(input);
    wrap.appendChild(document.createTextNode(' ' + col.label));
    grid.appendChild(wrap);
  }
  fs.appendChild(grid);

  const actions = document.createElement('div');
  actions.className = 'quick-actions';
  for (const [action, label] of [
           ['select-all', 'Select all'],
           ['select-none', 'None'],
           ['match-table', 'Match table view'],
  ]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.action = action;
    b.textContent = label;
    actions.appendChild(b);
  }
  fs.appendChild(actions);
  return fs;
}


function buildRelatedFieldset() {
  const fs = document.createElement('fieldset');
  fs.className = 'related-fieldset';
  const legend = document.createElement('legend');
  legend.textContent = 'Related data';
  fs.appendChild(legend);
  for (const kind of ['reviews', 'photos']) {
    const wrap = document.createElement('label');
    wrap.className = 'related-option';
    wrap.dataset.relatedKind = kind;
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.dataset.related = kind;
    wrap.appendChild(input);
    const text = document.createTextNode(` Include ${kind}.csv`);
    wrap.appendChild(text);
    fs.appendChild(wrap);
  }
  return fs;
}


function buildDialogButtons() {
  const row = document.createElement('div');
  row.className = 'dialog-buttons';
  for (const [action, label] of [
           ['cancel', 'Cancel'], ['download', 'Download']]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.action = action;
    b.textContent = label;
    row.appendChild(b);
  }
  return row;
}
