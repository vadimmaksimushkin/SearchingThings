'use strict';

export const API_BASE = (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
  ? 'http://localhost:8000'
  : '/api';
export const SHOW_DEBUG = false;
export const LANG = 'en';
// Combining diacritical marks: U+0300 to U+036F (range chars below are invisible in source).
const COMBINING_DIACRITICS_RE = /[\u0300-\u036f]/g;
const NON_ALPHANUMERIC_RE = /[^a-z0-9]/g;

export function normalizeTypeInput(s) {
  return s
      .toLowerCase()
      .normalize('NFD')
      .replace(COMBINING_DIACRITICS_RE, '')
      .replace(NON_ALPHANUMERIC_RE, '');
}

export function searchMainType(input, mainTypes) {
  const target = normalizeTypeInput(input);
  if (!target) return null;
  for (const [key, labels] of Object.entries(mainTypes)) {
    if (normalizeTypeInput(key) === target) return key;
    for (const label of Object.values(labels)) {
      if (normalizeTypeInput(label) === target) return key;
    }
  }
  return null;
}
