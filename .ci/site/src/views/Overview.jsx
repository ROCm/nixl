// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { Link, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material';
import { ago, stampDate, when } from '../data/format';
import { branchHistory, branchNames, checkStatus, countBy, jobStatus, latestNight, nightDetail, nightStatus, overall } from '../data/nightly';
import { attentionTotal, benchBranches } from '../data/bench';
import { installTarget } from '../data/wheels';
import InstallWheels from '../components/InstallWheels';
import StatusGrid, { Legend } from '../components/StatusGrid';
import { Commit, Empty, Mono, ScrollTable, Section, StatTile, StatusMark, Tiles } from '../components/shared';

function tally(counts) {
  const parts = [];
  if (counts.pass) parts.push(`${counts.pass} passed`);
  if (counts.fail) parts.push(`${counts.fail} failed`);
  if (counts.skip) parts.push(`${counts.skip} skipped`);
  return parts.join(', ') || 'none';
}

export default function Overview({ site, go }) {
  const { runs, bench } = site;
  const latest = latestNight(runs);
  if (!latest) return <Empty>No nightly run has been published yet.</Empty>;
  const stamps = runs.map((run) => run.stamp);
  const names = branchNames(runs);
  const buildable = latest.counts.pass + latest.counts.fail;
  const attention = bench ? attentionTotal(bench.latest) : null;
  const benchRuns = bench ? benchBranches(bench.latest) : [];
  const install = installTarget(site.wheels, runs);

  return (
    <Stack sx={{ gap: 2 }}>
      <Tiles>
        <StatTile
          label="Latest night"
          value={when(latest.stamp).slice(0, 10)}
          sub={`${when(latest.stamp).slice(11)} · ${ago(stampDate(latest.stamp))}`}
        />
        <StatTile
          label="Branches passing"
          tone={latest.counts.fail ? 'fail' : 'pass'}
          value={`${latest.counts.pass} of ${buildable}`}
          sub={latest.counts.skip ? `${latest.counts.skip} more cannot build ROCm wheels yet` : 'every branch builds ROCm wheels'}
        />
        <StatTile
          label="Failing branches"
          tone={latest.failing.length ? 'fail' : 'pass'}
          value={latest.failing.length}
          sub={latest.failing.map((entry) => entry.branch).join(', ') || 'none'}
          onClick={latest.failing.length ? () => go('nightly', { branch: latest.failing[0].branch }) : undefined}
        />
        {attention !== null && (
          <StatTile
            label="nixlbench cases needing attention"
            tone={attention ? 'timeout' : 'pass'}
            value={attention}
            sub={`across ${benchRuns.length} benchmarked ${benchRuns.length === 1 ? 'branch' : 'branches'}`}
            onClick={() => go('bench')}
          />
        )}
        <StatTile
          label="Upstream main"
          value={<Commit repository="ai-dynamo/nixl" sha={latest.upstreamSha} />}
          sub={latest.sync ? `sync: ${latest.sync}` : ''}
        />
      </Tiles>

      {install && <InstallWheels target={install} repository={latest.repository} go={go} />}

      <Section title={`Branches on ${when(latest.stamp)}`}>
        <ScrollTable>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Branch</TableCell>
                <TableCell>Night</TableCell>
                <TableCell>Version</TableCell>
                <TableCell>Commit</TableCell>
                <TableCell>Jobs</TableCell>
                <TableCell>Tests</TableCell>
                <TableCell>nixlbench</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {latest.branches.map((entry) => (
                <TableRow key={entry.branch} hover>
                  <TableCell>
                    <Link component="button" underline="hover" onClick={() => go('nightly', { branch: entry.branch })} sx={{ fontWeight: 600, textAlign: 'left' }}>
                      {entry.branch}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Stack direction="row" sx={{ gap: 1, alignItems: 'center' }}>
                      <StatusMark status={nightStatus(entry)} label />
                      <Typography variant="body2" color="text.secondary">
                        {nightDetail(entry)}
                      </Typography>
                    </Stack>
                  </TableCell>
                  <TableCell>{entry.version && <Mono>{entry.version}</Mono>}</TableCell>
                  <TableCell>
                    <Commit repository={latest.repository} sha={entry.sha} />
                  </TableCell>
                  <TableCell>{tally(countBy(entry.jobs, (job) => jobStatus(job.result)))}</TableCell>
                  <TableCell>
                    {entry.tests.length
                      ? `${tally(countBy(entry.tests, (test) => overall(test.checks)))} (${tally(
                          countBy(
                            entry.tests.flatMap((test) => test.checks || []),
                            (check) => checkStatus(check.status),
                          ),
                        )} checks)`
                      : 'none'}
                  </TableCell>
                  <TableCell>{entry.nixlbench ? entry.nixlbench.result || '' : ''}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </ScrollTable>
      </Section>

      <Section title={`Last ${stamps.length} nights`}>
        <Legend />
        <StatusGrid
          stamps={stamps}
          rows={names.map((name) => ({
            kind: 'branch',
            label: name,
            cells: branchHistory(runs, name).map((run) => ({
              stamp: run.stamp,
              status: nightStatus(run.data),
              detail: nightDetail(run.data),
            })),
          }))}
          onLabel={(row) => go('nightly', { branch: row.label })}
          onCell={(row, entry) => go('nightly', { branch: row.label, run: entry.stamp })}
        />
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          Select a branch or a night to open it.
        </Typography>
      </Section>
    </Stack>
  );
}
