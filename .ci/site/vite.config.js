// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import fs from 'node:fs';
import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const TYPES = { '.json': 'application/json', '.html': 'text/html; charset=utf-8', '.gz': 'application/gzip' };
// What the publishers own on gh-pages; everything else is the app's.
const DATA_PREFIXES = ['/data/', '/bench/', '/wheels/', '/classic.html'];

// Serves a checkout of gh-pages under the dev and preview servers, so the app
// reads the published data the way it does on Pages.
function publishedData(directory) {
  const root = path.resolve(directory);
  const middleware = (request, response, next) => {
    const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
    if (!DATA_PREFIXES.some((prefix) => pathname.startsWith(prefix))) return next();
    let file = path.join(root, pathname);
    if (!file.startsWith(root + path.sep)) return next();
    if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
    if (!fs.existsSync(file)) {
      response.statusCode = 404;
      return response.end('not found');
    }
    response.setHeader('Content-Type', TYPES[path.extname(file)] || 'application/octet-stream');
    return fs.createReadStream(file).pipe(response);
  };
  return {
    name: 'published-data',
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}

export default defineConfig({
  // Relative, so the site works from any sub-path, as the data URLs do.
  base: './',
  plugins: [react(), process.env.SITE_DATA_DIR ? publishedData(process.env.SITE_DATA_DIR) : null].filter(Boolean),
  build: {
    target: 'es2022',
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('/node_modules/zrender/') || id.includes('/node_modules/echarts/')) return 'charts';
          if (id.includes('/node_modules/@mui/') || id.includes('/node_modules/@emotion/')) return 'ui';
          return undefined;
        },
      },
    },
  },
  test: { environment: 'node', include: ['src/**/*.test.js'] },
});
