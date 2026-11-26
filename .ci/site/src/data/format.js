// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

export function number(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// "20261125T033057Z" -> Date, or null.
export function stampDate(stamp) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(stamp || '');
  if (!m) return null;
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
}

export function when(stamp) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/.exec(stamp || '');
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]} UTC` : String(stamp || '');
}

export function day(stamp) {
  return /^\d{8}T/.test(stamp || '') ? `${stamp.slice(4, 6)}-${stamp.slice(6, 8)}` : String(stamp || '');
}

export function seconds(value) {
  const v = number(value);
  if (v === null) return '';
  if (v >= 3600) return `${Math.floor(v / 3600)}h ${Math.round((v % 3600) / 60)}m`;
  if (v >= 60) return `${Math.floor(v / 60)}m ${Math.round(v % 60)}s`;
  if (v >= 10) return `${v.toFixed(0)}s`;
  if (v >= 1) return `${v.toFixed(1)}s`;
  return `${(v * 1000).toFixed(0)}ms`;
}

export function bytes(value) {
  let v = number(value);
  if (v === null) return '';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let unit = 0;
  while (v >= 1024 && unit < units.length - 1) {
    v /= 1024;
    unit += 1;
  }
  return `${unit && v < 100 ? v.toFixed(1) : v.toFixed(0)} ${units[unit]}`;
}

export function rate(value) {
  const text = bytes(value);
  return text ? `${text}/s` : '';
}

export function shortSha(sha) {
  return sha ? String(sha).slice(0, 10) : '';
}

export function ago(date, now = new Date()) {
  if (!date) return '';
  const hours = (now - date) / 3600000;
  if (hours < 1) return 'under an hour ago';
  if (hours < 48) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)} days ago`;
}
