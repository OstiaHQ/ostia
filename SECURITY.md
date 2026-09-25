# Security policy

## Reporting a vulnerability

Please report security problems privately. **Do not open a public issue, discussion or pull request.**

1. **Preferred:** use GitHub's [private vulnerability reporting](https://github.com/OstiaHQ/ostia/security/advisories/new): open the repository's **Security** tab and choose **Report a vulnerability**.
2. **Alternative:** email alirezashateri7@gmail.com with the subject `[ostia security]`.

Include what you found, the affected component (fabric, exchange, runtime, query, telemetry), steps or code to reproduce it, and the hardware and network setup if it matters.

## What to expect

- An acknowledgement within 3 working days.
- An initial assessment within 10 working days, including whether we consider it a vulnerability and its severity.
- A fix, or a plan with a timeline, before any public disclosure. We coordinate the disclosure date with you and credit you in the advisory unless you prefer otherwise.

## Supported versions

Ostia has no releases yet. Until the first release, only the `main` branch is supported. This section will list supported release lines once they exist.

## Scope

Ostia moves data between machines and exposes network endpoints, so we are especially interested in: memory-safety bugs in transports and buffer handling, ways for a peer to read or corrupt memory it shouldn't, denial of service through the control or data plane, and weaknesses in how peers identify each other.
