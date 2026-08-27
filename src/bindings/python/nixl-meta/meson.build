# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

fs = import('fs')

build_dir = meson.current_build_dir()

pyproject_toml = configure_file(
  input: 'pyproject.toml.in',
  output: 'pyproject.toml',
  configuration: {
    'VERSION': meson.project_version(),
    # A release meta wheel depends on every variant unconditionally: there is
    # no PEP 508 environment marker for GPU vendor, so the variant cannot be
    # selected at install time. nixl/__init__.py picks the right one at import.
    # Every variant listed here must be published for the release version, or
    # `pip install nixl` fails to resolve for all users, not just that vendor's.
    'WHEEL_DEPS': get_option('release_wheel') \
      ? '"nixl-cu12==' + meson.project_version() + '", "nixl-cu13==' + meson.project_version() + '", "nixl-rocm==' + meson.project_version() + '"' \
      : '"' + cuda_wheel_dir.replace('_', '-') + '==' + meson.project_version() + '"',
  }
)

readme_md = fs.copyfile('README.md')
nixl_meta_utils_copy = fs.copyfile('nixl_meta_utils.py')

source_root = meson.project_source_root()
root_license_path = join_paths(source_root, 'LICENSE')
license_path = fs.copyfile(root_license_path)

subdir('nixl')
subdir('nixl_ep')

uv = find_program('uv', required: false)
if uv.found()
  wheel_name = 'nixl-@0@-py3-none-any.whl'.format(meson.project_version())
  # Inputs intentionally point at the build-dir COPIES (return values of
  # fs.copyfile() captured in the subdirs), not the source files. This way
  # ninja runs the copies before `uv build` and incremental rebuilds pick
  # up edits to the source __init__.py files.
  meta_wheel = custom_target(
    'build_nixl_meta',
    input: [
      pyproject_toml,
      readme_md,
      license_path,
      nixl_meta_utils_copy,
      nixl_init_copy,
      nixl_api_copy,
      nixl_logging_copy,
      nixl_ep_init_copy,
    ],
    output: [wheel_name],
    command: [uv, 'build', '--wheel', '--out-dir', build_dir, build_dir],
    install: false,
    build_by_default: true
  )
else
  warning('uv not found, skipping meta package build')
endif
