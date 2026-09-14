# Changelog

All notable changes to OpenCollab-Eval are recorded in this file.

## [Unreleased]

## [0.7.0] - 2026-09-14

### Added

Paired with OpenCollab 0.7.0 and its current public runtime/tool interface.

Reusable server-local evaluation packaging, shared provider request limits, native Single configuration, and named collaboration workflows.

### Changed

Removed Eval's dedicated `run_tests` tool and its automatic runner selection,
command rewriting, and model-facing GREEN/RED reports. Collaboration workflows
execute native project commands through OpenCollab's Bash interface. Evaluation
observes actual command results for existing workflow evidence checks, and
mechanical candidate comparisons replay the recorded command unchanged.

### Fixed

Completed context-window and recovery-source propagation, bounded retries for
idempotent setup operations, candidate capture with read-only ignore views,
and proof-bound Python/JavaScript candidate-failure classification. Role limits
are resolved per invocation, and explicit provider recovery time reaches the
outer role wait.

Adapted default Single and team configurations to the current OC built-in tools.
Exact-target research evidence is collected from native Bash execution. Final verification
receives all role reports, cleanup failures retain captured candidates for
review, and truncated diff output is recovered through a complete file transfer.

Public runtime dependencies and Conda activation are prepared before generation. Failed candidate captures preserve recovery resources, completed response evidence retains known usage, and official evaluation receives a configurable container-binding interval.

Bulk dependency operations use the workspace-transfer timeout instead of the
short Docker control timeout. Test evidence preserves complete target paths,
invalidates overlapping evidence after failed reruns, and rejects missing,
truncated, or mismatched execution output.

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

[Unreleased]: https://github.com/RISE-X-Lab/OpenCollab-Eval/compare/v0.7.0...HEAD
[0.5.1]: https://github.com/RISE-X-Lab/OpenCollab-Eval/releases/tag/v0.5.1
[0.5.0]: https://github.com/RISE-X-Lab/OpenCollab-Eval/releases/tag/v0.5.0

[0.7.0]: https://github.com/RISE-X-Lab/OpenCollab-Eval/compare/v0.5.1...v0.7.0
