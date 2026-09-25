# Governance

## Roles

- **Project owner:** Alireza Shateri (@ShAlireza). Holds the rights granted under the [CLA](docs/legal/individual-cla.md), sets product direction through the [PRD](docs/product/prd.md), and has the final say when consensus can't be reached.
- **Maintainers:** listed in [MAINTAINERS.md](MAINTAINERS.md). They review and merge pull requests, triage issues and accept RFCs for the areas they maintain.
- **Contributors:** anyone who has had a pull request merged, or who helps through issues, discussions and reviews.

## How decisions are made

- **Everyday changes** are decided in pull request review: one maintainer approval and passing checks.
- **Designs** go through the RFC process in [docs/README.md](docs/README.md). An RFC is accepted when a maintainer of each affected component approves it and no maintainer objects after the review period.
- **Architectural decisions** are recorded as ADRs, so the reasoning stays findable after the discussion ends.
- **Disagreements** are resolved by discussion first. If maintainers still disagree, the project owner decides and records the decision and its reasoning in an ADR.

## Becoming a maintainer

Contributors who have made sustained, high-quality contributions and reviews in an area can be nominated by any maintainer. The nomination is a pull request adding them to [MAINTAINERS.md](MAINTAINERS.md); it is accepted with the project owner's approval and no objection from existing maintainers within 5 working days. Maintainers who are inactive for six months move to an emeritus list and can return by asking.

## Open core

The public repository (`OstiaHQ/ostia`) is licensed under Apache-2.0. Commercial editions of Layers 2 and 3 are developed separately and depend only on released versions of the public repository. What belongs in the open edition is decided in each layer's RFC, and nothing already released under Apache-2.0 is taken back out of it.

## Changes to this document

Changes to governance are proposed as pull requests and need the project owner's approval.
