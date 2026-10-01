# Contributing to Ostia

Thanks for your interest in Ostia. This guide explains how to propose changes, what we expect in a pull request, and how review works. By taking part you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Ostia is building its M0 foundations. Useful contributions right now are feedback on the [PRD](docs/product/prd.md) and on open RFCs, and work on the M0 build, CI and tooling.

## Ways to contribute

- **Ask a question or share an idea:** use [GitHub Discussions](https://github.com/OstiaHQ/ostia/discussions). See [SUPPORT.md](SUPPORT.md).
- **Report a bug:** open an issue with the bug report template. Include your hardware and topology; many bugs only appear on specific GPU, NIC or NVLink setups.
- **Report a security problem:** do **not** open a public issue. Follow [SECURITY.md](SECURITY.md).
- **Propose a design:** anything that needs an RFC (see below) starts as an RFC pull request, not as code.
- **Improve the docs:** typo fixes and clarifications are always welcome as small pull requests.

## Before you start

1. **Check existing issues, discussions and RFCs** in the [docs index](docs/README.md#index) so work isn't duplicated.
2. **Decide whether you need an RFC.** You need one for a new component, a change to a public API, the C ABI or a wire protocol, a change to a contract between layers, a new third-party dependency, or the design of a hot path. The full rule and the review lifecycle are in [docs/README.md](docs/README.md).
3. **For anything larger than a small fix, open an issue or discussion first** and say you plan to work on it.

## Pull requests

- Keep each pull request to one logical change. Large features land as a series of reviewable pull requests that follow their RFC.
- Fill in the pull request template, including which RFC the change implements.
- Add or update tests for behaviour changes, and update the docs in the same pull request.
- Follow the code style in [ADR-0013](docs/adr/0013-code-style.md) and the comment rules in [ADR-0015](docs/adr/0015-code-comments.md): comments explain why, invariants and hazards, not what the next line does.
- A pull request that touches GPU code (ADR-0014 rule 2 lists what counts) needs a GPU run before it merges. A maintainer reviews the diff, runs the `gpu` suite on a GPU node with `ostia-dev remote` ([docs/guides/remote-runs.md](docs/guides/remote-runs.md)), and posts the summary line. You don't need a GPU cluster: ask in a comment.
- CI must pass. Every job runs a pixi task you can run locally, and its summary shows the command that reproduces it; `pixi run check` covers the required subset (RFC-0001 §4.1).
- A maintainer reviews every pull request. Expect a first response within 5 working days. Address review comments with new commits; the pull request is squash-merged, so the history stays clean.

## Commit messages

Commits and pull request titles use [Conventional Commits](https://www.conventionalcommits.org/) prefixed with the matching [gitmoji](https://gitmoji.dev/):

```
<emoji> <type>(<scope>): <summary in the imperative, lower case, no final period>

<optional body: what and why, wrapped at 72 characters>

<optional footers: Implements RFC-0005 §4, Fixes #12, BREAKING CHANGE: ...>
```

For example: `✨ feat(fabric): add UCX backend for GPUDirect RDMA`

| Emoji | Type | Use for |
| --- | --- | --- |
| ✨ | `feat` | A new feature |
| 🐛 | `fix` | A bug fix |
| 🔒️ | `fix` | A security fix |
| ⚡️ | `perf` | A performance improvement |
| ♻️ | `refactor` | Code change that neither fixes a bug nor adds a feature |
| 📝 | `docs` | Documentation, RFCs and ADRs |
| ✅ | `test` | Adding or fixing tests |
| 📦️ | `build` | Build system, packaging, dependencies |
| 👷 | `ci` | CI configuration |
| 🎨 | `style` | Formatting only |
| 🔧 | `chore` | Tooling, configuration, housekeeping |
| ⏪️ | `revert` | Reverting an earlier commit |
| 💥 | any type with `!` | A breaking change, e.g. `💥 feat(exchange)!: rename shuffle options`; also add a `BREAKING CHANGE:` footer |

**Scopes** are the component names: `fabric`, `exchange`, `runtime`, `query`, `telemetry`, plus `build`, `ci`, `docs`, `rfc` and `adr`. For example: `📝 docs(rfc): add RFC-0005 ostia-fabric`.

## Contributor License Agreement

Ostia is open core: the open-source edition is Apache-2.0, and the project owner also offers commercial editions. To keep that possible, every contributor signs a Contributor License Agreement (CLA) once, before their first pull request is merged. You keep the copyright in your contributions; the CLA grants the project a license to use them, including in commercially licensed editions.

- **Individuals:** read the [Individual CLA](docs/legal/individual-cla.md). On your first pull request, a bot asks you to sign by posting a comment. It takes one comment and covers all future contributions.
- **Companies:** if your employer owns your work, your employer signs the [Corporate CLA](docs/legal/corporate-cla.md) and emails it to alirezashateri7@gmail.com, listing the employees it authorizes.

## Development setup

Install [pixi](https://pixi.sh), then follow [docs/guides/building.md](docs/guides/building.md): `pixi install`, `pixi run build`, `pixi run test`. Before you open a pull request, run `pixi run check`, which runs what CI requires. `pixi run hooks` installs a pre-commit hook that runs `pixi run lint`. To add or grow a component, see [docs/guides/adding-a-component.md](docs/guides/adding-a-component.md).

## For maintainers

Repository settings that back this process are listed in [MAINTAINERS.md](MAINTAINERS.md#repository-settings).
