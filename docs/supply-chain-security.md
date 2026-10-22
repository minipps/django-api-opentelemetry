# CI/CD supply-chain controls

The workflow code and dependency configuration are reviewed through pull
requests. Publishing uses a separate job with OIDC and the exact distributions
checked by CI. The [development guide](development-and-ci.md) describes the
workflow commands and release gate.

## Repository settings

The following settings were applied and read back through the GitHub API on
2026-10-22. They are repository settings, not settings activated by merging YAML:

- Dependabot vulnerability alerts and automatic security-update PRs are enabled.
  Secret scanning and secret push protection were already enabled.
- Actions must use full commit SHAs. The allowlist contains only
  `actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`,
  `docker/setup-buildx-action`, `docker/build-push-action`, and
  `pypa/gh-action-pypi-publish`. Updating their SHAs remains allowed; introducing
  a different action requires an administrator to extend the allowlist.
- The default workflow token is read-only and cannot approve PRs. All external
  contributors need approval before running fork PR workflows.
- The `pypi` and `testpypi` environments accept only tags matching `v*`, never
  branch refs. Existing PyPI Trusted Publisher identities remain unchanged.
- Only repository administrators can create `v*` release tags. A separate
  ruleset blocks updating and deleting those tags, including for administrators.
  An administrator must explicitly change that ruleset for an emergency repair.

These controls depend on trusted repository administrators, GitHub, the approved
action publishers and package registries. Hash pinning prevents a changed tag
from silently replacing reviewed content; it does not establish that the
original pinned content was safe. Keep administrator accounts protected and
review dependency PRs before merging.

## Dependabot groups

The [Dependabot configuration](../.github/dependabot.yml) takes effect on the
default branch after this PR merges. Checks run weekly. Version updates have a
seven-day cooldown; security updates are not delayed by that cooldown.

| Manifest | Groups |
| --- | --- |
| `pyproject.toml` and `uv.lock` | Django runtime, Ninja/Pydantic, DRF, OpenTelemetry, development tools and remaining transitive dependencies. |
| `docs/requirements.txt` | Documentation tools. |
| `.github/workflows/` | SHA-pinned GitHub Actions. |
| `Dockerfile` | Digest-pinned Python and uv images. |
| `compose.yaml` | Digest-pinned collector image. |

Separate `security-updates` groups mirror the Python, documentation and Actions
version groups. OpenTelemetry API, SDK, exporters and contrib instrumentation
are updated together because their versions are coupled. The fallback group
includes transitive packages rather than leaving them outside update coverage.

Alerts are individual advisories in the repository Security tab; groups control
the update PRs, not alert routing or notification preferences. Docker and Docker
Compose get version/digest update PRs; GitHub does not provide Dependabot
security-update PRs for those ecosystems. No automatic merging is configured.
Python image updates retain each matrix stage's major/minor version; digest
refreshes stay automatic, while adding a new Python series is a reviewed change.

The BuildKit builder and Dockerfile frontend are also pinned by digest. Review
their inline pins explicitly when upgrading the build toolchain; Dependabot
does not manage every image reference embedded in workflow inputs or comments.

GitHub documents the distinction between [version and security groups][groups],
the [cooldown behavior][cooldown], and [ecosystem support][ecosystems].

[groups]: https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#groups
[cooldown]: https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#cooldown
[ecosystems]: https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories
