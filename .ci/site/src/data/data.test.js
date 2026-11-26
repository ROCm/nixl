// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { bytes, day, seconds, stampDate, when } from './format';
import { branchHistory, branchNames, branchesOf, gridRows, latestNight, nightStatus, overall, testKey, trendSeries } from './nightly';
import { attentionTotal, benchBranches, laneRows, ratioRows, runHref, statusOrder } from './bench';
import { installCommand, installTarget, pythonVersion } from './wheels';
import { assignSlots, formatHash, parseHash } from '../state';

const check = (name, status, extra = {}) => ({ name, status, detail: '', duration_s: 1, bytes: null, path: null, ...extra });
const built = (version, tests) => ({
  schema: 1,
  ref: 'feat/wheels',
  version,
  supported: true,
  workflow_run_id: 7,
  jobs: [
    { name: 'plan', result: 'success', duration_s: 5 },
    { name: 'build', result: 'success', duration_s: 1800 },
  ],
  wheels: [{ file: 'a-cp310.whl', bytes: 1024, python: '3.10', rocm: ['10.1'] }],
  tests,
});
const unsupported = { ref: 'main', supported: false, jobs: [{ name: 'plan', result: 'success', duration_s: 4 }], wheels: [], tests: [] };
const planFailed = { ref: 'broken', supported: false, jobs: [{ name: 'plan', result: 'failure', duration_s: 4 }] };

const runs = [
  {
    stamp: '20261124T032953Z',
    summary: {
      repository: 'ROCm/nixl',
      branches: [
        { branch: 'feat/wheels', slug: 'feat-wheels', sha: 'a'.repeat(40), passed: true, results: built('1.0.dev1', [{ job: 'test', rocm: '10.1.0', checks: [check('import', 'PASS'), check('xfer', 'PASS', { bytes: 4096, duration_s: 2, path: 'ucx' })] }]) },
        { branch: 'gone', passed: true, results: built('1.0.dev1', []) },
      ],
    },
  },
  { stamp: '20261125T033057Z', summary: null },
  {
    stamp: '20261126T033000Z',
    summary: {
      repository: 'ROCm/nixl',
      upstream_main_sha: 'b'.repeat(40),
      branches: {
        main: { passed: false, reason: 'the branch cannot build ROCm wheels yet', results: unsupported },
        'feat/wheels': { slug: 'feat-wheels', passed: false, reason: 'test failed', results: built('1.0.dev2', [{ job: 'test', rocm: '10.1.0', checks: [check('import', 'PASS'), check('xfer', 'FAIL', { bytes: 4096, duration_s: 2 })] }, { job: 'rocjitsu', checks: [check('boot', 'SKIP')] }]) },
        broken: { passed: false, results: planFailed },
      },
    },
  },
];

describe('format', () => {
  it('reads a stamp', () => {
    expect(when('20261125T033057Z')).toBe('2026-11-25 03:30 UTC');
    expect(day('20261125T033057Z')).toBe('11-25');
    expect(stampDate('20261125T033057Z').toISOString()).toBe('2026-11-25T03:30:57.000Z');
    expect(stampDate('nonsense')).toBeNull();
  });
  it('scales units', () => {
    expect(seconds(0.0886)).toBe('89ms');
    expect(seconds(1887)).toBe('31m 27s');
    expect(seconds(null)).toBe('');
    expect(bytes(74950345)).toBe('71.5 MiB');
    expect(bytes(512)).toBe('512 B');
  });
});

describe('nightly', () => {
  it('reads branches as a list or an object and tells unsupported from failed', () => {
    const latest = branchesOf(runs[2].summary);
    expect(latest.map((entry) => [entry.branch, nightStatus(entry)])).toEqual([
      ['main', 'skip'],
      ['feat/wheels', 'fail'],
      ['broken', 'fail'],
    ]);
    expect(branchesOf(null)).toEqual([]);
    expect(nightStatus(branchesOf(runs[0].summary)[0])).toBe('pass');
  });

  it('orders branches by the newest night', () => {
    expect(branchNames(runs)).toEqual(['main', 'feat/wheels', 'broken', 'gone']);
  });

  it('keeps a night without a summary as a gap', () => {
    const history = branchHistory(runs, 'feat/wheels');
    expect(history.map((run) => Boolean(run.data))).toEqual([true, false, true]);
  });

  it('summarises checks', () => {
    expect(overall([check('a', 'PASS'), check('b', 'SKIP')])).toBe('pass');
    expect(overall([check('a', 'PASS'), check('b', 'FAIL')])).toBe('fail');
    expect(overall([])).toBe('none');
    expect(testKey({ job: 'test', rocm: '10.1.0', label: '' })).toBe('test · 10.1.0');
  });

  it('builds the grid with checks folded under their test', () => {
    const rows = gridRows(branchHistory(runs, 'feat/wheels'));
    expect(rows.map((row) => `${row.kind}:${row.label}`)).toEqual([
      'night:Night',
      'job:plan',
      'job:build',
      'test:test · 10.1.0',
      'check:import',
      'check:xfer',
      'test:rocjitsu',
      'check:boot',
    ]);
    const xfer = rows.find((row) => row.kind === 'check' && row.label === 'xfer');
    expect(xfer.parent).toBe('test · 10.1.0');
    expect(xfer.cells.map((cell) => cell.status)).toEqual(['pass', 'none', 'fail']);
    expect(rows[0].cells.map((cell) => cell.status)).toEqual(['pass', 'none', 'fail']);
    expect(rows[3].cells[2].detail).toBe('failed: xfer');
  });

  it('collects trends and leaves failed transfers out of the rates', () => {
    const history = branchHistory(runs, 'feat/wheels');
    const jobs = trendSeries(history, 'jobs');
    expect(jobs.map((entry) => entry.name)).toEqual(['plan', 'build']);
    expect(jobs[1].points.map((point) => point.value)).toEqual([1800, 1800]);
    const skipped = [{ stamp: 's', data: { jobs: [{ name: 'plan', result: 'success', duration_s: 4 }, { name: 'build', result: 'skipped', duration_s: 0 }], tests: [], wheels: [] } }];
    expect(trendSeries(skipped, 'jobs').map((entry) => entry.name)).toEqual(['plan']);
    const rates = trendSeries(history, 'rates');
    expect(rates).toEqual([{ name: 'test · 10.1.0 · xfer', points: [{ stamp: '20261124T032953Z', value: 2048, note: 'ucx' }] }]);
    expect(trendSeries(history, 'wheels')[0].name).toBe('Python 3.10');
    expect(trendSeries(history, 'tests')[0].points[0].value).toBe(3);
  });

  it('describes the latest night that has a summary', () => {
    const latest = latestNight(runs);
    expect(latest.stamp).toBe('20261126T033000Z');
    expect(latest.counts).toMatchObject({ pass: 0, fail: 2, skip: 1 });
    expect(latest.failing.map((entry) => entry.branch)).toEqual(['feat/wheels', 'broken']);
    expect(latestNight([{ stamp: 'x', summary: null }])).toBeNull();
  });
});

describe('bench', () => {
  const latest = {
    branches: {
      old: { branch: 'old', stamp: '20261120T000000Z', attention_total: 2, lanes: [], ratios: [] },
      'feat-wheels': {
        branch: 'feat/wheels',
        stamp: '20261125T033057Z',
        attention_total: 3,
        lanes: [
          { lane: 'vm1-loop', rocm_version: '10.2.0', n: [20, 0, 1, 0, 0, 0, 0], lane_blocked_by: [], started_at: '2026-11-25T04:00:00Z', finished_at: '2026-11-25T04:01:30Z', calib_gbps: 19.87, shard: 's.a1' },
          { lane: 'vm1-pcie', rocm_version: 'none', n: [0, 0, 0, 0, 0, 36, 0], lane_blocked_by: ['W10'] },
          { lane: 'bench-container', rocm_version: '10.1.0', n: [26, 0, 0, 0, 2, 0, 0], lane_blocked_by: [] },
        ],
        ratios: [
          { key: 'k1', kind: 'rc-tcp', lane: 'vm2-net', status: 'ok', points: [[65536, 4, 1.17, 13.5, 0.0867]] },
          { key: 'k2', kind: 'rc-tcp', lane: 'vm2-net', status: 'unavailable', why: 'den timeout', points: [] },
        ],
      },
    },
  };

  it('lists branches newest first and totals the attention', () => {
    expect(benchBranches(latest).map((entry) => entry.slug)).toEqual(['feat-wheels', 'old']);
    expect(attentionTotal(latest)).toBe(5);
    expect(benchBranches(null)).toEqual([]);
  });

  it('names lane counts by the manifest order and derives a state', () => {
    const statuses = statusOrder({ statuses: ['pass', 'fail', 'timeout', 'crash', 'skip', 'blocked', 'not-run'] });
    const lanes = laneRows(latest.branches['feat-wheels'], statuses);
    expect(lanes.map((lane) => lane.state)).toEqual(['attention', 'blocked', 'ok']);
    expect(lanes[0]).toMatchObject({ total: 21, durationS: 90, rocm: '10.2.0' });
    expect(lanes[0].counts.timeout).toBe(1);
    expect(lanes[1].rocm).toBe('');
    expect(statusOrder(null)).toHaveLength(7);
  });

  it('reads ratio points and links to the run page', () => {
    const ratios = ratioRows(latest.branches['feat-wheels']);
    expect(ratios[0].ok).toBe(true);
    expect(ratios[0].points[0]).toEqual({ block: 65536, batch: 4, num: 1.17, den: 13.5, value: 0.0867 });
    expect(ratios[1]).toMatchObject({ ok: false, why: 'den timeout' });
    expect(runHref('s.a1', 'failures')).toBe('bench/runs/s.a1.html#failures');
    expect(runHref('')).toBeNull();
  });
});

describe('state', () => {
  it('round-trips the fragment', () => {
    const state = parseHash('#/nightly?branch=for-upstream%2Ffeat&run=20261125T033057Z');
    expect(state).toEqual({ view: 'nightly', params: { branch: 'for-upstream/feat', run: '20261125T033057Z' } });
    expect(parseHash(formatHash(state))).toEqual(state);
    expect(parseHash('#/nowhere').view).toBe('overview');
    expect(formatHash({ view: 'trends', params: { metric: '' } })).toBe('#/trends');
  });

  it('never repaints a series that stays chosen', () => {
    const first = assignSlots({}, ['a', 'b', 'c']);
    expect(first).toEqual({ a: 0, b: 1, c: 2 });
    const second = assignSlots(first, ['a', 'c', 'd']);
    expect(second).toEqual({ a: 0, c: 2, d: 1 });
    const many = assignSlots({}, 'abcdefghij'.split(''));
    expect(Object.keys(many)).toHaveLength(8);
  });
});

describe('the wheels to install', () => {
  const night = (stamp, passed) => ({
    stamp,
    summary: { branches: [{ branch: 'feat/wheels', slug: 'feat-wheels', sha: 'abc', passed, results: built(`1.6.0.dev${stamp.slice(0, 8)}`, []) }] },
  });
  const nights = [night('20261124T033000Z', true), night('20261125T033000Z', false)];

  it('are those of the branch the publisher marks current', () => {
    const listing = {
      indexes: [
        { slug: 'old', current: false, version: '1.5.0.dev1' },
        { slug: 'feat-wheels', branch: 'feat/wheels', current: true, version: '1.6.0.dev20261124', sha: 'abc', date: '2026-11-24T04:12:40Z', python: ['cp310', 'cp39', 'cp314'] },
      ],
    };
    const target = installTarget(listing, nights);
    expect(target).toMatchObject({ slug: 'feat-wheels', branch: 'feat/wheels', version: '1.6.0.dev20261124', python: ['3.9', '3.10', '3.14'] });
    expect(target.built.toISOString()).toBe('2026-11-24T04:12:40.000Z');
  });

  it('are none when the publisher lists no current branch', () => {
    expect(installTarget({ indexes: [] }, nights)).toBeNull();
  });

  it('come from the last night a branch passed when nothing is listed', () => {
    expect(installTarget(null, nights)).toMatchObject({ slug: 'feat-wheels', version: '1.6.0.dev20261124', python: ['3.10'] });
    expect(installTarget(null, [nights[1]])).toBeNull();
  });

  it('are installed from the index of their branch', () => {
    expect(installCommand('https://x.test/wheels/feat-wheels/simple/')).toBe(
      'pip install --pre --index-url https://x.test/wheels/feat-wheels/simple/ --extra-index-url https://pypi.org/simple rocm-nixl',
    );
    expect(pythonVersion('cp312')).toBe('3.12');
    expect(pythonVersion('3.12')).toBe('3.12');
  });
});
