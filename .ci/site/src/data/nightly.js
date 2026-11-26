// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// The nightly summaries (data/nightly/<stamp>/summary.json) turned into what
// the views show. The rules match amd_results_site.py, which renders the same
// data as classic.html.

import { number } from './format';

const UNSUPPORTED = 'this branch cannot build ROCm wheels yet';

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function list(value) {
  return Array.isArray(value) ? value.filter(isObject) : [];
}

// One entry per branch of a summary. "branches" may be a list or an object
// keyed by branch name, and the results may sit in the entry itself.
export function branchesOf(summary) {
  let entries = (summary && summary.branches) || [];
  if (isObject(entries)) {
    entries = Object.keys(entries).map((name) => ({ branch: name, ...entries[name] }));
  }
  return list(entries).map((entry) => {
    const results = isObject(entry.results) ? entry.results : entry;
    const jobs = list(results.jobs);
    // A failed plan job leaves "supported" false too; that is a failure.
    const planned = jobs.some((job) => job.name === 'plan' && job.result === 'success');
    const branch = entry.branch || results.ref || '?';
    return {
      branch,
      slug: entry.slug || branch,
      sha: entry.sha || results.sha || '',
      passed: entry.passed === true,
      reason: entry.reason || '',
      unsupported: results.supported === false && planned,
      version: results.version || '',
      runId: results.workflow_run_id || null,
      jobs,
      wheels: list(results.wheels),
      tests: list(results.tests),
      nixlbench: isObject(results.nixlbench) ? results.nixlbench : null,
    };
  });
}

export function nightStatus(data) {
  if (!data) return 'none';
  if (data.passed) return 'pass';
  return data.unsupported ? 'skip' : 'fail';
}

export function nightDetail(data) {
  if (!data || data.passed) return '';
  return data.reason || (data.unsupported ? UNSUPPORTED : '');
}

export function jobStatus(result) {
  if (result === 'success') return 'pass';
  if (result === 'skipped') return 'skip';
  return result ? 'fail' : 'none';
}

export function checkStatus(status) {
  return { PASS: 'pass', FAIL: 'fail', SKIP: 'skip' }[status] || 'none';
}

export function overall(checks) {
  const seen = new Set(list(checks).map((check) => checkStatus(check.status)));
  for (const status of ['fail', 'pass', 'skip']) {
    if (seen.has(status)) return status;
  }
  return 'none';
}

export function testKey(test) {
  return [test.job, test.rocm, test.label]
    .filter((part) => part !== undefined && part !== null && part !== '')
    .join(' · ');
}

export function testDuration(test) {
  return list(test.checks).reduce((total, check) => total + (number(check.duration_s) || 0), 0);
}

export function countBy(items, statusOf) {
  const counts = { pass: 0, fail: 0, skip: 0, none: 0 };
  items.forEach((item) => {
    counts[statusOf(item)] += 1;
  });
  return counts;
}

// runs: [{stamp, summary|null}], oldest first. Branch names, the ones of the
// newest run first, so that retired branches sink to the end.
export function branchNames(runs) {
  const names = [];
  [...runs].reverse().forEach((run) => {
    branchesOf(run.summary).forEach((entry) => {
      if (!names.includes(entry.branch)) names.push(entry.branch);
    });
  });
  return names;
}

export function branchHistory(runs, branch) {
  return runs.map((run) => ({
    stamp: run.stamp,
    data: branchesOf(run.summary).find((entry) => entry.branch === branch) || null,
  }));
}

// Names in the order they first appear, newest run first, so the rows follow
// the current list and retired ones sink to the bottom.
function orderedNames(history, namesOf) {
  const order = [];
  [...history].reverse().forEach((run) => {
    if (!run.data) return;
    namesOf(run.data).forEach((name) => {
      if (!order.includes(name)) order.push(name);
    });
  });
  return order;
}

function row(kind, label, history, pick, extra = {}) {
  return {
    kind,
    label,
    ...extra,
    cells: history.map((run) => {
      const found = run.data ? pick(run.data) : null;
      return { stamp: run.stamp, status: found ? found.status : 'none', detail: (found && found.detail) || '' };
    }),
  };
}

// The status grid of one branch: a row for the night, one per job, one per
// test, and under each test one per check ("parent" names the test row).
export function gridRows(history) {
  const rows = [
    row('night', 'Night', history, (data) => ({ status: nightStatus(data), detail: nightDetail(data) })),
  ];
  orderedNames(history, (data) => data.jobs.map((job) => job.name)).forEach((name) => {
    rows.push(
      row('job', name, history, (data) => {
        const job = data.jobs.find((entry) => entry.name === name);
        return job ? { status: jobStatus(job.result), detail: job.result } : null;
      }),
    );
  });
  orderedNames(history, (data) => data.tests.map(testKey)).forEach((key) => {
    const checksOf = (data) => {
      const test = data.tests.find((entry) => testKey(entry) === key);
      return test ? list(test.checks) : null;
    };
    rows.push(
      row('test', key, history, (data) => {
        const checks = checksOf(data);
        if (!checks) return null;
        const failed = checks.filter((check) => checkStatus(check.status) === 'fail');
        return {
          status: overall(checks),
          detail: failed.length ? `failed: ${failed.map((check) => check.name).join(', ')}` : `${checks.length} checks`,
        };
      }),
    );
    orderedNames(history, (data) => (checksOf(data) || []).map((check) => check.name)).forEach((name) => {
      rows.push(
        row(
          'check',
          name,
          history,
          (data) => {
            const check = (checksOf(data) || []).find((entry) => entry.name === name);
            return check ? { status: checkStatus(check.status), detail: check.detail || '' } : null;
          },
          { parent: key },
        ),
      );
    });
  });
  return rows;
}

export const METRICS = [
  { id: 'jobs', label: 'Job durations', unit: 'seconds' },
  { id: 'tests', label: 'Test durations', unit: 'seconds' },
  { id: 'wheels', label: 'Wheel sizes', unit: 'bytes' },
  { id: 'rates', label: 'Transfer rates', unit: 'rate' },
];

function collect(data, metric) {
  if (metric === 'jobs') {
    // A skipped job reports no time worth plotting.
    return data.jobs
      .filter((job) => jobStatus(job.result) !== 'skip')
      .map((job) => ({ name: job.name, value: job.duration_s, note: job.result }));
  }
  if (metric === 'tests') {
    return data.tests.map((test) => ({ name: testKey(test), value: testDuration(test) }));
  }
  if (metric === 'wheels') {
    return data.wheels.map((wheel) => ({
      name: `Python ${wheel.python || '?'}`,
      value: wheel.bytes,
      note: wheel.file,
    }));
  }
  const found = [];
  data.tests.forEach((test) => {
    list(test.checks).forEach((check) => {
      if (check.status === 'PASS' && number(check.bytes) !== null && number(check.duration_s) > 0) {
        found.push({
          name: `${testKey(test)} · ${check.name}`,
          value: check.bytes / check.duration_s,
          note: check.path || '',
        });
      }
    });
  });
  return found;
}

// [{name, points: [{stamp, value, note}]}], in the order the names first appear.
export function trendSeries(history, metric) {
  const series = new Map();
  history.forEach((run) => {
    if (!run.data) return;
    collect(run.data, metric).forEach((item) => {
      const value = number(item.value);
      if (value === null) return;
      if (!series.has(item.name)) series.set(item.name, []);
      series.get(item.name).push({ stamp: run.stamp, value, note: item.note || '' });
    });
  });
  return [...series].map(([name, points]) => ({ name, points }));
}

// What the top of the page says about the newest night.
export function latestNight(runs) {
  const latest = [...runs].reverse().find((run) => run.summary);
  if (!latest) return null;
  const branches = branchesOf(latest.summary);
  const counts = countBy(branches, nightStatus);
  return {
    stamp: latest.stamp,
    runId: latest.summary.run_id || null,
    repository: latest.summary.repository || '',
    upstreamSha: latest.summary.upstream_main_sha || '',
    sync: latest.summary.sync || '',
    branches,
    counts,
    failing: branches.filter((entry) => nightStatus(entry) === 'fail'),
  };
}
