// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Box, Button, CircularProgress, CssBaseline, Link, Stack, Tab, Tabs, Typography } from '@mui/material';
import { ThemeProvider } from '@mui/material/styles';
import { buildTheme } from './theme';
import { VIEWS, useHashState } from './state';
import { loadSite } from './data/load';
import { latestNight } from './data/nightly';
import { when } from './data/format';
import Overview from './views/Overview';
import Nightly from './views/Nightly';
import Trends from './views/Trends';
import Bench from './views/Bench';
import Wheels from './views/Wheels';

const VIEW = { overview: Overview, nightly: Nightly, trends: Trends, bench: Bench, wheels: Wheels };
const REPOSITORY = 'https://github.com/ROCm/nixl';

function initialMode() {
  const saved = window.localStorage.getItem('theme');
  if (saved === 'light' || saved === 'dark') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export default function App() {
  const [mode, setMode] = useState(initialMode);
  const [load, setLoad] = useState({ site: null, error: null, loading: true });
  const { view, params, go, patch } = useHashState();
  const theme = useMemo(() => buildTheme(mode), [mode]);

  const reload = useCallback(() => {
    setLoad((current) => ({ ...current, loading: true }));
    loadSite().then(
      (site) => setLoad({ site, error: null, loading: false }),
      (error) => setLoad({ site: null, error, loading: false }),
    );
  }, []);
  useEffect(reload, [reload]);

  const toggleMode = () => {
    const next = mode === 'dark' ? 'light' : 'dark';
    window.localStorage.setItem('theme', next);
    setMode(next);
  };

  const { site, error, loading } = load;
  const latest = site ? latestNight(site.runs) : null;
  const View = VIEW[view];
  const tabs = VIEWS.filter((entry) => entry.id !== 'bench' || !site || site.bench);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box component="header" sx={{ borderBottom: 1, borderColor: 'divider', bgcolor: 'background.paper', px: { xs: 2, md: 3 }, pt: 2 }}>
        <Stack direction="row" sx={{ alignItems: 'center', justifyContent: 'space-between', gap: 2, flexWrap: 'wrap' }}>
          <Box>
            <Typography component="h1" variant="h1">
              NIXL on ROCm: nightly results
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {loading ? 'Loading…' : latest ? `Latest night ${when(latest.stamp)} · ${site.runs.length} nights loaded` : 'No data'}
            </Typography>
          </Box>
          <Stack direction="row" sx={{ gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
            <Button component={Link} href={REPOSITORY} target="_blank" rel="noreferrer" size="small">
              ROCm/nixl
            </Button>
            <Button component={Link} href="classic.html" size="small">
              Classic page
            </Button>
            <Button onClick={reload} disabled={loading} size="small">
              Reload
            </Button>
            <Button onClick={toggleMode} size="small" variant="outlined">
              {mode === 'dark' ? 'Light theme' : 'Dark theme'}
            </Button>
          </Stack>
        </Stack>
        <Tabs value={tabs.some((entry) => entry.id === view) ? view : 'overview'} onChange={(_event, value) => go(value)} variant="scrollable" sx={{ mt: 1, minHeight: 44 }}>
          {tabs.map((entry) => (
            <Tab key={entry.id} value={entry.id} label={entry.label} />
          ))}
        </Tabs>
      </Box>
      <Box component="main" sx={{ p: { xs: 2, md: 3 }, maxWidth: 1500, mx: 'auto' }}>
        {error ? (
          <Alert severity="error" action={<Button onClick={reload}>Retry</Button>}>
            The results could not be loaded ({error.message}). <Link href="classic.html">The classic page</Link> lists the runs as plain HTML.
          </Alert>
        ) : !site ? (
          <Stack sx={{ alignItems: 'center', py: 8 }}>
            <CircularProgress />
          </Stack>
        ) : (
          <View site={site} params={params} patch={patch} go={go} />
        )}
      </Box>
    </ThemeProvider>
  );
}
