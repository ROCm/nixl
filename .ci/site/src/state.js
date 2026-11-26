// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// The view and its selections live in the URL fragment ("#/nightly?branch=x"),
// so a selection can be linked to and the back button works.

import { useCallback, useEffect, useState } from 'react';
import { SERIES_SLOTS } from './theme';

export const VIEWS = [
  { id: 'overview', label: 'Overview' },
  { id: 'nightly', label: 'Nightly runs' },
  { id: 'trends', label: 'Trends' },
  { id: 'bench', label: 'Benchmarks' },
  { id: 'wheels', label: 'Wheels' },
];

export function parseHash(hash) {
  const text = String(hash || '').replace(/^#\/?/, '');
  const [view, query = ''] = text.split('?');
  const params = {};
  new URLSearchParams(query).forEach((value, key) => {
    params[key] = value;
  });
  return { view: VIEWS.some((entry) => entry.id === view) ? view : 'overview', params };
}

export function formatHash({ view, params }) {
  const query = new URLSearchParams();
  Object.keys(params || {})
    .sort()
    .forEach((key) => {
      const value = params[key];
      if (value !== undefined && value !== null && value !== '') query.set(key, value);
    });
  const text = query.toString();
  return `#/${view}${text ? `?${text}` : ''}`;
}

export function useHashState() {
  const [state, setState] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setState(parseHash(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  // A change of view is a history entry; a change of selection replaces it.
  const go = useCallback((view, params = {}) => {
    window.location.hash = formatHash({ view, params });
  }, []);
  const patch = useCallback((changes) => {
    const current = parseHash(window.location.hash);
    const next = { view: current.view, params: { ...current.params, ...changes } };
    window.history.replaceState(null, '', formatHash(next));
    setState(parseHash(window.location.hash));
  }, []);
  return { view: state.view, params: state.params, go, patch };
}

// Colour slots of the chosen series. A series keeps its slot while it stays
// chosen, so removing one never repaints the others; a new one takes the
// lowest free slot. At most SERIES_SLOTS are kept.
export function assignSlots(previous, names) {
  const kept = {};
  const used = new Set();
  names.slice(0, SERIES_SLOTS).forEach((name) => {
    const slot = previous[name];
    if (slot !== undefined && !used.has(slot)) {
      kept[name] = slot;
      used.add(slot);
    }
  });
  names.slice(0, SERIES_SLOTS).forEach((name) => {
    if (kept[name] !== undefined) return;
    let slot = 0;
    while (used.has(slot)) slot += 1;
    kept[name] = slot;
    used.add(slot);
  });
  return kept;
}
