// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from 'react';
import { Link, Stack, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material';
import { bytes, when } from '../data/format';
import { branchHistory, branchNames } from '../data/nightly';
import { siteUrl } from '../data/load';
import { Commit, Empty, FilterBar, Mono, Pick, ScrollTable, Section } from '../components/shared';

export default function Wheels({ site, params, patch }) {
  const { runs } = site;
  const histories = useMemo(() => {
    const found = {};
    branchNames(runs).forEach((name) => {
      const nights = branchHistory(runs, name)
        .filter((run) => run.data && run.data.wheels.length)
        .reverse();
      if (nights.length) found[name] = nights;
    });
    return found;
  }, [runs]);
  const names = Object.keys(histories);
  const branch = names.includes(params.branch) ? params.branch : names[0];
  if (!branch) return <Empty>No branch has built a wheel in the nights loaded.</Empty>;
  const nights = histories[branch];
  const night = nights.find((run) => run.stamp === params.run) || nights[0];
  const data = night.data;
  const repository = runs.map((run) => run.summary && run.summary.repository).filter(Boolean).pop() || '';
  const bench = data.nixlbench && data.nixlbench.wheel;
  const index = siteUrl(`wheels/${data.slug}/simple/`);

  return (
    <Stack sx={{ gap: 2 }}>
      <FilterBar>
        <Pick label="Branch" value={branch} options={names} onChange={(value) => patch({ branch: value, run: '' })} minWidth={320} />
        <Pick label="Night" value={night.stamp} options={nights.map((run) => ({ value: run.stamp, label: when(run.stamp) }))} onChange={(value) => patch({ run: value })} minWidth={220} />
        <Typography variant="body2" color="text.secondary">
          version <Mono>{data.version}</Mono> · commit <Commit repository={repository} sha={data.sha} />
        </Typography>
      </FilterBar>

      <Section title="Install">
        <Typography variant="body2" sx={{ mb: 1 }}>
          Development builds, tested against emulated devices only. A branch has a pip index only if it is published; <Link href="wheels/index.html">the wheel index page</Link> lists the ones that are.
        </Typography>
        <Mono sx={{ display: 'block', p: 1.5, border: 1, borderColor: 'divider', borderRadius: 1, overflowX: 'auto', whiteSpace: 'nowrap' }}>
          pip install --pre --index-url {index} --extra-index-url https://pypi.org/simple rocm-nixl
        </Mono>
      </Section>

      <Section title={`Wheels built on ${when(night.stamp)}`}>
        <ScrollTable>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>File</TableCell>
                <TableCell>Python</TableCell>
                <TableCell>ROCm</TableCell>
                <TableCell align="right">Size</TableCell>
                <TableCell>SHA-256</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {[...data.wheels, ...(bench ? [{ ...bench, python: 'any', rocm: data.nixlbench.rocm }] : [])].map((wheel) => (
                <TableRow key={wheel.file}>
                  <TableCell>
                    <Mono>{wheel.file}</Mono>
                  </TableCell>
                  <TableCell>{wheel.python}</TableCell>
                  <TableCell>{(wheel.rocm || []).join(', ')}</TableCell>
                  <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                    {bytes(wheel.bytes)}
                  </TableCell>
                  <TableCell>
                    <Mono title={wheel.sha256}>{String(wheel.sha256 || '').slice(0, 16)}</Mono>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </ScrollTable>
      </Section>
    </Stack>
  );
}
