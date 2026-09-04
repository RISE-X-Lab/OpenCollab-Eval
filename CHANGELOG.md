# Changelog

All notable changes to OpenCollab-Eval are recorded in this file.

## [Unreleased]

## [0.5.1] - 2026-09-04

### Fixed

- Accepted legacy per-instance sidecars that identify a task with `task` or
  `task_id`, while normalizing valid patch digests without weakening conflict
  or format checks.
- Preserved eligible, quiescent candidates when OpenCollab ends a run through
  the protected budget reserve or provider output truncation; incomplete
  evidence remains a technical failure.

## [0.5.0] - 2026-08-13

### Added

- Added trusted candidate construction with evaluator-owned Git metadata, fresh official evaluation workspaces, patch identity records, and quiescence checks.
- Added OpenCollab, OpenHands, and Claude Code solver adapters for SWE-bench Pro-Lite generation and official evaluation.
- Added deterministic end-to-end CI that exercises a fake model service, SSH transport, candidate extraction, Docker evaluation, evidence verification, and resource cleanup.
- Added Responses-compatible model transport support, reasoning continuity checks, runtime source binding, final report generation, and token-cost summaries.
- Added bilingual project, architecture, operation, integrity, task-format, CLI, troubleshooting, and contribution documentation.

### Changed

- Aligned the package version with OpenCollab 0.5.0 and raised the minimum compatible OpenCollab version to 0.5.0.
- Bound CI to the immutable OpenCollab 0.5.0 release commit `963585611ad2a1d0c1fc7f4ba0043af5a3d860bb`.
- Separated solver outcomes from evaluation failures so incomplete evidence, zero tests, workspace mutation, and identity drift remain technical failures.

### Fixed

- Corrected candidate, runtime, transport, report, and official-test evidence handling discovered during SWE-bench Pro-Lite evaluation.
- Prevented stale checkpoints, malformed streaming responses, duplicate model starts, cleanup races, and parser-specific evidence gaps from producing untrusted terminal results.
- Raised the deterministic SWE test budget so OpenCollab 0.5.0 can preserve the configured output allowance after conservative input reservation.

[Unreleased]: https://github.com/RISE-X-Lab/OpenCollab-Eval/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/RISE-X-Lab/OpenCollab-Eval/releases/tag/v0.5.1
[0.5.0]: https://github.com/RISE-X-Lab/OpenCollab-Eval/releases/tag/v0.5.0
