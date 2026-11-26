// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// The app in a browser, reading a site the publishers wrote. What a test
// expects is read from that site's JSON where it can be, so that the tests
// follow the fixture and fail only when the app and the data disagree.

import { expect, test as base } from '@playwright/test';

const BRANCH = 'for-upstream/feat/rocm-wheels';
const SLUG = 'for-upstream-feat-rocm-wheels';

// Every test fails on an uncaught error, a console error or a failed request.
const test = base.extend({
  page: async ({ page }, run) => {
    const problems = [];
    page.on('pageerror', (error) => problems.push(`uncaught: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') problems.push(`console: ${message.text()}`);
    });
    page.on('response', (response) => {
      if (response.status() >= 400) problems.push(`${response.status()}: ${response.url()}`);
    });
    page.allowed = (pattern) => {
      page.allowedProblems = pattern;
    };
    await run(page);
    const unexpected = problems.filter((problem) => !(page.allowedProblems && page.allowedProblems.test(problem)));
    expect(unexpected).toEqual([]);
  },
});

async function published(request, path) {
  const response = await request.get(path);
  expect(response.ok(), path).toBe(true);
  return response.json();
}

function tile(page, label) {
  return page.locator(`[data-tile="${label}"]`);
}

async function choose(page, label, option) {
  await page.getByRole('combobox', { name: label }).click();
  await page.getByRole('option', { name: option, exact: true }).click();
}

function hash(page) {
  return new URL(page.url()).hash;
}

test('every view renders', async ({ page }) => {
  const views = {
    overview: 'Latest night',
    nightly: 'Jobs',
    trends: 'Values',
    bench: 'Lanes',
    wheels: 'Install',
  };
  for (const [view, text] of Object.entries(views)) {
    await page.goto(`#/${view}`);
    await expect(page.getByRole('tab', { selected: true })).toBeVisible();
    await expect(page.getByText(text, { exact: true }).first()).toBeVisible();
  }
});

test('the overview summarises the latest night and opens a branch', async ({ page, request }) => {
  const index = await published(request, 'data/nightly/index.json');
  const latest = index.runs[0];
  const day = `${latest.stamp.slice(0, 4)}-${latest.stamp.slice(4, 6)}-${latest.stamp.slice(6, 8)}`;
  const buildable = latest.branches.filter((entry) => entry.supported !== false);
  const passing = buildable.filter((entry) => entry.passed);

  await page.goto('#/overview');
  await expect(tile(page, 'Latest night')).toContainText(day);
  await expect(tile(page, 'Branches passing')).toContainText(`${passing.length} of ${buildable.length}`);
  await expect(tile(page, 'Failing branches')).toContainText(String(buildable.length - passing.length));
  // One row per branch of that night, and one per branch ever seen in the grid.
  await expect(page.locator('main table').first().locator('tbody tr')).toHaveCount(latest.branches.length);
  const everSeen = new Set(index.runs.flatMap((run) => run.branches.map((entry) => entry.branch)));
  await expect(page.locator('main table').nth(1).locator('tbody tr')).toHaveCount(everSeen.size);

  await page.getByRole('button', { name: BRANCH }).first().click();
  await expect(page.getByRole('tab', { name: 'Nightly runs' })).toHaveAttribute('aria-selected', 'true');
  expect(hash(page)).toContain(encodeURIComponent(BRANCH));
  await expect(page.getByRole('combobox', { name: 'Branch' })).toHaveText(BRANCH);
});

test('a failed night shows its failed check, alone when asked', async ({ page, request }) => {
  const index = await published(request, 'data/nightly/index.json');
  const failed = index.runs.find((run) => run.branches.some((entry) => entry.branch === BRANCH && !entry.passed));
  expect(failed, 'the fixture has a night on which the branch fails').toBeTruthy();
  const summary = await published(request, `data/nightly/${failed.stamp}/summary.json`);
  const results = summary.branches.find((entry) => entry.branch === BRANCH).results;
  const failedJobs = results.jobs.filter((job) => !['success', 'skipped'].includes(job.result));
  const failedCheck = results.tests.flatMap((entry) => entry.checks).find((check) => check.status === 'FAIL');

  await page.goto(`#/nightly?branch=${encodeURIComponent(BRANCH)}&run=${failed.stamp}`);
  const jobs = page.locator('main table').first().locator('tbody tr');
  await expect(jobs).toHaveCount(results.jobs.length);
  // The failed test is open without being asked.
  await expect(page.getByText(failedCheck.detail)).toBeVisible();

  await page.getByRole('switch', { name: 'Failures only' }).check();
  expect(hash(page)).toContain('failures=1');
  await expect(jobs).toHaveCount(failedJobs.length);
  await expect(jobs.first()).toContainText(failedJobs[0].name);
  await expect(page.getByRole('heading', { name: 'Tests (1)' })).toBeVisible();

  await page.getByRole('button', { name: 'History' }).click();
  expect(hash(page)).toContain('show=history');
  await expect(page.locator('main table thead th[scope=col]')).toHaveCount(index.runs.length);
  // The failed night is a failed cell in the row of the night.
  await expect(page.locator('main table tbody tr').first().getByLabel('fail')).toHaveCount(1);
});

test('a selection survives a reload', async ({ page }) => {
  await page.goto(`#/nightly?branch=${encodeURIComponent(BRANCH)}&show=history`);
  await expect(page.getByRole('button', { name: 'History' })).toHaveAttribute('aria-pressed', 'true');
  await page.reload();
  await expect(page.getByRole('button', { name: 'History' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('combobox', { name: 'Branch' })).toHaveText(BRANCH);
});

test('trends plot the chosen metric', async ({ page, request }) => {
  const index = await published(request, 'data/nightly/index.json');
  await page.goto('#/trends');
  // The branch with the most to plot is the one shown first.
  await expect(page.getByRole('combobox', { name: 'Branch' })).toHaveText(BRANCH);
  await expect(page.getByRole('img', { name: /Job durations/ }).locator('canvas')).toBeVisible();
  const header = page.locator('main table thead th');
  await expect(header).toHaveCount(index.runs.length + 1);

  await choose(page, 'Metric', 'Transfer rates');
  expect(hash(page)).toContain('metric=rates');
  await expect(page.getByRole('heading', { name: `Transfer rates: ${BRANCH}` })).toBeVisible();
  const rows = page.locator('main table tbody tr');
  await expect(rows).toHaveCount(1);
  // The night the transfer failed has no rate.
  await expect(rows.first().locator('td').filter({ hasText: '/s' })).toHaveCount(index.runs.length - 1);
});

test('the benchmarks follow latest.json and filter by lane', async ({ page, request }) => {
  const latest = await published(request, 'data/bench/v1/latest.json');
  const branch = latest.branches[SLUG];
  const lane = branch.attention[0].lane;
  const inLane = branch.attention.filter((entry) => entry.lane === lane).length;
  expect(inLane).toBeLessThan(branch.attention.length);

  await page.goto('#/bench');
  await expect(tile(page, 'Cases passed')).toContainText(String(branch.totals.pass));
  await expect(tile(page, 'Cases blocked')).toContainText(String(branch.totals.blocked));
  await expect(tile(page, 'Tests')).toContainText(`${branch.tests.pass} passed`);
  await expect(page.locator('main table').first().locator('tbody tr')).toHaveCount(branch.lanes.length);
  await expect(page.getByRole('heading', { name: `Cases needing attention (${branch.attention.length} of ${branch.attention_total})` })).toBeVisible();
  await expect(page.getByRole('heading', { name: `Declared ratios (${branch.ratios.length} of ${branch.ratios.length})` })).toBeVisible();

  await choose(page, 'Lane', lane);
  expect(hash(page)).toContain(`lane=${lane}`);
  await expect(page.getByRole('heading', { name: `Cases needing attention (${inLane} of ${branch.attention_total})` })).toBeVisible();

  // The run page the bench publisher wrote is where the links lead.
  const href = await page.getByRole('link', { name: 'Run page' }).getAttribute('href');
  expect((await request.get(href)).ok()).toBe(true);
});

test('the overview says how to install the published wheels', async ({ page, request }) => {
  const listing = await published(request, 'wheels/index.json');
  const current = listing.indexes.find((entry) => entry.current);
  expect(current.slug).toBe(SLUG);
  expect(current.project).toBe('nixl-rocm');

  await page.goto('#/overview');
  const command = page.locator('[data-install="command"]');
  await expect(command).toHaveText(new RegExp(`^pip install --pre --index-url \\S+/wheels/${SLUG}/simple/ --extra-index-url https://pypi.org/simple ${current.project}$`));
  await expect(page.locator('[data-install="summary"]')).toContainText(`${current.project} ${current.version}`);
  await expect(page.locator('[data-install="summary"]')).toContainText(current.date.slice(0, 10));
  // The index the command names is one the publisher wrote.
  const index = (await command.textContent()).split(' ')[4];
  expect((await request.get(`${index}${current.project}/`)).ok()).toBe(true);

  await page.getByRole('button', { name: 'Wheel files and checksums' }).click();
  expect(hash(page)).toContain('#/wheels');
  await expect(page.getByRole('combobox', { name: 'Branch' })).toHaveText(BRANCH);
});

test('the wheels of a night are listed', async ({ page, request }) => {
  const index = await published(request, 'data/nightly/index.json');
  const summary = await published(request, `data/nightly/${index.runs[0].stamp}/summary.json`);
  const results = summary.branches.find((entry) => entry.branch === BRANCH).results;
  await page.goto('#/wheels');
  // The wheels of the branch and the nixlbench wheel.
  await expect(page.locator('main table tbody tr')).toHaveCount(results.wheels.length + 1);
  await expect(page.getByText(results.wheels[0].file)).toBeVisible();
  await expect(page.getByText(`wheels/${SLUG}/simple/`)).toBeVisible();
});

test('the theme can be changed and is remembered', async ({ page }) => {
  await page.goto('#/overview');
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const light = await background();
  await page.getByRole('button', { name: 'Dark theme' }).click();
  await expect(page.getByRole('button', { name: 'Light theme' })).toBeVisible();
  expect(await background()).not.toBe(light);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Light theme' })).toBeVisible();
});

test('the classic page is there and linked', async ({ page, request }) => {
  await page.goto('#/overview');
  const href = await page.getByRole('link', { name: 'Classic page' }).getAttribute('href');
  const classic = await request.get(href);
  expect(classic.ok()).toBe(true);
  expect(await classic.text()).toContain('NIXL on ROCm: nightly results');
});

test('without the nightly index the page says so and offers the classic page', async ({ page }) => {
  page.allowed(/data\/nightly\/index\.json|Failed to load resource/);
  await page.route('**/data/nightly/index.json', (route) => route.fulfill({ status: 500, body: 'no' }));
  await page.goto('#/overview');
  await expect(page.getByRole('alert')).toContainText('could not be loaded');
  await expect(page.getByRole('alert').getByRole('link', { name: 'The classic page' })).toBeVisible();
});

test('without bench data the benchmarks are left out', async ({ page }) => {
  page.allowed(/data\/bench\/v1\/|Failed to load resource/);
  await page.route('**/data/bench/v1/*.json', (route) => route.fulfill({ status: 404, body: 'not found' }));
  await page.goto('#/overview');
  await expect(tile(page, 'Latest night')).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Benchmarks' })).toHaveCount(0);
  await expect(tile(page, 'nixlbench cases needing attention')).toHaveCount(0);
});
