# Patches

One directory per upstream component; each directory is that component's
patchset, applied on top of its pinned release tag.

```
patches/
  nixl/    -> applied to ai-dynamo/nixl @ NIXL_REF
  mori/    -> applied to ROCm/mori      @ MORI_REF
```

## Rules

- Every `*.patch` in a component's directory is applied with `git apply`, in
  **lexical filename order**. Prefix with `NN-` to fix the sequence:
  `01-topic.patch`, `02-topic.patch`, …
- Patch N is applied to the tree produced by patches 1..N-1, so a later patch
  may depend on an earlier one.
- A patch that does not apply **fails the build** — there is no fuzzy or
  partial application, and no silent skip.
- An empty directory is valid: the pristine tag is built.
- Files that are not `*.patch` (this README, notes, `.gitkeep`) are ignored.

## Adding a patch

Work in a real checkout of the component at the pinned ref, then export:

```bash
git clone --depth 1 --branch v1.3.2 https://github.com/ai-dynamo/nixl.git
cd nixl
# ...make changes, git add...
git diff --cached --no-renames > /path/to/patches/nixl/01-my-change.patch
```

Add a comment header above the `diff --git` line explaining what the patch
does, where it came from (upstream PR, internal branch + commit), and how to
regenerate it after a version bump. `git apply` ignores leading prose, so the
header costs nothing.

Then validate without waiting on a full image build:

```bash
make patch-check              # both components against their pinned tags
make patch-check COMPONENT=nixl
```

## After a version bump

Bumping `NIXL_REF` / `MORI_REF` in the `Makefile` (and the matching `ARG` in
`docker/Dockerfile`) is the moment patches drift. Run `make patch-check`
immediately: it clones the new tag and dry-runs the whole stack, so a rebase is
a minute's work instead of a failure 30 minutes into an image build.
