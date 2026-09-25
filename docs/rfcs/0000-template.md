---
number: 0
title: RFC template
status: Template
authors: []
components: []
created: 2026-09-25
updated: 2026-09-25
supersedes: []
superseded_by: []
discussion:
---

# RFC-NNNN: Title

<!-- Copy this file to docs/rfcs/NNNN-short-title.md and fill in every section.
     Delete a section only if it truly does not apply, and say why in one line. -->

## Summary

One paragraph: what this RFC proposes and the outcome it enables.

## Motivation

The problem, who has it, and why now. Link the PRD section or earlier RFCs it builds on.

## Goals and non-goals

- **Goals:** what must be true when this is done.
- **Non-goals:** what this deliberately does not cover.

## Design

The proposal in enough detail to implement it: components, interfaces (headers, C ABI, wire formats), data flow, and state. Number subsections (§1, §2, …) so code and pull requests can cite them.

## Failure handling

What can fail, how each failure is detected, what the caller sees (typed errors, events), and what state is left behind.

## Observability

Metrics, trace events and debug checks this design adds, and at which telemetry build level (`metrics`, `trace`, `debug`).

## Performance

Expected costs and targets, with the benchmark that will measure them.

## Testing

Unit, integration and multi-process tests; which run in everyday CI and which need rented hardware.

## Alternatives considered

Other designs and why this one wins.

## Rollout

Milestone, feature flags, migration or compatibility concerns.

## Open questions

Anything unresolved at review time.
