// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react';
import { Autocomplete, Box, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { bytes, day, rate, seconds, when } from '../data/format';
import { METRICS, branchHistory, branchNames, trendSeries } from '../data/nightly';
import { assignSlots } from '../state';
import { SERIES_SLOTS, chartInk } from '../theme';
import TrendChart from '../components/Chart';
import { Empty, FilterBar, Pick, ScrollTable, Section } from '../components/shared';

const FORMAT = { seconds, bytes, rate };

function Swatch({ slot }) {
  const ink = chartInk(useTheme().palette.mode);
  return <Box component="span" sx={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', bgcolor: ink.series[slot], mr: 1 }} />;
}

export default function Trends({ site, params, patch }) {
  const { runs } = site;
  const names = useMemo(() => branchNames(runs), [runs]);
  const metric = METRICS.find((entry) => entry.id === params.metric) || METRICS[0];
  const all = useMemo(() => {
    const found = {};
    names.forEach((name) => {
      found[name] = trendSeries(branchHistory(runs, name), metric.id);
    });
    return found;
  }, [runs, names, metric.id]);
  // Branches with nothing to plot for this metric are not offered.
  const offered = names.filter((name) => all[name].length);
  // Unless one is chosen, the branch with the most to show: a branch that
  // cannot build wheels has the duration of its plan job and nothing else.
  const fullest = offered.reduce((best, name) => (best && all[best].length >= all[name].length ? best : name), '');
  const branch = offered.includes(params.branch) ? params.branch : fullest;
  const available = useMemo(() => (branch ? all[branch] : []), [all, branch]);

  const key = `${branch}/${metric.id}`;
  const [choice, setChoice] = useState({ key: '', names: [], slots: {} });
  const fresh = choice.key !== key;
  const chosen = fresh ? available.slice(0, SERIES_SLOTS).map((entry) => entry.name) : choice.names;
  const slots = fresh ? assignSlots({}, chosen) : choice.slots;
  const choose = (next) => {
    const kept = next.slice(0, SERIES_SLOTS);
    setChoice({ key, names: kept, slots: assignSlots(slots, kept) });
  };

  const stamps = useMemo(() => runs.map((run) => run.stamp), [runs]);
  const labels = useMemo(() => Object.fromEntries(stamps.map((stamp) => [stamp, day(stamp)])), [stamps]);
  const series = useMemo(
    () =>
      available
        .filter((entry) => chosen.includes(entry.name))
        .map((entry) => ({
          name: entry.name,
          slot: slots[entry.name],
          points: entry.points.map((point) => ({ x: point.stamp, value: point.value })),
        })),
    [available, chosen, slots],
  );
  const format = FORMAT[metric.unit];

  return (
    <Stack sx={{ gap: 2 }}>
      <FilterBar>
        <Pick label="Metric" value={metric.id} options={METRICS.map((entry) => ({ value: entry.id, label: entry.label }))} onChange={(value) => patch({ metric: value })} />
        <Pick label="Branch" value={branch || ''} options={offered} onChange={(value) => patch({ branch: value })} minWidth={320} />
        <Autocomplete
          multiple
          size="small"
          limitTags={3}
          disableCloseOnSelect
          options={available.map((entry) => entry.name)}
          value={chosen}
          onChange={(_event, value) => choose(value)}
          getOptionDisabled={(option) => chosen.length >= SERIES_SLOTS && !chosen.includes(option)}
          renderInput={(props) => <TextField {...props} label={`Series (${chosen.length} of ${available.length}, at most ${SERIES_SLOTS})`} />}
          sx={{ minWidth: 360, flex: 1 }}
        />
      </FilterBar>

      {!branch ? (
        <Empty>No branch has reported {metric.label.toLowerCase()} in the nights loaded.</Empty>
      ) : (
        <>
          <Section title={`${metric.label}: ${branch}`}>
            {series.length ? (
              <TrendChart categories={stamps} labels={labels} series={series} format={format} ariaLabel={`${metric.label} of ${branch} by night`} />
            ) : (
              <Empty>Choose a series to plot.</Empty>
            )}
            {metric.id === 'rates' && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                Emulated devices on shared runners: read these for trends only.
              </Typography>
            )}
          </Section>

          <Section title="Values">
            <ScrollTable maxHeight={420}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell>Series</TableCell>
                    {stamps.map((stamp) => (
                      <TableCell key={stamp} align="right" title={when(stamp)}>
                        {day(stamp)}
                      </TableCell>
                    ))}
                  </TableRow>
                </TableHead>
                <TableBody>
                  {series.map((entry) => {
                    const byStamp = new Map(entry.points.map((point) => [point.x, point.value]));
                    return (
                      <TableRow key={entry.name}>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>
                          <Swatch slot={entry.slot} />
                          {entry.name}
                        </TableCell>
                        {stamps.map((stamp) => (
                          <TableCell key={stamp} align="right" sx={{ whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
                            {byStamp.has(stamp) ? format(byStamp.get(stamp)) : ''}
                          </TableCell>
                        ))}
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </ScrollTable>
          </Section>
        </>
      )}
    </Stack>
  );
}
