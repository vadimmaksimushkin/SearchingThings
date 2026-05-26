'use strict';

export function buildPlaceRow(place) {
  const row = document.createElement('div');
  row.className = 'place-card';
  if (place.place_id) row.dataset.placeId = place.place_id;

  const typeCell = document.createElement('div');
  if (place.displayLabel) typeCell.textContent = place.displayLabel;
  row.appendChild(typeCell);

  const nameCell = document.createElement('div');
  nameCell.className = 'name-cell';
  const nameStrong = document.createElement('strong');
  nameStrong.textContent = place.name ?? '';
  nameCell.appendChild(nameStrong);
  row.appendChild(nameCell);

  const ratingCell = document.createElement('div');
  if (place.rating != null) {
    ratingCell.textContent = `★ ${place.rating.toFixed(1)}`;
  }
  row.appendChild(ratingCell);

  const ratingCountCell = document.createElement('div');
  if (place.rating_count != null) {
    ratingCountCell.textContent = String(place.rating_count);
  }
  row.appendChild(ratingCountCell);

  const websiteCell = document.createElement('div');
  if (place.website) {
    const a = document.createElement('a');
    a.href = place.website;
    a.textContent = place.website;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    websiteCell.appendChild(a);
  }
  row.appendChild(websiteCell);

  const emailsCell = document.createElement('div');
  if (place.emails?.length) {
    place.emails.forEach((email, i) => {
      if (i > 0) emailsCell.appendChild(document.createTextNode(', '));
      const a = document.createElement('a');
      a.href = `mailto:${email}`;
      a.textContent = email;
      emailsCell.appendChild(a);
    });
  }
  row.appendChild(emailsCell);

  const phoneCell = document.createElement('div');
  if (place.phone) {
    const a = document.createElement('a');
    a.href = `tel:${place.phone}`;
    a.textContent = place.phone;
    phoneCell.appendChild(a);
  }
  row.appendChild(phoneCell);

  const previewCell = document.createElement('div');
  if (place.preview_photo) {
    const img = document.createElement('img');
    img.src = place.preview_photo;
    img.alt = '';
    img.className = 'preview-photo';
    img.loading = 'lazy';
    img.decoding = 'async';
    previewCell.appendChild(img);
  }
  row.appendChild(previewCell);

  return row;
}

export function buildPlaceCard(place) {
  const card = document.createElement('div');

  if (place.displayLabel) {
    const label = document.createElement('div');
    label.className = 'place-label';
    label.textContent = place.displayLabel;
    card.appendChild(label);
  }

  if (place.preview_photo) {
    const img = document.createElement('img');
    img.src = place.preview_photo;
    img.alt = '';
    img.className = 'preview-photo';
    img.loading = 'lazy';
    img.decoding = 'async';
    card.appendChild(img);
  }

  const header = document.createElement('div');
  const nameStrong = document.createElement('strong');
  nameStrong.textContent = place.name ?? '';
  header.appendChild(nameStrong);
  if (place.rating != null) {
    header.appendChild(document.createTextNode(
        ` ★ ${place.rating.toFixed(1)} (${place.rating_count})`));
  }
  card.appendChild(header);

  if (place.website) {
    const row = document.createElement('div');
    const strong = document.createElement('strong');
    const a = document.createElement('a');
    a.href = place.website;
    a.textContent = place.website;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    strong.appendChild(a);
    row.appendChild(strong);
    card.appendChild(row);
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

  // appendReviewsSection(card, place.reviews);
  // appendPhotosSection(card, place.photos);
  return card;
}

function appendReviewsSection(card, reviews) {
  if (reviews?.length) {
    appendBoldRow(card, `Reviews (${reviews.length})`);
    for (const r of reviews) {
      const date = r.published_at ? r.published_at.slice(0, 10) : '';
      const star = r.rating != null ? `★ ${r.rating}` : '★ -';
      const author = r.author_name ?? 'anon';
      appendBoldRow(card, `${star} — ${author} (${date})`);
      appendTextRow(card, r.text);
    }
  }
}

function appendPhotosSection(card, photos) {
  if (photos?.length) {
    appendBoldRow(card, `Photos (${photos.length})`);
    for (const p of photos) {
      appendLinkRow(card, p.google_maps_uri, p.google_maps_uri, '_blank');
    }
  }
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
