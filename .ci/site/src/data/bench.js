// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// data/bench/v1/latest.json (per branch the headline bench run) turned into
// what the Benchmarks view shows. See .ci/docs/amd-bench.md.

export const DEFAULT_STATUSES = ['pass', 'fail', 'timeout', 'crash', 'skip', 'blocked', 'not-run'];

function list(value) {
  return Array.isArray(value) ? value : [];
}

export function benchBranches(latest) {
  const branches = (latest && latest.branches) || {};
  return Object.keys(branches)
    .map((slug) => ({ slug, ...branches[slug] }))
    .sort((a, b) => String(b.stamp || '').localeCompare(String(a.stamp || '')));
}

export function statusOrder(manifest) {
  const statuses = manifest && list(manifest.statuses);
  return statuses && statuses.length ? statuses : DEFAULT_STATUSES;
}

// The run page amd_bench_pages.py wrote for a shard.
export function runHref(shard, anchor) {
  if (!shard) return null;
  return `bench/runs/${encodeURIComponent(shard)}.html${anchor ? `#${anchor}` : ''}`;
}

export function laneState(lane) {
  if (list(lane.blockedBy).length) return 'blocked';
  if (lane.degraded) return 'degraded';
  const bad = (lane.counts.fail || 0) + (lane.counts.timeout || 0) + (lane.counts.crash || 0);
  if (bad) return 'attention';
  return lane.total ? 'ok' : 'empty';
}

export function laneRows(branch, statuses) {
  return list(branch && branch.lanes).map((lane) => {
    const counts = {};
    statuses.forEach((status, index) => {
      counts[status] = Number(list(lane.n)[index]) || 0;
    });
    const started = Date.parse(lane.started_at);
    const finished = Date.parse(lane.finished_at);
    const entry = {
      lane: lane.lane,
      rocm: lane.rocm_version && lane.rocm_version !== 'none' ? lane.rocm_version : '',
      env: lane.env_class || '',
      counts,
      total: statuses.reduce((sum, status) => sum + counts[status], 0),
      blockedBy: list(lane.lane_blocked_by),
      degraded: lane.degraded || '',
      calibGbps: typeof lane.calib_gbps === 'number' ? lane.calib_gbps : null,
      durationS: Number.isFinite(started) && Number.isFinite(finished) ? (finished - started) / 1000 : null,
      shard: lane.shard || '',
    };
    return { ...entry, state: laneState(entry) };
  });
}

// A point is [block, batch, numerator, denominator, ratio].
export function ratioRows(branch) {
  return list(branch && branch.ratios).map((ratio) => ({
    key: ratio.key,
    kind: ratio.kind || '',
    lane: ratio.lane || '',
    rocm: ratio.rocm_version && ratio.rocm_version !== 'none' ? ratio.rocm_version : '',
    num: ratio.num,
    den: ratio.den,
    ok: ratio.status === 'ok' && list(ratio.points).length > 0,
    why: ratio.why || '',
    shard: ratio.shard || '',
    points: list(ratio.points)
      .filter((point) => Array.isArray(point) && point.length === 5)
      .map(([block, batch, num, den, value]) => ({ block, batch, num, den, value })),
  }));
}

export function attentionRows(branch) {
  return list(branch && branch.attention).map((item) => ({
    caseKey: item.case_key,
    lane: item.lane || '',
    rocm: item.rocm_version && item.rocm_version !== 'none' ? item.rocm_version : '',
    status: item.status || '',
    reason: item.reason || '',
    required: item.required === true,
    excerpt: item.error_excerpt || '',
    shard: item.shard || '',
  }));
}

export function distinct(rows, field) {
  return [...new Set(rows.map((entry) => entry[field]).filter(Boolean))].sort();
}

// Cases needing attention across every branch's headline run.
export function attentionTotal(latest) {
  return benchBranches(latest).reduce((sum, branch) => sum + (Number(branch.attention_total) || 0), 0);
}
