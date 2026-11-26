// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { createTheme } from '@mui/material/styles';

const INK = {
  light: {
    page: '#f9f9f7',
    surface: '#fcfcfb',
    primary: '#0b0b0b',
    secondary: '#52514e',
    muted: '#898781',
    grid: '#e1e0d9',
    axis: '#c3c2b7',
    border: 'rgba(11,11,11,0.10)',
    link: '#256abf',
  },
  dark: {
    page: '#0d0d0d',
    surface: '#1a1a19',
    primary: '#f0efec',
    secondary: '#c3c2b7',
    muted: '#898781',
    grid: '#2c2c2a',
    axis: '#383835',
    border: 'rgba(255,255,255,0.10)',
    link: '#6da7ec',
  },
};

// Eight categorical slots, in a fixed order that keeps neighbours apart for
// colour-blind readers. A ninth series is not given a colour.
const SERIES = {
  light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'],
  dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#0ca30c', '#9085e9', '#e66767'],
};
export const SERIES_SLOTS = 8;

// Status colours are reserved for state and always sit beside a glyph or word.
const STATUS = {
  pass: { fill: '#0ca30c', text: '#ffffff', glyph: '✓', word: 'pass' },
  fail: { fill: '#d03b3b', text: '#ffffff', glyph: '✗', word: 'fail' },
  crash: { fill: '#d03b3b', text: '#ffffff', glyph: '!', word: 'crash' },
  timeout: { fill: '#fab219', text: '#0b0b0b', glyph: '◷', word: 'timeout' },
  skip: { fill: null, text: null, glyph: '–', word: 'skip' },
  blocked: { fill: null, text: null, glyph: '⊘', word: 'blocked' },
  'not-run': { fill: null, text: null, glyph: '·', word: 'not run' },
  none: { fill: null, text: null, glyph: '·', word: 'not run' },
};

export function statusStyle(status, mode) {
  const entry = STATUS[status] || STATUS.none;
  const ink = INK[mode];
  return {
    glyph: entry.glyph,
    word: entry.word,
    fill: entry.fill || (status === 'none' || status === 'not-run' ? 'transparent' : ink.grid),
    text: entry.text || ink.secondary,
    outlined: !entry.fill,
  };
}

export function chartInk(mode) {
  return { ...INK[mode], series: SERIES[mode] };
}

export function buildTheme(mode) {
  const ink = INK[mode];
  return createTheme({
    palette: {
      mode,
      primary: { main: ink.link },
      background: { default: ink.page, paper: ink.surface },
      text: { primary: ink.primary, secondary: ink.secondary, disabled: ink.muted },
      divider: ink.border,
    },
    shape: { borderRadius: 8 },
    typography: {
      fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
      h1: { fontSize: '1.35rem', fontWeight: 650 },
      h2: { fontSize: '1.05rem', fontWeight: 650 },
      h3: { fontSize: '0.95rem', fontWeight: 650 },
      body2: { fontSize: '0.875rem' },
    },
    components: {
      MuiPaper: { defaultProps: { elevation: 0, variant: 'outlined' } },
      MuiTableCell: { styleOverrides: { root: { borderColor: ink.grid }, head: { fontWeight: 650, color: ink.secondary } } },
      MuiTab: { styleOverrides: { root: { textTransform: 'none', minHeight: 44, fontWeight: 600 } } },
      MuiButton: { styleOverrides: { root: { textTransform: 'none' } } },
      MuiToggleButton: { styleOverrides: { root: { textTransform: 'none' } } },
      MuiTextField: { defaultProps: { size: 'small' } },
    },
  });
}
