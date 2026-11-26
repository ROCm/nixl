// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react';
import { Box, Button, Link, Stack, Typography } from '@mui/material';
import { ago } from '../data/format';
import { siteUrl } from '../data/load';
import { installCommand } from '../data/wheels';
import { Commit, Mono, Section } from './shared';

function pythonRange(python) {
  if (!python.length) return '';
  return python.length > 2 ? `Python ${python[0]} to ${python[python.length - 1]}` : `Python ${python.join(' and ')}`;
}

export default function InstallWheels({ target, repository, go }) {
  const [copied, setCopied] = useState(false);
  const command = installCommand(siteUrl(`wheels/${target.slug}/simple/`), target.project);
  const copy = () => {
    navigator.clipboard.writeText(command).then(
      () => setCopied(true),
      () => setCopied(false),
    );
  };
  const built = target.built ? `${target.built.toISOString().slice(0, 10)} (${ago(target.built)})` : '';
  const readme = repository && target.sha ? `https://github.com/${repository}/blob/${target.sha}/src/bindings/python/nixl-meta/README.rocm.md` : '';

  return (
    <Section
      title="Install the nightly wheels"
      sx={{ borderLeft: 4, borderLeftColor: 'primary.main' }}
      action={
        <Stack direction="row" sx={{ gap: 1, flexWrap: 'wrap' }}>
          <Button size="small" variant="outlined" onClick={() => go('wheels', { branch: target.branch })}>
            Wheel files and checksums
          </Button>
          <Button size="small" variant="outlined" component="a" href={`wheels/${target.slug}/simple/`}>
            pip index
          </Button>
        </Stack>
      }
    >
      <Typography variant="body2" sx={{ mb: 1 }} data-install="summary">
        <Mono>
          {target.project} {target.version}
        </Mono>
        {built && ` built ${built}`} from <strong>{target.branch}</strong>
        {target.sha && ' at '}
        <Commit repository={repository} sha={target.sha} />
        {target.python.length > 0 && `, for ${pythonRange(target.python)} on Linux x86-64`}.
      </Typography>
      <Stack direction="row" sx={{ gap: 1, alignItems: 'stretch' }}>
        <Box
          component="pre"
          data-install="command"
          sx={{
            flex: 1,
            minWidth: 0,
            m: 0,
            p: 1.5,
            border: 1,
            borderColor: 'divider',
            borderRadius: 1,
            bgcolor: 'action.hover',
            overflowX: 'auto',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            fontSize: '0.85rem',
          }}
        >
          {command}
        </Box>
        <Button variant="contained" onClick={copy} sx={{ flexShrink: 0, minWidth: 88 }}>
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
        A development build from the last night this branch passed, tested against emulated devices only and not a PyPI release. It needs a ROCm install on the machine
        {readme ? (
          <>
            : the{' '}
            <Link href={readme} target="_blank" rel="noreferrer">
              {target.project} README
            </Link>{' '}
            lists the supported ROCm versions and how <Mono>import nixl</Mono> finds one.
          </>
        ) : (
          '.'
        )}
      </Typography>
    </Section>
  );
}
