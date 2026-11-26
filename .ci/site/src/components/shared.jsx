// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { Box, Link, MenuItem, Paper, Stack, TextField, Tooltip, Typography } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import { statusStyle } from '../theme';
import { shortSha } from '../data/format';

// A status is never colour alone: the mark carries a glyph, and a word where
// there is room for one.
export function StatusMark({ status, detail = '', label = false, onClick }) {
  const mode = useTheme().palette.mode;
  const style = statusStyle(status, mode);
  const mark = (
    <Box
      component={onClick ? 'button' : 'span'}
      onClick={onClick}
      aria-label={style.word}
      sx={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '4px',
        minWidth: 22,
        height: 22,
        px: label ? '8px' : 0,
        border: style.outlined ? 1 : 0,
        borderColor: 'divider',
        borderRadius: '6px',
        bgcolor: style.fill,
        color: style.text,
        font: 'inherit',
        fontSize: '0.8rem',
        fontWeight: 650,
        lineHeight: 1,
        whiteSpace: 'nowrap',
        cursor: onClick ? 'pointer' : 'default',
      }}
    >
      <span aria-hidden="true">{style.glyph}</span>
      {label && <span>{style.word}</span>}
    </Box>
  );
  const title = [label ? '' : style.word, detail].filter(Boolean).join(': ');
  return title ? (
    <Tooltip title={<span style={{ whiteSpace: 'pre-line' }}>{title}</span>} arrow>
      {mark}
    </Tooltip>
  ) : (
    mark
  );
}

export function Section({ title, action, children, sx }) {
  return (
    <Paper sx={{ p: 2, ...sx }}>
      {(title || action) && (
        <Stack direction="row" sx={{ alignItems: 'center', justifyContent: 'space-between', gap: 2, mb: 1.5, flexWrap: 'wrap' }}>
          <Typography component="h2" variant="h2">
            {title}
          </Typography>
          {action}
        </Stack>
      )}
      {children}
    </Paper>
  );
}

// A headline number. The value wears text ink; "tone" adds a status mark.
export function StatTile({ label, value, sub, tone, onClick }) {
  return (
    <Paper
      component={onClick ? 'button' : 'div'}
      onClick={onClick}
      sx={{
        p: 2,
        textAlign: 'left',
        font: 'inherit',
        color: 'inherit',
        bgcolor: 'background.paper',
        cursor: onClick ? 'pointer' : 'default',
        '&:hover': onClick ? { borderColor: 'primary.main' } : undefined,
      }}
    >
      <Typography variant="body2" color="text.secondary">
        {label}
      </Typography>
      <Stack direction="row" sx={{ alignItems: 'center', gap: 1, mt: 0.5 }}>
        {tone && <StatusMark status={tone} />}
        <Typography sx={{ fontSize: '1.6rem', fontWeight: 650, lineHeight: 1.2 }}>{value}</Typography>
      </Stack>
      {sub && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
          {sub}
        </Typography>
      )}
    </Paper>
  );
}

export function Tiles({ children }) {
  return (
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))' }}>{children}</Box>
  );
}

// Filters sit in one row above what they filter.
export function FilterBar({ children }) {
  return (
    <Stack direction="row" sx={{ gap: 2, flexWrap: 'wrap', alignItems: 'center', mb: 2 }}>
      {children}
    </Stack>
  );
}

export function Pick({ label, value, options, onChange, minWidth = 200 }) {
  const items = options.map((option) => (typeof option === 'string' ? { value: option, label: option } : option));
  const known = items.some((item) => item.value === value);
  return (
    <TextField select label={label} value={known ? value : ''} onChange={(event) => onChange(event.target.value)} sx={{ minWidth }}>
      {items.map((item) => (
        <MenuItem key={item.value} value={item.value}>
          {item.label}
        </MenuItem>
      ))}
    </TextField>
  );
}

export function Mono({ children, sx }) {
  return (
    <Box component="code" sx={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '0.85em', ...sx }}>
      {children}
    </Box>
  );
}

export function Commit({ repository, sha }) {
  if (!sha) return null;
  const text = <Mono>{shortSha(sha)}</Mono>;
  return repository ? (
    <Link href={`https://github.com/${repository}/commit/${sha}`} target="_blank" rel="noreferrer" underline="hover">
      {text}
    </Link>
  ) : (
    text
  );
}

export function Empty({ children }) {
  return (
    <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
      {children}
    </Typography>
  );
}

export function ScrollTable({ children, maxHeight }) {
  return <Box sx={{ overflow: 'auto', maxHeight }}>{children}</Box>;
}
