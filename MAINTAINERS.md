# Maintainers

| Name | GitHub | Areas |
| --- | --- | --- |
| Alireza Shateri | @ShAlireza | All components (project owner) |

Code ownership for review requests is in [.github/CODEOWNERS](.github/CODEOWNERS). How people become maintainers is described in [GOVERNANCE.md](GOVERNANCE.md#becoming-a-maintainer).

## Emeritus

None yet.

## Repository settings

These settings back the contribution process. They live in GitHub, not in files, so check them when setting up or auditing the repository. All of these were set on 2026-09-25, when the repository went public.

**Collaboration**

- [x] **Discussions** enabled with the default categories (Announcements, General, Ideas, Polls, Q&A, Show and tell)
- [x] **Squash merging only**; the squash commit takes the pull request title and the commit messages, so titles must follow the [commit convention](CONTRIBUTING.md#commit-messages)
- [x] Head branches deleted automatically after merge; "Update branch" button enabled
- [x] Wiki and Projects turned off, so docs stay in the repository
- [x] Triage and component labels (`needs-triage`, `design`, `rfc`, `adr`, `breaking-change`, `component: fabric`, …) and topics

**Ruleset `main`** (Settings → Rules → Rulesets)

- [x] Pull requests required; squash is the only merge method; review threads must be resolved
- [x] **0 approvals required while there is one maintainer** (you can't approve your own pull request). Raise it to 1 when a second maintainer joins
- [x] Required status check: `cla`
- [x] Linear history; force pushes and branch deletion blocked
- [x] Repository admins may bypass the rules only when merging a pull request, never with a direct push

**Security**

- [x] **Private vulnerability reporting** enabled, the preferred channel in [SECURITY.md](SECURITY.md)
- [x] **Secret scanning** and **push protection** enabled
- [x] **Dependabot alerts** and **Dependabot security updates** enabled; version updates for GitHub Actions in [`.github/dependabot.yml`](.github/dependabot.yml)
- [x] **CodeQL** default setup enabled
- [x] **OpenSSF Scorecard** runs weekly and on pushes to `main` ([`.github/workflows/scorecard.yml`](.github/workflows/scorecard.yml))
- [x] **Actions** get a read-only `GITHUB_TOKEN` by default and can't approve pull requests; each workflow requests only the permissions it needs, and third-party actions are pinned by commit SHA

**CLA**

- [x] The `cla-signatures` branch holds signatures and is left unprotected so the CLA workflow can write to it; the `main` ruleset doesn't cover it

**With M0** (once there is code and CI):

- [ ] Add build dependencies to Dependabot and required CI checks to the `main` ruleset
- [ ] Organization setting: require two-factor authentication for members (Organization settings → Authentication security; not available through the API)
