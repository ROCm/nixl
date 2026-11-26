// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { stampDate } from './format';
import { branchesOf } from './nightly';

// The project the wheels are published as. An index from before it was
// renamed says rocm-nixl, and the install command follows the index.
export const PROJECT = 'nixl-rocm';

// "rocm_nixl-1.6.0-cp312-...whl" -> "rocm-nixl": the project a wheel file is of.
export function projectOf(file) {
  const m = /^([A-Za-z0-9_.]+?)-\d/.exec(String(file || ''));
  return m ? m[1].replace(/[_.]+/g, '-').toLowerCase() : PROJECT;
}

// "cp312" -> "3.12"; "3.12" stays.
export function pythonVersion(tag) {
  const m = /^cp(\d)(\d+)$/.exec(String(tag || ''));
  return m ? `${m[1]}.${m[2]}` : String(tag || '');
}

function versions(tags) {
  const seen = [...new Set(tags.map(pythonVersion).filter(Boolean))];
  return seen.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

// The pip index to install the nightly wheels from. wheels/index.json, which
// the wheel publisher writes, says which branch is published and what its
// index holds. A site from before that file has only the nights to go by: the
// wheels of a night are published when their branch passes.
export function installTarget(listing, runs) {
  const indexes = listing && Array.isArray(listing.indexes) ? listing.indexes : [];
  const current = indexes.find((entry) => entry && entry.current && entry.slug);
  if (current) {
    return {
      slug: current.slug,
      project: current.project || PROJECT,
      branch: current.branch || current.slug,
      version: current.version || '',
      sha: current.sha || '',
      built: current.date ? new Date(current.date) : null,
      python: versions(Array.isArray(current.python) ? current.python : []),
    };
  }
  if (listing) return null;
  for (const run of [...runs].reverse()) {
    const entry = branchesOf(run.summary).find((branch) => branch.passed && branch.wheels.length);
    if (entry) {
      return {
        slug: entry.slug,
        project: projectOf(entry.wheels[0].file),
        branch: entry.branch,
        version: entry.version,
        sha: entry.sha,
        built: stampDate(run.stamp),
        python: versions(entry.wheels.map((wheel) => wheel.python)),
      };
    }
  }
  return null;
}

export function installCommand(indexUrl, project = PROJECT) {
  return `pip install --pre --index-url ${indexUrl} --extra-index-url https://pypi.org/simple ${project}`;
}
