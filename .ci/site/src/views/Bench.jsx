// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from 'react';
import { FormControlLabel, Link, Stack, Switch, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material';
import { seconds, when } from '../data/format';
import { attentionRows, benchBranches, distinct, laneRows, ratioRows, runHref, statusOrder } from '../data/bench';
import { Commit, Empty, FilterBar, Mono, Pick, ScrollTable, Section, StatTile, StatusMark, Tiles } from '../components/shared';

const ALL = 'all';
const LANE_STATE = {
  ok: { status: 'pass', word: 'complete' },
  attention: { status: 'fail', word: 'needs attention' },
  blocked: { status: 'blocked', word: 'blocked' },
  degraded: { status: 'timeout', word: 'degraded' },
  empty: { status: 'none', word: 'no case' },
};

function withAll(values, label) {
  return [{ value: ALL, label: `All ${label}` }, ...values.map((value) => ({ value, label: value }))];
}

function RunLink({ shard, anchor, children }) {
  const href = runHref(shard, anchor);
  return href ? (
    <Link href={href} underline="hover">
      {children}
    </Link>
  ) : (
    children
  );
}

export default function Bench({ site, params, patch }) {
  const { bench } = site;
  const branches = useMemo(() => (bench ? benchBranches(bench.latest) : []), [bench]);
  const branch = branches.find((entry) => entry.slug === params.branch) || branches[0];
  const statuses = statusOrder(bench && bench.manifest);
  const lanes = useMemo(() => laneRows(branch, statuses), [branch, statuses]);
  const attention = useMemo(() => attentionRows(branch), [branch]);
  const ratios = useMemo(() => ratioRows(branch), [branch]);

  if (!branch) return <Empty>No nixlbench run has been published yet.</Empty>;

  const lane = params.lane || ALL;
  const status = params.status || ALL;
  const kind = params.kind || ALL;
  const measuredOnly = params.measured === '1';
  const shownAttention = attention.filter((row) => (lane === ALL || row.lane === lane) && (status === ALL || row.status === status));
  const shownRatios = ratios.filter((row) => (lane === ALL || row.lane === lane) && (kind === ALL || row.kind === kind) && (!measuredOnly || row.ok));
  const totals = branch.totals || {};
  const tests = branch.tests || {};
  const repository = (bench.manifest && bench.manifest.repo) || '';
  const shard = (branch.shards || [])[0];
  const bad = (totals.fail || 0) + (totals.timeout || 0) + (totals.crash || 0);

  return (
    <Stack sx={{ gap: 2 }}>
      <FilterBar>
        <Pick
          label="Branch"
          value={branch.slug}
          options={branches.map((entry) => ({ value: entry.slug, label: entry.branch || entry.slug }))}
          onChange={(value) => patch({ branch: value, lane: '', status: '', kind: '' })}
          minWidth={320}
        />
        <Pick label="Lane" value={lane} options={withAll(distinct(lanes, 'lane'), 'lanes')} onChange={(value) => patch({ lane: value === ALL ? '' : value })} />
        <Typography variant="body2" color="text.secondary">
          {when(branch.stamp)} · {branch.headline} run · commit <Commit repository={repository} sha={branch.sha} />
          {branch.stale ? ' · stale' : ''}
        </Typography>
        <Stack direction="row" sx={{ gap: 2, ml: 'auto' }}>
          <RunLink shard={shard}>Run page</RunLink>
          <Link href="bench/index.html" underline="hover">
            All bench runs
          </Link>
        </Stack>
      </FilterBar>

      <Tiles>
        <StatTile label="Cases passed" tone="pass" value={totals.pass || 0} sub={`${totals.skip || 0} skipped`} />
        <StatTile
          label="Cases failed, timed out or crashed"
          tone={bad ? 'fail' : 'pass'}
          value={bad}
          sub={`${totals.fail || 0} failed · ${totals.timeout || 0} timed out · ${totals.crash || 0} crashed`}
        />
        <StatTile label="Cases blocked" tone="blocked" value={totals.blocked || 0} sub="waiting for a change that is not merged" />
        <StatTile
          label="Tests"
          tone={(tests.fail || 0) + (tests.timeout || 0) ? 'fail' : 'pass'}
          value={`${tests.pass || 0} passed`}
          sub={`${tests.fail || 0} failed · ${tests.timeout || 0} timed out · ${tests.skip || 0} skipped`}
        />
      </Tiles>

      <Section title="Lanes">
        <ScrollTable>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Lane</TableCell>
                <TableCell>ROCm</TableCell>
                <TableCell>State</TableCell>
                {statuses.map((name) => (
                  <TableCell key={name} align="right">
                    {name}
                  </TableCell>
                ))}
                <TableCell align="right">Calibration</TableCell>
                <TableCell align="right">Duration</TableCell>
                <TableCell>Environment</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {lanes
                .filter((row) => lane === ALL || row.lane === lane)
                .map((row) => (
                  <TableRow key={`${row.lane}/${row.rocm}`} hover>
                    <TableCell sx={{ fontWeight: 600 }}>
                      <RunLink shard={row.shard}>{row.lane}</RunLink>
                    </TableCell>
                    <TableCell>{row.rocm}</TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>
                      <Stack direction="row" sx={{ gap: 1, alignItems: 'center' }}>
                        <StatusMark status={LANE_STATE[row.state].status} />
                        <span>
                          {LANE_STATE[row.state].word}
                          {row.blockedBy.length ? ` by ${row.blockedBy.join(', ')}` : ''}
                          {row.degraded ? `: ${row.degraded}` : ''}
                        </span>
                      </Stack>
                    </TableCell>
                    {statuses.map((name) => (
                      <TableCell key={name} align="right" sx={{ fontVariantNumeric: 'tabular-nums', color: row.counts[name] ? 'text.primary' : 'text.disabled' }}>
                        {row.counts[name]}
                      </TableCell>
                    ))}
                    <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                      {row.calibGbps === null ? '' : `${row.calibGbps.toFixed(1)} GB/s`}
                    </TableCell>
                    <TableCell align="right">{seconds(row.durationS)}</TableCell>
                    <TableCell>{row.env}</TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        </ScrollTable>
      </Section>

      <Section
        title={`Cases needing attention (${shownAttention.length} of ${branch.attention_total || attention.length})`}
        action={<Pick label="Status" value={status} options={withAll(distinct(attention, 'status'), 'statuses')} onChange={(value) => patch({ status: value === ALL ? '' : value })} minWidth={160} />}
      >
        {shownAttention.length ? (
          <ScrollTable maxHeight={420}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>Case</TableCell>
                  <TableCell>ROCm</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Required</TableCell>
                  <TableCell>Reason</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {shownAttention.map((row) => (
                  <TableRow key={`${row.caseKey}/${row.rocm}`}>
                    <TableCell>
                      <RunLink shard={row.shard} anchor="failures">
                        <Mono>{row.caseKey}</Mono>
                      </RunLink>
                    </TableCell>
                    <TableCell>{row.rocm}</TableCell>
                    <TableCell>
                      <StatusMark status={row.status} label />
                    </TableCell>
                    <TableCell>{row.required ? 'yes' : 'no'}</TableCell>
                    <TableCell sx={{ color: 'text.secondary' }}>{[row.reason !== row.status ? row.reason : '', row.excerpt].filter(Boolean).join(': ')}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollTable>
        ) : (
          <Empty>No case needs attention{lane !== ALL || status !== ALL ? ' with these filters' : ''}.</Empty>
        )}
      </Section>

      <Section
        title={`Declared ratios (${shownRatios.length} of ${ratios.length})`}
        action={
          <Stack direction="row" sx={{ gap: 2, alignItems: 'center', flexWrap: 'wrap' }}>
            <FormControlLabel control={<Switch checked={measuredOnly} onChange={(event) => patch({ measured: event.target.checked ? '1' : '' })} />} label="Measured only" />
            <Pick label="Kind" value={kind} options={withAll(distinct(ratios, 'kind'), 'kinds')} onChange={(value) => patch({ kind: value === ALL ? '' : value })} minWidth={180} />
          </Stack>
        }
      >
        {shownRatios.length ? (
          <ScrollTable maxHeight={520}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>Ratio</TableCell>
                  <TableCell>Kind</TableCell>
                  <TableCell>ROCm</TableCell>
                  <TableCell align="right">Value</TableCell>
                  <TableCell>Numerator over denominator</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {shownRatios.map((row) => (
                  <TableRow key={`${row.key}/${row.rocm}`}>
                    <TableCell>
                      <Mono>{row.key}</Mono>
                    </TableCell>
                    <TableCell>{row.kind}</TableCell>
                    <TableCell>{row.rocm}</TableCell>
                    <TableCell align="right" sx={{ whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
                      {row.ok
                        ? row.points.map((point) => (
                            <div key={`${point.block}x${point.batch}`}>
                              <b>{Number(point.value).toFixed(4)}</b>{' '}
                              <Typography component="span" variant="body2" color="text.secondary">
                                {point.block}×{point.batch}: {Number(point.num).toFixed(3)} / {Number(point.den).toFixed(3)}
                              </Typography>
                            </div>
                          ))
                        : <Typography variant="body2" color="text.secondary">unavailable: {row.why || 'no reason given'}</Typography>}
                    </TableCell>
                    <TableCell sx={{ color: 'text.secondary' }}>
                      <Mono>{row.num}</Mono> over <Mono>{row.den}</Mono>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollTable>
        ) : (
          <Empty>No ratio matches these filters.</Empty>
        )}
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          Under emulation throughput is tracked only as the ratio of two cases of the same job.
        </Typography>
      </Section>
    </Stack>
  );
}
