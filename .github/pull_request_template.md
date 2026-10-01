## What and why

<!-- One or two sentences. Link the issue, RFC or ADR this implements, e.g. "Implements RFC-0002 §4". -->

## Design

- [ ] This change does **not** need an RFC (see [when you need an RFC](https://github.com/OstiaHQ/ostia/blob/main/docs/README.md#when-you-need-an-rfc)), **or** it implements an accepted RFC: RFC-____
- [ ] Any new architectural decision is recorded as an ADR

## Checks

- [ ] The PR title follows the commit convention in [CONTRIBUTING.md](https://github.com/OstiaHQ/ostia/blob/main/CONTRIBUTING.md#commit-messages), e.g. `✨ feat(fabric): …`
- [ ] I have signed the [CLA](https://github.com/OstiaHQ/ostia/blob/main/docs/legal/individual-cla.md) (the bot asks on your first pull request)

- [ ] Tests added or updated
- [ ] GPU-affecting change ([ADR-0014](https://github.com/OstiaHQ/ostia/blob/main/docs/adr/0014-on-demand-remote-test-runs.md) rule 2): a maintainer reviewed the diff at the head SHA, ran `ostia-dev remote k8s --context <ctx> --profile l4 --suite gpu --ref pr/<n>` (plus `--suite bench-smoke` for `*/bench/`) and pasted the summary line here ([remote-runs.md](https://github.com/OstiaHQ/ostia/blob/main/docs/guides/remote-runs.md)), or this PR doesn't need one
- [ ] Docs updated (component README, guides, PRD if scope changed)
- [ ] `pixi run ostia-dev docs index --check` passes if RFCs or ADRs changed
