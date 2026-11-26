// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react';
import { Box, Stack, Typography } from '@mui/material';
import { day, when } from '../data/format';
import { StatusMark } from './shared';

export function Legend({ statuses = ['pass', 'fail', 'skip', 'none'] }) {
  return (
    <Stack direction="row" sx={{ gap: 2, flexWrap: 'wrap', mb: 1 }}>
      {statuses.map((status) => (
        <StatusMark key={status} status={status} label />
      ))}
    </Stack>
  );
}

// rows: [{kind, label, parent?, cells: [{stamp, status, detail}]}], one column
// per night, oldest first. Rows with a "parent" are folded under the row of
// that label until it is opened.
export default function StatusGrid({ stamps, rows, onCell, onLabel }) {
  const [open, setOpen] = useState({});
  const parents = new Set(rows.filter((row) => row.parent).map((row) => row.parent));
  const cell = { p: '2px', textAlign: 'center' };
  return (
    <Box sx={{ overflowX: 'auto' }}>
      <Box component="table" sx={{ borderCollapse: 'collapse', fontSize: '0.85rem' }}>
        <thead>
          <tr>
            <th />
            {stamps.map((stamp) => (
              <Box component="th" key={stamp} scope="col" title={when(stamp)} sx={{ ...cell, fontWeight: 500, color: 'text.secondary', fontSize: '0.7rem' }}>
                {day(stamp)}
              </Box>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            if (row.parent && !open[row.parent]) return null;
            const foldable = row.kind === 'test' && parents.has(row.label);
            const label = row.kind === 'job' ? `job: ${row.label}` : row.label;
            const action = foldable
              ? () => setOpen((current) => ({ ...current, [row.label]: !current[row.label] }))
              : onLabel
                ? () => onLabel(row)
                : null;
            return (
              <tr key={`${row.parent || ''}/${row.kind}/${row.label}`}>
                <Box
                  component="th"
                  scope="row"
                  sx={{
                    textAlign: 'left',
                    fontWeight: row.kind === 'check' ? 400 : 600,
                    color: row.kind === 'check' ? 'text.secondary' : 'text.primary',
                    pl: row.kind === 'check' ? 3 : 0,
                    pr: 2,
                    py: '2px',
                    whiteSpace: 'nowrap',
                    position: 'sticky',
                    left: 0,
                    bgcolor: 'background.paper',
                  }}
                >
                  {action ? (
                    <Box
                      component="button"
                      onClick={action}
                      aria-expanded={foldable ? Boolean(open[row.label]) : undefined}
                      sx={{ all: 'unset', cursor: 'pointer', '&:hover': { color: 'primary.main' }, '&:focus-visible': { outline: 2 } }}
                    >
                      {foldable && (
                        <Typography component="span" sx={{ display: 'inline-block', width: 16, fontSize: 'inherit' }}>
                          {open[row.label] ? '▾' : '▸'}
                        </Typography>
                      )}
                      {label}
                    </Box>
                  ) : (
                    label
                  )}
                </Box>
                {row.cells.map((entry) => (
                  <Box component="td" key={entry.stamp} sx={cell}>
                    <StatusMark
                      status={entry.status}
                      detail={[when(entry.stamp), entry.detail].filter(Boolean).join('\n')}
                      onClick={onCell ? () => onCell(row, entry) : undefined}
                    />
                  </Box>
                ))}
              </tr>
            );
          })}
        </tbody>
      </Box>
    </Box>
  );
}
