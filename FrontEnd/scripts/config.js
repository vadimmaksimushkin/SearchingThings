'use strict';

export const API_BASE = (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
  ? 'http://localhost:8000'
  : '/api';

export const SHOW_DEBUG = false;

const LANG = 'en';

const TYPE_ALIASES = {
  gym: ['gym', 'gimnasio'],
  shopping_mall: ['shoppingmall', 'centrocomercial'],
};
const TYPE_DISPLAY = {
  en: {gym: 'Gym', shopping_mall: 'Shopping Mall'},
  es: {gym: 'Gimnasio', shopping_mall: 'Centro Comercial'},
};

// Combining diacritical marks: U+0300 to U+036F (range chars below are invisible in source).
const COMBINING_DIACRITICS_RE = /[\u0300-\u036f]/g;
const N_TILDE_PLACEHOLDER = '__N_TILDE__';

function normalizeTypeInput(s) {
  return s
      .toLowerCase()
      .replaceAll('ñ', N_TILDE_PLACEHOLDER)
      .normalize('NFD')
      .replace(COMBINING_DIACRITICS_RE, '')
      .replaceAll(N_TILDE_PLACEHOLDER, 'ñ')
      .replace(/[\s/\\]/g, '');
}

export function canonicalType(input) {
  const n = normalizeTypeInput(input);
  for (const [type, aliases] of Object.entries(TYPE_ALIASES)) {
    if (aliases.includes(n)) return type;
  }
  return null;
}

export function displayType(type) {
  return TYPE_DISPLAY[LANG]?.[type] ?? type;
}
