# Contributing

Thanks for helping improve the IDC REST API + MCP server. This document is the **process**: how we
branch, commit, changelog, and version.

The **code** is documented elsewhere, and this file does not repeat it:

| You want to | Read |
|---|---|
| Set up, run, and test the project | [dev/developer_guide.md](dev/developer_guide.md) — *Setup*, *Run*, *Test* |
| Know what CI will run against your PR | [dev/developer_guide.md](dev/developer_guide.md) — *Continuous integration* |
| Understand the invariants reviewers check | [dev/developer_guide.md](dev/developer_guide.md) — *Conventions* |
| Add a capability (model → service → REST → MCP → parity test) | [dev/developer_guide.md](dev/developer_guide.md) — *Walkthrough* |
| Understand *why* it's built this way | [dev/architecture.md](dev/architecture.md) |
| Cut a release / deploy | [dev/deployment.md](dev/deployment.md) — *Cutting a release* |

In short: `uv pip install -e ".[dev]"`, then make sure `ruff`, `bandit`, and `pytest` pass before
you push — the developer guide has the exact commands CI uses.

Documentation is split by audience (user guide vs. the agent-facing `idc://guide` resource vs.
the always-on MCP `INSTRUCTIONS` vs. `dev/`). Keep each in its lane — the conventions, and which
file to touch when, are in [CLAUDE.md](CLAUDE.md#documentation-conventions).

## Branches

Branch off `main`, named `<type>/<short-slug>` using the same type vocabulary as commits:

```
feat/cohort-size-estimate     fix/mcp-trailing-slash
docs/api-endpoint-examples    ci/multi-tier-deploy
```

Pull requests are merged with a merge commit, so the individual commits on your branch land in
`main`'s history. Make them ones you'd want to read later.

## Commits

We follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<optional scope>): <imperative, lowercase summary>
```

**Types:** `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `style`, `build`, `ci`, `chore`.
**Scopes** in use: `rest`, `mcp`, `api`, `deploy`, `deps`.

```
feat(rest): redirect the bare domain to the interactive docs
fix(mcp): serve /mcp and /mcp/ directly instead of redirecting
docs(api): add OpenAPI summaries/descriptions to every REST route
```

This is a convention, not a CI gate — nothing will fail your build if you deviate. It exists so
history stays skimmable and so the changelog is easy to assemble at release time. Dependabot's
commits don't always conform; that's fine.

Mark a breaking change to the REST or MCP contract with a `!` (`feat(rest)!: …`) and a
`BREAKING CHANGE:` footer. See [Versioning](#versioning) — such a change needs a new URL prefix,
so it is a much bigger conversation than a commit message.

## Changelog

[CHANGELOG.md](CHANGELOG.md) is **hand-curated**, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. It is not generated from commits: it describes what changed for *callers of the API*,
which is a different thing from what changed in the tree.

**If your PR changes user-visible behavior, add an entry to `## [Unreleased]` in the same PR.**
User-visible means an endpoint, an MCP tool or its description, a response shape, a configuration
variable, or a default. Use the standard groupings — `Added`, `Changed`, `Deprecated`, `Removed`,
`Fixed`, `Security` — and write for someone consuming the API, not someone reading the diff:

```markdown
### Fixed
- MCP: `/mcp` and `/mcp/` are both served directly; neither redirects.
```

Refactors, test changes, CI, formatting, and dependency bumps do **not** get an entry. The git
history already records them.

> While `3.0.0b1` is still unreleased, fixes to code that has never shipped are folded into that
> release's `Added` section rather than listed under `Fixed` — there is no released behavior to
> have fixed. Once the beta ships, use the groupings normally.

## Versioning

[Semantic Versioning](https://semver.org/spec/v2.0.0.html), with one house rule:

**MAJOR is pinned to the served URL prefix.** `/v3` ↔ `3.y.z`, always.

| Change | Version | URL |
|---|---|---|
| Add an endpoint, MCP tool, or optional field | MINOR — `3.1.0` | `/v3` |
| Fix a bug without changing the contract | PATCH — `3.0.1` | `/v3` |
| Break the REST or MCP contract | MAJOR — `4.0.0` | new prefix `/v4` |

So a breaking change is never a silent break under `/v3`: it is a new prefix served alongside the
old one. This keeps `api_version` predictive of the URL, and matches the clean break v3 already
made from v1/v2.

Pre-releases use [PEP 440](https://peps.python.org/pep-0440/) spelling so the Python package
version and the git tag agree: `3.0.0b1` → tag `v3.0.0b1`; `3.0.0rc1` → tag `v3.0.0rc1`.

**The version lives in exactly one place: `version` in [pyproject.toml](pyproject.toml).**
Everything else derives from it — `idc_api.__version__` and `core/version.py:package_version()`
both read the installed distribution metadata. Never hardcode it a second time.

## Releasing

Cutting a release is a **maintainer** task that deploys to production. The runbook — including the
v3 beta plan — lives with the deploy machinery it depends on:
[dev/deployment.md § Cutting a release](dev/deployment.md#cutting-a-release).

Two things every contributor should know:

> [!IMPORTANT]
> **Pushing a `v*` tag deploys to production.** [promote.yml](.github/workflows/promote.yml)
> triggers on `push: tags: ["v*"]`, and that glob matches pre-release tags too — `v3.0.0b1` goes
> to prod exactly like `v3.0.0`. Never create a `v*` tag for bookkeeping, and be careful with
> `git push --tags`, which can fire a deploy from a stale local tag.

And you cannot tag a release without bumping `pyproject.toml` first: `promote.yml` asserts that
the tag equals `"v"` + the packaged version, and fails before the reviewer gate if they disagree.

## Reporting security issues

Please don't open a public issue for a vulnerability — use GitHub's private reporting flow, as
described in [SECURITY.md](SECURITY.md).
