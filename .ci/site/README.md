# Results site

The root page of the results site: a React app that reads the JSON the
nightly and bench publishers put on `gh-pages` and shows it as a summary with
selectable detail. What the site is and how it is published is in
[amd-ci.md](../docs/amd-ci.md#reading-the-site).

The stack follows the rocjitsu dashboard of
[ROCm/rocm-systems](https://github.com/ROCm/rocm-systems/tree/develop/emulation/rocjitsu/website/dashboard):
Vite, React, MUI and ECharts, with relative URLs so the site works from any
sub-path.

## Views

| View | Shows | Chosen by |
| --- | --- | --- |
| Overview | The latest night in numbers, every branch that night, and each branch night by night. | |
| Nightly runs | The jobs, tests and checks of one night, or the grid of every check by night. | Branch, night, failures only |
| Trends | Job and test durations, wheel sizes, transfer rates. | Metric, branch, up to eight series |
| Benchmarks | The headline nixlbench run: lanes, cases needing attention, declared ratios. | Branch, lane, status, kind |
| Wheels | The wheels a night built and the pip command for a published branch. | Branch, night |

A choice is kept in the address (`#/nightly?branch=...&run=...`).

## Layout

```text
src/data/      what the JSON means: nightly.js, bench.js, format.js (unit tested), load.js
src/state.js   the address fragment and the colour slots of chosen series
src/views/     one file per view
src/components shared pieces, the status grid, the chart
```

`src/data/nightly.js` follows the rules of
[`amd_results_site.py`](../scripts/amd_results_site.py), which renders the same
summaries as `classic.html`; a change to what a summary means belongs in both.

## Working on it

Node 22 or later.

```bash
cd .ci/site
npm ci
npm run lint && npm test
npm run build
```

The app has no data of its own. To see it with the published data, point the
dev or preview server at a checkout of `gh-pages`:

```bash
git worktree add /tmp/nixl-pages gh-pages
SITE_DATA_DIR=/tmp/nixl-pages npm run dev
```
