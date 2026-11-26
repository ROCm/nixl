// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from 'react';
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  FormControlLabel,
  Link,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material';
import { bytes, rate, seconds, when } from '../data/format';
import { branchHistory, branchNames, checkStatus, gridRows, jobStatus, nightDetail, nightStatus, overall, testDuration, testKey } from '../data/nightly';
import StatusGrid, { Legend } from '../components/StatusGrid';
import { Commit, Empty, FilterBar, Mono, Pick, ScrollTable, Section, StatusMark } from '../components/shared';

function RunDetail({ data, repository, failuresOnly }) {
  const tests = data.tests.filter((test) => !failuresOnly || overall(test.checks) === 'fail');
  const jobs = data.jobs.filter((job) => !failuresOnly || jobStatus(job.result) === 'fail');
  return (
    <Stack sx={{ gap: 2 }}>
      <Section title="Jobs">
        {jobs.length ? (
          <ScrollTable>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Job</TableCell>
                  <TableCell>Result</TableCell>
                  <TableCell align="right">Duration</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {jobs.map((job) => (
                  <TableRow key={job.name}>
                    <TableCell>{job.name}</TableCell>
                    <TableCell>
                      <StatusMark status={jobStatus(job.result)} label detail={job.result} />
                    </TableCell>
                    <TableCell align="right">{seconds(job.duration_s)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollTable>
        ) : (
          <Empty>{failuresOnly ? 'No job failed.' : 'No job was reported.'}</Empty>
        )}
        {data.runId && repository && (
          <Typography variant="body2" sx={{ mt: 1 }}>
            <Link href={`https://github.com/${repository}/actions/runs/${data.runId}`} target="_blank" rel="noreferrer">
              Workflow run {data.runId}
            </Link>
          </Typography>
        )}
      </Section>

      <Section title={`Tests (${tests.length})`}>
        {tests.length ? (
          tests.map((test) => {
            const checks = (test.checks || []).filter((check) => !failuresOnly || checkStatus(check.status) === 'fail');
            return (
              <Accordion key={testKey(test)} disableGutters defaultExpanded={overall(test.checks) === 'fail'} slotProps={{ transition: { unmountOnExit: true } }}>
                <AccordionSummary expandIcon={<span aria-hidden="true">▾</span>}>
                  <Stack direction="row" sx={{ gap: 1.5, alignItems: 'center', flexWrap: 'wrap' }}>
                    <StatusMark status={overall(test.checks)} label />
                    <Typography sx={{ fontWeight: 600 }}>{testKey(test)}</Typography>
                    <Typography variant="body2" color="text.secondary">
                      {[test.script, `${(test.checks || []).length} checks`, seconds(testDuration(test))].filter(Boolean).join(' · ')}
                    </Typography>
                  </Stack>
                </AccordionSummary>
                <AccordionDetails>
                  <ScrollTable>
                    <Table size="small">
                      <TableHead>
                        <TableRow>
                          <TableCell>Check</TableCell>
                          <TableCell>Status</TableCell>
                          <TableCell>Detail</TableCell>
                          <TableCell align="right">Duration</TableCell>
                          <TableCell align="right">Data</TableCell>
                          <TableCell align="right">Rate</TableCell>
                          <TableCell>Path</TableCell>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {checks.map((check) => (
                          <TableRow key={check.name}>
                            <TableCell sx={{ whiteSpace: 'nowrap' }}>{check.name}</TableCell>
                            <TableCell>
                              <StatusMark status={checkStatus(check.status)} label />
                            </TableCell>
                            <TableCell sx={{ color: 'text.secondary' }}>{check.detail}</TableCell>
                            <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                              {seconds(check.duration_s)}
                            </TableCell>
                            <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                              {bytes(check.bytes)}
                            </TableCell>
                            <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                              {check.status === 'PASS' && check.bytes && check.duration_s > 0 ? rate(check.bytes / check.duration_s) : ''}
                            </TableCell>
                            <TableCell>{check.path}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </ScrollTable>
                </AccordionDetails>
              </Accordion>
            );
          })
        ) : (
          <Empty>{failuresOnly ? 'No test failed.' : 'This branch ran no test that night.'}</Empty>
        )}
      </Section>
    </Stack>
  );
}

export default function Nightly({ site, params, patch }) {
  const { runs } = site;
  const names = useMemo(() => branchNames(runs), [runs]);
  const branch = names.includes(params.branch) ? params.branch : names[0];
  const history = useMemo(() => branchHistory(runs, branch), [runs, branch]);
  const nights = [...history].reverse().filter((run) => run.data);
  const stamp = nights.some((run) => run.stamp === params.run) ? params.run : nights[0] && nights[0].stamp;
  const selected = nights.find((run) => run.stamp === stamp);
  const mode = params.show === 'history' ? 'history' : 'run';
  const failuresOnly = params.failures === '1';
  const repository = runs.map((run) => run.summary && run.summary.repository).filter(Boolean).pop() || '';
  const rows = useMemo(() => gridRows(history), [history]);

  if (!branch) return <Empty>No nightly run has been published yet.</Empty>;
  const data = selected && selected.data;

  return (
    <Stack sx={{ gap: 2 }}>
      <FilterBar>
        <Pick label="Branch" value={branch} options={names} onChange={(value) => patch({ branch: value, run: '' })} minWidth={320} />
        {mode === 'run' && (
          <Pick
            label="Night"
            value={stamp || ''}
            options={nights.map((run) => ({ value: run.stamp, label: `${when(run.stamp)} · ${nightStatus(run.data)}` }))}
            onChange={(value) => patch({ run: value })}
            minWidth={260}
          />
        )}
        <ToggleButtonGroup size="small" exclusive value={mode} onChange={(_event, value) => value && patch({ show: value === 'history' ? 'history' : '' })}>
          <ToggleButton value="run">One night</ToggleButton>
          <ToggleButton value="history">History</ToggleButton>
        </ToggleButtonGroup>
        {mode === 'run' && (
          <FormControlLabel
            control={<Switch checked={failuresOnly} onChange={(event) => patch({ failures: event.target.checked ? '1' : '' })} />}
            label="Failures only"
          />
        )}
      </FilterBar>

      {mode === 'history' ? (
        <Section title={`${branch}: last ${history.length} nights`}>
          <Legend />
          <StatusGrid stamps={history.map((run) => run.stamp)} rows={rows} onCell={(_row, entry) => patch({ run: entry.stamp, show: '' })} />
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
            Open a test to see its checks. Select a cell to open that night.
          </Typography>
        </Section>
      ) : data ? (
        <>
          <Section>
            <Stack direction="row" sx={{ gap: 1.5, alignItems: 'center', flexWrap: 'wrap' }}>
              <StatusMark status={nightStatus(data)} label />
              <Typography sx={{ fontWeight: 600 }}>{when(stamp)}</Typography>
              {data.version && (
                <Typography variant="body2">
                  version <Mono>{data.version}</Mono>
                </Typography>
              )}
              <Typography variant="body2">
                commit <Commit repository={repository} sha={data.sha} />
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {nightDetail(data)}
              </Typography>
              <Link variant="body2" href={`data/nightly/${stamp}/summary.json`} target="_blank" rel="noreferrer" sx={{ ml: 'auto' }}>
                summary.json
              </Link>
            </Stack>
          </Section>
          <RunDetail data={data} repository={repository} failuresOnly={failuresOnly} />
        </>
      ) : (
        <Empty>This branch has no published night.</Empty>
      )}
    </Stack>
  );
}
