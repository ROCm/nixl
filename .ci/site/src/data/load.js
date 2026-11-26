// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Everything is fetched from the site the page is served from: the nightly
// and bench publishers write the JSON next to this page on gh-pages.

export const NIGHTS = 30;

export function siteUrl(path) {
  return new URL(path, document.baseURI).href;
}

async function json(path, { optional = false, mutable = false } = {}) {
  const response = await fetch(siteUrl(path), { cache: mutable ? 'no-store' : 'default' });
  if (response.status === 404 && optional) return null;
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

export async function loadSite({ nights = NIGHTS } = {}) {
  const [index, latest, manifest, wheels] = await Promise.all([
    json('data/nightly/index.json', { mutable: true }),
    json('data/bench/v1/latest.json', { optional: true, mutable: true }).catch(() => null),
    json('data/bench/v1/manifest.json', { optional: true, mutable: true }).catch(() => null),
    json('wheels/index.json', { optional: true, mutable: true }).catch(() => null),
  ]);
  const stamps = (Array.isArray(index.runs) ? index.runs : [])
    .slice(0, nights)
    .map((run) => run.stamp)
    .reverse();
  const runs = await Promise.all(
    stamps.map((stamp) =>
      json(`data/nightly/${stamp}/summary.json`).then(
        (summary) => ({ stamp, summary }),
        () => ({ stamp, summary: null }),
      ),
    ),
  );
  return { runs, bench: latest ? { latest, manifest } : null, wheels, loadedAt: new Date() };
}
