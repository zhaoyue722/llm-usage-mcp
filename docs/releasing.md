# Releasing

Cutting a release is two commands. Everything else is automated by
`.github/workflows/release.yml`, which triggers on a version tag.

```bash
uv run python scripts/check_version_consistency.py   # pre-flight
git tag v0.1.3 && git push --tags
```

## What the workflow does

On a `v*` tag push, from a clean runner, at the tagged commit:

1. **Version guard** — `scripts/check_version_consistency.py` asserts the
   tag matches `pyproject.toml` *and* both version fields in
   `server.json`. Fails before anything is published.
2. **Full quality gate** — ruff, ruff format, mypy `--strict`, pytest with
   the coverage floor. Same as CI, re-run because publishing is one-way.
3. **Build** — `uv build` (wheel + sdist).
4. **Wheel smoke test** — install the built wheel into a throwaway venv
   with `pip` (deliberately *not* `uv sync`, so dependencies resolve
   fresh against live PyPI instead of from `uv.lock`), then boot it.
   See "Why the smoke test exists" below.
5. **Publish to PyPI** — `uv publish` via Trusted Publishing.
6. **Publish to the MCP Registry** — `mcp-publisher login github-oidc`
   then `publish`.

Order matters: PyPI first, because the registry validates that the PyPI
package exists and carries the `mcp-name` marker (it lives in
`README.md`, which lands in the wheel's `PKG-INFO`).

Neither publish step uses a stored credential — both exchange GitHub's
per-run OIDC token for a short-lived one. There is no PyPI API token in
repo secrets and none on your laptop.

## One-time setup

The workflow cannot publish until a trusted publisher is registered on
PyPI. Do this once:

1. Go to <https://pypi.org/manage/project/llm-usage-mcp/settings/publishing/>
2. Add a **GitHub** publisher:
   - Owner: `zhaoyue722`
   - Repository: `llm-usage-mcp`
   - Workflow name: `release.yml`
   - Environment: *(leave blank)*
3. Save. Delete any long-lived API token you were previously publishing
   with — leaving it active keeps the credential you just stopped needing.

> **Hardening option.** PyPI's "Environment" field pairs with a GitHub
> Actions environment that can require manual approval before a release
> job runs. To use it, create an environment named `release` in the repo
> settings, put the same name in the PyPI publisher config, and add
> `environment: release` to the job in `release.yml`. All three must
> agree or publishing fails. Skipped by default to keep the first release
> from tripping on config drift.

The registry side needs no setup: the `io.github.zhaoyue722/*` namespace
is authenticated by this repository's own OIDC identity, which matches
the namespace owner.

## Cutting a release

1. Update the version in **all three** places (the guard checks them, but
   fixing them before tagging is faster than a failed run):
   - `pyproject.toml` → `[project].version`
   - `server.json` → `.version`
   - `server.json` → `.packages[0].version`
2. Write the `CHANGELOG.md` entry. Keep a Changelog format, semver.
   Move items out of `[Unreleased]`.
3. Run the pre-flight: `uv run python scripts/check_version_consistency.py`
4. Commit, then tag and push:
   ```bash
   git tag v0.1.3 && git push origin main --tags
   ```
5. Watch the run. On success the version is live on both PyPI and the
   registry.

### If a step fails after PyPI succeeded

PyPI uploads are irreversible — a version number cannot be reused. If
the registry step fails, re-run the workflow via **Actions → Release →
Run workflow**, passing the tag. Re-running is safe: PyPI rejects the
duplicate upload and the registry publish is an upsert.

## Why the smoke test exists

Both packaging failures this project has actually shipped were invisible
to the test suite, because the suite runs against the source tree and the
lockfile, and users get neither.

- **v0.1.0** — Alembic migrations lived outside the package directory, so
  they weren't in the wheel. Every install crashed on first boot with
  `alembic.ini not found`. Fixed in v0.1.1 by moving migrations inside
  the package and resolving them relative to `__file__`.
- **v0.1.2** — `mcp[cli]>=1.27.0` had no upper bound. When `mcp` 2.0.0
  removed `mcp.server.fastmcp`, fresh installs resolved to 2.x and every
  console script failed at import. `uv.lock` pinned 1.27.0, so the repo
  never saw it. Fixed by bounding the dependency `<2`.

Both are the same shape: *the thing you ship is not the thing you test.*
The smoke test closes it by installing the built wheel with `pip`, into a
venv with no checkout on the path, resolving dependencies fresh — then
running the first-boot path end to end and asserting the pricing catalog
is actually populated.

That last assertion matters: a migration that "succeeds" against an empty
catalog is not a passing release.

## Deliberately not automated

- **Version bumps and the changelog.** Deciding that a change is a minor
  rather than a patch is a judgment call about the public contract (MCP
  tool signatures, CLI flags, schema semantics), and the changelog is
  written for humans. Automating those would mean generating them from
  commit messages, which produces a worse changelog.
- **Merging the weekly pricing PR.** Same principle as
  `refresh-pricing.yml`: automate the part with no judgment in it, and
  make the part with judgment easy rather than automatic.
