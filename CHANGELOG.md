# Changelog

All notable changes to OpenCollab-Eval are recorded in this file.

## [Unreleased]

The batch driver gives every (instance, arm) run an id, writes it into the run's
`manifest.jsonl` row, and passes it to the generator as `OPENCOLLAB_RUN_ID`. The
evaluator hands it to OpenCollab's `run_id=` when the installed runtime accepts
it, and the metrics row records it, so the manifest row, the trajectory and the
metrics row join on one id. With OpenCollab 0.9.0 the run proceeds and its
trajectory keeps the runtime's own id.

## [0.9.1] - 2026-10-05

Paired with OpenCollab 0.9.1. The supported runtime dependency remains
`opencollab>=0.9,<0.10`.

Generation and evaluation preserve structured facility failures separately
from valid solver outcomes. Observation storage failures retain healthy
execution, failed candidate persistence retains recovery resources, and batch
cancellation saves completed results. Duo fallback candidates retain their
official scoring eligibility when completed discarded roles only fail during
cleanup. SWE-bench pass-to-pass tests follow the official allowed-skip rules.

The Terminal container backend tracks foreground command completion and process
group cancellation before candidate delivery. Task-specific verifier preparation
runs in an isolated scoring copy, preserves recovery evidence, and shares the
existing task timeout with the official verifier. Preparation failures retain a
score-only retry path, including cancellation receipts across Python versions.

External solvers preserve preinstalled runtime dependencies, and failed evidence
copies retain their source directories. Provider diagnostics use the effective
configuration. Monitoring reads native journals and discovers agent and workflow
snapshots. Usage reports retain unknown cache fields, include cache write costs,
and calculate stable confidence intervals. Prediction summaries use the attempts
actually selected for export.

## [0.9.0] - 2026-10-02

Paired with OpenCollab 0.9.0. Installation requirements, remote runtime
preparation, CI, and installed-wheel checks use the same supported 0.9.x
runtime range. CI pins the immutable OpenCollab 0.9.0 release commit.

The complete ICLR integration adds shared role task text, native team and
scripted collaboration, independent candidates, and generation batches.
Official SWE-bench 5.0.2 outcomes are associated with their exact attempts,
while reports retain selected and historical attempt consumption separately.

Declarative experiment specifications cover task sampling, batch execution,
retries, replacements, observations, reports, and offline analysis. Active
replacement selection is shared between reporting and prediction export.
Existing candidate preservation, quiescence, and evidence checks remain active.

## [0.8.3] - 2026-10-01

Paired with OpenCollab 0.8.3. This release updates the package version, release
documentation, and pinned OpenCollab CI commit. OpenCollab-Eval's evaluator
implementation follows the current main branch.

## [0.8.2] - 2026-09-28

Task prompts include issue discussion hints when present. Standalone runs record
their trajectory path even after a runtime failure, and workflow output errors
produce a stopped status in the run metrics. Container cleanup uses the
configured Docker timeout.

Standalone and workflow generation now share optional default step and token
caps and a 30-minute wall timeout. The batch runner determines completion from
non-empty candidate patches while retaining technical supervisor failures.

Standalone and workflow generation record the same LLM transport settings,
including stream-chat selection, alongside the model configuration. Context
window records include the supported GPT-5.6 Luna, DeepSeek V4.1 Flash, and
Qwen 3.8 Flash models.

## [0.8.1] - 2026-09-27

Directory traversal transfers descriptor ownership before closing the parent.
A termination signal arriving immediately after that close preserves the
original interruption and closes the current child descriptor.

Tests are grouped by generation, evaluation, orchestration, transport,
packaging, and end-to-end behavior. Shared preparation uses explicit support
packages, and copied test suites preserve package imports outside the checkout.
Repeated setup functions are shared within their behavior area.

The installed-wheel checks, deterministic evaluation scripts, and cross-project
coverage references use the organized test paths. Wheels contain runtime files,
while source distributions retain the complete development tests.

## [0.8.0] - 2026-09-27

Standalone generation selects Base through OpenCollab's named profile API,
currently resolving to Single2. CLI aliases and the single-agent evaluation
entry use the same implementation and store its concrete profile name.
Existing records retain their original profile attribution during reuse checks.
The previous Single-specific model wrapper has been removed.

OpenCollab-Eval now requires OpenCollab 0.8.x for public profile resolution.

## [0.7.1] - Unreleased

Duo uses a single OpenCollab built-in workflow with task-oriented role prompts
and complete file evidence. The G22 command remains an alias for Duo.

OpenCollab-Eval requires OpenCollab 0.7.1 or a later 0.7.x version, which
provides the public workflow, command-evidence, and patch-parsing APIs used
by the evaluator.

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

[0.7.1]: https://github.com/RISE-X-Lab/OpenCollab-Eval/compare/v0.7.0...HEAD
[0.5.1]: https://github.com/RISE-X-Lab/OpenCollab-Eval/releases/tag/v0.5.1
[0.5.0]: https://github.com/RISE-X-Lab/OpenCollab-Eval/releases/tag/v0.5.0

[0.7.0]: https://github.com/RISE-X-Lab/OpenCollab-Eval/compare/v0.5.1...v0.7.0

[0.9.0]: https://github.com/RISE-X-Lab/OpenCollab-Eval/compare/v0.8.3...v0.9.0

[0.9.1]: https://github.com/RISE-X-Lab/OpenCollab-Eval/compare/v0.9.0...v0.9.1
