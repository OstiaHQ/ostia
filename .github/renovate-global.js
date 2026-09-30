// Global configuration for self-hosted Renovate (RFC-0001 §2.4). The hosted Renovate app
// cannot run `pixi lock`, which updating pixi.lock needs, so Renovate runs from
// .github/workflows/renovate.yml with pixi allowed here. Repository settings are in
// renovate.json.
module.exports = {
  platform: "github",
  repositories: ["OstiaHQ/ostia"],
  onboarding: false,
  requireConfig: "required",
  binarySource: "install",
  allowedUnsafeExecutions: ["pixi"],
  // The kubectl bump refills its sha256 table (RFC-0005 §4.1).
  allowedCommands: ["^pixi run ostia-dev remote k8s kubectl --update-shas$"],
};
